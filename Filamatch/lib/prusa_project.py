"""Write a 3MF that PrusaSlicer opens with every body on its own extruder.

PrusaSlicer does not read the Bambu Studio / OrcaSlicer
``Metadata/model_settings.config`` that holds Filamatch's slot assignments, so
it opens a plain export with every body on extruder 1.  It reads its own
layout instead:

* one mesh per object in ``3D/3dmodel.model`` (parts are triangle ranges of
  that mesh), with ``<metadata name="slic3rpe:Version3mf">1</metadata>``;
* ``Metadata/Slic3r_PE_model.config`` with, per object, a ``<volume
  firstid=".." lastid="..">`` per part and its 1-based ``extruder``.

:func:`to_prusa_layout` rewrites a Filamatch export that way.  Bodies that
touch become the parts of one object (as ``--assemble`` does for OrcaSlicer),
so they keep their places; separate bodies stay separate objects.  The file
gets no ``Metadata/Slic3r_PE.config``: PrusaSlicer resets every key missing
from a partial config, so the user's own printer and filament settings are
kept by leaving it out.  The Bambu/OrcaSlicer settings are removed, because
their object numbers no longer match.

Only the Python standard library is used; ``python3 -m Filamatch.lib.prusa_project``
runs the self-test.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import traceback
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import slicer_profiles

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

MODEL_PART = "3D/3dmodel.model"
PRUSA_MODEL_CONFIG = "Metadata/Slic3r_PE_model.config"
#: Bambu Studio / OrcaSlicer entries left out of the PrusaSlicer file.
BAMBU_PARTS = ("Metadata/model_settings.config", "Metadata/project_settings.config")
CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PRODUCTION_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
SLIC3R_NS = "http://schemas.slic3r.org/3mf/2017/06"
#: Millimetres per model unit (3MF core specification).
UNIT_MM = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}

Matrix = Tuple[float, ...]
Point = Tuple[float, float, float]


class PrusaProjectError(Exception):
    """Raised when a 3MF cannot be written in the PrusaSlicer layout."""


class _Part:
    """One body: its name, world vertices (mm) and triangles."""

    def __init__(self, name: str, vertices: List[Point], triangles: List[Tuple[int, int, int]], extruder: int):
        self.name = name
        self.vertices = vertices
        self.triangles = triangles
        self.extruder = extruder


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def is_prusa_family(path: Any) -> bool:
    """True for PrusaSlicer and SuperSlicer (they read ``Slic3r_PE_model.config``)."""
    name = str(path or "").replace("\\", "/").rstrip("/").lower()
    name = name.split(".app/")[0].rsplit("/", 1)[-1]
    return "prusaslicer" in name or "superslicer" in name


def _object_extruders(archive: zipfile.ZipFile) -> Dict[Tuple[str, str], int]:
    """1-based extruders from the Bambu/Orca settings.

    Keys are ``(build-item object id, "")`` for an object and ``(build-item
    object id, component object id)`` for one of its parts (Bambu Studio and
    OrcaSlicer projects give each ``<part>`` its own extruder).
    """
    if "Metadata/model_settings.config" not in archive.namelist():
        return {}
    root = ET.fromstring(archive.read("Metadata/model_settings.config"))
    extruders: Dict[Tuple[str, str], int] = {}

    def read(element: Any, key: Tuple[str, str]) -> None:
        for meta in element.findall("metadata"):
            if meta.get("key") == "extruder":
                try:
                    extruders[key] = max(1, int(str(meta.get("value"))))
                except ValueError:
                    pass

    for element in root.findall("object"):
        object_id = str(element.get("id"))
        read(element, (object_id, ""))
        for part in element.findall("part"):
            read(part, (object_id, str(part.get("id"))))
    return extruders


def _read_parts(archive: zipfile.ZipFile) -> List[Tuple[str, List[_Part]]]:
    """Return ``(item object id, parts)`` per build item, vertices in world millimetres."""
    roots: Dict[str, Any] = {}

    def model(path: str) -> Any:
        if path not in roots:
            roots[path] = ET.fromstring(archive.read(path))
        return roots[path]

    main = model(MODEL_PART)
    scale = UNIT_MM.get(str(main.get("unit") or "millimeter").lower(), 1.0)
    extruders = _object_extruders(archive)

    def find_object(path: str, object_id: str) -> Any:
        for element in model(path).iter():
            if _local(element.tag) == "object" and element.get("id") == object_id:
                return element
        raise PrusaProjectError("Object {} is missing from {}".format(object_id, path))

    def collect(
        path: str, object_id: str, chain: List[Matrix], extruder: int, depth: int = 0, item_id: str = ""
    ) -> List[_Part]:
        if depth > 8:
            raise PrusaProjectError("Components are nested too deep (object {})".format(object_id))
        element = find_object(path, object_id)
        name = str(element.get("name") or "Object {}".format(object_id))
        parts: List[_Part] = []
        for child in element:
            kind = _local(child.tag)
            if kind == "mesh":
                vertices: List[Point] = []
                triangles: List[Tuple[int, int, int]] = []
                for node in child.iter():
                    tag = _local(node.tag)
                    if tag == "vertex":
                        point: Point = (float(node.get("x")), float(node.get("y")), float(node.get("z")))
                        for matrix in chain:
                            point = slicer_profiles._transform_point(matrix, point)
                        vertices.append((point[0] * scale, point[1] * scale, point[2] * scale))
                    elif tag == "triangle":
                        triangles.append((int(node.get("v1")), int(node.get("v2")), int(node.get("v3"))))
                if triangles:
                    parts.append(_Part(name, vertices, triangles, extruder))
            elif kind == "components":
                for component in child:
                    if _local(component.tag) != "component":
                        continue
                    target = str(component.get("{%s}path" % PRODUCTION_NS) or "").lstrip("/") or path
                    matrix = slicer_profiles._parse_transform(component.get("transform"))
                    child_id = str(component.get("objectid"))
                    child_extruder = extruders.get((item_id, child_id), extruder) if depth == 0 else extruder
                    parts.extend(collect(target, child_id, [matrix] + chain, child_extruder, depth + 1))
        return parts

    items: List[Tuple[str, List[_Part]]] = []
    for element in main.iter():
        if _local(element.tag) != "item":
            continue
        object_id = str(element.get("objectid"))
        target = str(element.get("{%s}path" % PRODUCTION_NS) or "").lstrip("/") or MODEL_PART
        matrix = slicer_profiles._parse_transform(element.get("transform"))
        parts = collect(target, object_id, [matrix], extruders.get((object_id, ""), 1), item_id=object_id)
        if parts:
            items.append((object_id, parts))
    if not items:
        raise PrusaProjectError("The 3MF has no meshes to convert")
    return items


def _number(value: float) -> str:
    return "{:.6f}".format(value).rstrip("0").rstrip(".") or "0"


def _write_model(objects: Sequence[Tuple[str, List[_Part]]]) -> Tuple[bytes, bytes]:
    """Return ``(3dmodel.model, Slic3r_PE_model.config)`` for the objects."""
    model = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<model unit="millimeter" xml:lang="en-US" xmlns="{}" xmlns:slic3rpe="{}">\n'.format(CORE_NS, SLIC3R_NS),
        ' <metadata name="slic3rpe:Version3mf">1</metadata>\n',
        ' <resources>\n',
    ]
    config = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>\n']
    build = []
    for number, (name, parts) in enumerate(objects, start=1):
        points = [point for part in parts for point in part.vertices]
        xs, ys, zs = zip(*points)
        # Object origin at the bottom centre; the build item puts it back in place.
        origin = ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0, min(zs))
        model.append('  <object id="{}" type="model">\n   <mesh>\n    <vertices>\n'.format(number))
        for part in parts:
            model.extend(
                '     <vertex x="{}" y="{}" z="{}"/>\n'.format(
                    _number(x - origin[0]), _number(y - origin[1]), _number(z - origin[2])
                )
                for x, y, z in part.vertices
            )
        model.append('    </vertices>\n    <triangles>\n')
        config.append(' <object id="{}" instances_count="1">\n'.format(number))
        config.append('  <metadata type="object" key="name" value="{}"/>\n'.format(_escape(name)))
        config.append('  <metadata type="object" key="extruder" value="{}"/>\n'.format(parts[0].extruder))
        vertex_offset = triangle_offset = 0
        for part in parts:
            model.extend(
                '     <triangle v1="{}" v2="{}" v3="{}"/>\n'.format(a + vertex_offset, b + vertex_offset, c + vertex_offset)
                for a, b, c in part.triangles
            )
            config.append('  <volume firstid="{}" lastid="{}">\n'.format(
                triangle_offset, triangle_offset + len(part.triangles) - 1))
            config.append('   <metadata type="volume" key="name" value="{}"/>\n'.format(_escape(part.name)))
            config.append('   <metadata type="volume" key="volume_type" value="ModelPart"/>\n')
            config.append('   <metadata type="volume" key="extruder" value="{}"/>\n'.format(part.extruder))
            config.append('  </volume>\n')
            vertex_offset += len(part.vertices)
            triangle_offset += len(part.triangles)
        model.append('    </triangles>\n   </mesh>\n  </object>\n')
        config.append(' </object>\n')
        build.append('  <item objectid="{}" transform="1 0 0 0 1 0 0 0 1 {} {} {}" printable="1"/>\n'.format(
            number, _number(origin[0]), _number(origin[1]), _number(origin[2])))
    model.append(' </resources>\n <build>\n')
    model.extend(build)
    model.append(' </build>\n</model>\n')
    config.append('</config>\n')
    return "".join(model).encode("utf-8"), "".join(config).encode("utf-8")


def _escape(text: str) -> str:
    return (
        str(text).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    )


def to_prusa_layout(project_3mf: Path, output: Optional[Path] = None) -> Dict[str, Any]:
    """Rewrite a Filamatch 3MF in the PrusaSlicer layout (in place without ``output``).

    Returns ``{"objects": int, "parts": int, "assembled": bool}``.  Other
    archive entries (thumbnail, Filamatch's slot record) are kept.

    Raises:
        PrusaProjectError: If the model cannot be read; ``project_3mf`` is
            then left unchanged.
    """
    project_3mf = Path(project_3mf)
    output = Path(output) if output is not None else project_3mf
    try:
        with zipfile.ZipFile(str(project_3mf), "r") as archive:
            items = _read_parts(archive)
            assembled = len(items) > 1 and "--assemble" in slicer_profiles._layout_flags(project_3mf)
            if assembled:
                objects = [(items[0][1][0].name, [part for _id, parts in items for part in parts])]
            else:
                objects = [(parts[0].name, parts) for _id, parts in items]
            model, config = _write_model(objects)
            skipped = set(BAMBU_PARTS) | {MODEL_PART, PRUSA_MODEL_CONFIG, "3D/_rels/3dmodel.model.rels"}
            descriptor, temporary = tempfile.mkstemp(prefix="filamatch_prusa_", suffix=".3mf", dir=str(output.parent))
            os.close(descriptor)
            try:
                with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as target:
                    for info in archive.infolist():
                        # Merged meshes from 3D/Objects/*.model are now in the main model.
                        if info.filename in skipped or info.filename.startswith("3D/Objects/"):
                            continue
                        target.writestr(info, archive.read(info.filename))
                    target.writestr(MODEL_PART, model)
                    target.writestr(PRUSA_MODEL_CONFIG, config)
            except Exception:
                os.unlink(temporary)
                raise
    except (KeyError, ValueError, TypeError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise PrusaProjectError("Could not convert {} for PrusaSlicer: {}".format(project_3mf.name, exc)) from exc
    os.replace(temporary, str(output))
    result = {"objects": len(objects), "parts": sum(len(parts) for _name, parts in objects), "assembled": assembled}
    logger.info("Wrote the PrusaSlicer layout to %s: %s", output, result)
    return result


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _cube(size: float) -> str:
    points = [(x, y, z) for z in (0, size) for y in (0, size) for x in (0, size)]
    faces = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
             (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    return "<mesh><vertices>{}</vertices><triangles>{}</triangles></mesh>".format(
        "".join('<vertex x="{}" y="{}" z="{}"/>'.format(*point) for point in points),
        "".join('<triangle v1="{}" v2="{}" v3="{}"/>'.format(*face) for face in faces),
    )


def _write_test_3mf(path: Path, objects: str, items: str, settings: str, unit: str = "millimeter") -> None:
    with zipfile.ZipFile(str(path), "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("Metadata/thumbnail.png", b"png")
        archive.writestr("Metadata/filamatch_slots.json", "{}")
        archive.writestr(MODEL_PART, (
            '<?xml version="1.0"?><model unit="{}" xmlns="{}" xmlns:p="{}"><resources>{}</resources>'
            '<build>{}</build></model>'
        ).format(unit, CORE_NS, PRODUCTION_NS, objects, items))
        archive.writestr("Metadata/model_settings.config", "<config>{}</config>".format(settings))
        archive.writestr("Metadata/project_settings.config", "{}")


def _settings(*extruders: Tuple[str, int]) -> str:
    return "".join(
        '<object id="{}"><metadata key="extruder" value="{}"/></object>'.format(object_id, extruder)
        for object_id, extruder in extruders
    )


def _check_separate_bodies(root: Path) -> None:
    path = root / "separate.3mf"
    _write_test_3mf(
        path,
        '<object id="1" name="Left" type="model">{0}</object><object id="2" name="Right" type="model">{0}</object>'.format(_cube(10)),
        '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/><item objectid="2" transform="1 0 0 0 1 0 0 0 1 50 0 0"/>',
        _settings(("1", 1), ("2", 3)),
    )
    result = to_prusa_layout(path)
    _expect(result == {"objects": 2, "parts": 2, "assembled": False}, "two separate bodies: {}".format(result))
    with zipfile.ZipFile(str(path)) as archive:
        names = archive.namelist()
        config = ET.fromstring(archive.read(PRUSA_MODEL_CONFIG))
        model = ET.fromstring(archive.read(MODEL_PART))
    _expect(not any(name in names for name in BAMBU_PARTS), "the Bambu settings are removed: {}".format(names))
    _expect("Metadata/thumbnail.png" in names and "Metadata/filamatch_slots.json" in names, "other entries are kept")
    extruders = [
        meta.get("value") for meta in config.iter("metadata") if meta.get("type") == "volume" and meta.get("key") == "extruder"
    ]
    _expect(extruders == ["1", "3"], "each body keeps its extruder: {}".format(extruders))
    version = [meta.text for meta in model.iter("{%s}metadata" % CORE_NS) if meta.get("name") == "slic3rpe:Version3mf"]
    _expect(version == ["1"], "PrusaSlicer's layout version is written")
    transforms = [item.get("transform") for item in model.iter("{%s}item" % CORE_NS)]
    _expect(transforms == ["1 0 0 0 1 0 0 0 1 5 5 0", "1 0 0 0 1 0 0 0 1 55 5 0"],
            "the build items keep each body in place: {}".format(transforms))


def _check_touching_bodies(root: Path) -> None:
    path = root / "touching.3mf"
    out = root / "touching_prusa.3mf"
    _write_test_3mf(
        path,
        '<object id="1" name="Base" type="model">{0}</object><object id="2" name="Inlay" type="model">{0}</object>'.format(_cube(10)),
        '<item objectid="1"/><item objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 10"/>',
        _settings(("1", 2), ("2", 1)),
    )
    result = to_prusa_layout(path, out)
    _expect(result == {"objects": 1, "parts": 2, "assembled": True}, "touching bodies become one object: {}".format(result))
    with zipfile.ZipFile(str(out)) as archive:
        config = ET.fromstring(archive.read(PRUSA_MODEL_CONFIG))
        model = ET.fromstring(archive.read(MODEL_PART))
    volumes = [(volume.get("firstid"), volume.get("lastid")) for volume in config.iter("volume")]
    _expect(volumes == [("0", "11"), ("12", "23")], "each part is a triangle range: {}".format(volumes))
    heights = sorted({float(vertex.get("z")) for vertex in model.iter("{%s}vertex" % CORE_NS)})
    _expect(heights == [0.0, 10.0, 20.0], "the inlay stays on top of the base: {}".format(heights))
    _expect(zipfile.ZipFile(str(path)).namelist().count("Metadata/model_settings.config") == 1,
            "with an output path the source stays unchanged")


def _check_components_and_units(root: Path) -> None:
    path = root / "components.3mf"
    _write_test_3mf(
        path,
        '<object id="1" name="Body" type="model">{}</object>'
        '<object id="2" name="Assembly" type="model"><components>'
        '<component objectid="1" transform="1 0 0 0 1 0 0 0 1 1 0 0"/></components></object>'.format(_cube(1)),
        '<item objectid="2"/>',
        _settings(("2", 4)),
        unit="inch",
    )
    to_prusa_layout(path)
    with zipfile.ZipFile(str(path)) as archive:
        model = ET.fromstring(archive.read(MODEL_PART))
        config = ET.fromstring(archive.read(PRUSA_MODEL_CONFIG))
    xs = sorted({float(vertex.get("x")) for vertex in model.iter("{%s}vertex" % CORE_NS)})
    transform = [item.get("transform") for item in model.iter("{%s}item" % CORE_NS)]
    _expect(xs == [-12.7, 12.7] and transform == ["1 0 0 0 1 0 0 0 1 38.1 12.7 0"],
            "inches become millimetres and the component offset is kept: {} {}".format(xs, transform))
    _expect([meta.get("value") for meta in config.iter("metadata") if meta.get("key") == "extruder"] == ["4", "4"],
            "the build item's extruder reaches its component meshes")

    # A Bambu Studio / OrcaSlicer project: one object, parts in 3D/Objects, an extruder per part.
    project = root / "orca_project.3mf"
    with zipfile.ZipFile(str(project), "w") as archive:
        archive.writestr("3D/Objects/object_1.model", (
            '<?xml version="1.0"?><model unit="millimeter" xmlns="{}"><resources>'
            '<object id="1" name="Base" type="model">{}</object><object id="2" name="Inlay" type="model">{}</object>'
            '</resources></model>'
        ).format(CORE_NS, _cube(10), _cube(5)))
        archive.writestr(MODEL_PART, (
            '<?xml version="1.0"?><model unit="millimeter" xmlns="{0}" xmlns:p="{1}"><resources>'
            '<object id="3" type="model"><components>'
            '<component p:path="/3D/Objects/object_1.model" objectid="1"/>'
            '<component p:path="/3D/Objects/object_1.model" objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 10"/>'
            '</components></object></resources><build><item objectid="3"/></build></model>'
        ).format(CORE_NS, PRODUCTION_NS))
        archive.writestr("Metadata/model_settings.config", (
            '<config><object id="3"><metadata key="extruder" value="1"/>'
            '<part id="1"><metadata key="extruder" value="1"/></part>'
            '<part id="2"><metadata key="extruder" value="2"/></part></object></config>'
        ))
    result = to_prusa_layout(project)
    with zipfile.ZipFile(str(project)) as archive:
        names = archive.namelist()
        config = ET.fromstring(archive.read(PRUSA_MODEL_CONFIG))
    volume_extruders = [
        meta.get("value") for meta in config.iter("metadata") if meta.get("type") == "volume" and meta.get("key") == "extruder"
    ]
    _expect(result["parts"] == 2 and volume_extruders == ["1", "2"], "each part keeps its own extruder: {}".format(volume_extruders))
    _expect(not any(name.startswith("3D/Objects/") for name in names), "merged part files are dropped: {}".format(names))


def _check_prusa_family() -> None:
    _expect(is_prusa_family("/Applications/Original Prusa Drivers/PrusaSlicer.app"), "PrusaSlicer bundle")
    _expect(is_prusa_family("/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"), "PrusaSlicer executable")
    _expect(is_prusa_family("C:\\Tools\\SuperSlicer\\superslicer.exe"), "SuperSlicer on Windows")
    _expect(not is_prusa_family("/Applications/OrcaSlicer.app") and not is_prusa_family(""), "others are not")


def _selftest() -> int:
    scenarios = (
        ("separate bodies keep their extruders and places", _check_separate_bodies),
        ("touching bodies become one object with one part each", _check_touching_bodies),
        ("components and inch units are flattened", _check_components_and_units),
    )
    failures = 0
    with tempfile.TemporaryDirectory(prefix="filamatch_prusa_test_") as temp:
        for label, check in scenarios:
            try:
                check(Path(temp))
            except Exception:
                failures += 1
                print("FAIL:", label)
                traceback.print_exc()
            else:
                print("PASS:", label)
    try:
        _check_prusa_family()
    except Exception:
        failures += 1
        print("FAIL: PrusaSlicer and SuperSlicer are recognised")
        traceback.print_exc()
    else:
        print("PASS: PrusaSlicer and SuperSlicer are recognised")
    if failures:
        print("{} prusa_project self-test(s) FAILED".format(failures))
        return 1
    print("PASS: all prusa_project self-tests completed")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
