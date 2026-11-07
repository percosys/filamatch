"""Colored-appearance material workflow for Filamatch.

Fusion 360 cannot recolor the appearance linked to a material: ``Material.appearance``
is read-only and mutating ``material.appearance.color`` is silently ignored.  This
module therefore works with *design appearances*:

* :func:`import_catalog_entries` imports the catalog materials (delegating to
  :mod:`importer`) and gives each one a separate, color-editable design appearance
  named ``Filamatch <catalog id>`` plus a description carrying category metadata
  (an older ``FFB <catalog id>`` appearance is found and reused).
* :func:`collect_assembly_materials` walks the whole assembly in stable
  first-appearance order, groups bodies by material (or by effective appearance for
  material-less bodies), resolves catalog entries and derives the automatic
  1-based extruder slots.
* :func:`build_mapping` and :func:`build_filament_slots` turn that detection into
  the name-to-extruder mapping and ``threemf_injector.FilamentSlot`` table.
* :func:`apply_export_appearances` temporarily paints every body with its slot
  color before a 3MF export, and :func:`restore_appearances` puts the previous
  material/override appearances back afterwards.
* :func:`apply_unique_export_names` temporarily renames bodies that share a name
  (Fusion writes only the body name into each 3MF object, so two ``Body1`` bodies
  in different components cannot be told apart), and :func:`restore_body_names`
  puts the original names back after the export.

The module imports cleanly outside Fusion 360: ``adsk`` is never imported at module
scope and every Fusion object is duck-typed, so ``python3 materials_workflow.py``
runs the self-test with fakes.  All Fusion interactions are wrapped in defensive
try/except blocks that log context instead of crashing the add-in.
"""

from __future__ import annotations

import logging
import math
import re
import sys
import traceback
import types
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from . import fusion_helpers as _fh
from . import testing

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: Maximum number of AMS / extruder slots supported by the slicer handoff.
MAX_MATERIAL_SLOTS = 16

#: Maximum component nesting level walked by the traversal helpers.
MAX_COMPONENT_DEPTH = 8

#: Filament type used when neither the catalog nor the material provides one.
DEFAULT_FILAMENT_TYPE = "PLA"

#: Colour used when no appearance colour can be read from a body.
DEFAULT_APPEARANCE_COLOR = "#808080"

#: Label used as the first path segment for bodies in the root component.
ROOT_PATH_LABEL = "Root"

#: Prefix for colored appearances created from catalog filaments.
CATALOG_APPEARANCE_PREFIX = "Filamatch "

#: Prefix for temporary export appearances.
EXPORT_APPEARANCE_PREFIX = "Filamatch Export "

#: Prefixes used by earlier versions; appearances named with them are reused.
LEGACY_CATALOG_APPEARANCE_PREFIX = "FFB "
LEGACY_EXPORT_APPEARANCE_PREFIX = "FFB Export "

#: Prefix for the material description written by the workflow.
MATERIAL_DESCRIPTION_PREFIX = "Filamatch | "

#: Physical-property name of the material density.
DENSITY_PROPERTY_NAME = "Density"

_UNSAFE_NAME_PATTERN = re.compile(r"[\r\n\t]+")

__all__ = [
    "MAX_MATERIAL_SLOTS",
    "MAX_COMPONENT_DEPTH",
    "DEFAULT_FILAMENT_TYPE",
    "DEFAULT_APPEARANCE_COLOR",
    "ROOT_PATH_LABEL",
    "CATALOG_APPEARANCE_PREFIX",
    "EXPORT_APPEARANCE_PREFIX",
    "MATERIAL_DESCRIPTION_PREFIX",
    "MaterialsWorkflowError",
    "NoMaterialsFoundError",
    "DetectedMaterial",
    "ImportedFilament",
    "AppearanceBackup",
    "BodyNameBackup",
    "rgb_from_hex",
    "color_hex_from_color",
    "ensure_colored_appearance",
    "find_source_appearance",
    "import_catalog_entries",
    "collect_assembly_materials",
    "build_mapping",
    "build_filament_slots",
    "apply_export_appearances",
    "restore_appearances",
    "apply_unique_export_names",
    "restore_body_names",
]


class MaterialsWorkflowError(RuntimeError):
    """Raised when the colored-material workflow cannot complete."""


class NoMaterialsFoundError(MaterialsWorkflowError):
    """Raised when the assembly contains no bodies to derive materials from."""


@dataclass(frozen=True)
class DetectedMaterial:
    """One automatically detected filament/material used by the assembly."""

    material_id: str
    material_name: str
    display_name: str
    color_hex: str
    filament_type: str
    density_g_cm3: Optional[float]
    category: str
    body_names: Tuple[str, ...]
    slot: int
    manufacturer: str = ""
    product_line: str = ""
    color_name: str = ""
    orca_filament_profile: str = ""


@dataclass(frozen=True)
class ImportedFilament:
    """Result of importing one catalog entry into the design."""

    catalog_id: str
    material: Any
    appearance: Any


@dataclass(frozen=True)
class AppearanceBackup:
    """Previous body appearance state recorded before a temporary override."""

    body: Any
    had_override: bool
    appearance: Any


@dataclass(frozen=True)
class BodyNameBackup:
    """Previous body name recorded before a temporary export rename."""

    body: Any
    name: str


@dataclass
class _MaterialGroup:
    """Internal accumulator used while grouping bodies by material identity."""

    material: Any
    appearance: Any
    material_id: str
    material_name: str
    body_names: List[str] = field(default_factory=list)
    color_hex: Optional[str] = None


# ---------------------------------------------------------------------------
# Generic defensive helpers
# ---------------------------------------------------------------------------

_safe_get = _fh.safe_get
_collection_items = _fh.collection_items
_item_by_name = _fh.item_by_name
_display_name = _fh.display_name
_entry_field = _fh.entry_field
_resolve_fusion_app = _fh.resolve_fusion_app
_same_body = _fh.same_body


def _require_design(design: Any, operation: str) -> None:
    """Raise MaterialsWorkflowError when ``design`` is ``None``."""
    if design is None:
        raise MaterialsWorkflowError(
            "{} requires a Fusion design, got None".format(operation)
        )


def _require_text(value: Any, label: str) -> str:
    """Return a stripped non-empty string or raise MaterialsWorkflowError."""
    if not isinstance(value, str) or not value.strip():
        raise MaterialsWorkflowError(
            "{} must be a non-empty string, got {!r}".format(label, value)
        )
    return value.strip()


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------


def rgb_from_hex(color_hex: str) -> Tuple[int, int, int]:
    """Convert ``#RRGGBB`` to an ``(r, g, b)`` tuple of 0-255 integers.

    Args:
        color_hex: Color string such as ``"#161616"``.

    Returns:
        The red, green and blue channels.

    Raises:
        MaterialsWorkflowError: If the string is not a six digit hex color.
    """
    try:
        return _fh.rgb_from_hex(color_hex)
    except ValueError as exc:
        raise MaterialsWorkflowError(str(exc)) from exc


def _normalized_hex(color_hex: str) -> str:
    """Return ``#RRGGBB`` uppercase for a validated color string."""
    red, green, blue = rgb_from_hex(color_hex)
    return "#{:02X}{:02X}{:02X}".format(red, green, blue)


def color_hex_from_color(color: Any) -> str:
    """Convert a Fusion ``Color`` to an uppercase ``#RRGGBB`` string.

    Only the red/green/blue channels are read; opacity is optional and ignored
    (Fusion colors have no ``alpha`` channel).

    Args:
        color: Object exposing ``red``, ``green`` and ``blue`` (0-255).

    Returns:
        The uppercase color string.

    Raises:
        MaterialsWorkflowError: If the color or one of its channels is unusable.
    """
    try:
        return _fh.hex_from_color(color)
    except ValueError as exc:
        raise MaterialsWorkflowError(str(exc)) from exc


def _new_color(color_hex: str) -> Any:
    """Build an opaque Fusion ``Color`` for ``color_hex`` (internal helper)."""
    try:
        return _fh.new_color(color_hex)
    except (ValueError, RuntimeError) as exc:
        raise MaterialsWorkflowError(str(exc)) from exc


def _apply_appearance_color(appearance: Any, color_hex: str, context: str) -> None:
    """Set and verify the color of a design appearance (internal helper)."""
    normalized = _normalized_hex(color_hex)
    color = _new_color(normalized)
    # The material swatch/preview also reads ``surface_albedo``; setting only
    # ``Appearance.color`` (opaque_albedo) leaves copied template appearances
    # white in the Fusion material browser.  Write the other colour slots once,
    # opaque_albedo through ``color`` below, and disconnect any colour texture.
    _fh.clear_color_texture(appearance, context)
    _fh.set_color_slots(appearance, color, context, ("surface_albedo", "transparent_color"))
    try:
        appearance.color = color
    except Exception as exc:
        logger.exception("Could not set the color of %s.", context)
        raise MaterialsWorkflowError(
            "Could not set the color of {}: {}".format(context, exc)
        ) from exc
    try:
        readback = color_hex_from_color(_safe_get(appearance, "color", context))
    except MaterialsWorkflowError:
        logger.warning("Could not read back the color of %s; color is not verified.", context)
        return
    if readback != normalized:
        logger.warning(
            "Color read-back for %s is %s instead of %s.", context, readback, normalized
        )


def _appearance_color_hex(appearance: Any) -> Optional[str]:
    """Return the ``#RRGGBB`` color of an appearance, or ``None``."""
    if appearance is None:
        return None
    color = _safe_get(appearance, "color", "an appearance")
    if color is None:
        return None
    try:
        return color_hex_from_color(color)
    except MaterialsWorkflowError:
        logger.warning("Could not convert an appearance color to hex.", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Design appearances
# ---------------------------------------------------------------------------


def ensure_colored_appearance(
    design: Any,
    name: str,
    color_hex: str,
    source_appearance: Any = None,
    legacy_names: Sequence[str] = (),
) -> Any:
    """Return a design appearance with the requested name and color.

    If ``design.appearances.itemByName(name)`` exists (or, failing that, one of
    ``legacy_names`` from an earlier version) it is reused and its color is
    re-applied.  Otherwise a copy of ``source_appearance`` is created (or a
    fresh default appearance via ``design.appearances.add(name)`` when no source
    is supplied), and the color is applied to the copy.

    Args:
        design: Active Fusion design (exposing ``appearances``).
        name: Appearance name, e.g. ``"Filamatch bambu_pla_basic_black"``.
        color_hex: Desired color as ``#RRGGBB``.
        source_appearance: Optional appearance to copy; ``None`` creates a fresh
            default appearance.
        legacy_names: Older names of the same appearance, reused under that
            name when ``name`` does not exist yet.

    Returns:
        The color-editable design appearance.

    Raises:
        MaterialsWorkflowError: If the design/appearances collection is
            unavailable, the color is invalid, or appearance creation fails.
    """
    _require_design(design, "ensure_colored_appearance")
    appearance_name = _require_text(name, "appearance name")
    normalized = _normalized_hex(color_hex)

    appearances = _safe_get(design, "appearances", "the active design")
    if appearances is None:
        raise MaterialsWorkflowError(
            "The active design has no appearances collection; cannot create "
            "appearance {!r}".format(appearance_name)
        )

    existing = _item_by_name(appearances, appearance_name, "design appearances")
    for legacy_name in legacy_names:
        if existing is not None:
            break
        if isinstance(legacy_name, str) and legacy_name.strip():
            existing = _item_by_name(appearances, legacy_name.strip(), "design appearances")
    if existing is not None:
        _apply_appearance_color(existing, normalized, "appearance {!r}".format(appearance_name))
        logger.debug("Reused existing design appearance %r.", appearance_name)
        return existing

    appearance: Any = None
    if source_appearance is not None:
        try:
            appearance = appearances.addByCopy(source_appearance, appearance_name)
        except Exception:
            logger.exception(
                "design.appearances.addByCopy failed for %r; falling back to add().",
                appearance_name,
            )
            appearance = None
    if appearance is None:
        try:
            appearance = appearances.add(appearance_name)
        except Exception as exc:
            logger.exception("Could not create design appearance %r.", appearance_name)
            raise MaterialsWorkflowError(
                "Fusion could not create appearance {!r}: {}".format(appearance_name, exc)
            ) from exc
    if appearance is None:
        raise MaterialsWorkflowError(
            "Fusion returned null when creating appearance {!r}".format(appearance_name)
        )

    _apply_appearance_color(appearance, normalized, "appearance {!r}".format(appearance_name))
    logger.info("Created design appearance %r (%s).", appearance_name, normalized)
    return appearance


def find_source_appearance(design: Any, app: Any = None) -> Any:
    """Find a source appearance to copy for design appearances.

    Looks for the first appearance of the first material in the design, then for
    the first appearance of the first material in the first application material
    library.  Returns ``None`` when nothing usable is found.

    Args:
        design: Active Fusion design (exposing ``materials``).
        app: Optional Fusion ``Application``; resolved automatically when omitted.

    Returns:
        An appearance object or ``None``.
    """
    if design is not None:
        design_materials = _safe_get(design, "materials", "the active design")
        for material in _collection_items(design_materials, "design materials"):
            appearance = _safe_get(material, "appearance", "a design material")
            if appearance is not None:
                logger.debug("Using a design material appearance as the color source.")
                return appearance

    if app is None:
        app = _resolve_fusion_app()
    libraries = _collection_items(
        _safe_get(app, "materialLibraries", "the Fusion application"), "material libraries"
    )
    for library in libraries:
        library_materials = _safe_get(library, "materials", "a material library")
        for material in _collection_items(library_materials, "library materials"):
            appearance = _safe_get(material, "appearance", "a library material")
            if appearance is not None:
                logger.debug("Using a library material appearance as the color source.")
                return appearance
    logger.warning("No source appearance is available; a default appearance will be created.")
    return None


# ---------------------------------------------------------------------------
# Catalog import
# ---------------------------------------------------------------------------


def _load_importer() -> Any:
    """Import the sibling ``importer`` module lazily, or return ``None``."""
    try:
        from . import importer
    except ImportError:
        logger.debug("importer module is unavailable; using the internal fallback.", exc_info=True)
        return None
    except Exception:
        logger.exception("Unexpected failure importing the importer module.")
        return None
    return importer


def _fallback_import_material(design: Any, entry: Any) -> Any:
    """Create/reuse a material when the importer module cannot be imported."""
    catalog_id = _require_text(_entry_field(entry, "id"), "catalog entry id")
    materials = _safe_get(design, "materials", "the active design")
    if materials is None:
        raise MaterialsWorkflowError(
            "The active design has no materials collection; cannot import {!r}".format(
                catalog_id
            )
        )
    existing = _item_by_name(materials, catalog_id, "design materials")
    if existing is not None:
        return existing
    template = None
    for candidate in _collection_items(materials, "design materials"):
        template = candidate
        break
    if template is None:
        raise MaterialsWorkflowError(
            "Cannot create material {!r}: the design contains no material to copy".format(
                catalog_id
            )
        )
    try:
        material = materials.addByCopy(template, catalog_id)
    except Exception as exc:
        logger.exception("Could not copy a design material for %r.", catalog_id)
        raise MaterialsWorkflowError(
            "Fusion could not create material {!r}: {}".format(catalog_id, exc)
        ) from exc
    if material is None:
        raise MaterialsWorkflowError(
            "Fusion returned null when creating material {!r}".format(catalog_id)
        )
    return material


def _import_or_reuse_material(design: Any, entry: Any, importer_module: Any) -> Any:
    """Import the catalog entry with the importer module, or fall back."""
    catalog_id = _require_text(_entry_field(entry, "id"), "catalog entry id")
    if importer_module is not None:
        try:
            return importer_module.import_filament(design, entry)
        except Exception as exc:
            logger.exception("importer.import_filament failed for %r.", catalog_id)
            raise MaterialsWorkflowError(
                "Could not import catalog filament {!r}: {}".format(catalog_id, exc)
            ) from exc
    logger.warning(
        "Importer module unavailable; creating material %r with the fallback copy logic.",
        catalog_id,
    )
    return _fallback_import_material(design, entry)


def _entry_category(entry: Any) -> str:
    """Return the entry category, falling back to its slicer filament type."""
    return _fh.entry_category(entry, DEFAULT_FILAMENT_TYPE)


def _entry_color_hex(entry: Any) -> str:
    """Return the validated uppercase color of a catalog entry."""
    catalog_id = _require_text(_entry_field(entry, "id"), "catalog entry id")
    color_hex = _entry_field(entry, "color_hex")
    try:
        return _normalized_hex(color_hex)
    except MaterialsWorkflowError as exc:
        raise MaterialsWorkflowError(
            "Catalog entry {!r} has an invalid color_hex: {}".format(catalog_id, exc)
        ) from exc


def _set_material_description(material: Any, entry: Any, catalog_id: str) -> None:
    """Write the category/manufacturer description onto a Fusion material."""
    description = _fh.entry_description(entry, MATERIAL_DESCRIPTION_PREFIX, DEFAULT_FILAMENT_TYPE)
    try:
        material.description = description
        logger.debug("Set description of material %r to %r.", catalog_id, description)
    except Exception:
        logger.warning(
            "Could not set the description of material %r (continuing).", catalog_id,
            exc_info=True,
        )


def import_catalog_entries(
    design: Any, entries: Sequence[Any], source_appearance: Any = None
) -> List[ImportedFilament]:
    """Import catalog filaments as materials plus colored design appearances.

    Each entry is imported (or reused) through :func:`importer.import_filament`,
    its description receives category/manufacturer metadata, and a design
    appearance named ``"Filamatch <catalog id>"`` (or an older ``"FFB <catalog
    id>"`` one) is created/reused with the catalog color.  The operation is idempotent: repeated calls reuse materials and
    appearances.

    Args:
        design: Active Fusion design.
        entries: Catalog entries (attribute or mapping based).
        source_appearance: Optional appearance copied for the new design
            appearances; ``None`` lets :func:`ensure_colored_appearance` create a
            default one.

    Returns:
        One :class:`ImportedFilament` per imported entry, in input order.

    Raises:
        MaterialsWorkflowError: If an entry cannot be imported or colored.
    """
    _require_design(design, "import_catalog_entries")
    if entries is None:
        raise MaterialsWorkflowError("entries must be a sequence of catalog entries, got None")
    importer_module = _load_importer()
    imported: List[ImportedFilament] = []
    for index, entry in enumerate(entries):
        catalog_id = _require_text(
            _entry_field(entry, "id"), "catalog entry id (entry index {})".format(index)
        )
        try:
            material = _import_or_reuse_material(design, entry, importer_module)
            _set_material_description(material, entry, catalog_id)
            appearance = ensure_colored_appearance(
                design,
                CATALOG_APPEARANCE_PREFIX + catalog_id,
                _entry_color_hex(entry),
                source_appearance,
                legacy_names=(LEGACY_CATALOG_APPEARANCE_PREFIX + catalog_id,),
            )
        except MaterialsWorkflowError:
            raise
        except Exception as exc:
            logger.exception("Failed to import catalog entry %r.", catalog_id)
            raise MaterialsWorkflowError(
                "Failed to import catalog entry {!r}: {}".format(catalog_id, exc)
            ) from exc
        imported.append(
            ImportedFilament(catalog_id=catalog_id, material=material, appearance=appearance)
        )
    logger.info("Imported %d catalog filament(s) into the design.", len(imported))
    return imported


# ---------------------------------------------------------------------------
# Assembly traversal and material detection
# ---------------------------------------------------------------------------


def _component_bodies(component: Any, path: str) -> List[Any]:
    """Return the solid and mesh bodies of a component in stable order."""
    label = "component {!r}".format(path)
    collections: List[Tuple[str, Any]] = []
    for attribute in ("bRepBodies", "meshBodies"):
        collection = _safe_get(component, attribute, label)
        if collection is not None:
            collections.append((attribute, collection))
    if not collections:
        legacy = _safe_get(component, "bodies", label)
        if legacy is not None:
            collections.append(("bodies", legacy))
    bodies: List[Any] = []
    for collection_label, collection in collections:
        bodies.extend(
            _collection_items(collection, "{} of {}".format(collection_label, label))
        )
    return bodies


def _iter_component_bodies(
    component: Any, path: str, depth: int, seen: Dict[int, Any]
) -> Iterator[Tuple[Any, str]]:
    """Depth-first iterator over bodies with stable ordering and deduplication."""
    label = "component {!r}".format(path)
    for body in _component_bodies(component, path):
        marker = id(body)
        if marker in seen:
            continue
        # Keep the body alive: Fusion returns a new wrapper per access, and a
        # freed wrapper's id() can be reused by the next, distinct body.
        seen[marker] = body
        yield body, path

    if depth >= MAX_COMPONENT_DEPTH:
        logger.warning(
            "Reached the component nesting limit (%d) at %r; deeper components are skipped.",
            MAX_COMPONENT_DEPTH,
            path,
        )
        return

    occurrences = _collection_items(
        _safe_get(component, "occurrences", label), "occurrences of {}".format(label)
    )
    for occurrence in occurrences:
        occurrence_name = _display_name(
            occurrence, "name", "occurrence in {}".format(label), "<unnamed occurrence>"
        )
        child = _safe_get(occurrence, "component", "occurrence {!r}".format(occurrence_name))
        if child is None:
            logger.warning("Occurrence %r has no accessible component; skipping.", occurrence_name)
            continue
        child_path = "{}/{}".format(path, occurrence_name)
        yield from _iter_component_bodies(child, child_path, depth + 1, seen)


def _iter_design_bodies(design: Any) -> Iterator[Tuple[Any, str]]:
    """Iterate every body in the design, depth-first in stable order."""
    if design is None:
        return
    root_component = _safe_get(design, "rootComponent", "the active design")
    if root_component is None:
        logger.warning("The design has no root component; no bodies found.")
        return
    seen: Dict[int, Any] = {}
    yield from _iter_component_bodies(root_component, ROOT_PATH_LABEL, 0, seen)


def _material_identity(material: Any) -> str:
    """Return a stable grouping key for a material (its id, else its name)."""
    raw_identifier = _safe_get(material, "id", "a material")
    if isinstance(raw_identifier, str) and raw_identifier.strip():
        return raw_identifier.strip()
    return _display_name(material, "name", "a material", "<unnamed material>")


def _effective_appearance(body: Any, material: Any) -> Any:
    """Return the appearance currently effective for a body, if any."""
    override = _safe_get(body, "appearance", "a body")
    if override is not None:
        return override
    if material is not None:
        return _safe_get(material, "appearance", "a material")
    return None


def _material_density_g_cm3(material: Any) -> Optional[float]:
    """Read a material's density in g/cm^3 through the importer.

    The importer converts from the Density property's own units and reads
    ``materialProperties`` or the legacy ``physicalProperties``.
    """
    if material is None:
        return None
    importer_module = _load_importer()
    if importer_module is None:
        return None
    try:
        density = importer_module.get_material_density(material)
    except Exception:
        logger.exception("Could not read the density of a material.")
        return None
    if density is None:
        return None
    density = round(float(density), 4)
    return density if density > 0.0 else None


def _optional_catalog_density(entry: Any) -> Optional[float]:
    """Return a validated catalog density, or ``None`` when absent."""
    raw_density = _entry_field(entry, "density_g_cm3")
    if raw_density is None:
        return None
    if isinstance(raw_density, bool) or not isinstance(raw_density, (int, float)):
        raise MaterialsWorkflowError(
            "Catalog density_g_cm3 must be a number, got {!r}".format(raw_density)
        )
    density = float(raw_density)
    if not math.isfinite(density) or density <= 0.0:
        raise MaterialsWorkflowError(
            "Catalog density_g_cm3 must be a positive number, got {!r}".format(raw_density)
        )
    return density


def _catalog_entries_by_id(entries: Sequence[Any]) -> Dict[str, Any]:
    """Index catalog entries by their id for material-name resolution."""
    by_id: Dict[str, Any] = {}
    for entry in entries:
        raw_identifier = _entry_field(entry, "id")
        if isinstance(raw_identifier, str) and raw_identifier.strip():
            by_id[raw_identifier.strip()] = entry
    return by_id


def _humanize_material_name(name: str) -> str:
    """Convert an underscore catalog id into a readable fallback label."""
    text = str(name or "").strip()
    if not text:
        return "Unknown filament"
    if "_" in text and " " not in text:
        return text.replace("_", " ").strip().title()
    return text


def collect_assembly_materials(design: Any, entries: Sequence[Any]) -> List[DetectedMaterial]:
    """Detect the assembly's materials/filaments and assign automatic slots.

    Surface bodies (``isSolid`` False) are skipped because they cannot be
    printed.  Bodies are grouped by material identity (``material.id``, else
    ``material.name``); material-less bodies are grouped by their effective
    appearance using the synthetic id ``appearance:<name>|<color>``.  Slots are
    assigned by the order in which each group first appears in a depth-first walk
    of the root component bodies followed by nested occurrences (deduplicated).

    Catalog entries are matched by ``material.name == entry.id`` (the importer
    names Fusion materials after catalog ids).  When matched, the entry supplies
    color, type, category and density; otherwise the body's effective appearance
    color, type ``"PLA"`` and the material's Density physical property are used.

    Args:
        design: Active Fusion design.
        entries: Catalog entries used to enrich the detected materials.

    Returns:
        One :class:`DetectedMaterial` per unique material/appearance, in slot
        order (slot 1..N).

    Raises:
        NoMaterialsFoundError: If the assembly contains no bodies.
        MaterialsWorkflowError: If more than :data:`MAX_MATERIAL_SLOTS` unique
            materials are detected, or the design is unusable.
    """
    _require_design(design, "collect_assembly_materials")
    entries = list(entries or [])
    catalog_by_id = _catalog_entries_by_id(entries)
    groups: Dict[str, _MaterialGroup] = {}
    total_bodies = 0

    for body, path in _iter_design_bodies(design):
        if _safe_get(body, "isSolid", "a body") is False:
            # Surface bodies are not printable; mesh bodies have no isSolid.
            continue
        total_bodies += 1
        body_name = _display_name(body, "name", "body in {}".format(path), "<unnamed body>")
        material = _safe_get(body, "material", "body {!r}".format(body_name))
        effective_appearance = _effective_appearance(body, material)
        effective_color = _appearance_color_hex(effective_appearance)

        if material is not None:
            identity = _material_identity(material)
            group_key = "material:" + identity
            group = groups.get(group_key)
            if group is None:
                group = _MaterialGroup(
                    material=material,
                    appearance=effective_appearance,
                    material_id=identity,
                    material_name=_display_name(
                        material, "name", "a material", identity
                    ),
                    color_hex=effective_color,
                )
                groups[group_key] = group
        else:
            appearance_name = _display_name(
                effective_appearance, "name", "an appearance", "<no appearance>"
            )
            color_hex = effective_color or DEFAULT_APPEARANCE_COLOR
            synthetic_id = "appearance:{}|{}".format(appearance_name, color_hex)
            group = groups.get(synthetic_id)
            if group is None:
                group = _MaterialGroup(
                    material=None,
                    appearance=effective_appearance,
                    material_id=synthetic_id,
                    material_name=appearance_name,
                    color_hex=color_hex,
                )
                groups[synthetic_id] = group
        if body_name not in group.body_names:
            group.body_names.append(body_name)

    if total_bodies == 0:
        raise NoMaterialsFoundError(
            "No solid bodies were found in the active design. Add bodies and assign "
            "materials before exporting; otherwise there is nothing to map."
        )
    if len(groups) > MAX_MATERIAL_SLOTS:
        raise MaterialsWorkflowError(
            "Detected {} unique materials but the slicer handoff supports at most {} "
            "AMS/extruder slots. Reuse existing materials on bodies before exporting.".format(
                len(groups), MAX_MATERIAL_SLOTS
            )
        )

    catalog_by_display: Dict[str, Any] = {}
    for item in entries:
        label = str(_entry_field(item, "display_name", "") or "").strip()
        if label:
            catalog_by_display.setdefault(label, item)

    detected: List[DetectedMaterial] = []
    for slot, group in enumerate(groups.values(), start=1):
        entry = catalog_by_id.get(group.material_name) or catalog_by_display.get(
            str(group.material_name or "").strip()
        )
        manufacturer = ""
        product_line = ""
        color_name = ""
        orca_filament_profile = ""
        if entry is not None:
            color_hex = _entry_color_hex(entry)
            filament_type = _entry_field(entry, "slicer_filament_type") or DEFAULT_FILAMENT_TYPE
            category = _entry_category(entry)
            density = _optional_catalog_density(entry)
            display_name = _entry_field(entry, "display_name") or group.material_name
            manufacturer = str(_entry_field(entry, "manufacturer") or "")
            product_line = str(_entry_field(entry, "product_line") or "")
            color_name = str(_entry_field(entry, "color_name") or "")
            orca_filament_profile = str(_entry_field(entry, "orca_filament_profile") or "")
        else:
            color_hex = group.color_hex or DEFAULT_APPEARANCE_COLOR
            filament_type = DEFAULT_FILAMENT_TYPE
            category = DEFAULT_FILAMENT_TYPE
            density = _material_density_g_cm3(group.material)
            display_name = _humanize_material_name(group.material_name)
        detected.append(
            DetectedMaterial(
                material_id=group.material_id,
                material_name=group.material_name,
                display_name=_require_text(display_name, "detected display name"),
                color_hex=_normalized_hex(color_hex),
                filament_type=_require_text(filament_type, "detected filament type"),
                density_g_cm3=density,
                category=_require_text(category, "detected category"),
                body_names=tuple(group.body_names),
                slot=slot,
                manufacturer=manufacturer,
                product_line=product_line,
                color_name=color_name,
                orca_filament_profile=orca_filament_profile,
            )
        )

    logger.info(
        "Detected %d material(s) across %d body(ies): %s",
        len(detected),
        total_bodies,
        ", ".join(
            "slot {}={} ({})".format(item.slot, item.display_name, item.color_hex)
            for item in detected
        ),
    )
    return detected


# ---------------------------------------------------------------------------
# Mapping / filament slots
# ---------------------------------------------------------------------------


def build_mapping(materials: Sequence[DetectedMaterial]) -> Dict[str, int]:
    """Build the 3MF object-name to extruder-slot mapping.

    Args:
        materials: Detected materials in slot order.

    Returns:
        Mapping of every detected body name to its 1-based slot.

    Raises:
        MaterialsWorkflowError: If the same body name is detected under two
            different slots (the 3MF mapping can only assign one slot per name).
    """
    if materials is None:
        raise MaterialsWorkflowError("materials must be a sequence of DetectedMaterial, got None")
    mapping: Dict[str, int] = {}
    for detected in materials:
        for body_name in detected.body_names:
            existing_slot = mapping.get(body_name)
            if existing_slot is not None and existing_slot != detected.slot:
                raise MaterialsWorkflowError(
                    "Body name {!r} appears with two different filament slots ({} and {}). "
                    "The 3MF mapping can only assign one extruder per name; rename the "
                    "duplicate bodies or assign them the same material.".format(
                        body_name, existing_slot, detected.slot
                    )
                )
            mapping[body_name] = detected.slot
    return mapping


def build_filament_slots(materials: Sequence[DetectedMaterial]) -> Dict[int, Any]:
    """Build the slicer filament slots for the detected materials.

    Args:
        materials: Detected materials in slot order.

    Returns:
        Mapping of slot number to ``threemf_injector.FilamentSlot``.

    Raises:
        MaterialsWorkflowError: If the injector module is unavailable or a slot
            cannot be built.
    """
    if materials is None:
        raise MaterialsWorkflowError("materials must be a sequence of DetectedMaterial, got None")
    try:
        from . import threemf_injector
    except ImportError as exc:
        raise MaterialsWorkflowError(
            "The 3MF injector module (threemf_injector.py) is not available: {}".format(exc)
        ) from exc
    slots: Dict[int, Any] = {}
    for detected in materials:
        if detected.slot in slots:
            raise MaterialsWorkflowError(
                "Duplicate filament slot {} in the detected materials.".format(detected.slot)
            )
        try:
            slots[detected.slot] = threemf_injector.FilamentSlot(
                extruder=detected.slot,
                color_hex=detected.color_hex,
                filament_type=detected.filament_type,
                density_g_cm3=detected.density_g_cm3,
                name=detected.display_name,
            )
        except Exception as exc:
            raise MaterialsWorkflowError(
                "Could not build filament slot {} for {!r}: {}".format(
                    detected.slot, detected.display_name, exc
                )
            ) from exc
    return slots


# ---------------------------------------------------------------------------
# Temporary export appearances
# ---------------------------------------------------------------------------


def _appearance_source_kind(source_type: Any) -> Optional[str]:
    """Classify a Fusion ``appearanceSourceType`` value as ``"override"`` or ``"other"``.

    Fusion returns an enum value; older/newer API versions and the self-test
    doubles may expose it as a string.  Both are handled: any member whose name
    contains ``override`` means the body carries a local appearance override,
    every other recognisable source (material/component appearance) means there
    is no local override to restore.
    """
    if source_type is None:
        return None
    text = str(source_type).strip().lower()
    if "override" in text:
        return "override"
    if "material" in text:
        return "other"
    try:
        import adsk.core
    except ImportError:
        return None
    except Exception:  # pragma: no cover - depends on the Fusion runtime.
        logger.exception("Unexpected failure importing adsk.core.")
        return None
    for enum_name in ("AppearanceSourceTypes", "AppearanceSourceType"):
        enum_type = getattr(adsk.core, enum_name, None)
        if enum_type is None:
            continue
        try:
            member_names = dir(enum_type)
        except Exception:
            logger.debug("Could not enumerate %r members.", enum_name, exc_info=True)
            continue
        for member_name in member_names:
            if member_name.startswith("_"):
                continue
            member = getattr(enum_type, member_name, None)
            if member is None:
                continue
            try:
                if source_type != member:
                    continue
            except Exception:
                logger.debug("Could not compare appearanceSourceType values.", exc_info=True)
                continue
            lowered = member_name.lower()
            if "override" in lowered:
                return "override"
            return "other"
    return None


def _has_appearance_override(body: Any) -> bool:
    """Return whether a body currently has an appearance override."""
    source_type = _safe_get(body, "appearanceSourceType", "a body")
    kind = _appearance_source_kind(source_type)
    if kind is not None:
        return kind == "override"
    logger.debug(
        "Unrecognised appearanceSourceType %r; assuming an override when an appearance is set.",
        source_type,
    )
    return _safe_get(body, "appearance", "a body") is not None


def apply_export_appearances(
    design: Any,
    materials: Sequence[DetectedMaterial],
    source_appearance: Any = None,
    appearance_prefix: str = EXPORT_APPEARANCE_PREFIX,
) -> List[AppearanceBackup]:
    """Temporarily paint bodies with their slot colors for a 3MF export.

    One color-editable design appearance is created/reused per detected material
    (name = prefix + slot + material id + color), then every body listed in the
    detected materials receives that appearance as an override.  The previous
    override state is returned as backups so :func:`restore_appearances` can put
    the design back afterwards.

    Args:
        design: Active Fusion design.
        materials: Detected materials in slot order.
        source_appearance: Optional appearance copied for the new export
            appearances.
        appearance_prefix: Prefix for the temporary appearance names.

    Returns:
        One :class:`AppearanceBackup` per body that was overridden (each body
        once, even when its component is used by several occurrences), in
        traversal order.  Failures to set an individual body are logged and
        skipped.

    Raises:
        MaterialsWorkflowError: If the design is unusable, a body name maps to two
            different materials, or a temporary appearance cannot be created.
    """
    _require_design(design, "apply_export_appearances")
    if materials is None:
        raise MaterialsWorkflowError("materials must be a sequence of DetectedMaterial, got None")
    materials_list = list(materials)
    if not materials_list:
        logger.warning("No detections supplied; no export appearances were applied.")
        return []

    prefix = appearance_prefix if isinstance(appearance_prefix, str) else EXPORT_APPEARANCE_PREFIX
    appearance_by_body: Dict[str, Any] = {}
    appearance_by_slot: Dict[int, Any] = {}
    for detected in materials_list:
        appearance_name = "{}{:02d} {} {}".format(
            prefix, detected.slot, detected.material_id, detected.color_hex
        )
        appearance = appearance_by_slot.get(detected.slot)
        if appearance is None:
            legacy_names = (
                ("{}{}".format(LEGACY_EXPORT_APPEARANCE_PREFIX, appearance_name[len(prefix):]),)
                if prefix == EXPORT_APPEARANCE_PREFIX
                else ()
            )
            appearance = ensure_colored_appearance(
                design,
                appearance_name,
                detected.color_hex,
                source_appearance,
                legacy_names=legacy_names,
            )
            appearance_by_slot[detected.slot] = appearance
        for body_name in detected.body_names:
            existing = appearance_by_body.get(body_name)
            if existing is not None and existing is not appearance:
                raise MaterialsWorkflowError(
                    "Body name {!r} maps to two different export appearances; rename the "
                    "duplicate bodies before exporting.".format(body_name)
                )
            appearance_by_body[body_name] = appearance

    backups: List[AppearanceBackup] = []
    handled = _fh.BodySet()
    for body, path in _iter_design_bodies(design):
        body_name = _display_name(body, "name", "body in {}".format(path), "<unnamed body>")
        export_appearance = appearance_by_body.get(body_name)
        if export_appearance is None:
            continue
        # A component used by several occurrences yields its native body once per
        # occurrence; a second backup would record the export appearance as the
        # "original" and leave the body painted after the restore.
        if not handled.add(body):
            continue
        had_override = _has_appearance_override(body)
        original_appearance = _safe_get(body, "appearance", "a body") if had_override else None
        try:
            body.appearance = export_appearance
        except Exception:
            logger.exception(
                "Could not apply the export appearance to body %r; it keeps its "
                "current appearance.",
                body_name,
            )
            continue
        backups.append(
            AppearanceBackup(
                body=body, had_override=had_override, appearance=original_appearance
            )
        )
    logger.info("Applied %d temporary export appearance override(s).", len(backups))
    return backups


def restore_appearances(backups: Sequence[AppearanceBackup]) -> None:
    """Restore body appearances recorded by :func:`apply_export_appearances`.

    Bodies that had an override get their previous appearance back; bodies that
    had none get their override cleared (``body.appearance = None``).  Backups
    are restored newest first, so a body backed up twice ends with the state of
    its first backup.  This
    function never raises; individual failures are logged so a failed restore can
    never break the add-in's post-export cleanup.

    Args:
        backups: Backups returned by :func:`apply_export_appearances`.
    """
    if not backups:
        return
    restored = 0
    failures = 0
    for backup in reversed(list(backups)):
        body = backup.body
        if body is None:
            logger.warning("Skipping an appearance backup that has no body.")
            failures += 1
            continue
        target = backup.appearance if backup.had_override else None
        try:
            body.appearance = target
            restored += 1
        except Exception:
            failures += 1
            logger.exception("Could not restore the appearance of a body after export.")
    logger.info(
        "Restored %d body appearance(s) after export (%d failure(s)).", restored, failures
    )


# ---------------------------------------------------------------------------
# Temporary unique export names
# ---------------------------------------------------------------------------


def _owner_label(path: str) -> str:
    """Return a short owner label (``"TEF"`` for ``"Root/TEF:1"``) for a body path."""
    segment = str(path or "").rstrip("/").rsplit("/", 1)[-1]
    label = re.sub(r":\d+$", "", segment).strip()
    return label or segment or ROOT_PATH_LABEL


def apply_unique_export_names(design: Any) -> List[BodyNameBackup]:
    """Temporarily rename bodies that share a name so every 3MF object is unique.

    Fusion's 3MF export writes only the body name into each ``<object>``, so two
    ``Body1`` bodies in different components reach the injector as two objects
    with the same name and cannot get different filament slots.  The first body
    (in traversal order) keeps its name; every later body with the same name is
    renamed to ``"<name> (<component>)"`` (plus `` 2``, `` 3``... while taken).
    A component used by several occurrences holds one native body, so it is
    renamed at most once.

    Args:
        design: Active Fusion design.

    Returns:
        One :class:`BodyNameBackup` per renamed body; pass them to
        :func:`restore_body_names` after the export.  Bodies that cannot be
        renamed are logged and skipped (the mapping then reports the conflict).

    Raises:
        MaterialsWorkflowError: If the design is unusable.
    """
    _require_design(design, "apply_unique_export_names")
    bodies: List[Tuple[Any, str, str]] = []
    seen = _fh.BodySet()
    for body, path in _iter_design_bodies(design):
        if not seen.add(body):
            continue
        name = _display_name(body, "name", "body in {}".format(path), "<unnamed body>")
        bodies.append((body, path, name))

    taken: Set[str] = {name for _body, _path, name in bodies}
    claimed: Set[str] = set()
    backups: List[BodyNameBackup] = []
    for body, path, name in bodies:
        if name not in claimed:
            claimed.add(name)
            continue
        base = "{} ({})".format(name, _owner_label(path))
        candidate = base
        counter = 2
        while candidate in taken:
            candidate = "{} {}".format(base, counter)
            counter += 1
        try:
            body.name = candidate
        except Exception:
            logger.exception(
                "Could not rename body %r in %r for the 3MF export; it keeps its name.",
                name,
                path,
            )
            continue
        actual = _display_name(body, "name", "renamed body in {}".format(path), candidate)
        taken.add(actual)
        claimed.add(actual)
        backups.append(BodyNameBackup(body=body, name=name))
        logger.info("Renamed body %r in %r to %r for the 3MF export.", name, path, actual)
    return backups


def restore_body_names(backups: Sequence[BodyNameBackup]) -> None:
    """Restore body names recorded by :func:`apply_unique_export_names`.

    This function never raises; individual failures are logged so a failed
    restore can never break the add-in's post-export cleanup.

    Args:
        backups: Backups returned by :func:`apply_unique_export_names`.
    """
    if not backups:
        return
    restored = 0
    failures = 0
    for backup in reversed(list(backups)):
        try:
            backup.body.name = backup.name
            restored += 1
        except Exception:
            failures += 1
            logger.exception("Could not restore the name %r of a body after export.", backup.name)
    logger.info("Restored %d body name(s) after export (%d failure(s)).", restored, failures)


# ---------------------------------------------------------------------------
# Self-test doubles and checks (only used by the __main__ self-test)
# ---------------------------------------------------------------------------


class FakeColor:
    """Test double for ``adsk.core.Color``."""

    def __init__(self, red: int, green: int, blue: int, opacity: int = 255) -> None:
        self.red = red
        self.green = green
        self.blue = blue
        self.opacity = opacity


class FakeAppearance:
    """Test double for ``adsk.core.Appearance`` (color is editable)."""

    def __init__(self, name: str, color: Optional[FakeColor] = None) -> None:
        self.name = name
        self.color = color if color is not None else FakeColor(255, 255, 255, 255)


class FakeAppearances:
    """Test double for ``adsk.core.Appearances``."""

    def __init__(self, appearances: Optional[Sequence[FakeAppearance]] = None) -> None:
        self._appearances = list(appearances or [])

    @property
    def count(self) -> int:
        """Number of appearances."""
        return len(self._appearances)

    def item(self, index: int) -> Any:
        """Return the appearance at ``index``, or ``None``."""
        if 0 <= index < len(self._appearances):
            return self._appearances[index]
        return None

    def itemByName(self, name: str) -> Optional[FakeAppearance]:
        """Return the first appearance named ``name``, or ``None``."""
        for appearance in self._appearances:
            if appearance.name == name:
                return appearance
        return None

    def add(self, name: str) -> FakeAppearance:
        """Create a fresh white appearance."""
        if self.itemByName(name) is not None:
            raise RuntimeError("duplicate appearance name: {}".format(name))
        appearance = FakeAppearance(name)
        self._appearances.append(appearance)
        return appearance

    def addByCopy(self, source: FakeAppearance, name: str) -> FakeAppearance:
        """Create a colored copy of ``source``."""
        if self.itemByName(name) is not None:
            raise RuntimeError("duplicate appearance name: {}".format(name))
        if source is None:
            raise RuntimeError("no source appearance to copy")
        source_color = source.color
        appearance = FakeAppearance(
            name,
            FakeColor(source_color.red, source_color.green, source_color.blue, 255),
        )
        self._appearances.append(appearance)
        return appearance


class FakePhysicalProperty:
    """Test double for a Fusion physical property."""

    def __init__(self, name: str, value: Any, units: Optional[str] = None) -> None:
        self.name = name
        self.value = value
        self.units = units


class FakeProperties:
    """Test double for ``adsk.core.Properties``."""

    def __init__(self, properties: Optional[Sequence[FakePhysicalProperty]] = None) -> None:
        self._properties = {prop.name: prop for prop in (properties or [])}

    def itemByName(self, name: str) -> Any:
        """Return the named property, or ``None``."""
        return self._properties.get(name)


class FakeMaterial:
    """Test double for ``adsk.core.Material`` (appearance is read-only)."""

    def __init__(
        self,
        name: str,
        material_id: Optional[str] = None,
        appearance: Optional[FakeAppearance] = None,
        density_kg_m3: Optional[float] = None,
    ) -> None:
        self.name = name
        self._identifier = material_id if material_id is not None else name
        self._appearance = appearance if appearance is not None else FakeAppearance(name)
        properties = []
        if density_kg_m3 is not None:
            properties.append(
                FakePhysicalProperty(DENSITY_PROPERTY_NAME, density_kg_m3, "KilogramPerCubicMeter")
            )
        self.materialProperties = FakeProperties(properties)
        self.description: Optional[str] = None

    @property
    def id(self) -> str:
        """Stable material identifier."""
        return self._identifier

    @property
    def appearance(self) -> FakeAppearance:
        """Read-only appearance linked to this material."""
        return self._appearance


class FakeMaterials:
    """Test double for ``adsk.core.Materials``."""

    def __init__(self, materials: Optional[Sequence[FakeMaterial]] = None) -> None:
        self._materials = list(materials or [])

    @property
    def count(self) -> int:
        """Number of materials."""
        return len(self._materials)

    def item(self, index: int) -> Any:
        """Return the material at ``index``, or ``None``."""
        if 0 <= index < len(self._materials):
            return self._materials[index]
        return None

    def itemByName(self, name: str) -> Optional[FakeMaterial]:
        """Return the first material named ``name``, or ``None``."""
        for material in self._materials:
            if material.name == name:
                return material
        return None

    def add(self, material: FakeMaterial) -> FakeMaterial:
        """Register an already created fake material."""
        self._materials.append(material)
        return material

    def addByCopy(self, source: FakeMaterial, name: str) -> FakeMaterial:
        """Create a copy of ``source`` registered under ``name``."""
        if self.itemByName(name) is not None:
            raise RuntimeError("duplicate material name: {}".format(name))
        material = FakeMaterial(name, material_id="copy-" + name, appearance=source.appearance)
        self._materials.append(material)
        return material


class FakeBody:
    """Test double for ``adsk.fusion.BRepBody``."""

    def __init__(
        self,
        name: str,
        material: Optional[FakeMaterial] = None,
        appearance: Optional[FakeAppearance] = None,
        override: bool = False,
        raise_on_appearance_set: bool = False,
    ) -> None:
        self.name = name
        self.material = material
        self._appearance = appearance
        self._override = override
        self._raise_on_appearance_set = raise_on_appearance_set

    @property
    def appearance(self) -> Optional[FakeAppearance]:
        """Effective body appearance (override first, else the material's)."""
        if self._appearance is not None:
            return self._appearance
        if self.material is not None:
            return self.material.appearance
        return None

    @appearance.setter
    def appearance(self, value: Optional[FakeAppearance]) -> None:
        if self._raise_on_appearance_set:
            raise RuntimeError("simulated Fusion failure setting body.appearance")
        self._appearance = value
        self._override = value is not None

    @property
    def appearanceSourceType(self) -> str:
        """Mimic Fusion's appearance source enum with a string value."""
        return "OverrideAppearance" if self._override else "MaterialAppearance"


class FakeComponent:
    """Test double for ``adsk.fusion.Component``."""

    def __init__(
        self,
        name: str,
        bodies: Optional[Sequence[FakeBody]] = None,
        occurrences: Optional[Sequence["FakeOccurrence"]] = None,
    ) -> None:
        self.name = name
        self.bRepBodies = FakeCollection(bodies)
        self.meshBodies = FakeCollection([])
        self.occurrences = FakeCollection(occurrences)


class FakeLegacyComponent:
    """Test double for components exposing only the legacy ``bodies`` property."""

    def __init__(self, name: str, bodies: Optional[Sequence[FakeBody]] = None) -> None:
        self.name = name
        self.bodies = FakeCollection(bodies)
        self.occurrences = FakeCollection([])


class FakeOccurrence:
    """Test double for ``adsk.fusion.Occurrence``."""

    def __init__(self, name: str, component: Any) -> None:
        self.name = name
        self.component = component


class FakeCollection:
    """Test double for a Fusion ``count``/``item(index)`` collection."""

    def __init__(self, items: Optional[Sequence[Any]] = None) -> None:
        self._items = list(items or [])

    @property
    def count(self) -> int:
        """Number of items."""
        return len(self._items)

    def item(self, index: int) -> Any:
        """Return the item at ``index``, or ``None``."""
        if 0 <= index < len(self._items):
            return self._items[index]
        return None


class FakeDesign:
    """Test double for ``adsk.fusion.Design``."""

    def __init__(
        self,
        root_component: Any = None,
        materials: Optional[FakeMaterials] = None,
        appearances: Optional[FakeAppearances] = None,
    ) -> None:
        self.rootComponent = root_component
        self.materials = materials if materials is not None else FakeMaterials()
        self.appearances = appearances if appearances is not None else FakeAppearances()


class FakeCatalogEntry:
    """Test double for a filament catalog entry."""

    def __init__(
        self,
        entry_id: str,
        display_name: str,
        manufacturer: str,
        product_line: str,
        color_name: str,
        color_hex: str,
        slicer_filament_type: str,
        density_g_cm3: Optional[float],
        category: str,
    ) -> None:
        self.id = entry_id
        self.display_name = display_name
        self.manufacturer = manufacturer
        self.product_line = product_line
        self.color_name = color_name
        self.color_hex = color_hex
        self.slicer_filament_type = slicer_filament_type
        self.density_g_cm3 = density_g_cm3
        self.category = category


def _expect(condition: bool, message: str) -> None:
    """Raise AssertionError with ``message`` when ``condition`` is false."""
    if not condition:
        raise AssertionError(message)


def _install_fake_importer(calls: List[Any]) -> Any:
    """Install a fake ``importer`` module into ``sys.modules`` and return a restore callback."""

    def import_filament(design: FakeDesign, entry: Any) -> FakeMaterial:
        calls.append(entry)
        existing = design.materials.itemByName(entry.id)
        if existing is not None:
            return existing
        material = FakeMaterial(
            entry.id,
            material_id="fusion-" + entry.id,
            appearance=FakeAppearance(entry.id + " base"),
        )
        return design.materials.add(material)

    module = types.ModuleType("importer")
    module.import_filament = import_filament  # type: ignore[attr-defined]
    previous = testing.sibling_get(__package__, "importer")
    testing.sibling_set(__package__, "importer", module)

    def restore() -> None:
        if previous is None:
            testing.sibling_pop(__package__, "importer")
        else:
            testing.sibling_set(__package__, "importer", previous)

    return restore


def _build_assembly() -> Tuple[FakeDesign, FakeMaterial, FakeMaterial]:
    """Build the fake design graph used by the detection/export checks."""
    pla_material = FakeMaterial(
        "bambu_pla_basic_black",
        material_id="material-pla",
        appearance=FakeAppearance("PLA Black", FakeColor(22, 22, 22)),
        density_kg_m3=1240.0,
    )
    petg_material = FakeMaterial(
        "bambu_petg_hf_white",
        material_id="material-petg",
        appearance=FakeAppearance("PETG White", FakeColor(255, 255, 255)),
        density_kg_m3=1270.0,
    )
    custom_material = FakeMaterial(
        "Custom Blue",
        material_id="material-custom",
        appearance=FakeAppearance("Blue", FakeColor(0x33, 0x66, 0xCC)),
        density_kg_m3=1250.0,
    )
    nested_component = FakeComponent(
        "Bracket Assembly",
        bodies=[
            FakeBody("Bracket", material=pla_material),
            FakeBody("Bushing", material=custom_material),
            FakeBody(
                "Unassigned Plate",
                material=None,
                appearance=FakeAppearance("Rough Red", FakeColor(255, 0, 0)),
            ),
        ],
    )
    shared_component = FakeComponent(
        "Shared Component",
        bodies=[FakeBody("Shared Body", material=petg_material)],
    )
    root_component = FakeComponent(
        "Root Component",
        bodies=[FakeBody("Main Body", material=pla_material)],
        occurrences=[
            FakeOccurrence("Component:1", nested_component),
            FakeOccurrence("Component:2", shared_component),
            FakeOccurrence("Component:3", shared_component),
        ],
    )
    design = FakeDesign(root_component=root_component)
    return design, pla_material, petg_material


def _catalog_entries() -> List[FakeCatalogEntry]:
    """Catalog entries used by the detection checks."""
    return [
        FakeCatalogEntry(
            entry_id="bambu_pla_basic_black",
            display_name="Bambu Lab - PLA Basic - Black",
            manufacturer="Bambu Lab",
            product_line="PLA Basic",
            color_name="Black",
            color_hex="#161616",
            slicer_filament_type="PLA",
            density_g_cm3=1.24,
            category="PLA",
        ),
        FakeCatalogEntry(
            entry_id="bambu_petg_hf_white",
            display_name="Bambu Lab - PETG HF - White",
            manufacturer="Bambu Lab",
            product_line="PETG HF",
            color_name="White",
            color_hex="#FFFFFF",
            slicer_filament_type="PETG",
            density_g_cm3=1.27,
            category="PETG",
        ),
    ]


def _check_color_helpers() -> None:
    """Hex and Fusion color conversion helpers round-trip and validate input."""
    _expect(rgb_from_hex("#161616") == (22, 22, 22), "rgb_from_hex(#161616) must be (22, 22, 22)")
    _expect(rgb_from_hex("#ff6600") == (255, 102, 0), "rgb_from_hex must be case-insensitive")
    _expect(
        color_hex_from_color(FakeColor(255, 102, 0, 255)) == "#FF6600",
        "color_hex_from_color must return uppercase #RRGGBB",
    )
    _expect(
        color_hex_from_color(types.SimpleNamespace(red=1, green=2, blue=3)) == "#010203",
        "color_hex_from_color must tolerate a color without opacity",
    )
    invalid = 0
    for bad_hex in ("#12345", "161616", "#GGGGGG", None):
        try:
            rgb_from_hex(bad_hex)  # type: ignore[arg-type]
        except MaterialsWorkflowError:
            invalid += 1
    _expect(invalid == 4, "invalid hex strings must raise MaterialsWorkflowError")
    for bad_color in (None, "nope"):
        try:
            color_hex_from_color(bad_color)
        except MaterialsWorkflowError:
            pass
        else:
            raise AssertionError("invalid color values must raise MaterialsWorkflowError")


def _check_ensure_colored_appearance() -> None:
    """ensure_colored_appearance creates, colors and reuses design appearances."""
    source = FakeAppearance("Default", FakeColor(200, 200, 200))
    design = FakeDesign()
    appearance = ensure_colored_appearance(design, "FFB test", "#ff6600", source)
    _expect(appearance.name == "FFB test", "appearance name must be preserved")
    _expect(
        color_hex_from_color(appearance.color) == "#FF6600",
        "appearance color must be normalized to #FF6600",
    )
    _expect(design.appearances.count == 1, "exactly one appearance must be created")

    reused = ensure_colored_appearance(design, "FFB test", "#00FF00", source)
    _expect(reused is appearance, "existing appearance must be reused")
    _expect(design.appearances.count == 1, "reuse must not create a second appearance")
    _expect(
        color_hex_from_color(reused.color) == "#00FF00",
        "existing appearance color must be re-applied",
    )

    default_created = ensure_colored_appearance(design, "FFB other", "#112233")
    _expect(default_created is not None, "a default appearance must be created without a source")

    try:
        ensure_colored_appearance(design, "FFB bad", "not-a-color")
    except MaterialsWorkflowError:
        pass
    else:
        raise AssertionError("invalid appearance colors must raise MaterialsWorkflowError")


def _check_import_catalog_entries() -> None:
    """import_catalog_entries creates materials, descriptions and appearances."""
    calls: List[Any] = []
    restore = _install_fake_importer(calls)
    try:
        design = FakeDesign(root_component=FakeComponent("Root"))
        entries = _catalog_entries()
        source = FakeAppearance("Default", FakeColor(10, 20, 30))
        imported = import_catalog_entries(design, entries, source_appearance=source)

        _expect(len(imported) == 2, "both catalog entries must be imported")
        _expect(len(calls) == 2, "the importer module must be used for every entry")
        _expect(imported[0].catalog_id == "bambu_pla_basic_black", "catalog id mismatch")
        _expect(
            imported[0].material.name == "bambu_pla_basic_black",
            "material must be named after the catalog id",
        )
        description = imported[0].material.description or ""
        _expect(
            description.startswith(MATERIAL_DESCRIPTION_PREFIX),
            "material description must start with {}".format(MATERIAL_DESCRIPTION_PREFIX),
        )
        for fragment in ("PLA", "Bambu Lab", "PLA Basic", "Black"):
            _expect(
                fragment in description,
                "description missing {!r}: {}".format(fragment, description),
            )
        _expect(
            color_hex_from_color(imported[0].appearance.color) == "#161616",
            "appearance color must match the catalog entry",
        )
        _expect(
            imported[1].appearance.name == "Filamatch bambu_petg_hf_white",
            "appearance name must be 'Filamatch ' + catalog id",
        )

        again = import_catalog_entries(design, entries, source_appearance=source)
        _expect(len(again) == 2, "repeated import must still return both entries")
        _expect(design.materials.count == 2, "repeated import must not duplicate materials")
        _expect(design.appearances.count == 2, "repeated import must not duplicate appearances")

        legacy = FakeAppearance("FFB bambu_pla_basic_black", FakeColor(1, 1, 1))
        old_design = FakeDesign(
            root_component=FakeComponent("Root"), appearances=FakeAppearances([legacy])
        )
        reused = import_catalog_entries(old_design, entries[:1], source_appearance=source)
        _expect(reused[0].appearance is legacy, "an older 'FFB ' appearance must be reused")
        _expect(old_design.appearances.count == 1, "reusing 'FFB ' must not add an appearance")
        _expect(
            color_hex_from_color(legacy.color) == "#161616",
            "the reused 'FFB ' appearance must be recoloured",
        )
    finally:
        restore()


def _check_collect_assembly_materials() -> None:
    """collect_assembly_materials groups, orders and enriches detected materials."""
    design, _pla, _petg = _build_assembly()
    detected = collect_assembly_materials(design, _catalog_entries())

    _expect(len(detected) == 4, "expected 4 detected groups, got {}".format(len(detected)))
    _expect([item.slot for item in detected] == [1, 2, 3, 4], "slots must be 1..N in order")

    first = detected[0]
    _expect(first.slot == 1, "first detected material must be slot 1")
    _expect(first.material_name == "bambu_pla_basic_black", "catalog resolution by material name")
    _expect(first.material_id == "material-pla", "material id must come from material.id")
    _expect(
        first.display_name == "Bambu Lab - PLA Basic - Black",
        "display name from catalog must use Brand - Material - Color",
    )
    _expect(first.color_hex == "#161616", "catalog color must win over appearance color")
    _expect(first.filament_type == "PLA", "catalog filament type must be used")
    _expect(first.category == "PLA", "catalog category must be used")
    _expect(first.density_g_cm3 == 1.24, "catalog density must be used")
    _expect(
        first.body_names == ("Main Body", "Bracket"),
        "body grouping order mismatch: {}".format(first.body_names),
    )

    second = detected[1]
    _expect(second.material_name == "Custom Blue", "uncatalogued material name")
    _expect(second.color_hex == "#3366CC", "fallback color must come from the appearance")
    _expect(second.filament_type == DEFAULT_FILAMENT_TYPE, "fallback filament type must be PLA")
    _expect(second.category == DEFAULT_FILAMENT_TYPE, "fallback category must be PLA")
    _expect(
        second.density_g_cm3 == 1.25,
        "density must convert kg/m3 to g/cm3, got {}".format(second.density_g_cm3),
    )
    _expect(second.display_name == "Custom Blue", "fallback display name must be the material name")

    third = detected[2]
    _expect(third.slot == 3, "material-less appearance group must get slot 3")
    _expect(
        third.material_id.startswith("appearance:Rough Red|"),
        "material-less group id must be synthetic, got {}".format(third.material_id),
    )
    _expect(third.color_hex == "#FF0000", "material-less group color from its appearance")
    _expect(third.body_names == ("Unassigned Plate",), "material-less body grouping mismatch")
    _expect(third.density_g_cm3 is None, "material-less group must have no density")

    _expect(
        detected[3].material_name == "bambu_petg_hf_white",
        "shared-component material must be detected once",
    )
    _expect(
        detected[3].body_names == ("Shared Body",),
        "duplicate component instances must deduplicate bodies",
    )
    _expect(detected[3].slot == 4, "petg material must be slot 4")
    _expect(len(detected[3].body_names) == 1, "deduplicated bodies must appear once")


def _check_collect_edge_cases() -> None:
    """Detection rejects empty designs and more than MAX_MATERIAL_SLOTS groups."""
    empty_design = FakeDesign(root_component=FakeComponent("Empty Root"))
    try:
        collect_assembly_materials(empty_design, [])
    except NoMaterialsFoundError:
        pass
    else:
        raise AssertionError("an empty design must raise NoMaterialsFoundError")

    bodies = []
    for index in range(MAX_MATERIAL_SLOTS + 1):
        material = FakeMaterial(
            "material-{}".format(index),
            material_id="id-{}".format(index),
            appearance=FakeAppearance("Color {}".format(index), FakeColor(index, index, index)),
        )
        bodies.append(FakeBody("Body {}".format(index), material=material))
    crowded = FakeDesign(root_component=FakeComponent("Crowded", bodies=bodies))
    try:
        collect_assembly_materials(crowded, [])
    except MaterialsWorkflowError as exc:
        _expect(
            str(MAX_MATERIAL_SLOTS) in str(exc),
            "the >16 materials error must mention the slot limit: {}".format(exc),
        )
    else:
        raise AssertionError(
            "more than MAX_MATERIAL_SLOTS materials must raise MaterialsWorkflowError"
        )

    legacy_body = FakeBody(
        "Legacy Body",
        material=FakeMaterial("Legacy", material_id="legacy", density_kg_m3=1100.0),
    )
    legacy_design = FakeDesign(
        root_component=FakeLegacyComponent("Legacy Root", bodies=[legacy_body])
    )
    legacy_detected = collect_assembly_materials(legacy_design, [])
    _expect(
        len(legacy_detected) == 1 and legacy_detected[0].density_g_cm3 == 1.1,
        "legacy component.bodies must be traversed",
    )

    gram_material = FakeMaterial("Grams", material_id="grams")
    gram_material.materialProperties = FakeProperties(
        [FakePhysicalProperty(DENSITY_PROPERTY_NAME, 1.3, "GramPerCubicCentimeter")]
    )
    surface = FakeBody("Surface", material=FakeMaterial("Skin", material_id="skin"))
    surface.isSolid = False  # type: ignore[attr-defined]
    solid = FakeBody("Solid", material=gram_material)
    solid.isSolid = True  # type: ignore[attr-defined]
    mixed = FakeDesign(root_component=FakeComponent("Mixed", bodies=[surface, solid]))
    mixed_detected = collect_assembly_materials(mixed, None)  # type: ignore[arg-type]
    _expect(
        [item.material_name for item in mixed_detected] == ["Grams"],
        "surface bodies must be skipped and entries=None accepted, got {}".format(
            [item.material_name for item in mixed_detected]
        ),
    )
    _expect(
        mixed_detected[0].density_g_cm3 == 1.3,
        "density must follow the property units, got {}".format(mixed_detected[0].density_g_cm3),
    )
    only_surface = FakeDesign(root_component=FakeComponent("Skin", bodies=[surface]))
    try:
        collect_assembly_materials(only_surface, [])
    except NoMaterialsFoundError:
        pass
    else:
        raise AssertionError("a design with only surface bodies must raise NoMaterialsFoundError")

    try:
        collect_assembly_materials(None, [])  # type: ignore[arg-type]
    except MaterialsWorkflowError:
        pass
    else:
        raise AssertionError("collect_assembly_materials(None) must raise MaterialsWorkflowError")


def _check_mapping_and_slots() -> None:
    """build_mapping and build_filament_slots produce the slicer handoff data."""
    design, _pla, _petg = _build_assembly()
    detected = collect_assembly_materials(design, _catalog_entries())
    mapping = build_mapping(detected)

    _expect(
        mapping
        == {
            "Main Body": 1,
            "Bracket": 1,
            "Bushing": 2,
            "Unassigned Plate": 3,
            "Shared Body": 4,
        },
        "unexpected mapping: {}".format(mapping),
    )

    conflict_first = DetectedMaterial(
        material_id="a",
        material_name="a",
        display_name="A",
        color_hex="#111111",
        filament_type="PLA",
        density_g_cm3=None,
        category="PLA",
        body_names=("Duplicate",),
        slot=1,
    )
    conflict_second = DetectedMaterial(
        material_id="b",
        material_name="b",
        display_name="B",
        color_hex="#222222",
        filament_type="PLA",
        density_g_cm3=None,
        category="PLA",
        body_names=("Duplicate",),
        slot=2,
    )
    try:
        build_mapping([conflict_first, conflict_second])
    except MaterialsWorkflowError as exc:
        _expect(
            "Duplicate" in str(exc),
            "the duplicate-body error must name the body: {}".format(exc),
        )
    else:
        raise AssertionError("conflicting body names must raise MaterialsWorkflowError")

    slots = build_filament_slots(detected)
    _expect(set(slots) == {1, 2, 3, 4}, "one filament slot per detected material")
    slot_one = slots[1]
    _expect(slot_one.extruder == 1, "FilamentSlot extruder must be 1-based")
    _expect(slot_one.color_hex == "#161616", "FilamentSlot color must match the catalog")
    _expect(slot_one.filament_type == "PLA", "FilamentSlot type must match the catalog")
    _expect(slot_one.density_g_cm3 == 1.24, "FilamentSlot density must match the catalog")
    _expect(
        slot_one.name == "Bambu Lab - PLA Basic - Black",
        "FilamentSlot name must be the detected display name",
    )


def _check_export_appearances() -> None:
    """apply_export_appearances paints bodies and restore_appearances undoes it."""
    design, pla_material, petg_material = _build_assembly()
    detected = collect_assembly_materials(design, _catalog_entries())
    source = FakeAppearance("Default", FakeColor(10, 20, 30))
    original_override = FakeAppearance("Manual Override", FakeColor(9, 9, 9))
    first_body = design.rootComponent.bRepBodies.item(0)
    first_body.appearance = original_override
    shared_body = None
    for body, _path in _iter_design_bodies(design):
        if body.name == "Bracket":
            body.appearance = original_override
        if body.name == "Shared Body":
            shared_body = body

    backups = apply_export_appearances(design, detected, source_appearance=source)
    _expect(len(backups) == 5, "expected one backup per mapped body, got {}".format(len(backups)))
    _expect(shared_body is not None, "shared body must be found by traversal")

    override_backup = [backup for backup in backups if backup.had_override]
    no_override_backup = [backup for backup in backups if not backup.had_override]
    _expect(len(override_backup) == 2, "two bodies had manual overrides")
    _expect(len(no_override_backup) == 3, "three bodies had no override")
    _expect(
        override_backup[0].appearance is original_override,
        "the previous override appearance must be recorded",
    )
    for body, _path in _iter_design_bodies(design):
        appearance = body.appearance
        _expect(
            appearance is not None and str(appearance.name).startswith(EXPORT_APPEARANCE_PREFIX),
            "body {!r} must receive a temporary export appearance".format(body.name),
        )

    restore_appearances(backups)
    _expect(first_body.appearance is original_override, "first body override must be restored")
    _expect(
        shared_body.appearance is petg_material.appearance,
        "bodies without an override must fall back to their material appearance again",
    )
    _expect(
        len([backup for backup in backups if not backup.had_override]) == 3,
        "backup had_override flags must stay stable",
    )

    broken = FakeBody(
        "Broken", material=pla_material, raise_on_appearance_set=True, override=False
    )
    broken_backup = AppearanceBackup(body=broken, had_override=True, appearance=original_override)
    restore_appearances(
        [broken_backup, AppearanceBackup(body=None, had_override=False, appearance=None)]
    )  # must never raise

    _expect(
        apply_export_appearances(design, [], source) == [],
        "empty detections must yield no backups",
    )


class FakeBodyView:
    """A fresh wrapper per access around one native body, like Fusion's API.

    Wrappers of one body compare equal and share its entity token, but are
    different Python objects, so ``id()`` cannot deduplicate them.
    """

    def __init__(self, native: FakeBody, token: Optional[str]) -> None:
        object.__setattr__(self, "_native", native)
        object.__setattr__(self, "_token", token)

    def __getattr__(self, name: str) -> Any:
        if name == "entityToken":
            if self._token is None:
                raise AttributeError(name)
            return self._token
        return getattr(self._native, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._native, name, value)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, FakeBodyView) and other._native is self._native

    __hash__ = None  # type: ignore[assignment]


class FakeViewCollection(FakeCollection):
    """Collection that returns a new :class:`FakeBodyView` on every ``item`` call."""

    def __init__(self, bodies: Sequence[FakeBody], tokens: bool) -> None:
        super().__init__(bodies)
        self._tokens = tokens
        self.views: List[FakeBodyView] = []

    def item(self, index: int) -> Any:
        native = super().item(index)
        if native is None:
            return None
        view = FakeBodyView(native, "token-{}".format(native.name) if self._tokens else None)
        self.views.append(view)
        return view


def _check_shared_component_export() -> None:
    """A body in a component used twice is backed up once and restored unpainted."""
    for tokens in (True, False):
        petg = FakeMaterial(
            "bambu_petg_hf_white",
            material_id="material-petg",
            appearance=FakeAppearance("PETG White", FakeColor(255, 255, 255)),
        )
        native = FakeBody("Shared Body", material=petg)
        shared = FakeComponent("Shared")
        shared.bRepBodies = FakeViewCollection([native], tokens)
        legacy_export = FakeAppearance("FFB Export 01 material-petg #FFFFFF", FakeColor(0, 0, 0))
        design = FakeDesign(
            root_component=FakeComponent(
                "Root",
                occurrences=[FakeOccurrence("Shared:1", shared), FakeOccurrence("Shared:2", shared)],
            ),
            appearances=FakeAppearances([legacy_export]),
        )
        detected = collect_assembly_materials(design, _catalog_entries())
        backups = apply_export_appearances(design, detected)
        _expect(len(backups) == 1, "the shared body must be backed up once, got {}".format(len(backups)))
        _expect(native._appearance is legacy_export, "an older 'FFB Export' appearance must be reused")
        _expect(design.appearances.count == 1, "reusing 'FFB Export' must not add an appearance")
        restore_appearances(backups)
        _expect(
            native._appearance is None and not native._override,
            "the shared body must lose the export appearance after the restore",
        )
        _expect(apply_unique_export_names(design) == [], "one shared body must not be renamed")

    doubled = FakeBody("Doubled", material=petg)
    restore_appearances(
        [
            AppearanceBackup(body=doubled, had_override=False, appearance=None),
            AppearanceBackup(body=doubled, had_override=True, appearance=legacy_export),
        ]
    )
    _expect(doubled._appearance is None, "restore must run newest first so the first backup wins")


def _check_find_source_appearance() -> None:
    """find_source_appearance prefers design materials and falls back to libraries."""
    design_appearance = FakeAppearance("Design Source", FakeColor(1, 2, 3))
    design = FakeDesign(
        materials=FakeMaterials([FakeMaterial("Design Material", appearance=design_appearance)])
    )
    _expect(
        find_source_appearance(design) is design_appearance,
        "the first design material appearance must be used as the source",
    )

    library_material = FakeMaterial("Library Material", appearance=FakeAppearance("Library Source"))
    fake_library = types.SimpleNamespace(materials=FakeMaterials([library_material]))
    fake_app = types.SimpleNamespace(materialLibraries=FakeCollection([fake_library]))
    empty_design = FakeDesign()
    _expect(
        find_source_appearance(empty_design, app=fake_app) is library_material.appearance,
        "the first library material appearance must be used when the design has none",
    )
    _expect(
        find_source_appearance(empty_design) is None,
        "find_source_appearance must return None when nothing is available",
    )


class FakeLockedBody(FakeBody):
    """Body whose name cannot be changed (e.g. inside a linked component)."""

    def __setattr__(self, key: str, value: Any) -> None:
        if key == "name" and "name" in self.__dict__:
            raise RuntimeError("simulated Fusion failure renaming a body")
        super().__setattr__(key, value)


def _check_unique_export_names() -> None:
    """apply_unique_export_names fixes duplicate names and restore puts them back."""
    pla, petg = _build_assembly()[1:]
    root_body = FakeBody("Body1", material=pla)
    tef_body = FakeBody("Body1", material=petg)
    taken_body = FakeBody("Body1 (TEF)", material=pla)
    shared_body = FakeBody("Shared Body", material=petg)
    shared = FakeComponent("Shared", bodies=[shared_body])
    design = FakeDesign(
        root_component=FakeComponent(
            "Root Component",
            bodies=[root_body, taken_body],
            occurrences=[
                FakeOccurrence("TEF:1", FakeComponent("TEF", bodies=[tef_body])),
                FakeOccurrence("Shared:1", shared),
                FakeOccurrence("Shared:2", shared),
            ],
        )
    )

    try:
        build_mapping(collect_assembly_materials(design, []))
    except MaterialsWorkflowError:
        pass
    else:
        raise AssertionError("duplicate body names must still conflict without renaming")

    backups = apply_unique_export_names(design)
    _expect(root_body.name == "Body1", "the first body keeps its name")
    _expect(
        tef_body.name == "Body1 (TEF) 2",
        "a duplicate gets its component name and skips taken names: {!r}".format(tef_body.name),
    )
    _expect(shared_body.name == "Shared Body", "one body shared by two occurrences is not renamed")
    _expect(len(backups) == 1, "only the duplicate body is recorded")
    mapping = build_mapping(collect_assembly_materials(design, []))
    _expect(
        mapping.get("Body1") != mapping.get("Body1 (TEF) 2"),
        "renamed bodies map to different slots: {}".format(mapping),
    )

    restore_body_names(backups)
    _expect(tef_body.name == "Body1", "restore_body_names puts the original name back")
    restore_body_names([])

    locked = FakeLockedBody("Body1", material=petg)
    locked_design = FakeDesign(
        root_component=FakeComponent(
            "Root Component",
            bodies=[FakeBody("Body1", material=pla)],
            occurrences=[FakeOccurrence("Linked:1", FakeComponent("Linked", bodies=[locked]))],
        )
    )
    _expect(
        apply_unique_export_names(locked_design) == [],
        "a body that cannot be renamed is skipped without raising",
    )
    restore_body_names([BodyNameBackup(body=locked, name="Body1")])


def _selftest() -> int:
    """Run the materials-workflow self-test against fake Fusion objects.

    Returns:
        Process exit code: 0 when every check passed, 1 otherwise.
    """
    scenarios = (
        ("color helpers convert hex and Fusion colors", _check_color_helpers),
        ("find_source_appearance prefers the design then libraries", _check_find_source_appearance),
        (
            "ensure_colored_appearance creates, colors and reuses appearances",
            _check_ensure_colored_appearance,
        ),
        (
            "import_catalog_entries sets descriptions and appearance colors",
            _check_import_catalog_entries,
        ),
        (
            "collect_assembly_materials groups, orders and enriches materials",
            _check_collect_assembly_materials,
        ),
        (
            "collect_assembly_materials handles empty/oversized/legacy designs",
            _check_collect_edge_cases,
        ),
        ("build_mapping and build_filament_slots build the handoff data", _check_mapping_and_slots),
        (
            "apply_export_appearances and restore_appearances round-trip safely",
            _check_export_appearances,
        ),
        (
            "apply_unique_export_names and restore_body_names round-trip safely",
            _check_unique_export_names,
        ),
        (
            "a body in a component used twice is painted and restored once",
            _check_shared_component_export,
        ),
    )
    try:
        for label, scenario in scenarios:
            scenario()
            print("PASS: {}".format(label))
    except Exception:
        traceback.print_exc()
        print("FAIL: materials_workflow self-test aborted", file=sys.stderr)
        return 1
    print("PASS: all materials_workflow self-tests completed")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(_selftest())
