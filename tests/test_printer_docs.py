"""The README's printer tables match Filamatch/data/printers.json.

Run from the repository root:

    python3 tests/test_printer_docs.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "printer_docs.py"), "--check"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    print((result.stdout + result.stderr).strip())
    if result.returncode != 0:
        print("PRINTER DOCS CHECK FAILED")
        return 1
    print("PRINTER DOCS CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
