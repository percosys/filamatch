"""What Filamatch knows about printers, read from ``data/printers.json``.

One data file holds the printer-specific facts the code needs (name tokens
and aliases for preset matching, the printers OrcaSlicer cannot slice, the
serial-number prefixes of Bambu Lab printers) and the test level of every
printer, from which ``scripts/printer_docs.py`` writes the README's printer
tables.  Adding a printer is an edit of that file, not of the code.

No Fusion or network dependencies; ``python3 -m Filamatch.lib.printer_catalog``
runs the self-test.
"""

from __future__ import annotations

import json
import sys
import textwrap
import traceback
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .. import config

#: Markers around the generated part of README.md.
README_START = "<!-- printers:start (written by scripts/printer_docs.py from Filamatch/data/printers.json) -->"
README_END = "<!-- printers:end -->"


@lru_cache(maxsize=4)
def load(path: Optional[str] = None) -> Dict[str, Any]:
    """Return the printer data (cached per path)."""
    with open(path or str(config.PRINTERS_FILE), "r", encoding="utf-8") as handle:
        return json.load(handle)


def aliases() -> Tuple[Tuple[str, str], ...]:
    """Spelled-out model names and the short code presets use (``("X1 CARBON", "X1C")``)."""
    return tuple((str(phrase), str(token)) for phrase, token in load()["printer_aliases"])


def tokens() -> Tuple[str, ...]:
    """Printer family tokens, in matching order."""
    return tuple(str(token) for token in load()["printer_tokens"])


def orca_unsupported() -> Dict[str, str]:
    """Token -> why OrcaSlicer cannot slice that printer."""
    return {str(token): str(reason) for token, reason in load()["orca_unsupported_tokens"].items()}


def bambu_serial_prefixes() -> Dict[str, str]:
    """First three serial-number characters -> Bambu Lab model."""
    return {str(prefix): str(model) for prefix, model in load()["bambu_serial_prefixes"].items()}


def _bullet(text: str) -> str:
    return textwrap.fill(text, width=76, initial_indent="- ", subsequent_indent="  ")


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|")


def readme_section(data: Optional[Mapping[str, Any]] = None) -> str:
    """The README's printer levels (between the markers), as Markdown."""
    levels = (data or load())["levels"]
    lines: List[str] = [README_START, ""]
    validated = levels["validated"]
    lines += ["**{}**".format(validated["title"]), "", "| Printer | What is tested |", "|---|---|"]
    lines += ["| {} | {} |".format(_cell(row["name"]), _cell(row["tested"])) for row in validated["printers"]]
    checked = levels["checked"]
    lines += ["", "**{}**".format(checked["title"]), "", "| Printer | What is checked | Not tested |", "|---|---|---|"]
    lines += [
        "| {} | {} | {} |".format(_cell(row["name"]), _cell(row["tested"]), _cell(row.get("not_tested", "")))
        for row in checked["printers"]
    ]
    expected = levels["expected"]
    lines += ["", "**{}**".format(expected["title"])]
    lines += [_bullet(item) for item in expected["items"]]
    lines += ["", textwrap.fill(levels["plain"]["text"], width=76), "", README_END]
    return "\n".join(lines) + "\n"


def replace_readme_section(readme: str, data: Optional[Mapping[str, Any]] = None) -> str:
    """Return ``readme`` with the part between the markers rewritten."""
    start, end = readme.find(README_START), readme.find(README_END)
    if start < 0 or end < start:
        raise ValueError("README.md has no printer markers ({} ... {})".format(README_START, README_END))
    return readme[:start] + readme_section(data) + readme[end + len(README_END) + 1:]


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _check_data() -> None:
    data = load()
    token_list = tokens()
    _expect(len(token_list) == len(set(token_list)), "tokens are unique")
    _expect(all(token in token_list for _phrase, token in aliases()), "every alias points to a token")
    _expect(all(token in token_list for token in orca_unsupported()), "OrcaSlicer exceptions are known tokens")
    _expect(bambu_serial_prefixes().get("094") == "H2D", "the H2D serial prefix is listed")
    levels = data["levels"]
    _expect(set(levels) == {"validated", "checked", "expected", "plain"}, "the four README levels are present")
    _expect(
        all(row.get("name") and row.get("tested") for level in ("validated", "checked") for row in levels[level]["printers"]),
        "every listed printer says what is tested",
    )
    _expect(all(row.get("not_tested") for row in levels["checked"]["printers"]),
            "every printer checked only in the slicers says what is not tested")


def _check_readme_section() -> None:
    sample = {"levels": {
        "validated": {"title": "1. Real:", "printers": [{"name": "A|B", "tested": "x"}]},
        "checked": {"title": "2. Slicer:", "printers": [{"name": "C", "tested": "y", "not_tested": "z"}]},
        "expected": {"title": "3. Expected:", "items": ["one " * 30]},
        "plain": {"text": "Plain."},
    }}
    section = readme_section(sample)
    _expect(section.startswith(README_START) and section.rstrip().endswith(README_END), "the section has its markers")
    _expect("| A\\|B | x |" in section and "| C | y | z |" in section, "table rows are written and | is escaped")
    _expect(all(len(line) <= 76 for line in section.splitlines() if line.startswith(("- ", "  "))), "bullets are wrapped")
    readme = "intro\n{}\nold\n{}\noutro\n".format(README_START, README_END)
    _expect(replace_readme_section(readme, sample) == "intro\n" + section + "outro\n", "only the marked part is replaced")


def _selftest() -> int:
    scenarios = (
        ("printer data is complete and consistent", _check_data),
        ("the README section is written between its markers", _check_readme_section),
    )
    failures = 0
    for label, check in scenarios:
        try:
            check()
        except Exception:
            failures += 1
            print("FAIL:", label)
            traceback.print_exc()
        else:
            print("PASS:", label)
    if failures:
        print("{} printer_catalog self-test(s) FAILED".format(failures))
        return 1
    print("PASS: all printer_catalog self-tests completed")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
