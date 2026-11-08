# Moved from Printegrity 05a4847 Printegrity/lib/slicer_check.py (now part of Filamatch's As printed tab)
"""Slice each body once and read its grams (docs/PLAN.md, "Calibrate the mass").

For one body: write a plain 3MF of its mesh (turned so the build direction
points up), let Filamatch's slicer code (:mod:`.slicer_profiles`, copied
unchanged) prepare a project for the chosen printer and process, write the
print settings into that project, and slice it. The density that makes
Fusion's mass equal the slicer's grams is ``grams / volume``.

The slicer's total grams also hold part of the printer's start routine (a
purge line). That is not part of the body. So the body's grams are the
filament its own G-code features use (:func:`body_extrusion`, by the
feature labels) times the slicer's grams per millimetre of filament
(its weight over its filament length).

No Fusion API: safe to run in a background job. The body's mesh is read on
Fusion's UI thread first (:func:`.body_geometry.slicing_meshes`).

Limits: OrcaSlicer and Bambu Studio only (PrusaSlicer uses other setting
names).

Self-test with a fake slicer: ``python3 -m Filamatch.lib.slicer_check``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import shutil
import subprocess
import tempfile
import types
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import config
from . import body_settings
from . import mass_model
from . import slicer_profiles

LOGGER = logging.getLogger(config.ADDIN_NAME + ".slicer_check")

PROJECT_SETTINGS = "Metadata/project_settings.config"

#: Filament types the slicer code knows, longest first ("PETG" before "PET").
FILAMENT_TYPES: Tuple[str, ...] = (
    "PETG", "PCTG", "PLA", "ABS", "ASA", "TPU", "PET", "PC", "PA", "PP", "PVA", "HIPS",
)

#: More bodies than this are not sliced in one check.
MAX_BODIES = 20

#: G-code features that are not part of the body (lower case): the start and end
#: routine ("Custom"), and what the slicer adds around it.
NOT_BODY_FEATURES = frozenset({
    "custom", "skirt", "brim", "prime tower", "wipe tower", "support", "support interface",
    "support transition", "purge",
})

#: Times the slicer is run again after it crashes (OrcaSlicer 2.5 crashes now and then).
CRASH_RETRIES = 2


@dataclasses.dataclass(frozen=True)
class BodyMesh:
    """One body, ready to slice: a mesh in the design's coordinates (cm)."""

    name: str
    path: str
    coordinates: Tuple[float, ...]
    indices: Tuple[int, ...]
    volume_cm3: float
    density_g_cm3: float
    material: str = ""
    #: The native body's entity token (finds the body's row again).
    token: str = ""


@dataclasses.dataclass(frozen=True)
class BodyCheck:
    """The slicer's answer for one body."""

    name: str
    path: str
    token: str = ""
    #: The settings the body was sliced with (:meth:`mass_model.PrintSettings.key` and label).
    key: str = ""
    label: str = ""
    #: The body only; ``total_grams`` also holds the start routine's purge.
    grams: Optional[float] = None
    total_grams: Optional[float] = None
    minutes: Optional[int] = None
    density_g_cm3: Optional[float] = None
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def filament_type(material_name: str) -> str:
    """Guess the filament type from a material name ("PLA" when nothing fits)."""
    words = re.split(r"[^A-Z0-9]+", str(material_name or "").upper())
    for known in FILAMENT_TYPES:
        # "PA6", "PA12", "PETGCF", "PLAGF30" count as their base type.
        if any(re.fullmatch(known + r"\d*(?:CF|GF)?\d*", word) for word in words):
            return known
    return "PLA"


#: Settings that are off for a slicer check: brim, skirt, supports and the prime
#: tower are not part of the body (a prime tower alone added about 0.2 g per
#: body in Bambu Studio).
NOT_BODY_OFF: Dict[str, str] = {
    "brim_type": "no_brim",
    "skirt_loops": "0",
    "enable_support": "0",
    "enable_prime_tower": "0",
}


def body_overrides(own: Mapping[str, Any], slicer_kind: str = "") -> Dict[str, str]:
    """The project settings for slicing a body as Filamatch prints it: the print
    process as it is, the body's own settings on top (Filamatch's body_settings
    keys, under the slicer's own names), and nothing that is not the body."""
    overrides = body_settings.to_bambu(own, slicer_kind)
    overrides.update(NOT_BODY_OFF)
    return overrides


def process_overrides(settings: mass_model.PrintSettings, slicer_kind: str = "") -> Dict[str, str]:
    """Project settings that make the slicer print the body as the palette says.

    The layer counts decide the skin (the minimum thicknesses are off), and
    brim, skirt, supports and the prime tower are off: they are not part of
    the body (a prime tower alone added about 0.2 g per body in Bambu Studio).
    """
    return {
        "layer_height": "{:g}".format(settings.layer_height_mm),
        "wall_loops": str(settings.wall_count),
        "top_shell_layers": str(settings.top_layers),
        "bottom_shell_layers": str(settings.bottom_layers),
        "top_shell_thickness": "0",
        "bottom_shell_thickness": "0",
        "sparse_infill_density": "{:g}%".format(settings.infill_percent),
        # The infill type under the slicer's own name ("zig-zag" is OrcaSlicer's "rectilinear").
        "sparse_infill_pattern": body_settings.pattern_for(slicer_kind, settings.infill_pattern),
        **NOT_BODY_OFF,
    }


def apply_print_settings(project_3mf: Path, settings: mass_model.PrintSettings,
                         slicer_kind: str = "", own: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Write the print settings into a prepared project; return the project settings.

    With ``own`` (the body's own settings) only those go on top of the
    process; without it every palette setting is written.
    """
    with zipfile.ZipFile(str(project_3mf), "r") as archive:
        project = json.loads(archive.read(PROJECT_SETTINGS).decode("utf-8"))
    project.update(body_overrides(own, slicer_kind) if own is not None else process_overrides(settings, slicer_kind))
    slicer_profiles.rewrite_zip_entries(
        Path(project_3mf), {PROJECT_SETTINGS: json.dumps(project, indent=4).encode("utf-8")},
        required=(PROJECT_SETTINGS,),
    )
    return project


def nozzle_note(project: Dict[str, Any], settings: mass_model.PrintSettings) -> str:
    """A note when the printer's nozzle differs from the palette's ("" when it matches)."""
    nozzle = project.get("nozzle_diameter")
    value = nozzle[0] if isinstance(nozzle, list) and nozzle else nozzle
    try:
        printer_nozzle = float(value)
    except (TypeError, ValueError):
        return ""
    if abs(printer_nozzle - settings.nozzle_mm) < 1e-6:
        return ""
    return "The printer preset has a {:g} mm nozzle; the slicer used it, not {:g} mm.".format(
        printer_nozzle, settings.nozzle_mm)


def feature_extrusion(gcode: str) -> Dict[str, float]:
    """Millimetres of filament used per G-code feature (relative or absolute E).

    A retraction pulls filament back; the next push first refills it, and
    only the rest counts as new filament.
    """
    totals: Dict[str, float] = {}
    feature = "Custom"
    relative = False
    last_e = 0.0
    retracted = 0.0
    for line in gcode.splitlines():
        if line.startswith("; FEATURE:") or line.startswith(";TYPE:"):
            feature = line.split(":", 1)[1].strip() or "Custom"
            continue
        command = line.split(";", 1)[0].strip()
        if not command:
            continue
        if command.startswith("M83"):
            relative = True
        elif command.startswith("M82"):
            relative = False
        elif command.startswith("G92"):
            match = re.search(r"\bE(-?[\d.]+)", command)
            if match:
                last_e = float(match.group(1))
        elif command.startswith(("G0", "G1", "G2", "G3")):
            match = re.search(r"\bE(-?[\d.]+)", command)
            if not match:
                continue
            value = float(match.group(1))
            pushed = value if relative else value - last_e
            if not relative:
                last_e = value
            if pushed < 0:
                retracted -= pushed
                continue
            refill = min(pushed, retracted)
            retracted -= refill
            if pushed - refill > 0:
                totals[feature] = totals.get(feature, 0.0) + pushed - refill
    return totals


def body_extrusion(features: Dict[str, float]) -> float:
    """Millimetres of filament that go into the body itself."""
    return sum(value for name, value in features.items() if name.strip().lower() not in NOT_BODY_FEATURES)


def slice_project(project_3mf: Path, slicer_exe: str, work_dir: Path) -> Dict[str, Any]:
    """Slice a prepared project; return the body's grams, the slicer's total grams and the minutes.

    Like Filamatch's ``estimate_print`` (same command, nozzle record and crash
    retries), but it keeps the sliced file to split the grams by feature.
    """
    slicer_exe = slicer_profiles.resolve_slicer_executable(slicer_exe)
    copy = work_dir / "slice.3mf"
    shutil.copy2(str(project_3mf), str(copy))
    with zipfile.ZipFile(str(copy), "r") as archive:
        project = json.loads(archive.read(PROJECT_SETTINGS).decode("utf-8"))
    stats = slicer_profiles._nozzle_stats(project)
    if stats and not project.get("extruder_nozzle_stats"):
        project["extruder_nozzle_stats"] = stats
        slicer_profiles.rewrite_zip_entries(copy, {PROJECT_SETTINGS: json.dumps(project).encode("utf-8")})
    command = [str(slicer_exe), "--slice", "0", "--export-3mf", "sliced.3mf", "--outputdir", str(work_dir), str(copy)]
    result: Any = None
    for _attempt in range(CRASH_RETRIES + 1):
        # In the work folder: the slicers write logs and result.json where they run.
        result = slicer_profiles._run_command(command, slicer_profiles.CLI_TIMEOUT_SECONDS, cwd=str(work_dir))
        code = getattr(result, "returncode", None)
        if not (isinstance(code, int) and code < 0):
            break
    sliced = work_dir / "sliced.3mf"
    if not sliced.is_file():
        raise slicer_profiles.SlicerProfilesError("The slicer gave no result (exit code {}).".format(
            getattr(result, "returncode", None)))
    with zipfile.ZipFile(str(sliced), "r") as archive:
        names = archive.namelist()
        info = archive.read("Metadata/slice_info.config").decode("utf-8", "replace") \
            if "Metadata/slice_info.config" in names else ""
        gcode = "".join(archive.read(name).decode("utf-8", "replace") for name in names if name.endswith(".gcode"))
    weights = [float(value) for value in re.findall(r'key="weight" value="([\d.]+)"', info)]
    seconds = [float(value) for value in re.findall(r'key="prediction" value="([\d.]+)"', info)]
    metres = [float(value) for value in re.findall(r'<filament [^>]*?used_m="([\d.]+)"', info)]
    if not weights or not sum(metres) or not gcode:
        raise slicer_profiles.SlicerProfilesError("The slicer gave no weight, filament length or G-code for the body.")
    total = sum(weights)
    grams_per_mm = total / (sum(metres) * 1000.0)
    return {
        "grams": body_extrusion(feature_extrusion(gcode)) * grams_per_mm,
        "total_grams": total,
        "minutes": int(round(sum(seconds) / 60.0)),
    }


def _turn_up(x: float, y: float, z: float, build_direction: str) -> Tuple[float, float, float]:
    """Turn a point so the build direction points up (+Z); right-handed, so triangles keep facing out."""
    if build_direction == "y_up":
        return x, -z, y
    if build_direction == "x_up":
        return -z, y, x
    return x, y, z


def write_body_3mf(path: Path, mesh: BodyMesh, build_direction: str) -> None:
    """Write a plain 3MF (millimetres) of one body, standing on z = 0, centred on x = y = 0."""
    points = [
        _turn_up(mesh.coordinates[i] * 10.0, mesh.coordinates[i + 1] * 10.0, mesh.coordinates[i + 2] * 10.0,
                 build_direction)
        for i in range(0, len(mesh.coordinates) - 2, 3)
    ]
    if not points:
        raise ValueError("the body has no mesh")
    low = [min(point[axis] for point in points) for axis in range(3)]
    high = [max(point[axis] for point in points) for axis in range(3)]
    shift = (-(low[0] + high[0]) / 2.0, -(low[1] + high[1]) / 2.0, -low[2])
    vertices = "".join(
        '<vertex x="{:.5f}" y="{:.5f}" z="{:.5f}"/>'.format(x + shift[0], y + shift[1], z + shift[2])
        for x, y, z in points
    )
    triangles = "".join(
        '<triangle v1="{}" v2="{}" v3="{}"/>'.format(*mesh.indices[i:i + 3])
        for i in range(0, len(mesh.indices) - 2, 3)
    )
    name = re.sub(r'[<>&"]', "_", mesh.name) or "Body"
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        '<resources><object id="1" type="model" name="{}"><mesh><vertices>{}</vertices>'
        '<triangles>{}</triangles></mesh></object></resources>'
        '<build><item objectid="1"/></build></model>'
    ).format(name, vertices, triangles)
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
        '</Types>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
    )
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("3D/3dmodel.model", model)


def check_body(mesh: BodyMesh, settings: mass_model.PrintSettings, target: Any, slicer_exe: str,
               work_dir: Path, own: Optional[Mapping[str, Any]] = None) -> Tuple[BodyCheck, str]:
    """Slice one body. Returns its result and a note about the printer ("" when none)."""
    base = work_dir / "base.3mf"
    project = work_dir / "project.3mf"
    sliced_with = dict(token=mesh.token, key=settings.key(), label=settings.label())
    try:
        write_body_3mf(base, mesh, settings.build_direction)
        material = types.SimpleNamespace(
            color_hex="#808080",
            filament_type=filament_type(mesh.material),
            density_g_cm3=mesh.density_g_cm3,
            display_name=mesh.material or "Filament",
            material_id="printegrity_check",
            category=filament_type(mesh.material),
        )
        slicer_profiles.prepare_project(base, project, target, slicer_exe, [material], log_dir=work_dir / "logs")
        project_settings = apply_print_settings(project, settings, slicer_profiles.slicer_kind(slicer_exe), own)
        sliced = slice_project(project, slicer_exe, work_dir)
        grams = float(sliced["grams"])
        if grams <= 0:
            raise slicer_profiles.SlicerProfilesError("The slicer printed nothing for this body.")
        return BodyCheck(
            name=mesh.name,
            path=mesh.path,
            **sliced_with,
            grams=grams,
            total_grams=float(sliced["total_grams"]),
            minutes=int(sliced["minutes"]),
            density_g_cm3=mass_model.calibrated_density(mesh.volume_cm3, grams),
        ), nozzle_note(project_settings, settings)
    except Exception as exc:  # noqa: BLE001 - one body's failure must not stop the others
        LOGGER.warning("The slicer check of %r failed: %s", mesh.name, exc)
        return BodyCheck(name=mesh.name, path=mesh.path, error=str(exc) or type(exc).__name__, **sliced_with), ""


def check_bodies(items: Sequence[Tuple[Any, ...]], target: Any, slicer_exe: str) -> Dict[str, Any]:
    """Slice every body (at most :data:`MAX_BODIES`) with its own settings, in a temporary cache folder.

    Each item is ``(mesh, settings)`` (every setting written) or ``(mesh,
    settings, own)`` (the process as it is with the body's own settings).

    Returns:
        ``{"bodies": [BodyCheck dicts], "skipped": n, "notes": [...]}``.
    """
    if slicer_profiles.slicer_kind(slicer_exe) == "prusa":
        raise RuntimeError("The slicer check works with OrcaSlicer and Bambu Studio, not PrusaSlicer yet.")
    root = Path(config.CACHE_DIR) / "slicer_check"
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="check_", dir=str(root)))
    results: List[Dict[str, Any]] = []
    notes: List[str] = []
    try:
        for index, item in enumerate(items[:MAX_BODIES]):
            mesh, settings = item[0], item[1]
            own = item[2] if len(item) > 2 else None
            body_dir = work / "{:03d}_{}".format(index, uuid.uuid4().hex[:6])
            body_dir.mkdir()
            result, note = check_body(mesh, settings, target, slicer_exe, body_dir, own)
            results.append(result.to_dict())
            if note and note not in notes:
                notes.append(note)
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
    return {"bodies": results, "skipped": max(0, len(items) - MAX_BODIES), "notes": notes}


# ---------------------------------------------------------------------------
# Self-test with a fake slicer
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


def _cube_mesh(name: str = "Cube", size_cm: float = 2.0) -> BodyMesh:
    coordinates, indices = mass_model._box_mesh(size_cm, size_cm, size_cm / 2)
    return BodyMesh(name=name, path="", coordinates=tuple(coordinates), indices=tuple(indices),
                    volume_cm3=size_cm * size_cm * size_cm / 2, density_g_cm3=1.24, material="Filamatch PETG HF White",
                    token="token-" + name)


def _read_points(path: Path) -> List[Tuple[float, float, float]]:
    with zipfile.ZipFile(str(path)) as archive:
        model = archive.read("3D/3dmodel.model").decode("utf-8")
    return [tuple(float(value) for value in match)
            for match in re.findall(r'<vertex x="([-\d.]+)" y="([-\d.]+)" z="([-\d.]+)"/>', model)]


def _self_test() -> int:
    global MAX_BODIES
    _check(filament_type("Filamatch PETG HF White") == "PETG" and filament_type("PA6-CF Black") == "PA"
           and filament_type("Bambu PLA Basic") == "PLA" and filament_type("Steel") == "PLA",
           "the filament type comes from the material name; PLA when nothing fits")
    rectilinear = mass_model.PrintSettings(wall_count=3, infill_percent=22.5, infill_pattern="rectilinear")
    overrides = process_overrides(rectilinear, "bambu")
    _check(process_overrides(rectilinear, "orca")["sparse_infill_pattern"] == "rectilinear"
           and process_overrides(mass_model.PrintSettings(infill_pattern="zig-zag"), "orca")["sparse_infill_pattern"]
           == "rectilinear", "the infill type goes under each slicer's own name, as Filamatch does")
    _check(overrides["wall_loops"] == "3" and overrides["sparse_infill_density"] == "22.5%"
           and overrides["sparse_infill_pattern"] == "zig-zag" and overrides["brim_type"] == "no_brim"
           and overrides["top_shell_thickness"] == "0" and overrides["enable_prime_tower"] == "0",
           "print settings become the slicer's words; brim, skirt, supports and prime tower are off")
    absolute = "M82\nG92 E0\n;TYPE:Custom\nG1 E5\n;TYPE:External perimeter\nG1 E25\nG1 E24\nG1 E30\nG92 E0\nG1 E5\n"
    split = feature_extrusion(absolute)
    _check(split == {"Custom": 5.0, "External perimeter": 30.0} and body_extrusion(split) == 30.0,
           "absolute E and PrusaSlicer-style labels are read; a refill after a retraction is not new filament")
    _check(body_overrides({"infill": 30, "infill_pattern": "zig-zag", "walls": 4}, "orca")
           == {"sparse_infill_density": "30%", "sparse_infill_pattern": "rectilinear", "wall_loops": "4", **NOT_BODY_OFF}
           and body_overrides({}, "bambu") == NOT_BODY_OFF,
           "as Filamatch prints: the process as it is, the body's own settings on top, nothing that is not the body")
    _check(body_extrusion({"Custom": 3.0, "Skirt": 1.0, "Prime tower": 2.0}) == 0.0,
           "start routine, skirt and prime tower are not body")

    with tempfile.TemporaryDirectory(prefix="printegrity_slicer_check_") as temp:
        temp_dir = Path(temp)
        base = temp_dir / "base.3mf"
        plate = _cube_mesh()  # 2 x 2 x 1 cm
        write_body_3mf(base, plate, "z_up")
        points = _read_points(base)
        spans = [max(p[a] for p in points) - min(p[a] for p in points) for a in range(3)]
        _check(len(points) == 8 and spans == [20.0, 20.0, 10.0] and min(p[2] for p in points) == 0.0
               and abs(sum(p[0] for p in points)) < 1e-9,
               "the 3MF is in millimetres, stands on z = 0 and is centred")
        write_body_3mf(base, plate, "x_up")
        spans = [max(p[a] for p in _read_points(base)) - min(p[a] for p in _read_points(base)) for a in range(3)]
        _check(spans == [10.0, 20.0, 20.0], "x_up turns the body so its X axis points up")
        write_body_3mf(base, plate, "y_up")
        spans = [max(p[a] for p in _read_points(base)) - min(p[a] for p in _read_points(base)) for a in range(3)]
        _check(spans == [20.0, 10.0, 20.0], "y_up turns the body so its Y axis points up")

        calls: List[Any] = []

        def fake_prepare(base_3mf, output_3mf, target, slicer_exe, materials, log_dir=None, extra_entries=None):
            calls.append(("prepare", materials[0].filament_type, materials[0].density_g_cm3))
            with zipfile.ZipFile(str(output_3mf), "w") as archive:
                archive.writestr("3D/3dmodel.model", zipfile.ZipFile(str(base_3mf)).read("3D/3dmodel.model"))
                archive.writestr(PROJECT_SETTINGS, json.dumps({"nozzle_diameter": ["0.6"], "wall_loops": "9"}))

        # 80 mm of the body and 20 mm of purge; the slicer reports 4.0 g for 100 mm: the body is 3.2 g.
        gcode = ("M83\n; FEATURE: Custom\nG1 X1 E16\nG1 E-1\nG1 E1\n; FEATURE: Outer wall\nG1 X2 E50\n"
                 "; FEATURE: Sparse infill\nG1 X3 E30\n; FEATURE: Custom\nG1 E4\n")

        def fake_run(command, timeout, cwd=None):
            project_3mf, out = Path(command[-1]), Path(command[command.index("--outputdir") + 1])
            with zipfile.ZipFile(str(project_3mf)) as archive:
                project = json.loads(archive.read(PROJECT_SETTINGS))
                model = archive.read("3D/3dmodel.model").decode()
            calls.append(("slice", project["wall_loops"], project["brim_type"], cwd == str(out)))
            if "Broken" in model:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="")
            with zipfile.ZipFile(str(out / "sliced.3mf"), "w") as archive:
                archive.writestr("Metadata/slice_info.config",
                                 '<config><plate><metadata key="prediction" value="720"/>'
                                 '<metadata key="weight" value="4.00"/><filament id="1" used_m="0.10" used_g="4.00"/>'
                                 '</plate></config>')
                archive.writestr("Metadata/plate_1.gcode", gcode)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        fake_exe = temp_dir / "OrcaSlicer"
        fake_exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_exe.chmod(0o755)
        saved = (slicer_profiles.prepare_project, slicer_profiles._command_runner, config.CACHE_DIR, MAX_BODIES)
        slicer_profiles.prepare_project = fake_prepare
        slicer_profiles._command_runner = fake_run
        config.CACHE_DIR = temp_dir / "cache"
        try:
            result = check_bodies([(_cube_mesh(), mass_model.PrintSettings()),
                                   (_cube_mesh("Broken"), mass_model.PrintSettings(wall_count=5))], None, str(fake_exe))
            good, bad = result["bodies"]
            _check(abs(good["grams"] - 3.2) < 1e-9 and good["total_grams"] == 4.0 and good["minutes"] == 12
                   and abs(good["density_g_cm3"] - 0.8) < 1e-9,
                   "the body gets its own share of the grams (not the purge) and the density grams / volume")
            _check(("prepare", "PETG", 1.24) in calls and ("slice", "2", "no_brim", True) in calls,
                   "the slicer gets the body's filament and density and the palette's settings, in the work folder")
            _check(bad["grams"] is None and "no result" in bad["error"] and good["error"] == "",
                   "a failed body reports its error; the others still get grams")
            body_only = check_bodies([(_cube_mesh(), mass_model.PrintSettings(wall_count=7), {"infill": 40})],
                                     None, str(fake_exe))
            _check(body_only["bodies"][0]["grams"] and ("slice", "9", "no_brim", True) in calls,
                   "with the body's own settings the process keeps its walls (9 in the fake project)")
            _check(good["key"] == mass_model.PrintSettings().key() and ("slice", "5", "no_brim", True) in calls
                   and bad["key"] == mass_model.PrintSettings(wall_count=5).key() and good["token"] == "token-Cube",
                   "each body is sliced with its own settings, and says which")
            _check(result["notes"] == ["The printer preset has a 0.6 mm nozzle; the slicer used it, not 0.4 mm."],
                   "a nozzle that differs from the printer preset is noted")
            _check(list((temp_dir / "cache" / "slicer_check").iterdir()) == [],
                   "the temporary slicer folder is removed")
            MAX_BODIES = 1
            _check(check_bodies([(_cube_mesh(), mass_model.PrintSettings())] * 2, None, str(fake_exe))["skipped"] == 1,
                   "bodies past the limit are counted, not sliced")
            try:
                check_bodies([(_cube_mesh(), mass_model.PrintSettings())], None, "/Applications/PrusaSlicer.app")
                refused = False
            except RuntimeError:
                refused = True
            _check(refused, "PrusaSlicer is refused for now, in plain words")
        finally:
            slicer_profiles.prepare_project, slicer_profiles._command_runner, config.CACHE_DIR, MAX_BODIES = saved

    print()
    if _FAILED:
        print("SLICER CHECK SELF-TEST FAILED ({} passed, {} failed)".format(_PASSED, _FAILED))
        return 1
    print("SLICER CHECK SELF-TEST PASSED ({} checks)".format(_PASSED))
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
