#!/usr/bin/env python3
"""Check the filament data sheet files and write their link table.

Usage (from the repo root):

    python3 scripts/datasheet_docs.py           # check, then rewrite the table in the README
    python3 scripts/datasheet_docs.py --check   # exit 1 on a bad record or an old table

The files are ``Filamatch/data/datasheets/<maker>.json``; their format is in
that folder's ``README.md``, and the table goes there between the
datasheet markers. ``tests/test_datasheet_docs.py`` runs the check.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
FOLDER = REPO_ROOT / "Filamatch" / "data" / "datasheets"
README = FOLDER / "README.md"
RULES = REPO_ROOT / "Filamatch" / "data" / "filament_rules.json"
START = "<!-- datasheets:start -->"
END = "<!-- datasheets:end -->"

PROPERTIES = (
    "density", "tensile_modulus", "tensile_strength", "yield_strength", "elongation_at_break",
    "flexural_modulus", "flexural_strength", "impact_strength", "impact_strength_notched",
    "heat_deflection_045", "heat_deflection_18", "vicat_softening", "shore_hardness_a",
    "shore_hardness_d", "water_absorption",
)
DIRECTIONS = ("XY", "Z", "XZ", "YZ", "")
CONDITIONS = ("dry", "conditioned")
FILAMENT_KEYS = {"product", "type", "product_url", "datasheet_url", "datasheet_version",
                 "specimen", "properties", "notes"}
OPTIONAL_KEYS = {"aliases"}
PROPERTY_KEYS = {"property", "direction", "value", "plus_minus", "unit", "standard", "condition"}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def known_types() -> set:
    """Every filament type name Filamatch's rules know."""
    rules = json.loads(RULES.read_text(encoding="utf-8"))
    names = set()
    for key in ("high_temp", "low_temp", "high_low_compatible", "aliases", "bed_temperature"):
        names.update(rules.get(key) or ())
    return names


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _url_ok(value: Any) -> bool:
    return value == "" or (isinstance(value, str) and value.startswith("https://"))


def check_file(path: Path, types: set) -> Tuple[List[str], List[str]]:
    """Return ``(errors, warnings)`` for one maker file."""
    errors: List[str] = []
    warnings: List[str] = []
    data = json.loads(path.read_text(encoding="utf-8"))
    name = path.name
    if data.get("schema") != 1:
        errors.append("{}: schema must be 1".format(name))
    if not isinstance(data.get("maker"), str) or not data.get("maker"):
        errors.append("{}: maker is missing".format(name))
    if not DATE.match(str(data.get("accessed", ""))):
        errors.append("{}: accessed must be YYYY-MM-DD".format(name))
    filaments = data.get("filaments")
    if not isinstance(filaments, list) or not filaments:
        return errors + ["{}: filaments must be a list with records".format(name)], warnings
    seen = set()
    for index, record in enumerate(filaments):
        where = "{} #{} {}".format(name, index + 1, record.get("product", "?"))
        extra = set(record) - FILAMENT_KEYS - OPTIONAL_KEYS
        missing = FILAMENT_KEYS - set(record)
        if extra:
            errors.append("{}: unknown keys {}".format(where, sorted(extra)))
        if missing:
            errors.append("{}: missing keys {}".format(where, sorted(missing)))
        product = record.get("product")
        if not isinstance(product, str) or not product:
            errors.append("{}: product is missing".format(where))
        elif product in seen:
            errors.append("{}: product is listed twice".format(where))
        seen.add(product)
        aliases = record.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(a, str) and a.strip() for a in aliases):
            errors.append("{}: aliases must be a list of names".format(where))
        else:
            for alias in aliases:
                if alias in seen:
                    errors.append("{}: alias {!r} is already a name".format(where, alias))
                seen.add(alias)
        if not isinstance(record.get("type"), str) or not record.get("type"):
            errors.append("{}: type is missing".format(where))
        elif record["type"] not in types:
            warnings.append("{}: type {!r} is not in filament_rules.json".format(where, record["type"]))
        for key in ("product_url", "datasheet_url"):
            if not _url_ok(record.get(key)):
                errors.append("{}: {} must be https or empty".format(where, key))
        if not isinstance(record.get("specimen", {}), dict):
            errors.append("{}: specimen must be an object".format(where))
        properties = record.get("properties")
        if not isinstance(properties, list):
            errors.append("{}: properties must be a list".format(where))
            continue
        if properties and not record.get("datasheet_url"):
            errors.append("{}: values without a data sheet link".format(where))
        keys = set()
        for entry in properties:
            label = "{}: {}".format(where, entry.get("property"))
            if set(entry) - PROPERTY_KEYS:
                errors.append("{} has unknown keys {}".format(label, sorted(set(entry) - PROPERTY_KEYS)))
            if entry.get("property") not in PROPERTIES:
                errors.append("{} is not a known property".format(label))
            if entry.get("direction", "") not in DIRECTIONS:
                errors.append("{} has direction {!r}".format(label, entry.get("direction")))
            if "condition" in entry and entry["condition"] not in CONDITIONS:
                errors.append("{} has condition {!r}".format(label, entry["condition"]))
            if not _is_number(entry.get("value")):
                errors.append("{} has no number value".format(label))
            if "plus_minus" in entry and not _is_number(entry["plus_minus"]):
                errors.append("{} has a plus_minus that is not a number".format(label))
            if not isinstance(entry.get("unit"), str) or not entry.get("unit"):
                errors.append("{} has no unit".format(label))
            key = (entry.get("property"), entry.get("direction", ""), entry.get("condition", ""))
            if key in keys:
                errors.append("{} is listed twice".format(" ".join([label] + [k for k in key[1:] if k])))
            keys.add(key)
    return errors, warnings


def _mpa(entry: Optional[Dict[str, Any]]) -> str:
    if entry is None:
        return "-"
    factor = {"MPa": 1.0, "GPa": 1000.0}.get(entry.get("unit", ""))
    if factor is None:
        return "-"
    text = "{:g}".format(round(entry["value"] * factor, 1))
    return text + (" (cond.)" if entry.get("condition") == "conditioned" else "")


def _pick(properties: Sequence[Dict[str, Any]], name: str, direction: str) -> Optional[Dict[str, Any]]:
    """The dry (or only) value; a conditioned value only when there is no other."""
    found = [p for p in properties if p.get("property") == name and p.get("direction", "") == direction]
    found.sort(key=lambda p: p.get("condition") == "conditioned")
    return found[0] if found else None


def _pair(properties: Sequence[Dict[str, Any]], name: str) -> str:
    xy = _pick(properties, name, "XY") or _pick(properties, name, "")
    return "{} / {}".format(_mpa(xy), _mpa(_pick(properties, name, "Z")))


def table(paths: Sequence[Path]) -> str:
    """The README table: one row per filament, maker by maker."""
    lines = [
        START,
        "",
        "Stiffness and strength are in MPa, along the layers / across the layers",
        "(XY / Z). \"-\" means the sheet gives no value. Generated by",
        "`python3 scripts/datasheet_docs.py`; do not edit by hand.",
        "",
    ]
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        filaments = data["filaments"]
        with_sheet = sum(1 for f in filaments if f.get("datasheet_url"))
        lines += [
            "### {} ({} filaments, {} with a data sheet; accessed {})".format(
                data["maker"], len(filaments), with_sheet, data["accessed"]),
            "",
            "| Filament | Type | Data sheet | Tensile modulus | Tensile strength |",
            "| --- | --- | --- | --- | --- |",
        ]
        for record in filaments:
            product = record["product"]
            if record.get("product_url"):
                product = "[{}]({})".format(product, record["product_url"])
            if record.get("datasheet_url"):
                sheet = "[{}]({})".format(record.get("datasheet_version") or "TDS", record["datasheet_url"])
            else:
                sheet = "none found"
            properties = record.get("properties") or []
            lines.append("| {} | {} | {} | {} | {} |".format(
                product, record["type"], sheet,
                _pair(properties, "tensile_modulus"), _pair(properties, "tensile_strength")))
        lines.append("")
    lines.append(END)
    return "\n".join(lines)


def replace_section(text: str, section: str) -> str:
    if START in text and END in text:
        head = text[:text.index(START)]
        tail = text[text.index(END) + len(END):]
        return head + section + tail
    return text.rstrip("\n") + "\n\n## Data sheets\n\n" + section + "\n"


def main(argv: Sequence[str]) -> int:
    paths = sorted(FOLDER.glob("*.json"))
    if not paths:
        print("No data sheet files in {}".format(FOLDER))
        return 1
    types = known_types()
    errors: List[str] = []
    for path in paths:
        file_errors, warnings = check_file(path, types)
        errors += file_errors
        for warning in warnings:
            print("warning: " + warning)
    for error in errors:
        print("error: " + error)
    if errors:
        print("{} error(s) in the data sheet files".format(len(errors)))
        return 1
    current = README.read_text(encoding="utf-8")
    wanted = replace_section(current, table(paths))
    if "--check" in argv:
        if current != wanted:
            print("The data sheet table is out of date: run python3 scripts/datasheet_docs.py")
            return 1
        print("Data sheet files are valid and the README table matches them")
        return 0
    if current != wanted:
        README.write_text(wanted, encoding="utf-8")
        print("Rewrote the data sheet table in {}".format(README))
    else:
        print("The data sheet table is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
