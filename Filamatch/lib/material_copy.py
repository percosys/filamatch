# Moved from Printegrity 05a4847 Printegrity/lib/material_copy.py (now part of Filamatch's As printed tab)
"""Give each body its own as-printed material, put the filament back, and clean up.

Apply gives each filament body its own design-material copy of its filament
material, named ``<filament> · <body> · as printed``, that holds the body's
printed density, so Fusion's mass is the printed mass. The density depends
on the body's shape, so bodies never share a copy. Applying again updates the
copy the body wears in place (found by what the body wears, not by its
entity token: Fusion gives a body a new token when it changes). Restore puts
the filament material back.

The copy names its source in its description, in the format agreed with
Filamatch (Filamatch commit e227a25 reads it):

    As printed | filamatch_source=<source name> | <settings label> | as_printed_id=<12 hex>
      | settings_id=<12 hex> | from=<slicer|model> | volume_cm3=<volume>

``as_printed_id`` comes from the source and the body's entity token when
the copy is made, and then stays. ``settings_id`` is a fingerprint of every print
setting the copy was made for. ``from`` and ``volume_cm3`` say where the
density came from and for which volume, so a slicer check's density is kept
while the body's settings and shape are unchanged.

Copies made before the move into Filamatch start with ``Printegrity |``
and hold ``printegrity_id=``; they are still read, never written (a body
that wears one gets its own copy on the next Apply).

So Filamatch still sees the filament, and takes its density, type and
colour from its own catalog, never from the copy.

Rules (CLAUDE.md): only design materials are made. Only materials whose
description starts with ``As printed |`` (or the older ``Printegrity |``)
are ever written to or deleted, and only when no body uses them
(:func:`clean_up`, after Fusion's own ``isUsed``; Fusion refuses to delete a
used material too). Filamatch's filament materials, its library and
Favorites are never touched. Copies keep the source's Fusion material id
(Fusion does that), so they are found by the id in the description.

Fusion API calls only: run this on Fusion's UI thread.

Self-test with fake Fusion objects: ``python3 -m Filamatch.lib.material_copy``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import config

LOGGER = logging.getLogger(config.ADDIN_NAME + ".material_copy")

DESCRIPTION_PREFIX = "As printed | "
NAME_SUFFIX = " · as printed"
#: Prefixes of copies made before the move into Filamatch (still read, never written).
OLD_DESCRIPTION_PREFIXES = ("Printegrity | ",)
DENSITY_PROPERTY_ID = "structural_Density"
#: Densities closer than this (relative) are the same; a copy within it is not rewritten.
DENSITY_TOLERANCE = 0.002

#: The same pattern Filamatch reads the source with (agreed 2026-11-08).
SOURCE_PATTERN = re.compile(r"(?:^|\|)\s*filamatch_source=([^|]+?)\s*(?=\||$)")
#: as_printed_id= (and printegrity_id= of older copies).
COPY_ID_PATTERN = re.compile(r"(?:^|\|)\s*(?:as_printed|printegrity)_id=([0-9a-f]{12})\s*(?=\||$)")
SETTINGS_ID_PATTERN = re.compile(r"(?:^|\|)\s*settings_id=([0-9a-f]{12})\s*(?=\||$)")
FROM_PATTERN = re.compile(r"(?:^|\|)\s*from=(slicer|model)\s*(?=\||$)")
VOLUME_PATTERN = re.compile(r"(?:^|\|)\s*volume_cm3=([0-9.]+)\s*(?=\||$)")
VALUES_PATTERN = re.compile(r"(?:^|\|)\s*values_id=([0-9a-f]{12})\s*(?=\||$)")
#: Part of values_id: a new writer (a fix in write_values) writes every copy once again.
#: 2: Fusion's unit name "Kilopascal" (version 1 wrote only Poisson's ratio).
VALUES_WRITER = "2"


def values_fingerprint(values_key: str) -> str:
    """values_id: the values and the writer that wrote them ("" without values)."""
    return settings_fingerprint("{}|writer={}".format(values_key, VALUES_WRITER)) if values_key else ""

#: Fusion's stiffness and strength properties: (id, StrengthEstimate key, is a stress).
MECHANICAL_PROPERTIES = (
    ("structural_Young_modulus", "e_mpa", True),
    ("structural_Poisson_ratio", "poisson", False),
    ("structural_Shear_modulus", "shear_mpa", True),
    ("structural_Minimum_yield_stress", "yield_mpa", True),
    ("structural_Minimum_tensile_strength", "tensile_mpa", True),
)
#: The behavior choice: Simulation uses isotropic materials only.
BEHAVIOR_PROPERTY_ID = "structural_Behavior"
ISOTROPIC = "structural_isotropic"
#: One MPa in the units Fusion reports for a stress property (FloatProperty.units: Fusion says
#: "Kilopascal"; short names too). Keys are lower case, without spaces.
STRESS_UNITS = {"pa": 1e6, "pascal": 1e6, "n/m^2": 1e6, "n/m2": 1e6,
                "kpa": 1e3, "kilopascal": 1e3, "kn/m^2": 1e3,
                "mpa": 1.0, "megapascal": 1.0, "n/mm^2": 1.0, "n/mm2": 1.0,
                "gpa": 1e-3, "gigapascal": 1e-3,
                "psi": 145.0377377, "poundforcepersquareinch": 145.0377377, "ksi": 0.1450377377}


def stress_factor(units: Any) -> Optional[float]:
    """One MPa in these units (None when unknown)."""
    return STRESS_UNITS.get(re.sub(r"[\s_]+", "", str(units or "")).lower())


class MaterialCopyError(RuntimeError):
    """A body could not be given its as-printed material, said in plain words."""


@dataclasses.dataclass(frozen=True)
class BodyResult:
    """What Apply or Restore did to one body."""

    name: str
    path: str
    material: str = ""
    density_g_cm3: Optional[float] = None
    source: str = ""
    error: str = ""
    #: Something Fusion did not take (a stiffness or strength value), in plain words.
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ApplyItem:
    """One body to apply: who it is, its density for Fusion and the settings it is for."""

    body: Any
    #: The native body's entity token: one copy per body.
    token: str
    #: How the copy names the body ("Body1 in Lid").
    body_label: str
    path: str
    density_g_cm3: float
    #: "slicer" or "model".
    origin: str
    label: str
    settings_key: str
    volume_cm3: float
    #: Stiffness and strength for Fusion (MPa; strength_model.StrengthEstimate keys), or None
    #: to keep the filament's own values.
    values: Optional[Mapping[str, float]] = None
    #: strength_model.StrengthEstimate.key(): tells when the values change.
    values_key: str = ""


def is_copy(material: Any) -> bool:
    """True for an as-printed copy (found by its description, never by its name)."""
    try:
        return str(material.description or "").startswith((DESCRIPTION_PREFIX,) + OLD_DESCRIPTION_PREFIXES)
    except Exception:
        return False


def _field(material: Any, pattern: "re.Pattern[str]") -> str:
    try:
        match = pattern.search(str(material.description or ""))
    except Exception:
        return ""
    return match.group(1).strip() if match else ""


def source_name(material: Any) -> str:
    """The name of the material a copy was made from ("" when it names none)."""
    return _field(material, SOURCE_PATTERN)


def copy_id(material: Any) -> str:
    """The as-printed id in a copy's description ("" when it has none)."""
    return _field(material, COPY_ID_PATTERN)


def density_origin(material: Any) -> str:
    """Where a copy's density came from: "slicer", "model" or "" (older copies)."""
    return _field(material, FROM_PATTERN)


def copy_volume(material: Any) -> Optional[float]:
    """The body volume (cm³) a copy's density is for (None for older copies)."""
    text = _field(material, VOLUME_PATTERN)
    try:
        return float(text) if text else None
    except ValueError:
        return None


def settings_fingerprint(settings_key: str) -> str:
    """A short fingerprint of every print setting (:meth:`mass_model.PrintSettings.key`)."""
    return hashlib.sha1(settings_key.encode("utf-8")).hexdigest()[:12]


def copy_label(material: Any) -> str:
    """The settings label in a copy's description (the third part; "" when there is none)."""
    try:
        parts = [part.strip() for part in str(material.description or "").split("|")]
    except Exception:
        return ""
    return parts[2] if len(parts) > 3 else ""


def is_current(material: Any, settings_key: str, label: str) -> bool:
    """True when a copy was made for these settings.

    Copies hold a fingerprint of every setting; older ones only a label,
    which is compared instead.
    """
    try:
        match = SETTINGS_ID_PATTERN.search(str(material.description or ""))
    except Exception:
        return False
    if match:
        return match.group(1) == settings_fingerprint(settings_key)
    return copy_label(material) == label


def new_copy_id(source: str, token: str) -> str:
    """A new copy's id, from its filament and its body (it stays when the body's token changes)."""
    return hashlib.sha1("{}|{}".format(source, token).encode("utf-8")).hexdigest()[:12]


def copy_name(source: str, body_label: str) -> str:
    """``<filament> · <body> · as printed``: the Material Browser shows whose copy it is."""
    return "{} · {}{}".format(source, body_label, NAME_SUFFIX)


def copy_description(source: str, label: str, identifier: str, settings_key: str = "",
                     origin: str = "", volume_cm3: Optional[float] = None, values_key: str = "") -> str:
    """The agreed description. The source name must not hold "|" (Filamatch could not read it)."""
    if "|" in source:
        raise MaterialCopyError("The material name {!r} holds a \"|\", so Filamatch could not read it.".format(source))
    if "|" in label:
        raise MaterialCopyError("The settings label holds a \"|\".")
    text = "{}filamatch_source={} | {} | as_printed_id={}".format(DESCRIPTION_PREFIX, source, label, identifier)
    if settings_key:
        text += " | settings_id={}".format(settings_fingerprint(settings_key))
    if origin in ("slicer", "model"):
        text += " | from={}".format(origin)
    if volume_cm3 is not None:
        text += " | volume_cm3={:.4f}".format(volume_cm3)
    if values_key:
        text += " | values_id={}".format(values_fingerprint(values_key))
    return text


def _materials(design: Any) -> List[Any]:
    collection = design.materials
    return [collection.item(index) for index in range(int(collection.count))]


def solid_material(design: Any, material: Any) -> Optional[Any]:
    """The filament material behind a body's material: itself, or a copy's source (None when it is gone)."""
    if material is None or not is_copy(material):
        return material
    name = source_name(material)
    if not name:
        return None
    try:
        return design.materials.itemByName(name)
    except Exception:
        return None


def _density_property(material: Any) -> Any:
    prop = material.materialProperties.itemById(DENSITY_PROPERTY_ID)
    if prop is None:
        raise MaterialCopyError("The material {!r} has no density in Fusion.".format(material.name))
    return prop


def copy_density(material: Any) -> Optional[float]:
    """A copy's density in g/cm³ (None when Fusion gives none)."""
    try:
        value = float(_density_property(material).value)
    except Exception:
        return None
    return value / 1000.0 if value > 0 else None


def use_counts(bodies: Sequence[Any]) -> Dict[str, int]:
    """How many of these bodies wear each material, by material name."""
    counts: Dict[str, int] = {}
    for body in bodies:
        try:
            name = str(body.material.name)
        except Exception:
            continue
        counts[name] = counts.get(name, 0) + 1
    return counts


def is_own_copy(material: Any, source: str, uses: Mapping[str, int]) -> bool:
    """True when the material is a copy of this filament that no other body wears."""
    try:
        name = str(material.name)
    except Exception:
        return False
    return is_copy(material) and source_name(material) == source and uses.get(name, 1) <= 1


def needs_apply(material: Any, source: str, settings_key: str, label: str, density_g_cm3: float,
                origin: str, volume_cm3: float, uses: Optional[Mapping[str, int]] = None,
                values_key: str = "") -> bool:
    """True when a body's material is not its own up-to-date copy.

    Up to date: a copy of the body's filament that no other body wears
    (``uses``: bodies per material name), made for these settings and these
    stiffness and strength values, with this density, or with a slicer
    check's density for the same volume while only the model's estimate is
    at hand.
    """
    if not is_own_copy(material, source, uses or {}):
        return True
    if not is_current(material, settings_key, label):
        return True
    held = _field(material, VALUES_PATTERN)
    if held != values_fingerprint(values_key):
        return True
    volume = copy_volume(material)
    same_shape = volume is not None and abs(volume - volume_cm3) <= 1e-4 * max(1.0, volume_cm3)
    if origin == "model" and density_origin(material) == "slicer" and same_shape:
        return False
    current = copy_density(material)
    return current is None or abs(current - density_g_cm3) > DENSITY_TOLERANCE * density_g_cm3


def _set(prop: Any, value: float) -> None:
    """Set a property; a direction-dependent one (several values) gets the value in every direction."""
    if getattr(prop, "hasMultipleValues", False):
        prop.values = [value] * max(1, len(list(prop.values)))
    else:
        prop.value = value


def read_values(material: Any) -> Optional[Dict[str, float]]:
    """A material's own stiffness and strength (MPa; StrengthEstimate keys), or None when Fusion gives none.

    A direction-dependent value gives its lowest direction.
    """
    found: Dict[str, float] = {}
    try:
        for prop_id, key, is_stress in MECHANICAL_PROPERTIES:
            prop = material.materialProperties.itemById(prop_id)
            if prop is None:
                continue
            value = min(prop.values) if getattr(prop, "hasMultipleValues", False) and prop.values else prop.value
            value = float(value)
            if is_stress:
                factor = stress_factor(getattr(prop, "units", ""))
                if factor is None:
                    continue
                value /= factor
            if value > 0:
                found[key] = value
    except Exception:
        return None
    return found if "e_mpa" in found and "tensile_mpa" in found else None


def write_values(material: Any, values: Optional[Mapping[str, float]], source: Any) -> str:
    """Write stiffness and strength into a copy (MPa, converted to Fusion's units).

    ``values`` None puts the filament's own values back. With values, the
    copy's behavior becomes isotropic (Fusion Simulation uses only isotropic
    materials). Returns what Fusion did not take, in plain words ("" when all
    went in).
    """
    left_out = []
    behavior = material.materialProperties.itemById(BEHAVIOR_PROPERTY_ID)
    if behavior is not None:
        original = source.materialProperties.itemById(BEHAVIOR_PROPERTY_ID) if source is not None else None
        wanted = ISOTROPIC if values is not None else (str(original.value) if original is not None else None)
        if wanted is not None and str(behavior.value) != wanted:
            try:
                behavior.value = wanted
            except Exception:
                left_out.append("isotropic behavior")
    for prop_id, key, is_stress in MECHANICAL_PROPERTIES:
        prop = material.materialProperties.itemById(prop_id)
        if prop is None:
            left_out.append(prop_id.replace("structural_", "").replace("_", " ").lower())
            continue
        if values is None:
            original = source.materialProperties.itemById(prop_id) if source is not None else None
            if original is not None:
                _set(prop, float(original.value))
            continue
        number = float(values[key])
        if is_stress:
            factor = stress_factor(getattr(prop, "units", ""))
            if factor is None:
                left_out.append("{} (unknown unit {!r})".format(prop_id.replace("structural_", "").replace("_", " ").lower(),
                                                                  getattr(prop, "units", "")))
                continue
            number *= factor
        _set(prop, number)
    return "Fusion did not take: {}.".format(", ".join(left_out)) if left_out else ""


def _free_name(design: Any, wanted: str, identifier: str, mine: Any = None) -> str:
    """``wanted`` when it is free (an unused copy that holds it is deleted first), else ``wanted · <id>``."""
    try:
        other = design.materials.itemByName(wanted)
    except Exception:
        other = None
    if other is None or (mine is not None and other == mine):
        return wanted
    try:
        if is_copy(other) and not other.isUsed and other.deleteMe():
            return wanted
    except Exception:
        LOGGER.exception("Could not delete an unused copy that holds the name %r", wanted)
    return "{} · {}".format(wanted, identifier[:6])


def apply(design: Any, items: Sequence[ApplyItem], bodies: Sequence[Any] = ()) -> List[BodyResult]:
    """Give each body its own copy: the copy it wears, updated in place, or a new one.

    Args:
        bodies: every body of the design, to tell a copy no other body
            wears (the body's own) from one it shares; without them only the
            items are counted.
    """
    uses = use_counts(list(bodies) or [item.body for item in items])
    results = []
    for item in items:
        body = item.body
        name = str(body.name)
        try:
            density = float(item.density_g_cm3)
            if not 0.05 <= density <= 5.0:
                raise MaterialCopyError("{:.3f} g/cm³ is not a believable printed density.".format(density))
            current = body.material
            source = solid_material(design, current)
            if source is None:
                raise MaterialCopyError("The filament this body's copy came from is no longer in the design.")
            if is_copy(source):
                raise MaterialCopyError("An as-printed copy cannot be the source of another copy.")
            source_text = str(source.name)
            wanted = copy_name(source_text, item.body_label)
            if is_own_copy(current, source_text, uses):
                material = current
                identifier = copy_id(current) or new_copy_id(source_text, item.token)
            else:
                identifier = new_copy_id(source_text, item.token)
                material = design.materials.addByCopy(source, _free_name(design, wanted, identifier))
                if material is None:
                    raise MaterialCopyError("Fusion did not make a copy of {!r}.".format(source_text))
                LOGGER.info("Made the material copy %r", material.name)
            description = copy_description(source_text, item.label, identifier, item.settings_key,
                                           item.origin, item.volume_cm3,
                                           item.values_key if item.values is not None else "")
            if str(material.description or "") != description:
                material.description = description
            if not is_copy(material):
                raise MaterialCopyError("Fusion did not keep the copy's description, so it is not written to.")
            if str(material.name) != wanted:
                renamed = _free_name(design, wanted, identifier, material)
                if str(material.name) != renamed:
                    try:
                        material.name = renamed  # the body was renamed, or the plain name is free again
                    except Exception:
                        LOGGER.info("Could not rename the material copy %r", material.name)
            if copy_density(material) is None or abs(copy_density(material) - density) > 1e-9:
                _density_property(material).value = density * 1000.0
            note = write_values(material, item.values, source)
            if current is None or current != material:
                if current is not None:
                    uses[str(current.name)] = uses.get(str(current.name), 1) - 1
                body.material = material
                uses[str(material.name)] = uses.get(str(material.name), 0) + 1
            results.append(BodyResult(name=name, path=item.path, material=str(material.name),
                                      density_g_cm3=density, source=item.origin, note=note))
        except Exception as exc:  # noqa: BLE001 - one body's failure must not stop the others
            if not isinstance(exc, MaterialCopyError):
                LOGGER.exception("Could not apply the as-printed material to %r", name)
            results.append(BodyResult(name=name, path=item.path, error=str(exc) or type(exc).__name__))
    return results


def restore(design: Any, bodies: Sequence[Tuple[Any, str]]) -> List[BodyResult]:
    """Put each body with an as-printed copy back on the filament the copy came from."""
    results = []
    for body, path in bodies:
        material = body.material
        if not is_copy(material):
            continue
        name = str(body.name)
        try:
            source = solid_material(design, material)
            if source is None:
                raise MaterialCopyError("The filament {!r} is no longer in the design.".format(source_name(material)))
            body.material = source
            results.append(BodyResult(name=name, path=path, material=str(source.name)))
        except Exception as exc:  # noqa: BLE001
            if not isinstance(exc, MaterialCopyError):
                LOGGER.exception("Could not restore the filament of %r", name)
            results.append(BodyResult(name=name, path=path, error=str(exc) or type(exc).__name__))
    return results


def clean_up(design: Any) -> List[str]:
    """Delete every as-printed copy no body uses; returns their names.

    Only copies (by their description) that Fusion says are unused; Fusion
    refuses to delete a used material too. Never a filament material.
    """
    deleted = []
    if design is None:
        return deleted
    for material in _materials(design):
        try:
            if not is_copy(material) or material.isUsed:
                continue
            name = str(material.name)
            if material.deleteMe():
                deleted.append(name)
        except Exception:
            LOGGER.exception("Could not delete an unused as-printed copy")
    if deleted:
        LOGGER.info("Deleted %d unused as-printed cop%s", len(deleted), "y" if len(deleted) == 1 else "ies")
    return deleted


# ---------------------------------------------------------------------------
# Self-test with fake Fusion objects
# ---------------------------------------------------------------------------

_PASSED = 0
_FAILED = 0


def _check(condition: bool, label: str) -> None:
    """Record and print one PASS/FAIL check."""
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print("PASS {}".format(label))
    else:
        _FAILED += 1
        print("FAIL {}".format(label))


class _FakeProperty:
    def __init__(self, value: float, units: str = "", several: int = 0) -> None:
        self.value = value
        self.units = units
        self.hasMultipleValues = several > 0
        self.values = [value] * several


class _FakeMaterial:
    def __init__(self, name: str, density_kg_m3: float, description: str = "", fusion_id: str = "PrismMaterial-022",
                 design: Any = None) -> None:
        self.name = name
        self.id = fusion_id
        self.description = description
        self.properties = {DENSITY_PROPERTY_ID: _FakeProperty(density_kg_m3, "kg/m^3"),
                           "structural_Young_modulus": _FakeProperty(2.0e6, "Kilopascal"),
                           "structural_Poisson_ratio": _FakeProperty(0.35),
                           "structural_Shear_modulus": _FakeProperty(0.8e6, "Kilopascal"),
                           "structural_Minimum_yield_stress": _FakeProperty(4.0e4, "kPa"),
                           "structural_Minimum_tensile_strength": _FakeProperty(5.0e4, "kPa"),
                           BEHAVIOR_PROPERTY_ID: _FakeProperty("structural_transverse_isotropic")}
        self.materialProperties = type("Props", (), {"itemById": lambda _s, key: self.properties.get(key)})()
        self.design = design

    @property
    def isUsed(self) -> bool:
        return self.design is not None and any(body.material is self for body in self.design.bodies)

    def deleteMe(self) -> bool:
        if self.isUsed:
            return False  # Fusion refuses too
        self.design.materials.items.remove(self)
        return True


class _FakeMaterials:
    def __init__(self, design: Any) -> None:
        self.items: List[_FakeMaterial] = []
        self.design = design

    @property
    def count(self) -> int:
        return len(self.items)

    def item(self, index: int) -> _FakeMaterial:
        return self.items[index]

    def itemByName(self, name: str) -> Optional[_FakeMaterial]:
        return next((material for material in self.items if material.name == name), None)

    def addByCopy(self, source: _FakeMaterial, name: str) -> _FakeMaterial:
        if self.itemByName(name) is not None:
            raise RuntimeError("duplicate name")
        copy = _FakeMaterial(name, source.properties[DENSITY_PROPERTY_ID].value, source.description, source.id, self.design)
        self.items.append(copy)
        return copy


def _fake_design() -> Any:
    design = type("Design", (), {})()
    design.bodies = []
    design.materials = _FakeMaterials(design)
    pla = _FakeMaterial("Bambu Lab - PLA Basic - Black", 1240.0, "Filamatch | PLA | Bambu Lab - PLA Basic - Black",
                        design=design)
    odd = _FakeMaterial("Odd | Name", 1240.0, design=design)
    spare = _FakeMaterial("Filamatch spare filament", 1240.0, "Filamatch | PETG | spare", design=design)
    design.materials.items += [pla, odd, spare]
    for name, material in (("Bracket", pla), ("Clip", pla), ("Weird", odd)):
        design.bodies.append(type("Body", (), {"name": name, "material": material})())
    return design


def _item(body: Any, token: str, density: float, origin: str = "model", label: str = "L", key: str = "k1",
          volume: float = 10.0, body_label: str = "") -> ApplyItem:
    return ApplyItem(body=body, token=token, body_label=body_label or body.name, path="", density_g_cm3=density,
                     origin=origin, label=label, settings_key=key, volume_cm3=volume)


def _copies(design: Any) -> List[Any]:
    return [material for material in design.materials.items if is_copy(material)]


def _self_test() -> int:
    label = "0.4/0.2 · 2 walls · 5/3 layers · 15% gyroid"
    description = copy_description("Bambu Lab - PLA Basic - Black", label, "3f9a12c47e05")
    _check(description == "As printed | filamatch_source=Bambu Lab - PLA Basic - Black | " + label
           + " | as_printed_id=3f9a12c47e05", "the description is the format agreed with Filamatch")
    old = type("Old", (), {"description": "Printegrity | filamatch_source=PLA | label | printegrity_id=0123456789ab"})()
    _check(is_copy(old) and copy_id(old) == "0123456789ab" and source_name(old) == "PLA",
           "a copy made before the move into Filamatch is still read")
    current = type("New", (), {"description": copy_description("PLA", label, "3f9a12c47e05", "key-a", "slicer", 12.5)})()
    _check(is_current(current, "key-a", "other label") and not is_current(current, "key-b", label)
           and is_current(old, "any key", "label") and not is_current(old, "any key", label)
           and copy_label(current) == label and density_origin(current) == "slicer" and copy_volume(current) == 12.5
           and density_origin(old) == "" and copy_volume(old) is None,
           "a copy knows its settings, where its density came from and for which volume (older copies: label only)")
    filamatch_reads = re.compile(r"(?:^|\|)\s*filamatch_source=([^|]+?)\s*(?=\||$)").search(current.description)
    _check(filamatch_reads is not None and filamatch_reads.group(1) == "PLA",
           "Filamatch's own pattern reads the source name back")
    _check(new_copy_id("a", "t1") == new_copy_id("a", "t1") and new_copy_id("a", "t1") != new_copy_id("a", "t2")
           and new_copy_id("a", "t1") != new_copy_id("b", "t1"), "a new copy's id comes from its filament and body")

    design = _fake_design()
    bracket, clip, weird = design.bodies
    pla = design.materials.itemByName("Bambu Lab - PLA Basic - Black")
    results = apply(design, [_item(bracket, "t-bracket", 0.434, "slicer", label),
                             _item(clip, "t-clip", 0.52, body_label="Clip in Clips"), _item(weird, "t-weird", 0.5)],
                    design.bodies)
    copies = _copies(design)
    _check(len(copies) == 2 and bracket.material is not clip.material and all(m in copies for m in (bracket.material, clip.material)),
           "each body gets its own copy (the density depends on its shape)")
    _check(abs(bracket.material.properties[DENSITY_PROPERTY_ID].value - 434.0) < 1e-9
           and abs(clip.material.properties[DENSITY_PROPERTY_ID].value - 520.0) < 1e-9
           and pla.properties[DENSITY_PROPERTY_ID].value == 1240.0 and pla.description.startswith("Filamatch |"),
           "each copy holds its body's printed density; the Filamatch material is untouched")
    _check(bracket.material.name == "Bambu Lab - PLA Basic - Black · Bracket · as printed"
           and clip.material.name == "Bambu Lab - PLA Basic - Black · Clip in Clips · as printed"
           and source_name(bracket.material) == pla.name and bracket.material.id == pla.id,
           "a copy is named for its filament and its body, and names its source")
    _check(results[0].source == "slicer" and not results[0].error and density_origin(bracket.material) == "slicer"
           and copy_volume(bracket.material) == 10.0, "each body reports its copy; the copy says where its density came from")
    _check(weird.material.name == "Odd | Name" and "\"|\"" in results[2].error,
           "a source name with \"|\" is refused in plain words, and the body is left alone")
    _check(solid_material(design, bracket.material) is pla, "a copy leads back to its filament")

    first = bracket.material
    first_id = copy_id(first)
    apply(design, [_item(bracket, "t-bracket-new-token", 0.47, key="k2")], design.bodies)
    _check(bracket.material is first and len(_copies(design)) == 2 and copy_id(first) == first_id
           and first.name == "Bambu Lab - PLA Basic - Black · Bracket · as printed"
           and abs(first.properties[DENSITY_PROPERTY_ID].value - 470.0) < 1e-9
           and is_current(first, "k2", label) and not is_current(first, "k1", label),
           "new settings update the copy the body wears in place, with its plain name, even after Fusion "
           "gave the body a new token")
    apply(design, [_item(bracket, "t-bracket", 0.47, key="k2", body_label="Bracket v2")], design.bodies)
    _check(bracket.material is first and first.name == "Bambu Lab - PLA Basic - Black · Bracket v2 · as printed",
           "a renamed body renames its copy")
    refused = "not a believable" in apply(design, [_item(bracket, "t-bracket", 12.0)], design.bodies)[0].error
    _check(refused and abs(first.properties[DENSITY_PROPERTY_ID].value - 470.0) < 1e-9,
           "an unbelievable density is refused, and the copy keeps its density")

    uses = use_counts(design.bodies)
    _check(not needs_apply(first, pla.name, "k2", label, 0.47, "model", 10.0, uses)
           and needs_apply(first, pla.name, "k1", label, 0.47, "model", 10.0, uses)
           and needs_apply(first, pla.name, "k2", label, 0.50, "model", 10.0, uses)
           and needs_apply(first, "Another filament", "k2", label, 0.47, "model", 10.0, uses)
           and needs_apply(first, pla.name, "k2", label, 0.47, "model", 10.0, {first.name: 2})
           and needs_apply(pla, pla.name, "k2", label, 0.47, "model", 10.0, uses),
           "a body needs Apply when it wears no copy, a shared one, another filament's, or one for other "
           "settings or density")
    apply(design, [_item(bracket, "t-bracket", 0.40, "slicer", key="k2")], design.bodies)
    _check(not needs_apply(bracket.material, pla.name, "k2", label, 0.47, "model", 10.0, uses)
           and needs_apply(bracket.material, pla.name, "k2", label, 0.47, "model", 11.0, uses)
           and needs_apply(bracket.material, pla.name, "k2", label, 0.42, "slicer", 10.0, uses),
           "a slicer check's density stays while the shape is the same; a new slicer check or shape replaces it")

    shared = _FakeMaterial("As printed · old shared", 430.0, copy_description(pla.name, label, "0123456789ab", "k1"),
                           design=design)
    design.materials.items.append(shared)
    bracket.material = shared
    clip.material = shared
    apply(design, [_item(bracket, "t-bracket", 0.43, key="k1"),
                   _item(clip, "t-clip", 0.44, key="k1", body_label="Clip in Clips")], design.bodies)
    _check(bracket.material is not shared and clip.material is shared and shared.name.endswith("Clip in Clips · as printed")
           and abs(shared.properties[DENSITY_PROPERTY_ID].value - 440.0) < 1e-9
           and bracket.material.name == "Bambu Lab - PLA Basic - Black · Bracket · as printed",
           "bodies that shared an older copy end with one each: the last keeps it (renamed), the other gets "
           "a new one; unused copies that held their plain names are deleted first")
    _check(clean_up(design) == [] and len(_copies(design)) == 2
           and all(m.isUsed for m in _copies(design)) and pla in design.materials.items
           and design.materials.itemByName("Filamatch spare filament") is not None,
           "clean-up never deletes a filament (used or not) or a copy a body wears")

    spare_name = copy_name(pla.name, "Weird")
    holder = _FakeMaterial(spare_name, 400.0, copy_description(pla.name, label, "fedcba987654"), design=design)
    design.materials.items.append(holder)
    weird.material = pla
    apply(design, [_item(weird, "t-weird", 0.45)], design.bodies)
    _check(weird.material.name == spare_name and weird.material is not holder and holder not in design.materials.items,
           "an unused copy that holds a body's plain name is deleted first, so the new copy gets the plain name")

    restored = restore(design, [(bracket, ""), (clip, "Clips:1"), (weird, "")])
    _check(bracket.material is pla and clip.material is pla and weird.material is pla and len(restored) == 3
           and all(not item.error for item in restored),
           "Restore puts the filament back on every body with a copy")
    _check(len(clean_up(design)) == 3 and not _copies(design) and clean_up(design) == [] and clean_up(None) == [],
           "after Restore, clean-up deletes every copy; nothing is left to delete")
    values = {"e_mpa": 2500.0, "poisson": 0.36, "shear_mpa": 919.0, "yield_mpa": 26.0, "tensile_mpa": 30.0}
    item = dataclasses.replace(_item(bracket, "t-bracket", 0.45), values=values, values_key="PLA|safe|2500")
    result = apply(design, [item], design.bodies)[0]
    props = bracket.material.properties
    _check(not result.error and not result.note and props["structural_Young_modulus"].value == 2.5e6
           and props["structural_Poisson_ratio"].value == 0.36 and props["structural_Minimum_yield_stress"].value == 2.6e4
           and props["structural_Minimum_tensile_strength"].value == 3.0e4 and props["structural_Shear_modulus"].value == 919e3
           and pla.properties["structural_Young_modulus"].value == 2.0e6
           and props[BEHAVIOR_PROPERTY_ID].value == ISOTROPIC
           and pla.properties[BEHAVIOR_PROPERTY_ID].value == "structural_transverse_isotropic",
           "stiffness and strength go into the copy in Fusion's units (kPa here), and it becomes isotropic; "
           "the filament keeps its own")
    uses = use_counts(design.bodies)
    _check(not needs_apply(bracket.material, pla.name, "k1", "L", 0.45, "model", 10.0, uses, "PLA|safe|2500")
           and needs_apply(bracket.material, pla.name, "k1", "L", 0.45, "model", 10.0, uses, "PLA|typical|2700")
           and needs_apply(bracket.material, pla.name, "k1", "L", 0.45, "model", 10.0, uses, ""),
           "a copy needs Apply when its stiffness and strength change, or go away")
    apply(design, [_item(bracket, "t-bracket", 0.45)], design.bodies)
    _check(props["structural_Young_modulus"].value == 2.0e6 and props["structural_Minimum_tensile_strength"].value == 5.0e4
           and props[BEHAVIOR_PROPERTY_ID].value == "structural_transverse_isotropic"
           and not needs_apply(bracket.material, pla.name, "k1", "L", 0.45, "model", 10.0, uses, ""),
           "without values the copy gets its filament's own stiffness and strength back")
    props_holder = bracket.material.materialProperties
    props["structural_Young_modulus"].units = "furlongs"
    props["structural_Shear_modulus"] = _FakeProperty(0.8e6, "MPa", several=3)
    result = apply(design, [item], design.bodies)[0]
    _check(stress_factor("Kilopascal") == 1e3 and stress_factor("kPa") == 1e3 and stress_factor("Mega pascal") == 1.0
           and stress_factor("psi") > 145 and stress_factor("furlongs") is None,
           "Fusion's unit names (\"Kilopascal\") and short ones convert")
    older = copy_description(pla.name, "L", "0123456789ab", "k1", "model", 10.0)
    older += " | values_id={}".format(settings_fingerprint("PLA|safe|2500"))
    _check(needs_apply(type("Old", (), {"name": "x", "description": older, "materialProperties": props_holder})(),
                       pla.name, "k1", "L", 0.45, "model", 10.0, {}, "PLA|safe|2500"),
           "a copy written by an older writer (values_id without the writer) is written again")
    _check("young modulus" in result.note and "furlongs" in result.note and not result.error
           and props["structural_Young_modulus"].value == 2.0e6 and props["structural_Shear_modulus"].values == [919.0] * 3,
           "an unknown unit is left out and said; a direction-dependent value is set in every direction")
    _check(read_values(pla) == {"e_mpa": 2000.0, "poisson": 0.35, "shear_mpa": 800.0, "yield_mpa": 40.0,
                                "tensile_mpa": 50.0} and read_values(object()) is None,
           "a material's own stiffness and strength are read in MPa (none from an object without them)")
    restore(design, [(bracket, "")])
    clean_up(design)

    lost = _FakeMaterial("As printed · lost", 400.0, copy_description("Gone filament", label, "0123456789ab"), design=design)
    design.materials.items.append(lost)
    body = type("Body", (), {"name": "Lost", "material": lost})()
    design.bodies.append(body)
    _check("no longer in the design" in restore(design, [(body, "")])[0].error and body.material is lost
           and clean_up(design) == [] and lost in design.materials.items,
           "a copy whose filament is gone is reported and kept while a body uses it")

    print()
    if _FAILED:
        print("MATERIAL COPY SELF-TEST FAILED ({} passed, {} failed)".format(_PASSED, _FAILED))
        return 1
    print("MATERIAL COPY SELF-TEST PASSED ({} checks)".format(_PASSED))
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
