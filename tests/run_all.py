#!/usr/bin/env python3
"""Run every Filamatch check: module self-tests, test files and a JS syntax check.

Run from anywhere: ``python3 tests/run_all.py``. Exit code 0 means all passed.
Nothing here needs Fusion; tests that touch settings use a temp folder.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = REPO_ROOT / "Filamatch" / "lib"

#: Modules with a self-test (run as ``python3 -m Filamatch.lib.<name>``).
SELF_TEST_MODULES = (
    "catalog", "importer", "exporter", "diagnostics", "materials_workflow", "favorites",
    "slicer_profiles", "openfilament", "custom_library", "printer_model", "secret_store",
    "mqtt_lite", "bambuddy", "bambu_lan", "printer_sources", "inventory_sync", "slot_mapping", "palette_bridge", "fusion_helpers", "prusa_project",
)


def commands() -> list:
    """Return ``(label, argv)`` for every check."""
    python = sys.executable
    runs = [("lib." + name, [python, "-m", "Filamatch.lib." + name]) for name in SELF_TEST_MODULES]
    # The injector also works as a standalone command-line tool, so it runs as a script.
    runs.append(("lib.threemf_injector", [python, str(LIB_DIR / "threemf_injector.py"), "--self-test"]))
    for test in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        runs.append((test.name, [python, str(test)]))
    if shutil.which("node"):
        runs.append(("palette/app.js syntax", ["node", "--check", str(REPO_ROOT / "Filamatch" / "palette" / "app.js")]))
    return runs


def main() -> int:
    failed = []
    with tempfile.TemporaryDirectory(prefix="filamatch-tests-") as data_dir:
        env = dict(os.environ, FILAMATCH_DATA_DIR=data_dir, FILAMATCH_SECRET_BACKEND="memory")
        for label, argv in commands():
            started = time.monotonic()
            result = subprocess.run(argv, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True)
            seconds = time.monotonic() - started
            status = "PASS" if result.returncode == 0 else "FAIL"
            print("{} {:<44} {:6.1f} s".format(status, label, seconds))
            if result.returncode != 0:
                failed.append(label)
                print((result.stdout + result.stderr)[-3000:])
    print()
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
