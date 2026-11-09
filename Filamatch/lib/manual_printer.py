"""The manual printer source: the user says which filament is in which slot.

It works for every printer, with no network at all (Prusa, Creality,
Klipper, a single-nozzle printer...). The palette saves one printer under
the setting ``manual_printers``: its name, nozzle size and nozzle material,
and its slots. Each slot names a Filamatch filament by its catalog id; when
the palette saves, the add-in stores a copy of the filament's fields next to
the id (``filament``), so a slot keeps working when the catalog changes.

:class:`ManualSource` turns the saved printer into the same
:class:`printer_model.PrinterContext` the other sources return: one
:class:`SlotInfo` per slot, with a :class:`SpoolInfo` that carries the
filament's catalog id, product line, density and slicer preset. So slot
matching, the "Loaded" note, "Use it", the design check and the slicer
preset work as they do for a Bambu printer. An empty slot has no spool.

Standard library only; ``python3 -m Filamatch.lib.manual_printer`` runs the
self-test.
"""

from __future__ import annotations

import logging
import sys
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .printer_model import NozzleInfo, PrinterContext, PrinterInfo, SlotInfo, SpoolInfo

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

SOURCE_ID = "manual"
LABEL = "Manual"
SETTINGS_KEY = "manual_printers"
#: Printer ids of manual printers (other sources use small ids and 10^9 + n).
FIRST_PRINTER_ID = 3_000_000_001
#: Spool ids of manual slots: above Bambuddy's and the LAN source's ids.
SPOOL_ID_BASE = 2_000_000_000
DEFAULT_NOZZLE_DIAMETER = 0.4
#: Nozzle materials the design check knows (data/filament_rules.json "nozzle_hardness").
NOZZLE_MATERIALS = ("brass", "hardened_steel", "stainless_steel", "tungsten_carbide")
MAX_SLOTS = 32
#: Filament fields kept with each slot.
FILAMENT_FIELDS = ("display_name", "manufacturer", "product_line", "color_name", "color_hex",
                   "slicer_filament_type", "density_g_cm3", "preset")


class ManualPrinterError(ValueError):
    """A manual printer in the settings or from the palette is not usable."""


@dataclass(frozen=True)
class ManualSlot:
    label: str
    catalog_id: str = ""
    filament: Mapping[str, Any] = field(default_factory=dict)
    remaining_g: Optional[float] = None


@dataclass(frozen=True)
class ManualPrinter:
    id: int
    name: str
    nozzle_diameter: float = DEFAULT_NOZZLE_DIAMETER
    nozzle_material: str = ""
    slots: Sequence[ManualSlot] = ()

    def to_settings(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "nozzle_diameter": self.nozzle_diameter,
            "nozzle_material": self.nozzle_material,
            "slots": [{"label": slot.label, "catalog_id": slot.catalog_id, "filament": dict(slot.filament),
                       "remaining_g": slot.remaining_g} for slot in self.slots],
        }

    def public(self) -> Dict[str, Any]:
        """What the palette shows and edits."""
        return {
            "id": self.id,
            "name": self.name,
            "nozzle_diameter": self.nozzle_diameter,
            "nozzle_material": self.nozzle_material,
            "slots": [{
                "label": slot.label or default_label(index),
                "catalog_id": slot.catalog_id,
                "display_name": str(slot.filament.get("display_name") or ""),
                "color_hex": str(slot.filament.get("color_hex") or ""),
                "remaining_g": slot.remaining_g,
            } for index, slot in enumerate(self.slots)],
        }


def default_label(index: int) -> str:
    return "Slot {}".format(index + 1)


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number >= 0 else None


def _printer(raw: Any) -> Optional[ManualPrinter]:
    if not isinstance(raw, Mapping):
        return None
    try:
        printer_id = int(raw.get("id") or 0)
    except (TypeError, ValueError):
        printer_id = 0
    slots = []
    for item in list(raw.get("slots") or [])[:MAX_SLOTS]:
        if not isinstance(item, Mapping):
            continue
        filament = item.get("filament")
        slots.append(ManualSlot(
            label=str(item.get("label") or "").strip(),
            catalog_id=str(item.get("catalog_id") or "").strip(),
            filament={key: filament[key] for key in FILAMENT_FIELDS if key in filament} if isinstance(filament, Mapping) else {},
            remaining_g=_number(item.get("remaining_g")),
        ))
    diameter = _number(raw.get("nozzle_diameter"))
    material = str(raw.get("nozzle_material") or "").strip().lower()
    return ManualPrinter(
        id=printer_id if printer_id > 0 else FIRST_PRINTER_ID,
        name=str(raw.get("name") or "").strip() or "My printer",
        nozzle_diameter=diameter if diameter else DEFAULT_NOZZLE_DIAMETER,
        nozzle_material=material if material in NOZZLE_MATERIALS else "",
        slots=tuple(slots),
    )


def printers_from_settings(settings: Mapping[str, Any]) -> List[ManualPrinter]:
    """The saved manual printers (bad entries skipped)."""
    raw = settings.get(SETTINGS_KEY)
    printers = [_printer(item) for item in raw] if isinstance(raw, list) else []
    return [printer for printer in printers if printer is not None]


def is_configured(settings: Mapping[str, Any]) -> bool:
    """True when a manual printer has at least one slot with a filament."""
    return any(slot.catalog_id for printer in printers_from_settings(settings) for slot in printer.slots)


def preset_name(entry: Any) -> str:
    """The slicer preset a catalog filament names: its own, else the Open Filament Database's."""
    own = str(getattr(entry, "orca_filament_profile", "") or "").strip()
    if own:
        return own[:-5] if own.lower().endswith(".json") else own
    data = getattr(entry, "slicer_data", None) or {}
    slicers = data.get("slicers") if isinstance(data, Mapping) else None
    for slicer in ("orcaslicer", "bambustudio"):
        found = slicers.get(slicer) if isinstance(slicers, Mapping) else None
        name = str(found.get("profile_name") or "").strip() if isinstance(found, Mapping) else ""
        if name:
            return name
    return ""


def filament_fields(entry: Any) -> Dict[str, Any]:
    """The fields a slot keeps from a catalog entry."""
    return {
        "display_name": str(getattr(entry, "display_name", "") or ""),
        "manufacturer": str(getattr(entry, "manufacturer", "") or ""),
        "product_line": str(getattr(entry, "product_line", "") or ""),
        "color_name": str(getattr(entry, "color_name", "") or ""),
        "color_hex": str(getattr(entry, "color_hex", "") or ""),
        "slicer_filament_type": str(getattr(entry, "slicer_filament_type", "") or ""),
        "density_g_cm3": _number(getattr(entry, "density_g_cm3", None)),
        "preset": preset_name(entry),
    }


def printer_from_palette(raw: Mapping[str, Any], find_entry: Callable[[str], Any],
                         existing: Optional[ManualPrinter] = None) -> ManualPrinter:
    """A printer from the palette form; each slot's filament is looked up by its catalog id.

    A slot whose filament is no longer in the catalog keeps the fields it had.
    Raises :class:`ManualPrinterError` for an unusable form.
    """
    if not isinstance(raw, Mapping):
        raise ManualPrinterError("The printer form is empty.")
    raw_slots = raw.get("slots")
    if not isinstance(raw_slots, list) or not raw_slots:
        raise ManualPrinterError("Add at least one slot.")
    if len(raw_slots) > MAX_SLOTS:
        raise ManualPrinterError("A printer can have up to {} slots.".format(MAX_SLOTS))
    diameter = _number(raw.get("nozzle_diameter"))
    if not diameter or not 0.1 <= diameter <= 2.0:
        raise ManualPrinterError("Pick a nozzle size between 0.1 and 2.0 mm.")
    material = str(raw.get("nozzle_material") or "").strip().lower()
    if material and material not in NOZZLE_MATERIALS:
        raise ManualPrinterError("Unknown nozzle material {!r}.".format(material))
    old = {slot.catalog_id: slot.filament for slot in (existing.slots if existing else ()) if slot.catalog_id}
    slots = []
    for index, item in enumerate(raw_slots):
        item = item if isinstance(item, Mapping) else {}
        catalog_id = str(item.get("catalog_id") or "").strip()
        filament: Mapping[str, Any] = {}
        if catalog_id:
            entry = find_entry(catalog_id)
            if entry is not None:
                filament = filament_fields(entry)
            elif catalog_id in old:
                filament = old[catalog_id]
            else:
                raise ManualPrinterError("Slot {}: the filament is not in Filamatch's list.".format(index + 1))
        grams = item.get("remaining_g")
        remaining = _number(grams) if grams not in (None, "") else None
        if grams not in (None, "") and remaining is None:
            raise ManualPrinterError("Slot {}: grams left must be a number.".format(index + 1))
        slots.append(ManualSlot(label=str(item.get("label") or "").strip()[:40], catalog_id=catalog_id,
                                filament=filament, remaining_g=remaining))
    return ManualPrinter(
        id=existing.id if existing else FIRST_PRINTER_ID,
        name=str(raw.get("name") or "").strip()[:60] or "My printer",
        nozzle_diameter=round(diameter, 3),
        nozzle_material=material,
        slots=tuple(slots),
    )


def spool_id(printer_id: int, index: int, catalog_id: str) -> int:
    """A stable spool id for one manual slot (positive, never a Bambuddy or LAN id)."""
    return SPOOL_ID_BASE + zlib.crc32("{}:{}:{}".format(printer_id, index, catalog_id).encode("utf-8")) % 100_000_000


def _subtype(kind: str, product_line: str) -> str:
    """What follows the type in the product line ("PLA Basic" -> "Basic"), else the product line."""
    if kind and product_line.upper().startswith(kind.upper() + " "):
        return product_line[len(kind) + 1:].strip()
    return "" if product_line.upper() == kind.upper() else product_line


def slot_info(printer: ManualPrinter, index: int, slot: ManualSlot) -> SlotInfo:
    filament = slot.filament
    kind = str(filament.get("slicer_filament_type") or "").strip()
    color_hex = str(filament.get("color_hex") or "").strip().upper()
    spool = None
    if slot.catalog_id:
        product_line = str(filament.get("product_line") or "").strip()
        preset = str(filament.get("preset") or "").strip()
        spool = SpoolInfo(
            id=spool_id(printer.id, index, slot.catalog_id),
            material=kind,
            brand=str(filament.get("manufacturer") or "").strip(),
            color_name=str(filament.get("color_name") or "").strip(),
            color_hex=color_hex,
            remaining_g=slot.remaining_g,
            label_weight_g=None,
            weight_used_g=None,
            slicer_filament="",
            slicer_filament_name=preset,
            subtype=_subtype(kind, product_line),
            location="{} - {}".format(printer.name, slot.label or default_label(index)),
            catalog_id=slot.catalog_id,
            product_line=product_line,
            density_g_cm3=_number(filament.get("density_g_cm3")),
        )
    return SlotInfo(
        printer_id=printer.id,
        printer_name=printer.name,
        ams_id=0,
        tray_id=index,
        spool=spool,
        filament_type=kind,
        color_hex=color_hex,
        loaded=spool is not None,
        custom_label=slot.label or default_label(index),
        extruder=None,
    )


class ManualSource:
    """Slot source for printers the user describes by hand."""

    source_id = SOURCE_ID
    label = LABEL

    def __init__(self, printers: Sequence[ManualPrinter]) -> None:
        self.printers = list(printers)

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> "ManualSource":
        return cls(printers_from_settings(settings))

    @staticmethod
    def is_configured(settings: Mapping[str, Any]) -> bool:
        return is_configured(settings)

    def _printer_info(self, printer: ManualPrinter) -> PrinterInfo:
        return PrinterInfo(
            id=printer.id,
            name=printer.name,
            model=LABEL,
            nozzles=(NozzleInfo(id=0, diameter=printer.nozzle_diameter, material=printer.nozzle_material),),
        )

    def test_connection(self) -> Dict[str, Any]:
        if not self.printers:
            raise ManualPrinterError("No manual printer is set up.")
        return {"ok": True, "printers": len(self.printers)}

    def list_printers(self) -> List[PrinterInfo]:
        return [self._printer_info(printer) for printer in self.printers]

    def fetch_context(self) -> PrinterContext:
        if not self.printers:
            raise ManualPrinterError("No manual printer is set up. Add one in Settings.")
        slots = [slot_info(printer, index, slot) for printer in self.printers
                 for index, slot in enumerate(printer.slots)]
        return PrinterContext(
            printers=tuple(self._printer_info(printer) for printer in self.printers),
            spools=tuple(slot.spool for slot in slots if slot.spool is not None),
            slots=tuple(slots),
            source=SOURCE_ID,
        )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class _Entry:
    def __init__(self, entry_id: str, manufacturer: str, product_line: str, color_name: str, color_hex: str,
                 kind: str, density: float, profile: Optional[str] = None, slicer_data: Any = None) -> None:
        self.id, self.manufacturer, self.product_line, self.color_name = entry_id, manufacturer, product_line, color_name
        self.color_hex, self.slicer_filament_type, self.density_g_cm3 = color_hex, kind, density
        self.orca_filament_profile, self.slicer_data = profile, slicer_data
        self.display_name = "{} - {} - {}".format(manufacturer, product_line, color_name)


def _selftest() -> int:
    catalog = {
        "bambu_pla_basic_black": _Entry("bambu_pla_basic_black", "Bambu Lab", "PLA Basic", "Black", "#161616", "PLA",
                                        1.24, "Bambu PLA Basic @BBL X1C.json"),
        "ofd_polylite_abs": _Entry("ofd_polylite_abs", "Polymaker", "PolyLite ABS", "Green", "#00AA44", "ABS", 1.04,
                                   slicer_data={"slicers": {"orcaslicer": {"profile_name": "Polymaker PolyLite ABS"}}}),
    }
    form = {"name": " Prusa XL ", "nozzle_diameter": "0.6", "nozzle_material": "hardened_steel", "slots": [
        {"label": "T1", "catalog_id": "bambu_pla_basic_black", "remaining_g": "250"},
        {"label": "", "catalog_id": ""},
        {"label": "T3", "catalog_id": "ofd_polylite_abs"},
    ]}
    printer = printer_from_palette(form, catalog.get)
    _expect(printer.name == "Prusa XL" and printer.nozzle_diameter == 0.6 and printer.nozzle_material == "hardened_steel",
            "the form's printer fields are kept")
    settings = {SETTINGS_KEY: [printer.to_settings()]}
    _expect(is_configured(settings) and not is_configured({SETTINGS_KEY: []}), "configured with a filament slot")
    context = ManualSource.from_settings(settings).fetch_context()
    _expect(context.source == SOURCE_ID and len(context.slots) == 3 and len(context.spools) == 2, "three slots, two spools")
    first, empty, third = context.slots
    _expect(first.slot_label == "T1" and empty.slot_label == "Slot 2" and not empty.loaded and empty.spool is None,
            "labels: own, else Slot n; an empty slot has no spool")
    spool = first.spool
    _expect(spool is not None and spool.catalog_id == "bambu_pla_basic_black" and spool.remaining_g == 250.0
            and spool.slicer_filament_name == "Bambu PLA Basic @BBL X1C" and spool.material == "PLA",
            "the spool carries the catalog id, grams and the preset without .json")
    _expect(spool.display_name == "Bambu Lab - PLA Basic - Black", "the spool is named like the catalog filament")
    _expect(third.spool.display_name == "Polymaker - PolyLite ABS - Green"
            and third.spool.slicer_filament_name == "Polymaker PolyLite ABS",
            "a product line that does not start with the type keeps its name; OFD preset name")
    _expect(spool.id > SPOOL_ID_BASE and spool.id != third.spool.id and spool.id == spool_id(printer.id, 0, spool.catalog_id),
            "spool ids are stable, distinct and above the other sources' ids")
    nozzle = context.printers[0].nozzles[0]
    _expect(nozzle.diameter == 0.6 and nozzle.material == "hardened_steel", "the nozzle reaches the design check")

    # A filament that left the catalog keeps the fields it had; an unknown new one is refused.
    again = printer_from_palette(form, lambda entry_id: None if entry_id == "ofd_polylite_abs" else catalog.get(entry_id),
                                 existing=printer)
    _expect(again.slots[2].filament.get("product_line") == "PolyLite ABS" and again.id == printer.id,
            "a filament no longer in the catalog keeps its saved fields")
    for bad, words in (({"slots": []}, "at least one slot"),
                       ({"nozzle_diameter": "x", "slots": [{}]}, "nozzle size"),
                       ({"nozzle_diameter": 0.4, "slots": [{"catalog_id": "nope"}]}, "not in Filamatch"),
                       ({"nozzle_diameter": 0.4, "slots": [{"remaining_g": "lots"}]}, "number")):
        try:
            printer_from_palette(bad, catalog.get)
        except ManualPrinterError as error:
            _expect(words in str(error), "error names the problem: {}".format(error))
        else:
            raise AssertionError("form {!r} must be refused".format(bad))
    _expect(printers_from_settings({SETTINGS_KEY: [{"slots": [{"catalog_id": "x"}]}, "junk"]})[0].id == FIRST_PRINTER_ID,
            "bad entries are skipped and a missing id gets the first manual id")
    print("PASS: manual printer self-test")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    sys.exit(_selftest())
