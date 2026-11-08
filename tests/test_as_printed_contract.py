"""Static contract checks for the printed weight card (palette/as_printed.js <-> lib/as_printed_bridge.py).

Reads the request -> reply pairs from both sides, so a new action fails here
until both sides handle it, and checks the card keeps the palette's rules
(docs/development.md): textContent only, its values in Filamatch's body
lists, no build direction, and estimates marked as such.

Run from the repository root:

    python3 tests/test_as_printed_contract.py
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SCRIPT = REPO_ROOT / "Filamatch" / "palette" / "as_printed.js"
STYLE = REPO_ROOT / "Filamatch" / "palette" / "as_printed.css"
INDEX = REPO_ROOT / "Filamatch" / "palette" / "index.html"

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


def main() -> int:
    js = SCRIPT.read_text(encoding="utf-8")
    css = STYLE.read_text(encoding="utf-8")
    html = INDEX.read_text(encoding="utf-8")
    bridge = importlib.import_module("Filamatch.lib.as_printed_bridge")

    block = re.search(r"var ACTIONS = \{(.*?)\};", js, re.S)
    pairs = dict(re.findall(r"(\w+): \['(\w+)'", block.group(1))) if block else {}
    check(bool(pairs), "as_printed.js lists its requests and replies in ACTIONS")
    check(pairs == bridge.REPLIES, "the page and as_printed_bridge agree on every request and reply: {}".format(pairs))
    for request in sorted(pairs):
        check(request in bridge._ACTIONS, "as_printed_bridge handles {!r}".format(request))
        check(request.startswith("as_printed_"), "{!r} has the tab's prefix (no clash with Filamatch's actions)".format(request))
    for reply in sorted(set(pairs.values())):
        check("api.onMessage('{}'".format(reply) in js, "as_printed.js handles reply {!r}".format(reply))
        check("registerDemo('{}'".format([k for k, v in pairs.items() if v == reply][0]) in js,
              "as_printed.js has a demo reply for {!r}".format(reply))

    code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    check("innerHTML" not in code and "textContent" in code, "the card writes text with textContent, never innerHTML")
    check("registerBodyField" not in code and "build_direction" not in code and "Up on the bed" not in code,
          "no build direction: the estimate uses the design's orientation, as the slicer does")
    check("api.registerBodyDetail(" in code and "api.registerBodyNotes(" in code and "api.refreshBodyLists()" in code
          and "createBodyList" not in code,
          "each body's grams and notes go into Filamatch's body lists (no list of the card's own)")
    check("btn--primary" not in code and "switch-field" in code and "Fusion uses the printed values" in code,
          "the card has no primary button (Send is the Print view's); the switch is a switch-field")
    check("rough estimates" in code and "not design values" in code and "Where the values come from" in code,
          "stiffness and strength are marked as rough estimates, not design values, with their sources")
    check("estimate" in js and "slicer check" in js, "values say whether they are estimates or from the slicer check")
    check(not re.search(r"#[0-9a-fA-F]{3,6}\b", re.sub(r"/\*.*?\*/", "", css, flags=re.S)),
          "as_printed.css uses style.css's colour tokens only")
    check("as_printed.js" in html and "as_printed.css" in html and 'id="as-printed-root"' in html,
          "index.html loads the card's files and has its root")
    # The SVG namespace is a name, not an address the palette loads.
    check(not re.search(r"https?://", (js + css).replace("http://www.w3.org/2000/svg", "")),
          "the card is self-contained (no network)")

    print()
    if FAILURES:
        print("AS PRINTED CONTRACT FAILED ({} of {} checks)".format(len(FAILURES), PASSED + len(FAILURES)))
        return 1
    print("AS PRINTED CONTRACT PASSED ({} checks)".format(PASSED))
    return 0


if __name__ == "__main__":
    sys.exit(main())
