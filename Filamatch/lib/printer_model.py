"""Source-neutral printer, spool and filament-slot data for Filamatch.

Every printer slot source (Bambuddy today; direct printer connectors and a
manual source later, see ``docs/PRINTER_CONNECTIONS_PLAN.md``) returns these
types, so the palette, the slot matcher (:mod:`slot_mapping`) and the Fusion
material sync (:mod:`inventory_sync`) never depend on one source.

Field order of :class:`SpoolInfo` and :class:`SlotInfo` is part of the
contract: callers and fixtures build them positionally.  New fields are only
ever appended, with defaults.

This module imports cleanly outside Fusion 360 (standard library only) and
includes a self-test.
"""

from __future__ import annotations

import logging
import math
import re
import sys
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: Material type or family -> Fusion template material for spool-derived entries.
_TEMPLATE_BY_MATERIAL: Dict[str, str] = {
    "PLA": "ABS Plastic",
    "PLA-CF": "ABS Plastic",
    "PETG": "PET Plastic",
    "PETG-CF": "PET Plastic",
    "ABS": "ABS Plastic",
    "ASA": "ABS Plastic",
    "ABS-GF": "ABS Plastic",
    "ASA-CF": "ABS Plastic",
    "PC": "ABS Plastic",
    "PA": "Nylon Plastic",
    "PA-CF": "Nylon Plastic",
    "PAHT": "Nylon Plastic",
    "PAHT-CF": "Nylon Plastic",
    "PVA": "ABS Plastic",
    "HIPS": "ABS Plastic",
    "TPU": "ABS Plastic",
    "TPE": "ABS Plastic",
}

#: Density fallbacks (g/cm^3) keyed by exact material type, then family.
_DENSITY_BY_MATERIAL: Dict[str, float] = {
    "PLA": 1.24,
    "PLA-CF": 1.22,
    "PETG": 1.27,
    "PETG-CF": 1.25,
    "ABS": 1.04,
    "ASA": 1.05,
    "ABS-GF": 1.08,
    "ASA-CF": 1.08,
    "PC": 1.21,
    "PA": 1.14,
    "PA-CF": 1.15,
    "PAHT": 1.16,
    "PAHT-CF": 1.18,
    "PVA": 1.23,
    "HIPS": 1.04,
    "TPU": 1.21,
    "TPE": 1.15,
}

_HEX_RE = re.compile(r"^[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")

#: Bambu-style unit id of the external spool holder.
EXTERNAL_UNIT_ID = 255

#: The slicers' flow types by the second letter of a Bambu Lab nozzle type
#: code ("HH01": hardened steel, High Flow; "HS01": Standard).
NOZZLE_FLOWS = {"S": "Standard", "H": "High Flow"}

#: Nozzle materials by the first letter of a Bambu Lab nozzle type code, and
#: the names older printers report in ``nozzle_type``.
NOZZLE_MATERIALS = {"H": "hardened_steel", "S": "stainless_steel", "T": "tungsten_carbide"}
NOZZLE_MATERIAL_NAMES = ("hardened_steel", "stainless_steel", "tungsten_carbide", "brass")


def nozzle_flow(type_code: Any) -> str:
    """The flow type of a Bambu Lab nozzle type code ("HH01" -> "High Flow"), or ""."""
    text = str(type_code or "").strip().upper()
    if re.fullmatch(r"[A-Z]{2}\d{2}", text):
        return NOZZLE_FLOWS.get(text[1], "")
    return ""


def nozzle_material(value: Any) -> str:
    """The nozzle material of a type code ("HH01" -> "hardened_steel") or name, or ""."""
    text = str(value or "").strip()
    if text.lower() in NOZZLE_MATERIAL_NAMES:
        return text.lower()
    if re.fullmatch(r"[A-Z]{2}\d{2}", text.upper()):
        return NOZZLE_MATERIALS.get(text.upper()[0], "")
    return ""


@dataclass(frozen=True)
class NozzleInfo:
    """One nozzle installed on a printer."""

    #: The printer's own extruder id (Bambu Lab H2D: 0 is the right nozzle, 1 the left).
    id: int
    diameter: float
    #: The slicers' flow type ("Standard", "High Flow"); "" when the printer does not say.
    flow: str = ""
    #: "hardened_steel", "stainless_steel", ...; "" when the printer does not say.
    material: str = ""


@dataclass(frozen=True)
class PrinterInfo:
    """One printer known to a slot source."""

    id: int
    name: str
    model: str = ""
    serial_number: str = ""
    ip_address: str = ""
    location: str = ""
    is_active: bool = True
    brand: str = ""
    #: AMS auto refill (switch to a spool of the same filament when one runs
    #: out): True/False when the printer reports it, else None.
    auto_refill: Optional[bool] = None
    #: The installed nozzles, by extruder id; empty when the printer does not say.
    nozzles: Tuple[NozzleInfo, ...] = ()

    @property
    def label(self) -> str:
        """Readable printer label for the palette."""
        parts = [self.name or "Printer {}".format(self.id)]
        if self.model:
            parts.append(self.model)
        return " - ".join(parts)


@dataclass(frozen=True)
class SpoolInfo:
    """One spool from an inventory (Bambuddy today, Spoolman later)."""

    id: int
    material: str
    brand: str
    color_name: str
    color_hex: str
    remaining_g: Optional[float]
    label_weight_g: Optional[float]
    weight_used_g: Optional[float]
    slicer_filament: str
    slicer_filament_name: str
    subtype: str = ""
    location: str = ""
    archived: bool = False

    @property
    def material_label(self) -> str:
        """Human readable material (``PLA Matte``)."""
        return " ".join(part for part in (self.material, self.subtype) if part).strip()

    @property
    def display_name(self) -> str:
        """Standard ``Brand - Material - Color`` label."""
        color = self.color_name or self.color_hex or "Unknown"
        return "{} - {} - {}".format(
            self.brand or "Unknown", self.material_label or "Filament", color
        )

    @property
    def remaining_percent(self) -> Optional[float]:
        """Remaining spool percentage, or ``None`` when unknown."""
        if not self.label_weight_g or self.remaining_g is None:
            return None
        if self.label_weight_g <= 0:
            return None
        return max(0.0, min(100.0, self.remaining_g / self.label_weight_g * 100.0))


@dataclass(frozen=True)
class SlotInfo:
    """One loaded filament slot (AMS tray, CFS slot, MMU gate, external spool).

    ``ams_id``/``tray_id`` are the unit and slot index inside the printer; the
    names are kept from the Bambu-first design.  ``custom_label`` lets a source
    name a slot its own way (``CFS 1 - A``, ``Gate 3``); ``extruder`` is the
    nozzle feeding the slot when the printer reports it; ``vendor_filament_id``
    is the printer's own filament id (Bambu ``tray_info_idx``, the same id as
    Orca's ``filament_id``); ``remaining_percent`` is the printer-reported
    amount left when no inventory spool is known.
    """

    printer_id: int
    printer_name: str
    ams_id: int
    tray_id: int
    spool: Optional[SpoolInfo] = None
    filament_type: str = ""
    color_hex: str = ""
    loaded: bool = True
    custom_label: str = ""
    extruder: Optional[int] = None
    vendor_filament_id: str = ""
    remaining_percent: Optional[float] = None

    @property
    def key(self) -> str:
        """Stable ``printer:ams:tray`` identifier used by the palette mapping."""
        return "{}:{}:{}".format(self.printer_id, self.ams_id, self.tray_id)

    @property
    def slot_label(self) -> str:
        """Short slot label, e.g. ``AMS 1 - T2`` or ``External - T1``."""
        if self.custom_label:
            return self.custom_label
        if self.ams_id == EXTERNAL_UNIT_ID:
            return "External - T{}".format(self.tray_id + 1)
        return "AMS {} - T{}".format(self.ams_id + 1, self.tray_id + 1)

    @property
    def display_name(self) -> str:
        """Best available filament label for the slot."""
        if self.spool is not None:
            return self.spool.display_name
        color = self.color_hex or "#808080"
        return "{} - {}".format(self.filament_type or "Filament", color)

    @property
    def remaining(self) -> Optional[float]:
        """Remaining percentage from the inventory spool, else from the printer."""
        if self.spool is not None and self.spool.remaining_percent is not None:
            return self.spool.remaining_percent
        return self.remaining_percent


@dataclass(frozen=True)
class PrinterContext:
    """Snapshot of printers, spools and loaded slots from one source."""

    printers: Tuple[PrinterInfo, ...] = ()
    spools: Tuple[SpoolInfo, ...] = ()
    slots: Tuple[SlotInfo, ...] = ()
    fetched_at: str = ""
    source: str = ""
    warnings: Tuple[str, ...] = ()

    @property
    def spool_by_id(self) -> Dict[int, SpoolInfo]:
        """Index spools by spool id."""
        return {spool.id: spool for spool in self.spools}

    def slots_for_printer(self, printer_id: int) -> List[SlotInfo]:
        """Return assigned slots for one printer, ordered by unit/slot."""
        return [slot for slot in self.slots if slot.printer_id == printer_id]

    def slot_by_key(self, key: str) -> Optional[SlotInfo]:
        """Return one slot by its ``printer:ams:tray`` key."""
        for slot in self.slots:
            if slot.key == key:
                return slot
        return None


# ---------------------------------------------------------------------------
# Material helpers
# ---------------------------------------------------------------------------


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def normalize_color_hex(value: Any, fallback: str = "#808080") -> str:
    """Normalise ``RGBA``/``RRGGBB``/``RRGGBBAA`` colour text to ``#RRGGBB``."""
    text = str(value or "").strip().lstrip("#")
    if _HEX_RE.match(text):
        return "#" + text[:6].upper()
    return fallback


def lab_from_hex(value: Any) -> Tuple[float, float, float]:
    """CIE L*a*b* (D65) of a ``#RRGGBB`` colour (sRGB); unreadable text is mid grey."""
    text = normalize_color_hex(value)

    def linear(channel: int) -> float:
        level = channel / 255.0
        return level / 12.92 if level <= 0.04045 else ((level + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(int(text[index:index + 2], 16)) for index in (1, 3, 5))
    xyz = (
        (0.4124 * red + 0.3576 * green + 0.1805 * blue) / 0.95047,
        0.2126 * red + 0.7152 * green + 0.0722 * blue,
        (0.0193 * red + 0.1192 * green + 0.9505 * blue) / 1.08883,
    )
    f_x, f_y, f_z = (part ** (1.0 / 3.0) if part > 0.008856 else 7.787 * part + 16.0 / 116.0 for part in xyz)
    return 116.0 * f_y - 16.0, 500.0 * (f_x - f_y), 200.0 * (f_y - f_z)


def color_difference(first: Any, second: Any) -> float:
    """How different two colours look: CIEDE2000 delta E (0 same, about 2 just
    noticeable, 10 clearly another shade, 100 black against white)."""
    l1, a1, b1 = lab_from_hex(first)
    l2, a2, b2 = lab_from_hex(second)
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    c_mean = (c1 + c2) / 2.0
    g = 0.5 * (1.0 - math.sqrt(c_mean ** 7 / (c_mean ** 7 + 25.0 ** 7)))
    a1p, a2p = (1.0 + g) * a1, (1.0 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360.0 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360.0 if c2p else 0.0
    delta_l, delta_c = l2 - l1, c2p - c1p
    delta_h = 0.0
    if c1p and c2p:
        delta_h = h2p - h1p
        if delta_h > 180.0:
            delta_h -= 360.0
        elif delta_h < -180.0:
            delta_h += 360.0
    delta_hh = 2.0 * math.sqrt(c1p * c2p) * math.sin(math.radians(delta_h / 2.0))
    l_mean, cp_mean = (l1 + l2) / 2.0, (c1p + c2p) / 2.0
    if not (c1p and c2p):
        h_mean = h1p + h2p
    elif abs(h1p - h2p) <= 180.0:
        h_mean = (h1p + h2p) / 2.0
    else:
        h_mean = (h1p + h2p + (360.0 if h1p + h2p < 360.0 else -360.0)) / 2.0
    t = (1.0 - 0.17 * math.cos(math.radians(h_mean - 30.0)) + 0.24 * math.cos(math.radians(2.0 * h_mean))
         + 0.32 * math.cos(math.radians(3.0 * h_mean + 6.0)) - 0.20 * math.cos(math.radians(4.0 * h_mean - 63.0)))
    s_l = 1.0 + 0.015 * (l_mean - 50.0) ** 2 / math.sqrt(20.0 + (l_mean - 50.0) ** 2)
    s_c = 1.0 + 0.045 * cp_mean
    s_h = 1.0 + 0.015 * cp_mean * t
    r_t = -2.0 * math.sqrt(cp_mean ** 7 / (cp_mean ** 7 + 25.0 ** 7)) * math.sin(
        math.radians(60.0 * math.exp(-(((h_mean - 275.0) / 25.0) ** 2))))
    return math.sqrt((delta_l / s_l) ** 2 + (delta_c / s_c) ** 2 + (delta_hh / s_h) ** 2
                     + r_t * (delta_c / s_c) * (delta_hh / s_h))


def material_family(material: str) -> str:
    """Return the base material family (``ABS`` for ``ABS-GF``)."""
    text = _text(material).upper()
    if not text:
        return ""
    if text.startswith("PLA"):
        return "PLA"
    if text.startswith("PETG"):
        return "PETG"
    if text.startswith("ABS"):
        return "ABS"
    if text.startswith("ASA"):
        return "ASA"
    if text.startswith("PAHT"):
        return "PAHT"
    if text.startswith("PA"):
        return "PA"
    if text.startswith("TPU"):
        return "TPU"
    if text.startswith("TPE"):
        return "TPE"
    return text.split("-")[0].split(" ")[0]


#: Density used when neither the material type nor its family is known.
DEFAULT_DENSITY = 1.24

#: Fusion template used when neither the material type nor its family is known.
DEFAULT_TEMPLATE_MATERIAL = "ABS Plastic"


def _material_lookup(table: Dict[str, Any], material: str, default: Any) -> Any:
    """Look up the exact type (``ABS-GF``, ``PLA CF``) first, then its family."""
    exact = re.sub(r"[\s_]+", "-", _text(material).upper())
    if exact in table:
        return table[exact]
    return table.get(material_family(material), default)


def default_density(material: str) -> float:
    """Density fallback for a filament material name."""
    return _material_lookup(_DENSITY_BY_MATERIAL, material, DEFAULT_DENSITY)


def default_template_material(material: str) -> str:
    """Fusion template material to copy for a filament material name."""
    return _material_lookup(_TEMPLATE_BY_MATERIAL, material, DEFAULT_TEMPLATE_MATERIAL)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _check_helpers() -> None:
    """Colour and material helpers normalise input."""
    _expect(normalize_color_hex("c6c6c6ff") == "#C6C6C6", "RRGGBBAA must drop alpha")
    _expect(normalize_color_hex("#5b6579") == "#5B6579", "colours are upper-cased")
    _expect(normalize_color_hex("nope") == "#808080", "invalid colour falls back")
    _expect(material_family("ABS-GF") == "ABS", "ABS-GF belongs to ABS")
    _expect(material_family("PAHT-CF") == "PAHT", "PAHT is not PA")
    _expect(default_density("ABS-GF") == 1.08, "exact type density wins over the family")
    _expect(default_density("pla cf") == 1.22, "type lookup ignores case and separators")
    _expect(default_density("PAHT-CF") == 1.18, "PAHT-CF has its own density")
    _expect(default_density("ABS") == 1.04, "base type density")
    _expect(default_density("PLA Matte") == 1.24, "unknown subtype falls back to the family")
    _expect(default_density("Unobtainium") == DEFAULT_DENSITY, "unknown material default")
    _expect(default_template_material("PETG-CF") == "PET Plastic", "PETG-CF template")
    _expect(default_template_material("PA6") == "Nylon Plastic", "PA6 falls back to the PA family")
    _expect(default_template_material("PA-CF") == "Nylon Plastic", "PA uses nylon")
    _expect(nozzle_flow("HH01") == "High Flow" and nozzle_flow("hs01") == "Standard", "nozzle type codes")
    _expect(nozzle_flow("hardened_steel") == "" and nozzle_flow("") == "" and nozzle_flow("HX01") == "",
            "a material name or an unknown code gives no flow")
    _expect([nozzle_material(value) for value in ("HH01", "ss00", "Hardened_Steel", "brass", "XS01", "")]
            == ["hardened_steel", "stainless_steel", "hardened_steel", "brass", "", ""],
            "nozzle material from a type code's first letter or the reported name")


def _check_slots() -> None:
    """Slot keys, labels and remaining amount."""
    spool = SpoolInfo(6, "ABS-GF", "Bambu Lab", "Black", "#000000", 240.0, 1000.0, 760.0, "GFB50", "Bambu ABS-GF")
    slot = SlotInfo(1, "Workshop H2D", 1, 3, spool, "ABS-GF", "#000000")
    _expect(slot.key == "1:1:3", "key is printer:unit:slot")
    _expect(slot.slot_label == "AMS 2 - T4", "Bambu slot label")
    _expect(abs((slot.remaining or 0) - 24.0) < 1e-9, "inventory remaining wins")
    external = SlotInfo(1, "Workshop H2D", EXTERNAL_UNIT_ID, 0)
    _expect(external.slot_label == "External - T1", "external spool label")
    direct = SlotInfo(
        3, "K2", 0, 1, None, "PLA", "#FFFFFF",
        custom_label="CFS 1 - B", vendor_filament_id="01001", remaining_percent=55.0,
    )
    _expect(direct.slot_label == "CFS 1 - B", "a source can name its slots")
    _expect(direct.remaining == 55.0, "printer remaining is used without a spool")
    _expect(direct.display_name == "PLA - #FFFFFF", "display name without a spool")
    context = PrinterContext(slots=(slot, direct), spools=(spool,), source="bambuddy")
    _expect(context.slot_by_key("3:0:1") is direct, "slot lookup by key")
    _expect(context.slots_for_printer(1) == [slot], "slots filter by printer")
    _expect(context.spool_by_id[6] is spool, "spools index by id")


def _selftest() -> int:
    """Run the printer model self-test."""
    scenarios = (
        ("colour and material helpers normalise input", _check_helpers),
        ("slot keys, labels and remaining amount", _check_slots),
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
        print("{} printer model self-test(s) FAILED".format(failures))
        return 1
    print("PASS: all printer model self-tests completed")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
