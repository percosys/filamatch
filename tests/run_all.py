#!/usr/bin/env python3
"""Run every Filamatch check: module self-tests, test files and a JS syntax check.

Run from anywhere: ``python3 tests/run_all.py``. Exit code 0 means all passed.
Nothing here needs Fusion; tests that touch settings use a temp folder.

Faster runs while you work:

    python3 tests/run_all.py --changed          # only the checks your changes can affect
    python3 tests/run_all.py --changed v0.6.1   # ... compared with another branch, tag or commit
    python3 tests/run_all.py --only bambu_lan,test_palette_contract.py

``--changed`` looks at the files changed against ``main`` (committed, staged,
unstaged and new files). A changed module runs its own self-test, the
self-tests of every module that imports it (directly or not), and every test
file that names one of them. Files it cannot place (``config.py``, the add-in
entry point, this file...) run everything. Before you merge, run everything.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = REPO_ROOT / "Filamatch" / "lib"

#: Modules with a self-test (run as ``python3 -m Filamatch.lib.<name>``).
SELF_TEST_MODULES = (
    "catalog", "importer", "exporter", "diagnostics", "materials_workflow", "favorites",
    "slicer_profiles", "openfilament", "custom_library", "printer_model", "secret_store",
    "mqtt_lite", "bambuddy", "bambu_lan", "printer_sources", "inventory_sync", "slot_mapping", "palette_bridge", "fusion_helpers", "prusa_project", "printer_catalog", "printer_report", "filament_presets", "prusa_profiles", "design_check", "body_settings",
    # The As printed tab (moved from Printegrity).
    "mass_model", "strength_model", "body_geometry", "material_copy", "slicer_check", "as_printed_bridge",
)
#: Modules whose checks live in another module's self-test.
TESTED_BY = {"project_fixes": "slicer_profiles", "slicer_profiles_selftest": "slicer_profiles", "testing": None}
JS_LABEL = "palette scripts syntax"
#: Files outside Filamatch/lib and the checks they need.
OTHER_FILES = {
    "Filamatch/palette/as_printed.js": ["test_as_printed_contract.py", JS_LABEL],
    "Filamatch/palette/as_printed.css": ["test_as_printed_contract.py"],
    "Filamatch/palette/": ["test_palette_contract.py", "test_palette_export_flow.py", JS_LABEL],
    "Filamatch/data/printers.json": ["lib.printer_catalog", "test_printer_docs.py"],
    "Filamatch/data/filament_rules.json": ["lib.design_check"],
    "Filamatch/data/filaments.json": ["lib.catalog"],
    "scripts/slicer_matrix.py": ["test_slicer_matrix.py"],
    "tests/slicer_matrix.json": ["test_slicer_matrix.py"],
    "scripts/printer_docs.py": ["test_printer_docs.py"],
    "Filamatch/data/datasheets/": ["test_datasheet_docs.py"],
    "scripts/datasheet_docs.py": ["test_datasheet_docs.py"],
    "README.md": ["test_printer_docs.py"],
}
#: Changes that need no check.
NO_CHECKS = ("docs/", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE", ".github/", "_private/", ".gitignore", ".gitattributes")


def commands() -> list:
    """Return ``(label, argv)`` for every check."""
    python = sys.executable
    runs = [("lib." + name, [python, "-m", "Filamatch.lib." + name]) for name in SELF_TEST_MODULES]
    # The injector also works as a standalone command-line tool, so it runs as a script.
    runs.append(("lib.threemf_injector", [python, str(LIB_DIR / "threemf_injector.py"), "--self-test"]))
    for test in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        runs.append((test.name, [python, str(test)]))
    if shutil.which("node"):
        # node --check takes one file; "&&" would need a shell, so check each and join.
        palette = REPO_ROOT / "Filamatch" / "palette"
        runs.append((JS_LABEL, [sys.executable, "-c", (
            "import subprocess, sys\n"
            "sys.exit(max(subprocess.run(['node', '--check', path]).returncode for path in sys.argv[1:]))"),
            str(palette / "app.js"), str(palette / "as_printed.js")]))
    return runs


def _git(*args: str) -> List[str]:
    result = subprocess.run(["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def changed_files(base: str) -> List[str]:
    """Files changed against ``base``: committed since it, staged, unstaged and untracked."""
    files = set(_git("diff", "--name-only", "{}...HEAD".format(base)))
    files |= set(_git("diff", "--name-only", "HEAD"))
    files |= set(_git("ls-files", "--others", "--exclude-standard"))
    return sorted(files)


def importers() -> Dict[str, Set[str]]:
    """Module -> the lib modules that import it (``from . import x`` / ``from .x import``)."""
    found: Dict[str, Set[str]] = {}
    for path in LIB_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        names = set(re.findall(r"^\s*from \.(\w+) import", text, re.M))
        for group in re.findall(r"^\s*from \. import ([\w, ()\n]+?)(?:#|$)", text, re.M):
            names |= {name.strip() for name in group.replace("(", " ").replace(")", " ").split(",") if name.strip()}
        for name in names:
            found.setdefault(name, set()).add(path.stem)
    return found


def checks_for(files: Sequence[str]) -> Optional[Set[str]]:
    """Labels of the checks the changed files need; None means run everything."""
    labels: Set[str] = set()
    graph = importers()
    tests = {path.name: path.read_text(encoding="utf-8", errors="replace") for path in (REPO_ROOT / "tests").glob("test_*.py")}
    for name in files:
        if name.startswith(NO_CHECKS) or name.endswith((".md", ".png", ".jpg", ".svg")) and not name == "README.md":
            continue
        other = [labels_ for prefix, labels_ in OTHER_FILES.items() if name == prefix or (prefix.endswith("/") and name.startswith(prefix))]
        if other:
            for group in other:
                labels |= set(group)
            continue
        if name.startswith("tests/test_") and name.endswith(".py"):
            labels.add(Path(name).name)
            continue
        if not (name.startswith("Filamatch/lib/") and name.endswith(".py")):
            return None
        module = Path(name).stem
        affected, todo = set(), [module]
        while todo:
            current = todo.pop()
            if current in affected:
                continue
            affected.add(current)
            todo.extend(graph.get(current, ()))
        for current in affected:
            owner = TESTED_BY.get(current, current)
            if owner in SELF_TEST_MODULES:
                labels.add("lib." + owner)
            if current == "threemf_injector":
                labels.add("lib.threemf_injector")
            labels |= {test for test, text in tests.items() if re.search(r"\b{}\b".format(re.escape(current)), text)}
    return labels


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--changed", nargs="?", const="main", default=None, metavar="BASE",
                        help="run only the checks affected by changes against BASE (default: main)")
    parser.add_argument("--only", default="", help="comma-separated checks: module names, test files or 'js'")
    args = parser.parse_args(argv)
    runs = commands()
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        labels = {("lib." + item) if not item.startswith(("lib.", "test_")) and item != "js" else item for item in wanted}
        labels = {JS_LABEL if item == "js" else item for item in labels}
        runs = [run for run in runs if run[0] in labels]
        if not runs:
            print("No check matches --only {}".format(args.only))
            return 2
    elif args.changed is not None:
        files = changed_files(args.changed)
        needed = checks_for(files)
        if needed is None:
            print("Changes need every check ({} file(s) against {}).".format(len(files), args.changed))
        else:
            runs = [run for run in runs if run[0] in needed]
            print("{} changed file(s) against {}: {} of {} checks.".format(len(files), args.changed, len(runs), len(commands())))
            if not runs:
                print("No checks needed.")
                return 0
    failed = []
    with tempfile.TemporaryDirectory(prefix="filamatch-tests-") as data_dir:
        env = dict(os.environ, FILAMATCH_DATA_DIR=data_dir, FILAMATCH_SECRET_BACKEND="memory")
        for label, argv_ in runs:
            started = time.monotonic()
            result = subprocess.run(argv_, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True)
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
    print("ALL CHECKS PASSED" if len(runs) == len(commands()) else "SELECTED CHECKS PASSED ({})".format(len(runs)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
