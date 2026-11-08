"""The macOS install script: a normal copy, the --dev link, and back.

It runs the script against a temporary AddIns folder and Trash
(``FILAMATCH_ADDINS_DIR``, ``FILAMATCH_TRASH_DIR``), never against Fusion.

Run from the repository root:

    python3 tests/test_install_script.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "install_addin_macos.sh"
SOURCE = REPO_ROOT / "Filamatch"

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


def install(addins: Path, trash: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, FILAMATCH_ADDINS_DIR=str(addins), FILAMATCH_TRASH_DIR=str(trash))
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True)


def main() -> int:
    if not shutil.which("bash") or not shutil.which("rsync"):
        print("SKIPPED: bash or rsync is not installed")
        return 0
    before = sorted(str(path.relative_to(SOURCE)) for path in SOURCE.rglob("*") if "__pycache__" not in path.parts)
    with tempfile.TemporaryDirectory(prefix="filamatch-install-test-") as temp:
        addins, trash = Path(temp) / "AddIns", Path(temp) / "Trash"
        target = addins / "Filamatch"

        result = install(addins, trash)
        check(result.returncode == 0 and target.is_dir() and not target.is_symlink()
              and (target / "Filamatch.manifest").is_file(), "the default install copies the add-in")

        result = install(addins, trash, "--dev")
        check(result.returncode == 0 and target.is_symlink() and target.resolve() == SOURCE.resolve(),
              "--dev links the add-in folder to the repository")
        check((target / "palette" / "index.html").is_file(), "the palette is reachable through the link")
        check(len(list(trash.glob("Filamatch-installed-*"))) == 1, "the installed copy went to the Trash, not deleted")

        result = install(addins, trash, "--dev")
        check(result.returncode == 0 and target.is_symlink(), "running --dev again keeps the link")

        result = install(addins, trash)
        check(result.returncode == 0 and target.is_dir() and not target.is_symlink(),
              "a normal install turns the link back into a copy")

        result = install(addins, trash, "--dev", "--dry-run")
        check(result.returncode == 0 and "would become a link" in result.stdout and not target.is_symlink(),
              "--dev --dry-run changes nothing")
    after = sorted(str(path.relative_to(SOURCE)) for path in SOURCE.rglob("*") if "__pycache__" not in path.parts)
    check(before == after, "the repository's add-in folder is unchanged")

    print()
    if FAILURES:
        print("INSTALL SCRIPT CHECKS FAILED ({} check(s))".format(len(FAILURES)))
        for label in FAILURES:
            print(" -", label)
        return 1
    print("INSTALL SCRIPT CHECKS PASSED ({} checks)".format(PASSED))
    return 0


if __name__ == "__main__":
    sys.exit(main())
