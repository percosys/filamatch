"""The design check before sending, with rules from ``data/filament_rules.json``.

Problems the slicer shows late or not at all:

* filaments that cannot share one bed, by Bambu Studio's own temperature
  groups (a high-temperature filament such as ABS with a low-temperature one
  such as PLA or PETG);
* an abrasive filament (carbon or glass fibre) on a nozzle softer than
  hardened steel;
* bodies too thin for the nozzle (see :func:`detail_width_mm`).

This module gives the facts (:func:`filament_traits`, :func:`nozzle_hardness`,
:func:`body_detail_width_mm`); the palette compares them with the printer it
sends to.  No network dependencies; ``python3 -m Filamatch.lib.design_check``
runs the self-test.
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .. import config
from . import printer_model

#: A face is a side face when its normal is less than 45 degrees from horizontal.
SIDE_NORMAL_Z = 0.7071

#: Bodies with more faces are not measured (slow, and seldom thin everywhere).
MAX_MEASURED_FACES = 2000

_ABRASIVE = re.compile(r"(?<![A-Z])(?:CF|GF)\d*(?![A-Z])|CARBON|GLASS ?FIB")


@lru_cache(maxsize=4)
def load(path: Optional[str] = None) -> Dict[str, Any]:
    """Return the filament rules (cached per path)."""
    with open(path or str(config.FILAMENT_RULES_FILE), "r", encoding="utf-8") as handle:
        return json.load(handle)


def _known_types() -> frozenset:
    rules = load()
    return frozenset(rules["high_temp"]) | frozenset(rules["low_temp"]) | frozenset(
        rules["high_low_compatible"]) | frozenset(rules["bed_temperature"])


def type_key(filament_type: Any) -> str:
    """The rules' name for a filament type ("PLA" for "PLA Basic", "PA-CF" for "PAHT-CF"), or ""."""
    text = str(filament_type or "").strip().upper()
    if not text:
        return ""
    words = [word for word in re.split(r"[\s_/]+", text) if word]
    candidates = ["-".join(words), words[0]]
    if len(words) > 1 and words[1] in ("CF", "GF"):
        candidates.insert(1, words[0] + "-" + words[1])
    candidates.append(printer_model.material_family(text))
    candidates.extend(words[1:])
    aliases, known = load()["aliases"], _known_types()
    for candidate in candidates:
        candidate = aliases.get(candidate, candidate)
        if candidate in known:
            return candidate
    return ""


def is_abrasive(filament_type: Any, name: Any = "") -> bool:
    """True for carbon or glass fibre filled filament ("PLA-CF", "PAHT-CF", "ABS-GF")."""
    return bool(_ABRASIVE.search("{} {}".format(filament_type or "", name or "").upper()))


def filament_traits(filament_type: Any, name: Any = "") -> Dict[str, Any]:
    """What the design check needs to know about one filament.

    Returns ``{"type", "temp_group", "abrasive", "bed"}``: ``temp_group`` is
    "high", "low", "any" (fits with both) or "" (unknown); ``bed`` the
    recommended bed temperature range ``[low, high]`` in °C, or None.
    """
    rules = load()
    key = type_key(filament_type) or type_key(name)
    if key in rules["high_temp"]:
        group = "high"
    elif key in rules["low_temp"]:
        group = "low"
    elif key in rules["high_low_compatible"]:
        group = "any"
    else:
        group = ""
    beds = rules["bed_temperature"]
    bed = beds.get(key) or beds.get(printer_model.material_family(key))
    return {"type": key, "temp_group": group, "abrasive": is_abrasive(filament_type, name),
            "bed": list(bed) if bed else None}


def nozzle_hardness(material: Any) -> Optional[int]:
    """Rockwell hardness of a nozzle material ("hardened_steel" -> 55), or None when unknown."""
    value = load()["nozzle_hardness"].get(str(material or "").strip().lower())
    return int(value) if value is not None else None


def detail_width_mm(volume_cm3: Any, faces: Iterable[Tuple[float, float]]) -> Optional[float]:
    """How wide a body is, about, in mm: twice its volume over its side area.

    ``faces`` are ``(area in cm², z part of the face normal)``; a face is a
    side face when its normal is near horizontal.  A strip of width w gives w
    and a round pin of diameter d gives d/2, so the result is small only for
    bodies that are thin everywhere: raised text, inlays, thin walls.  A thin
    layer on top of a part (a colour inlay 0.2 mm high) is not thin here: its
    height is only layers.  None when the body has no side faces.
    """
    try:
        volume = float(volume_cm3)
    except (TypeError, ValueError):
        return None
    side = sum(float(area) for area, normal_z in faces if abs(float(normal_z)) < SIDE_NORMAL_Z)
    if volume <= 0 or side <= 0:
        return None
    return round(2.0 * volume / side * 10.0, 2)


def body_detail_width_mm(body: Any, max_faces: int = MAX_MEASURED_FACES) -> Optional[float]:
    """:func:`detail_width_mm` of a Fusion body (Fusion units are cm); None when not measured."""
    try:
        faces = body.faces
        count = faces.count
        if not count or count > max_faces:
            return None
        measured: List[Tuple[float, float]] = []
        for index in range(count):
            face = faces.item(index)
            found, normal = face.evaluator.getNormalAtPoint(face.pointOnFace)
            if found and normal is not None:
                measured.append((face.area, normal.z))
        return detail_width_mm(body.volume, measured)
    except Exception:  # A body Fusion cannot measure is not checked.
        return None


def thinnest_by_material(placed_bodies: Iterable[Tuple[str, Any]]) -> Dict[str, float]:
    """The narrowest measured body of each material (``material_id`` -> mm).

    ``placed_bodies`` as materials_workflow.iter_placed_bodies gives them.
    """
    thinnest: Dict[str, float] = {}
    for material_id, body in placed_bodies:
        width = body_detail_width_mm(body)
        if width is not None and (material_id not in thinnest or width < thinnest[material_id]):
            thinnest[material_id] = width
    return thinnest


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _check_rules() -> None:
    rules = load()
    groups = [set(rules["high_temp"]), set(rules["low_temp"]), set(rules["high_low_compatible"])]
    _expect(not (groups[0] & groups[1]) and not (groups[0] & groups[2]) and not (groups[1] & groups[2]),
            "a filament type is in one temperature group only")
    _expect(all(target in _known_types() for target in rules["aliases"].values()), "aliases name known types")
    _expect(all(low < high for low, high in rules["bed_temperature"].values()), "bed ranges go up")


def _check_types() -> None:
    cases = {
        "PLA": "PLA", "PLA Basic": "PLA", "PETG HF": "PETG", "PETG-HF": "PETG", "PLA CF": "PLA-CF",
        "PLA-CF": "PLA-CF", "PAHT-CF": "PA-CF", "PA6-GF": "PA-GF", "ABS-GF": "ABS-GF", "PolyLite ABS": "ABS",
        "TPU 95A HF": "TPU", "ABS+": "ABS", "PPS GF": "PPS", "Wood": "", "": "",
    }
    found = {text: type_key(text) for text in cases}
    _expect(found == cases, "type names: {}".format({k: v for k, v in found.items() if v != cases[k]}))
    abs_gf = filament_traits("ABS-GF")
    _expect(abs_gf == {"type": "ABS-GF", "temp_group": "high", "abrasive": True, "bed": [90, 110]},
            "ABS-GF: hot, abrasive, the ABS bed: {}".format(abs_gf))
    _expect(filament_traits("PETG HF")["temp_group"] == "low" and filament_traits("HIPS")["temp_group"] == "any"
            and filament_traits("Wood")["temp_group"] == "", "PETG is low, HIPS fits both, unknown is empty")
    _expect(filament_traits("", "Bambu Lab - PAHT-CF - Black")["type"] == "PA-CF", "the name helps without a type")
    _expect([is_abrasive(text) for text in ("PLA-CF", "PETG CF", "PA6-GF30", "Carbon Fiber PLA", "PLA Basic",
                                            "PETG HF", "Gulf Blue")] == [True, True, True, True, False, False, False],
            "abrasive: carbon and glass fibre only")
    _expect(nozzle_hardness("hardened_steel") == 55 and nozzle_hardness("Stainless_Steel") == 20
            and nozzle_hardness("") is None, "nozzle hardness from Bambu Studio's table")


def _check_detail_width() -> None:
    # A strip 10 mm long, 0.3 mm wide, 1 mm high (cm units): its long sides make the side area.
    strip = detail_width_mm(1.0 * 0.03 * 0.1, [(1.0 * 0.1, 0.0), (1.0 * 0.1, 0.0), (0.03 * 0.1, 0.0),
                                               (0.03 * 0.1, 0.0), (1.0 * 0.03, 1.0), (1.0 * 0.03, -1.0)])
    _expect(strip is not None and 0.28 <= strip <= 0.31, "a 0.3 mm strip is about 0.3 mm wide: {}".format(strip))
    # A colour inlay 20 mm square and 0.2 mm high: thin in height only.
    inlay = detail_width_mm(2.0 * 2.0 * 0.02, [(2.0 * 0.02, 0.0)] * 4 + [(4.0, 1.0), (4.0, -1.0)])
    _expect(inlay is not None and inlay > 5, "a thin layer is not a thin detail: {}".format(inlay))
    _expect(detail_width_mm(1.0, [(1.0, 1.0)]) is None and detail_width_mm("n/a", []) is None,
            "no side faces or no volume: not measured")

    class Vector:
        def __init__(self, z: float) -> None:
            self.z = z

    class Evaluator:
        def __init__(self, z: float) -> None:
            self.z = z

        def getNormalAtPoint(self, point: Any) -> Tuple[bool, Vector]:
            return True, Vector(self.z)

    class Face:
        def __init__(self, area: float, z: float) -> None:
            self.area, self.evaluator, self.pointOnFace = area, Evaluator(z), None

    class Faces(list):
        @property
        def count(self) -> int:
            return len(self)

        def item(self, index: int) -> Face:
            return self[index]

    class Body:
        volume = 1.0 * 0.03 * 0.1
        faces = Faces([Face(0.1, 0.0), Face(0.1, 0.0), Face(0.03, 1.0), Face(0.03, -1.0)])

    _expect(body_detail_width_mm(Body()) == 0.3, "a Fusion body is measured in mm")
    _expect(thinnest_by_material([("a", Body()), ("b", object()), ("a", object())]) == {"a": 0.3},
            "each material keeps its narrowest measured body")
    _expect(body_detail_width_mm(Body(), max_faces=3) is None and body_detail_width_mm(object()) is None,
            "too many faces or no faces: not measured")


def _selftest() -> int:
    scenarios = (
        ("the rules file is consistent", _check_rules),
        ("filament types, groups and abrasive fillers", _check_types),
        ("thin details are measured across, not in height", _check_detail_width),
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
        print("{} design_check self-test(s) FAILED".format(failures))
        return 1
    print("PASS: all design_check self-tests completed")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
