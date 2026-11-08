"""Slicer filament presets built from the Open Filament Database.

When a design names a filament the slicer has no preset for ("addnorth ABS
rABS"), Filamatch builds one: the slicer's own generic preset for that type
and printer, with the database's temperatures, fan speeds, flow, overrides
and brand (:func:`build_preset`).  The prepared project carries it.  With
Settings > "Save new filament presets in the slicer" it is also saved as a
user preset of Bambu Studio or OrcaSlicer (:func:`save_to_slicer`), never
over a preset that exists.

The slicers' own user presets (made in Bambu Studio or OrcaSlicer) are found
by :func:`user_presets`, so a spool set to "Polymaker PPS GF" on the printer
gets that preset.

Files only, no Fusion API and no network.  ``python3 -m
Filamatch.lib.filament_presets`` runs the self-test.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: The database's slicer names, by Filamatch's slicer kind.
DATABASE_SLICERS: Mapping[str, Tuple[str, ...]] = {
    "bambu": ("bambustudio", "orcaslicer"),
    "orca": ("orcaslicer", "bambustudio"),
    "prusa": ("prusaslicer",),
}
#: Application folder names, by slicer kind.
APP_FOLDERS: Mapping[str, str] = {"bambu": "BambuStudio", "orca": "OrcaSlicer"}
#: Bed temperature keys a database bed temperature sets (with ``_initial_layer``).
#: Cool plates keep the generic values: they are for low bed temperatures.
PLATE_KEYS: Tuple[str, ...] = ("hot_plate_temp", "textured_plate_temp", "eng_plate_temp")
NOTE = "Made by Filamatch from the Open Filament Database (MIT license)."

_CONF_ROOT_OVERRIDE: Optional[Path] = None


# ---------------------------------------------------------------------------
# Building a preset
# ---------------------------------------------------------------------------


def database_settings(slicer_data: Mapping[str, Any], kind: str) -> Dict[str, Any]:
    """The database's settings for this slicer: its own block, else the other one's."""
    slicers = slicer_data.get("slicers") if isinstance(slicer_data, Mapping) else None
    if not isinstance(slicers, Mapping):
        return {}
    for name in DATABASE_SLICERS.get(kind, ("orcaslicer", "bambustudio")):
        values = slicers.get(name)
        if isinstance(values, Mapping) and values:
            return dict(values)
    return {}


def preset_hint(slicer_data: Mapping[str, Any], kind: str) -> str:
    """The database's preset name for this slicer ("addnorth ABS rABS"), or ""."""
    name = str(database_settings(slicer_data, kind).get("profile_name") or "").strip()
    return name.split(" @", 1)[0].strip()


def filament_title(slicer_data: Mapping[str, Any], kind: str, brand: str, product: str) -> str:
    """The filament name of a built preset: the database's preset name, else "Brand Product"."""
    hint = preset_hint(slicer_data, kind)
    if hint:
        return hint
    brand, product = str(brand or "").strip(), str(product or "").strip()
    if not brand or product.lower().startswith(brand.lower()):
        return product or brand
    return "{} {}".format(brand, product)


def filament_id(title: str) -> str:
    """A user-preset filament id (``P`` and 7 hex digits, like Bambu Studio's)."""
    return "P" + hashlib.sha1(title.strip().lower().encode("utf-8")).hexdigest()[:7]


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _number_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else "{:g}".format(value)


def _temperature(settings: Mapping[str, Any], generic: Mapping[str, Any], key: str,
                 low: Optional[float], high: Optional[float]) -> Optional[float]:
    """A temperature the database gives, else the middle of its range (to 5 degrees)."""
    for source in (settings, generic):
        value = _number(source.get(key))
        if value is not None and value > 0:
            return value
    if low and high:
        return float(int((low + high) / 2.0 / 5.0 + 0.5) * 5)
    return low or high or None


def _set_all(data: Dict[str, Any], key: str, value: Any) -> bool:
    """Set a key the preset has; a list (one value per extruder variant) gets it in every place."""
    if key not in data:
        return False
    text = _number_text(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)
    current = data[key]
    data[key] = [text] * max(1, len(current)) if isinstance(current, list) else text
    return True


def _first_value(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def build_preset(base: Mapping[str, Any], slicer_data: Mapping[str, Any], kind: str,
                 brand: str, product: str, printer_name: str) -> Dict[str, Any]:
    """A filament preset for ``printer_name``: ``base`` (a flattened generic preset)
    with the database's values.

    Only keys the base preset has are set, so the slicer gets no unknown
    keys.  The result is named ``"<filament> @<printer>"`` like the slicers'
    own user presets, inherits nothing and lists only this printer.
    """
    data: Dict[str, Any] = json.loads(json.dumps(dict(base)))
    settings = database_settings(slicer_data, kind)
    slicers = slicer_data.get("slicers") if isinstance(slicer_data.get("slicers"), Mapping) else {}
    generic = slicers.get("generic") if isinstance(slicers.get("generic"), Mapping) else {}
    low, high = _number(slicer_data.get("min_print_temperature")), _number(slicer_data.get("max_print_temperature"))
    nozzle = _temperature(settings, generic, "nozzle_temp", low, high)
    if nozzle:
        first = _temperature(settings, generic, "first_layer_nozzle_temp", None, None) or nozzle
        _set_all(data, "nozzle_temperature", nozzle)
        _set_all(data, "nozzle_temperature_initial_layer", first)
    if low:
        _set_all(data, "nozzle_temperature_range_low", low)
    if high:
        _set_all(data, "nozzle_temperature_range_high", high)
    bed_low, bed_high = _number(slicer_data.get("min_bed_temperature")), _number(slicer_data.get("max_bed_temperature"))
    bed = _temperature(settings, generic, "bed_temp", bed_low, bed_high)
    if bed:
        first_bed = _temperature(settings, generic, "first_layer_bed_temp", None, None) or bed
        for plate in PLATE_KEYS:
            # 0 means the plate is not for this filament; that stays.
            if plate in data and _number_or_zero(_first_value(data[plate])) > 0:
                _set_all(data, plate, bed)
                _set_all(data, plate + "_initial_layer", first_bed)
    for key, field in (("fan_min_speed", "min_fan_speed"), ("fan_max_speed", "max_fan_speed")):
        value = _number(slicer_data.get(field))
        if value is not None and 0 <= value <= 100:
            _set_all(data, key, value)
    flow = _number(slicer_data.get("max_volumetric_speed"))
    if flow and flow > 0:
        _set_all(data, "filament_max_volumetric_speed", flow)
    chamber = _number(slicer_data.get("chamber_temperature"))
    if chamber and chamber > 0:
        _set_all(data, "chamber_temperatures", chamber) or _set_all(data, "chamber_temperature", chamber)
    overrides = settings.get("overrides")
    if isinstance(overrides, Mapping):
        for key, value in overrides.items():
            if isinstance(value, (int, float, str)) and not isinstance(value, bool):
                _set_all(data, str(key), value)
    title = filament_title(slicer_data, kind, brand, product)
    name = "{} @{}".format(title, printer_name)
    for key in ("setting_id", "base_id", "inherits", "include", "filament_settings_id"):
        data.pop(key, None)
    data.update(
        name=name,
        type="filament",
        filament_vendor=[str(brand or "").strip() or "Generic"],
        filament_id=filament_id(title),
        compatible_printers=[printer_name],
        compatible_printers_condition="",
        instantiation="true",
    )
    data["from"] = "user"
    _set_all(data, "filament_notes", NOTE)
    return data


def prusa_values(slicer_data: Mapping[str, Any], brand: str, base: Mapping[str, str]) -> Dict[str, str]:
    """PrusaSlicer filament settings from the database values (one value each).

    The same rules as :func:`build_preset`: an exact database temperature,
    else the middle of the range; only settings the base preset has, apart
    from the vendor.
    """
    settings = database_settings(slicer_data, "prusa")
    slicers = slicer_data.get("slicers") if isinstance(slicer_data.get("slicers"), Mapping) else {}
    generic = slicers.get("generic") if isinstance(slicers.get("generic"), Mapping) else {}
    values: Dict[str, Any] = {}
    low, high = _number(slicer_data.get("min_print_temperature")), _number(slicer_data.get("max_print_temperature"))
    nozzle = _temperature(settings, generic, "nozzle_temp", low, high)
    if nozzle:
        values["temperature"] = nozzle
        values["first_layer_temperature"] = _temperature(settings, generic, "first_layer_nozzle_temp", None, None) or nozzle
    bed = _temperature(settings, generic, "bed_temp", _number(slicer_data.get("min_bed_temperature")),
                       _number(slicer_data.get("max_bed_temperature")))
    if bed:
        values["bed_temperature"] = bed
        values["first_layer_bed_temperature"] = _temperature(settings, generic, "first_layer_bed_temp", None, None) or bed
    for key, field in (("min_fan_speed", "min_fan_speed"), ("max_fan_speed", "max_fan_speed")):
        value = _number(slicer_data.get(field))
        if value is not None and 0 <= value <= 100:
            values[key] = value
    flow = _number(slicer_data.get("max_volumetric_speed"))
    if flow and flow > 0:
        values["filament_max_volumetric_speed"] = flow
    overrides = settings.get("overrides")
    if isinstance(overrides, Mapping):
        values.update({str(key): value for key, value in overrides.items() if isinstance(value, (int, float, str))})
    result = {key: _number_text(value) if isinstance(value, (int, float)) else str(value)
              for key, value in values.items() if key in base}
    if brand:
        result["filament_vendor"] = brand
    return result


def _number_or_zero(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# The slicers' user presets
# ---------------------------------------------------------------------------


def _support_root() -> Path:
    if _CONF_ROOT_OVERRIDE is not None:
        return Path(_CONF_ROOT_OVERRIDE)
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or home)
    return home / ".config"


def user_filament_folder(kind: str) -> Optional[Path]:
    """The slicer's own user filament folder: ``user/<preset_folder>/filament``.

    ``preset_folder`` in the slicer's settings file, else ``default`` (the
    folder used when no one is signed in).
    """
    app = APP_FOLDERS.get(kind)
    if not app:
        return None
    root = _support_root() / app
    folder = ""
    try:
        text = (root / (app + ".conf")).read_text(encoding="utf-8")
        start = text.find("{")
        conf = json.loads(text[start:text.rfind("}") + 1]) if start >= 0 else {}
        folder = str((conf.get("app") or {}).get("preset_folder") or "").strip()
    except (OSError, ValueError, AttributeError):
        pass
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", folder or "") or folder in (".", ".."):
        folder = "default"
    return root / "user" / folder / "filament"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def user_presets(kind: str, all_folders: bool = False) -> List[Dict[str, Any]]:
    """The slicer's own user filament presets: ``{"path", "name", "data"}``.

    The active user folder only, or with ``all_folders`` every user folder
    (a signed-in account's folder too), the active one first.
    """
    folder = user_filament_folder(kind)
    if folder is None:
        return []
    folders = [folder]
    if all_folders:
        try:
            folders += [other / "filament" for other in sorted(folder.parent.parent.iterdir())
                        if other.is_dir() and other / "filament" != folder]
        except OSError:
            pass
    directories = [path for item in folders for path in (item, item / "base")]
    presets = []
    for directory in directories:
        try:
            paths = sorted(directory.glob("*.json"))
        except OSError:
            continue
        for path in paths:
            data = _read_json(path)
            if data is None:
                continue
            name = str(data.get("name") or path.stem).strip()
            presets.append({"path": str(path), "name": name, "data": data})
    return presets


def save_to_slicer(preset: Mapping[str, Any], kind: str) -> Tuple[str, Optional[Path]]:
    """Save a built preset as one of the slicer's user presets.

    Returns ``("saved", path)``, ``("exists", path)`` when a user preset of
    that name is there (it is never overwritten) or ``("unsupported", None)``.
    The slicer shows the preset after its next start.
    """
    name = str(preset.get("name") or "").strip()
    if kind == "prusa" and name:
        return _save_prusa(preset, name)
    folder = user_filament_folder(kind)
    if folder is None or not name:
        return "unsupported", None
    for existing in user_presets(kind):
        if existing["name"] == name:
            return "exists", Path(existing["path"])
    data = dict(preset)
    data["from"] = "User"
    data["inherits"] = ""
    base = folder / "base"
    base.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "Filamatch filament"
    target = base / (safe + ".json")
    if target.exists():
        return "exists", target
    _write_text(target, json.dumps(data, indent=4, ensure_ascii=False) + "\n")
    info = "sync_info = \nuser_id = \nsetting_id = \nbase_id = \nupdated_time = {}\n".format(int(time.time()))
    _write_text(target.with_suffix(".info"), info)
    logger.info("Saved the filament preset %r in %s.", name, APP_FOLDERS[kind])
    return "saved", target


#: Keys of a made PrusaSlicer preset that are not settings.
_PRUSA_META_KEYS = ("name", "filamatch_slicer", "inherits", "compatible_printers", "compatible_printers_condition",
                    "compatible_prints", "compatible_prints_condition", "renamed_from", "alias")


def _save_prusa(preset: Mapping[str, Any], name: str) -> Tuple[str, Optional[Path]]:
    """A PrusaSlicer user filament preset: ``<PrusaSlicer settings>/filament/<name>.ini``, never overwritten."""
    folder = _support_root() / "PrusaSlicer" / "filament"
    safe = re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "Filamatch filament"
    target = folder / (safe + ".ini")
    if target.exists():
        return "exists", target
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["# generated by Filamatch: {}".format(NOTE)]
    lines += ["{} = {}".format(key, value) for key, value in sorted(preset.items()) if key not in _PRUSA_META_KEYS]
    _write_text(target, "\n".join(lines) + "\n")
    logger.info("Saved the filament preset %r in PrusaSlicer.", name)
    return "saved", target


def _write_text(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".filamatch-", suffix=".tmp", dir=str(path.parent))
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    # Readable like the slicer's own preset files (mkstemp makes 0600).
    os.chmod(temporary, 0o644)
    os.replace(temporary, str(path))


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


_BASE = {
    "type": "filament", "name": "Generic ABS @BBL H2D", "inherits": "Generic ABS @base", "from": "system",
    "setting_id": "GFSB99_10", "filament_id": "GFB99", "filament_type": ["ABS"], "filament_vendor": ["Generic"],
    "nozzle_temperature": ["270", "270"], "nozzle_temperature_initial_layer": ["270", "270"],
    "nozzle_temperature_range_low": ["240"], "nozzle_temperature_range_high": ["280"],
    "hot_plate_temp": ["90"], "hot_plate_temp_initial_layer": ["90"], "cool_plate_temp": ["0"],
    "textured_plate_temp": ["90"], "textured_plate_temp_initial_layer": ["90"], "eng_plate_temp": ["0"],
    "fan_max_speed": ["80"], "filament_max_volumetric_speed": ["16", "16"], "filament_flow_ratio": ["0.95"],
    "chamber_temperatures": ["0"], "filament_notes": "", "compatible_printers": ["Bambu Lab H2D 0.4 nozzle"],
}
_DATA = {
    "min_print_temperature": 240, "max_print_temperature": 262, "min_bed_temperature": 95,
    "max_bed_temperature": 110, "max_fan_speed": 30, "max_volumetric_speed": 12, "chamber_temperature": 45,
    "slicers": {"orcaslicer": {"profile_name": "addnorth ABS rABS", "overrides": {"filament_flow_ratio": 0.93}},
                "bambustudio": {"profile_name": "addnorth ABS rABS", "generic_id": "GFB99"}},
}


def _check_build() -> None:
    preset = build_preset(_BASE, _DATA, "orca", "addnorth", "ABS rABS", "Bambu Lab H2D 0.4 nozzle")
    _expect(preset["name"] == "addnorth ABS rABS @Bambu Lab H2D 0.4 nozzle", preset["name"])
    _expect(preset["nozzle_temperature"] == ["250", "250"], "the middle of 240-262 to 5 degrees, in both places")
    _expect(preset["nozzle_temperature_range_low"] == ["240"] and preset["nozzle_temperature_range_high"] == ["262"],
            "the range")
    _expect(preset["hot_plate_temp"] == ["105"] and preset["textured_plate_temp_initial_layer"] == ["105"],
            "bed temperatures: {}".format(preset["hot_plate_temp"]))
    _expect(preset["eng_plate_temp"] == ["0"] and preset["cool_plate_temp"] == ["0"], "unused plates stay 0")
    _expect(preset["fan_max_speed"] == ["30"] and preset["filament_max_volumetric_speed"] == ["12", "12"],
            "fans and flow")
    _expect(preset["chamber_temperatures"] == ["45"], "chamber")
    _expect(preset["filament_flow_ratio"] == ["0.93"], "OrcaSlicer overrides apply")
    _expect(preset["filament_vendor"] == ["addnorth"] and re.fullmatch(r"P[0-9a-f]{7}", preset["filament_id"]),
            "vendor and a user-preset id")
    _expect(all(key not in preset for key in ("setting_id", "inherits")) and preset["from"] == "user"
            and preset["type"] == "filament" and preset["compatible_printers"] == ["Bambu Lab H2D 0.4 nozzle"],
            "a stand-alone user preset for this printer")
    _expect("unknown_key" not in build_preset(_BASE, {"slicers": {"orcaslicer": {"overrides": {"unknown_key": 1}}}},
                                              "orca", "x", "y", "P"), "keys the base lacks are not added")
    exact = build_preset(_BASE, {"slicers": {"bambustudio": {"nozzle_temp": 255, "bed_temp": 100}}},
                         "bambu", "Polymaker", "Polymaker PolyLite ABS", "Bambu Lab H2D 0.4 nozzle")
    _expect(exact["nozzle_temperature"] == ["255", "255"] and exact["hot_plate_temp"] == ["100"],
            "an exact database temperature wins over the range")
    _expect(exact["name"] == "Polymaker PolyLite ABS @Bambu Lab H2D 0.4 nozzle", "no doubled brand: " + exact["name"])
    _expect(filament_id("addnorth ABS rABS") == preset["filament_id"], "the same filament keeps its id")
    base = {"temperature": "240", "first_layer_temperature": "245", "bed_temperature": "100",
            "first_layer_bed_temperature": "100", "max_fan_speed": "15", "filament_type": "ABS"}
    values = prusa_values(_DATA, "addnorth", base)
    _expect(values == {"temperature": "250", "first_layer_temperature": "250", "bed_temperature": "105",
                       "first_layer_bed_temperature": "105", "max_fan_speed": "30", "filament_vendor": "addnorth"},
            "PrusaSlicer values: the same rules, only settings the base has: {}".format(values))


def _check_save() -> None:
    global _CONF_ROOT_OVERRIDE
    with tempfile.TemporaryDirectory(prefix="filamatch-presets-test-") as temp:
        _CONF_ROOT_OVERRIDE = Path(temp)
        try:
            preset = build_preset(_BASE, _DATA, "bambu", "addnorth", "ABS rABS", "Bambu Lab H2D 0.4 nozzle")
            status, path = save_to_slicer(preset, "bambu")
            _expect(status == "saved" and path is not None and path.parent == Path(temp) / "BambuStudio" / "user"
                    / "default" / "filament" / "base", "saved in the default user folder: {}".format(path))
            saved = json.loads(path.read_text(encoding="utf-8"))
            _expect(saved["from"] == "User" and saved["inherits"] == "" and path.with_suffix(".info").is_file(),
                    "a user preset with its .info file")
            _expect(oct(path.stat().st_mode & 0o777) == "0o644", "readable like the slicer's own files")
            _expect(save_to_slicer(preset, "bambu")[0] == "exists", "an existing preset is never overwritten")
            _expect([item["name"] for item in user_presets("bambu")] == [preset["name"]], "user presets are found")
            other = Path(temp) / "BambuStudio" / "user" / "290488310" / "filament" / "base"
            other.mkdir(parents=True)
            (other / "Mine.json").write_text('{"name": "Mine @Bambu Lab H2D 0.4 nozzle"}', encoding="utf-8")
            _expect([item["name"] for item in user_presets("bambu", all_folders=True)]
                    == [preset["name"], "Mine @Bambu Lab H2D 0.4 nozzle"], "every user folder, the active one first")
            conf = Path(temp) / "OrcaSlicer" / "OrcaSlicer.conf"
            conf.parent.mkdir(parents=True)
            conf.write_text('# header\n{"app": {"preset_folder": "12345"}}\n', encoding="utf-8")
            _expect(user_filament_folder("orca") == Path(temp) / "OrcaSlicer" / "user" / "12345" / "filament",
                    "preset_folder picks the user folder")
            _expect(save_to_slicer(preset, "cura") == ("unsupported", None), "other slicers are not written")
            prusa = {"name": "addnorth ABS rABS", "temperature": "245", "inherits": "Generic ABS @XL",
                     "compatible_printers_condition": "printer_model==\"XL\"", "filamatch_slicer": "prusa"}
            status, path = save_to_slicer(prusa, "prusa")
            text = path.read_text(encoding="utf-8") if path else ""
            _expect(status == "saved" and path == Path(temp) / "PrusaSlicer" / "filament" / "addnorth ABS rABS.ini"
                    and "temperature = 245" in text and "inherits" not in text and "compatible" not in text,
                    "a PrusaSlicer user preset without inherits and conditions: {}".format(text))
            _expect(save_to_slicer(dict(prusa, temperature="200"), "prusa")[0] == "exists"
                    and "temperature = 245" in path.read_text(encoding="utf-8"), "a PrusaSlicer preset is never overwritten")
        finally:
            _CONF_ROOT_OVERRIDE = None


def _selftest() -> int:
    failures = 0
    for label, check in (
        ("a preset is built from a generic preset and the database values", _check_build),
        ("a built preset is saved once in the slicer's user folder", _check_save),
    ):
        try:
            check()
        except Exception:
            failures += 1
            print("FAIL:", label)
            traceback.print_exc()
        else:
            print("PASS:", label)
    if failures:
        print("{} filament_presets self-test(s) FAILED".format(failures))
        return 1
    print("PASS: all filament_presets self-tests completed")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
