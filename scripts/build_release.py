#!/usr/bin/env python3
"""Build a release zip of the Filamatch add-in.

Usage (from the repo root):

    python3 scripts/build_release.py            # dist/Filamatch-<version>.zip
    python3 scripts/build_release.py --out DIR

The zip holds one top-level ``Filamatch/`` folder, ready to unzip into
Fusion's AddIns folder. Caches, compiled files and macOS metadata are left
out. The version comes from ``Filamatch/Filamatch.manifest``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDIN_DIR = REPO_ROOT / "Filamatch"
EXCLUDED_PARTS = {"__pycache__", ".DS_Store"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


def addin_version() -> str:
    """Return the version from the add-in manifest."""
    manifest = json.loads((ADDIN_DIR / "Filamatch.manifest").read_text(encoding="utf-8"))
    return str(manifest["version"])


def release_files() -> list:
    """Return the add-in files tracked by git (or every file outside git), sorted.

    Using git's list keeps stray local files (logs, slicer output) out of a release.
    """
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "Filamatch"], cwd=str(REPO_ROOT),
            capture_output=True, check=True,
        ).stdout.decode("utf-8").split("\0")
        candidates = [REPO_ROOT / name for name in listed if name]
    except (OSError, subprocess.CalledProcessError):
        candidates = list(ADDIN_DIR.rglob("*"))
    files = []
    for path in sorted(candidates):
        if not path.is_file():
            continue
        if EXCLUDED_PARTS.intersection(path.parts) or path.suffix in EXCLUDED_SUFFIXES:
            continue
        files.append(path)
    return files


def build(out_dir: Path) -> Path:
    """Write ``Filamatch-<version>.zip`` into ``out_dir`` and return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "Filamatch-{}.zip".format(addin_version())
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in release_files():
            archive.write(path, Path("Filamatch") / path.relative_to(ADDIN_DIR))
    return target


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "dist", help="output folder")
    args = parser.parse_args(argv)
    target = build(args.out)
    print("Built {} ({} files)".format(target, len(release_files())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
