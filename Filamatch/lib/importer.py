"""Import catalog filaments into a Fusion 360 design as materials.

This module stays import-safe outside Fusion 360: ``adsk`` is never imported at
module scope, and every public function accepts duck-typed Fusion objects so the
logic can be exercised in a plain Python process.  Inside Fusion the caller
passes the active ``adsk.core.Design``; outside Fusion the self-test at the
bottom of this file uses lightweight fakes.

Typical use::

    from .catalog import load_catalog
    from .importer import import_catalog_entries

    entries = load_catalog()
    materials = import_catalog_entries(design, entries)

``design.materials.addByCopy(template, name)`` accepts the target name directly;
:func:`import_filament` additionally enforces the rename through
``Material.name`` (which is writable for materials that live in a Design) and
logs a warning if Fusion refuses.

Fusion API compatibility
------------------------
The material property collection was accessed as ``material.physicalProperties``
in older Fusion 360 releases; current Autodesk Fusion releases expose it as
``material.materialProperties`` (an ``adsk.core.Properties`` collection).
:func:`set_material_density` supports both.  Density values are stored in the
property's own units (current Fusion uses ``KilogramPerCubicMeter``, i.e. an ABS
density of 1060.0), so the catalog's g/cm^3 values are converted before writing
and converted back for verification/logging.
"""

from __future__ import annotations

import logging
import math
import re
import sys
import traceback
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import fusion_helpers as _fh

if TYPE_CHECKING:  # pragma: no cover - avoids a hard runtime import dependency
    from .catalog import FilamentEntry

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: Name of the physical property that receives the catalog density.
DENSITY_PROPERTY_NAME = "Density"

#: Relative read-back tolerance before a density mismatch is reported.
DENSITY_READBACK_TOLERANCE = 0.005

#: Multipliers from g/cm^3 (catalog values) to the unit used by the Fusion
#: Density property.  Current Fusion stores density in kg/m^3 (ABS = 1060.0),
#: older documents may report g/cm^3 or another unit.
_DENSITY_UNIT_FACTORS = {
    "grampercubiccentimeter": 1.0,
    "kilogrampercubicmeter": 1000.0,
    "grampercubicmillimeter": 0.001,
    "kilogrampercubiccentimeter": 0.001,
    "grampercubicmeter": 1000000.0,
    "milligrampercubicmillimeter": 1.0,
    "tonpercubicmeter": 1.0,
    "poundpercubicinch": 0.036127292,
    "ouncepercubicinch": 0.578036672,
}

#: Factor assumed when the Density property does not report its units.
DEFAULT_DENSITY_UNIT_FACTOR = 1.0


class MaterialImportError(RuntimeError):
    """Raised when a catalog filament cannot be imported into the active design."""


class TemplateMaterialNotFoundError(MaterialImportError):
    """Raised when no template material with the requested name can be found.

    The message names the requested template and explains how to fix the
    problem: load the Fusion 360 Material Library (or the manufacturer
    ``.adsklib``) or change ``template_material`` in ``catalog/filaments.json``.
    """


#: Template materials found in an application library, keyed by casefolded
#: name: ``(app, library material)``.  Library materials outlive designs, so one
#: lookup serves every import; entries are revalidated before use.
_TEMPLATE_CACHE: Dict[str, Tuple[Any, Any]] = {}

#: Names not found in any library, keyed by casefolded name: ``(app, library
#: count, name sample)``.  A miss is only trusted while the number of loaded
#: libraries is unchanged.
_TEMPLATE_MISSES: Dict[str, Tuple[Any, int, Tuple[str, ...]]] = {}


# ---------------------------------------------------------------------------
# Fusion API access helpers
# ---------------------------------------------------------------------------

_resolve_fusion_app = _fh.resolve_fusion_app
_collection_items = _fh.collection_items
_item_by_name = _fh.item_by_name



def _material_name(material: Any) -> Optional[str]:
    """Return a stripped Fusion material name, or ``None`` when it cannot be read."""
    try:
        name = material.name
    except Exception:
        logger.exception("Could not read the name of material %r.", material)
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip()


def _rename_material(material: Any, desired_name: str) -> bool:
    """Rename a Fusion material, enforcing the catalog id as the material name.

    ``Materials.addByCopy`` already accepts a target name, but Fusion has been
    observed to keep the template name in some library configurations.  For
    materials stored in a Design the ``Material.name`` property is writable, so
    the rename is applied defensively here.  A failed rename is logged and does
    not abort the import - the material is still usable.

    Args:
        material: Material returned by ``design.materials.addByCopy``.
        desired_name: Catalog id the material should be named after.

    Returns:
        ``True`` when the material now carries ``desired_name``, ``False``
        otherwise (the mismatch is logged as a warning).
    """
    current_name = _material_name(material)
    if current_name == desired_name:
        return True
    try:
        material.name = desired_name
    except Exception:
        logger.exception(
            "Fusion refused to rename the imported material from %r to %r; keeping the "
            "Fusion-assigned name.",
            current_name,
            desired_name,
        )
        return False

    renamed = _material_name(material)
    if renamed == desired_name:
        logger.debug("Renamed imported material from %r to %r.", current_name, desired_name)
        return True
    logger.warning(
        "After renaming, the imported material is still named %r instead of %r. "
        "The material was imported but its name will not match the catalog id.",
        renamed,
        desired_name,
    )
    return False


def _design_materials_or_none(design: Any) -> Any:
    """Return ``design.materials`` or ``None``, logging any Fusion API failure."""
    if design is None:
        # Favorites work without a design; this is expected, not a problem.
        logger.debug("No Fusion design was supplied; searching only application libraries.")
        return None
    try:
        materials = design.materials
    except Exception:
        logger.exception("Could not access design.materials while locating the template material.")
        return None
    if materials is None:
        logger.warning(
            "design.materials is unavailable; searching only application material libraries."
        )
    return materials


def _require_design_materials(design: Any) -> Any:
    """Return ``design.materials``, raising MaterialImportError when unavailable."""
    if design is None:
        raise MaterialImportError("A Fusion design is required to import materials, got None")
    try:
        materials = design.materials
    except Exception as exc:
        logger.exception("Could not access design.materials for material import.")
        raise MaterialImportError(
            "Unable to access the active design's materials collection: {}".format(exc)
        ) from exc
    if materials is None:
        raise MaterialImportError(
            "The active design's materials collection is unavailable (design.materials is None)"
        )
    return materials


def _material_libraries(app: Any) -> List[Any]:
    """Return the application's material libraries as a list (empty outside Fusion)."""
    if app is None:
        return []
    try:
        libraries = app.materialLibraries
    except Exception:
        logger.exception(
            "Could not access app.materialLibraries while locating the template material."
        )
        return []
    if libraries is None:
        logger.warning(
            "app.materialLibraries is unavailable; the template must exist in the design."
        )
        return []
    return _collection_items(libraries, "app.materialLibraries")


def _library_label(library: Any, index: int) -> str:
    """Return a display label for a material library."""
    try:
        name = library.name
    except Exception:
        logger.exception("Could not read the name of material library #%d.", index + 1)
        return "library #{}".format(index + 1)
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "library #{}".format(index + 1)


def _library_materials(library: Any, label: str) -> Any:
    """Return ``library.materials`` or ``None`` when it cannot be accessed."""
    try:
        materials = library.materials
    except Exception:
        logger.exception("Could not access the materials of material library %r.", label)
        return None
    if materials is None:
        logger.warning("Material library %r exposes no materials collection; skipping it.", label)
    return materials


def _find_case_insensitive(
    collection: Any, wanted_casefolded: str, available_names: List[str], label: str
) -> Any:
    """Return the first material whose name matches case-insensitively.

    Names encountered are appended to ``available_names`` so callers can build
    an actionable "not found" message.
    """
    for material in _collection_items(collection, label):
        name = _material_name(material)
        if name is None:
            continue
        available_names.append(name)
        if name.casefold() == wanted_casefolded:
            return material
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_template_material(design: Any, template_name: str, app: Any = None) -> Any:
    """Find the Fusion material that a catalog filament should be copied from.

    The lookup is defensive because material libraries vary between Fusion
    installations and the active design may not contain the template material:

    1. exact match in ``design.materials``;
    2. explicit ``app`` argument, or ``adsk.core.Application.get()`` otherwise;
    3. exact match in every library listed by ``app.materialLibraries``;
    4. case-insensitive scan of the design and every library.

    Library hits are cached per name (and revalidated); a name missing from
    every library is not scanned for again until the library count changes.
    :func:`clear_template_cache` forgets both.

    Args:
        design: Active Fusion design (anything exposing ``materials``).
        template_name: Name of the template material, e.g. ``"PLA Plastic"``.
        app: Optional Fusion ``Application``.  When omitted the running
            application is resolved automatically.

    Returns:
        The matching template material object.

    Raises:
        MaterialImportError: If ``template_name`` is empty.
        TemplateMaterialNotFoundError: If no matching material can be found.
    """
    if not isinstance(template_name, str) or not template_name.strip():
        raise MaterialImportError(
            "template_name must be a non-empty string, got {!r}".format(template_name)
        )
    requested = template_name.strip()

    design_materials = _design_materials_or_none(design)
    if design_materials is not None:
        material = _item_by_name(design_materials, requested, "active design materials")
        if material is not None:
            logger.debug("Using template material %r from the active design.", requested)
            return material

    if app is None:
        app = _resolve_fusion_app()

    wanted = requested.casefold()
    cached = _cached_template(app, wanted)
    if cached is not None:
        logger.debug("Using cached template material %r.", requested)
        return cached
    library_count = _library_count(app)
    miss = _TEMPLATE_MISSES.get(wanted)
    if miss is not None and miss[0] is app and miss[1] == library_count:
        # Same libraries as the last full scan: only the design can have changed.
        available_names: List[str] = list(miss[2])
        if design_materials is not None:
            material = _find_case_insensitive(
                design_materials, wanted, available_names, "active design materials"
            )
            if material is not None:
                return material
        raise _template_not_found(requested, available_names)

    searched: List[Tuple[Any, str]] = []
    for library_index, library in enumerate(_material_libraries(app)):
        label = _library_label(library, library_index)
        materials = _library_materials(library, label)
        if materials is None:
            continue
        searched.append((materials, "material library {!r}".format(label)))
        material = _item_by_name(materials, requested, "material library {!r}".format(label))
        if material is not None:
            logger.debug("Using template material %r from material library %r.", requested, label)
            _TEMPLATE_CACHE[wanted] = (app, material)
            return material

    available_names = []
    if design_materials is not None:
        material = _find_case_insensitive(
            design_materials, wanted, available_names, "active design materials"
        )
        if material is not None:
            logger.debug(
                "Using template material %r (case-insensitive match) from the design.", requested
            )
            return material
    library_names: List[str] = []
    for collection, label in searched:
        material = _find_case_insensitive(collection, wanted, library_names, label)
        if material is not None:
            logger.debug(
                "Using template material %r (case-insensitive match) from %s.", requested, label
            )
            _TEMPLATE_CACHE[wanted] = (app, material)
            return material

    sample = sorted(set(library_names))[:10]
    _TEMPLATE_MISSES[wanted] = (app, library_count, tuple(sample))
    raise _template_not_found(requested, available_names + sample)


def clear_template_cache() -> None:
    """Forget cached template lookups (call after loading or unloading libraries)."""
    _TEMPLATE_CACHE.clear()
    _TEMPLATE_MISSES.clear()


def _library_count(app: Any) -> int:
    """Return the number of loaded material libraries, or -1 when unknown."""
    libraries = _fh.safe_get(app, "materialLibraries", "the Fusion application")
    try:
        return int(libraries.count)
    except Exception:
        return -1


def _cached_template(app: Any, wanted: str) -> Any:
    """Return the cached library template for ``wanted`` while it is still valid."""
    cached = _TEMPLATE_CACHE.get(wanted)
    if cached is None or cached[0] is not app:
        return None
    material = cached[1]
    try:
        valid = bool(getattr(material, "isValid", True))
        name = material.name
    except Exception:
        valid, name = False, None
    if valid and isinstance(name, str) and name.strip().casefold() == wanted:
        return material
    _TEMPLATE_CACHE.pop(wanted, None)
    return None


def _template_not_found(requested: str, available_names: List[str]) -> TemplateMaterialNotFoundError:
    """Build the actionable error for a template that is nowhere to be found."""
    available_sample = sorted(set(available_names))[:10]
    return TemplateMaterialNotFoundError(
        "Template material {!r} was not found in the active design or any loaded material "
        "library. Fix: load the Fusion 360 Material Library (or the manufacturer .adsklib) in "
        "Fusion, or change 'template_material' for this catalog entry in "
        "catalog/filaments.json. Available material names (sample): {}.".format(
            requested,
            (
                ", ".join(repr(name) for name in available_sample)
                if available_sample
                else "<none found>"
            ),
        )
    )


def _get_material_properties(material: Any) -> Any:
    """Return the material's physical-properties collection across Fusion versions.

    Current Autodesk Fusion exposes ``material.materialProperties`` (an
    ``adsk.core.Properties`` collection).  Older Fusion 360 releases exposed
    ``material.physicalProperties``.  Both are accepted; the first non-``None``
    collection wins.

    Args:
        material: Fusion material object.

    Returns:
        The properties collection, or ``None`` when the material exposes none.
    """
    for attribute in ("materialProperties", "physicalProperties"):
        try:
            properties = getattr(material, attribute)
        except AttributeError:
            continue
        except Exception:
            logger.exception("Could not read material.%s.", attribute)
            continue
        if properties is not None:
            logger.debug("Using material.%s for physical properties.", attribute)
            return properties
    return None


def _density_unit_factor(units: Any) -> float:
    """Return the multiplier from g/cm^3 to the Density property's units.

    Args:
        units: The property's unit enum/value (e.g. ``"KilogramPerCubicMeter"``
            or ``"GramPerCubicCentimeter"``), or ``None``.

    Returns:
        A float multiplier.  Unknown or missing units fall back to ``1.0``
        (g/cm^3) so legacy Fusion 360 property objects keep working.
    """
    if not units:
        logger.debug("Density property reports no units; assuming g/cm^3.")
        return DEFAULT_DENSITY_UNIT_FACTOR
    normalized = re.sub(r"[^a-z]", "", str(units).lower())
    factor = _DENSITY_UNIT_FACTORS.get(normalized)
    if factor is None:
        logger.warning(
            "Unknown Density property units %r; assuming the value is in g/cm^3.", units
        )
        return DEFAULT_DENSITY_UNIT_FACTOR
    return factor


def get_material_density(material: Any) -> Optional[float]:
    """Return a material's density in g/cm^3, or ``None`` when it cannot be read."""
    properties = _get_material_properties(material)
    try:
        density_property = (
            properties.itemByName(DENSITY_PROPERTY_NAME) if properties is not None else None
        )
        if density_property is None:
            return None
        factor = _density_unit_factor(getattr(density_property, "units", None))
        value = float(density_property.value) / factor
    except Exception:
        logger.debug("Could not read the density of %r.", _material_name(material), exc_info=True)
        return None
    return value if math.isfinite(value) and value > 0 else None


def set_material_density(material: Any, density_g_cm3: float) -> bool:
    """Apply a density value to a Fusion material's physical properties.

    Supports both the current Fusion API (``material.materialProperties``) and
    the legacy Fusion 360 API (``material.physicalProperties``).  The catalog
    density is given in g/cm^3 and converted into the Density property's own
    units (current Fusion stores kg/m^3, so 1.24 g/cm^3 becomes 1240.0).

    Density application is best-effort: Fusion can omit the property for some
    template materials, expose it read-only, or fail while writing; in those
    cases a warning is logged and ``False`` is returned instead of raising.

    Args:
        material: Fusion material object (exposing ``materialProperties`` or
            ``physicalProperties``).
        density_g_cm3: Desired density in g/cm^3.

    Returns:
        True when the density was written (and read back), False when the
        Fusion material exposes no writable Density property.

    Raises:
        ValueError: If ``density_g_cm3`` is not a finite positive number.
    """
    _validate_density_argument(density_g_cm3)
    requested = float(density_g_cm3)
    label = _material_name(material) or "<unnamed material>"

    properties = _get_material_properties(material)
    if properties is None:
        logger.warning(
            "Material %r exposes neither materialProperties nor physicalProperties; "
            "density %s g/cm^3 not applied.",
            label,
            requested,
        )
        return False

    try:
        density_property = properties.itemByName(DENSITY_PROPERTY_NAME)
    except Exception:
        logger.exception(
            "Could not look up the %r property on material %r; density not applied.",
            DENSITY_PROPERTY_NAME,
            label,
        )
        return False
    if density_property is None:
        logger.warning(
            "Material %r has no %r physical property; density %s g/cm^3 not applied.",
            label,
            DENSITY_PROPERTY_NAME,
            requested,
        )
        return False

    try:
        if bool(getattr(density_property, "isReadOnly", False)):
            logger.warning(
                "The %r property of material %r is read-only; density %s g/cm^3 not applied.",
                DENSITY_PROPERTY_NAME,
                label,
                requested,
            )
            return False
    except Exception:
        logger.debug("Could not read Density.isReadOnly for material %r.", label, exc_info=True)

    units = getattr(density_property, "units", None)
    factor = _density_unit_factor(units)
    requested_in_property_units = requested * factor

    try:
        density_property.value = requested_in_property_units
    except Exception:
        logger.exception(
            "Could not write the %r property of material %r (units %r); density not applied.",
            DENSITY_PROPERTY_NAME,
            label,
            units,
        )
        return False

    try:
        actual_raw = float(density_property.value)
    except Exception:
        logger.exception("Density was written to material %r but could not be read back.", label)
        return True
    actual = actual_raw / factor if factor else actual_raw

    logger.info(
        "Set density of material %r to %s g/cm^3 (%s %s; read back %s g/cm^3).",
        label,
        requested,
        requested_in_property_units,
        units or "unknown units",
        actual,
    )
    if abs(actual - requested) > DENSITY_READBACK_TOLERANCE * requested:
        logger.warning(
            "Density read-back for material %r differs from the requested value: requested %s "
            "g/cm^3, got %s g/cm^3.",
            label,
            requested,
            actual,
        )
    return True


def import_filament(design: Any, entry: FilamentEntry, app: Any = None) -> Any:
    """Import one catalog filament into the design as a new Fusion material.

    A material named after ``entry.id`` is copied from
    ``entry.template_material`` and receives the catalog density.  Calls are
    idempotent: if a material with the catalog id already exists in the design
    it is reused (and its density re-applied) instead of copied again.

    Args:
        design: Active Fusion design (anything exposing ``materials``).
        entry: Catalog entry to import (a :class:`catalog.FilamentEntry`).
        app: Optional Fusion ``Application`` used to search material libraries.
            When omitted the running application is resolved automatically.

    Returns:
        The new (or previously imported) Fusion material.

    Raises:
        MaterialImportError: If the entry is invalid, the design materials
            collection is unavailable, or Fusion fails to copy the template.
        TemplateMaterialNotFoundError: If the template material cannot be found
            and the filament has not already been imported.
    """
    entry_id = _entry_text(entry, "id")
    template_name = _entry_text(entry, "template_material")
    density = _entry_density(entry)
    design_materials = _require_design_materials(design)

    existing_material = _item_by_name(design_materials, entry_id, "active design materials")
    if existing_material is not None:
        logger.info(
            "Material %r is already present in the design; reusing it instead of copying again.",
            entry_id,
        )
        set_material_density(existing_material, density)
        return existing_material

    template_material = find_template_material(design, template_name, app=app)
    logger.info("Copying template material %r to new material %r.", template_name, entry_id)
    try:
        material = design_materials.addByCopy(template_material, entry_id)
    except Exception as exc:
        logger.exception("design.materials.addByCopy(%r, %r) failed.", template_name, entry_id)
        material = _item_by_name(design_materials, entry_id, "active design materials")
        if material is None:
            raise MaterialImportError(
                "Fusion could not copy template material {!r} into a material named "
                "{!r}: {}".format(template_name, entry_id, exc)
            ) from exc
        logger.warning(
            "Reusing existing material %r after addByCopy failed (it was probably already "
            "present).",
            entry_id,
        )
        set_material_density(material, density)
        return material

    if material is None:
        raise MaterialImportError(
            "design.materials.addByCopy({!r}, {!r}) returned None; the material was not "
            "imported".format(template_name, entry_id)
        )

    # addByCopy already receives the catalog id as the target name; enforce the
    # rename anyway so the material identity always matches the catalog entry.
    _rename_material(material, entry_id)
    set_material_density(material, density)
    logger.info(
        "Imported filament %r (%s g/cm^3, template %r) into the design.",
        entry_id,
        density,
        template_name,
    )
    return material


def import_catalog_entries(
    design: Any, entries: Sequence[FilamentEntry], app: Any = None
) -> List[Any]:
    """Import several catalog filaments, continuing after per-entry failures.

    Args:
        design: Active Fusion design.
        entries: Catalog entries to import, in order.
        app: Optional Fusion ``Application`` for material-library lookups.

    Returns:
        The materials that were imported or reused successfully, in input order.

    Raises:
        MaterialImportError: If ``entries`` is ``None``.
    """
    if entries is None:
        raise MaterialImportError(
            "entries must be a sequence of FilamentEntry objects, got None"
        )
    imported: List[Any] = []
    total = 0
    for index, entry in enumerate(entries):
        total += 1
        entry_label = getattr(entry, "id", "<unknown>")
        try:
            imported.append(import_filament(design, entry, app=app))
        except Exception:
            logger.exception(
                "Failed to import catalog entry #%d (%r); continuing with the remaining entries.",
                index + 1,
                entry_label,
            )
    logger.info(
        "Imported %d of %d catalog filament(s) into the design.", len(imported), total
    )
    return imported


# ---------------------------------------------------------------------------
# Catalog entry helpers
# ---------------------------------------------------------------------------


def _entry_text(entry: Any, attribute: str) -> str:
    """Return a required non-empty string attribute of a catalog entry."""
    try:
        value = getattr(entry, attribute)
    except Exception as exc:
        raise MaterialImportError(
            "Catalog entry is missing the required attribute {!r}: {}".format(attribute, exc)
        ) from exc
    if not isinstance(value, str) or not value.strip():
        raise MaterialImportError(
            "Catalog entry attribute {!r} must be a non-empty string, got {!r}".format(
                attribute, value
            )
        )
    return value.strip()


def _validate_density_argument(density_g_cm3: Any) -> None:
    """Raise ValueError unless ``density_g_cm3`` is a finite positive number."""
    if isinstance(density_g_cm3, bool) or not isinstance(density_g_cm3, (int, float)):
        raise ValueError(
            "density_g_cm3 must be a number, got {!r}".format(density_g_cm3)
        )
    value = float(density_g_cm3)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(
            "density_g_cm3 must be finite and greater than zero, got {!r}".format(density_g_cm3)
        )


def _entry_density(entry: Any) -> float:
    """Return the density of a catalog entry as a validated positive float."""
    try:
        raw_density = entry.density_g_cm3
    except Exception as exc:
        raise MaterialImportError(
            "Catalog entry has no readable density_g_cm3 attribute: {}".format(exc)
        ) from exc
    try:
        _validate_density_argument(raw_density)
    except ValueError as exc:
        raise MaterialImportError(
            "Catalog entry {!r} has an invalid density_g_cm3: {}".format(
                getattr(entry, "id", "<unknown>"), exc
            )
        ) from exc
    return float(raw_density)


# ---------------------------------------------------------------------------
# Self-test doubles and checks (only used by the __main__ self-test)
# ---------------------------------------------------------------------------


class FakePhysicalProperty:
    """Test double for ``adsk.core.FloatProperty`` (legacy: PhysicalProperty)."""

    def __init__(
        self,
        name: str = "Density",
        value: float = 1.0,
        units: Optional[str] = None,
        raise_on_set: bool = False,
        read_only: bool = False,
    ) -> None:
        self.name = name
        self._value = value
        self._raise_on_set = raise_on_set
        self.units = units
        self.isReadOnly = read_only

    @property
    def value(self) -> float:
        """Current property value in the property's units."""
        return self._value

    @value.setter
    def value(self, new_value: float) -> None:
        if self.isReadOnly:
            raise RuntimeError("simulated Fusion failure writing a read-only physical property")
        if self._raise_on_set:
            raise RuntimeError("simulated Fusion failure writing physical property value")
        self._value = new_value


class FakePhysicalProperties:
    """Test double for ``adsk.core.Properties`` (legacy: PhysicalProperties)."""

    def __init__(self, properties: Optional[Sequence[FakePhysicalProperty]] = None) -> None:
        self._properties = {prop.name: prop for prop in (properties or [])}

    @property
    def count(self) -> int:
        """Number of properties in the collection."""
        return len(self._properties)

    def itemByName(self, name: str) -> Optional[FakePhysicalProperty]:
        """Return the named physical property, or ``None`` when absent."""
        return self._properties.get(name)


class FakeMaterial:
    """Test double for ``adsk.core.Material``.

    ``material_properties`` models current Autodesk Fusion
    (``material.materialProperties``); ``physical_properties`` models legacy
    Fusion 360 releases (``material.physicalProperties``).
    """

    def __init__(
        self,
        name: str,
        material_properties: Optional[FakePhysicalProperties] = None,
        physical_properties: Optional[FakePhysicalProperties] = None,
        raise_on_properties: bool = False,
    ) -> None:
        self.name = name
        self._material_properties = material_properties
        self._physical_properties = physical_properties
        self._raise_on_properties = raise_on_properties

    @property
    def materialProperties(self) -> Optional[FakePhysicalProperties]:
        """Current-Fusion property collection; raises when configured to simulate failure."""
        if self._raise_on_properties:
            raise RuntimeError("simulated Fusion failure reading materialProperties")
        return self._material_properties

    @property
    def physicalProperties(self) -> Optional[FakePhysicalProperties]:
        """Legacy-Fusion property collection; raises when configured to simulate failure."""
        if self._raise_on_properties:
            raise RuntimeError("simulated Fusion failure reading physicalProperties")
        return self._physical_properties


class FakeMaterials:
    """Test double for ``adsk.core.Materials`` with copy support."""

    def __init__(
        self,
        materials: Optional[Sequence[FakeMaterial]] = None,
        copy_factory: Optional[Callable[[str], FakeMaterial]] = None,
    ) -> None:
        self._materials = list(materials or [])
        self._copy_factory = copy_factory

    @property
    def count(self) -> int:
        """Number of materials in the collection."""
        return len(self._materials)

    def item(self, index: int) -> Optional[FakeMaterial]:
        """Return the material at ``index``, or ``None`` when out of range."""
        if 0 <= index < len(self._materials):
            return self._materials[index]
        return None

    def itemByName(self, name: str) -> Optional[FakeMaterial]:
        """Return the first material named ``name``, or ``None``."""
        for material in self._materials:
            if material.name == name:
                return material
        return None

    def addByCopy(self, source: FakeMaterial, name: str) -> FakeMaterial:
        """Create a fake copy of ``source`` registered under ``name``."""
        if self.itemByName(name) is not None:
            raise RuntimeError("duplicate material name: {}".format(name))
        if self._copy_factory is not None:
            material = self._copy_factory(name)
        else:
            material = FakeMaterial(name)
        self._materials.append(material)
        return material


class FakeMaterialLibrary:
    """Test double for ``adsk.core.MaterialLibrary``."""

    def __init__(self, name: str, materials: Optional[Sequence[FakeMaterial]] = None) -> None:
        self.name = name
        self.materials = FakeMaterials(materials)


class FakeMaterialLibraries:
    """Test double for ``adsk.core.MaterialLibraries``."""

    def __init__(self, libraries: Optional[Sequence[FakeMaterialLibrary]] = None) -> None:
        self._libraries = list(libraries or [])

    @property
    def count(self) -> int:
        """Number of material libraries."""
        return len(self._libraries)

    def item(self, index: int) -> Optional[FakeMaterialLibrary]:
        """Return the library at ``index``, or ``None`` when out of range."""
        if 0 <= index < len(self._libraries):
            return self._libraries[index]
        return None


class FakeApp:
    """Test double for ``adsk.core.Application``."""

    def __init__(self, libraries: Optional[Sequence[FakeMaterialLibrary]] = None) -> None:
        self.materialLibraries = FakeMaterialLibraries(libraries)


class FakeDesign:
    """Test double for ``adsk.core.Design``."""

    def __init__(self, materials: Optional[FakeMaterials] = None) -> None:
        self.materials = materials if materials is not None else FakeMaterials()


def _expect(condition: bool, message: str) -> None:
    """Raise AssertionError with ``message`` when ``condition`` is false."""
    if not condition:
        raise AssertionError(message)


def _make_entry(
    entry_id: str = "test_filament",
    template_material: str = "PLA Plastic",
    density_g_cm3: float = 1.24,
) -> FilamentEntry:
    """Build a real catalog entry for the importer self-test."""
    from .catalog import FilamentEntry

    return FilamentEntry(
        id=entry_id,
        manufacturer="Test Manufacturer",
        product_line="Test Line",
        color_name="Black",
        color_hex="#161616",
        slicer_filament_type="PLA",
        density_g_cm3=density_g_cm3,
        template_material=template_material,
    )


def _density_material(
    name: str,
    density: float = 1.0,
    units: Optional[str] = "KilogramPerCubicMeter",
    read_only: bool = False,
    legacy: bool = False,
) -> FakeMaterial:
    """Create a fake material carrying a writable Density property.

    By default the current-Fusion API shape is used (``materialProperties`` with
    kg/m^3 units); pass ``legacy=True`` for the old ``physicalProperties``
    attribute, whose fake property reports no units (g/cm^3).
    """
    properties = FakePhysicalProperties(
        [
            FakePhysicalProperty(
                DENSITY_PROPERTY_NAME,
                density,
                units=units,
                read_only=read_only,
            )
        ]
    )
    if legacy:
        return FakeMaterial(name, physical_properties=properties)
    return FakeMaterial(name, material_properties=properties)


def _check_successful_import() -> None:
    """A template in the design is copied and receives the catalog density."""
    materials = FakeMaterials(
        [_density_material("PLA Plastic")], copy_factory=lambda name: _density_material(name)
    )
    design = FakeDesign(materials)
    entry = _make_entry()

    material = import_filament(design, entry)

    _expect(material is not None, "import_filament must return a material")
    _expect(
        material.name == entry.id,
        "imported material must be named after the entry id, got {!r}".format(material.name),
    )
    density_property = material.materialProperties.itemByName(DENSITY_PROPERTY_NAME)
    _expect(
        density_property is not None,
        "imported material must expose a Density property via materialProperties",
    )
    # The catalog value is g/cm^3; current Fusion stores Density in kg/m^3.
    _expect(
        abs(density_property.value - 1240.0) < 1e-6,
        "catalog density 1.24 g/cm3 must be written as 1240 kg/m3, got {!r}".format(
            density_property.value
        ),
    )
    _expect(materials.count == 2, "the copied material must be added to the design")


def _check_rename_enforced() -> None:
    """The copied material is renamed to the catalog id even if Fusion kept another name."""
    materials = FakeMaterials(
        [_density_material("PLA Plastic")],
        copy_factory=lambda _name: _density_material("PLA Plastic"),
    )
    design = FakeDesign(materials)
    entry = _make_entry()

    material = import_filament(design, entry)

    _expect(
        material.name == entry.id,
        "the copied material must be renamed to the catalog id, got {!r}".format(
            material.name
        ),
    )


def _check_idempotent_import() -> None:
    """Repeated imports reuse the existing material and re-apply density."""
    materials = FakeMaterials(
        [_density_material("PLA Plastic")], copy_factory=lambda name: _density_material(name)
    )
    design = FakeDesign(materials)
    entry = _make_entry()

    first = import_filament(design, entry)
    first.materialProperties.itemByName(DENSITY_PROPERTY_NAME).value = 9.99
    second = import_filament(design, entry)

    _expect(second is first, "repeated import must return the already imported material")
    _expect(materials.count == 2, "repeated import must not create a duplicate material")
    _expect(
        abs(second.materialProperties.itemByName(DENSITY_PROPERTY_NAME).value - 1240.0) < 1e-6,
        "existing material density must be re-applied on repeated import",
    )


def _check_template_from_library() -> None:
    """A template that is only present in an application material library is found."""
    library = FakeMaterialLibrary("Fusion 360 Material Library", [_density_material("PLA Plastic")])
    app = FakeApp([library])
    materials = FakeMaterials([], copy_factory=lambda name: _density_material(name))
    design = FakeDesign(materials)

    material = import_filament(design, _make_entry(), app=app)

    _expect(
        material.name == "test_filament",
        "material imported from a library template must use the catalog id as its name",
    )


def _check_case_insensitive_template() -> None:
    """A template name differing only in case is still resolved."""
    materials = FakeMaterials(
        [_density_material("pla plastic")], copy_factory=lambda name: _density_material(name)
    )
    design = FakeDesign(materials)

    material = import_filament(design, _make_entry())

    _expect(material is not None, "case-insensitive template lookup must succeed")
    _expect(materials.count == 2, "case-insensitive template lookup must copy the template")


def _check_missing_template() -> None:
    """A missing template raises TemplateMaterialNotFoundError with fix guidance."""
    design = FakeDesign(FakeMaterials([]))

    try:
        find_template_material(design, "PLA Plastic")
    except TemplateMaterialNotFoundError as exc:
        message = str(exc)
        _expect(
            "PLA Plastic" in message,
            "error must name the requested template: {}".format(message),
        )
        _expect(
            "Material Library" in message,
            "error must mention loading the Fusion 360 Material Library: {}".format(message),
        )
        _expect(
            "template_material" in message,
            "error must mention changing template_material in the catalog: {}".format(message),
        )
    else:
        raise AssertionError("missing template must raise TemplateMaterialNotFoundError")


class _CountingMaterials(FakeMaterials):
    """FakeMaterials that counts name lookups and full scans."""

    def __init__(self, materials: Sequence[FakeMaterial]) -> None:
        super().__init__(materials)
        self.lookups = 0
        self.scans = 0

    def itemByName(self, name: str) -> Optional[FakeMaterial]:
        self.lookups += 1
        return super().itemByName(name)

    @property
    def count(self) -> int:
        self.scans += 1
        return len(self._materials)


def _check_template_cache() -> None:
    """Library hits are cached and revalidated; misses are not rescanned."""
    clear_template_cache()
    template = _density_material("PLA Plastic")
    library = FakeMaterialLibrary("Fusion 360 Material Library")
    library.materials = _CountingMaterials([template, _density_material("ABS Plastic")])
    app = FakeApp([library])
    design = FakeDesign(FakeMaterials([]))
    first = find_template_material(design, "PLA Plastic", app)
    lookups = library.materials.lookups
    second = find_template_material(None, "pla plastic", app)
    _expect(first is template and second is template, "cached lookups must return the template")
    _expect(library.materials.lookups == lookups, "a cached template must not be looked up again")

    template.isValid = False  # type: ignore[attr-defined]
    try:
        find_template_material(None, "PLA Plastic", app)
    except TemplateMaterialNotFoundError:
        pass
    _expect(library.materials.lookups > lookups, "an invalid cached template must be looked up again")
    template.isValid = True  # type: ignore[attr-defined]

    for _attempt in range(2):
        try:
            find_template_material(None, "Unobtainium", app)
        except TemplateMaterialNotFoundError:
            pass
        else:
            raise AssertionError("a missing template must still raise")
    scans = library.materials.scans
    try:
        find_template_material(None, "Unobtainium", app)
    except TemplateMaterialNotFoundError as exc:
        _expect("ABS Plastic" in str(exc), "a cached miss must keep the name sample")
    _expect(library.materials.scans == scans, "a cached miss must not rescan the libraries")
    app.materialLibraries._libraries.append(
        FakeMaterialLibrary("New", [_density_material("Unobtainium")])
    )
    _expect(
        find_template_material(None, "Unobtainium", app).name == "Unobtainium",
        "a newly loaded library must invalidate the cached miss",
    )
    clear_template_cache()


def _check_get_material_density() -> None:
    """get_material_density converts the property units back to g/cm^3."""
    _expect(
        abs(get_material_density(_density_material("ABS", 1060.0)) - 1.06) < 1e-9,
        "kg/m^3 must be converted to g/cm^3",
    )
    _expect(
        get_material_density(_density_material("Legacy", 1.24, units=None, legacy=True)) == 1.24,
        "legacy g/cm^3 values must be returned as is",
    )
    _expect(
        get_material_density(FakeMaterial("None", FakePhysicalProperties([]))) is None,
        "a missing Density property must return None",
    )


def _check_missing_density_property() -> None:
    """A material without a Density property imports successfully with a warning."""
    materials = FakeMaterials(
        [_density_material("PLA Plastic")],
        copy_factory=lambda name: FakeMaterial(name, FakePhysicalProperties([])),
    )
    design = FakeDesign(materials)
    entry = _make_entry()

    material = import_filament(design, entry)

    _expect(material is not None and material.name == entry.id, "import must still succeed")
    _expect(
        set_material_density(material, 1.24) is False,
        "set_material_density must return False when the Density property is missing",
    )


def _check_density_failures_do_not_raise() -> None:
    """Density write/read failures are logged and reported instead of raised."""
    materials = FakeMaterials(
        [_density_material("PLA Plastic")],
        copy_factory=lambda name: FakeMaterial(
            name,
            FakePhysicalProperties(
                [FakePhysicalProperty(DENSITY_PROPERTY_NAME, 1.0, raise_on_set=True)]
            ),
        ),
    )
    design = FakeDesign(materials)

    material = import_filament(design, _make_entry())
    _expect(material is not None, "import must succeed even when writing density fails")
    _expect(
        set_material_density(material, 1.24) is False,
        "set_material_density must return False when writing the property raises",
    )

    unreadable = FakeMaterial("unreadable", raise_on_properties=True)
    _expect(
        set_material_density(unreadable, 1.24) is False,
        "set_material_density must return False when the property collection raises",
    )


def _check_legacy_physical_properties_api() -> None:
    """The legacy Fusion 360 ``physicalProperties`` API is still supported."""
    materials = FakeMaterials(
        [_density_material("PLA Plastic", legacy=True, units=None)],
        copy_factory=lambda name: _density_material(name, legacy=True, units=None),
    )
    design = FakeDesign(materials)

    material = import_filament(design, _make_entry())

    legacy_property = material.physicalProperties.itemByName(DENSITY_PROPERTY_NAME)
    _expect(legacy_property is not None, "legacy material must expose physicalProperties")
    _expect(
        abs(legacy_property.value - 1.24) < 1e-9,
        "legacy unit-less Density must receive the g/cm3 value unchanged, got {!r}".format(
            legacy_property.value
        ),
    )


def _check_read_only_density_property() -> None:
    """A read-only Density property is reported as False without failing the import."""
    materials = FakeMaterials(
        [_density_material("PLA Plastic")],
        copy_factory=lambda name: _density_material(name, read_only=True),
    )
    design = FakeDesign(materials)

    material = import_filament(design, _make_entry())

    _expect(material is not None, "import must succeed when Density is read-only")
    _expect(
        set_material_density(material, 1.24) is False,
        "set_material_density must return False for a read-only Density property",
    )


def _check_batch_import_continues() -> None:
    """Batch import keeps going after an entry whose template is missing."""
    materials = FakeMaterials(
        [_density_material("PLA Plastic")], copy_factory=lambda name: _density_material(name)
    )
    design = FakeDesign(materials)
    entries = [
        _make_entry("good_one"),
        _make_entry("bad_one", template_material="Does Not Exist"),
        _make_entry("good_two"),
    ]

    imported = import_catalog_entries(design, entries)

    _expect(
        [material.name for material in imported] == ["good_one", "good_two"],
        "batch import must skip the failing entry and keep the successful ones, got {}".format(
            [material.name for material in imported]
        ),
    )


def _selftest() -> int:
    """Run the importer self-test against fake Fusion objects.

    Returns:
        Process exit code: 0 when every check passed, 1 otherwise.
    """
    scenarios = (
        ("successful import copies the template and sets density (kg/m3 conversion)",
         _check_successful_import),
        ("copied material is renamed to the catalog id", _check_rename_enforced),
        ("repeated import is idempotent and re-applies density", _check_idempotent_import),
        ("legacy physicalProperties API still receives the g/cm3 density",
         _check_legacy_physical_properties_api),
        ("read-only Density property is reported but does not fail the import",
         _check_read_only_density_property),
        ("template resolved from an application material library", _check_template_from_library),
        ("template resolved case-insensitively from the design", _check_case_insensitive_template),
        ("template lookups are cached, revalidated and misses not rescanned", _check_template_cache),
        (
            "missing template raises TemplateMaterialNotFoundError with fix guidance",
            _check_missing_template,
        ),
        ("missing Density property warns but the import succeeds", _check_missing_density_property),
        ("get_material_density reads the density back in g/cm^3", _check_get_material_density),
        (
            "density write/read failures return False without raising",
            _check_density_failures_do_not_raise,
        ),
        ("batch import continues past per-entry failures", _check_batch_import_continues),
    )
    try:
        for label, scenario in scenarios:
            scenario()
            print("PASS: {}".format(label))
    except Exception:
        traceback.print_exc()
        print("FAIL: importer self-test aborted", file=sys.stderr)
        return 1
    print("PASS: all importer self-tests completed")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(_selftest())
