"""Checks for scripts/slicer_matrix.py that need no slicer.

The matrix itself runs the installed slicers; this file checks its pure parts:
the case list, the test model, the clean-up of run-to-run noise, and how two
runs are compared and reported.

Run from the repository root:

    python3 tests/test_slicer_matrix.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "slicer_matrix.py"

FAILURES: List[str] = []
PASSED = 0


def check(condition: bool, label: str) -> None:
    """Record and print one PASS/FAIL check."""
    global PASSED
    if condition:
        PASSED += 1
        print("PASS {}".format(label))
    else:
        FAILURES.append(label)
        print("FAIL {}".format(label))


def load_matrix():
    spec = importlib.util.spec_from_file_location("slicer_matrix", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    matrix = load_matrix()

    cases = json.loads((REPO_ROOT / "tests" / "slicer_matrix.json").read_text(encoding="utf-8"))
    ids = [case["id"] for case in cases["cases"]]
    check(len(ids) == len(set(ids)), "case ids are unique")
    check(
        all(case["materials"] in cases["materials"] for case in cases["cases"])
        and all(case.get("slicer") in matrix.SLICERS for case in cases["cases"] if case.get("kind") != "prusaslicer"),
        "every case names a known material set and slicer",
    )
    check(
        all(len(case.get("nozzles", [])) in (0, len(cases["materials"][case["materials"]])) for case in cases["cases"]),
        "nozzle pins have one entry per filament",
    )

    with tempfile.TemporaryDirectory(prefix="filamatch-matrix-test-") as temp:
        model = matrix.write_test_model(Path(temp) / "touching.3mf", cases["materials"]["pla_petg"], "touching")
        with zipfile.ZipFile(str(model)) as archive:
            names = archive.namelist()
            settings = archive.read("Metadata/model_settings.config").decode()
            body = archive.read("3D/3dmodel.model").decode()
        bodies = matrix.body_boxes(model)
    check("Metadata/project_settings.config" in names and settings.count('key="extruder"') == 4,
          "the test model is a Filamatch export with four slots")
    check("<item objectid=\"2\"/>" in body and 'z="60"' in body, "touching cubes form one stacked part, positions in the mesh as in Fusion")
    check(bodies["White cube"][2] == 20.0 and bodies["Red cube"][0] == 130.0, "body boxes are read from the meshes")
    moved = dict(bodies, **{"Red cube": tuple(value + (1.0 if index in (2, 5) else 0.0) for index, value in enumerate(bodies["Red cube"]))})
    check(matrix.placement_error(bodies, bodies) == 0.0 and matrix.placement_error(bodies, moved) == 1.0,
          "a part that moved 1 mm is measured")
    shifted = {name: tuple(value - 100 for value in box) for name, box in bodies.items()}
    check(matrix.placement_error(bodies, shifted) == 0.0, "moving the whole object is fine")

    noisy = ('<part id="1" subtype="normal_part" uuid="44d4f575-85ab"><metadata key="source_file" '
             'value="filamatch_base_wtpvitp5.3mf"/></part>')
    quiet = ('<part id="1" subtype="normal_part" uuid="3f2f5a1b-46c8"><metadata key="source_file" '
             'value="filamatch_base_8wqmxcbt.3mf"/></part>')
    check(matrix.normalize_model_settings(noisy) == matrix.normalize_model_settings(quiet),
          "random part ids and temp file names are ignored")

    ok = {"status": "ok", "settings_sha": "a", "model_sha": "m", "estimate": [10, [1.0, 2.0]]}
    check(matrix.compare_entries(ok, dict(ok))[0] == "SAME", "equal runs are SAME")
    changed = matrix.compare_entries(ok, dict(ok, settings_sha="b"), {"wipe_tower_y": ["216"]}, {"wipe_tower_y": ["202"]})
    check(changed == ("CHANGED", "settings: wipe_tower_y"), "a changed setting is named: {}".format(changed))
    check(matrix.compare_entries(ok, dict(ok, estimate=[11, [1.0, 2.0]]))[0] == "CHANGED", "a changed estimate is CHANGED")
    check(matrix.compare_entries({"status": "prepare_failed", "error": "x"}, ok)[0] == "FIXED", "fail -> ok is FIXED")
    check(matrix.compare_entries(ok, {"status": "prepare_failed", "error": "x"})[0] == "BROKEN", "ok -> fail is BROKEN")
    no_estimate = dict(ok, estimate=None, estimate_error="crash")
    check(matrix.compare_entries(ok, no_estimate)[0] == "BROKEN", "losing the estimate is BROKEN")
    misplaced = {"status": "misplaced", "error": "parts moved up to 1.0 mm", "estimate": [10, [2.1]]}
    check(matrix.compare_entries(misplaced, ok)[0] == "FIXED" and matrix.compare_entries(ok, misplaced)[0] == "BROKEN",
          "moved parts count as failing")
    check(matrix.compare_entries(None, ok)[0] == "NEW", "a new case is NEW")
    check(matrix.compare_entries({"status": "unsupported"}, ok)[0] == "NEW", "a feature the base lacks is NEW")
    check(matrix.compare_entries(ok, {"status": "skipped"})[0] == "SKIPPED", "a missing slicer is SKIPPED")
    still = matrix.compare_entries({"status": "prepare_failed", "error": "x"}, {"status": "prepare_failed", "error": "x"})
    check(still == ("SAME", "still fails"), "the same failure is SAME")

    report = matrix.report_markdown(
        [("h2d-bambu", "SAME", "", ok), ("u1-mixed-orca", "FIXED", "before: refused", ok),
         ("prusaslicer-layout", "SAME", "", {"status": "ok", "estimate": ["tools", ["T0", "T1"]]})],
        "v0.6.1",
    )
    check(report.startswith("Slicer test matrix against v0.6.1: 1 fixed, 2 same."), "the report counts the results")
    check("| h2d-bambu | SAME | 10 min, 3.0 g |" in report and "tools (T0, T1)" in report, "the report shows each estimate")

    print()
    if FAILURES:
        print("SLICER MATRIX CHECKS FAILED ({} check(s))".format(len(FAILURES)))
        for label in FAILURES:
            print(" -", label)
        return 1
    print("SLICER MATRIX CHECKS PASSED ({} checks)".format(PASSED))
    return 0


if __name__ == "__main__":
    sys.exit(main())
