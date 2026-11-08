#!/usr/bin/env python3
"""Slicer test matrix: prepare and estimate every supported printer, and compare two versions.

Usage (from the repo root):

    python3 scripts/slicer_matrix.py compare                 # working tree against the last release tag
    python3 scripts/slicer_matrix.py compare --base v0.6.0   # against another tag or commit
    python3 scripts/slicer_matrix.py run --out /tmp/matrix   # one run of the working tree

Every case in ``tests/slicer_matrix.json`` prepares a neutral test model (four
20 mm cubes, one filament each) for one printer with one installed slicer
(Bambu Studio, OrcaSlicer or PrusaSlicer), pins the nozzles where the case says
so, and slices a copy for the print estimate, through the same code the add-in
uses.

``compare`` runs the cases twice, with the base version (a temporary git
worktree) and with the working tree, and prints one line per case:

* ``SAME``     the project settings, the slot assignments and the estimate are equal;
* ``CHANGED``  something differs (the changed keys are listed): check it is intended;
* ``FIXED``    the base version failed and this one works;
* ``BROKEN``   the base version worked and this one fails;
* ``NEW``      the base version has no such feature (for example an older version);
* ``SKIPPED``  the slicer for the case is not installed.

It exits with 1 when a case is BROKEN, or CHANGED without ``--accept-changes``,
and writes ``report.md`` (a table for the release notes) and both summaries to
the output folder. Random part ids and temporary file names that the slicers
write on every run are ignored.

The slicers are found in /Applications; set ``FILAMATCH_BAMBU_STUDIO``,
``FILAMATCH_ORCASLICER`` or ``FILAMATCH_PRUSASLICER`` to an executable to use
another one. Nothing here needs Fusion. Runs share the user's slicer profile
cache (``~/Library/Caches/Filamatch``), so the first run of a new online vendor
downloads its presets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
CASES_FILE = REPO_ROOT / "tests" / "slicer_matrix.json"

SLICERS = {
    "bambu": ("FILAMATCH_BAMBU_STUDIO", "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"),
    "orca": ("FILAMATCH_ORCASLICER", "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"),
    "prusaslicer": (
        "FILAMATCH_PRUSASLICER",
        "/Applications/Original Prusa Drivers/PrusaSlicer.app/Contents/MacOS/PrusaSlicer",
    ),
}
CUBE_MM = 20.0
#: How far (mm) a part may move against the export before the case fails.
PLACEMENT_TOLERANCE_MM = 0.05
#: Results in the order they are reported.
RESULTS = ("BROKEN", "CHANGED", "FIXED", "NEW", "SAME", "SKIPPED")


# ---------------------------------------------------------------------------
# Test model
# ---------------------------------------------------------------------------


def _cube_mesh(size: float, offset_x: float = 0.0, offset_y: float = 0.0, offset_z: float = 0.0) -> str:
    points = [(x + offset_x, y + offset_y, z + offset_z) for z in (0, size) for y in (0, size) for x in (0, size)]
    faces = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
             (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    return "<mesh><vertices>{}</vertices><triangles>{}</triangles></mesh>".format(
        "".join('<vertex x="{:g}" y="{:g}" z="{:g}"/>'.format(*point) for point in points),
        "".join('<triangle v1="{}" v2="{}" v3="{}"/>'.format(*face) for face in faces),
    )


def write_test_model(path: Path, materials: Sequence[Sequence[str]], layout: str = "separate") -> Path:
    """Write the test model as a Filamatch export: one cube per material, slots 1, 2, 3...

    ``layout`` "separate" leaves 10 mm between the cubes; "touching" builds one
    multi-colour part from them (several bodies of one design): two cubes side
    by side, the third on top of the first, the fourth on top of the third and
    half over the air, so a part that moves in any direction is noticed.
    Like Fusion's export, the positions are in the mesh and the build items
    have no transform.
    """
    corners = [(120 + index * 30.0, 150.0, 0.0) for index in range(len(materials))]
    if layout == "touching":
        corners = [(120.0, 150.0, 0.0), (140.0, 150.0, 0.0), (120.0, 150.0, 20.0), (130.0, 150.0, 40.0)]
    objects, items = [], []
    for number, (name, _colour, _type, _profile, *_more) in enumerate(materials, start=1):
        x, y, z = corners[number - 1]
        mesh = _cube_mesh(CUBE_MM, x, y, z)
        objects.append('<object id="{}" name="{}" type="model">{}</object>'.format(number, name, mesh))
        items.append('<item objectid="{}"/>'.format(number))
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>{}</resources>'
        '<build>{}</build></model>'
    ).format("".join(objects), "".join(items))
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = path.with_name(path.stem + "_raw.3mf")
    with zipfile.ZipFile(str(raw), "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>'
        ))
        archive.writestr("_rels/.rels", (
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
        ))
        archive.writestr("3D/3dmodel.model", model)
    mapping = path.with_name(path.stem + "_mapping.json")
    mapping.write_text(json.dumps({row[0]: number for number, row in enumerate(materials, start=1)}))
    filaments = path.with_name(path.stem + "_filaments.json")
    filaments.write_text(json.dumps([
        {"extruder": number, "color_hex": colour, "filament_type": kind, "density_g_cm3": 1.24}
        for number, (_name, colour, kind, _profile, *_more) in enumerate(materials, start=1)
    ]))
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "Filamatch" / "lib" / "threemf_injector.py"), "-i", str(raw),
         "-m", str(mapping), "--filaments", str(filaments), "-o", str(path)],
        check=True, capture_output=True,
    )
    return path


# ---------------------------------------------------------------------------
# One run (inside a checkout)
# ---------------------------------------------------------------------------


def slicer_path(kind: str) -> Optional[str]:
    variable, default = SLICERS[kind]
    path = os.environ.get(variable) or default
    return path if Path(path).is_file() else None


def normalize_model_settings(text: str) -> str:
    """Drop what the slicers write differently on every run: part ``uuid`` values and the temp source file."""
    text = re.sub(r'\s+uuid="[^"]*"', "", text)
    return re.sub(r'(<metadata key="source_file" value=")[^"]*(")', r"\1\2", text)


_VERTEX = re.compile(r'<vertex\s+x="([-\d.eE+]+)"\s+y="([-\d.eE+]+)"\s+z="([-\d.eE+]+)"')


def _boxes(model_text: str) -> Dict[str, Tuple[float, ...]]:
    """Object id -> mesh box ``(x0, y0, z0, x1, y1, z1)`` in one model part."""
    boxes = {}
    for object_id, body in re.findall(r'<object id="(\d+)"[^>]*>(.*?)</object>', model_text, re.S):
        points = [tuple(float(value) for value in point) for point in _VERTEX.findall(body)]
        if points:
            boxes[object_id] = tuple(min(p[i] for p in points) for i in range(3)) + tuple(max(p[i] for p in points) for i in range(3))
    return boxes


def body_boxes(model: Path) -> Dict[str, Tuple[float, ...]]:
    """Body name -> box in the test model (positions are in the meshes)."""
    text = zipfile.ZipFile(str(model)).read("3D/3dmodel.model").decode("utf-8")
    names = dict(re.findall(r'<object id="(\d+)" name="([^"]+)"', text))
    return {names[object_id]: box for object_id, box in _boxes(text).items() if object_id in names}


def part_boxes(project: Path) -> Optional[Dict[str, Tuple[float, ...]]]:
    """Part name -> box inside the prepared project's object (None unless it is one object with parts)."""
    with zipfile.ZipFile(str(project)) as archive:
        main = archive.read("3D/3dmodel.model").decode("utf-8")
        if len(re.findall(r"<item\b", main)) != 1:
            return None
        settings = archive.read("Metadata/model_settings.config").decode("utf-8")
        names = dict(re.findall(r'<part id="(\d+)"[^>]*>\s*<metadata key="name" value="([^"]+)"', settings))
        boxes: Dict[str, Tuple[float, ...]] = {}
        for tag in re.findall(r"<component\b[^>]*>", main):
            object_id = re.search(r'objectid="(\d+)"', tag).group(1)
            path = re.search(r'path="/?([^"]+)"', tag)
            transform = re.search(r'transform="([^"]+)"', tag)
            shift = [float(value) for value in transform.group(1).split()][9:12] if transform else [0.0, 0.0, 0.0]
            box = _boxes(archive.read(path.group(1)).decode("utf-8") if path else main).get(object_id)
            if box and object_id in names:
                boxes[names[object_id]] = tuple(box[i] + shift[i % 3] for i in range(6))
    return boxes or None


def placement_error(bodies: Mapping[str, Tuple[float, ...]], parts: Mapping[str, Tuple[float, ...]]) -> float:
    """Largest move (mm) of a part relative to the first body, against the export."""
    names = [name for name in bodies if name in parts]
    if len(names) < 2:
        return 0.0
    first = names[0]
    error = 0.0
    for name in names[1:]:
        for axis in range(3):
            wanted = bodies[name][axis] - bodies[first][axis]
            found = parts[name][axis] - parts[first][axis]
            error = max(error, abs(wanted - found))
    return round(error, 3)


def _sha(data: Any) -> str:
    text = data if isinstance(data, str) else json.dumps(data, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _materials(spec: Sequence[Sequence[Any]]) -> List[Any]:
    """Design materials of a material set; a fifth entry adds catalog fields
    (``manufacturer``, ``product_line``, ``slicer_data`` from the Open Filament Database)."""
    materials = []
    for number, (name, colour, kind, profile, *more) in enumerate(spec, start=1):
        extra = dict(more[0]) if more and isinstance(more[0], Mapping) else {}
        materials.append(SimpleNamespace(display_name=name, color_hex=colour, filament_type=kind, density_g_cm3=1.24,
                                         orca_filament_profile=profile, body_names=(name,), slot=number, **extra))
    return materials


def _run_slicer_case(case: Mapping[str, Any], model: Path, out: Path, materials: List[Any]) -> Dict[str, Any]:
    from Filamatch.lib import palette_bridge, slicer_profiles

    exe = slicer_path(case["slicer"])
    if exe is None:
        return {"status": "skipped", "error": "{} is not installed".format(case["slicer"])}
    try:
        _module, prepare_exe, target = palette_bridge._resolve_profile_target({"target": {
            "slicer_path": exe, "vendor": case["vendor"], "printer": case["printer"],
            "process": case.get("process", ""),
        }})
        project = out / (case["id"] + ".3mf")
        slicer_profiles.prepare_project(model, project, target, prepare_exe, materials)
        if case.get("nozzles"):
            slicer_profiles.apply_nozzle_map(project, case["nozzles"])
    except Exception as exc:
        return {"status": "prepare_failed", "error": _short(exc)}
    with zipfile.ZipFile(str(project)) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
        model_settings = normalize_model_settings(archive.read("Metadata/model_settings.config").decode("utf-8"))
    # Settings the slicer writes differently from run to run for this case.
    for key in case.get("ignore_keys", []):
        settings.pop(key, None)
    (out / (case["id"] + ".settings.json")).write_text(json.dumps(settings, indent=1, sort_keys=True))
    result: Dict[str, Any] = {"status": "ok", "settings_sha": _sha(settings), "model_sha": _sha(model_settings)}
    parts = part_boxes(project)
    if parts is not None:
        # One object with parts (touching bodies): every part must stay where its body was.
        result["placement_error_mm"] = placement_error(body_boxes(model), parts)
        if result["placement_error_mm"] > PLACEMENT_TOLERANCE_MM:
            result["status"] = "misplaced"
            result["error"] = "parts moved up to {} mm against the export".format(result["placement_error_mm"])
    try:
        estimate = slicer_profiles.estimate_print(project, prepare_exe)
        result["estimate"] = [estimate.get("minutes"), estimate.get("filament_grams")]
    except Exception as exc:
        result["estimate"] = None
        result["estimate_error"] = _short(exc)
    return result


def _run_prusaslicer_case(case: Mapping[str, Any], model: Path, out: Path) -> Dict[str, Any]:
    try:
        from Filamatch.lib import prusa_project
    except ImportError:
        return {"status": "unsupported", "error": "this version has no PrusaSlicer layout"}
    exe = slicer_path("prusaslicer")
    if exe is None:
        return {"status": "skipped", "error": "PrusaSlicer is not installed"}
    project = out / (case["id"] + ".3mf")
    try:
        layout = prusa_project.to_prusa_layout(model, project)
    except Exception as exc:
        return {"status": "prepare_failed", "error": _short(exc)}
    count = int(case.get("extruders", 4))
    gcode = out / (case["id"] + ".gcode")
    run = subprocess.run(
        [exe, "--export-gcode", "--nozzle-diameter", ",".join(["0.4"] * count),
         "--temperature", ",".join(["215"] * count), "--first-layer-temperature", ",".join(["215"] * count),
         "--wipe-tower", "--use-relative-e-distances", "--bed-shape", "0x0,250x0,250x210,0x210",
         "--center", "125,105", "-o", str(gcode), str(project)],
        capture_output=True, text=True, cwd=str(out),
    )
    if run.returncode != 0 or not gcode.is_file():
        return {"status": "estimate_failed", "error": (run.stdout + run.stderr).strip()[-200:]}
    text = gcode.read_text(errors="replace")
    tools = sorted(set(re.findall(r"^(T\d+)\s*$", text, re.M)))
    time = re.search(r"^; estimated printing time \(normal mode\) = (.*)$", text, re.M)
    return {
        "status": "ok",
        "settings_sha": _sha(layout),
        "model_sha": _sha(normalize_model_settings(
            zipfile.ZipFile(str(project)).read("Metadata/Slic3r_PE_model.config").decode("utf-8"))),
        # Only the tools are compared: PrusaSlicer 2.6's time changes from run to
        # run (sometimes "2147483647d ..."), so it is kept as information only.
        "estimate": ["tools", tools],
        "time": time.group(1).strip() if time else None,
    }


def _short(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:240]


def run_cases(checkout: Path, models: Mapping[str, Path], out: Path, only: Sequence[str] = ()) -> Dict[str, Any]:
    """Run every case with the code of ``checkout``; return and write ``out/summary.json``."""
    sys.path.insert(0, str(checkout))
    import logging

    logging.disable(logging.CRITICAL)
    spec = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    summary: Dict[str, Any] = {}
    for case in spec["cases"]:
        if only and case["id"] not in only:
            continue
        materials = spec["materials"][case["materials"]]
        model = models["{}-{}".format(case.get("model", "separate"), case["materials"])]
        if case.get("kind") == "prusaslicer":
            summary[case["id"]] = _run_prusaslicer_case(case, model, out)
        else:
            summary[case["id"]] = _run_slicer_case(case, model, out, _materials(materials))
        print("  {:<22} {}".format(case["id"], _one_line(summary[case["id"]])), flush=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True))
    return summary


def _one_line(entry: Mapping[str, Any]) -> str:
    if entry["status"] != "ok":
        return "{}: {}".format(entry["status"], entry.get("error", ""))[:120]
    if entry.get("estimate") is None:
        return "prepared; estimate failed: {}".format(entry.get("estimate_error", ""))[:120]
    return "ok, estimate {}".format(entry["estimate"])


def build_models(folder: Path) -> Dict[str, Path]:
    """Write every test model the cases use (with this repository's injector)."""
    spec = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    models = {}
    for layout in ("separate", "touching"):
        for name, materials in spec["materials"].items():
            models["{}-{}".format(layout, name)] = write_test_model(folder / "{}-{}.3mf".format(layout, name), materials, layout)
    return models


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------


def compare_entries(
    old: Optional[Mapping[str, Any]], new: Mapping[str, Any],
    old_settings: Optional[Mapping[str, Any]] = None, new_settings: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, str]:
    """Return ``(result, detail)`` for one case: SAME, CHANGED, FIXED, BROKEN, NEW or SKIPPED."""
    if new.get("status") == "skipped":
        return "SKIPPED", new.get("error", "")
    if old is None or old.get("status") in ("unsupported", "skipped"):
        # A case the base version did not run: nothing to compare with.
        return "NEW", "" if new.get("status") == "ok" else new.get("error", "")

    def works(entry: Mapping[str, Any]) -> int:
        """0 = prepare failed, 1 = parts moved, 2 = prepared but no estimate, 3 = all fine."""
        if entry.get("status") == "misplaced":
            return 1
        if entry.get("status") != "ok":
            return 0
        return 3 if entry.get("estimate") is not None else 2

    before, after = works(old), works(new)
    if after < before:
        return "BROKEN", new.get("error") or new.get("estimate_error") or ""
    if after > before:
        return "FIXED", "before: " + str(old.get("error") or old.get("estimate_error") or "")
    if after <= 1:
        same = old.get("error") == new.get("error")
        return ("SAME", "still fails") if same else ("CHANGED", "fails differently: " + new.get("error", ""))
    changes = []
    if old.get("settings_sha") != new.get("settings_sha"):
        keys = _changed_keys(old_settings or {}, new_settings or {})
        changes.append("settings: " + (", ".join(keys[:8]) + (" ..." if len(keys) > 8 else "") if keys else "differ"))
    if old.get("model_sha") != new.get("model_sha"):
        changes.append("slot assignments differ")
    if old.get("estimate") != new.get("estimate"):
        changes.append("estimate {} -> {}".format(old.get("estimate"), new.get("estimate")))
    if after == 2 and old.get("estimate_error") != new.get("estimate_error"):
        changes.append("estimate fails differently")
    return ("CHANGED", "; ".join(changes)) if changes else ("SAME", "")


def _changed_keys(old: Mapping[str, Any], new: Mapping[str, Any]) -> List[str]:
    return sorted(key for key in set(old) | set(new) if old.get(key) != new.get(key))


def compare_summaries(old_dir: Path, new_dir: Path) -> List[Tuple[str, str, str, Mapping[str, Any]]]:
    old = json.loads((old_dir / "summary.json").read_text())
    new = json.loads((new_dir / "summary.json").read_text())
    rows = []
    for case_id, entry in new.items():
        old_settings = _read_json(old_dir / (case_id + ".settings.json"))
        new_settings = _read_json(new_dir / (case_id + ".settings.json"))
        result, detail = compare_entries(old.get(case_id), entry, old_settings, new_settings)
        rows.append((case_id, result, detail, entry))
    return rows


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def report_markdown(rows: Sequence[Tuple[str, str, str, Mapping[str, Any]]], base: str) -> str:
    """The release-notes table: one row per case, with its estimate."""
    counts = {result: sum(1 for row in rows if row[1] == result) for result in RESULTS}
    lines = [
        "Slicer test matrix against {}: {}.".format(
            base, ", ".join("{} {}".format(counts[result], result.lower()) for result in RESULTS if counts[result])
        ),
        "",
        "| Case | Result | Estimate |",
        "|---|---|---|",
    ]
    for case_id, result, detail, entry in rows:
        estimate = entry.get("estimate")
        if isinstance(estimate, list) and estimate and isinstance(estimate[1], list) and estimate[1] and isinstance(estimate[1][0], (int, float)):
            shown = "{} min, {:.1f} g".format(estimate[0], sum(estimate[1]))
        elif estimate:
            shown = "{} ({})".format(estimate[0], ", ".join(estimate[1]) if isinstance(estimate[1], list) else estimate[1])
        else:
            shown = "no estimate: " + str(entry.get("estimate_error") or entry.get("error") or "")[:60]
        lines.append("| {} | {}{} | {} |".format(case_id, result, " ({})".format(detail[:80]) if detail and result != "SAME" else "", shown))
    return "\n".join(lines) + "\n"


def latest_tag() -> str:
    """The newest version tag (``v*``): the last release."""
    tags = subprocess.run(["git", "tag", "--list", "v*", "--sort=-v:refname"], cwd=str(REPO_ROOT),
                          capture_output=True, text=True).stdout.split()
    if not tags:
        raise SystemExit("FAILED: no version tag to compare with")
    return tags[0]


def _run_in_subprocess(checkout: Path, models_dir: Path, out: Path, only: Sequence[str]) -> None:
    command = [sys.executable, str(Path(__file__).resolve()), "_run", "--checkout", str(checkout),
               "--models", str(models_dir), "--out", str(out)]
    if only:
        command += ["--only", ",".join(only)]
    subprocess.run(command, check=True, cwd=str(out.parent))


def compare(base: str, out: Path, accept_changes: bool, only: Sequence[str]) -> int:
    out = out.resolve()
    if out.exists():
        shutil.rmtree(str(out))
    models_dir = out / "models"
    build_models(models_dir)
    worktree = Path(tempfile.mkdtemp(prefix="filamatch-matrix-base-"))
    shutil.rmtree(str(worktree))
    subprocess.run(["git", "worktree", "add", "--detach", "--quiet", str(worktree), base], cwd=str(REPO_ROOT), check=True)
    try:
        print("Base {} ...".format(base), flush=True)
        _run_in_subprocess(worktree, models_dir, out / "base", only)
        print("This version ...", flush=True)
        _run_in_subprocess(REPO_ROOT, models_dir, out / "new", only)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=str(REPO_ROOT), capture_output=True)
    rows = compare_summaries(out / "base", out / "new")
    print()
    for case_id, result, detail, _entry in rows:
        print("{:<8} {:<22} {}".format(result, case_id, detail))
    report = report_markdown(rows, base)
    (out / "report.md").write_text(report)
    print()
    print(report.splitlines()[0])
    print("Report: {}".format(out / "report.md"))
    if all(row[1] == "SKIPPED" for row in rows):
        print("WARNING: no slicer is installed; nothing was compared.")
    if any(row[1] == "BROKEN" for row in rows):
        print("FAILED: a case that worked in {} is broken.".format(base))
        return 1
    if any(row[1] == "CHANGED" for row in rows) and not accept_changes:
        print("Check the CHANGED cases; if the changes are intended, run again with --accept-changes.")
        return 1
    return 0


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    compare_parser = sub.add_parser("compare", help="compare the working tree with a release")
    compare_parser.add_argument("--base", default="", help="tag or commit to compare with (default: the last release tag)")
    compare_parser.add_argument("--out", type=Path, default=REPO_ROOT / "dist" / "matrix", help="output folder")
    compare_parser.add_argument("--accept-changes", action="store_true", help="CHANGED cases do not fail the run")
    compare_parser.add_argument("--only", default="", help="comma-separated case ids")
    run_parser = sub.add_parser("run", help="run the cases with the working tree only")
    run_parser.add_argument("--out", type=Path, required=True)
    run_parser.add_argument("--only", default="")
    hidden = sub.add_parser("_run")  # one checkout, in a fresh process
    hidden.add_argument("--checkout", type=Path, required=True)
    hidden.add_argument("--models", type=Path, required=True)
    hidden.add_argument("--out", type=Path, required=True)
    hidden.add_argument("--only", default="")
    args = parser.parse_args(argv)
    only = [item.strip() for item in args.only.split(",") if item.strip()]
    if args.command == "compare":
        return compare(args.base or latest_tag(), args.out, args.accept_changes, only)
    if args.command == "run":
        out = args.out.resolve()
        models = build_models(out / "models")
        run_cases(REPO_ROOT, models, out, only)
        return 0
    models = {path.stem: path for path in args.models.glob("*.3mf") if not path.stem.endswith("_raw")}
    run_cases(args.checkout.resolve(), models, args.out.resolve(), only)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
