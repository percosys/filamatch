#!/usr/bin/env python3
"""Write the README's printer tables from Filamatch/data/printers.json.

Usage (from the repo root):

    python3 scripts/printer_docs.py           # rewrite README.md between the printer markers
    python3 scripts/printer_docs.py --check   # exit 1 when README.md is out of date

``tests/test_printer_docs.py`` runs the check, so the README and the data
file cannot drift apart.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
sys.path.insert(0, str(REPO_ROOT))

from Filamatch.lib import printer_catalog  # noqa: E402


def main(argv: list) -> int:
    current = README.read_text(encoding="utf-8")
    wanted = printer_catalog.replace_readme_section(current)
    if "--check" in argv:
        if current != wanted:
            print("README.md is out of date: run python3 scripts/printer_docs.py")
            return 1
        print("README.md printer tables match Filamatch/data/printers.json")
        return 0
    if current != wanted:
        README.write_text(wanted, encoding="utf-8")
        print("Rewrote the printer tables in README.md")
    else:
        print("README.md printer tables are up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
