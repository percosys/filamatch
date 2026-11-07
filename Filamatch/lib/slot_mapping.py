"""Automatic matching of Fusion materials to Bambuddy AMS slots.

The palette shows one row per detected Fusion material and lets the user map
each material to a live Bambuddy slot.  This module supplies the automatic
first guess (material type first, then colour, then brand) and applies the
manual overrides.  The user always wins: overrides are layered on top of
the automatic proposal.

No Fusion or network dependencies; includes a self-test.
"""

from __future__ import annotations

import logging
import sys
import traceback
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import printer_model

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: Highest possible score, used by the UI only for sorting.
MAX_SCORE = 250

#: Minimum score for an automatic match; below this the material stays unmapped.
MIN_AUTO_SCORE = 130


class SlotMappingError(Exception):
    """Raised when a requested material -> slot mapping is invalid."""


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read an attribute or mapping key."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def color_distance(first: str, second: str) -> int:
    """Return the Euclidean RGB distance between two ``#RRGGBB`` colours."""
    def rgb(value: Any) -> Tuple[int, int, int]:
        text = printer_model.normalize_color_hex(value)
        return int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16)

    red_a, green_a, blue_a = rgb(first)
    red_b, green_b, blue_b = rgb(second)
    return int(((red_a - red_b) ** 2 + (green_a - green_b) ** 2 + (blue_a - blue_b) ** 2) ** 0.5)


def score_slot(material: Any, slot: Any) -> int:
    """Score one material/slot pair; negative means unusable (empty slot)."""
    spool = _field(slot, "spool")
    if spool is None:
        return -1
    material_type = str(_field(material, "filament_type", "") or "").strip().upper()
    slot_type = str(_field(slot, "filament_type", "") or _field(spool, "material", "")).strip().upper()
    material_family = printer_model.material_family(material_type)
    slot_family = printer_model.material_family(slot_type)
    score = 0
    if material_type and slot_type:
        if material_type == slot_type:
            score += 120
        elif material_family and material_family == slot_family:
            score += 80
        else:
            score -= 50
    material_color = str(_field(material, "color_hex", "#808080") or "#808080")
    slot_color = str(_field(slot, "color_hex", "") or _field(spool, "color_hex", ""))
    distance = color_distance(material_color, slot_color)
    if distance == 0:
        score += 60
    elif distance <= 12:
        score += 50
    elif distance <= 40:
        score += 25
    elif distance <= 90:
        score += 5
    else:
        score -= min(30, distance // 15)
    manufacturer = str(_field(material, "manufacturer", "") or "").strip().lower()
    brand = str(_field(spool, "brand", "") or "").strip().lower()
    if manufacturer and brand and (manufacturer in brand or brand in manufacturer):
        score += 15
    color_name = str(_field(material, "color_name", "") or "").strip().lower()
    spool_color_name = str(_field(spool, "color_name", "") or "").strip().lower()
    if color_name and spool_color_name and color_name == spool_color_name:
        score += 8
    remaining = _field(spool, "remaining_percent")
    if isinstance(remaining, (int, float)) and remaining > 0:
        score += 1
    return max(-1, min(MAX_SCORE, score))


def auto_match(
    materials: Sequence[Any], slots: Sequence[Any]
) -> Dict[str, Dict[str, Any]]:
    """Greedily match each material to a unique loaded slot.

    Materials are considered in input order; every material/slot pair is
    scored and the best available slot is taken.  Empty slots and materials
    with no matching slot are left out, so the palette can show "unmapped".
    """
    available = [slot for slot in slots if _field(slot, "spool") is not None]
    candidates: List[Tuple[int, int, Any, Any]] = []
    for material_index, material in enumerate(materials):
        for slot in available:
            value = score_slot(material, slot)
            if value >= MIN_AUTO_SCORE:
                candidates.append((value, -material_index, material, slot))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    matches: Dict[str, Dict[str, Any]] = {}
    used_slots: set = set()
    for value, _negative_index, material, slot in candidates:
        material_id = str(_field(material, "material_id", ""))
        slot_key = str(_field(slot, "key", ""))
        if not material_id or material_id in matches or slot_key in used_slots:
            continue
        matches[material_id] = {
            "slot_key": slot_key,
            "slot_label": str(_field(slot, "slot_label", "")),
            "slot_display": str(_field(slot, "display_name", "")),
            "score": int(value),
            "auto": True,
        }
        used_slots.add(slot_key)
    return matches


def fallback_match(
    materials: Sequence[Any],
    slots: Sequence[Any],
    matched_material_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Best-effort same-material-family proposal for otherwise unmapped rows.

    This is intentionally lower confidence than :func:`auto_match`: it only
    runs for materials that had no good type+colour match and it never reuses a
    slot.  The palette labels these rows **review** so the user confirms the
    colour/spool before exporting.
    """
    used: set = set()
    matched = set(str(value) for value in (matched_material_ids or []))
    proposals: Dict[str, Dict[str, Any]] = {}
    available = [slot for slot in slots if _field(slot, "spool") is not None]
    for material in materials:
        material_id = str(_field(material, "material_id", ""))
        if not material_id or material_id in matched:
            continue
        family = printer_model.material_family(_field(material, "filament_type", ""))
        if not family:
            continue
        best_value = 0
        best_slot = None
        for slot in available:
            slot_key = str(_field(slot, "key", ""))
            if slot_key in used:
                continue
            slot_family = printer_model.material_family(
                str(_field(slot, "filament_type", "") or _field(_field(slot, "spool"), "material", ""))
            )
            if slot_family != family:
                continue
            value = score_slot(material, slot)
            if value > best_value:
                best_value = value
                best_slot = slot
        if best_slot is None:
            continue
        key = str(_field(best_slot, "key", ""))
        proposals[material_id] = {
            "slot_key": key,
            "slot_label": str(_field(best_slot, "slot_label", "")),
            "slot_display": str(_field(best_slot, "display_name", "")),
            "score": int(best_value),
            "auto": True,
            "confidence": "review",
            "review_reason": "No close colour match; this is the best same-material slot.",
        }
        used.add(key)
    return proposals


def _slot_index(slots: Sequence[Any]) -> Dict[str, Any]:
    index: Dict[str, Any] = {}
    for slot in slots:
        key = str(_field(slot, "key", ""))
        if key:
            index[key] = slot
    return index


def build_mapping(
    materials: Sequence[Any],
    slots: Sequence[Any],
    overrides: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Return one mapping row per material, combining auto-match and overrides.

    Override values are slot keys; an empty/``"none"`` value explicitly leaves a
    material unmapped.  Rows include the auto proposal so the UI can show
    whether the current selection was automatic or manual.
    """
    automatic = auto_match(materials, slots)
    fallback = fallback_match(materials, slots, automatic.keys())
    index = _slot_index(slots)
    override_map = dict(overrides or {})
    rows: List[Dict[str, Any]] = []
    for material in materials:
        material_id = str(_field(material, "material_id", ""))
        row: Dict[str, Any] = {
            "material_id": material_id,
            "material_name": str(_field(material, "material_name", "")),
            "display_name": str(_field(material, "display_name", "")),
            "color_hex": str(_field(material, "color_hex", "")),
            "filament_type": str(_field(material, "filament_type", "")),
            "slot_key": "",
            "slot_label": "",
            "slot_display": "",
            "score": 0,
            "auto": False,
            "overridden": False,
        }
        proposal = automatic.get(material_id)
        if proposal is not None:
            row.update(proposal)
            row["confidence"] = "auto"
        elif material_id in fallback:
            row.update(fallback[material_id])
        if material_id in override_map:
            raw = override_map.get(material_id)
            key = str(raw or "").strip()
            row["overridden"] = True
            # A manual choice replaces the automatic proposal entirely, so its
            # review hint no longer applies.
            row.pop("review_reason", None)
            if not key or key.lower() == "none":
                row.update(
                    {
                        "slot_key": "",
                        "slot_label": "",
                        "slot_display": "",
                        "score": 0,
                        "auto": False,
                        "confidence": "unmapped",
                    }
                )
            else:
                slot = index.get(key)
                if slot is None:
                    raise SlotMappingError(
                        "Material {!r} maps to unknown Bambuddy slot {!r}.".format(
                            row["display_name"] or material_id, key
                        )
                    )
                row.update(
                    {
                        "slot_key": key,
                        "slot_label": str(_field(slot, "slot_label", "")),
                        "slot_display": str(_field(slot, "display_name", "")),
                        "score": score_slot(material, slot),
                        "auto": False,
                        "confidence": "manual",
                    }
                )
        if not row.get("slot_key") and not row.get("confidence"):
            row["confidence"] = "unmapped"
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeMaterial:
    material_id: str
    display_name: str
    color_hex: str
    filament_type: str
    manufacturer: str = ""
    color_name: str = ""


def _fixture():
    spool_abs = printer_model.SpoolInfo(
        8, "ABS-GF", "Bambu Lab", "", "#F48438", 480.0, 1000.0, 520.0,
        "GFB50", "Bambu ABS-GF",
    )
    spool_pla = printer_model.SpoolInfo(
        9, "PLA", "Bambu Lab", "Charcoal", "#000000", 210.0, 1000.0, 790.0,
        "GFA01", "Bambu PLA Matte", subtype="Matte",
    )
    slots = [
        printer_model.SlotInfo(2, "Studio P1S", 0, 1, spool_abs, "ABS-GF", "#F48438"),
        printer_model.SlotInfo(2, "Studio P1S", 0, 2, spool_pla, "PLA", "#000000"),
        printer_model.SlotInfo(2, "Studio P1S", 0, 3, None, "PLA", "#FFFFFF"),
    ]
    materials = [
        _FakeMaterial("pla_black", "Bambu Lab - PLA Matte - Charcoal", "#000000", "PLA", "Bambu Lab", "Charcoal"),
        _FakeMaterial("abs_orange", "Bambu Lab - ABS-GF - Orange", "#F48438", "ABS", "Bambu Lab", "Orange"),
        _FakeMaterial("petg_white", "Generic - PETG - White", "#FFFFFF", "PETG", "Generic", "White"),
    ]
    return materials, slots


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _check_auto_match() -> None:
    """Type and colour pick the exact slots; empty slots are never chosen."""
    materials, slots = _fixture()
    matches = auto_match(materials, slots)
    _expect(matches["pla_black"]["slot_key"] == "2:0:2", "PLA must match the black PLA slot")
    _expect(matches["abs_orange"]["slot_key"] == "2:0:1", "ABS must match the orange ABS slot")
    _expect("petg_white" not in matches, "unmatched material must stay unmapped")
    _expect(
        all(match["slot_key"] != "2:0:3" for match in matches.values()),
        "empty slot must never be auto-matched",
    )


def _check_fallback_review() -> None:
    """Unmatched materials get a review proposal from a same-family slot."""
    materials = [
        _FakeMaterial("steel_gray", "Steel", "#888888", "PLA", "Fusion", "Steel"),
    ]
    spool = printer_model.SpoolInfo(
        10, "PLA", "Bambu Lab", "Charcoal", "#000000", 1000.0, 1000.0, 0.0,
        "GFA01", "Bambu PLA Matte", subtype="Matte",
    )
    slots = [printer_model.SlotInfo(1, "Workshop H2D", 0, 1, spool, "PLA", "#000000")]
    rows = build_mapping(materials, slots, {})
    _expect(rows[0]["slot_key"] == "1:0:1", "fallback must propose a same-family slot")
    _expect(rows[0]["confidence"] == "review", "fallback must be labelled review")
    _expect("close colour" in rows[0].get("review_reason", ""), "review reason must explain the guess")


def _check_overrides() -> None:
    """Manual overrides replace auto proposals; 'none' clears them."""
    materials, slots = _fixture()
    rows = build_mapping(materials, slots, {"pla_black": "2:0:1"})
    by_id = {row["material_id"]: row for row in rows}
    _expect(by_id["pla_black"]["slot_key"] == "2:0:1", "manual override must win")
    _expect(by_id["pla_black"]["overridden"] is True, "override flag must be set")
    _expect(by_id["pla_black"]["auto"] is False, "overridden row must not be auto")
    _expect(by_id["abs_orange"]["slot_key"] == "2:0:1", "auto row must remain available")
    rows = build_mapping(materials, slots, {"pla_black": "none"})
    by_id = {row["material_id"]: row for row in rows}
    _expect(by_id["pla_black"]["slot_key"] == "", "none override must clear the slot")
    _expect(by_id["pla_black"]["auto"] is False, "'none' override must not stay auto")
    _expect(by_id["pla_black"]["confidence"] == "unmapped", "'none' override is unmapped")
    review_material = [_FakeMaterial("steel_gray", "Steel", "#888888", "PLA", "Fusion", "Steel")]
    review_rows = build_mapping(review_material, slots, {})
    _expect(review_rows[0].get("review_reason"), "fixture must start as a review proposal")
    cleared = build_mapping(review_material, slots, {"steel_gray": "none"})[0]
    _expect("review_reason" not in cleared, "'none' override must drop the stale review reason")
    _expect(cleared["auto"] is False, "'none' override must reset auto")
    try:
        build_mapping(materials, slots, {"pla_black": "9:9:9"})
    except SlotMappingError as exc:
        _expect("unknown" in str(exc), "unknown slot error must be actionable")
    else:
        raise AssertionError("unknown override slot must raise SlotMappingError")


def _check_colors() -> None:
    """Colour distance helper is symmetric and bounded."""
    _expect(color_distance("#000000", "#000000") == 0, "identical colours must be zero")
    _expect(color_distance("#000000", "#FFFFFF") > 400, "black/white distance mismatch")
    _expect(
        color_distance("#FF6600", "#FF6601") <= 2,
        "near-identical colours must be close",
    )


def _selftest() -> int:
    scenarios = (
        ("auto-match uses type, colour and unique slots", _check_auto_match),
        ("unmatched materials get a review proposal", _check_fallback_review),
        ("manual overrides replace auto proposals and validate", _check_overrides),
        ("colour distance helper is sane", _check_colors),
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
        print("{} slot-mapping self-test(s) FAILED".format(failures))
        return 1
    print("PASS: all slot-mapping self-tests completed")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
