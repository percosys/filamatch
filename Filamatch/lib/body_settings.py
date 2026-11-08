"""Print settings for single bodies, kept on the bodies in Fusion.

A body can have its own infill (density and type) and its own walls, top
and bottom layers.
The settings live in a Fusion attribute on the body (group ``Filamatch``,
name ``print_settings``, JSON), so they stay with the design.  In the 3MF they
are the Bambu Studio / OrcaSlicer per-object (or per-part) keys of
``Metadata/model_settings.config`` (:func:`to_bambu`); prusa_project turns
them into PrusaSlicer's per-part keys (:func:`to_prusa`).  An empty setting
means "as the print process says".  Supports stay with the slicer: they
depend on how the part sits on the bed, and the slicers paint them.

No Fusion or network dependencies outside :func:`read_body` and
:func:`write_body`; ``python3 -m Filamatch.lib.body_settings`` runs the
self-test.
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from typing import Any, Dict, Iterable, List, Mapping, Tuple

ATTRIBUTE_GROUP = "Filamatch"
ATTRIBUTE_NAME = "print_settings"

#: Filamatch key -> (Bambu Studio / OrcaSlicer key, PrusaSlicer key), in display order.
FIELDS: Dict[str, Tuple[str, str]] = {
    "infill": ("sparse_infill_density", "fill_density"),
    "infill_pattern": ("sparse_infill_pattern", "fill_pattern"),
    "walls": ("wall_loops", "perimeters"),
    "top_layers": ("top_shell_layers", "top_solid_layers"),
    "bottom_layers": ("bottom_shell_layers", "bottom_solid_layers"),
}

#: Allowed range of each number setting.
LIMITS: Dict[str, Tuple[int, int]] = {"infill": (0, 100), "walls": (0, 20), "top_layers": (0, 50), "bottom_layers": (0, 50)}

#: Infill types the slicers may know, by their own names, with the palette's
#: label.  Which ones a slicer accepts is asked from it (slicer_profiles.infill_patterns).
INFILL_PATTERNS: Dict[str, str] = {
    "zig-zag": "Zig-zag", "rectilinear": "Rectilinear", "alignedrectilinear": "Aligned rectilinear",
    "grid": "Grid", "line": "Line", "cubic": "Cubic", "adaptivecubic": "Adaptive cubic",
    "quartercubic": "Quarter cubic", "supportcubic": "Support cubic", "triangles": "Triangles",
    "tri-hexagon": "Tri-hexagon", "stars": "Stars", "gyroid": "Gyroid", "honeycomb": "Honeycomb",
    "3dhoneycomb": "3D honeycomb", "lateral-honeycomb": "Lateral honeycomb", "lateral-lattice": "Lateral lattice",
    "tpmsd": "TPMS-D", "tpmsfk": "TPMS-FK", "crosshatch": "Cross hatch", "crosszag": "Cross zag",
    "lockedzag": "Locked zag", "zigzag": "Zigzag", "lightning": "Lightning", "concentric": "Concentric",
    "hilbertcurve": "Hilbert curve", "archimedeanchords": "Archimedean chords", "octagramspiral": "Octagram spiral",
}

#: The same infill under another slicer's name: Bambu Studio's "zig-zag" is
#: OrcaSlicer's and PrusaSlicer's "rectilinear"; Bambu's "tri-hexagon" is
#: PrusaSlicer's "stars".
PATTERN_ALIASES: Dict[str, Dict[str, str]] = {
    "bambu": {"rectilinear": "zig-zag", "stars": "tri-hexagon"},
    "orca": {"zig-zag": "rectilinear", "stars": "tri-hexagon"},
    "prusa": {"zig-zag": "rectilinear", "tri-hexagon": "stars"},
}


#: Other values of a print process the As printed estimate uses:
#: key -> (Bambu Studio / OrcaSlicer key, PrusaSlicer key).
PROCESS_FIELDS: Dict[str, Tuple[str, str]] = {
    "layer_height": ("layer_height", "layer_height"),
    "outer_wall_width": ("outer_wall_line_width", "external_perimeter_extrusion_width"),
    "inner_wall_width": ("inner_wall_line_width", "perimeter_extrusion_width"),
    "top_min_mm": ("top_shell_thickness", "top_solid_min_thickness"),
    "bottom_min_mm": ("bottom_shell_thickness", "bottom_solid_min_thickness"),
}


def _process_number(value: Any) -> Any:
    """A number, or the slicer's text for a share of the nozzle ("105%"); None when unreadable."""
    if isinstance(value, list):
        value = value[0] if value else None
    text = str(value if value is not None else "").strip()
    try:
        if text.endswith("%"):
            float(text[:-1])
            return text
        return float(text)
    except ValueError:
        return None


def process_values(config: Mapping[str, Any], slicer_kind: str) -> Dict[str, Any]:
    """A print process's own values: the body settings keys plus :data:`PROCESS_FIELDS`.

    ``config`` is a flattened process preset; ``slicer_kind`` "prusa" reads
    PrusaSlicer's keys, anything else Bambu Studio's / OrcaSlicer's.
    """
    prusa = slicer_kind == "prusa"
    values = from_prusa(config) if prusa else from_bambu(config)
    for key, names in PROCESS_FIELDS.items():
        number = _process_number(config.get(names[1] if prusa else names[0]))
        if number is not None:
            values[key] = number
    return values


def pattern_for(slicer_kind: str, pattern: str) -> str:
    """The name a slicer ("bambu", "orca", "prusa") uses for an infill type."""
    return PATTERN_ALIASES.get(slicer_kind, {}).get(pattern, pattern)


def pattern_label(pattern: str) -> str:
    """The palette's name of an infill type ("3dhoneycomb" -> "3D honeycomb")."""
    return INFILL_PATTERNS.get(pattern) or pattern.replace("-", " ").capitalize()


def normalize(raw: Any) -> Dict[str, Any]:
    """Keep the known settings with valid values: whole numbers in range, an
    infill type as a slicer name ("gyroid").

    Empty or missing values are left out (the print process decides).

    Raises:
        ValueError: For a value that is not a number, or is out of range.
    """
    settings: Dict[str, Any] = {}
    if not isinstance(raw, Mapping):
        return settings
    for key in FIELDS:
        value = raw.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if key == "infill_pattern":
            text = str(value).strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", text):
                raise ValueError("infill type {!r} is not a slicer name".format(value))
            settings[key] = text
            continue
        try:
            number = float(str(value).strip().rstrip("%"))
        except ValueError:
            raise ValueError("{} must be a number, not {!r}".format(key.replace("_", " "), value)) from None
        low, high = LIMITS[key]
        if number != int(number) or not low <= number <= high:
            raise ValueError("{} must be a whole number from {} to {}".format(key.replace("_", " "), low, high))
        settings[key] = int(number)
    return settings


def _slicer_value(key: str, value: Any) -> str:
    return "{}%".format(value) if key == "infill" else str(value)


def to_bambu(settings: Mapping[str, Any], slicer_kind: str = "") -> Dict[str, str]:
    """The Bambu Studio / OrcaSlicer model_settings keys (``sparse_infill_density`` "30%"),
    with the infill type under ``slicer_kind``'s name ("bambu" or "orca")."""
    found = normalize(settings)
    if "infill_pattern" in found:
        found["infill_pattern"] = pattern_for(slicer_kind, found["infill_pattern"])
    return {FIELDS[key][0]: _slicer_value(key, value) for key, value in found.items()}


def _readable(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """normalize() key by key: a value that does not fit is left out, not an error."""
    settings: Dict[str, Any] = {}
    for key, value in raw.items():
        try:
            settings.update(normalize({key: value}))
        except ValueError:
            continue
    return settings


def from_bambu(metadata: Mapping[str, Any]) -> Dict[str, Any]:
    """Read settings back from Bambu Studio / OrcaSlicer keys; other keys are ignored."""
    return _readable({key: metadata.get(bambu) for key, (bambu, _prusa) in FIELDS.items() if bambu in metadata})


def bambu_keys() -> Tuple[str, ...]:
    """The model_settings keys this module writes."""
    return tuple(bambu for bambu, _prusa in FIELDS.values())


def to_prusa(settings: Mapping[str, Any]) -> Dict[str, str]:
    """PrusaSlicer's per-part (volume) keys (``fill_density`` "30%", ``fill_pattern`` "rectilinear")."""
    found = normalize(settings)
    if "infill_pattern" in found:
        found["infill_pattern"] = pattern_for("prusa", found["infill_pattern"])
    return {FIELDS[key][1]: _slicer_value(key, value) for key, value in found.items()}


def from_prusa(config: Mapping[str, Any]) -> Dict[str, Any]:
    """Read settings from PrusaSlicer keys (a print preset); other keys are ignored."""
    return _readable({key: config.get(prusa) for key, (_bambu, prusa) in FIELDS.items() if prusa in config})


def _in_sentence(label: str) -> str:
    """"Gyroid" -> "gyroid"; "3D honeycomb" and "TPMS-D" stay as they are."""
    if len(label) > 1 and (label[0].isdigit() or label[1].isupper()):
        return label
    return label[:1].lower() + label[1:]


def describe(settings: Mapping[str, Any]) -> str:
    """Short text: "30% gyroid infill · 4 walls"."""
    found = normalize(settings)
    parts: List[str] = []
    if "infill" in found or "infill_pattern" in found:
        parts.append(" ".join(word for word in (
            "{}%".format(found["infill"]) if "infill" in found else "",
            _in_sentence(pattern_label(found["infill_pattern"])) if "infill_pattern" in found else "",
            "infill") if word))
    words = {"walls": "{} walls", "top_layers": "{} top layers", "bottom_layers": "{} bottom layers"}
    parts.extend(words[key].format(found[key]) for key in words if key in found)
    return " · ".join(parts)


def common(all_settings: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """The settings every one of several bodies has the same; the others are left out."""
    rows = [normalize(settings) for settings in all_settings]
    if not rows:
        return {}
    return {key: value for key, value in rows[0].items() if all(row.get(key) == value for row in rows[1:])}


def _native(body: Any) -> Any:
    try:
        return body.nativeObject or body
    except Exception:
        return body


def stored_text(body: Any) -> str:
    """The attribute's text as stored ("" when none): a cheap change marker."""
    try:
        attribute = _native(body).attributes.itemByName(ATTRIBUTE_GROUP, ATTRIBUTE_NAME)
        return str(attribute.value) if attribute is not None else ""
    except Exception:
        return ""


def read_body(body: Any) -> Dict[str, Any]:
    """The settings stored on a Fusion body (empty when none or unreadable)."""
    try:
        attribute = _native(body).attributes.itemByName(ATTRIBUTE_GROUP, ATTRIBUTE_NAME)
        return normalize(json.loads(attribute.value)) if attribute is not None else {}
    except Exception:
        return {}


def write_body(body: Any, settings: Mapping[str, Any]) -> None:
    """Store settings on a Fusion body; empty settings remove the attribute."""
    attributes = _native(body).attributes
    clean = normalize(settings)
    existing = attributes.itemByName(ATTRIBUTE_GROUP, ATTRIBUTE_NAME)
    if not clean:
        if existing is not None:
            existing.deleteMe()
        return
    attributes.add(ATTRIBUTE_GROUP, ATTRIBUTE_NAME, json.dumps(clean, sort_keys=True))


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _check_values() -> None:
    _expect(normalize({"infill": "30%", "walls": 4, "top_layers": "", "supports": "on", "colour": "red"})
            == {"infill": 30, "walls": 4}, "numbers and percent are read; empty and unknown keys are left out")
    for bad in ({"infill": 120}, {"walls": 2.5}, {"walls": "many"}):
        try:
            normalize(bad)
        except ValueError:
            continue
        raise AssertionError("{} must be refused".format(bad))
    settings = {"infill": 30, "walls": 4, "top_layers": 6, "bottom_layers": 5}
    _expect(to_bambu(settings) == {"sparse_infill_density": "30%", "wall_loops": "4", "top_shell_layers": "6",
                                   "bottom_shell_layers": "5"}, "Bambu keys")
    _expect(from_bambu(dict(to_bambu(settings), extruder="2", enable_support="1")) == settings,
            "Bambu keys read back")
    _expect(to_prusa(settings) == {"fill_density": "30%", "perimeters": "4", "top_solid_layers": "6",
                                   "bottom_solid_layers": "5"}, "PrusaSlicer keys")
    _expect(from_prusa({"fill_density": "15%", "perimeters": "2", "support_material": "1"})
            == {"infill": 15, "walls": 2}, "PrusaSlicer print values read back")
    _expect(describe(settings) == "30% infill · 4 walls · 6 top layers · 5 bottom layers",
            "description: {}".format(describe(settings)))
    _expect(common([{"infill": 30, "walls": 4}, {"infill": 30, "walls": 2}]) == {"infill": 30} and common([]) == {},
            "only what all bodies share")
    gyroid = {"infill": 20, "infill_pattern": "Gyroid"}
    _expect(normalize(gyroid) == {"infill": 20, "infill_pattern": "gyroid"}
            and describe(gyroid) == "20% gyroid infill" and describe({"infill_pattern": "3dhoneycomb"}) == "3D honeycomb infill",
            "an infill type is a slicer name: {}".format(describe(gyroid)))
    try:
        normalize({"infill_pattern": "<gyroid>"})
    except ValueError:
        pass
    else:
        raise AssertionError("an infill type must be a plain slicer name")
    zig = {"infill_pattern": "zig-zag"}
    _expect(to_bambu(zig, "bambu") == {"sparse_infill_pattern": "zig-zag"}
            and to_bambu(zig, "orca") == {"sparse_infill_pattern": "rectilinear"}
            and to_prusa(zig) == {"fill_pattern": "rectilinear"}
            and to_bambu({"infill_pattern": "rectilinear"}, "bambu") == {"sparse_infill_pattern": "zig-zag"}
            and to_prusa({"infill_pattern": "tri-hexagon"}) == {"fill_pattern": "stars"},
            "the same infill goes under each slicer's own name")
    _expect(from_bambu({"sparse_infill_density": "15%", "sparse_infill_pattern": "crosshatch", "wall_loops": "two"})
            == {"infill": 15, "infill_pattern": "crosshatch"}, "a value that does not fit is left out, not an error")
    bambu = process_values({"sparse_infill_density": "15%", "layer_height": "0.2", "outer_wall_line_width": "0.42",
                            "inner_wall_line_width": "105%", "top_shell_thickness": "1", "bottom_shell_thickness": "0"},
                           "bambu")
    prusa = process_values({"fill_density": "20%", "layer_height": "0.15", "external_perimeter_extrusion_width": "0.45",
                            "perimeter_extrusion_width": "auto", "top_solid_min_thickness": "0.7"}, "prusa")
    _expect(bambu == {"infill": 15, "layer_height": 0.2, "outer_wall_width": 0.42, "inner_wall_width": "105%",
                      "top_min_mm": 1.0, "bottom_min_mm": 0.0}
            and prusa == {"infill": 20, "layer_height": 0.15, "outer_wall_width": 0.45, "top_min_mm": 0.7},
            "process values for the As printed estimate: {} {}".format(bambu, prusa))


def _check_attributes() -> None:
    class Attribute:
        def __init__(self, owner: "Attributes", value: str) -> None:
            self.owner, self.value = owner, value

        def deleteMe(self) -> bool:
            self.owner.items.pop((ATTRIBUTE_GROUP, ATTRIBUTE_NAME), None)
            return True

    class Attributes:
        def __init__(self) -> None:
            self.items: Dict[Tuple[str, str], Attribute] = {}

        def itemByName(self, group: str, name: str) -> Any:
            return self.items.get((group, name))

        def add(self, group: str, name: str, value: str) -> Attribute:
            self.items[(group, name)] = Attribute(self, value)
            return self.items[(group, name)]

    class Body:
        nativeObject = None

        def __init__(self) -> None:
            self.attributes = Attributes()

    class Proxy:
        def __init__(self, native: Body) -> None:
            self.nativeObject = native

    body = Body()
    _expect(read_body(body) == {} and stored_text(body) == "", "no attribute: no settings")
    write_body(Proxy(body), {"infill": 15, "walls": 3})
    _expect(read_body(body) == {"infill": 15, "walls": 3} and stored_text(body) == '{"infill": 15, "walls": 3}',
            "a proxy writes on its body")
    write_body(body, {})
    _expect(body.attributes.items == {} and read_body(object()) == {}, "empty settings remove the attribute")


def _selftest() -> int:
    scenarios = (
        ("values, slicer keys and descriptions", _check_values),
        ("settings live in a body attribute", _check_attributes),
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
        print("{} body_settings self-test(s) FAILED".format(failures))
        return 1
    print("PASS: all body_settings self-tests completed")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
