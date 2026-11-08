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
