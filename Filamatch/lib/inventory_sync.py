"""Turn Bambuddy spools into Fusion filament entries.

The Bambuddy connector returns normalized inventory and live AMS slots.  This
module converts the spools behind selected slots into catalog-like entries that
the existing Favorites/importer pipeline can consume.  The entries carry the
standard ``Brand - Material - Color`` display name plus the Bambuddy metadata
(spool id, remaining weight, Orca/Bambu filament preset and live slot key) so
the palette and the prepared slicer project can keep the association.
"""

from __future__ import annotations

import colorsys
import logging
import sys
import traceback
import types
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import testing

from . import printer_model

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: Entry id prefix for Bambuddy-derived materials.
ENTRY_PREFIX = "bambuddy_spool_"

#: Basic colour names used when Bambuddy has no colour_name for a spool.
_BASIC_COLORS: Tuple[Tuple[str, str], ...] = (
    ("#000000", "Black"),
    ("#FFFFFF", "White"),
    ("#808080", "Grey"),
    ("#C6C6C6", "Light Grey"),
    ("#FF0000", "Red"),
    ("#FF6600", "Orange"),
    ("#FFA500", "Orange"),
    ("#FFFF00", "Yellow"),
    ("#00AE42", "Green"),
    ("#008000", "Green"),
    ("#0000FF", "Blue"),
    ("#000080", "Navy"),
    ("#800080", "Purple"),
    ("#A52A2A", "Brown"),
    ("#00FFFF", "Cyan"),
    ("#FF00FF", "Magenta"),
)


class InventorySyncError(Exception):
    """Raised when a Bambuddy spool cannot be converted/imported."""


@dataclass(frozen=True)
class InventoryFilament:
    """Catalog-like filament entry derived from a Bambuddy spool.

    The attribute names match ``catalog.FilamentEntry`` so the existing
    ``favorites.ensure_favorite_filament``/``importer`` pipeline can consume
    this object unchanged.  The extra fields are retained for display and for
    the slicer handoff manifest.
    """

    id: str
    manufacturer: str
    product_line: str
    color_name: str
    color_hex: str
    slicer_filament_type: str
    density_g_cm3: float
    template_material: str
    category: str = ""
    adsklib_path: Optional[str] = None
    orca_filament_profile: Optional[str] = None
    spool_id: int = 0
    spool_preset: str = ""
    remaining_g: Optional[float] = None
    label_weight_g: Optional[float] = None
    remaining_percent: Optional[float] = None
    slot_key: str = ""
    ams_id: int = -1
    tray_id: int = -1
    location: str = ""

    @property
    def display_name(self) -> str:
        """Standard ``Brand - Material - Color`` label."""
        return "{} - {} - {}".format(
            self.manufacturer, self.product_line, self.color_name
        )

    @property
    def material_label(self) -> str:
        """Material plus subtype (already used as ``product_line``)."""
        return self.product_line

    def to_catalog_dict(self) -> Dict[str, Any]:
        """Return the persistent user-catalog shape for this spool filament.

        Persisting the entry lets the material detector resolve design materials
        imported from Bambuddy back to their display name, density, type and
        Orca preset after the palette is reopened.
        """
        return {
            "id": self.id,
            "manufacturer": self.manufacturer,
            "product_line": self.product_line,
            "color_name": self.color_name,
            "color_hex": self.color_hex,
            "slicer_filament_type": self.slicer_filament_type,
            "density_g_cm3": self.density_g_cm3,
            "template_material": self.template_material,
            "adsklib_path": None,
            "orca_filament_profile": self.orca_filament_profile,
            "category": self.category,
            "bambuddy_spool_id": self.spool_id,
            "bambuddy_slot_key": self.slot_key,
            "bambuddy_preset": self.spool_preset,
        }


def _hex_to_rgb(color_hex: str) -> Optional[Tuple[int, int, int]]:
    text = printer_model.normalize_color_hex(color_hex, fallback="")
    if not text:
        return None
    return int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16)


def color_name_for_hex(color_hex: str) -> str:
    """Return a readable colour name for a hex value.

    Bambuddy often has a ``color_name`` for branded spools but leaves it empty
    for RFID/generic spools.  The importer still needs a proper colour token for
    ``Brand - Material - Color``, so derive a stable descriptive name.
    """
    text = str(color_hex or "").strip().upper()
    rgb = _hex_to_rgb(text)
    if rgb is None:
        return "Unknown"
    red, green, blue = rgb
    if red == green == blue:
        if red <= 24:
            return "Black"
        if red <= 80:
            return "Dark Grey"
        if red <= 170:
            return "Grey"
        if red <= 230:
            return "Light Grey"
        return "White"
    for known_hex, name in _BASIC_COLORS:
        if text.upper() == known_hex:
            return name
    hue, lightness, saturation = colorsys.rgb_to_hls(red / 255.0, green / 255.0, blue / 255.0)
    degrees = hue * 360.0
    if saturation < 0.18:
        if blue >= red and blue >= green:
            return "Blue Grey"
        return "Grey"
    if degrees < 15 or degrees >= 345:
        return "Red"
    if degrees < 45:
        return "Orange"
    if degrees < 70:
        return "Yellow"
    if degrees < 165:
        return "Green"
    if degrees < 200:
        return "Cyan"
    if degrees < 260:
        return "Blue"
    if degrees < 320:
        return "Purple"
    return "Pink"


def entry_for_spool(spool: printer_model.SpoolInfo, slot: Optional[printer_model.SlotInfo] = None) -> InventoryFilament:
    """Build a catalog-like entry for one Bambuddy spool."""
    if spool is None:
        raise InventorySyncError("entry_for_spool requires a spool")
    family = printer_model.material_family(spool.material) or spool.material or "PLA"
    material_label = spool.material_label or family
    color_name = spool.color_name or color_name_for_hex(spool.color_hex)
    # A spool that is a catalog filament (manual source) is imported as that
    # filament: its id, type and density.
    catalog_id = getattr(spool, "catalog_id", "") or ""
    density = getattr(spool, "density_g_cm3", None)
    return InventoryFilament(
        id=catalog_id or "{}{}".format(ENTRY_PREFIX, max(0, int(spool.id))),
        manufacturer=spool.brand or "Unknown",
        product_line=material_label,
        color_name=color_name,
        color_hex=spool.color_hex,
        slicer_filament_type=(spool.material or family) if catalog_id else family,
        density_g_cm3=density if catalog_id and density else printer_model.default_density(spool.material),
        template_material=printer_model.default_template_material(spool.material),
        category=family,
        orca_filament_profile=spool.slicer_filament_name or spool.slicer_filament or None,
        spool_id=spool.id,
        spool_preset=spool.slicer_filament_name or spool.slicer_filament,
        remaining_g=spool.remaining_g,
        label_weight_g=spool.label_weight_g,
        remaining_percent=spool.remaining_percent,
        slot_key=slot.key if slot is not None else "",
        ams_id=slot.ams_id if slot is not None else -1,
        tray_id=slot.tray_id if slot is not None else -1,
        location=spool.location,
    )


def unique_entries_for_slots(
    slots: Sequence[printer_model.SlotInfo],
    selected_keys: Optional[Iterable[str]] = None,
) -> List[InventoryFilament]:
    """Return one entry per loaded spool behind the selected slots.

    The same physical spool can appear in several slots; it is imported once,
    in the first selected slot order, so the Fusion library does not collect
    duplicates per slot.
    """
    wanted = set(str(key) for key in (selected_keys or []))

    def selected(slot: printer_model.SlotInfo) -> bool:
        return not wanted or slot.key in wanted

    entries: List[InventoryFilament] = []
    seen: set = set()
    for slot in slots:
        if not selected(slot) or slot.spool is None:
            continue
        spool_id = int(slot.spool.id)
        if spool_id in seen:
            continue
        seen.add(spool_id)
        entries.append(entry_for_spool(slot.spool, slot))
    entries.sort(key=lambda entry: (entry.spool_id, entry.slot_key))
    return entries


def _remove_unused_spool_materials(design: Any, entries: Sequence[InventoryFilament]) -> int:
    """Delete unused design materials named after spool ids by older syncs.

    Design materials now carry the filament name; the per-spool copies
    (``bambuddy_spool_<id>``) left by older syncs are removed when no body uses
    them.  They run first, before any copy shares their material id.
    """
    materials = getattr(design, "materials", None)
    removed = 0
    for entry in entries:
        try:
            legacy = materials.itemByName(entry.id) if materials is not None else None
            if legacy is None or bool(getattr(legacy, "isUsed", False)):
                continue
            legacy.deleteMe()
            removed += 1
        except Exception:
            logger.warning("Could not remove the old design material %r.", entry.id, exc_info=True)
    if removed:
        logger.info("Removed %d unused per-spool design material(s).", removed)
    return removed


def sync_slots_to_favorites(
    app: Any,
    design: Any,
    slots: Sequence[printer_model.SlotInfo],
    selected_keys: Optional[Iterable[str]] = None,
    template_resolver: Optional[Any] = None,
) -> Tuple[List[InventoryFilament], List[Any]]:
    """Import the spools behind the selected slots into Favorites and the design.

    Args:
        app: Fusion ``Application``.
        design: Active Fusion design.
        slots: Normalized Bambuddy slot assignments.
        selected_keys: Slot keys to import; all loaded slots when omitted.
        template_resolver: Optional callable entry -> template material.

    Returns:
        ``(entries, favorite_results)``.

    Raises:
        InventorySyncError: If no loaded slots were selected or the import fails.
    """
    entries = unique_entries_for_slots(slots, selected_keys)
    if not entries:
        raise InventorySyncError(
            "No loaded Bambuddy spools were selected. Assign spools to AMS slots in "
            "Bambuddy or select at least one loaded slot."
        )
    _remove_unused_spool_materials(design, entries)
    # Several spools of one filament share one favorite and one design material.
    filaments: List[InventoryFilament] = []
    for entry in entries:
        if all(entry.display_name != known.display_name for known in filaments):
            filaments.append(entry)
    try:
        from . import favorites

        results = favorites.import_catalog_to_favorites(
            app, design, filaments, template_resolver=template_resolver
        )
    except InventorySyncError:
        raise
    except Exception as exc:
        raise InventorySyncError(
            "Could not import {} Bambuddy spool(s) into Fusion: {}".format(
                len(entries), exc
            )
        ) from exc
    logger.info(
        "Synced %d Bambuddy spool(s) into Favorites and the design: %s",
        len(entries),
        ", ".join(entry.display_name for entry in entries),
    )
    return entries, results


def slot_manifest(
    entries: Sequence[InventoryFilament], printer_name: str = "", printer_id: int = 0
) -> Dict[str, Any]:
    """Build the JSON manifest stored inside exported 3MF files.

    OrcaSlicer does not read this file itself; it is the association record used
    by Filamatch (and a future Bambuddy dispatch path) so a project
    opened in Orca can be traced back to the exact spool/AMS slot it was meant
    for.
    """
    return {
        "schema": "filamatch.slot_manifest.v1",
        "printer_id": int(printer_id),
        "printer_name": printer_name,
        "filaments": [
            {
                "fusion_material": entry.id,
                "display_name": entry.display_name,
                "brand": entry.manufacturer,
                "material": entry.product_line,
                "color": entry.color_hex,
                "color_name": entry.color_name,
                "spool_id": entry.spool_id,
                "spool_preset": entry.spool_preset,
                "remaining_g": entry.remaining_g,
                "remaining_percent": entry.remaining_percent,
                "slot_key": entry.slot_key,
                "ams_id": entry.ams_id,
                "tray_id": entry.tray_id,
            }
            for entry in entries
        ],
    }


#: Entry inside prepared 3MFs carrying the Fusion<->Bambuddy slot association.
MANIFEST_ENTRY = "Metadata/filamatch.json"
#: Manifest entry written by earlier versions (FusionFilament Bridge).
LEGACY_MANIFEST_ENTRY = "Metadata/fusionfilament_bridge.json"


def manifest_entries(manifest: Mapping[str, Any]) -> Dict[str, Optional[bytes]]:
    """Return the archive changes that store ``manifest`` in a prepared 3MF.

    The result maps :data:`MANIFEST_ENTRY` to the JSON payload and
    :data:`LEGACY_MANIFEST_ENTRY` to ``None`` (drop it), in the form accepted
    by ``slicer_profiles.prepare_project(extra_entries=...)`` and
    ``slicer_profiles.rewrite_zip_entries``, so the manifest can be written in
    the same archive rewrite as the project settings.
    """
    import json

    payload = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    return {MANIFEST_ENTRY: payload, LEGACY_MANIFEST_ENTRY: None}


def attach_slot_manifest(output_path: Path, manifest: Mapping[str, Any]) -> Path:
    """Add/replace the Filamatch slot manifest inside a 3MF archive.

    The manifest is ignored by OrcaSlicer itself.  It records which Bambuddy
    spool/AMS slot each project filament came from, so a later dispatch through
    Bambuddy can reuse the exact mapping and the palette can show provenance
    when the project is re-exported.  A manifest written by earlier versions
    under the legacy name is removed; :data:`MANIFEST_ENTRY` is written once.
    """
    from . import slicer_profiles

    path = Path(output_path)
    if not path.is_file():
        raise InventorySyncError("Cannot attach a slot manifest: {!r} does not exist".format(str(path)))
    try:
        slicer_profiles.rewrite_zip_entries(path, manifest_entries(manifest))
    except slicer_profiles.SlicerProfilesError as exc:
        raise InventorySyncError(
            "Could not attach the slot manifest to {!r}: {}".format(str(path), exc)
        ) from exc
    return path


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _fixture_spools() -> Tuple[printer_model.SpoolInfo, printer_model.SpoolInfo]:
    return (
        printer_model.SpoolInfo(
            id=8,
            material="ABS-GF",
            brand="Bambu Lab",
            color_name="",
            color_hex="#F48438",
            remaining_g=480.0,
            label_weight_g=1000.0,
            weight_used_g=520.0,
            slicer_filament="GFB50",
            slicer_filament_name="Bambu ABS-GF",
        ),
        printer_model.SpoolInfo(
            id=9,
            material="PLA",
            brand="Bambu Lab",
            color_name="Charcoal",
            color_hex="#000000",
            remaining_g=210.0,
            label_weight_g=1000.0,
            weight_used_g=790.0,
            slicer_filament="GFA01",
            slicer_filament_name="Bambu PLA Matte",
            subtype="Matte",
        ),
    )


def _check_conversion() -> None:
    """Spool conversion produces the standard display and metadata."""
    spool, matte = _fixture_spools()
    slot = printer_model.SlotInfo(
        printer_id=2,
        printer_name="Studio P1S",
        ams_id=0,
        tray_id=1,
        spool=spool,
        filament_type="ABS-GF",
        color_hex="#F48438",
    )
    entry = entry_for_spool(spool, slot)
    _expect(entry.id == "bambuddy_spool_8", "entry id must be spool-based")
    _expect(entry.display_name == "Bambu Lab - ABS-GF - Orange", "display name mismatch: " + entry.display_name)
    _expect(entry.color_name == "Orange", "missing color name must be derived")
    _expect(entry.slot_key == "2:0:1", "slot key mismatch")
    _expect(entry.slicer_filament_type == "ABS", "ABS-GF family must map to ABS")
    _expect(entry.template_material == "ABS Plastic", "template mismatch")
    _expect(entry.remaining_g == 480.0, "remaining weight mismatch")
    _expect(entry.remaining_percent == 48.0, "remaining percent mismatch")
    matte_entry = entry_for_spool(matte)
    _expect(matte_entry.display_name == "Bambu Lab - PLA Matte - Charcoal", "matte name mismatch")
    _expect(matte_entry.slicer_filament_type == "PLA", "PLA type mismatch")


def _check_selection() -> None:
    """One entry per physical spool, in selected slot order."""
    spool, matte = _fixture_spools()
    slots = [
        printer_model.SlotInfo(1, "Workshop H2D", 0, 0, spool, "ABS-GF", "#F48438"),
        printer_model.SlotInfo(1, "Workshop H2D", 0, 1, spool, "ABS-GF", "#F48438"),
        printer_model.SlotInfo(1, "Workshop H2D", 0, 2, matte, "PLA", "#000000"),
        printer_model.SlotInfo(1, "Workshop H2D", 0, 3, None, "PLA", "#FFFFFF"),
    ]
    entries = unique_entries_for_slots(slots)
    _expect(len(entries) == 2, "duplicate spools must collapse to one entry")
    _expect([entry.spool_id for entry in entries] == [8, 9], "entry order mismatch")
    entries = unique_entries_for_slots(slots, ["1:0:2"])
    _expect(len(entries) == 1 and entries[0].spool_id == 9, "selected slot filtering mismatch")
    _expect(unique_entries_for_slots([]) == [], "empty slot selection must return no entries")


def _check_sync_one_material_per_filament() -> None:
    """Spools of one filament are imported once; unused per-spool copies are removed."""
    spool, matte = _fixture_spools()
    second_matte = printer_model.SpoolInfo(
        id=10,
        material="PLA",
        brand="Bambu Lab",
        color_name="Charcoal",
        color_hex="#000000",
        remaining_g=900.0,
        label_weight_g=1000.0,
        weight_used_g=100.0,
        slicer_filament="GFA01",
        slicer_filament_name="Bambu PLA Matte",
        subtype="Matte",
    )
    slots = [
        printer_model.SlotInfo(1, "Workshop H2D", 0, 0, spool, "ABS-GF", "#F48438"),
        printer_model.SlotInfo(1, "Workshop H2D", 0, 1, matte, "PLA", "#000000"),
        printer_model.SlotInfo(1, "Workshop H2D", 0, 2, second_matte, "PLA", "#000000"),
    ]

    class Material:
        def __init__(self, name: str, used: bool) -> None:
            self.name = name
            self.isUsed = used
            self.deleted = False

        def deleteMe(self) -> None:
            self.deleted = True

    old_unused = Material("bambuddy_spool_9", False)
    old_used = Material("bambuddy_spool_10", True)
    design = SimpleNamespace(
        materials=SimpleNamespace(
            itemByName=lambda name: {m.name: m for m in (old_unused, old_used)}.get(name)
        )
    )
    imported: List[Any] = []
    fake = types.ModuleType("favorites")
    fake.import_catalog_to_favorites = lambda app, design, entries, template_resolver=None: (  # type: ignore[attr-defined]
        imported.extend(entries) or [object() for _ in entries]
    )
    previous = testing.sibling_get(__package__, "favorites")
    testing.sibling_set(__package__, "favorites", fake)
    try:
        entries, _results = sync_slots_to_favorites(object(), design, slots)
    finally:
        if previous is None:
            testing.sibling_pop(__package__, "favorites")
        else:
            testing.sibling_set(__package__, "favorites", previous)
    _expect(len(entries) == 3, "every spool must still be reported")
    _expect(
        [entry.display_name for entry in imported]
        == ["Bambu Lab - ABS-GF - Orange", "Bambu Lab - PLA Matte - Charcoal"],
        "each filament must be imported once",
    )
    _expect(old_unused.deleted and not old_used.deleted, "only unused per-spool copies are removed")


def _check_colors() -> None:
    """Colour naming covers the common RFID/generic cases."""
    rows = {
        "#000000": "Black",
        "#FFFFFF": "White",
        "#C6C6C6": "Light Grey",
        "#F48438": "Orange",
        "#5B6579": "Blue Grey",
        "#00AE42": "Green",
        "#FF00FF": "Magenta",
        "#123456": "Blue",
    }
    for color_hex, expected in rows.items():
        actual = color_name_for_hex(color_hex)
        _expect(actual == expected, "{} must map to {}, got {}".format(color_hex, expected, actual))


def _check_attach_manifest() -> None:
    """The manifest is written into a copy of the project archive."""
    import json
    import tempfile
    import zipfile
    from pathlib import Path

    spool, _matte = _fixture_spools()
    entry = entry_for_spool(spool)
    manifest = slot_manifest([entry], printer_name="Workshop H2D", printer_id=1)
    with tempfile.TemporaryDirectory() as temporary_dir:
        archive = Path(temporary_dir) / "project.3mf"
        with zipfile.ZipFile(str(archive), "w") as handle:
            handle.writestr("Metadata/project_settings.config", b"{}")
            handle.writestr(LEGACY_MANIFEST_ENTRY, b"{}")
        attach_slot_manifest(archive, manifest)
        attach_slot_manifest(archive, manifest)
        with zipfile.ZipFile(str(archive)) as handle:
            names = handle.namelist()
            _expect(names.count(MANIFEST_ENTRY) == 1, "manifest entry must be written once")
            _expect(LEGACY_MANIFEST_ENTRY not in names, "the legacy manifest must be dropped")
            data = json.loads(handle.read(MANIFEST_ENTRY).decode("utf-8"))
            _expect(data["filaments"][0]["slot_key"] == "", "manifest slot must be preserved")
            _expect("Metadata/project_settings.config" in names, "project settings must be preserved")
        _expect(
            not list(Path(temporary_dir).glob(".filamatch_zip_*")),
            "temporary manifest files must be cleaned up",
        )


def _check_manifest() -> None:
    """The exported manifest carries the slot/spool association."""
    spool, _matte = _fixture_spools()
    entry = entry_for_spool(
        spool,
        printer_model.SlotInfo(1, "Workshop H2D", 0, 0, spool, "ABS-GF", "#F48438"),
    )
    manifest = slot_manifest([entry], printer_name="Workshop H2D", printer_id=1)
    _expect(manifest["printer_name"] == "Workshop H2D", "manifest printer mismatch")
    filament = manifest["filaments"][0]
    _expect(filament["slot_key"] == "1:0:0", "manifest slot key mismatch")
    _expect(filament["spool_id"] == 8, "manifest spool id mismatch")
    _expect(filament["spool_preset"] == "Bambu ABS-GF", "manifest preset mismatch")


def _selftest() -> int:
    scenarios = (
        ("Bambuddy spools convert to standard filament entries", _check_conversion),
        ("slot selection deduplicates spools and filters keys", _check_selection),
        ("sync imports each filament once and removes unused spool copies", _check_sync_one_material_per_filament),
        ("missing spool colour names are derived", _check_colors),
        ("slot manifest records printer/spool/slot association", _check_manifest),
        ("slot manifest is attached to a 3MF archive", _check_attach_manifest),
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
        print("{} inventory-sync self-test(s) FAILED".format(failures))
        return 1
    print("PASS: all inventory-sync self-tests completed")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
