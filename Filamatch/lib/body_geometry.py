# Moved from Printegrity 05a4847 Printegrity/lib/body_geometry.py (now part of Filamatch's As printed tab)
"""Read what the mass model needs from Fusion bodies.

For every visible solid body in the active design (including bodies inside
components): its volume, a slope histogram of its surface (from Fusion's
triangle mesh, in the design's own coordinates) and the solid density of
its filament. For the slicer check, a finer mesh of each filament body
(:func:`slicing_meshes`).

A body that wears an as-printed copy (:mod:`.material_copy`) holds the
printed density; its filament's solid density comes from the material the
copy names as its source.

Per-body print settings: infill, walls and top/bottom layers are Filamatch's
(its attribute, read by :mod:`.body_settings`). The estimate uses the
design's own orientation (Z up), as the slicer check does; build directions
saved by earlier versions are ignored. A component placed twice is one body
with two copies (:func:`design_bodies`).

Fusion API calls only: run this on Fusion's UI thread, never in a
background job. Nothing in the design is changed.

Self-test with fake Fusion objects: ``python3 -m Filamatch.lib.body_geometry``.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import config
from . import body_settings
from . import mass_model
from . import material_copy
from . import slicer_check

LOGGER = logging.getLogger(config.ADDIN_NAME + ".body_geometry")

try:  # pragma: no cover - the adsk modules only exist inside Fusion.
    import adsk.core
    import adsk.fusion
except ImportError:  # pragma: no cover - exercised by the standalone self-test.
    adsk = None  # type: ignore[assignment]

#: Fusion's material property id for density (kg/m³).
DENSITY_PROPERTY_ID = "structural_Density"

#: Solid densities (g/cm³) that look like a 3D-printing filament.
FILAMENT_DENSITY_RANGE = (0.8, 2.5)

#: More bodies than this are not measured (the palette says so).
MAX_BODIES = 200

@dataclasses.dataclass
class DesignBody:
    """One body of the design, however many times its component is placed."""

    #: The first placed copy (a proxy for a body inside a component).
    body: Any
    #: The native body's entity token: it finds the body again.
    token: str
    copies: int
    #: The print settings shared with Filamatch (body_settings keys); {} uses the defaults.
    own: Dict[str, Any]

    def settings(self, defaults: mass_model.PrintSettings) -> mass_model.PrintSettings:
        """The settings this body is printed with: the defaults with its own on top."""
        return defaults.with_body(self.own)


@dataclasses.dataclass(frozen=True)
class BodyGeometry:
    """What the mass model needs from one body."""

    name: str
    path: str
    #: The material the body wears now (an as-printed copy after Apply).
    material: str
    volume_cm3: float
    histogram: Tuple[Tuple[float, float], ...]
    #: The filament's solid density (from a copy's source).
    density_g_cm3: Optional[float]
    #: The filament: the material itself, or the source of an as-printed copy.
    filament: str = ""
    #: True when the body wears an as-printed copy.
    applied: bool = False
    #: The copy the body wears (None for a plain filament); is it for today's settings?
    copy: Any = None
    #: Fusion's mass of the body now (g).
    fusion_grams: Optional[float] = None

    @property
    def looks_like_filament(self) -> bool:
        low, high = FILAMENT_DENSITY_RANGE
        return self.density_g_cm3 is not None and low <= self.density_g_cm3 <= high


def visible_solid_bodies(design: Any, include_hidden: bool = False) -> List[Any]:
    """Visible solid bodies of the design: the root's, then each component occurrence's."""
    root = design.rootComponent
    found = [body for body in _items(root.bRepBodies)]
    for occurrence in _items(root.allOccurrences):
        found.extend(_items(occurrence.bRepBodies))
    return [body for body in found if _flag(body, "isSolid") and (include_hidden or _flag(body, "isVisible"))]


def all_bodies(design: Any) -> List[Any]:
    """Every body of the design once (native bodies; hidden and surface bodies too): who wears which material."""
    if design is None:
        return []
    root = design.rootComponent
    found = list(_items(root.bRepBodies))
    for occurrence in _items(root.allOccurrences):
        found.extend(_items(occurrence.bRepBodies))
    seen = set()
    bodies = []
    for body in found:
        token = body_token(body) or "object:{}".format(id(native(body)))
        if token not in seen:
            seen.add(token)
            bodies.append(native(body))
    return bodies


def native(body: Any) -> Any:
    """The body itself for a proxy inside a placed component."""
    try:
        return body.nativeObject or body
    except Exception:
        return body


def component_name(body: Any) -> str:
    """The component of a body inside a placed component ("" for a root body), as Filamatch's body list."""
    try:
        if getattr(body, "nativeObject", None) is None:
            return ""
        return str(getattr(native(body).parentComponent, "name", "") or "")
    except Exception:
        return ""


def body_label(body: Any) -> str:
    """How the palette names a body: "Body1", or "Body1 in Lid" inside a placed component."""
    name = str(getattr(native(body), "name", "") or "Body")
    component = component_name(body)
    return "{} in {}".format(name, component) if component else name


def body_token(body: Any) -> str:
    """The native body's entity token ("" when Fusion gives none)."""
    try:
        return str(native(body).entityToken or "")
    except Exception:
        return ""


def design_bodies(design: Any, include_hidden: bool = False) -> Tuple[List[DesignBody], int]:
    """The design's solid bodies, one per native body, with their own settings.

    Returns:
        ``(bodies, skipped)``: at most :data:`MAX_BODIES` bodies, and how many
        more were left out.
    """
    if design is None:
        return [], 0
    rows: List[DesignBody] = []
    by_token: Dict[str, DesignBody] = {}
    skipped = 0
    for body in visible_solid_bodies(design, include_hidden):
        token = body_token(body) or "object:{}".format(id(native(body)))
        if token in by_token:
            by_token[token].copies += 1
            continue
        if len(rows) >= MAX_BODIES:
            skipped += 1
            by_token[token] = DesignBody(body, token, 1, {})
            continue
        entry = DesignBody(body=body, token=token, copies=1, own=body_settings.read_body(body))
        by_token[token] = entry
        rows.append(entry)
    return rows, skipped


def material_density(material: Any) -> Optional[float]:
    """The material's density in g/cm³, or None when Fusion does not give one."""
    try:
        prop = material.materialProperties.itemById(DENSITY_PROPERTY_ID)
        value = float(prop.value)
    except Exception:
        return None
    return value / 1000.0 if value > 0 else None


def body_path(body: Any) -> str:
    """Where the body lives: the occurrence path, or "" for root bodies."""
    try:
        context = body.assemblyContext
        return str(context.fullPathName) if context is not None else ""
    except Exception:
        return ""


def _mesh(body: Any, high_quality: bool) -> Any:
    """Fusion's triangle mesh of a body (cm, design coordinates)."""
    calculator = body.meshManager.createMeshCalculator()
    if adsk is not None:
        quality = adsk.fusion.TriangleMeshQualityOptions
        calculator.setQuality(quality.HighQualityTriangleMesh if high_quality else quality.LowQualityTriangleMesh)
    return calculator.calculate()


def measure(body: Any, build_direction: str, design: Any = None) -> BodyGeometry:
    """Measure one body for the mass model."""
    mesh = _mesh(body, high_quality=False)
    histogram = mass_model.slope_histogram(
        list(mesh.nodeCoordinatesAsDouble), list(mesh.nodeIndices), build_direction
    )
    material = body.material
    filament = material_copy.solid_material(design, material) if design is not None else material
    properties = body.physicalProperties
    try:
        fusion_grams: Optional[float] = float(properties.mass) * 1000.0
    except Exception:
        fusion_grams = None
    return BodyGeometry(
        name=str(body.name),
        path=body_path(body),
        material=str(getattr(material, "name", "") or ""),
        volume_cm3=float(properties.volume),
        histogram=tuple(histogram),
        density_g_cm3=material_density(filament) if filament is not None else None,
        filament=str(getattr(filament, "name", "") or ""),
        applied=material_copy.is_copy(material),
        fusion_grams=fusion_grams,
        copy=material if material_copy.is_copy(material) else None,
    )


def slicing_meshes(design: Any, defaults: mass_model.PrintSettings
                   ) -> Tuple[List[Tuple[slicer_check.BodyMesh, mass_model.PrintSettings]], List[str]]:
    """Fine meshes of the design's filament bodies and their settings, ready for the slicer check.

    Returns:
        ``(items, left_out)``: at most :data:`slicer_check.MAX_BODIES` bodies
        whose material looks like a filament, each with the settings it is
        printed with, and the names of the bodies left out (not a filament,
        or past the limit).
    """
    if design is None:
        return [], []
    meshes: List[Tuple[slicer_check.BodyMesh, mass_model.PrintSettings]] = []
    left_out: List[str] = []
    entries, skipped = design_bodies(design)
    if skipped:
        left_out.append("{} more bodies".format(skipped))
    for entry in entries:
        body = entry.body
        material = material_copy.solid_material(design, body.material)
        density = material_density(material) if material is not None else None
        low, high = FILAMENT_DENSITY_RANGE
        if density is None or not low <= density <= high or len(meshes) >= slicer_check.MAX_BODIES:
            left_out.append(str(body.name))
            continue
        try:
            mesh = _mesh(body, high_quality=True)
            meshes.append((slicer_check.BodyMesh(
                name=str(body.name),
                path=body_path(body),
                coordinates=tuple(mesh.nodeCoordinatesAsDouble),
                indices=tuple(mesh.nodeIndices),
                volume_cm3=float(body.physicalProperties.volume),
                density_g_cm3=density,
                material=str(getattr(material, "name", "") or ""),
                token=entry.token,
            ), entry.settings(defaults)))
        except Exception:
            LOGGER.exception("Could not mesh a body for the slicer")
            left_out.append(str(body.name))
    return meshes, left_out


def _items(collection: Any) -> List[Any]:
    """The items of a Fusion collection (count + item(i)), or of a plain list."""
    if collection is None:
        return []
    if isinstance(collection, (list, tuple)):
        return list(collection)
    return [collection.item(index) for index in range(int(collection.count))]


def _flag(entity: Any, name: str) -> bool:
    try:
        return bool(getattr(entity, name))
    except Exception:
        return False


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


class _FakeCollection:
    def __init__(self, items: Sequence[Any]) -> None:
        self._items = list(items)
        self.count = len(self._items)

    def item(self, index: int) -> Any:
        return self._items[index]


class _FakeMaterial:
    def __init__(self, name: str, density_kg_m3: Optional[float]) -> None:
        self.name = name
        props = {} if density_kg_m3 is None else {DENSITY_PROPERTY_ID: type("P", (), {"value": density_kg_m3})()}
        self.materialProperties = type("Props", (), {"itemById": staticmethod(props.get)})()


class _FakeAttribute:
    def __init__(self, owner: "_FakeAttributes", key: Tuple[str, str], value: str) -> None:
        self.owner, self.key, self.value = owner, key, value

    def deleteMe(self) -> bool:
        self.owner.items.pop(self.key, None)
        return True


class _FakeAttributes:
    def __init__(self) -> None:
        self.items: Dict[Tuple[str, str], _FakeAttribute] = {}

    def itemByName(self, group: str, name: str) -> Any:
        return self.items.get((group, name))

    def add(self, group: str, name: str, value: str) -> _FakeAttribute:
        self.items[(group, name)] = _FakeAttribute(self, (group, name), value)
        return self.items[(group, name)]


class _FakeProxy:
    """A placed copy of a body inside a component."""

    def __init__(self, native_body: Any, path: str) -> None:
        self.nativeObject = native_body
        self.assemblyContext = type("Occ", (), {"fullPathName": path})()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.nativeObject, name)


class _FakeBody:
    _count = 0

    def __init__(self, name: str, size: Tuple[float, float, float], material: Any,
                 visible: bool = True, solid: bool = True, context: Any = None) -> None:
        _FakeBody._count += 1
        self.entityToken = "token-{}-{}".format(name, _FakeBody._count)
        self.nativeObject = None
        self.attributes = _FakeAttributes()
        coordinates, indices = mass_model._box_mesh(*size)
        mesh = type("Mesh", (), {"nodeCoordinatesAsDouble": coordinates, "nodeIndices": indices})()
        calculator = type("Calc", (), {"calculate": lambda self: mesh, "setQuality": lambda self, q: None})()
        self.meshManager = type("MM", (), {"createMeshCalculator": lambda self: calculator})()
        self.name = name
        self.material = material
        self.isVisible = visible
        self.isSolid = solid
        self.assemblyContext = context
        volume = size[0] * size[1] * size[2]
        self.physicalProperties = type("PP", (), {"volume": volume, "mass": property(
            lambda _self: volume * (material_density(self.material) or 0.0) / 1000.0)})()


def _fake_design() -> Any:
    pla = _FakeMaterial("Filamatch bambu_pla_basic_black", 1240.0)
    steel = _FakeMaterial("Steel", 7850.0)
    root_bodies = [
        _FakeBody("Cube", (2.0, 2.0, 2.0), pla),
        _FakeBody("Hidden", (1.0, 1.0, 1.0), pla, visible=False),
        _FakeBody("Surface", (1.0, 1.0, 1.0), pla, solid=False),
    ]
    plate = _FakeBody("Plate", (4.0, 4.0, 0.5), steel)
    plate.parentComponent = type("Component", (), {"name": "Bracket"})()
    occurrences = [type("O", (), {"bRepBodies": _FakeCollection([_FakeProxy(plate, "Bracket:{}".format(n))])})()
                   for n in (1, 2)]
    root = type("Root", (), {"bRepBodies": _FakeCollection(root_bodies),
                             "allOccurrences": _FakeCollection(occurrences)})()
    materials = _FakeCollection([pla, steel])
    materials.itemByName = lambda name: next((m for m in (pla, steel) if m.name == name), None)
    everything = root_bodies + [plate]
    find = staticmethod(lambda token: [body for body in everything if body.entityToken == token])
    return type("Design", (), {"rootComponent": root, "materials": materials, "findEntityByToken": find})()


def _self_test() -> int:
    global MAX_BODIES
    design = _fake_design()
    names = [body.name for body in visible_solid_bodies(design)]
    _check(names == ["Cube", "Plate", "Plate"], "visible solid bodies, root first, every placed copy: {}".format(names))

    entries, skipped = design_bodies(design)
    cube_entry, plate_entry = entries
    _check(skipped == 0 and [entry.copies for entry in entries] == [1, 2]
           and body_path(plate_entry.body) == "Bracket:1" and cube_entry.own == {},
           "a component placed twice is one body with two copies; no own settings yet")
    cube = measure(cube_entry.body, "z_up", design)
    plate = measure(plate_entry.body, "z_up", design)
    _check(cube.volume_cm3 == 8.0 and cube.path == "" and plate.path == "Bracket:1",
           "volume and occurrence path are read")
    _check(component_name(plate_entry.body) == "Bracket" and component_name(cube_entry.body) == ""
           and component_name(object()) == "" and body_label(plate_entry.body) == "Plate in Bracket"
           and body_label(cube_entry.body) == "Cube", "a body in a component names it; a root body has none")
    _check(abs(mass_model.total_area(cube.histogram) - 24.0) < 1e-9, "the mesh gives the slope histogram")
    _check(cube.density_g_cm3 == 1.24 and cube.looks_like_filament, "a filament's density is read in g/cm³")
    _check(plate.density_g_cm3 == 7.85 and not plate.looks_like_filament, "steel does not look like a filament")
    _check(material_density(_FakeMaterial("Odd", None)) is None, "a material without density gives None")
    _check(design_bodies(None) == ([], 0), "no design, no bodies")
    _check([body.name for body in all_bodies(design)] == ["Cube", "Hidden", "Surface", "Plate"] and all_bodies(None) == [],
           "every body once, hidden and surface bodies too (for who wears which material)")
    _check(cube.fusion_grams == 8.0 * 1.24 and not cube.applied and cube.filament == cube.material,
           "Fusion's mass is read; a plain filament is its own filament")

    body_settings.write_body(plate_entry.body, {"infill": 40, "walls": 3})
    native(plate_entry.body).attributes.add("Filamatch", "as_printed", '{"build_direction": "x_up"}')
    second_copy = visible_solid_bodies(design)[2]
    _check(body_settings.read_body(second_copy) == {"infill": 40, "walls": 3},
           "settings written through one placed copy are on the body itself")
    plate_entry = design_bodies(design)[0][1]
    _check(plate_entry.settings(mass_model.PrintSettings())
           == mass_model.PrintSettings(infill_percent=40.0, wall_count=3),
           "a body is printed with the defaults and its own settings on top; an old build direction is ignored")

    body_settings.write_body(cube_entry.body, {"infill": 25})
    items, left_out = slicing_meshes(design, mass_model.PrintSettings(wall_count=5))
    _check([mesh.name for mesh, _settings in items] == ["Cube"] and left_out == ["Plate"]
           and items[0][0].token == cube_entry.token and items[0][0].volume_cm3 == 8.0
           and items[0][0].density_g_cm3 == 1.24 and len(items[0][0].indices) == 36
           and items[0][1] == mass_model.PrintSettings(wall_count=5, infill_percent=25.0),
           "the slicer gets filament bodies only, each with its own settings; the steel body is left out by name")
    _check(slicing_meshes(None, mass_model.PrintSettings()) == ([], []), "no design, nothing to slice")

    applied = _FakeMaterial("As printed · PLA · 0.434", 434.0)
    applied.description = material_copy.copy_description("Filamatch bambu_pla_basic_black", "label", "0123456789ab")
    copy_body = _FakeBody("Copy", (2.0, 2.0, 2.0), applied)
    geometry = measure(copy_body, "z_up", design)
    _check(geometry.applied and geometry.density_g_cm3 == 1.24 and geometry.looks_like_filament
           and geometry.filament == "Filamatch bambu_pla_basic_black" and abs(geometry.fusion_grams - 8.0 * 0.434) < 1e-9,
           "a body with an as-printed copy keeps its filament's solid density; Fusion's mass is the printed one")

    saved = MAX_BODIES
    MAX_BODIES = 1
    try:
        entries, skipped = design_bodies(design)
    finally:
        MAX_BODIES = saved
    _check(len(entries) == 1 and skipped == 1, "bodies past the limit are counted, not measured")

    print()
    if _FAILED:
        print("BODY GEOMETRY SELF-TEST FAILED ({} passed, {} failed)".format(_PASSED, _FAILED))
        return 1
    print("BODY GEOMETRY SELF-TEST PASSED ({} checks)".format(_PASSED))
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
