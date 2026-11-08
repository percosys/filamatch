"""Fusion side of the Filamatch palette.

- Creates the HTML palette (``palette/index.html``) and dispatches the
  actions its page sends with ``adsk.fusionSendData`` (see :func:`dispatch`
  for the full list); replies go back with ``palette.sendInfoToHTML``.
- Keeps the settings in ``config.SETTINGS_FILE`` (secrets in the system
  keychain) and the session log shown in the palette.
- Watches finished Fusion commands and design switches, so the palette's
  filament list follows material changes without a refresh button.
- Runs slow work (printer fetch, profile lists, the slicer CLI, the Open
  Filament Database download) on a worker thread, so Fusion never freezes.

The workflow: the user assigns filament materials to bodies in Fusion and
the add-in detects them (first appearance gives the slot order). Export
applies coloured appearances temporarily, exports the 3MF, injects the
per-object slicer metadata, restores the appearances, and can prepare a full
printer project with the slicer CLI.

Import-safe outside Fusion: ``adsk`` imports are guarded and handler classes
are only defined inside Fusion. Self-test: ``python3 -m Filamatch.lib.palette_bridge``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import bambu_lan
from . import body_settings
from . import design_check
from . import testing

from .. import config

APP_NAME = config.ADDIN_NAME
ADDIN_VERSION = config.VERSION

PALETTE_ID = config.PALETTE_ID
PALETTE_NAME = config.ADDIN_NAME
PALETTE_WIDTH = 460
PALETTE_HEIGHT = 800
#: Smallest height Filamatch asks for when it fits the palette to a screen.
PALETTE_MIN_HEIGHT = 360
MAX_EXTRUDER = 16

PALETTE_HTML = config.PALETTE_DIR / "index.html"

#: Settings, the session log and user data (see config.py for the locations;
#: ``FILAMATCH_DATA_DIR`` isolates the tests from the real files).
DATA_DIR = config.DATA_DIR
LOG_DIR = config.LOG_DIR
SETTINGS_FILE = config.SETTINGS_FILE

#: Marker written once the data of earlier versions (temp folders) is copied.
_LEGACY_MIGRATED_MARKER = ".migrated_from_temp"
#: Download caches of earlier versions: not copied, they are downloaded again.
_LEGACY_CACHE_ENTRIES = frozenset({"openfilament", "profile_cache", "flattened"})
_LOG_LIMIT = 400

DEFAULT_SETTINGS: Dict[str, Any] = {
    "output_path": "",
    "write_project_settings": True,
    "orca_enabled": True,
    "orca_path": "",
    "orca_profile_dir": "",
    "slicer_path": "",
    "prepare_enabled": True,
    "target_vendor": "",
    "target_printer": "",
    "target_process": "",
    "auto_library_update": True,
    # Slice a copy after sending for print time, filament and the spool check.
    "estimate_enabled": True,
    # Shown before the filament cost; the slicer presets' prices have no currency.
    "currency_symbol": "$",
    # Save filament presets built from the Open Filament Database in the
    # slicer's own user presets (filament_presets.save_to_slicer).
    "save_slicer_presets": False,
    "custom_library_path": "",
    "printer_source": "bambuddy",
    "bambuddy_url": "",
    "bambuddy_api_key": "",
    "bambuddy_printer_id": 0,
    "bambuddy_slot_map": {},
    # False allows an https Bambuddy with a self-signed certificate (LAN).
    "bambuddy_verify_tls": True,
    # Bambu printers read directly over the LAN (see bambu_lan.LanPrinter);
    # each access code lives in the keychain, the file keeps it empty.
    "bambu_lan_printers": [],
    # The As printed tab's own settings (lib/as_printed_bridge.py owns them).
    "as_printed": {},
}

#: Settings whose values live in the OS keychain (see ``secret_store``) when
#: it works; ``settings.json`` then stores them empty.
SECRET_SETTINGS: Tuple[str, ...] = ("bambuddy_api_key",)

#: Keychain use is limited to the Fusion process; the self-test lifts this.
_SECRETS_NEED_FUSION = True

LOGGER = logging.getLogger("Filamatch")

try:  # pragma: no cover - the adsk modules only exist inside Fusion.
    import adsk.core
    import adsk.fusion
except ImportError:  # pragma: no cover - exercised by the standalone self-test.
    adsk = None  # type: ignore[assignment]

#: Session log lines shown in the palette ("HH:MM:SS message").
_LOG_LINES: List[str] = []

#: Strong references to Fusion palette event handlers.
_PALETTE_HANDLERS: List[Any] = []


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def log_message(message: str) -> None:
    """Append a timestamped message to the palette log and the add-in log file."""
    line = "{} {}".format(time.strftime("%H:%M:%S"), message)
    _LOG_LINES.append(line)
    del _LOG_LINES[:-_LOG_LIMIT]
    LOGGER.info(message)


def get_log_lines() -> List[str]:
    """Return a copy of the current session log lines."""
    return list(_LOG_LINES)


def _error_text(exc: BaseException) -> str:
    """Return a concise error label for palette replies."""
    return "{}: {}".format(type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------


def load_settings(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load persisted palette settings, falling back to defaults.

    Args:
        path: Optional settings JSON path (defaults to the add-in temp dir).

    Returns:
        A settings dict containing every key from :data:`DEFAULT_SETTINGS`.
    """
    settings = dict(DEFAULT_SETTINGS)
    settings_path = Path(path) if path is not None else SETTINGS_FILE
    if (
        adsk is not None
        and not os.environ.get(config.DATA_DIR_ENV, "").strip()
        and settings_path == SETTINGS_FILE
    ):
        migrate_legacy_data_dir()
    try:
        if settings_path.is_file():
            raw = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in DEFAULT_SETTINGS:
                    if key in raw:
                        settings[key] = raw[key]
    except Exception:
        LOGGER.exception("Could not read palette settings from %s", settings_path)
        _set_aside_unreadable(settings_path)
    if _uses_secret_store(settings_path):
        _load_secret_settings(settings, settings_path)
    return settings


def migrate_legacy_data_dir(
    legacy_dirs: Optional[Sequence[Path]] = None,
    data_dir: Optional[Path] = None,
    legacy_library_dir: Optional[Path] = None,
) -> int:
    """Copy data that earlier versions kept in temp folders into DATA_DIR, once.

    Versions before 0.3 kept settings, added filaments and imported profiles
    in ``$TMPDIR/Filamatch`` (and FusionFilament Bridge in
    ``$TMPDIR/FusionFilamentBridge``), which the OS cleans up. Entries that
    are not in ``data_dir`` yet are copied (newest folder first; logs and
    download caches are skipped), then a saved library path inside the old
    FusionFilament Bridge library folder is dropped. Returns the number of
    copied entries. The marker file stops repeat attempts, also after an error.
    """
    legacy_dirs = list(legacy_dirs if legacy_dirs is not None else config.LEGACY_DATA_DIRS)
    data_dir = Path(data_dir or DATA_DIR)
    legacy_library_dir = Path(legacy_library_dir or config.LEGACY_DOCUMENTS_DIR)
    marker = data_dir / _LEGACY_MIGRATED_MARKER
    if marker.exists() or not any(Path(folder).is_dir() for folder in legacy_dirs):
        return 0
    copied = 0
    note = "copied"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        for folder in legacy_dirs:
            folder = Path(folder)
            if not folder.is_dir():
                continue
            for entry in folder.iterdir():
                target = data_dir / entry.name
                if (
                    entry.suffix == ".log"
                    or entry.name in _LEGACY_CACHE_ENTRIES
                    or entry.name.startswith(".")
                    or target.exists()
                ):
                    continue
                staging = data_dir / (".partial-" + entry.name)
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
                if entry.is_dir():
                    shutil.copytree(entry, staging)
                else:
                    shutil.copy2(entry, staging)
                os.replace(staging, target)
                copied += 1
        settings_path = data_dir / "settings.json"
        if settings_path.is_file():
            raw = json.loads(settings_path.read_text(encoding="utf-8"))
            library = str(raw.get("custom_library_path") or "") if isinstance(raw, dict) else ""
            if library.startswith(str(legacy_library_dir) + os.sep):
                raw["custom_library_path"] = ""
                _write_settings_file(raw, settings_path)
        LOGGER.info("Copied %d item(s) from the old temp data folders", copied)
    except Exception as exc:
        note = "failed: {}".format(exc)
        LOGGER.exception("Could not copy the old temp data folders")
    try:
        marker.write_text("{} ({})\n".format(note, ", ".join(str(f) for f in legacy_dirs)), encoding="utf-8")
    except OSError:
        LOGGER.debug("Could not write the migration marker", exc_info=True)
    return copied


def _uses_secret_store(settings_path: Path) -> bool:
    """Return whether secrets for this settings file belong in the keychain.

    Only the real session settings file, inside Fusion, uses the keychain.
    Tests run outside Fusion or point ``FILAMATCH_DATA_DIR`` / an
    explicit path elsewhere and keep the plain-file behaviour, so they never
    touch the user's keychain (a keychain item created by Fusion would make
    macOS prompt another process that reads it).
    """
    if _SECRETS_NEED_FUSION and adsk is None:
        return False
    if os.environ.get(config.DATA_DIR_ENV, "").strip():
        return False
    return Path(settings_path) == SETTINGS_FILE


def _load_secret_settings(settings: Dict[str, Any], settings_path: Path) -> None:
    """Fill secrets from the keychain; move plain-text ones into it."""
    from . import secret_store

    migrated = False
    for key in SECRET_SETTINGS:
        plain = str(settings.get(key) or "").strip()
        if plain:
            if secret_store.set_secret(key, plain):
                migrated = True
            continue
        stored = secret_store.get_secret(key)
        if stored:
            settings[key] = stored
    printers = []
    for account, entry in _lan_printer_secrets(settings):
        plain = str(entry.get("access_code") or "").strip()
        if plain:
            if secret_store.set_secret(account, plain):
                migrated = True
        else:
            entry["access_code"] = secret_store.get_secret(account) or ""
        printers.append(entry)
    if printers:
        settings[bambu_lan.SETTINGS_KEY] = printers
    if migrated:
        _write_settings_file(settings, settings_path)
        log_message("Moved saved secrets into the system keychain")


def _lan_printer_secrets(settings: Mapping[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Return ``(keychain account, copy of entry)`` for each saved LAN printer."""
    raw = settings.get(bambu_lan.SETTINGS_KEY)
    pairs: List[Tuple[str, Dict[str, Any]]] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, Mapping) and str(item.get("serial") or "").strip():
            pairs.append((bambu_lan.secret_account(str(item["serial"]).strip()), dict(item)))
    return pairs


def _set_aside_unreadable(settings_path: Path) -> None:
    """Rename an unreadable settings file so the next save cannot overwrite it."""
    backup = settings_path.with_name(
        "{}.unreadable-{}".format(settings_path.name, time.strftime("%Y%m%d-%H%M%S"))
    )
    try:
        os.replace(settings_path, backup)
        log_message(
            "The settings file could not be read; it was kept as {} and the "
            "defaults are used.".format(backup.name)
        )
    except OSError:
        LOGGER.debug("Could not set the unreadable settings file aside", exc_info=True)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so a crash leaves the old or the new file, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".{}.{}.tmp".format(path.name, os.getpid()))
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _write_settings_file(settings: Mapping[str, Any], settings_path: Path) -> None:
    """Write settings JSON, keeping keychain-stored secrets out of the file."""
    stored = dict(settings)
    if _uses_secret_store(settings_path):
        from . import secret_store

        for key in SECRET_SETTINGS:
            value = str(stored.get(key) or "").strip()
            if not value:
                continue
            if secret_store.get_secret(key) == value or secret_store.set_secret(key, value):
                stored[key] = ""
        printers = []
        for account, entry in _lan_printer_secrets(stored):
            code = str(entry.get("access_code") or "").strip()
            if code and (secret_store.get_secret(account) == code or secret_store.set_secret(account, code)):
                entry["access_code"] = ""
            printers.append(entry)
        if printers:
            stored[bambu_lan.SETTINGS_KEY] = printers
    try:
        _atomic_write_text(settings_path, json.dumps(stored, indent=2) + "\n")
    except Exception:
        LOGGER.exception("Could not write palette settings to %s", settings_path)


def save_settings(settings: Mapping[str, Any], path: Optional[Path] = None) -> Dict[str, Any]:
    """Persist recognised palette settings keys and return the stored dict.

    Args:
        settings: Mapping with any of the :data:`DEFAULT_SETTINGS` keys.
        path: Optional settings JSON path (defaults to the add-in temp dir).

    Returns:
        The merged settings that were written.
    """
    merged = load_settings(path)
    for key in DEFAULT_SETTINGS:
        if key in settings:
            merged[key] = settings[key]
    settings_path = Path(path) if path is not None else SETTINGS_FILE
    _write_settings_file(merged, settings_path)
    return merged


def public_settings(settings: Mapping[str, Any]) -> Dict[str, Any]:
    """Return settings safe to send to the palette (never the API key itself)."""
    payload = dict(settings)
    key = str(payload.pop("bambuddy_api_key", "") or "")
    payload["bambuddy_api_key_set"] = bool(key.strip())
    slot_map = payload.get("bambuddy_slot_map")
    if not isinstance(slot_map, dict):
        payload["bambuddy_slot_map"] = {}
    payload[bambu_lan.SETTINGS_KEY] = _lan_printers_public(settings)
    return payload


def _lan_printers_public(settings: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Saved LAN printers without access codes or full serials."""
    return [printer.public() for printer in bambu_lan.printers_from_settings(settings)]


def _printer_source_payload(settings: Mapping[str, Any]) -> Dict[str, Any]:
    """Source fields the palette needs to show the printer setup."""
    from . import printer_sources

    return {
        "printer_source": printer_sources.selected_source_id(settings),
        "printer_configured": printer_sources.is_configured(settings),
        "bambu_lan": {"printers": _lan_printers_public(settings)},
        "sources": [
            {"id": info.source_id, "label": info.label}
            for info in printer_sources.registered_sources()
        ],
    }


# ---------------------------------------------------------------------------
# Fusion object helpers
# ---------------------------------------------------------------------------


def _active_design() -> Any:
    """Return the active Fusion ``Design``, or ``None`` when unavailable."""
    if adsk is None:
        return None
    try:
        app = adsk.core.Application.get()
        return adsk.fusion.Design.cast(app.activeProduct)
    except Exception:
        LOGGER.exception("Could not read the active Fusion design")
        return None


def _active_app() -> Any:
    """Return the Fusion ``Application`` object, or ``None`` outside Fusion."""
    if adsk is None:
        return None
    try:
        return adsk.core.Application.get()
    except Exception:
        LOGGER.exception("Could not read the Fusion application object")
        return None


def _default_output_path() -> str:
    """Return a sensible default output path for the active design."""
    design = _active_design()
    try:
        from . import exporter

        if design is not None:
            return str(exporter.default_export_path(design))
    except Exception:
        LOGGER.exception("Could not compute a default export path")
    return ""


# ---------------------------------------------------------------------------
# Payload builders (pure helpers, also covered by the self-test)
# ---------------------------------------------------------------------------


def _catalog_payload(entries: Sequence[Any]) -> List[Dict[str, Any]]:
    """Convert catalog entries into JSON-friendly palette rows."""
    rows: List[Dict[str, Any]] = []
    for entry in entries:
        rows.append(
            {
                "id": entry.id,
                "display_name": entry.display_name,
                "manufacturer": getattr(entry, "manufacturer", ""),
                "product_line": getattr(entry, "product_line", ""),
                "color_name": getattr(entry, "color_name", ""),
                "color_hex": entry.color_hex,
                "slicer_filament_type": getattr(entry, "slicer_filament_type", ""),
                "density_g_cm3": getattr(entry, "density_g_cm3", None),
                "category": getattr(entry, "category", "") or getattr(entry, "slicer_filament_type", ""),
            }
        )
    return rows


#: Parsed catalog entries, reused while the catalog files are unchanged.
_CATALOG_CACHE: Dict[str, Any] = {"key": None, "entries": []}


def _catalog_files_key() -> Tuple[Any, ...]:
    """Return the modification times of the bundled and user catalog files."""
    stamps = []
    for path in (config.CATALOG_FILE, config.USER_CATALOG_FILE):
        try:
            stamps.append(Path(path).stat().st_mtime_ns)
        except OSError:
            stamps.append(None)
    return tuple(stamps)


def _load_catalog_entries() -> List[Any]:
    """Load bundled catalog entries plus persistent user (OpenFilament) entries.

    Cached until one of the catalog files changes: every finished Fusion
    command can trigger a refresh, so re-reading both files each time is waste.
    """
    key = _catalog_files_key()
    if _CATALOG_CACHE["key"] == key and _CATALOG_CACHE["entries"]:
        return list(_CATALOG_CACHE["entries"])
    entries = _read_catalog_entries()
    _CATALOG_CACHE.update(key=key, entries=list(entries))
    return entries


def _read_catalog_entries() -> List[Any]:
    """Read and merge the bundled and user catalogs from disk."""
    from . import catalog

    entries = list(catalog.load_catalog())
    try:
        from . import openfilament

        user_entries = openfilament.load_user_catalog()
        if user_entries:
            # Lenient: one bad user entry must not hide all the others.
            parsed = catalog.parse_catalog(user_entries, source="<user_catalog>", strict=False)
            entries = catalog.merge_catalogs(entries, parsed, "<user_catalog>")
    except Exception:
        LOGGER.exception("Could not merge the user filament catalog")
    return entries


def _profiles_summary() -> Dict[str, Any]:
    """Return a lightweight profile summary (installed slicers + imported presets)."""
    try:
        from . import slicer_profiles

        settings = load_settings()
        installs = slicer_profiles.find_installed_slicers()
        return {
            "ok": True,
            "slicers": [
                {"kind": item.kind, "name": item.name, "path": item.path, "version": item.version}
                for item in installs
            ],
            "imported": slicer_profiles.imported_profiles(),
            "selected": {
                "slicer_path": settings.get("slicer_path") or settings.get("orca_path") or "",
                "vendor": settings.get("target_vendor") or "",
                "printer": settings.get("target_printer") or "",
                "process": settings.get("target_process") or "",
            },
            "vendors": [],
            "printers": [],
            "processes": [],
        }
    except Exception as exc:
        LOGGER.exception("Could not build the profile summary")
        return {"ok": False, "error": _error_text(exc), "slicers": [], "imported": {}, "selected": {}}


def _openfilament_summary() -> Dict[str, Any]:
    """Return the OpenFilament cache summary without forcing a download."""
    try:
        from . import openfilament

        return openfilament.dataset_summary()
    except Exception:
        LOGGER.exception("Could not read the OpenFilament dataset summary")
        return {"cached": False, "version": "", "counts": {}}


def _custom_library_destination(settings: Optional[Mapping[str, Any]] = None) -> Optional[Path]:
    """Return the configured custom-library path (or None for the default)."""
    values = settings if settings is not None else load_settings()
    configured = str(values.get("custom_library_path") or "").strip()
    return Path(configured).expanduser() if configured else None


def _custom_library_payload(app: Any) -> Dict[str, Any]:
    """Return the custom-library status row for the palette."""
    try:
        from . import custom_library

        status = custom_library.custom_library_status(
            app, destination=_custom_library_destination()
        )
        return {
            "ok": True,
            "path": status.path,
            "exists": bool(status.exists),
            "loaded": bool(status.loaded),
            "material_count": int(status.material_count),
            "message": status.message,
        }
    except Exception as exc:
        LOGGER.exception("Could not read the custom library status")
        return {
            "ok": False,
            "path": "",
            "exists": False,
            "loaded": False,
            "material_count": 0,
            "message": _error_text(exc),
        }


def _normalize_favorite_material_names(app: Any) -> int:
    """Rename existing Favorites materials to standard brand/material/color labels."""
    if app is None:
        return 0
    try:
        favorites = app.favoriteMaterials
        entries = _load_catalog_entries()
    except Exception:
        LOGGER.debug("Favorite names could not be normalized", exc_info=True)
        return 0
    mapping: Dict[str, str] = {}
    for entry in entries:
        display_name = str(getattr(entry, "display_name", "") or "").strip()
        if not display_name:
            continue
        mapping[entry.id] = display_name
        mapping[display_name] = display_name
        legacy = "{} {} - {}".format(
            getattr(entry, "manufacturer", ""),
            getattr(entry, "product_line", ""),
            getattr(entry, "color_name", ""),
        ).strip()
        if legacy:
            mapping[legacy] = display_name
    renamed = 0
    try:
        count = int(favorites.count)
    except Exception:
        return 0
    for index in range(count):
        try:
            material = favorites.item(index)
            current = str(getattr(material, "name", "") or "").strip()
            desired = mapping.get(current)
            if desired and desired != current:
                material.name = desired
                renamed += 1
                LOGGER.info("Renamed favorite %r to %r.", current, desired)
        except Exception:
            LOGGER.debug("Could not normalize one favorite name", exc_info=True)
    if renamed:
        log_message("Normalized {} favorite material name(s)".format(renamed))
    return renamed


def _stale_swatch_note(app: Any, stale: Optional[Sequence[str]] = None) -> str:
    """Tell the user which favorites still show an old (white) template swatch.

    ``stale`` can come from :func:`favorites.replace_and_list_stale`, which
    saves a second walk over every favorite.
    """
    try:
        from . import favorites

        if stale is None:
            stale = favorites.stale_swatch_favorites(app, _load_catalog_entries())
    except Exception:
        LOGGER.debug("Could not check favorite swatches", exc_info=True)
        return ""
    renamed = [name for name in stale if name.startswith(favorites.OLD_FAVORITE_PREFIX)]
    others = [name for name in stale if name not in renamed]
    parts = []
    if renamed:
        parts.append(
            "{} old favorite(s) were replaced by new ones with close colours and renamed "
            "'{}...': {}. Fusion cannot delete favorites for us: right-click each in "
            "Favorites > Remove from Favorites.".format(
                len(renamed), favorites.OLD_FAVORITE_PREFIX, ", ".join(renamed)
            )
        )
    if others:
        parts.append(
            "{} favorite(s) still show the swatch of an old template: {}. Import or "
            "sync them again to replace them.".format(len(others), ", ".join(others))
        )
    note = " ".join(parts)
    if note:
        log_message(note)
    return note


def _refresh_custom_library(app: Any, settings: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Rebuild and load the custom library from the current Favorites snapshot."""
    try:
        from . import custom_library
        from . import favorites

        _normalize_favorite_material_names(app)
        replaced, stale = favorites.replace_and_list_stale(app, _load_catalog_entries())
        if replaced:
            log_message(
                "Replaced {} old favorite(s) with close-colour ones: {}".format(
                    len(replaced), ", ".join(replaced)
                )
            )
        status = custom_library.ensure_custom_library(
            app, destination=_custom_library_destination(settings), force=True
        )
        log_message(
            "Custom library refreshed: {!r} ({} materials, loaded={})".format(
                status.path, status.material_count, status.loaded
            )
        )
        return {
            "ok": True,
            "path": status.path,
            "exists": bool(status.exists),
            "loaded": bool(status.loaded),
            "material_count": int(status.material_count),
            "message": " ".join(part for part in (status.message, _stale_swatch_note(app, stale)) if part),
        }
    except Exception as exc:
        LOGGER.exception("Custom library refresh failed")
        log_message("Custom library refresh failed: {}".format(_error_text(exc)))
        return {
            "ok": False,
            "path": "",
            "exists": False,
            "loaded": False,
            "material_count": 0,
            "message": _error_text(exc),
        }


def _categories_payload(entries: Sequence[Any]) -> List[Dict[str, Any]]:
    """Group catalog ids by category, in catalog (first-appearance) order.

    Args:
        entries: Catalog entries.

    Returns:
        ``[{"name": "PLA", "filament_ids": [...]}, ...]`` ordered by the first
        occurrence of each category in the catalog.
    """
    order: List[str] = []
    ids_by_category: Dict[str, List[str]] = {}
    for entry in entries:
        category = (getattr(entry, "category", "") or getattr(entry, "slicer_filament_type", "")).strip()
        if not category:
            category = "Other"
        if category not in ids_by_category:
            order.append(category)
            ids_by_category[category] = []
        ids_by_category[category].append(entry.id)
    return [{"name": category, "filament_ids": ids_by_category[category]} for category in order]


def _detected_material_payload(material: Any) -> Dict[str, Any]:
    """Convert one ``materials_workflow.DetectedMaterial`` into a palette row."""
    body_names = list(getattr(material, "body_names", ()) or ())
    density = getattr(material, "density_g_cm3", None)
    return {
        "material_id": getattr(material, "material_id", ""),
        "material_name": getattr(material, "material_name", ""),
        "display_name": getattr(material, "display_name", ""),
        "color_hex": getattr(material, "color_hex", "#FFFFFF"),
        "filament_type": getattr(material, "filament_type", ""),
        "density_g_cm3": density,
        "category": getattr(material, "category", ""),
        "body_count": len(body_names),
        "bodies": body_names,
        "slot": getattr(material, "slot", 0),
        # False: the type is Filamatch's fallback ("PLA"), not the filament's.
        "in_catalog": bool(getattr(material, "in_catalog", False)),
        # Temperature group, abrasive filler and bed range for the design check.
        "traits": design_check.filament_traits(
            getattr(material, "filament_type", ""), getattr(material, "display_name", "")),
    }


def _materials_payload(design: Any, entries: Sequence[Any]) -> List[Dict[str, Any]]:
    """Detect the assembly's materials and convert them into palette rows."""
    if design is None:
        return []
    from . import materials_workflow

    try:
        detected = materials_workflow.collect_assembly_materials(design, entries)
    except materials_workflow.NoMaterialsFoundError:
        LOGGER.info("The active design has no solid bodies yet.")
        return []
    except Exception:
        LOGGER.exception("Could not detect assembly materials")
        log_message("Could not detect assembly materials: {}".format(traceback.format_exc()))
        return []
    try:
        placed = list(materials_workflow.iter_placed_bodies(design))
        thinnest = design_check.thinnest_by_material(placed)
    except Exception:  # The thin-detail check is a hint; detection must not fail on it.
        LOGGER.exception("Could not measure the bodies for thin details")
        placed, thinnest = [], {}
    bodies_by_material = _body_rows(placed)
    rows = [_detected_material_payload(material) for material in detected]
    for row in rows:
        row["detail_width_mm"] = thinnest.get(row["material_id"])
        row["body_list"] = bodies_by_material.get(row["material_id"], [])
    return rows


def _body_rows(placed: Sequence[Tuple[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Each filament's bodies for the palette, by ``material_id``.

    One row per body (a component placed twice is one body, ``copies`` 2):
    ``{token, name, component, copies, settings, summary}``; ``component`` is
    "" for a body of the design's top level.  ``token`` finds the body again
    (set_body_settings, select_bodies).
    """
    rows: Dict[str, List[Dict[str, Any]]] = {}
    by_token: Dict[str, Dict[str, Any]] = {}
    for material_id, body in placed:
        native = getattr(body, "nativeObject", None) or body
        token = str(getattr(native, "entityToken", "") or "")
        if not token:
            continue
        if token in by_token:
            by_token[token]["copies"] += 1
            continue
        component = getattr(native, "parentComponent", None)
        in_occurrence = getattr(body, "nativeObject", None) is not None
        settings = body_settings.read_body(native)
        row = {
            "token": token,
            "name": str(getattr(native, "name", "") or ""),
            "component": str(getattr(component, "name", "") or "") if in_occurrence else "",
            "copies": 1,
            "settings": settings,
            "summary": body_settings.describe(settings),
        }
        by_token[token] = row
        rows.setdefault(material_id, []).append(row)
    return rows


def _materials_used_payload(detected: Sequence[Any], result: Any) -> List[Dict[str, Any]]:
    """Build the export-result list of materials/filament slots that were used."""
    rows: List[Dict[str, Any]] = []
    for material in detected:
        rows.append(
            {
                "slot": getattr(material, "slot", 0),
                "material_id": getattr(material, "material_id", ""),
                "display_name": getattr(material, "display_name", ""),
                "color_hex": getattr(material, "color_hex", ""),
                "filament_type": getattr(material, "filament_type", ""),
                "density_g_cm3": getattr(material, "density_g_cm3", None),
                "body_count": len(getattr(material, "body_names", ()) or ()),
            }
        )
    return rows


def _favorites_summary(app: Any) -> Dict[str, Any]:
    """Return the app-level Favorites summary shown in the palette."""
    if app is None:
        return {"count": 0, "names": []}
    try:
        from . import favorites

        return favorites.favorites_summary(app)
    except Exception:
        LOGGER.exception("Could not read the Favorites library summary")
        return {"count": 0, "names": []}


# ---------------------------------------------------------------------------
# Palette replies
# ---------------------------------------------------------------------------


def _send(palette: Any, action: str, payload: Mapping[str, Any]) -> None:
    """Send one JSON reply to the palette, ignoring a closed/invalid palette."""
    body = dict(payload)
    body.setdefault("log", get_log_lines())
    try:
        palette.sendInfoToHTML(action, json.dumps(body))
    except Exception:
        LOGGER.exception("Could not send %r to the palette", action)


def _send_error(palette: Any, action: str, message: str, **extra: Any) -> None:
    """Send an error reply to the palette (always includes the session log)."""
    payload: Dict[str, Any] = {"ok": False, "error": message, "log": get_log_lines()}
    payload.update(extra)
    _send(palette, action, payload)


def _parse_payload(data: Any) -> Dict[str, Any]:
    """Decode an incoming palette payload into a dict (never raises)."""
    if data is None or data == "":
        return {}
    if isinstance(data, dict):
        return data
    try:
        decoded = json.loads(str(data))
    except Exception:
        LOGGER.warning("Palette sent malformed JSON (%d characters)", len(str(data)))
        return {}
    return decoded if isinstance(decoded, dict) else {}


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------
#
# Fusion calls the palette handlers on its UI thread, so a slow network call
# or slicer run would freeze Fusion. Such work runs on a worker thread
# instead; when it ends, a Fusion custom event brings the result back to the
# UI thread, where the Fusion API and the palette can be used again. Worker
# code must not touch the Fusion API. Outside Fusion (tests) jobs run inline.

_JOBS_LOCK = threading.Lock()
_JOB_RESULTS: Dict[str, Tuple[Any, Any, Optional[BaseException]]] = {}
_JOB_EVENT: Dict[str, Any] = {"app": None, "event": None, "handler": None, "unavailable": False}


def _register_job_event(app: Any) -> bool:
    """Register the custom event that reports finished jobs; False when unavailable."""
    if _JOB_EVENT["event"] is not None:
        return True
    if _JOB_EVENT["unavailable"] or adsk is None:
        return False
    try:
        try:
            app.unregisterCustomEvent(config.JOB_EVENT_ID)  # left over from an earlier load
        except Exception:
            pass
        event = app.registerCustomEvent(config.JOB_EVENT_ID)
        handler = _JobFinishedHandler()
        event.add(handler)
        _JOB_EVENT.update(app=app, event=event, handler=handler)
        return True
    except Exception:
        _JOB_EVENT["unavailable"] = True
        LOGGER.warning("Background jobs are unavailable; slow actions run inline", exc_info=True)
        return False


def _unregister_job_event() -> None:
    """Remove the custom event (palette closed); unfinished jobs are dropped."""
    event, handler, app = _JOB_EVENT["event"], _JOB_EVENT["handler"], _JOB_EVENT["app"]
    _JOB_EVENT.update(app=None, event=None, handler=None)
    with _JOBS_LOCK:
        _JOB_RESULTS.clear()
    try:
        if event is not None and handler is not None:
            event.remove(handler)
        if app is not None:
            app.unregisterCustomEvent(config.JOB_EVENT_ID)
    except Exception:
        LOGGER.debug("Could not unregister the job event", exc_info=True)


def _run_job(name: str, work: Any, finish: Any) -> None:
    """Run ``work()`` on a worker thread, then ``finish(result, error)`` on Fusion's UI thread."""
    app = _active_app()
    if app is None or not _register_job_event(app):
        try:
            result, error = work(), None
        except Exception as exc:
            result, error = None, exc
        finish(result, error)
        return
    job_id = uuid.uuid4().hex

    def target() -> None:
        result, error = None, None
        try:
            result = work()
        except Exception as exc:
            error = exc
        with _JOBS_LOCK:
            _JOB_RESULTS[job_id] = (finish, result, error)
        try:
            app.fireCustomEvent(config.JOB_EVENT_ID, job_id)
        except Exception:
            LOGGER.exception("Could not report the finished %s job", name)

    threading.Thread(target=target, name="Filamatch-" + name, daemon=True).start()


def _is_user_error(error: BaseException) -> bool:
    """Errors that explain themselves (wrong access code, server offline): no traceback."""
    from . import bambuddy

    return isinstance(error, (bambu_lan.BambuLanError, bambuddy.BambuddyError))


def _complete_job(job_id: str) -> None:
    """Call the finish step of a job that ended (on Fusion's UI thread)."""
    with _JOBS_LOCK:
        item = _JOB_RESULTS.pop(job_id, None)
    if item is None:
        return
    finish, result, error = item
    if error is not None:
        if _is_user_error(error):
            LOGGER.warning("Background job: %s", error)
        else:
            LOGGER.error("Background job failed", exc_info=(type(error), error, error.__traceback__))
    try:
        finish(result, error)
    except Exception:
        LOGGER.exception("Could not finish a background job")


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


def _initialize_custom_library(app: Any, settings: Mapping[str, Any]) -> Dict[str, Any]:
    """Auto-load (and refresh when stale) the custom library on palette startup."""
    try:
        from . import custom_library

        _normalize_favorite_material_names(app)
        status = custom_library.ensure_custom_library(
            app,
            destination=_custom_library_destination(settings),
            force=False,
        )
        log_message(
            "Custom library available at palette start: {!r} ({} materials, loaded={})".format(
                status.path, status.material_count, status.loaded
            )
        )
        return {
            "ok": True,
            "path": status.path,
            "exists": bool(status.exists),
            "loaded": bool(status.loaded),
            "material_count": int(status.material_count),
            "message": " ".join(part for part in (status.message, _stale_swatch_note(app)) if part),
        }
    except Exception as exc:
        LOGGER.exception("Could not auto-load the custom material library")
        return {
            "ok": False,
            "path": "",
            "exists": False,
            "loaded": False,
            "material_count": 0,
            "message": _error_text(exc),
        }


def _bambuddy_slot_display_name(slot: Any) -> str:
    """Return the standard display label for a Bambuddy slot."""
    try:
        from . import inventory_sync

        if getattr(slot, "spool", None) is not None:
            return inventory_sync.entry_for_spool(slot.spool, slot).display_name
    except Exception:
        LOGGER.debug("Could not derive the slot display name", exc_info=True)
    return str(getattr(slot, "display_name", "") or "")


def _bambuddy_context_payload(
    settings: Mapping[str, Any], materials: Optional[Sequence[Any]] = None
) -> Dict[str, Any]:
    """Fetch the printer slot context and auto-match it against the design materials."""
    from . import printer_sources
    from . import slot_mapping

    context = printer_sources.fetch_context(settings)
    printer_ids = {printer.id for printer in context.printers}
    selected_printer_id = int(settings.get("bambuddy_printer_id") or 0)
    if selected_printer_id not in printer_ids:
        selected_printer_id = context.printers[0].id if context.printers else 0
    slots = [
        slot for slot in context.slots if slot.printer_id == selected_printer_id
    ]
    detected = list(materials or [])
    saved_map = settings.get("bambuddy_slot_map")
    if not isinstance(saved_map, dict):
        saved_map = {}
    mapping = slot_mapping.build_mapping(detected, slots, saved_map)
    return {
        "ok": True,
        "configured": True,
        "source": context.source,
        "fetched_at": context.fetched_at,
        "selected_printer_id": selected_printer_id,
        "printers": [
            {
                "id": printer.id,
                "name": printer.name,
                "model": printer.model,
                "location": printer.location,
                "label": printer.label,
                # The installed nozzles; side "left"/"right" on a dual-nozzle printer.
                "nozzles": [
                    {
                        "side": _nozzle_side(nozzle, len(printer.nozzles)),
                        "diameter": nozzle.diameter,
                        "flow": nozzle.flow,
                        "material": nozzle.material,
                        "hardness": design_check.nozzle_hardness(nozzle.material),
                    }
                    for nozzle in getattr(printer, "nozzles", ()) or ()
                ],
            }
            for printer in context.printers
        ],
        "slots": [
            {
                "key": slot.key,
                "printer_id": slot.printer_id,
                "printer_name": slot.printer_name,
                "ams_id": slot.ams_id,
                "tray_id": slot.tray_id,
                "label": slot.slot_label,
                "loaded": slot.loaded,
                "filament_type": slot.filament_type,
                "color_hex": slot.color_hex,
                "display_name": _bambuddy_slot_display_name(slot),
                "traits": design_check.filament_traits(slot.filament_type, _bambuddy_slot_display_name(slot)),
                "spool_display_name": slot.spool.display_name if slot.spool is not None else "",

                "spool_id": slot.spool.id if slot.spool is not None else 0,
                "spool_preset": _spool_preset_name(slot.spool) if slot.spool is not None else "",
                "remaining_g": slot.spool.remaining_g if slot.spool is not None else None,
                "remaining_percent": slot.remaining,
                "location": slot.spool.location if slot.spool is not None else "",
            }
            for slot in slots
        ],
        "mapping": mapping,
        "warnings": list(context.warnings),
    }


def _bambuddy_request_updates(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the Bambuddy fields one palette request changes (not saved yet)."""
    updates: Dict[str, Any] = {}
    for key in ("bambuddy_url", "bambuddy_api_key"):
        if key in data and data.get(key) is not None and str(data.get(key)).strip():
            updates[key] = str(data.get(key)).strip()
    if "bambuddy_verify_tls" in data:
        updates["bambuddy_verify_tls"] = bool(data.get("bambuddy_verify_tls"))
    if "bambuddy_printer_id" in data:
        try:
            updates["bambuddy_printer_id"] = int(data.get("bambuddy_printer_id") or 0)
        except (TypeError, ValueError):
            updates["bambuddy_printer_id"] = 0
    return updates


def _bambuddy_settings_for_request(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Saved settings with this request's Bambuddy fields applied (in memory only).

    The caller saves the fields with :func:`save_settings` once Bambuddy
    answered, so a mistyped address is never stored.
    """
    current = load_settings()
    current.update(_bambuddy_request_updates(data))
    return current


def _handle_bambuddy_context(data: Mapping[str, Any], palette: Any) -> None:
    """Fetch printers/slots from the printer source (in the background) and auto-match."""
    try:
        from . import materials_workflow

        settings = _bambuddy_settings_for_request(data)
        design = _active_design()
        entries = _load_catalog_entries()
        materials = (
            materials_workflow.collect_assembly_materials(design, entries)
            if design is not None
            else []
        )
    except Exception as exc:
        LOGGER.exception("Could not read the design for the printer context")
        _send_error(palette, "bambuddyContextData", _error_text(exc))
        return

    def finish(payload: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            _send_error(palette, "bambuddyContextData", _error_text(error))
            return
        updates = _bambuddy_request_updates(data)
        if updates:
            save_settings(updates)
        log_message(
            "Bambuddy context: {} printer(s), {} slot(s), {} auto-matched material(s)".format(
                len(payload["printers"]),
                len(payload["slots"]),
                sum(1 for row in payload["mapping"] if row.get("slot_key")),
            )
        )
        _send(palette, "bambuddyContextData", payload)

    _run_job("printer", lambda: _bambuddy_context_payload(settings, materials), finish)


def _handle_bambuddy_import(data: Mapping[str, Any], palette: Any) -> None:
    """Import the spools currently in the selected AMS slots into Fusion.

    The printer context is fetched in the background; the Fusion import runs
    when it arrives.
    """
    try:
        from . import printer_sources

        settings = _bambuddy_settings_for_request(data)
        if _active_design() is None:
            raise RuntimeError("Open a Fusion design before importing Bambuddy spools.")
        if _active_app() is None:
            raise RuntimeError("Bambuddy spool import requires the Fusion application.")
    except Exception as exc:
        LOGGER.exception("Could not start the Bambuddy import")
        _send_error(palette, "bambuddyImportResult", _error_text(exc))
        return

    def finish(context: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            _send_error(palette, "bambuddyImportResult", _error_text(error))
            return
        updates = _bambuddy_request_updates(data)
        if updates:
            save_settings(updates)
        _import_bambuddy_slots(data, settings, context, palette)

    _run_job("printer", lambda: printer_sources.fetch_context(settings), finish)


def _import_bambuddy_slots(
    data: Mapping[str, Any], settings: Mapping[str, Any], context: Any, palette: Any
) -> None:
    """Import the fetched slots into Favorites and the design (Fusion UI thread)."""
    try:
        from . import inventory_sync

        design = _active_design()
        app = _active_app()
        if design is None or app is None:
            raise RuntimeError("Open a Fusion design before importing Bambuddy spools.")
        printer_id = int(settings.get("bambuddy_printer_id") or 0)
        slots = [slot for slot in context.slots if slot.printer_id == printer_id]
        raw_keys = data.get("slot_keys")
        selected_keys = (
            [str(key) for key in raw_keys if str(key).strip()]
            if isinstance(raw_keys, list) and raw_keys
            else None
        )
        entries, results = inventory_sync.sync_slots_to_favorites(
            app, design, slots, selected_keys=selected_keys
        )
        try:
            from . import openfilament

            openfilament.append_user_catalog(
                [entry.to_catalog_dict() for entry in entries]
            )
        except Exception:
            LOGGER.exception("Could not persist Bambuddy spool entries in the user catalog")
        custom_library_payload = _custom_library_payload(app)
        if bool(settings.get("auto_library_update", True)):
            custom_library_payload = _refresh_custom_library(app, settings)
        catalog_entries = _load_catalog_entries()
        payload = {
            "ok": True,
            "imported": [
                {
                    "id": entry.id,
                    "display_name": entry.display_name,
                    "spool_id": entry.spool_id,
                    "slot_key": entry.slot_key,
                    "slot_label": next(
                        (
                            slot.slot_label
                            for slot in slots
                            if slot.key == entry.slot_key
                        ),
                        "",
                    ),
                    "color_hex": entry.color_hex,
                    "remaining_g": entry.remaining_g,
                    "spool_preset": entry.spool_preset,
                }
                for entry in entries
            ],
            "favorites": _favorites_summary(app),
            "custom_library": custom_library_payload,
            "materials": _materials_payload(design, catalog_entries),
            "catalog": _catalog_payload(catalog_entries),
            "categories": _categories_payload(catalog_entries),
        }
        log_message(
            "Imported {} Bambuddy spool(s) into Favorites and the design".format(
                len(entries)
            )
        )
        _send(palette, "bambuddyImportResult", payload)
    except Exception as exc:
        LOGGER.exception("Could not import Bambuddy spools")
        _send_error(palette, "bambuddyImportResult", _error_text(exc))


def _handle_use_loaded_filament(data: Mapping[str, Any], palette: Any) -> None:
    """Replace one design filament on its bodies with the spool loaded in a slot.

    The spool is imported like ``bambuddy_import`` (Favorites and the design);
    the slot stays chosen for the new material.  Replies ``loadedFilamentUsed``.
    """
    try:
        from . import printer_sources

        material_id = str(data.get("material_id") or "").strip()
        slot_key = str(data.get("slot_key") or "").strip()
        if not material_id or not slot_key:
            raise ValueError("A design filament and a printer slot are needed.")
        settings = _bambuddy_settings_for_request(data)
        if _active_design() is None or _active_app() is None:
            raise RuntimeError("Open a Fusion design first.")
    except Exception as exc:
        LOGGER.exception("Could not start use_loaded_filament")
        _send_error(palette, "loadedFilamentUsed", _error_text(exc))
        return

    def finish(context: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            _send_error(palette, "loadedFilamentUsed", _error_text(error))
            return
        _use_loaded_filament(material_id, slot_key, settings, context, palette)

    _run_job("printer", lambda: printer_sources.fetch_context(settings), finish)


def _use_loaded_filament(
    material_id: str, slot_key: str, settings: Mapping[str, Any], context: Any, palette: Any
) -> None:
    """Fusion UI thread part of :func:`_handle_use_loaded_filament`."""
    try:
        from . import inventory_sync, materials_workflow

        design, app = _active_design(), _active_app()
        if design is None or app is None:
            raise RuntimeError("Open a Fusion design first.")
        printer_id = int(settings.get("bambuddy_printer_id") or 0)
        slots = [slot for slot in context.slots if slot.printer_id == printer_id]
        if not any(slot.key == slot_key and slot.loaded for slot in slots):
            raise RuntimeError("That printer slot has no spool now. Read the printer again.")
        entries, results = inventory_sync.sync_slots_to_favorites(app, design, slots, selected_keys=[slot_key])
        if not results or getattr(results[0], "design_material", None) is None:
            raise RuntimeError("The loaded filament could not be added to the design.")
        material = results[0].design_material
        changed = materials_workflow.replace_body_material(design, material_id, material)
        new_id = materials_workflow.material_identity(material)
        slot_map = dict(load_settings().get("bambuddy_slot_map") or {})
        slot_map.pop(material_id, None)
        slot_map[new_id] = slot_key
        save_settings({"bambuddy_slot_map": slot_map})
        try:
            from . import openfilament

            openfilament.append_user_catalog([entry.to_catalog_dict() for entry in entries])
        except Exception:
            LOGGER.exception("Could not keep the loaded filament in the user catalog")
        catalog_entries = _load_catalog_entries()
        display_name = results[0].display_name
        log_message("Replaced a design filament with the loaded {} on {} body(ies)".format(display_name, changed))
        _send(palette, "loadedFilamentUsed", {
            "ok": True,
            "replaced": changed,
            "display_name": display_name,
            "old_material_id": material_id,
            "material_id": new_id,
            "slot_key": slot_key,
            "slot_map": slot_map,
            "favorites": _favorites_summary(app),
            "materials": _materials_payload(design, catalog_entries),
            "catalog": _catalog_payload(catalog_entries),
            "categories": _categories_payload(catalog_entries),
        })
    except Exception as exc:
        LOGGER.exception("Could not use the loaded filament")
        _send_error(palette, "loadedFilamentUsed", _error_text(exc))


def _handle_init(palette: Any) -> None:
    """Send catalog, detected materials, settings and library info to the palette."""
    from . import printer_sources

    try:
        entries = _load_catalog_entries()
        settings = load_settings()
        if not settings.get("output_path"):
            settings["output_path"] = _default_output_path()
        design = _active_design()
        _DESIGN_WATCH["fingerprint"] = design_fingerprint(design)
        app = _active_app()
        if app is not None and bool(settings.get("auto_library_update", True)):
            custom_library_payload = _initialize_custom_library(app, settings)
        else:
            custom_library_payload = _custom_library_payload(app)
        payload = {
            "ok": True,
            "version": ADDIN_VERSION,
            "catalog": _catalog_payload(entries),
            "categories": _categories_payload(entries),
            "design_name": _design_name(design),
            "materials": _materials_payload(design, entries),
            "settings": public_settings(settings),
            "bambuddy": {
                # The selected printer source (Bambuddy or LAN) is set up.
                "configured": printer_sources.is_configured(settings),
                "key_set": bool(str(settings.get("bambuddy_api_key") or "").strip()),
                "url": str(settings.get("bambuddy_url") or ""),
                "printer_id": int(settings.get("bambuddy_printer_id") or 0),
                "slot_map": settings.get("bambuddy_slot_map") or {},
            },
            "favorites": _favorites_summary(app),
            "profiles": _profiles_summary(),
            "openfilament": _openfilament_summary(),
            "custom_library": custom_library_payload,
        }
        payload.update(_printer_source_payload(settings))
        log_message(
            "Palette initialised ({} catalog entries, {} categories)".format(
                len(entries), len(payload["categories"])
            )
        )
        _send(palette, "initData", payload)
    except Exception as exc:
        LOGGER.exception("Palette init failed")
        _send_error(palette, "initData", _error_text(exc))


def _import_selected_materials(app: Any, design: Any, selected: Sequence[Any]) -> List[Any]:
    """Import catalog entries into Favorites + the design (legacy fallback).

    Args:
        app: Fusion ``Application`` (needed for the Favorites library).
        design: Active Fusion design.
        selected: Catalog entries to import.

    Returns:
        Result objects exposing ``catalog_id``, ``favorite_material``,
        ``favorite_appearance``, ``design_material``, ``color_hex``,
        ``replaced_existing`` and ``relinked_bodies``.
    """
    try:
        from . import favorites
    except ImportError:
        LOGGER.warning("favorites module unavailable; using design-only import")
        from . import materials_workflow

        source_appearance = materials_workflow.find_source_appearance(design, app)
        legacy = materials_workflow.import_catalog_entries(
            design, selected, source_appearance=source_appearance
        )
        return [
            type(
                "LegacyImportResult",
                (),
                {
                    "catalog_id": item.catalog_id,
                    "favorite_material": None,
                    "favorite_appearance": None,
                    "design_material": item.material,
                    "color_hex": "",
                    "replaced_existing": False,
                    "relinked_bodies": 0,
                },
            )()
            for item in legacy
        ]
    return favorites.import_catalog_to_favorites(app, design, selected)


def _handle_import(data: Mapping[str, Any], palette: Any) -> None:
    """Import the selected catalog entries (with colored appearances)."""
    try:
        from . import catalog
        from . import materials_workflow

        raw_ids = data.get("ids") or []
        if not isinstance(raw_ids, list) or not raw_ids:
            _send_error(palette, "importResult", "No catalog entries were selected.")
            return
        wanted = {str(item) for item in raw_ids}
        entries = _load_catalog_entries()
        selected = [entry for entry in entries if entry.id in wanted]
        if not selected:
            _send_error(palette, "importResult", "None of the selected catalog ids exist.")
            return

        design = _active_design()
        if design is None:
            _send_error(
                palette,
                "importResult",
                "Open a Fusion design before importing filament materials.",
            )
            return

        app = _active_app()
        import_settings = load_settings()
        imported: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        custom_library_payload: Dict[str, Any] = _custom_library_payload(app)
        try:
            results = _import_selected_materials(app, design, selected)
            for result in results:
                design_material = getattr(result, "design_material", None)
                favorite_material = getattr(result, "favorite_material", None)
                favorite_appearance = getattr(result, "favorite_appearance", None)
                design_appearance = (
                    getattr(design_material, "appearance", None) if design_material else None
                )
                imported.append(
                    {
                        "id": getattr(result, "catalog_id", ""),
                        "fusion_name": getattr(design_material, "name", ""),
                        "appearance_name": getattr(design_appearance, "name", ""),
                        "favorite_name": getattr(favorite_material, "name", ""),
                        "favorite_appearance_name": getattr(favorite_appearance, "name", ""),
                        "color_hex": getattr(result, "color_hex", ""),
                        "replaced": bool(getattr(result, "replaced_existing", False)),
                        "relinked_bodies": int(getattr(result, "relinked_bodies", 0) or 0),
                    }
                )
                log_message(
                    "Imported {!r} into Favorites ({!r}, {}) and design material {!r}".format(
                        getattr(result, "catalog_id", ""),
                        getattr(favorite_material, "name", ""),
                        getattr(result, "color_hex", ""),
                        getattr(design_material, "name", ""),
                    )
                )
        except Exception as exc:
            LOGGER.exception("Import failed")
            for entry in selected:
                errors.append({"id": entry.id, "message": _error_text(exc)})

        if imported and bool(import_settings.get("auto_library_update", True)):
            custom_library_payload = _refresh_custom_library(app, import_settings)
        elif app is not None:
            custom_library_payload = _custom_library_payload(app)

        _send(
            palette,
            "importResult",
            {
                "ok": bool(imported) and not errors,
                "imported": imported,
                "errors": errors,
                "materials": _materials_payload(design, entries),
                "favorites": _favorites_summary(app),
                "custom_library": custom_library_payload,
            },
        )
    except Exception as exc:
        LOGGER.exception("Palette import action failed")
        _send_error(palette, "importResult", _error_text(exc))


def _handle_refresh_materials(palette: Any, fingerprint: Optional[str] = None) -> None:
    """Rescan the active design and send the detected assembly materials."""
    try:
        entries = _load_catalog_entries()
        design = _active_design()
        _DESIGN_WATCH["fingerprint"] = fingerprint or design_fingerprint(design)
        log_message("Refreshed assembly materials from the active design")
        _send(
            palette,
            "materialsData",
            {
                "ok": True,
                "design_name": _design_name(design),
                "materials": _materials_payload(design, entries),
            },
        )
    except Exception as exc:
        LOGGER.exception("Palette refresh_materials failed")
        _send_error(palette, "materialsData", _error_text(exc))


# ---------------------------------------------------------------------------
# Design change detection
# ---------------------------------------------------------------------------

#: Last design fingerprint sent to the palette, and the Fusion event handlers
#: that watch for changes while the palette is open.
_DESIGN_WATCH: Dict[str, Any] = {"fingerprint": "", "handlers": []}

#: Commands that end constantly and never change materials.
_IGNORED_COMMANDS = frozenset({"SelectCommand"})


def _design_name(design: Any) -> str:
    """Return the active design's document name without the ``vNN`` suffix."""
    try:
        name = str(design.parentDocument.name or "")
    except Exception:
        return ""
    parts = name.rsplit(" v", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return name


def design_fingerprint(design: Any) -> str:
    """Return a cheap signature of every body, its physical material and its
    own print settings.

    Assigning, removing or swapping a material, adding or deleting a body,
    changing a body's print settings (also from another add-in, such as
    Printegrity, that writes the same attribute) and switching designs all
    change it; moving or orbiting does not.
    """
    if design is None:
        return ""
    rows: List[str] = []
    try:
        for component in design.allComponents:
            component_material = getattr(component, "material", None)
            component_key = "{}|{}".format(
                getattr(component, "id", "") or component.name,
                getattr(component_material, "id", "") if component_material else "",
            )
            for body in component.bRepBodies:
                material = getattr(body, "material", None)
                rows.append(
                    "{}|{}|{}|{}".format(
                        component_key,
                        body.name,
                        getattr(material, "id", "") if material else "",
                        body_settings.stored_text(body),
                    )
                )
    except Exception:
        LOGGER.debug("Could not fingerprint the design", exc_info=True)
        return ""
    digest = hashlib.sha1("\n".join(sorted(rows)).encode("utf-8")).hexdigest()
    return "{}:{}".format(_design_name(design), digest)


def _check_design_changed(palette: Any) -> bool:
    """Send fresh materials to the palette when the design fingerprint changed."""
    fingerprint = design_fingerprint(_active_design())
    if fingerprint == _DESIGN_WATCH["fingerprint"]:
        return False
    _DESIGN_WATCH["fingerprint"] = fingerprint
    _handle_refresh_materials(palette, fingerprint)
    return True


def _visible_palette() -> Any:
    """Return the add-in palette when it is open and visible, else ``None``."""
    if adsk is None:
        return None
    try:
        palette = adsk.core.Application.get().userInterface.palettes.itemById(PALETTE_ID)
        if palette is not None and palette.isVisible:
            return palette
    except Exception:
        LOGGER.debug("Could not look up the palette", exc_info=True)
    return None


def _start_design_watch(app: Any, user_interface: Any) -> None:
    """Watch for finished commands and design switches while the palette is open."""
    _stop_design_watch()
    watches = (
        (user_interface.commandTerminated, _CommandTerminatedHandler()),
        (app.documentActivated, _DocumentActivatedHandler()),
    )
    for event, handler in watches:
        try:
            event.add(handler)
            _DESIGN_WATCH["handlers"].append((event, handler))
        except Exception:
            LOGGER.exception("Could not watch the design for material changes")


def _stop_design_watch() -> None:
    """Remove the design change handlers."""
    for event, handler in _DESIGN_WATCH["handlers"]:
        try:
            event.remove(handler)
        except Exception:
            LOGGER.debug("Could not remove a design watch handler", exc_info=True)
    del _DESIGN_WATCH["handlers"][:]


def _usable_slicer_path(candidate: str, installs: Sequence[Any]) -> str:
    """Return a valid slicer path, falling back to an installed slicer."""
    text = str(candidate or "").strip()
    if text:
        try:
            if Path(text).expanduser().exists():
                return text
        except OSError:
            pass
    preferred = [item for item in installs if getattr(item, "kind", "") == "orca"] + list(
        installs
    )
    for item in preferred:
        path = str(getattr(item, "path", "") or "").strip()
        if path:
            try:
                if Path(path).exists():
                    return path
            except OSError:
                continue
    return ""


def _slicer_kind(path: str) -> str:
    """``orca``, ``bambu`` or ``""`` for a slicer path."""
    from . import slicer_profiles

    return slicer_profiles.slicer_kind(path)


def _slicer_display_name(path: str) -> str:
    """Display name of the slicer at ``path``."""
    from . import slicer_profiles

    return slicer_profiles.slicer_name(path)


def _installed_slicer(installs: Sequence[Any], kind: str) -> str:
    """Path of the first installed slicer of ``kind`` ("" when none)."""
    for item in installs:
        path = str(getattr(item, "path", "") or "").strip()
        if getattr(item, "kind", "") == kind and path:
            try:
                if Path(path).exists():
                    return path
            except OSError:
                continue
    return ""


def _prepare_slicer_path(chosen: str, installs: Sequence[Any], vendor: str = "") -> str:
    """Return the slicer whose command line prepares the project ("" when none).

    The chosen slicer prepares its own project: OrcaSlicer and PrusaSlicer
    always, Bambu Studio for printers from its own presets (``bambu_local``).
    Other printers sent to Bambu Studio, and slicers that cannot prepare, use
    an installed OrcaSlicer; the chosen slicer then only opens the result.
    """
    from . import slicer_profiles

    kind = slicer_profiles.slicer_kind(chosen)
    if kind in ("orca", "prusa") or (kind == "bambu" and vendor in ("", "bambu_local")):
        return chosen
    orca = _installed_slicer(installs, "orca")
    if orca or kind == "bambu" or vendor not in ("", "bambu_local"):
        return orca
    return _installed_slicer(installs, "bambu")


def _profiles_payload(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the full profile listing for the palette (vendors/printers/processes)."""
    from . import slicer_profiles

    settings = load_settings()
    requested_slicer = str(
        data.get("slicer_path")
        or settings.get("slicer_path")
        or settings.get("orca_path")
        or ""
    ).strip()
    installs = slicer_profiles.find_installed_slicers()
    slicer_exe = _usable_slicer_path(requested_slicer, installs)
    if slicer_profiles.slicer_kind(slicer_exe) != "prusa":
        # An installed OrcaSlicer picks the matching online profile version;
        # PrusaSlicer lists its own presets.
        slicer_exe = _installed_slicer(installs, "orca") or slicer_exe

    vendors = slicer_profiles.list_vendors(slicer_exe) if slicer_exe else []
    vendor = str(
        data.get("vendor") or settings.get("target_vendor") or (vendors[0]["id"] if vendors else "")
    ).strip()
    if vendors and vendor not in {item["id"] for item in vendors}:
        # A vendor of another slicer (after switching between OrcaSlicer and PrusaSlicer).
        vendor = vendors[0]["id"]
    printers = slicer_profiles.list_printers(slicer_exe, vendor) if vendor else []
    printer_names = {item["name"] for item in printers}
    printer = str(data.get("printer") or settings.get("target_printer") or "").strip()
    if printer not in printer_names:
        printer = printers[0]["name"] if printers else ""
    processes = (
        slicer_profiles.list_processes(slicer_exe, vendor, printer)
        if vendor and printer
        else []
    )
    process_names = {item["name"] for item in processes}
    process = str(data.get("process") or settings.get("target_process") or "").strip()
    if process not in process_names:
        process = ""
        for item in processes:
            if item.get("default"):
                process = item["name"]
                break
        if not process and processes:
            process = processes[0]["name"]
    try:
        # The infill types this slicer accepts, for the body settings menu.
        infill_patterns = [{"value": name, "label": body_settings.pattern_label(name)}
                           for name in slicer_profiles.infill_patterns(slicer_exe)] if slicer_exe else []
    except Exception:
        LOGGER.exception("Could not read the slicer's infill types")
        infill_patterns = []
    try:
        # The chosen printer's values the As printed estimate needs.
        printer_values = {"nozzle": slicer_profiles.printer_nozzle(slicer_exe, vendor, printer)} if printer else {}
    except Exception:
        LOGGER.exception("Could not read the printer's nozzle")
        printer_values = {}
    return {
        "ok": True,
        "slicers": [
            {"kind": item.kind, "name": item.name, "path": item.path, "version": item.version}
            for item in installs
        ],
        "vendors": vendors,
        "printers": printers,
        "processes": processes,
        "infill_patterns": infill_patterns,
        "printer_values": printer_values,
        # Other slicers' names for the same infill ("zig-zag" is OrcaSlicer's "rectilinear").
        "infill_aliases": body_settings.PATTERN_ALIASES.get(slicer_profiles.slicer_kind(slicer_exe), {}),
        "imported": slicer_profiles.imported_profiles(),
        "selected": {
            "slicer_path": slicer_exe,
            "vendor": vendor,
            "printer": printer,
            "process": process,
        },
    }


def _handle_get_profiles(data: Mapping[str, Any], palette: Any) -> None:
    """List slicer vendors/printers/processes (in the background: may download)."""

    def finish(payload: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            _send_error(palette, "profilesData", _error_text(error))
        else:
            _send(palette, "profilesData", payload)

    _run_job("profiles", lambda: _profiles_payload(data), finish)


def _handle_import_profiles(palette: Any) -> None:
    """Open a native multi-select dialog and import user slicer profile JSONs."""
    try:
        from . import slicer_profiles

        dialog = _create_file_dialog("Import slicer profiles (machine/process/filament JSON)")
        _set_dialog_property(dialog, "filter", "Slicer profiles (*.json);;All files (*)")
        _set_dialog_property(dialog, "isMultiSelectEnabled", True)
        imported = slicer_profiles.imported_profiles()
        root = str(imported.get("root") or "")
        if root and Path(root).is_dir():
            _set_dialog_property(dialog, "initialDirectory", root)
        result = dialog.showOpen()
        if result != adsk.core.DialogResults.DialogOK:
            _send(palette, "profilesImported", {"ok": False, "cancelled": True})
            return
        try:
            chosen = list(dialog.filenames or [])
        except Exception:
            chosen = []
        if not chosen:
            filename = str(getattr(dialog, "filename", "") or "").strip()
            chosen = [filename] if filename else []
        if not chosen:
            _send(palette, "profilesImported", {"ok": False, "cancelled": True})
            return
        summary = slicer_profiles.import_profile_files(chosen)
        payload = _profiles_payload({})
        payload.update({"ok": True, "imported": summary})
        log_message(
            "Imported slicer profiles: {} machine(s), {} process(es), {} filament(s)".format(
                len(summary.get("machines") or []),
                len(summary.get("processes") or []),
                len(summary.get("filaments") or []),
            )
        )
        _send(palette, "profilesImported", payload)
    except Exception as exc:
        LOGGER.exception("Importing slicer profiles failed")
        _send_error(palette, "profilesImported", _error_text(exc))


def _handle_create_library(palette: Any) -> None:
    """Rebuild the user's custom material library from the current Favorites."""
    try:
        app = _active_app()
        if app is None:
            _send_error(
                palette,
                "libraryCreated",
                "Open Fusion before creating the material library.",
            )
            return
        settings = load_settings()
        payload = _refresh_custom_library(app, settings)
        if payload.get("ok"):
            try:
                save_settings({"custom_library_path": str(payload.get("path") or "")})
            except Exception:
                LOGGER.exception("Could not persist the custom library path")
        _send(
            palette,
            "libraryCreated",
            {
                "ok": bool(payload.get("ok")),
                "custom_library": payload,
                "error": payload.get("message") if not payload.get("ok") else "",
            },
        )
    except Exception as exc:
        LOGGER.exception("Creating the custom library failed")
        _send_error(
            palette,
            "libraryCreated",
            _error_text(exc),
            custom_library={"ok": False, "message": _error_text(exc)},
        )


def _handle_openfilament_search(data: Mapping[str, Any], palette: Any) -> None:
    """Search the Open Filament Database (in the background: the first search downloads it)."""

    def finish(payload: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            _send_error(palette, "openFilamentData", _error_text(error))
            return
        log_message("OpenFilament search {!r}: {} result(s)".format(payload.pop("query"), len(payload["results"])))
        _send(palette, "openFilamentData", payload)

    _run_job("search", lambda: _openfilament_search_payload(data), finish)


def _openfilament_search_payload(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Run one Open Filament Database search (no Fusion API: safe on a worker thread)."""
    from . import openfilament

    query = str(data.get("query") or "").strip()
    material = str(data.get("material") or "").strip() or None
    try:
        limit = max(1, min(100, int(data.get("limit") or 50)))
    except Exception:
        limit = 50
    results = openfilament.search(query, material=material, limit=limit)
    summary = openfilament.dataset_summary()
    try:
        materials = openfilament.search_materials()
    except Exception:
        materials = []
    rows = [
        {
            "key": item.key,
            "brand": item.brand,
            "material": item.material,
            "filament": item.filament,
            "variant": item.variant,
            "color_hex": item.color_hex,
            "density_g_cm3": item.density_g_cm3,
            "min_print_temp": item.min_print_temp,
            "max_print_temp": item.max_print_temp,
            "min_bed_temp": item.min_bed_temp,
            "max_bed_temp": item.max_bed_temp,
        }
        for item in results
    ]
    return {"ok": True, "results": rows, "summary": summary, "materials": materials, "query": query}


def _handle_openfilament_import(data: Mapping[str, Any], palette: Any) -> None:
    """Import selected OpenFilament results into Favorites + the active design."""
    try:
        from . import catalog
        from . import favorites
        from . import openfilament

        keys = data.get("keys") or []
        if not isinstance(keys, list) or not keys:
            _send_error(palette, "openFilamentImportResult", "No online filaments were selected.")
            return
        design = _active_design()
        if design is None:
            _send_error(
                palette, "openFilamentImportResult", "Open a Fusion design before importing."
            )
            return
        entries_data = openfilament.get_entries([str(key) for key in keys])
        if not entries_data:
            _send_error(
                palette,
                "openFilamentImportResult",
                "The selected OpenFilament entries could not be resolved from the cached dataset.",
            )
            return
        parsed = catalog.parse_catalog(entries_data, source="<openfilament>")
        results = favorites.import_catalog_to_favorites(_active_app(), design, parsed)
        imported = []
        for result in results:
            imported.append(
                {
                    "id": result.catalog_id,
                    "fusion_name": getattr(result.design_material, "name", ""),
                    "favorite_name": getattr(result.favorite_material, "name", ""),
                    "color_hex": result.color_hex,
                }
            )
        added = 0
        try:
            added = openfilament.append_user_catalog(entries_data)
        except Exception:
            LOGGER.exception("Could not persist the imported OpenFilament entries")
        custom_library_payload = _custom_library_payload(_active_app())
        import_settings = load_settings()
        if imported and bool(import_settings.get("auto_library_update", True)):
            custom_library_payload = _refresh_custom_library(
                _active_app(), import_settings
            )
        log_message(
            "Imported {} OpenFilament filament(s) into Favorites and the design".format(
                len(imported)
            )
        )
        catalog_entries = _load_catalog_entries()
        _send(
            palette,
            "openFilamentImportResult",
            {
                "ok": bool(imported),
                "imported": imported,
                "errors": [],
                "favorites": _favorites_summary(_active_app()),
                "custom_library": custom_library_payload,
                "catalog_count": len(catalog_entries),
                "catalog": _catalog_payload(catalog_entries),
                "categories": _categories_payload(catalog_entries),
                "user_catalog_added": added,
            },
        )
    except Exception as exc:
        LOGGER.exception("OpenFilament import failed")
        _send_error(palette, "openFilamentImportResult", _error_text(exc))


def _resolve_profile_target(data: Mapping[str, Any]) -> Any:
    """Resolve the saved/payload target into a slicer_profiles.ProfileTarget."""
    from . import slicer_profiles

    settings = load_settings()
    target = data.get("target") if isinstance(data.get("target"), dict) else {}
    slicer_exe = str(
        target.get("slicer_path")
        or data.get("slicer_path")
        or settings.get("slicer_path")
        or settings.get("orca_path")
        or ""
    ).strip()
    installs = slicer_profiles.find_installed_slicers()
    slicer_exe = _usable_slicer_path(slicer_exe, installs)
    if not slicer_exe:
        raise RuntimeError(
            "No slicer executable configured. Set the slicer path in the palette and "
            "try again (OrcaSlicer or Bambu Studio)."
        )
    vendor = str(target.get("vendor") or settings.get("target_vendor") or "").strip()
    printer = str(target.get("printer") or settings.get("target_printer") or "").strip()
    process = str(target.get("process") or settings.get("target_process") or "").strip()
    prepare_exe = _prepare_slicer_path(slicer_exe, installs, vendor)
    if not prepare_exe:
        name = slicer_profiles.slicer_name(slicer_exe)
        raise RuntimeError(
            "Preparing the project for {0} with this printer needs OrcaSlicer: {0} can "
            "prepare only printers from Bambu Studio's own list. Install OrcaSlicer, pick "
            "a Bambu Lab printer, or turn off Advanced > Prepare to send a plain "
            "3MF.".format(name)
        )
    slicer_exe = prepare_exe
    if not vendor or not printer:
        raise RuntimeError(
            "Choose a target vendor and printer in the Target Printer section before "
            "preparing the project."
        )
    printers = slicer_profiles.list_printers(slicer_exe, vendor)
    machine = next((item for item in printers if item.get("name") == printer), None)
    if machine is None and len(printers) == 1:
        machine = printers[0]
    if machine is None:
        raise RuntimeError(
            "Printer preset {!r} was not found for vendor {!r}. Refresh the profile list "
            "and choose the printer again.".format(printer, vendor)
        )
    if slicer_profiles.slicer_kind(slicer_exe) == "orca" and not slicer_profiles.orca_supports_printer(machine["name"]):
        raise RuntimeError(
            "OrcaSlicer cannot slice the {} yet. Choose Bambu Studio 2.9.1 or newer in "
            "Settings > Send to, or turn off Advanced > Prepare to send a plain 3MF.".format(machine["name"])
        )
    processes = slicer_profiles.list_processes(slicer_exe, vendor, machine["name"])
    proc = next((item for item in processes if item.get("name") == process), None)
    if proc is None:
        proc = next((item for item in processes if item.get("default")), None)
    if proc is None and processes:
        proc = processes[0]
    if proc is None:
        raise RuntimeError("No process presets were found for {!r}.".format(machine["name"]))
    return slicer_profiles, slicer_exe, slicer_profiles.ProfileTarget(
        vendor=vendor,
        printer_name=machine["name"],
        machine_path=str(machine.get("machine_path") or ""),
        process_name=proc["name"],
        process_path=str(proc.get("process_path") or ""),
        source=str(machine.get("source") or ""),
    )


def _slot_nozzles(rows: Sequence[Mapping[str, Any]], slots_by_key: Mapping[str, Any]) -> List[Optional[str]]:
    """Return the nozzle feeding each row's slot ("left"/"right"), None when unknown."""
    from . import slicer_profiles

    sides = {
        bambu_lan.EXTRUDER_LEFT: slicer_profiles.NOZZLE_LEFT,
        bambu_lan.EXTRUDER_RIGHT: slicer_profiles.NOZZLE_RIGHT,
    }
    nozzles: List[Optional[str]] = []
    for row in rows:
        slot = slots_by_key.get(str(row.get("slot_key") or ""))
        nozzles.append(sides.get(getattr(slot, "extruder", None)))
    return nozzles


def _pin_slot_nozzles(output_path: Path, nozzles: Sequence[Optional[str]]) -> None:
    """Pin the prepared project's filaments to their slots' nozzles (never fails the export)."""
    from . import slicer_profiles

    try:
        if slicer_profiles.apply_nozzle_map(output_path, nozzles):
            log_message("Each filament uses the nozzle its slot feeds: {}".format(", ".join(nozzles)))
    except Exception as exc:
        LOGGER.exception("Could not pin the filaments to their nozzles")
        log_message("The slicer picks the nozzles itself: {}".format(_error_text(exc)))


def _is_prusa_family(path: str) -> bool:
    """True for PrusaSlicer and SuperSlicer, which read their own 3MF layout."""
    from . import prusa_project

    return prusa_project.is_prusa_family(path)


def _write_prusa_layout(output_path: Path) -> None:
    """Rewrite the plain 3MF so PrusaSlicer keeps each body's extruder (never fails the export)."""
    from . import prusa_project

    try:
        result = prusa_project.to_prusa_layout(output_path)
        log_message(
            "PrusaSlicer layout: {} object(s) with {} part(s), each on its own extruder".format(
                result["objects"], result["parts"]
            )
        )
    except Exception as exc:
        LOGGER.exception("Could not write the PrusaSlicer layout")
        log_message("PrusaSlicer gets the plain 3MF; its slots are not kept: {}".format(_error_text(exc)))


def _use_bambu_studio_words(output_path: Path, target: Any) -> None:
    """Write Bambu Studio's words in an OrcaSlicer project (never fails the export)."""
    from . import slicer_profiles

    try:
        changed = slicer_profiles.use_bambu_studio_words(
            output_path, getattr(target, "process_path", None)
        )
        if changed:
            log_message("Wrote Bambu Studio's words for: {}".format(", ".join(sorted(changed))))
    except Exception as exc:
        LOGGER.exception("Could not write Bambu Studio's words")
        log_message("Bambu Studio may show 'Some values have been replaced': {}".format(_error_text(exc)))


def _read_slot_rows(detected: Sequence[Any], settings: Mapping[str, Any]) -> Optional[types.SimpleNamespace]:
    """Read the printer's slots and match each design material to one (network).

    Returns ``printers`` (by id), ``printer_id``, ``slots`` and ``rows`` (one
    :func:`slot_mapping.build_mapping` row per material), or None when no
    printer source is set up.
    """
    from . import printer_sources
    from . import slot_mapping

    if not printer_sources.is_configured(settings):
        return None
    context = printer_sources.fetch_context(settings)
    printer_id = int(settings.get("bambuddy_printer_id") or 0)
    printers = {printer.id: printer for printer in context.printers}
    if printer_id not in printers and context.printers:
        printer_id = context.printers[0].id
    slots = [slot for slot in context.slots if slot.printer_id == printer_id]
    saved_map = settings.get("bambuddy_slot_map")
    rows = slot_mapping.build_mapping(list(detected), slots, saved_map if isinstance(saved_map, dict) else {})
    return types.SimpleNamespace(printers=printers, printer_id=printer_id, slots=slots, rows=rows)


def _spool_preset_name(spool: Any) -> str:
    """The slicer preset name of a loaded spool (``PolyLite ABS``), or ""."""
    name = str(getattr(spool, "slicer_filament_name", "") or "").strip()
    if not name:
        preset = bambu_lan.filament_preset(str(getattr(spool, "slicer_filament", "") or "").strip())
        name = preset.name if preset is not None else ""
    return name


def _same_filament_name(loaded: str, design_name: str) -> bool:
    """True when every word of a spool's preset name ("Bambu PLA Basic") is in
    the design's filament name without its colour ("Bambu Lab - PLA Basic")."""
    parts = [part.strip() for part in str(design_name or "").split(" - ")]
    design_words = " ".join(parts[:-1] if len(parts) >= 2 else parts).upper().split()
    return all(word in design_words for word in str(loaded or "").split(" @")[0].upper().split())


def _with_database_data(detected: Sequence[Any]) -> List[Any]:
    """Open Filament Database filaments added before their catalog entries kept
    slicer data get it from the cached dataset (see openfilament)."""
    materials = list(detected)
    for index, material in enumerate(materials):
        name = str(getattr(material, "material_name", "") or "")
        if not name.startswith("ofd_") or getattr(material, "slicer_data", None) or not dataclasses.is_dataclass(material):
            continue
        from . import openfilament

        data = openfilament.slicer_data_for_catalog_id(name)
        if data:
            materials[index] = dataclasses.replace(material, slicer_data=data)
    return materials


def _with_spool_presets(detected: Sequence[Any], slot_rows: Optional[types.SimpleNamespace]) -> List[Any]:
    """Give each design material the slicer preset of the spool it prints with.

    The slicer project then names the filament that is loaded ("PolyLite
    ABS"), not the one the design names ("Bambu ABS").  Only for a spool of
    the same material type, and not for a spool set to a generic preset when
    the design names a brand.  A spool of another filament than the design's
    also drops the design's database data, which is not the spool's.
    """
    materials = list(detected)
    if slot_rows is None:
        return materials
    slots_by_key = {slot.key: slot for slot in slot_rows.slots}
    by_material = {str(row.get("material_id")): slots_by_key.get(str(row.get("slot_key"))) for row in slot_rows.rows}
    changed = []
    for index, material in enumerate(materials):
        slot = by_material.get(str(getattr(material, "material_id", "")))
        spool = getattr(slot, "spool", None)
        if spool is None or not dataclasses.is_dataclass(material):
            continue
        wanted = str(getattr(material, "filament_type", "") or "").strip().upper()
        loaded = str(getattr(slot, "filament_type", "") or spool.material or "").strip().upper()
        name = _spool_preset_name(spool)
        if not name or not wanted or wanted != loaded or name == getattr(material, "orca_filament_profile", ""):
            continue
        brand = str(getattr(material, "manufacturer", "") or "").strip().lower()
        if "generic" in name.lower() and brand not in ("", "generic", "unknown"):
            continue
        changes: Dict[str, Any] = {"orca_filament_profile": name}
        if getattr(material, "slicer_data", None) and not _same_filament_name(
            name, str(getattr(material, "display_name", "") or "")
        ):
            changes["slicer_data"] = {}
        materials[index] = dataclasses.replace(material, **changes)
        changed.append("{} -> {}".format(getattr(material, "display_name", "") or wanted, name))
    if changed:
        log_message("Slicer presets of the loaded spools: {}".format(", ".join(changed)))
    return materials


def _nozzle_side(nozzle: Any, count: int) -> str:
    """"left" or "right" for one of two nozzles (printer id 0 is the right one), else ""."""
    if count != 2:
        return ""
    return "right" if int(getattr(nozzle, "id", -1)) == bambu_lan.EXTRUDER_RIGHT else "left"


def _nozzle_text(nozzle: Any, count: int) -> str:
    """"Left 0.4 High Flow" for one of two nozzles, "0.4 Standard" for a single one."""
    side = _nozzle_side(nozzle, count).capitalize()
    side = side + " " if side else ""
    size = "{:g}".format(float(getattr(nozzle, "diameter", 0.0) or 0.0))
    return (side + size + " " + str(getattr(nozzle, "flow", "") or "")).strip()


def _apply_printer_nozzles(output: Path, slot_rows: Optional[types.SimpleNamespace]) -> None:
    """Write the selected printer's nozzle flow types into the prepared project (never fails the export)."""
    if slot_rows is None:
        return
    printer = slot_rows.printers.get(slot_rows.printer_id)
    nozzles = tuple(getattr(printer, "nozzles", ()) or ())
    if not nozzles:
        return
    from . import slicer_profiles

    try:
        if slicer_profiles.apply_printer_nozzles(output, nozzles):
            log_message("The project uses the printer's nozzles: {}".format(
                ", ".join(_nozzle_text(nozzle, len(nozzles)) for nozzle in nozzles)))
    except Exception as exc:
        LOGGER.exception("Could not write the printer's nozzles")
        log_message("The project keeps the preset's nozzle flow: {}".format(_error_text(exc)))


def _report_built_presets(paths: Sequence[str], kind: str, save: bool) -> None:
    """Log the filament presets built from the Open Filament Database and, when
    ``save`` (Settings), save them in the slicer (never fails the export)."""
    from . import filament_presets

    for path in paths:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            name = str(data.get("name") or Path(path).stem)
            if not save:
                log_message("Made the filament preset {!r} from the Open Filament Database".format(name))
                continue
            status, saved = filament_presets.save_to_slicer(data, kind)
            app = {"bambu": "Bambu Studio", "orca": "OrcaSlicer"}.get(kind, "this slicer")
            if status == "saved":
                log_message("Made the filament preset {!r} and saved it in {} (it shows after {} restarts)".format(
                    name, app, app))
            elif status == "exists":
                log_message("Made the filament preset {!r}; {} already has one of that name".format(name, app))
            else:
                log_message("Made the filament preset {!r}; saving presets works for Bambu Studio and "
                            "OrcaSlicer only".format(name))
        except Exception as exc:
            LOGGER.exception("Could not save the built filament preset %r", path)
            log_message("Could not save the filament preset {}: {}".format(Path(path).stem, _error_text(exc)))


def _attach_bambuddy_slot_manifest(
    output_path: Path,
    detected: Sequence[Any],
    settings: Mapping[str, Any],
    pin_nozzles: bool = False,
    slot_rows: Optional[types.SimpleNamespace] = None,
) -> Optional[Dict[str, Any]]:
    """Attach the Bambuddy/AMS slot association manifest to an exported 3MF.

    With ``pin_nozzles`` (a prepared project) each filament is also pinned to
    the nozzle its slot feeds.  ``slot_rows`` (from :func:`_read_slot_rows`)
    are read here when not given.  The export must never fail just because
    Bambuddy is offline; errors are logged and reported separately.  Returns
    the manifest payload (None when Bambuddy is not configured or no material
    is mapped).
    """
    from . import inventory_sync
    from . import slot_mapping

    try:
        if slot_rows is None:
            slot_rows = _read_slot_rows(detected, settings)
        if slot_rows is None:
            return None
        printers, printer_id = slot_rows.printers, slot_rows.printer_id
        slots, rows = slot_rows.slots, slot_rows.rows
        mapped_rows = [row for row in rows if row.get("slot_key")]
        slots_by_key = {slot.key: slot for slot in slots}
        if pin_nozzles:
            _pin_slot_nozzles(output_path, _slot_nozzles(rows, slots_by_key))
        entries = []
        seen_spools = set()
        for row in mapped_rows:
            slot = slots_by_key.get(str(row.get("slot_key")))
            if slot is None or slot.spool is None:
                continue
            if slot.spool.id in seen_spools:
                continue
            seen_spools.add(slot.spool.id)
            entries.append(inventory_sync.entry_for_spool(slot.spool, slot))
        if not entries:
            return None
        printer = printers.get(printer_id)
        manifest = inventory_sync.slot_manifest(
            entries,
            printer_name=printer.name if printer is not None else "",
            printer_id=printer_id,
        )
        taken = [str(row["slot_key"]) for row in mapped_rows]

        def backups(row: Mapping[str, Any]) -> List[Dict[str, Any]]:
            slot = slots_by_key.get(str(row["slot_key"]))
            if slot is None:
                return []
            return [
                {"slot_key": other.key, "slot_label": other.slot_label, "remaining_g": slot_mapping.grams_left(other)}
                for other in slot_mapping.backup_slots(slot, slots, taken)
            ]

        # AMS auto refill on the printer: True/False when it reports it, else None.
        manifest["auto_refill"] = getattr(printer, "auto_refill", None) if printer is not None else None
        manifest["materials"] = [
            {
                "fusion_material_id": row["material_id"],
                "display_name": row["display_name"],
                "slot_key": row["slot_key"],
                "slot_label": row["slot_label"],
                # Same filament on the same nozzle: what an AMS can refill from.
                "backup_slots": backups(row),
            }
            for row in mapped_rows
        ]
        inventory_sync.attach_slot_manifest(output_path, manifest)
        log_message(
            "Attached Bambuddy slot manifest for {} mapped filament(s)".format(
                len(mapped_rows)
            )
        )
        return manifest
    except Exception:
        LOGGER.exception("Could not attach the Bambuddy slot manifest")
        log_message("Bambuddy slot manifest was not attached (export continued)")
        return None


def _launch_orca(executable: str, file_path: Path) -> bool:
    """Launch Orca Slicer (or another configured slicer) with an exported 3MF.

    Args:
        executable: Path to the slicer executable or macOS ``.app`` bundle.
        file_path: Exported 3MF to open.

    Returns:
        True when a process was started.

    Raises:
        OSError: If the executable does not exist or launching fails.
    """
    executable = (executable or "").strip()
    if not executable:
        raise OSError("no slicer executable configured")
    path = Path(executable)
    if not path.exists():
        raise OSError("slicer executable not found: {}".format(executable))
    if sys.platform == "darwin" and (path.is_dir() or path.suffix.lower() == ".app"):
        command = ["open", "-a", str(path), str(file_path)]
    elif path.is_dir():
        raise OSError("slicer path is a folder, not a program: {}".format(executable))
    else:
        command = [str(path), str(file_path)]
    kwargs: Dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)
    return True


class _PrepareFailed(Exception):
    """Printer preparation failed; ``extra`` describes the 3MF that was kept."""

    def __init__(self, message: str, extra: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.extra = dict(extra)


def _handle_export(data: Mapping[str, Any], palette: Any) -> None:
    """Export the design, then prepare the printer project and open the slicer.

    The Fusion export runs here on Fusion's UI thread. Preparing the project
    with the slicer CLI (up to minutes), attaching the printer slot manifest
    (network) and launching the slicer run in the background
    (:func:`_finish_export_files`); :func:`_report_export` then saves the
    settings and answers the palette.

    The base 3MF always carries the per-object material metadata but no partial
    ``project_settings.config`` (that partial file made slicers show an
    "Untitled" printer). With a printer target the slicer CLI builds a
    complete project from the target's machine, process and filament presets.
    """
    backups: List[Any] = []
    name_backups: List[Any] = []
    detected: Sequence[Any] = []
    try:
        from . import exporter
        from . import materials_workflow
        from . import slicer_profiles

        output_path = str(data.get("output_path") or "").strip()
        if not output_path:
            _send_error(palette, "exportResult", "Choose an output .3mf path first.")
            return
        target_data = data.get("target") if isinstance(data.get("target"), dict) else {}
        prepare_enabled = bool(target_data.get("prepare", False))
        orca = data.get("orca") if isinstance(data.get("orca"), dict) else {}
        settings = load_settings()
        incoming_slots = data.get("bambuddy_slot_map")
        if isinstance(incoming_slots, dict):
            settings["bambuddy_slot_map"] = incoming_slots
        try:
            incoming_printer = int(data.get("bambuddy_printer_id") or 0)
        except (TypeError, ValueError):
            incoming_printer = 0
        if incoming_printer:
            settings["bambuddy_printer_id"] = incoming_printer

        design = _active_design()
        if design is None:
            _send_error(palette, "exportResult", "Open a Fusion design before exporting.")
            return

        entries = _load_catalog_entries()
        # Fusion writes only body names into the 3MF, so duplicate names (two
        # "Body1" in different components) are made unique until the export ends.
        name_backups = materials_workflow.apply_unique_export_names(design)
        detected = materials_workflow.collect_assembly_materials(design, entries)
        if not detected:
            _send_error(
                palette,
                "exportResult",
                "No materials were detected in the assembly. Assign Filamatch "
                "materials to your bodies/components and click Refresh, then export.",
            )
            return

        mapping = materials_workflow.build_mapping(detected)
        # Print settings set on single bodies, by their (now unique) names, with
        # infill types under the chosen slicer's names.
        object_settings = _body_print_settings(design, slicer_profiles.slicer_kind(
            str(orca.get("path") or settings.get("slicer_path") or settings.get("orca_path") or "")))
        if object_settings:
            log_message("Own print settings on {} body(ies): {}".format(
                len(object_settings), ", ".join(sorted(object_settings))))
        source_appearance = materials_workflow.find_source_appearance(design, _active_app())
        output = Path(output_path).expanduser()
        base_output = (
            output.with_name(output.stem + ".base.3mf") if prepare_enabled else output
        )
        log_message(
            "Exporting {} object(s) with {} detected material(s) to {!r}{}".format(
                len(mapping),
                len(detected),
                str(output),
                " (printer preparation enabled)" if prepare_enabled else "",
            )
        )
        try:
            backups = materials_workflow.apply_export_appearances(
                design, detected, source_appearance=source_appearance
            )
            base_result = exporter.export_and_inject(
                design=design,
                output_path=base_output,
                mapping=mapping,
                filaments=None,
                write_project_settings=False,
                object_settings=object_settings,
            )
        except exporter.InjectionFailedError as exc:
            LOGGER.exception("Export succeeded but material metadata injection failed")
            _send_error(
                palette,
                "exportResult",
                "The 3MF was exported but material metadata injection failed: {}".format(
                    _error_text(exc)
                ),
                raw_export_path=str(getattr(exc, "raw_export_path", base_output)),
                output_path=str(getattr(exc, "raw_export_path", base_output)),
                materials_used=_materials_used_payload(detected, None),
            )
            return
        except Exception as exc:
            LOGGER.exception("Automatic material export failed")
            _send_error(
                palette,
                "exportResult",
                _error_text(exc),
                materials_used=_materials_used_payload(detected, None),
            )
            return
        finally:
            materials_workflow.restore_appearances(backups)
            backups = []
            materials_workflow.restore_body_names(name_backups)
            name_backups = []
    except Exception as exc:
        LOGGER.exception("Palette export action failed")
        try:
            from . import materials_workflow

            materials_workflow.restore_appearances(backups)
        except Exception:
            LOGGER.exception("Could not restore appearances after export failure")
        _send_error(
            palette,
            "exportResult",
            _error_text(exc),
            materials_used=_materials_used_payload(detected, None),
        )
        return
    finally:
        if name_backups:
            try:
                from . import materials_workflow

                materials_workflow.restore_body_names(name_backups)
            except Exception:
                LOGGER.exception("Could not restore body names after export")

    skipped = list(getattr(base_result, "unmatched_names", None) or [])
    if skipped:
        log_message(
            "Not in the 3MF, so left out (hidden or not solid?): {}".format(", ".join(skipped))
        )
    job: Dict[str, Any] = {
        "data": data,
        "settings": settings,
        "target_data": target_data,
        "orca": orca,
        "prepare_enabled": prepare_enabled,
        "output": output,
        "base_output": base_output,
        "detected": list(detected),
        "mapped_count": base_result.mapped_count,
        "materials_used": _materials_used_payload(detected, base_result),
    }
    _run_job(
        "export",
        lambda: _finish_export_files(job),
        lambda outcome, error: _report_export(job, outcome, error, palette),
    )


def _wants_slicer_launch(orca: Mapping[str, Any], settings: Mapping[str, Any]) -> bool:
    """The palette's choice for this export wins; the saved setting is the fallback."""
    if "enabled" in orca:
        return bool(orca.get("enabled"))
    return bool(settings.get("orca_enabled"))


def _finish_export_files(job: Mapping[str, Any]) -> Dict[str, Any]:
    """Prepare the project, attach the slot manifest and open the slicer.

    Runs on a worker thread: files, subprocesses and network only, no Fusion
    API. Raises :class:`_PrepareFailed` when printer preparation fails (the
    material-tagged 3MF is kept and described in its ``extra``).
    """
    data, settings = job["data"], job["settings"]
    target_data, orca = job["target_data"], job["orca"]
    output, base_output, detected = job["output"], job["base_output"], job["detected"]
    outcome: Dict[str, Any] = {
        "prepared": False,
        "printer_name": "",
        "process_name": "",
        "log_tail": "",
        "prepared_target": None,
        "slot_manifest": None,
        "orca_launched": False,
        "orca_error": "",
        "estimate_slicer": None,
        "resolved_slicer_path": str(
            target_data.get("slicer_path")
            or settings.get("slicer_path")
            or settings.get("orca_path")
            or ""
        ).strip(),
    }
    prepare = bool(job["prepare_enabled"])
    open_app = str(
        orca.get("executable") or outcome["resolved_slicer_path"] or ""
    ).strip()
    if prepare and open_app and _slicer_kind(open_app) not in ("orca", "bambu", "prusa"):
        # Prepared projects are for Bambu Studio, OrcaSlicer and PrusaSlicer only.
        log_message(
            "{} cannot open prepared projects; sending the plain 3MF.".format(
                _slicer_display_name(open_app)
            )
        )
        prepare = False
        if base_output != output:
            shutil.move(str(base_output), str(output))
    # Read once: the prepared project names the loaded spools, the manifest records them.
    try:
        slot_rows = _read_slot_rows(detected, settings)
    except Exception:
        LOGGER.exception("Could not read the printer's slots")
        log_message("Bambuddy slot manifest was not attached (export continued)")
        slot_rows = None
    if prepare:
        try:
            slicer_profiles, slicer_exe, prepared_target = _resolve_profile_target(data)
            # slicer_exe prepares (OrcaSlicer); the saved slicer stays the one the user picked.
            outcome["resolved_slicer_path"] = str(outcome["resolved_slicer_path"] or slicer_exe)
            log_message(
                "Preparing project for {!r} / {!r} via {!r}".format(
                    prepared_target.printer_name, prepared_target.process_name, slicer_exe
                )
            )
            prepared_result = slicer_profiles.prepare_project(
                base_output,
                output,
                prepared_target,
                slicer_exe,
                _with_spool_presets(_with_database_data(detected), slot_rows),
            )
            _report_built_presets(
                getattr(prepared_result, "built_presets", ()) or (),
                _slicer_kind(open_app) or _slicer_kind(slicer_exe),
                bool(settings.get("save_slicer_presets", False)),
            )
            _apply_printer_nozzles(output, slot_rows)
            if _slicer_kind(open_app) == "bambu" and _slicer_kind(slicer_exe) != "bambu":
                # OrcaSlicer prepared this file for Bambu Studio.
                _use_bambu_studio_words(output, prepared_target)
            outcome.update(
                prepared=True,
                prepared_target=prepared_target,
                estimate_slicer=slicer_exe,
                printer_name=str(getattr(prepared_result, "printer_name", "") or ""),
                process_name=str(getattr(prepared_result, "process_name", "") or ""),
                log_tail=str(getattr(prepared_result, "log_tail", "") or "")[-4000:],
            )
            if base_output != output:
                try:
                    base_output.unlink()
                except OSError:
                    LOGGER.debug("Could not remove the intermediate base 3MF", exc_info=True)
        except Exception as exc:
            LOGGER.exception("Printer preparation failed")
            preserved = output
            try:
                shutil.copy2(str(base_output), str(output))
                if base_output != output:
                    base_output.unlink()
            except Exception:
                preserved = base_output
                LOGGER.exception("Could not preserve the base 3MF after prepare failure")
            extra: Dict[str, Any] = {
                "prepared": False,
                "base_export_path": str(preserved),
                "output_path": str(preserved),
                "materials_used": _materials_used_payload(detected, None),
            }
            log_file_text = str(getattr(exc, "log_tail", "") or "")
            if log_file_text:
                extra["log_tail"] = log_file_text[-4000:]
            raise _PrepareFailed(
                "Printer preparation failed: {} (the material-tagged 3MF without a "
                "printer profile was kept at {})".format(_error_text(exc), preserved),
                extra,
            ) from exc
    else:
        # prepare_project checks prepared projects; the plain 3MF gets the same check.
        from . import slicer_profiles

        slicer_profiles.check_slot_assignments(output, detected)

    try:
        if slot_rows is not None:
            outcome["slot_manifest"] = _attach_bambuddy_slot_manifest(
                output, detected, settings, pin_nozzles=bool(outcome["prepared"]), slot_rows=slot_rows
            )
    except Exception:
        LOGGER.exception("Unexpected slot-manifest failure during export")

    if not outcome["prepared"] and _is_prusa_family(open_app):
        _write_prusa_layout(output)

    if _wants_slicer_launch(orca, settings):
        executable = str(
            orca.get("executable")
            or target_data.get("slicer_path")
            or settings.get("slicer_path")
            or settings.get("orca_path")
            or ""
        ).strip()
        try:
            outcome["orca_launched"] = _launch_orca(executable, output)
            log_message("Launched slicer: {}".format(executable))
        except Exception as exc:
            outcome["orca_error"] = _error_text(exc)
            log_message("Could not launch slicer: {}".format(outcome["orca_error"]))
    return outcome


def _report_export(
    job: Mapping[str, Any], outcome: Any, error: Optional[BaseException], palette: Any
) -> None:
    """Save the export choices and answer the palette (Fusion UI thread)."""
    if isinstance(error, _PrepareFailed):
        _send_error(palette, "exportResult", str(error), **error.extra)
        return
    if error is not None:
        _send_error(
            palette, "exportResult", _error_text(error), materials_used=job["materials_used"]
        )
        return
    settings, target_data, orca = job["settings"], job["target_data"], job["orca"]
    output = job["output"]
    prepared_target = outcome["prepared_target"]
    updates: Dict[str, Any] = {
        "output_path": str(output),
        "slicer_path": outcome["resolved_slicer_path"],
        "prepare_enabled": job["prepare_enabled"],
        "target_vendor": str(
            (prepared_target.vendor if prepared_target else "")
            or target_data.get("vendor")
            or settings.get("target_vendor")
            or ""
        ),
        "target_printer": str(
            (prepared_target.printer_name if prepared_target else "")
            or target_data.get("printer")
            or settings.get("target_printer")
            or ""
        ),
        "target_process": str(
            (prepared_target.process_name if prepared_target else "")
            or target_data.get("process")
            or settings.get("target_process")
            or ""
        ),
    }
    if "enabled" in orca:
        updates["orca_enabled"] = bool(orca.get("enabled"))
    # An empty value in this request must not wipe a saved path.
    orca_path = str(orca.get("executable") or target_data.get("slicer_path") or "").strip()
    if orca_path:
        updates["orca_path"] = orca_path
    if "profile_dir" in orca:
        updates["orca_profile_dir"] = str(orca.get("profile_dir") or "")
    try:
        settings = save_settings(updates)
    except Exception:
        LOGGER.exception("Could not persist palette settings after export")

    slot_manifest = outcome["slot_manifest"] or {}
    payload: Dict[str, Any] = {
        "ok": True,
        "output_path": str(output),
        "prepared": outcome["prepared"],
        "printer_name": outcome["printer_name"],
        "process_name": outcome["process_name"],
        "materials_used": job["materials_used"],
        "mapped_count": job["mapped_count"],
        "project_settings_written": outcome["prepared"],
        "filament_count": len(job["detected"]),
        "orca_launched": outcome["orca_launched"],
        "settings": public_settings(settings),
        "bambuddy_slots": slot_manifest.get("materials", []),
        "bambuddy_printer": slot_manifest.get("printer_name", ""),
    }
    if outcome["log_tail"]:
        payload["log_tail"] = outcome["log_tail"]
    if outcome["orca_error"]:
        payload["orca_error"] = outcome["orca_error"]
    if not outcome["prepared"]:
        payload["warning"] = (
            "No printer profile was applied. Choose a Target Printer in the palette "
            "to export a fully configured slicer project."
        )
    log_message(
        "Export complete: {!r} ({} materials, {} objects, prepared={}, printer={!r})".format(
            str(output),
            len(job["detected"]),
            job["mapped_count"],
            outcome["prepared"],
            outcome["printer_name"],
        )
    )
    estimate_slicer = outcome.get("estimate_slicer") if outcome["prepared"] else None
    if not bool(settings.get("estimate_enabled", True)):
        estimate_slicer = None
    payload["estimating"] = bool(estimate_slicer)
    _send(palette, "exportResult", payload)
    if estimate_slicer:
        _start_print_estimate(palette, output, estimate_slicer, job["detected"], outcome["slot_manifest"])


#: The spool check wants this much more filament on a spool than the estimate.
SPOOL_MARGIN = 0.10


def _spool_check(
    detected: Sequence[Any], manifest: Optional[Mapping[str, Any]], filament_grams: Sequence[Any]
) -> List[Dict[str, Any]]:
    """Compare each filament's estimated grams with the grams left on its spool.

    One row per filament whose spool weight is known (from Bambuddy, or the
    printer for RFID spools): name, slot, grams needed and left, and
    ``enough`` (left covers the estimate plus :data:`SPOOL_MARGIN`).  Loaded
    spools of the same filament on the same nozzle are counted as backups
    (``backup_g``, ``backup_count``, ``enough_with_backup``): an AMS with
    auto refill switches to them when the first spool runs out.
    """
    if not manifest:
        return []
    rows_by_material = {
        str(row.get("fusion_material_id")): row for row in manifest.get("materials") or []
    }
    left_by_slot = {
        str(entry.get("slot_key")): entry.get("remaining_g") for entry in manifest.get("filaments") or []
    }
    checks: List[Dict[str, Any]] = []
    for index, material in enumerate(detected):
        if index >= len(filament_grams):
            break
        row = rows_by_material.get(str(getattr(material, "material_id", "")))
        left = left_by_slot.get(str(row.get("slot_key"))) if row else None
        if left is None:
            continue
        needed = float(filament_grams[index] or 0.0)
        wanted = needed * (1.0 + SPOOL_MARGIN)
        known_backups = [
            float(backup["remaining_g"])
            for backup in row.get("backup_slots") or []
            if backup.get("remaining_g") is not None
        ]
        checks.append(
            {
                "display_name": str(getattr(material, "display_name", "") or ""),
                "slot_label": str(row.get("slot_label") or ""),
                "needed_g": round(needed, 1),
                "left_g": round(float(left), 1),
                "enough": float(left) >= wanted,
                "backup_count": len(known_backups),
                "backup_g": round(sum(known_backups), 1),
                "enough_with_backup": float(left) + sum(known_backups) >= wanted,
            }
        )
    return checks


def _start_print_estimate(
    palette: Any,
    project: Path,
    slicer_exe: Any,
    detected: Sequence[Any] = (),
    manifest: Optional[Mapping[str, Any]] = None,
) -> None:
    """Slice a copy in the background, then send ``printEstimate`` (time, grams, spool check)."""
    from . import slicer_profiles

    def finish(result: Any, error: Optional[BaseException]) -> None:
        if error is not None or not result:
            log_message("No print estimate: {}".format(_error_text(error) if error else "none reported"))
            _send(palette, "printEstimate", {"ok": False, "output_path": str(project)})
            return
        spools = _spool_check(detected, manifest, result.get("filament_grams") or [])
        log_message("Print estimate: {} min, {} g, {} plate(s), filament cost {}".format(
            result.get("minutes"), result.get("grams"), result.get("plates", 1), result.get("cost")))
        for spool in spools:
            if not spool["enough"]:
                log_message(
                    "{} may run out: needs {} g, {} g left in {} (+{} g on {} backup spool(s))".format(
                        spool["display_name"], spool["needed_g"], spool["left_g"], spool["slot_label"],
                        spool["backup_g"], spool["backup_count"],
                    )
                )
        _send(
            palette,
            "printEstimate",
            dict(
                result, ok=True, output_path=str(project), spools=spools,
                auto_refill=(manifest or {}).get("auto_refill"),
            ),
        )

    _run_job("estimate", lambda: slicer_profiles.estimate_print(project, slicer_exe), finish)


def _set_dialog_property(dialog: Any, name: str, value: Any) -> None:
    """Best-effort setter for a FileDialog property (platform differences)."""
    try:
        setattr(dialog, name, value)
    except Exception:
        LOGGER.debug("Could not set FileDialog.%s to %r", name, value, exc_info=True)


def _create_file_dialog(title: str) -> Any:
    """Create a Fusion FileDialog for the active application.

    Args:
        title: Dialog window title.

    Returns:
        An ``adsk.core.FileDialog`` instance.

    Raises:
        RuntimeError: If the Fusion user interface is unavailable.
    """
    app = _active_app()
    if app is None:
        raise RuntimeError("Fusion is not available; cannot open a file dialog.")
    dialog = app.userInterface.createFileDialog()
    _set_dialog_property(dialog, "title", title)
    return dialog


def _handle_browse_export_path(data: Mapping[str, Any], palette: Any) -> None:
    """Open Fusion's native save dialog and return the chosen .3mf path."""
    try:
        current_path = str(data.get("current_path") or "").strip()
        dialog = _create_file_dialog("Choose 3MF export file")
        _set_dialog_property(dialog, "filter", "3MF files (*.3mf);;All files (*)")
        current = Path(current_path).expanduser() if current_path else None
        if current is not None and current.name:
            _set_dialog_property(dialog, "initialFilename", current.name)
        if current is not None and current.parent.is_dir():
            _set_dialog_property(dialog, "initialDirectory", str(current.parent))
        else:
            _set_dialog_property(dialog, "initialDirectory", str(Path.home()))
        result = dialog.showSave()
        if result == adsk.core.DialogResults.DialogOK:
            chosen = str(dialog.filename or "").strip()
            if not chosen:
                _send(
                    palette,
                    "exportPathData",
                    {"ok": False, "cancelled": True, "path": current_path},
                )
                return
            _send(palette, "exportPathData", {"ok": True, "path": chosen})
            log_message("Export path selected: {!r}".format(chosen))
        else:
            _send(
                palette,
                "exportPathData",
                {"ok": False, "cancelled": True, "path": current_path},
            )
    except Exception as exc:
        LOGGER.exception("File dialog for the export path failed")
        _send(palette, "exportPathData", {"ok": False, "error": _error_text(exc)})


def _handle_browse_slicer_path(data: Mapping[str, Any], palette: Any) -> None:
    """Open Fusion's native open dialog and return a slicer executable path."""
    try:
        current_path = str(data.get("current_path") or "").strip()
        dialog = _create_file_dialog("Choose slicer executable")
        initial_directory = str(Path.home())
        if sys.platform == "darwin":
            _set_dialog_property(dialog, "filter", "Applications (*.app);;All files (*)")
            if Path("/Applications").is_dir():
                initial_directory = "/Applications"
        elif os.name == "nt":
            _set_dialog_property(dialog, "filter", "Executables (*.exe);;All files (*)")
            program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
            if Path(program_files).is_dir():
                initial_directory = program_files
        else:
            _set_dialog_property(dialog, "filter", "All files (*)")
        current = Path(current_path).expanduser() if current_path else None
        if current is not None and current.name:
            _set_dialog_property(dialog, "initialFilename", current.name)
            if current.parent.is_dir():
                initial_directory = str(current.parent)
        _set_dialog_property(dialog, "initialDirectory", initial_directory)
        result = dialog.showOpen()
        if result == adsk.core.DialogResults.DialogOK:
            chosen = str(dialog.filename or "").strip()
            if not chosen:
                _send(
                    palette,
                    "slicerPathData",
                    {"ok": False, "cancelled": True, "path": current_path},
                )
                return
            _send(palette, "slicerPathData", {"ok": True, "path": chosen})
            log_message("Slicer path selected: {!r}".format(chosen))
        else:
            _send(
                palette,
                "slicerPathData",
                {"ok": False, "cancelled": True, "path": current_path},
            )
    except Exception as exc:
        LOGGER.exception("File dialog for the slicer path failed")
        _send(palette, "slicerPathData", {"ok": False, "error": _error_text(exc)})


def _handle_diagnostics(palette: Any) -> None:
    """Run the add-in diagnostics and return the report text."""
    try:
        try:
            from . import diagnostics

            text = diagnostics.run_diagnostics()
        except Exception:
            LOGGER.exception("Diagnostics module unavailable")
            text = "Diagnostics failed:\n" + traceback.format_exc()
        _send(palette, "diagnosticsResult", {"ok": True, "text": text})
    except Exception as exc:
        LOGGER.exception("Palette diagnostics action failed")
        _send_error(palette, "diagnosticsResult", _error_text(exc))


def _handle_save_printer_report(palette: Any) -> None:
    """Save the connected printer's raw status with private values removed (worker thread)."""
    settings = load_settings()

    def work() -> Dict[str, Any]:
        from . import printer_report

        report = printer_report.collect(settings)
        path = printer_report.save(report)
        return {"path": str(path), "printers": len(report.get("printers") or [])}

    def finish(result: Optional[Dict[str, Any]], error: Optional[BaseException]) -> None:
        if error is not None or result is None:
            LOGGER.warning("Could not save the printer report: %s", _error_text(error))
            _send_error(palette, "printerReportResult", _error_text(error))
            return
        log_message("Saved the printer report: {}".format(result["path"]))
        _send(palette, "printerReportResult", dict(result, ok=True))

    _run_job("printer-report", work, finish)


def _body_print_settings(design: Any, slicer_kind: str = "") -> Dict[str, Dict[str, str]]:
    """Print settings of single bodies by body name, as Bambu Studio / OrcaSlicer keys."""
    from . import materials_workflow

    found: Dict[str, Dict[str, str]] = {}
    for _material_id, body in materials_workflow.iter_placed_bodies(design):
        settings = body_settings.read_body(body)
        if settings:
            found[str(getattr(body, "name", "") or "")] = body_settings.to_bambu(settings, slicer_kind)
    return found


def _handle_set_body_settings(payload: Mapping[str, Any], palette: Any) -> None:
    """Store print settings on bodies by entity token (``bodySettingsSaved``); empty settings clear them."""
    try:
        settings = body_settings.normalize(payload.get("settings") or {})
    except ValueError as exc:  # A value the user typed: say what is wrong, no traceback.
        _send_error(palette, "bodySettingsSaved", str(exc).capitalize() + ".")
        return
    try:
        design = _active_design()
        if design is None:
            raise RuntimeError("Open a Fusion design first.")
        count = 0
        for token in payload.get("tokens") or []:
            found = design.findEntityByToken(str(token))
            if found:
                body_settings.write_body(found[0], settings)
                count += 1
        summary = body_settings.describe(settings) or "the print process settings"
        log_message("Print settings for {} body(ies): {}".format(count, summary))
        _send(palette, "bodySettingsSaved", {
            "ok": True,
            "count": count,
            "settings": settings,
            "materials": _materials_payload(design, _load_catalog_entries()),
        })
    except Exception as exc:
        LOGGER.exception("Palette set_body_settings failed")
        _send_error(palette, "bodySettingsSaved", _error_text(exc))


def _handle_select_bodies(payload: Mapping[str, Any], palette: Any) -> None:
    """Select bodies in Fusion (``bodiesSelected`` replies): all bodies of the
    ``material_ids``, or the bodies with the ``tokens`` (every placed copy)."""
    from . import materials_workflow

    wanted = {str(value) for value in payload.get("material_ids") or [] if str(value)}
    tokens = {str(value) for value in payload.get("tokens") or [] if str(value)}
    try:
        app = _active_app()
        ui = getattr(app, "userInterface", None) if app is not None else None
        if ui is None:
            raise RuntimeError("Fusion's user interface is not available.")
        selections = ui.activeSelections
        selections.clear()
        count = 0
        for material_id, body in materials_workflow.iter_placed_bodies(_active_design()):
            native = getattr(body, "nativeObject", None) or body
            if material_id not in wanted and str(getattr(native, "entityToken", "") or "") not in tokens:
                continue
            try:
                selections.add(body)
                count += 1
            except Exception:  # A hidden body cannot be selected.
                LOGGER.debug("Could not select a body", exc_info=True)
        log_message("Selected {} body(ies) in Fusion".format(count))
        _send(palette, "bodiesSelected", {"ok": True, "count": count})
    except Exception as exc:
        LOGGER.exception("Palette select_bodies failed")
        _send_error(palette, "bodiesSelected", _error_text(exc))


def _handle_open_material_browser(palette: Any) -> None:
    """Open Fusion's Physical Material browser so the user can assign materials."""
    try:
        app = _active_app()
        if app is None:
            raise RuntimeError("The Fusion application is not available.")
        ui = getattr(app, "userInterface", None)
        if ui is None:
            raise RuntimeError("Fusion's user interface is not available.")
        definitions = getattr(ui, "commandDefinitions", None)
        if definitions is None:
            raise RuntimeError("Fusion's command definitions are not available.")
        command = None
        for command_id in ("PhysicalMaterialCommand", "MaterialCommand"):
            try:
                candidate = definitions.itemById(command_id)
            except Exception:
                candidate = None
            if candidate is not None:
                command = candidate
                break
        if command is None:
            raise RuntimeError(
                "Fusion did not expose the Physical Material command. Open Physical Material "
                "from the Fusion toolbar instead."
            )
        # Acknowledge first: executing the native command opens a modal Material
        # Browser and can block the palette event loop until it closes, which
        # previously made the UI report a 20 s timeout even though the command
        # had launched.
        _send(palette, "materialBrowserData", {"ok": True})
        try:
            command.execute()
        except Exception:
            LOGGER.exception("Fusion rejected the Physical Material command after acknowledgement")
        else:
            log_message("Opened the Fusion Physical Material browser")
    except Exception as exc:
        LOGGER.exception("Could not open the Fusion Material Browser")
        _send_error(palette, "materialBrowserData", _error_text(exc))


def _handle_save_settings(data: Mapping[str, Any], palette: Any) -> None:
    """Persist palette settings and acknowledge them.

    The LAN printer list is changed only by its own actions: the palette holds
    a public copy without serials and access codes.
    """
    try:
        changes = {key: value for key, value in data.items() if key != bambu_lan.SETTINGS_KEY}
        settings = save_settings(changes)
        log_message("Settings saved")
        _send(palette, "settingsSaved", _settings_reply(settings))
    except Exception as exc:
        LOGGER.exception("Could not save palette settings")
        _send_error(palette, "settingsSaved", _error_text(exc))


def _handle_clear_bambuddy_key(palette: Any) -> None:
    """Forget the saved Bambuddy API key (keychain and settings file)."""
    try:
        from . import secret_store

        secret_store.delete_secret("bambuddy_api_key")
        settings = save_settings({"bambuddy_api_key": ""})
        settings["bambuddy_api_key"] = ""
        log_message("Removed the saved Bambuddy API key")
        _send(palette, "settingsSaved", {"ok": True, "settings": public_settings(settings)})
    except Exception as exc:
        LOGGER.exception("Could not remove the Bambuddy API key")
        _send_error(palette, "settingsSaved", _error_text(exc))


def _settings_reply(settings: Mapping[str, Any]) -> Dict[str, Any]:
    from . import printer_sources

    return {
        "ok": True,
        "settings": public_settings(settings),
        "printer_configured": printer_sources.is_configured(settings),
    }


def _handle_set_printer_source(data: Mapping[str, Any], palette: Any) -> None:
    """Select Bambuddy or the LAN printers as the slot source."""
    from . import printer_sources

    try:
        source = str(data.get("source") or "").strip().lower()
        if source not in {info.source_id for info in printer_sources.registered_sources()}:
            raise ValueError("Unknown printer source: {!r}".format(source))
        settings = save_settings({printer_sources.SETTINGS_KEY: source})
        log_message("Printer source: {}".format(source))
        _send(palette, "settingsSaved", _settings_reply(settings))
    except Exception as exc:
        LOGGER.exception("Could not change the printer source")
        _send_error(palette, "settingsSaved", _error_text(exc))


def _lan_request(data: Mapping[str, Any]) -> Tuple[str, str, str]:
    """``(ip, access code, fingerprint to trust)`` from a palette request.

    An empty access code reuses the saved code of the printer at that address,
    so editing a printer does not need the code again.
    """
    ip = str(data.get("ip") or "").strip()
    code = str(data.get("access_code") or "").strip()
    if not code:
        for printer in bambu_lan.printers_from_settings(load_settings()):
            if printer.ip == ip:
                code = printer.access_code
                break
    return ip, code, str(data.get("trust_fingerprint") or "").strip()


def _lan_error_payload(error: BaseException) -> Dict[str, Any]:
    """Palette form of a failed printer test; asks for trust when that helps."""
    if isinstance(error, bambu_lan.UntrustedCertificate):
        return {
            "ok": False,
            "needs_trust": True,
            "fingerprint": error.fingerprint,
            "serial_tail": error.serial[-4:],
            "error": str(error),
        }
    if isinstance(error, bambu_lan.BambuLanError):
        return {"ok": False, "error": str(error)}
    LOGGER.error("Bambu printer test failed", exc_info=error)
    return {"ok": False, "error": _error_text(error)}


def _handle_bambu_lan_test(data: Mapping[str, Any], palette: Any) -> None:
    """Read one printer (in the background) and show what it has loaded."""
    try:
        ip, code, trust = _lan_request(data)
    except Exception as exc:
        LOGGER.exception("Could not start the printer test")
        _send(palette, "bambuLanTestResult", {"ok": False, "error": _error_text(exc)})
        return

    def finish(result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            _send(palette, "bambuLanTestResult", _lan_error_payload(error))
            return
        log_message(
            "Bambu printer test: {} at {}, {} slot(s), certificate {}".format(
                result.model, ip, len(result.slots), result.certificate
            )
        )
        _send(palette, "bambuLanTestResult", result.public())

    _run_job("printer-test", lambda: bambu_lan.probe(ip, code, trust), finish)


def _lan_printers_reply(settings: Mapping[str, Any], **extra: Any) -> Dict[str, Any]:
    reply = _settings_reply(settings)
    reply["printers"] = _lan_printers_public(settings)
    reply.update(extra)
    return reply


def _handle_bambu_lan_save(data: Mapping[str, Any], palette: Any) -> None:
    """Test a printer again, then save it and select the LAN source.

    The access code goes to the keychain; nothing is saved when the test fails.
    """
    try:
        ip, code, trust = _lan_request(data)
        name = str(data.get("name") or "").strip()
    except Exception as exc:
        LOGGER.exception("Could not start saving the printer")
        _send_error(palette, "bambuLanPrinters", _error_text(exc))
        return

    def finish(result: Any, error: Optional[BaseException]) -> None:
        from . import printer_sources

        if error is not None:
            reply = _lan_printers_reply(load_settings(), **_lan_error_payload(error))
            _send(palette, "bambuLanPrinters", reply)
            return
        try:
            printer = bambu_lan.LanPrinter(
                id=bambu_lan.printer_id_for_serial(result.serial),
                name=name or result.model,
                model=result.model,
                ip=ip,
                serial=result.serial,
                fingerprint=result.fingerprint if result.certificate == "pinned" else "",
                access_code=code,
            )
            current = load_settings()
            kept = [
                entry.to_settings()
                for entry in bambu_lan.printers_from_settings(current)
                if entry.serial != printer.serial and entry.ip != printer.ip
            ]
            settings = save_settings(
                {
                    bambu_lan.SETTINGS_KEY: kept + [printer.to_settings()],
                    printer_sources.SETTINGS_KEY: bambu_lan.SOURCE_ID,
                }
            )
            log_message("Saved Bambu printer {} ({}) at {}".format(printer.name, printer.model, ip))
            _send(palette, "bambuLanPrinters", _lan_printers_reply(settings))
        except Exception as exc:
            LOGGER.exception("Could not save the Bambu printer")
            _send(palette, "bambuLanPrinters", _lan_printers_reply(load_settings(), ok=False, error=_error_text(exc)))

    _run_job("printer-save", lambda: bambu_lan.probe(ip, code, trust), finish)


def _handle_bambu_lan_remove(data: Mapping[str, Any], palette: Any) -> None:
    """Forget one LAN printer and its access code."""
    try:
        from . import secret_store

        try:
            printer_id = int(data.get("id") or 0)
        except (TypeError, ValueError):
            printer_id = 0
        current = load_settings()
        printers = bambu_lan.printers_from_settings(current)
        removed = [printer for printer in printers if printer.id == printer_id]
        for printer in removed:
            secret_store.delete_secret(bambu_lan.secret_account(printer.serial))
            log_message("Removed Bambu printer {}".format(printer.name))
        settings = save_settings(
            {
                bambu_lan.SETTINGS_KEY: [
                    printer.to_settings() for printer in printers if printer.id != printer_id
                ]
            }
        )
        bambu_lan.clear_cache()
        _send(palette, "bambuLanPrinters", _lan_printers_reply(settings))
    except Exception as exc:
        LOGGER.exception("Could not remove the Bambu printer")
        _send_error(palette, "bambuLanPrinters", _error_text(exc))


def _handle_get_log(palette: Any) -> None:
    """Send the current session log lines."""
    _send(palette, "logData", {"ok": True})


def _fit_palette_height(payload: Mapping[str, Any], palette: Any) -> None:
    """Fit the palette height to the page content (fire-and-forget, no reply).

    ``viewport`` is the page's own height, so the difference to the palette
    height is Fusion's title and button bars. Fusion ignores the new height
    while the palette is docked.
    """
    try:
        content = int(payload.get("content") or 0)
        viewport = int(payload.get("viewport") or 0)
    except (TypeError, ValueError):
        return
    if content <= 0 or viewport <= 0:
        return
    try:
        current = int(palette.height)
        wanted = max(PALETTE_MIN_HEIGHT, min(PALETTE_HEIGHT, content + max(0, current - viewport)))
        if abs(wanted - current) > 8:
            palette.height = wanted
    except Exception:
        LOGGER.debug("Could not fit the palette height", exc_info=True)


def dispatch(action: str, data: Any, palette: Any) -> None:
    """Dispatch one incoming palette action to its handler.

    ``response`` messages are the return values Fusion sends back for every
    ``sendInfoToHTML`` call; they are ignored to avoid a feedback loop. Unknown
    actions are logged but never answered (a reply could also loop).
    """
    payload = _parse_payload(data)
    if action == "response":
        return
    LOGGER.debug("Palette action %r", action)
    if action == "init":
        _handle_init(palette)
    elif action == "import_selected":
        _handle_import(payload, palette)
    elif action == "refresh_materials":
        _handle_refresh_materials(palette)
    elif action == "design_check":
        _check_design_changed(palette)
    elif action == "fit_height":
        _fit_palette_height(payload, palette)
    elif action == "export_3mf":
        _handle_export(payload, palette)
    elif action == "browse_export_path":
        _handle_browse_export_path(payload, palette)
    elif action == "browse_slicer_path":
        _handle_browse_slicer_path(payload, palette)
    elif action == "get_profiles":
        _handle_get_profiles(payload, palette)
    elif action == "import_profiles":
        _handle_import_profiles(palette)
    elif action == "create_library":
        _handle_create_library(palette)
    elif action == "bambuddy_context":
        _handle_bambuddy_context(payload, palette)
    elif action == "bambuddy_import":
        _handle_bambuddy_import(payload, palette)
    elif action == "open_material_browser":
        _handle_open_material_browser(palette)
    elif action == "select_bodies":
        _handle_select_bodies(payload, palette)
    elif action == "use_loaded_filament":
        _handle_use_loaded_filament(payload, palette)
    elif action == "set_body_settings":
        _handle_set_body_settings(payload, palette)
    elif action == "openfilament_search":
        _handle_openfilament_search(payload, palette)
    elif action == "openfilament_import":
        _handle_openfilament_import(payload, palette)
    elif action == "run_diagnostics":
        _handle_diagnostics(palette)
    elif action == "save_printer_report":
        _handle_save_printer_report(palette)
    elif action == "clear_bambuddy_key":
        _handle_clear_bambuddy_key(palette)
    elif action == "save_settings":
        _handle_save_settings(payload, palette)
    elif action == "set_printer_source":
        _handle_set_printer_source(payload, palette)
    elif action == "bambu_lan_test":
        _handle_bambu_lan_test(payload, palette)
    elif action == "bambu_lan_save":
        _handle_bambu_lan_save(payload, palette)
    elif action == "bambu_lan_remove":
        _handle_bambu_lan_remove(payload, palette)
    elif action == "get_log":
        _handle_get_log(palette)
    elif not _dispatch_module(action, payload, palette):
        LOGGER.warning("Unknown palette action %r", action)
        log_message("Unknown palette action: {!r}".format(action))


def _dispatch_module(action: str, payload: Mapping[str, Any], palette: Any) -> bool:
    """Hand an action to another palette module (the As printed tab); True when it took it."""
    try:
        from . import as_printed_bridge
    except ImportError:
        return False
    try:
        return bool(as_printed_bridge.dispatch(action, payload, palette))
    except Exception:
        LOGGER.exception("The As printed tab failed on %r", action)
        log_message("The As printed tab failed on {!r}: {}".format(action, traceback.format_exc()))
        return True


# ---------------------------------------------------------------------------
# Palette lifecycle (Fusion only)
# ---------------------------------------------------------------------------


def show_palette(ui: Any = None) -> Any:
    """Create (or reveal) the add-in HTML palette and attach its event handler.

    Args:
        ui: Optional Fusion ``UserInterface``; resolved from the application
            when omitted.

    Returns:
        The Fusion ``Palette`` object.

    Raises:
        RuntimeError: When the Fusion API or the palette HTML is unavailable.
    """
    if adsk is None:
        raise RuntimeError("The adsk modules are not available outside Fusion 360")
    if not PALETTE_HTML.is_file():
        raise RuntimeError(
            "Palette HTML file is missing: {}\nThe add-in folder must include the "
            "'palette' directory (index.html, style.css, app.js).".format(PALETTE_HTML)
        )

    app = adsk.core.Application.get()
    user_interface = ui if ui is not None else app.userInterface
    palettes = user_interface.palettes
    if not os.environ.get(config.DATA_DIR_ENV, "").strip():
        # Before the page asks for anything that reads the data folder.
        migrate_legacy_data_dir()

    existing = palettes.itemById(PALETTE_ID)
    if existing is not None and not _PALETTE_HANDLERS:
        # Left over from an earlier load of the add-in: its page talks to a
        # handler this module does not own, so build a fresh palette.
        try:
            existing.deleteMe()
            existing = None
        except Exception as exc:
            raise RuntimeError(
                "An old Filamatch palette is still open and could not be closed. "
                "Close it, or restart Fusion."
            ) from exc
    if existing is not None:
        try:
            existing.isVisible = True
        except Exception:
            LOGGER.exception("Could not show the existing palette")
        return existing

    # Fusion's documented signature is:
    #   add(id, name, htmlFileURL, isVisible, showCloseButton, isResizable, width, height[, useNewWebBrowser])
    # Some builds use older argument orders, so several layouts are attempted.
    palette = None
    errors: List[str] = []
    argument_variants = [
        (True, True, True, PALETTE_WIDTH, PALETTE_HEIGHT),
        (True, True, PALETTE_WIDTH, PALETTE_HEIGHT, True),
        (True, True, PALETTE_WIDTH, PALETTE_HEIGHT),
    ]
    for html_url in (str(PALETTE_HTML), PALETTE_HTML.as_uri()):
        for extra_arguments in argument_variants:
            try:
                palette = palettes.add(PALETTE_ID, PALETTE_NAME, html_url, *extra_arguments)
                break
            except Exception as exc:
                errors.append("{} (args={}): {}".format(html_url, extra_arguments, exc))
                LOGGER.exception(
                    "palettes.add failed for %r with arguments %r", html_url, extra_arguments
                )
        if palette is not None:
            break
    if palette is None:
        raise RuntimeError("Could not create the palette: {}".format("; ".join(errors)))

    # Floating, so the page can fit the palette height to each screen
    # (Fusion ignores the height of a docked palette). Users can still dock it.
    try:
        palette.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateFloating
    except Exception:
        LOGGER.warning("Could not make the palette float", exc_info=True)

    handler = _PaletteIncomingHandler(palette)
    try:
        palette.incomingFromHTML.add(handler)
        _PALETTE_HANDLERS.append(handler)
    except Exception:
        LOGGER.exception("Could not attach the palette incoming-message handler")
        raise
    _start_design_watch(app, user_interface)

    log_message("Palette opened")
    return palette


def close_palette(ui: Any = None) -> None:
    """Delete the palette if it exists (called from the add-in ``stop``)."""
    if adsk is None:
        return
    try:
        app = adsk.core.Application.get()
        user_interface = ui if ui is not None else app.userInterface
        palette = user_interface.palettes.itemById(PALETTE_ID)
        if palette is not None:
            palette.deleteMe()
    except Exception:
        LOGGER.exception("Could not close the palette")
    finally:
        _stop_design_watch()
        _unregister_job_event()
        del _PALETTE_HANDLERS[:]


if adsk is not None:  # pragma: no branch - always true inside Fusion.

    class _PaletteIncomingHandler(adsk.core.HTMLEventHandler):  # type: ignore[misc]
        """Dispatch messages sent by ``adsk.fusionSendData`` in the palette."""

        def __init__(self, palette: Any) -> None:
            super().__init__()
            self._palette = palette

        def notify(self, args: Any) -> None:
            """Handle one incoming palette message without breaking the palette."""
            try:
                dispatch(args.action, args.data, self._palette)
            except Exception:
                LOGGER.exception("Unhandled palette message failure")

    class _JobFinishedHandler(adsk.core.CustomEventHandler):  # type: ignore[misc]
        """Finish a background job on Fusion's UI thread."""

        def notify(self, args: Any) -> None:
            try:
                _complete_job(str(getattr(args, "additionalInfo", "") or ""))
            except Exception:
                LOGGER.exception("Could not finish a background job")

    class _CommandTerminatedHandler(adsk.core.ApplicationCommandEventHandler):  # type: ignore[misc]
        """Re-read the design after a Fusion command ends (Physical Material, undo...)."""

        def notify(self, args: Any) -> None:
            try:
                if str(getattr(args, "commandId", "") or "") in _IGNORED_COMMANDS:
                    return
                palette = _visible_palette()
                if palette is not None:
                    _check_design_changed(palette)
            except Exception:
                LOGGER.exception("Design change check failed after a command")

    class _DocumentActivatedHandler(adsk.core.DocumentEventHandler):  # type: ignore[misc]
        """Re-read the design when another document tab becomes active."""

        def notify(self, args: Any) -> None:
            try:
                palette = _visible_palette()
                if palette is not None:
                    _check_design_changed(palette)
            except Exception:
                LOGGER.exception("Design change check failed after a document switch")
                print(traceback.format_exc())


# ---------------------------------------------------------------------------
# Pure-Python self-test (no Fusion required)
# ---------------------------------------------------------------------------

_PASSED = 0
_FAILED = 0


def _check(condition: bool, label: str) -> None:
    """Record and print one PASS/FAIL check."""
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print("PASS {}".format(label))
    else:
        _FAILED += 1
        print("FAIL {}".format(label))


class _FakePalette:
    """Minimal palette stand-in recording Python -> HTML replies."""

    def __init__(self) -> None:
        self.sent: List[Any] = []

    def sendInfoToHTML(self, action: str, data: str) -> bool:
        """Record one reply message."""
        try:
            self.sent.append((action, json.loads(data)))
        except Exception:
            self.sent.append((action, {}))
        return True


def _self_test() -> int:
    """Run the palette-bridge pure-Python self-test.

    Returns:
        Process exit code (0 success, 1 failure).
    """
    from . import catalog

    entries = catalog.load_catalog()
    _check(len(entries) == 5, "bundled catalog loads 5 entries")

    catalog_payload = _catalog_payload(entries)
    _check(len(catalog_payload) == 5, "catalog payload has 5 rows")
    _check(
        catalog_payload[0]["id"] == "bambu_pla_basic_black"
        and catalog_payload[0]["color_hex"] == "#161616"
        and catalog_payload[0]["category"] == "PLA",
        "catalog payload exposes palette fields including category",
    )

    categories = _categories_payload(entries)
    names = [category["name"] for category in categories]
    _check(
        names == ["PLA", "PETG", "ABS", "Fiber-Reinforced"],
        "categories are grouped in first-appearance order: {}".format(names),
    )
    _check(
        sum(len(category["filament_ids"]) for category in categories) == len(entries),
        "every catalog entry is assigned to exactly one category group",
    )
    by_name = {category["name"]: category["filament_ids"] for category in categories}
    _check(
        by_name["PLA"] == ["bambu_pla_basic_black", "prusament_pla_orange"],
        "PLA category holds the two PLA entries in catalog order",
    )

    fake_material = type(
        "FakeMaterial",
        (),
        {
            "material_id": "bambu_pla_basic_black",
            "material_name": "bambu_pla_basic_black",
            "display_name": "Bambu Lab PLA Basic - Black",
            "color_hex": "#161616",
            "filament_type": "PLA",
            "density_g_cm3": 1.24,
            "category": "PLA",
            "body_names": ("Body1", "Body2"),
            "slot": 1,
        },
    )()
    row = _detected_material_payload(fake_material)
    _check(
        row["slot"] == 1 and row["body_count"] == 2 and row["color_hex"] == "#161616",
        "detected material payload exposes slot/colour/body count",
    )
    _check(
        _materials_payload(None, entries) == [],
        "assembly materials are empty when no design is active",
    )

    with tempfile.TemporaryDirectory(prefix="filamatch_palette_") as temp_dir:
        settings_path = Path(temp_dir) / "settings.json"
        defaults = load_settings(settings_path)
        _check(
            defaults["write_project_settings"] is True
            and defaults["output_path"] == ""
            and defaults["prepare_enabled"] is True
            and "target_vendor" in defaults
            and "slicer_path" in defaults,
            "default settings load including printer-target keys",
        )
        saved = save_settings(
            {"output_path": "/tmp/out.3mf", "orca_path": "/Applications/OrcaSlicer.app"},
            settings_path,
        )
        reloaded = load_settings(settings_path)
        _check(
            saved["output_path"] == reloaded["output_path"] == "/tmp/out.3mf"
            and reloaded["orca_enabled"] is True,
            "settings round-trip through JSON",
        )
        _check(
            load_settings(settings_path)["printer_source"] == "bambuddy",
            "Bambuddy is the default printer source",
        )

        # The session settings file keeps the API key in the keychain
        # (memory backend here, so the real keychain is never touched).
        from . import secret_store

        global SETTINGS_FILE, _SECRETS_NEED_FUSION
        saved_globals = (SETTINGS_FILE, _SECRETS_NEED_FUSION)
        saved_env = {
            name: os.environ.pop(name, None)
            for name in ("FILAMATCH_DATA_DIR", secret_store.BACKEND_ENV)
        }
        os.environ[secret_store.BACKEND_ENV] = "memory"
        secret_store.reset_backend()
        SETTINGS_FILE = Path(temp_dir) / "session_settings.json"
        _SECRETS_NEED_FUSION = False
        try:
            SETTINGS_FILE.write_text(
                json.dumps({"bambuddy_api_key": "bb_legacy"}), encoding="utf-8"
            )
            migrated = load_settings()
            on_disk = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            _check(
                migrated["bambuddy_api_key"] == "bb_legacy"
                and on_disk["bambuddy_api_key"] == ""
                and secret_store.get_secret("bambuddy_api_key") == "bb_legacy",
                "a plain-text Bambuddy API key moves into the keychain",
            )
            save_settings({"bambuddy_api_key": "bb_new", "output_path": "/tmp/k.3mf"})
            on_disk = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            reloaded = load_settings()
            _check(
                on_disk["bambuddy_api_key"] == ""
                and on_disk["output_path"] == "/tmp/k.3mf"
                and reloaded["bambuddy_api_key"] == "bb_new"
                and public_settings(reloaded)["bambuddy_api_key_set"] is True,
                "a saved Bambuddy API key stays out of settings.json",
            )
            lan_printer = bambu_lan.LanPrinter(
                id=7, name="Shop", model="X1C", ip="192.0.2.7",
                serial="00M00TEST0007", access_code="12345678",
            )
            save_settings({bambu_lan.SETTINGS_KEY: [lan_printer.to_settings()]})
            on_disk = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            reloaded = load_settings()
            shown = json.dumps(public_settings(reloaded)) + json.dumps(_printer_source_payload(reloaded))
            _check(
                on_disk[bambu_lan.SETTINGS_KEY][0]["access_code"] == ""
                and secret_store.get_secret(bambu_lan.secret_account(lan_printer.serial)) == "12345678"
                and bambu_lan.printers_from_settings(reloaded)[0].access_code == "12345678"
                and "12345678" not in shown
                and lan_printer.serial not in shown,
                "a LAN printer access code stays in the keychain and out of the palette",
            )
            lan_palette = _FakePalette()
            dispatch(
                "save_settings",
                json.dumps({bambu_lan.SETTINGS_KEY: [], "output_path": "/tmp/l.3mf"}),
                lan_palette,
            )
            kept = bambu_lan.printers_from_settings(load_settings())
            _check(
                len(kept) == 1 and kept[0].serial == lan_printer.serial
                and lan_palette.sent[-1][1].get("printer_configured") is False,
                "save_settings from the palette cannot overwrite the LAN printers",
            )
            dispatch("set_printer_source", json.dumps({"source": "bambu_lan"}), lan_palette)
            _check(
                lan_palette.sent[-1][0] == "settingsSaved"
                and lan_palette.sent[-1][1]["settings"]["printer_source"] == "bambu_lan"
                and lan_palette.sent[-1][1]["printer_configured"] is True,
                "set_printer_source selects the LAN printers",
            )
            dispatch("bambu_lan_remove", json.dumps({"id": 7}), lan_palette)
            _check(
                lan_palette.sent[-1][0] == "bambuLanPrinters"
                and lan_palette.sent[-1][1]["printers"] == []
                and secret_store.get_secret(bambu_lan.secret_account(lan_printer.serial)) is None,
                "removing a LAN printer deletes its access code",
            )
            _SECRETS_NEED_FUSION = True
            _check(
                load_settings()["bambuddy_api_key"] == "",
                "outside Fusion the keychain is never read",
            )
        finally:
            SETTINGS_FILE, _SECRETS_NEED_FUSION = saved_globals
            for name, value in saved_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            secret_store.reset_backend()

    # The palette height follows the page content, inside the min/max limits.
    sized = types.SimpleNamespace(height=PALETTE_HEIGHT)
    dispatch("fit_height", json.dumps({"content": 400, "viewport": 733}), sized)
    tall = types.SimpleNamespace(height=PALETTE_HEIGHT)
    dispatch("fit_height", {"content": 5000, "viewport": 733}, tall)
    short = types.SimpleNamespace(height=PALETTE_HEIGHT)
    dispatch("fit_height", {"content": 50, "viewport": 733}, short)
    _check(
        sized.height == 467 and tall.height == PALETTE_HEIGHT and short.height == PALETTE_MIN_HEIGHT,
        "the palette height fits the page content within its limits",
    )

    # Upgrade from a temp data folder: copied once, old library path dropped.
    legacy = Path(temp_dir) / "legacy_data"
    fresh = Path(temp_dir) / "fresh_data"
    old_library = Path(temp_dir) / "OldDocs"
    (legacy / "openfilament").mkdir(parents=True)
    (legacy / "openfilament" / "all.json").write_text("{}", encoding="utf-8")
    (legacy / "user_catalog.json").write_text("[]", encoding="utf-8")
    (legacy / "old.log").write_text("x", encoding="utf-8")
    (legacy / "settings.json").write_text(json.dumps({
        "bambuddy_url": "http://printer:8000",
        "custom_library_path": str(old_library / "FusionFilament Library builds" / "1" / "FusionFilament Library.adsklib"),
    }), encoding="utf-8")
    copied = migrate_legacy_data_dir([legacy], fresh, old_library)
    moved = json.loads((fresh / "settings.json").read_text(encoding="utf-8"))
    _check(
        copied == 2
        and not (fresh / "openfilament").exists()
        and (fresh / "user_catalog.json").is_file()
        and not (fresh / "old.log").exists()
        and moved["bambuddy_url"] == "http://printer:8000"
        and moved["custom_library_path"] == "",
        "old temp data is copied (not caches or logs) and the old library path is dropped",
    )
    (fresh / "user_catalog.json").unlink()
    _check(
        migrate_legacy_data_dir([legacy], fresh, old_library) == 0
        and not (fresh / "user_catalog.json").exists(),
        "the old data folder is copied only once",
    )

    # Design change detection: the fingerprint follows bodies and their materials.
    def fake_design(first: str, second: str, own: str = "") -> Any:
        def body(name: str, material_id: str, settings: str = "") -> Any:
            material = types.SimpleNamespace(id=material_id) if material_id else None
            attribute = types.SimpleNamespace(value=settings) if settings else None
            return types.SimpleNamespace(name=name, material=material, nativeObject=None,
                                         attributes=types.SimpleNamespace(itemByName=lambda group, key: attribute))

        component = types.SimpleNamespace(
            id="c1", name="Root", material=None,
            bRepBodies=[body("Body1", first, own), body("Body2", second)],
        )
        return types.SimpleNamespace(
            allComponents=[component],
            parentDocument=types.SimpleNamespace(name="Bracket v12"),
        )

    baseline = design_fingerprint(fake_design("A", "B"))
    _check(
        baseline == design_fingerprint(fake_design("A", "B"))
        and baseline != design_fingerprint(fake_design("A", "C"))
        and baseline != design_fingerprint(fake_design("A", "B", '{"infill": 30}'))
        and baseline.startswith("Bracket:")
        and design_fingerprint(None) == "",
        "the design fingerprint follows body materials and their own print settings",
    )
    refreshed: List[Any] = []
    current = {"design": fake_design("A", "B")}
    saved_hooks = (_active_design, _handle_refresh_materials, _DESIGN_WATCH["fingerprint"])
    globals()["_active_design"] = lambda: current["design"]
    globals()["_handle_refresh_materials"] = lambda palette, fingerprint=None: refreshed.append(palette)
    _DESIGN_WATCH["fingerprint"] = ""
    try:
        dispatch("design_check", "{}", "first")
        dispatch("design_check", "{}", "unchanged")
        current["design"] = fake_design("A", "C")
        dispatch("design_check", "{}", "changed")
    finally:
        globals()["_active_design"], globals()["_handle_refresh_materials"] = saved_hooks[:2]
        _DESIGN_WATCH["fingerprint"] = saved_hooks[2]
    _check(
        refreshed == ["first", "changed"],
        "design_check refreshes the palette only when materials changed",
    )

    # The JS -> Python "response" echo must be ignored (feedback-loop guard).
    palette = _FakePalette()
    dispatch("response", '{"value": true}', palette)
    _check(palette.sent == [], "response messages are ignored without a reply loop")
    dispatch("unknown_action_xyz", "{}", palette)
    _check(palette.sent == [], "unknown actions are logged but never answered")

    # Favorites summary is app-level and degrades gracefully without Fusion.
    _check(
        _favorites_summary(None) == {"count": 0, "names": []},
        "favorites summary is empty without a Fusion application",
    )
    fake_favorites = types.ModuleType("favorites")
    fake_favorites.favorites_summary = lambda app: {"count": 2, "names": ["a", "b"]}
    testing.sibling_set(__package__, "favorites", fake_favorites)
    try:
        _check(
            _favorites_summary(object())["count"] == 2,
            "favorites summary delegates to the favorites module",
        )
    finally:
        testing.sibling_pop(__package__, "favorites")

    # Spool check: grams needed per filament against grams left on its spool, with a 10% margin.
    detected = [
        types.SimpleNamespace(material_id="m-black", display_name="ABS-GF Black"),
        types.SimpleNamespace(material_id="m-green", display_name="PolyLite Green"),
        types.SimpleNamespace(material_id="m-white", display_name="PETG White"),
        types.SimpleNamespace(material_id="m-red", display_name="PLA Red"),
    ]
    manifest = {
        "materials": [
            {"fusion_material_id": "m-black", "slot_key": "1:1:3", "slot_label": "AMS 2 - T4 (R)"},
            {"fusion_material_id": "m-green", "slot_key": "1:1:0", "slot_label": "AMS 2 - T1 (R)"},
            {"fusion_material_id": "m-white", "slot_key": "1:0:0", "slot_label": "AMS 1 - T1 (L)",
             "backup_slots": [{"slot_key": "1:0:1", "slot_label": "AMS 1 - T2 (L)", "remaining_g": 25.0},
                              {"slot_key": "1:0:2", "slot_label": "AMS 1 - T3 (L)", "remaining_g": None}]},
        ],
        "filaments": [
            {"slot_key": "1:1:3", "remaining_g": 240.0},
            {"slot_key": "1:1:0", "remaining_g": None},
            {"slot_key": "1:0:0", "remaining_g": 10.5},
        ],
    }
    spools = _spool_check(detected, manifest, [2.6, 4.3, 10.0, 99.0])
    _check(
        [(row["display_name"], row["enough"]) for row in spools] == [("ABS-GF Black", True), ("PETG White", False)]
        and spools[1]["needed_g"] == 10.0 and spools[1]["left_g"] == 10.5 and spools[1]["slot_label"] == "AMS 1 - T1 (L)",
        "spool check: enough, unknown weight skipped, 10.5 g left for 10 g is too tight, unmapped skipped",
    )
    _check(
        spools[1]["backup_count"] == 1 and spools[1]["backup_g"] == 25.0 and spools[1]["enough_with_backup"] is True
        and spools[0]["backup_count"] == 0 and spools[0]["enough_with_backup"] is True,
        "backup spools of the same filament count (unknown weights left out)",
    )
    _check(_spool_check(detected, None, [1.0]) == [], "no slot record, no spool check")

    # Slot record: of two identical black spools the fullest is used, the other is its backup.
    from . import printer_model

    def black_slot(tray: int, grams: float) -> Any:
        spool = printer_model.SpoolInfo(
            tray + 1, "PLA", "Bambu Lab", "Black", "#000000", grams, 1000.0, None, "GFA00", "Bambu PLA Basic"
        )
        return printer_model.SlotInfo(
            1, "H2D", 0, tray, spool, "PLA", "#000000", extruder=1, vendor_filament_id="GFA00"
        )

    nearly_empty, full = black_slot(0, 40.0), black_slot(1, 700.0)
    fake_sources = types.ModuleType("printer_sources")
    fake_sources.is_configured = lambda settings: True
    fake_sources.fetch_context = lambda settings: types.SimpleNamespace(
        printers=[printer_model.PrinterInfo(1, "H2D", auto_refill=True)], slots=[nearly_empty, full]
    )
    fake_inventory = types.ModuleType("inventory_sync")
    fake_inventory.entry_for_spool = lambda spool, slot: {"slot_key": slot.key, "remaining_g": spool.remaining_g}
    fake_inventory.slot_manifest = lambda entries, printer_name="", printer_id=0: {"filaments": list(entries)}
    fake_inventory.attach_slot_manifest = lambda path, manifest: None
    testing.sibling_set(__package__, "printer_sources", fake_sources)
    testing.sibling_set(__package__, "inventory_sync", fake_inventory)
    try:
        black = types.SimpleNamespace(
            material_id="m-black", display_name="Bambu Lab PLA Basic Black", color_hex="#000000",
            filament_type="PLA", manufacturer="Bambu Lab", color_name="Black",
        )
        manifest = _attach_bambuddy_slot_manifest(Path("unused.3mf"), [black], {}) or {}
    finally:
        testing.sibling_pop(__package__, "printer_sources")
        testing.sibling_pop(__package__, "inventory_sync")
    material = (manifest.get("materials") or [{}])[0]
    _check(
        manifest.get("auto_refill") is True
        and material.get("slot_key") == full.key
        and material.get("backup_slots") == [
            {"slot_key": nearly_empty.key, "slot_label": nearly_empty.slot_label, "remaining_g": 40.0}
        ],
        "slot record: fullest identical spool used, the other listed as backup, auto refill passed on",
    )

    # The prepared project names the loaded spool's preset, not the design's.
    @dataclasses.dataclass(frozen=True)
    class Design:
        material_id: str
        display_name: str
        filament_type: str
        orca_filament_profile: str = ""
        manufacturer: str = ""
        slicer_data: Dict[str, Any] = dataclasses.field(default_factory=dict, compare=False)

    polylite = printer_model.SlotInfo(
        1, "H2D", 1, 0,
        printer_model.SpoolInfo(9, "ABS", "Polymaker", "Green", "#00AE42", 900.0, 1000.0, None, "GFB60", ""),
        "ABS", "#00AE42", extruder=1, vendor_filament_id="GFB60",
    )
    designs = [Design("m-abs", "Bambu Lab - ABS - Bambu Green", "ABS"), Design("m-pla", "PLA", "PLA"),
               Design("m-petg", "PETG", "PETG"), Design("m-free", "ASA", "ASA")]
    rows = types.SimpleNamespace(printers={}, printer_id=1, slots=[polylite, nearly_empty, full], rows=[
        {"material_id": "m-abs", "slot_key": polylite.key},
        {"material_id": "m-pla", "slot_key": nearly_empty.key},
        {"material_id": "m-petg", "slot_key": full.key},
        {"material_id": "m-free", "slot_key": ""},
    ])
    original_preset = bambu_lan.filament_preset
    bambu_lan.filament_preset = lambda filament_id: (
        bambu_lan.FilamentPreset("GFB60", "PolyLite ABS", "Polymaker") if filament_id == "GFB60" else None
    )
    try:
        named = _with_spool_presets(designs, rows)
    finally:
        bambu_lan.filament_preset = original_preset
    _check(
        [item.orca_filament_profile for item in named] == ["PolyLite ABS", "Bambu PLA Basic", "", ""]
        and _with_spool_presets(designs, None) == designs,
        "the prepared project uses the loaded spool's preset (same type only)",
    )

    def spool_slot(key_tray: int, preset: str, kind: str = "ABS") -> Any:
        spool = printer_model.SpoolInfo(20 + key_tray, kind, "Any", "Green", "#00AE42", 900.0, 1000.0, None, "", preset)
        return printer_model.SlotInfo(1, "H2D", 3, key_tray, spool, kind, "#00AE42", extruder=1)

    data = {"min_print_temperature": 240}
    generic, other, same = spool_slot(0, "Generic ABS"), spool_slot(1, "Bambu ABS"), spool_slot(2, "PolyLite ABS")
    branded = [Design("a", "Polymaker - PolyLite ABS - Green", "ABS", manufacturer="Polymaker", slicer_data=data)] * 3
    branded = [dataclasses.replace(item, material_id=key) for item, key in zip(branded, "abc")]
    rows = types.SimpleNamespace(printers={}, printer_id=1, slots=[generic, other, same], rows=[
        {"material_id": "a", "slot_key": generic.key},
        {"material_id": "b", "slot_key": other.key},
        {"material_id": "c", "slot_key": same.key},
    ])
    named = _with_spool_presets(branded, rows)
    _check(
        named[0].orca_filament_profile == "" and named[0].slicer_data == data
        and named[1].orca_filament_profile == "Bambu ABS" and named[1].slicer_data == {}
        and named[2].orca_filament_profile == "PolyLite ABS" and named[2].slicer_data == data,
        "a generic spool keeps the design's filament; another filament drops the design's database data",
    )

    from . import filament_presets

    with tempfile.TemporaryDirectory(prefix="filamatch-built-") as temp:
        built = Path(temp) / "built.json"
        built.write_text(json.dumps({"name": "addnorth ABS rABS @Bambu Lab H2D 0.4 nozzle", "type": "filament",
                                     "inherits": "addnorth ABS rABS @Bambu Lab H2D 0.4 nozzle"}), encoding="utf-8")
        previous_root = filament_presets._CONF_ROOT_OVERRIDE
        filament_presets._CONF_ROOT_OVERRIDE = Path(temp) / "support"
        try:
            _report_built_presets([str(built)], "bambu", False)
            kept_out = not (Path(temp) / "support").exists()
            _report_built_presets([str(built)], "bambu", True)
            saved = list((Path(temp) / "support" / "BambuStudio" / "user" / "default" / "filament" / "base").glob("*.json"))
        finally:
            filament_presets._CONF_ROOT_OVERRIDE = previous_root
    _check(
        kept_out and len(saved) == 1,
        "a built preset is saved in the slicer only when the setting is on",
    )

    two = (printer_model.NozzleInfo(0, 0.4, "High Flow"), printer_model.NozzleInfo(1, 0.6, "Standard"))
    _check(
        [_nozzle_text(nozzle, 2) for nozzle in two] == ["Right 0.4 High Flow", "Left 0.6 Standard"]
        and _nozzle_text(printer_model.NozzleInfo(0, 0.4, ""), 1) == "0.4",
        "nozzles read as Left/Right size flow",
    )

    # Each filament's nozzle comes from the slot chosen for it (H2D: AMS 2 feeds the right).
    slots_by_key = {
        "1:1:3": types.SimpleNamespace(extruder=bambu_lan.EXTRUDER_RIGHT),
        "1:0:0": types.SimpleNamespace(extruder=bambu_lan.EXTRUDER_LEFT),
        "1:2:0": types.SimpleNamespace(extruder=None),
    }
    _check(
        _slot_nozzles(
            [{"slot_key": "1:1:3"}, {"slot_key": "1:0:0"}, {"slot_key": "1:2:0"}, {"slot_key": ""}],
            slots_by_key,
        )
        == ["right", "left", None, None],
        "slot nozzles follow the AMS side; unknown or unmapped slots stay None",
    )

    print()
    if _FAILED:
        print("PALETTE BRIDGE SELF-TEST FAILED ({} passed, {} failed)".format(_PASSED, _FAILED))
        return 1
    print("PALETTE BRIDGE SELF-TEST PASSED ({} checks)".format(_PASSED))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(_self_test())
