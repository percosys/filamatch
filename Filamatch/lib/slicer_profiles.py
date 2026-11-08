"""Slicer profile targeting and slicer-CLI project preparation.

Filamatch exports a plain 3MF (``Metadata/model_settings.config`` and
no ``Metadata/project_settings.config``).  This module turns that export into a
Bambu Studio / OrcaSlicer project for a *chosen printer + process* by driving the
slicer's own CLI:

``<slicer> --load-settings "<machine>;<process>" --load-filaments "a;b"
--export-3mf <basename> <base.3mf> --outputdir <dir> --logfile <log> --debug 4``

Verified live with OrcaSlicer 2.5.0-rc (Bambu Studio shares the flags).  Key
constraints encoded here:

* ``--export-3mf`` must be a bare file name together with ``--outputdir``; an
  absolute path is rejected by the slicer.
* The base 3MF must not contain a partial ``Metadata/project_settings.config``
  (Orca fails with "filament_is_support's count 1 not equal to
  filament_colour's size 2"), so a temporary copy without that entry is used.
* Remote Orca presets are downloaded from the OrcaSlicer GitHub repository at a
  tag matched to the installed Orca version (falling back to ``main``) and their
  inheritance chains are **flattened** into ``from: user`` presets, because the
  CLI cannot resolve ``from: system`` remote parents.
* After the slicer writes the project, ``Metadata/project_settings.config`` is
  post-processed so the filament colour/type/density arrays exactly describe the
  detected materials; every other archive entry is preserved byte-identically.

The module imports cleanly without Fusion/network/slicer (``adsk`` is never
imported; subprocess and urllib are behind replaceable hooks), and the
``__main__`` self-test exercises the whole pipeline with fakes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
from urllib.parse import quote
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import config
from . import body_settings
from . import filament_presets
from . import printer_catalog
from .project_fixes import (  # noqa: F401 - re-exported: callers and the self-test use these names here
    NOZZLE_LEFT,
    NOZZLE_RIGHT,
    WIPE_TOWER_MARGIN_MM,
    _FILAMENT_MAP_VALUES,
    _PRINTER_EXTRUDER_IDS,
    _area_bounds,
    _filament_map_values,
    _fit_bridge_line_width,
    _fit_nozzle_volume_types,
    _fit_wipe_tower,
    _nozzle_stats,
    _unprintable_on_nozzle,
    _wipe_tower_footprint,
)

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: Root of the add-in's download cache for remote Orca profiles.
CACHE_ROOT = config.CACHE_DIR

#: How long downloaded remote manifests/presets stay fresh, in seconds.
CACHE_TTL_SECONDS = 24 * 60 * 60

#: Timeout for a single network request, in seconds.
NETWORK_TIMEOUT_SECONDS = 30

#: Shorter per-request timeout used by the vendor/printer/process listings, so
#: an unreachable GitHub does not freeze the palette for minutes.
LIST_NETWORK_TIMEOUT_SECONDS = 8

#: Largest remote profile/manifest accepted, in bytes.
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024

#: Timeout for the slicer CLI process, in seconds.
CLI_TIMEOUT_SECONDS = 180

#: Gap (mm) under which two exported bodies count as touching (one multi-colour part).
TOUCH_TOLERANCE_MM = 0.05

#: Extra runs of the estimate slice when the slicer crashes (not when it refuses).
ESTIMATE_CRASH_RETRIES = 2

#: Orca/Bambu build plate names and the per-filament temperature key of each
#: (a 0 temperature means the filament must not be printed on that plate).
BED_TYPE_TEMPERATURE_KEYS: Dict[str, str] = {
    "Cool Plate": "cool_plate_temp",
    "Engineering Plate": "eng_plate_temp",
    "High Temp Plate": "hot_plate_temp",
    "Textured PEI Plate": "textured_plate_temp",
    "Textured Cool Plate": "textured_cool_plate_temp",
    "Supertack Plate": "supertack_plate_temp",
}

#: Plates tried, in order, when the preset's plate does not suit every filament.
BED_TYPE_PREFERENCE: Tuple[str, ...] = (
    "Textured PEI Plate",
    "High Temp Plate",
    "Engineering Plate",
    "Textured Cool Plate",
    "Cool Plate",
    "Supertack Plate",
)

#: Number of slicer log lines included in an error / PreparedProject.
LOG_TAIL_LINES = 30

#: Raw GitHub URL template for OrcaSlicer profile resources.
#:
#: The canonical repository is ``OrcaSlicer/OrcaSlicer`` (the project moved from
#: the historical ``SoftFever`` organisation). Profiles are fetched from the
#: release tag matching the installed OrcaSlicer version when available, with a
#: ``main`` fallback -- pinning to the release tag matters because ``main`` is a
#: development branch that can carry presets newer than the installed slicer.
REMOTE_PROFILES_BASE = (
    "https://raw.githubusercontent.com/OrcaSlicer/OrcaSlicer/"
    "{ref}/resources/profiles/{relative_path}"
)

#: Filament presets shared by every vendor in the OrcaSlicer repository.
ORCA_FILAMENT_LIBRARY_RELATIVE = "OrcaFilamentLibrary.json"

#: Curated vendors offered for remote profile targeting (id, display name).
CURATED_VENDORS: Tuple[Tuple[str, str], ...] = (
    ("BBL", "Bambu Lab"),
    ("Prusa", "Prusa"),
    ("Creality", "Creality"),
    ("Elegoo", "Elegoo"),
    ("Anycubic", "Anycubic"),
    ("Sovol", "Sovol"),
    ("Qidi", "Qidi"),
    ("Voron", "Voron"),
    ("Flashforge", "Flashforge"),
    ("Artillery", "Artillery"),
    ("Snapmaker", "Snapmaker"),
)

_PROJECT_SETTINGS_PART = "Metadata/project_settings.config"
_MODEL_SETTINGS_PART = "Metadata/model_settings.config"
_KIND_TO_PLURAL = {"machine": "machines", "process": "processes", "filament": "filaments"}
_PROFILE_KIND_ALIASES = {
    "machine": "machine",
    "printer": "machine",
    "process": "process",
    "print": "process",
    "filament": "filament",
}
_VERSION_PATTERN = re.compile(
    r"(?:OrcaSlicer|BambuStudio|Bambu Studio|Orca|PrusaSlicer)[-\s]+v?"
    r"(\d+(?:\.\d+){1,3}(?:[-_][0-9A-Za-z.]+)?)",
    re.IGNORECASE,
)
_UNSAFE_FILENAME_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_PATTERN = re.compile(r"\s+")
_FILAMENT_TYPE_TOKENS: Tuple[str, ...] = (
    "PLA-CF",
    "PETG-CF",
    "ABS-GF",
    "PAHT-CF",
    "PA-CF",
    "PLA",
    "PETG",
    "ABS",
    "ASA",
    "TPU",
    "PVA",
    "HIPS",
    "PET",
    "PC",
    "PA",
    "POM",
    "PP",
    "PCTG",
    "PEEK",
    "PEI",
    "BVOH",
    "NYLON",
)
#: Spelled-out model names and the short code presets use for them, and the
#: printer family tokens in matching order (from ``data/printers.json``).
_PRINTER_ALIASES: Tuple[Tuple[str, str], ...] = printer_catalog.aliases()
_PRINTER_TOKENS: Tuple[str, ...] = printer_catalog.tokens()

__all__ = [
    "CACHE_ROOT",
    "CACHE_TTL_SECONDS",
    "NETWORK_TIMEOUT_SECONDS",
    "LIST_NETWORK_TIMEOUT_SECONDS",
    "MAX_DOWNLOAD_BYTES",
    "CLI_TIMEOUT_SECONDS",
    "LOG_TAIL_LINES",
    "REMOTE_PROFILES_BASE",
    "CURATED_VENDORS",
    "SlicerProfilesError",
    "SlicerInstall",
    "ProfileTarget",
    "PreparedProject",
    "find_installed_slicers",
    "list_vendors",
    "list_printers",
    "list_processes",
    "imported_profiles",
    "import_profile_files",
    "prepare_project",
    "resolve_slicer_executable",
    "rewrite_zip_entries",
]


class SlicerProfilesError(RuntimeError):
    """Raised when slicer profile discovery or project preparation fails."""


class _RemoteFetchError(SlicerProfilesError):
    """A remote profile could not be downloaded and no cached copy exists."""

    def __init__(self, message: str, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found


@dataclass
class _NetworkSession:
    """Network budget shared by the downloads of one listing / preparation.

    ``allowed`` drops to false after the first connection-level failure, so
    the remaining downloads go straight to the cache instead of each waiting
    for its own timeout.
    """

    timeout: float = NETWORK_TIMEOUT_SECONDS
    allowed: bool = True


@dataclass(frozen=True)
class SlicerInstall:
    """One installed slicer executable."""

    kind: str
    name: str
    path: str
    version: str


@dataclass(frozen=True)
class ProfileTarget:
    """A printer + process profile target for :func:`prepare_project`."""

    vendor: str
    printer_name: str
    machine_path: str
    process_name: str
    process_path: str
    source: str


@dataclass(frozen=True)
class PreparedProject:
    """Result of a successful slicer-CLI project preparation."""

    output_path: Path
    printer_name: str
    process_name: str
    filament_names: Tuple[str, ...]
    slicer_version: str
    log_tail: str
    #: Filament presets made from the Open Filament Database for this project
    #: (stand-alone preset files, see filament_presets.build_preset).
    built_presets: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Replaceable hooks (the self-test swaps these; production uses the defaults)
# ---------------------------------------------------------------------------


def _default_command_runner(
    command: Sequence[str], timeout: float, cwd: Optional[str] = None
) -> Any:
    """Run a child process and return a ``subprocess.CompletedProcess``."""
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
    )


def _default_url_opener(url: str, timeout: float) -> bytes:
    """Download ``url`` and return the raw bytes (production network hook)."""
    request = urllib.request.Request(
        url, headers={"User-Agent": config.USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise SlicerProfilesError(
            "Refusing {!r}: the response is larger than {} bytes".format(url, MAX_DOWNLOAD_BYTES)
        )
    return data


_CACHE_ROOT_OVERRIDE: Optional[Path] = None
_BAMBU_SYSTEM_DIR_OVERRIDE: Optional[Path] = None
_SLICER_CANDIDATES_OVERRIDE: Optional[Sequence[Tuple[str, str, Path]]] = None
_command_runner: Callable[..., Any] = _default_command_runner
_url_opener: Callable[[str, float], bytes] = _default_url_opener
#: ``(executable path, mtime) -> (version, kind)``; only successful probes are kept.
_VERSION_CACHE: Dict[Tuple[str, Optional[float]], Tuple[str, str]] = {}
#: Session-wide negative caches (sets are safe to update from worker threads).
#: URLs that returned 404, refs (tags) that do not exist, and refs known to work.
_MISSING_URLS: set = set()
_MISSING_REFS: set = set()
_GOOD_REFS: set = set()


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _safe_get(obj: Any, attribute: str, context: str) -> Any:
    """Read ``obj.attribute``, logging failures and returning ``None``."""
    if obj is None:
        return None
    try:
        return getattr(obj, attribute)
    except AttributeError:
        logger.debug("Object %s has no %r attribute.", context, attribute)
        return None
    except Exception:
        logger.exception("Could not read %r from %s.", attribute, context)
        return None


def _require_text(value: Any, label: str) -> str:
    """Return a stripped non-empty string or raise SlicerProfilesError."""
    if not isinstance(value, str) or not value.strip():
        raise SlicerProfilesError(
            "{} must be a non-empty string, got {!r}".format(label, value)
        )
    return value.strip()


def _material_field(material: Any, name: str, default: Any = None) -> Any:
    """Read an attribute (or mapping key) from a detected material object."""
    if material is None:
        return default
    if isinstance(material, MappingABC):
        return material.get(name, default)
    try:
        value = getattr(material, name)
    except AttributeError:
        return default
    except Exception:
        logger.exception("Could not read material field %r.", name)
        return default
    return default if value is None else value


def _result_text(result: Any) -> str:
    """Return the combined stdout/stderr text of a command result."""
    stdout = _safe_get(result, "stdout", "a command result") or ""
    stderr = _safe_get(result, "stderr", "a command result") or ""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    return str(stdout) + str(stderr)


def _remove_file_quietly(path: Optional[Path]) -> None:
    """Delete ``path`` when it exists, ignoring filesystem errors."""
    if path is None:
        return
    try:
        Path(path).unlink()
    except OSError:
        pass


def _safe_filename(name: str) -> str:
    """Convert a preset name into a cross-platform safe file name stem."""
    stem = _UNSAFE_FILENAME_PATTERN.sub("_", str(name)).strip()
    stem = _WHITESPACE_PATTERN.sub(" ", stem).strip(" ._")
    if len(stem) > 100:
        stem = stem[:100].rstrip(" ._")
    return stem or "profile"


def _read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    """Read a JSON object from disk, returning ``None`` and logging on failure."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        logger.exception("Could not read profile file %r.", str(path))
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.exception("Profile file %r is not valid JSON.", str(path))
        return None
    if not isinstance(data, dict):
        logger.warning("Profile file %r does not contain a JSON object.", str(path))
        return None
    return data


def _preset_name(data: Dict[str, Any], path: Path) -> str:
    """Return a preset's display name, falling back to the file stem."""
    name = data.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return Path(path).stem


def _is_selectable_preset(
    name: str, data: Dict[str, Any], skip_common: bool = False
) -> bool:
    """Return whether a preset is an instantiable (non base/template) preset."""
    lowered = name.lower()
    if name.startswith("fdm_"):
        return False
    if "template" in lowered:
        return False
    if data.get("importable") is False:
        return False
    if str(data.get("instantiation", "")).lower() == "false":
        return False
    if skip_common and "common" in lowered:
        return False
    return True


def _profile_kind(data: Dict[str, Any]) -> Optional[str]:
    """Map a preset ``type`` field to machine/process/filament."""
    raw_type = data.get("type")
    if not isinstance(raw_type, str):
        return None
    return _PROFILE_KIND_ALIASES.get(raw_type.strip().lower())


# ---------------------------------------------------------------------------
# Cache roots and filesystem locations
# ---------------------------------------------------------------------------


def _cache_root() -> Path:
    """Return the add-in cache root (test override aware)."""
    return Path(_CACHE_ROOT_OVERRIDE) if _CACHE_ROOT_OVERRIDE is not None else CACHE_ROOT


def _profiles_root() -> Path:
    """Return the imported-profiles root (user data, not a cache)."""
    if _CACHE_ROOT_OVERRIDE is not None:
        return _cache_root() / "profiles"
    return config.PROFILES_DIR


def _profile_cache_root() -> Path:
    """Return the remote profile cache root directory."""
    return _cache_root() / "profile_cache"


def _profile_dir(kind: str) -> Path:
    """Return the imported-profile directory for a preset kind."""
    return _profiles_root() / kind


def _bambu_system_candidates() -> List[Path]:
    """Return candidate Bambu Studio system profile directories for this OS."""
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "BambuStudio" / "system" / "BBL"]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        return [Path(appdata) / "BambuStudio" / "system" / "BBL"] if appdata else []
    return [home / ".config" / "BambuStudio" / "system" / "BBL"]


def _bambu_system_dir() -> Optional[Path]:
    """Return the first existing Bambu Studio system profile directory."""
    if _BAMBU_SYSTEM_DIR_OVERRIDE is not None:
        return Path(_BAMBU_SYSTEM_DIR_OVERRIDE)
    for candidate in _bambu_system_candidates():
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            logger.exception("Could not inspect Bambu Studio profile dir %r.", str(candidate))
    return None


# ---------------------------------------------------------------------------
# Remote cache / network helpers
# ---------------------------------------------------------------------------


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically, creating parent directories."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".filamatch_write_", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary_name, str(path))
    finally:
        _remove_file_quietly(Path(temporary_name))


def _write_json_atomic(path: Path, data: Any) -> None:
    """Write a JSON object to ``path`` atomically."""
    payload = (json.dumps(data, indent=2) + "\n").encode("utf-8")
    _write_bytes_atomic(path, payload)


def _is_not_found(exc: BaseException) -> bool:
    """Return whether a download failed because the remote file does not exist."""
    return isinstance(exc, urllib.error.HTTPError) and exc.code == 404


def _is_connection_failure(exc: BaseException) -> bool:
    """Return whether a download failed at the connection level (offline, DNS, timeout)."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (urllib.error.URLError, OSError, TimeoutError))


def _fetch_bytes_cached(
    url: str,
    cache_path: Path,
    ttl: float = CACHE_TTL_SECONDS,
    network: Optional[_NetworkSession] = None,
) -> bytes:
    """Return remote bytes from cache when fresh, else download and cache them.

    Network failures fall back to a stale cache copy so profile listing keeps
    working offline; :class:`SlicerProfilesError` is raised only when no cached
    copy exists at all.  A 404 is remembered for the session, and a
    connection-level failure switches *network* to cache-only.
    """
    session = network if network is not None else _NetworkSession()
    cache_file = Path(cache_path)
    try:
        if cache_file.is_file() and (
            time.time() - cache_file.stat().st_mtime
        ) <= ttl:
            return cache_file.read_bytes()
    except OSError:
        logger.exception("Could not inspect cached profile %r.", str(cache_file))

    failure: Optional[BaseException] = None
    not_found = False
    if url in _MISSING_URLS:
        failure = SlicerProfilesError("not found earlier in this session")
        not_found = True
    elif not session.allowed:
        failure = SlicerProfilesError("skipped after an earlier connection failure")
    else:
        try:
            data = _url_opener(url, session.timeout)
        except Exception as exc:
            failure = exc
            if _is_not_found(exc):
                not_found = True
                _MISSING_URLS.add(url)
            elif _is_connection_failure(exc):
                logger.warning(
                    "Network unavailable (%s); using cached profiles for the rest of this listing.",
                    exc,
                )
                session.allowed = False
    if failure is not None:
        try:
            stale = cache_file.read_bytes()
        except OSError:
            raise _RemoteFetchError(
                "Could not download {!r} and no cached copy exists: {}".format(url, failure),
                not_found=not_found,
            ) from failure
        logger.warning(
            "Network fetch failed for %r (%s); using the cached copy.", url, failure
        )
        return stale
    if not isinstance(data, (bytes, bytearray)):
        raise SlicerProfilesError(
            "Network fetch for {!r} returned {!r} instead of bytes".format(
                url, type(data).__name__
            )
        )
    _write_bytes_atomic(cache_file, bytes(data))
    return bytes(data)


def _remote_relative_path(relative_path: str) -> str:
    """Normalise a repository-relative profile path and reject path escapes.

    Rejects ``..`` parts and anything Windows could read as a drive or stream
    (``D:``, ``name:stream``) as well as NUL characters.
    """
    text = str(relative_path).replace("\\", "/").lstrip("/")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(
        part == ".." or ":" in part or "\x00" in part for part in parts
    ):
        raise SlicerProfilesError(
            "Refusing unsafe remote profile path {!r}".format(relative_path)
        )
    return "/".join(parts)


def _safe_ref(ref: str) -> str:
    """Validate a repository ref (``main`` or ``v<version>``) used as a path part."""
    text = str(ref)
    if (
        not text
        or text in (".", "..")
        or re.match(r"^[0-9A-Za-z._-]+$", text) is None
    ):
        raise SlicerProfilesError("Refusing unsafe repository ref {!r}".format(ref))
    return text


def _remote_url(ref: str, relative_path: str) -> str:
    """Build the raw GitHub URL for a repository-relative profile path.

    Preset filenames contain spaces (for example
    ``machine/Prusa MK4 0.4 nozzle.json``) and must be percent-encoded or
    ``urllib`` rejects the request with an invalid-URL error.
    """
    clean = _remote_relative_path(relative_path)
    encoded = "/".join(quote(part, safe="") for part in clean.split("/"))
    return REMOTE_PROFILES_BASE.format(ref=_safe_ref(ref), relative_path=encoded)


def _remote_cache_path(ref: str, relative_path: str) -> Path:
    """Return the local cache path for a repository-relative profile path."""
    clean = _remote_relative_path(relative_path)
    root = _profile_cache_root()
    path = root / _safe_ref(ref) / Path(*clean.split("/"))
    # Belt and braces: whatever the manifest said, the file must stay in the cache.
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        raise SlicerProfilesError(
            "Refusing remote profile path {!r}: it leaves the profile cache".format(relative_path)
        )
    return path


def _load_remote_json_ref(
    relative_path: str, ref: str, network: Optional[_NetworkSession] = None
) -> Dict[str, Any]:
    """Load a remote JSON object at a specific repository ref."""
    url = _remote_url(ref, relative_path)
    cache_path = _remote_cache_path(ref, relative_path)
    raw = _fetch_bytes_cached(url, cache_path, network=network)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SlicerProfilesError(
            "Remote profile {!r} is not valid JSON: {}".format(url, exc)
        ) from exc
    if not isinstance(data, dict):
        raise SlicerProfilesError(
            "Remote profile {!r} does not contain a JSON object".format(url)
        )
    return data


def _candidate_refs(slicer_exe: Any) -> List[str]:
    """Return repository refs to try: tag matched to the install, then ``main``.

    Only OrcaSlicer versions have a matching tag in the OrcaSlicer repository
    (Bambu Studio's ``02.07.x`` never does), and a tag that returned 404
    earlier in this session is not tried again.
    """
    version, kind = _slicer_identity(slicer_exe)
    refs: List[str] = []
    if version and kind == "orca":
        tag = "v" + version
        if tag not in _MISSING_REFS:
            refs.append(tag)
    refs.append("main")
    return refs


def _load_remote_json(
    relative_path: str, slicer_exe: Any, network: Optional[_NetworkSession] = None
) -> Tuple[Dict[str, Any], str]:
    """Load a remote JSON object, trying the tag-matched ref then ``main``."""
    last_error: Optional[SlicerProfilesError] = None
    for ref in _candidate_refs(slicer_exe):
        try:
            data = _load_remote_json_ref(relative_path, ref, network=network)
        except SlicerProfilesError as exc:
            last_error = exc
            if (
                isinstance(exc, _RemoteFetchError)
                and exc.not_found
                and ref != "main"
                and ref not in _GOOD_REFS
            ):
                # Nothing was ever found at this tag: treat the tag itself as missing.
                logger.info("Repository ref %r was not found; using 'main' this session.", ref)
                _MISSING_REFS.add(ref)
            logger.debug(
                "Remote profile %r is unavailable at ref %r; trying the next ref.",
                relative_path,
                ref,
            )
            continue
        _GOOD_REFS.add(ref)
        return data, ref
    raise last_error or SlicerProfilesError(
        "Could not load remote profile {!r}".format(relative_path)
    )


def _load_vendor_manifest(
    vendor_id: str, slicer_exe: Any, network: Optional[_NetworkSession] = None
) -> Tuple[Dict[str, Any], str]:
    """Load a vendor manifest (e.g. ``BBL.json``) and the ref it came from."""
    vendor = _require_text(vendor_id, "vendor id")
    return _load_remote_json(vendor + ".json", slicer_exe, network=network)


def _manifest_entries(manifest: Dict[str, Any], list_key: str) -> List[Dict[str, Any]]:
    """Return the dict entries of a manifest list (``machine_list`` etc.)."""
    entries = manifest.get(list_key)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


# ---------------------------------------------------------------------------
# Slicer discovery / version parsing
# ---------------------------------------------------------------------------


def _default_slicer_candidates() -> List[Tuple[str, str, Path]]:
    """Return the platform's known slicer executable locations."""
    candidates: List[Tuple[str, str, Path]] = []
    if sys.platform == "darwin":
        applications = Path("/Applications")
        candidates.append(
            (
                "orca",
                "OrcaSlicer",
                applications / "OrcaSlicer.app" / "Contents" / "MacOS" / "OrcaSlicer",
            )
        )
        candidates.append(
            (
                "bambu",
                "BambuStudio",
                applications / "BambuStudio.app" / "Contents" / "MacOS" / "BambuStudio",
            )
        )
        # PrusaSlicer's installer puts it in "Original Prusa Drivers".
        for folder in (applications, applications / "Original Prusa Drivers"):
            candidates.append(
                ("prusa", "PrusaSlicer", folder / "PrusaSlicer.app" / "Contents" / "MacOS" / "PrusaSlicer")
            )
        return candidates
    if os.name == "nt":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        candidates.append(
            ("orca", "OrcaSlicer", Path(program_files) / "OrcaSlicer" / "orca-slicer.exe")
        )
        candidates.append(
            ("bambu", "BambuStudio", Path(program_files) / "BambuStudio" / "bambu-studio.exe")
        )
        if local_app_data:
            candidates.append(
                (
                    "bambu",
                    "BambuStudio",
                    Path(local_app_data) / "Programs" / "BambuStudio" / "bambu-studio.exe",
                )
            )
        candidates.append(
            ("prusa", "PrusaSlicer", Path(program_files) / "Prusa3D" / "PrusaSlicer" / "prusa-slicer-console.exe")
        )
        return candidates
    candidates.append(("orca", "OrcaSlicer", Path("/usr/bin/orca-slicer")))
    candidates.append(("bambu", "BambuStudio", Path("/usr/bin/bambu-studio")))
    candidates.append(("prusa", "PrusaSlicer", Path("/usr/bin/prusa-slicer")))
    return candidates


def _run_command(
    command: Sequence[str], timeout: float, cwd: Optional[str] = None
) -> Any:
    """Run a command through the replaceable command runner hook."""
    return _command_runner(list(command), timeout=timeout, cwd=cwd)


def _is_launchable_file(item: Path, windows: Optional[bool] = None) -> bool:
    """Return whether ``item`` is a file the CLI could launch.

    On Windows ``os.access(X_OK)`` is true for every file (DLLs included), so
    only ``.exe`` files count there.
    """
    if windows is None:
        windows = os.name == "nt"
    if not item.is_file():
        return False
    if windows:
        return item.suffix.lower() == ".exe"
    return os.access(str(item), os.X_OK)


def resolve_slicer_executable(slicer_exe: Any) -> str:
    """Resolve a slicer path to the executable file the CLI can launch.

    On macOS a native file picker commonly returns the application **bundle**
    (``/Applications/OrcaSlicer.app``) rather than the binary inside it
    (``.../Contents/MacOS/OrcaSlicer``). Running a bundle directly fails with
    ``PermissionError: [Errno 13]``, so callers must resolve it first.

    Args:
        slicer_exe: Executable path, path to a macOS ``.app`` bundle, or an
            application directory containing an executable.

    Returns:
        Absolute path (as a string) of the executable file.

    Raises:
        SlicerProfilesError: If no executable can be found at the given path.
    """
    if slicer_exe is None:
        raise SlicerProfilesError("No slicer executable was provided.")
    text = str(slicer_exe).strip()
    if not text:
        raise SlicerProfilesError("No slicer executable was provided.")
    candidate = Path(text).expanduser()
    try:
        if candidate.is_file():
            return str(candidate)
    except OSError:
        pass
    candidate_dirs = []
    if candidate.is_dir():
        candidate_dirs.append(candidate / "Contents" / "MacOS")
        candidate_dirs.append(candidate)
    for directory in candidate_dirs:
        try:
            if not directory.is_dir():
                continue
            executables = [
                item for item in sorted(directory.iterdir()) if _is_launchable_file(item)
            ]
        except OSError:
            continue
        if not executables:
            continue
        bundle_stem = candidate.stem.lower()
        preferred = next(
            (item for item in executables if bundle_stem and bundle_stem in item.name.lower()),
            None,
        )
        if preferred is None:
            preferred = next(
                (
                    item
                    for item in executables
                    if any(token in item.name.lower() for token in ("orca", "bambu", "slicer"))
                ),
                executables[0],
            )
        return str(preferred)
    raise SlicerProfilesError(
        "Slicer executable not found at {!r}. Choose the slicer executable "
        "(or a macOS .app bundle containing it) in the palette.".format(text)
    )


def _kind_from_path(path: str) -> str:
    """Guess the slicer kind (``orca``/``bambu``/``prusa``) from its executable path."""
    lowered = Path(str(path)).name.lower()
    if "orca" in lowered:
        return "orca"
    if "bambu" in lowered:
        return "bambu"
    if "prusaslicer" in lowered or "prusa-slicer" in lowered:
        return "prusa"
    return ""


def slicer_kind(path: Any) -> str:
    """Return ``orca``, ``bambu``, ``prusa`` or ``""`` for a slicer path (executable or ``.app``)."""
    text = str(path or "").strip()
    if not text:
        return ""
    bundle = re.search(r"([^/\\]+)\.app(?:[/\\]|$)", text, re.IGNORECASE)
    return _kind_from_path(bundle.group(1) if bundle else text)


def slicer_name(path: Any) -> str:
    """Return a slicer's display name (``Bambu Studio``) from its path."""
    text = str(path or "").strip()
    kind = slicer_kind(text)
    if kind == "orca":
        return "OrcaSlicer"
    if kind == "bambu":
        return "Bambu Studio"
    if kind == "prusa":
        return "PrusaSlicer"
    bundle = re.search(r"([^/\\]+)\.app(?:[/\\]|$)", text, re.IGNORECASE)
    if bundle:
        return bundle.group(1)
    name = re.sub(r"\.exe$", "", re.split(r"[/\\]", text)[-1], flags=re.IGNORECASE)
    return name or "the slicer"


def _slicer_identity(slicer_exe: Any) -> Tuple[str, str]:
    """Return ``(version, kind)`` of an installed slicer, parsed from ``--help``.

    ``kind`` is ``"orca"``, ``"bambu"`` or ``""``.  Results are cached per
    executable path and modification time (so an update is noticed); a probe
    that fails or prints nothing is not cached, so it is retried next time.
    """
    if slicer_exe is None:
        return "", ""
    try:
        key = resolve_slicer_executable(slicer_exe)
    except SlicerProfilesError:
        key = str(slicer_exe)
    try:
        mtime: Optional[float] = os.stat(key).st_mtime
    except OSError:
        mtime = None
    cache_key = (key, mtime)
    cached = _VERSION_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        result = _run_command([key, "--help"], NETWORK_TIMEOUT_SECONDS)
        output = _result_text(result)
    except Exception:
        logger.debug("Could not read the version of %r.", key, exc_info=True)
        return "", _kind_from_path(key)
    version = ""
    kind = ""
    lines = output.splitlines()
    first_line = lines[0] if lines else ""
    match = _VERSION_PATTERN.search(first_line) or _VERSION_PATTERN.search(output)
    if match is not None:
        version = match.group(1)
        lowered = match.group(0).lower()
        kind = "orca" if lowered.startswith("orca") else "prusa" if lowered.startswith("prusa") else "bambu"
    identity = (version, kind or _kind_from_path(key))
    if output.strip():
        _VERSION_CACHE[cache_key] = identity
    return identity


def _slicer_version(slicer_exe: Any) -> str:
    """Return the installed slicer version parsed from ``--help`` (or ``""``)."""
    return _slicer_identity(slicer_exe)[0]


def find_installed_slicers() -> List[SlicerInstall]:
    """Discover installed Bambu Studio / OrcaSlicer executables.

    The platform's known install locations are probed; an executable is reported
    even when its ``--help`` version probe fails (``version`` is then ``""``).

    Returns:
        One :class:`SlicerInstall` per discovered executable.
    """
    candidates = (
        list(_SLICER_CANDIDATES_OVERRIDE)
        if _SLICER_CANDIDATES_OVERRIDE is not None
        else _default_slicer_candidates()
    )
    installs: List[SlicerInstall] = []
    seen: set = set()
    for kind, name, path in candidates:
        try:
            if not path.is_file():
                continue
        except OSError:
            logger.exception("Could not inspect slicer candidate %r.", str(path))
            continue
        absolute = str(path)
        if absolute in seen:
            continue
        seen.add(absolute)
        installs.append(
            SlicerInstall(
                kind=kind, name=name, path=absolute, version=_slicer_version(path)
            )
        )
    return installs


# ---------------------------------------------------------------------------
# Local Bambu Studio preset discovery
# ---------------------------------------------------------------------------


def _scan_local_presets(
    directory: Optional[Path], skip_common: bool = False
) -> List[Dict[str, Any]]:
    """Scan a local preset directory into ``{name,path,data}`` dicts."""
    if directory is None:
        return []
    try:
        paths = sorted(Path(directory).glob("*.json"))
    except OSError:
        logger.exception("Could not scan preset directory %r.", str(directory))
        return []
    presets: List[Dict[str, Any]] = []
    for path in paths:
        data = _read_json_file(path)
        if data is None:
            continue
        name = _preset_name(data, path)
        if not _is_selectable_preset(name, data, skip_common=skip_common):
            logger.debug("Skipping base/template preset %r.", name)
            continue
        presets.append({"name": name, "path": str(path), "data": data})
    return presets


def _scan_local_machines(root: Path) -> List[Dict[str, Any]]:
    """Return the Bambu Studio machine presets in a system profile dir."""
    return _scan_local_presets(root / "machine")


def _scan_local_processes(root: Path) -> List[Dict[str, Any]]:
    """Return the Bambu Studio process presets in a system profile dir."""
    return _scan_local_presets(root / "process", skip_common=True)


def _scan_local_filaments(root: Path) -> List[Dict[str, Any]]:
    """Return the Bambu Studio filament presets in a system profile dir."""
    return _scan_local_presets(root / "filament")


# ---------------------------------------------------------------------------
# Remote preset resolution and inheritance flattening
# ---------------------------------------------------------------------------


def _resolve_remote_preset(
    vendor: str,
    sub_path: str,
    slicer_exe: Any = None,
    ref: Optional[str] = None,
    download: bool = True,
    network: Optional[_NetworkSession] = None,
) -> Path:
    """Return the cache path of a remote preset, downloading it when needed.

    Args:
        vendor: Vendor id used only for diagnostics.
        sub_path: Repository-relative preset path from the vendor manifest.
        slicer_exe: Installed slicer used to derive the repository tag.
        ref: Explicit repository ref; derived from ``slicer_exe`` when omitted.
        download: When false, only the cache path is computed.
        network: Shared network budget (timeout / cache-only after a failure).

    Returns:
        Path of the cached (or to-be-cached) preset file.
    """
    resolved_ref = ref
    if resolved_ref is None:
        resolved_ref = _candidate_refs(slicer_exe)[0]
    clean = _remote_relative_path(sub_path)
    if clean.split("/")[0] in ("machine", "process", "filament"):
        # Manifest sub-paths are vendor-relative; name-map paths are already
        # prefixed with their source directory (vendor or OrcaFilamentLibrary).
        clean = vendor + "/" + clean
    cache_path = _remote_cache_path(resolved_ref, clean)
    if download:
        url = _remote_url(resolved_ref, clean)
        _fetch_bytes_cached(url, cache_path, network=network)
        logger.debug("Cached %s preset %r at %s.", vendor, sub_path, str(cache_path))
    return cache_path


def _build_remote_name_map(
    vendor_id: str,
    slicer_exe: Any,
    ref: str,
    network: Optional[_NetworkSession] = None,
) -> Dict[str, str]:
    """Build a preset-name to repository-sub-path map for inheritance resolution.

    The map is assembled from the vendor manifest and
    ``OrcaFilamentLibrary.json`` so machine/process parents from the same vendor
    and filament parents from the shared library can both be resolved.
    """
    name_map: Dict[str, str] = {}
    relative_paths = [vendor_id + ".json", ORCA_FILAMENT_LIBRARY_RELATIVE]
    for relative_path in relative_paths:
        try:
            manifest = _load_remote_json_ref(relative_path, ref, network=network)
        except SlicerProfilesError:
            if relative_path == vendor_id + ".json":
                raise
            logger.debug(
                "Optional remote manifest %r is unavailable; continuing without it.",
                relative_path,
                exc_info=True,
            )
            continue
        source_prefix = (
            vendor_id
            if relative_path == vendor_id + ".json"
            else "OrcaFilamentLibrary"
        )
        for list_key in ("machine_list", "process_list", "filament_list"):
            for entry in _manifest_entries(manifest, list_key):
                name = entry.get("name")
                sub_path = entry.get("sub_path")
                if (
                    isinstance(name, str)
                    and name.strip()
                    and isinstance(sub_path, str)
                    and sub_path.strip()
                ):
                    clean_sub_path = sub_path.strip().replace("\\", "/").lstrip("/")
                    prefix = source_prefix + "/"
                    name_map[name.strip()] = (
                        clean_sub_path
                        if clean_sub_path.startswith(prefix)
                        else prefix + clean_sub_path
                    )
    return name_map


def _flatten_remote_preset_at(
    vendor_id: str,
    sub_path: str,
    ref: str,
    name_map: Dict[str, str],
    network: Optional[_NetworkSession] = None,
) -> Dict[str, Any]:
    """Flatten one remote preset's inheritance chain into a ``from: user`` dict."""
    flattened_cache: Dict[str, Dict[str, Any]] = {}

    def load_preset(current_sub_path: str) -> Dict[str, Any]:
        preset_path = _resolve_remote_preset(
            vendor_id, current_sub_path, ref=ref, download=True, network=network
        )
        data = _read_json_file(preset_path)
        if data is None:
            raise SlicerProfilesError(
                "Remote preset {!r} could not be read from {!r}".format(
                    current_sub_path, str(preset_path)
                )
            )
        return data

    def flatten(current_sub_path: str, chain: List[str]) -> Dict[str, Any]:
        if current_sub_path in flattened_cache:
            return flattened_cache[current_sub_path]
        data = load_preset(current_sub_path)
        inherits = data.get("inherits")
        parents: List[str] = []
        if isinstance(inherits, str) and inherits.strip():
            parents = [inherits.strip()]
        elif isinstance(inherits, (list, tuple)):
            parents = [
                item for item in inherits if isinstance(item, str) and item.strip()
            ]
        merged: Dict[str, Any] = {}
        for parent_name in parents:
            parent_sub_path = name_map.get(parent_name)
            if parent_sub_path is None:
                raise SlicerProfilesError(
                    "Could not resolve parent preset {!r} in the inheritance chain of "
                    "remote preset {!r} (vendor {!r}). Chain: {}. The OrcaSlicer "
                    "profile repository layout may have changed.".format(
                        parent_name,
                        data.get("name", current_sub_path),
                        vendor_id,
                        " -> ".join(chain + [parent_name]),
                    )
                )
            if parent_sub_path in chain:
                raise SlicerProfilesError(
                    "Circular preset inheritance detected for {!r}: {}. Remove or fix "
                    "the remote preset chain and retry.".format(
                        current_sub_path, " -> ".join(chain + [parent_sub_path])
                    )
                )
            merged.update(flatten(parent_sub_path, chain + [parent_sub_path]))
        for key, value in data.items():
            if key == "inherits":
                continue
            merged[key] = value
        merged["from"] = "user"
        merged.pop("inherits", None)
        flattened_cache[current_sub_path] = merged
        return merged

    return flatten(sub_path, [sub_path])


def _write_flattened_profile(directory: Path, name: str, data: Dict[str, Any]) -> Path:
    """Write a flattened preset JSON into the cache directory and return its path."""
    path = Path(directory) / (_safe_filename(name) + ".json")
    _write_json_atomic(path, data)
    return path


def _local_preset_path(directory: Path, name: str) -> Path:
    """Find the preset called ``name`` in a local preset folder."""
    candidate = directory / (name + ".json")
    if candidate.is_file():
        return candidate
    for path in sorted(directory.glob("*.json")):
        data = _read_json_file(path)
        if data is not None and str(data.get("name") or "").strip() == name:
            return path
    raise SlicerProfilesError(
        "Could not find the parent preset {!r} in {!r}. Bambu Studio's presets may be "
        "incomplete; open Bambu Studio once so it updates them.".format(name, str(directory))
    )


def _flatten_local_preset(path: Path) -> Dict[str, Any]:
    """Merge a local preset with its ``inherits`` parent and ``include`` files."""

    def flatten(current: Path, chain: List[str]) -> Dict[str, Any]:
        data = _read_json_file(current)
        if data is None:
            raise SlicerProfilesError("Preset {!r} could not be read".format(str(current)))
        merged: Dict[str, Any] = {}
        parents = [str(data.get("inherits") or "").strip()]
        includes = data.get("include") if isinstance(data.get("include"), list) else []
        for name in parents + [str(item).strip() for item in includes]:
            if not name:
                continue
            if name in chain:
                raise SlicerProfilesError(
                    "Circular preset inheritance: {}".format(" -> ".join(chain + [name]))
                )
            merged.update(flatten(_local_preset_path(current.parent, name), chain + [name]))
        merged.update({key: value for key, value in data.items() if key not in ("inherits", "include")})
        return merged

    return flatten(Path(path), [str(Path(path).stem)])


def _standalone_bambu_preset(path: Any, for_bambu_studio: bool = False) -> str:
    """Return a stand-alone copy of a Bambu Studio system preset for the CLI.

    Bambu Studio's system presets hold only what differs from their parents,
    and neither command line resolves them reliably: OrcaSlicer 2.5 refuses
    Bambu Studio 2.9's vendor bundle ("Vendor bundle contains invalid
    presets"), and Bambu Studio's own command line does not read parents at
    all (a generic start G-code, 100 mm height and PLA temperatures).  The
    merged copy is ``from: user``.  For Bambu Studio it names itself in
    ``inherits`` (without that Bambu Studio reports "process not compatible
    with printer"); for OrcaSlicer it inherits nothing, like the flattened
    remote presets.  Other presets are returned unchanged.
    """
    preset_path = Path(str(path))
    system = _bambu_system_dir()
    if system is None:
        return str(preset_path)
    try:
        relative = preset_path.resolve().relative_to(system.resolve())
    except (OSError, ValueError):
        return str(preset_path)
    if _read_json_file(preset_path) is None:
        return str(preset_path)
    flattened = _flatten_local_preset(preset_path)
    flattened["from"] = "user"
    name = _preset_name(flattened, preset_path)
    if for_bambu_studio:
        flattened["inherits"] = name
    folder = "for_bambu_studio" if for_bambu_studio else "for_orca"
    output_dir = _cache_root() / "flattened" / "bambu_local" / folder / _safe_filename(relative.parts[0])
    return str(_write_flattened_profile(output_dir, name, flattened))


def _coloured_filament_presets(paths: Sequence[str], materials: Sequence[Any]) -> List[str]:
    """Give every filament slot its own preset copy carrying the material's colour.

    The slicer works out the purge volumes (``flush_volumes_matrix``) from the
    filament presets' colours.  Bambu Studio's presets have none, so the
    slicers used placeholder colours (OrcaSlicer: orange and white) and the
    purge volumes did not fit the real filaments.
    """
    directory = _cache_root() / "prepare" / "filaments"
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("*.json"):
        _remove_file_quietly(old)
    coloured: List[str] = []
    for index, path in enumerate(paths):
        data = _read_json_file(Path(path))
        if data is None or index >= len(materials):
            coloured.append(str(path))
            continue
        colour = _normalize_color_hex(_material_field(materials[index], "color_hex", "#FFFFFF"))
        data["default_filament_colour"] = [colour]
        data["filament_colour"] = [colour]
        target = directory / "{:02d} {}.json".format(
            index + 1, _safe_filename(_preset_name(data, Path(path)))
        )
        _write_json_atomic(target, data)
        coloured.append(str(target))
    return coloured


# ---------------------------------------------------------------------------
# Public vendor / printer / process listings
# ---------------------------------------------------------------------------


def list_vendors(slicer_exe: Any) -> List[Dict[str, Any]]:
    """List profile vendors available for targeting.

    Includes the local Bambu Studio preset library when installed, the curated
    remote OrcaSlicer vendors (tag-matched to the installed Orca version, with a
    ``main`` fallback; unreachable vendors are listed with ``has_printers``
    false instead of failing), and the add-in's imported profiles.

    Args:
        slicer_exe: Installed slicer executable used to derive the remote tag.

    Returns:
        ``[{"id", "name", "source", "has_printers"}]`` entries.

    Remote manifests use :data:`LIST_NETWORK_TIMEOUT_SECONDS`, and after the
    first connection-level failure the remaining vendors come from the cache
    only, so an offline machine lists in seconds instead of minutes.
    """
    if slicer_kind(slicer_exe) == "prusa":
        from . import prusa_profiles

        return prusa_profiles.list_vendors(resolve_slicer_executable(slicer_exe))
    vendors: List[Dict[str, Any]] = []
    network = _NetworkSession(timeout=LIST_NETWORK_TIMEOUT_SECONDS)

    bambu_dir = _bambu_system_dir()
    if bambu_dir is not None:
        has_local = bool(_scan_local_machines(bambu_dir))
        vendors.append(
            {
                "id": "bambu_local",
                "name": "Bambu Lab (local Bambu Studio presets)",
                "source": "bambu_local",
                "has_printers": has_local,
            }
        )

    for vendor_id, vendor_name in CURATED_VENDORS:
        has_printers = False
        try:
            manifest, _ref = _load_vendor_manifest(vendor_id, slicer_exe, network=network)
            machines = [
                entry
                for entry in _manifest_entries(manifest, "machine_list")
                if isinstance(entry.get("name"), str)
                and not str(entry.get("name")).startswith("fdm_")
            ]
            has_printers = bool(machines)
        except SlicerProfilesError:
            logger.warning(
                "Remote vendor %r could not be loaded; listing it as unavailable.",
                vendor_id,
                exc_info=network.allowed,
            )
        vendors.append(
            {
                "id": vendor_id,
                "name": vendor_name,
                "source": "orca_remote",
                "has_printers": has_printers,
            }
        )

    imported = imported_profiles()
    vendors.append(
        {
            "id": "imported",
            "name": "Imported Profiles",
            "source": "imported",
            "has_printers": bool(imported["machines"]),
        }
    )
    return vendors


def list_printers(slicer_exe: Any, vendor_id: str) -> List[Dict[str, Any]]:
    """List printer/machine presets for a vendor.

    Args:
        slicer_exe: Installed slicer executable.
        vendor_id: ``"bambu_local"``, ``"imported"`` or a remote vendor id.

    Returns:
        ``[{"name", "machine_path", "default_process", "source"}]`` entries.

    Raises:
        SlicerProfilesError: If the requested vendor is unavailable.
    """
    vendor = _require_text(vendor_id, "vendor id")
    if slicer_kind(slicer_exe) == "prusa":
        from . import prusa_profiles

        return prusa_profiles.list_printers(resolve_slicer_executable(slicer_exe), vendor)

    if vendor == "bambu_local":
        root = _bambu_system_dir()
        if root is None:
            raise SlicerProfilesError(
                "No Bambu Studio system profile directory was found; install Bambu "
                "Studio or choose a remote vendor instead."
            )
        return [
            {
                "name": preset["name"],
                "machine_path": preset["path"],
                "default_process": preset["data"].get("default_print_profile"),
                "source": "bambu_local",
            }
            for preset in _scan_local_machines(root)
        ]

    if vendor == "imported":
        summary = imported_profiles()
        return [
            {
                "name": entry["name"],
                "machine_path": entry["path"],
                "default_process": None,
                "source": "imported",
            }
            for entry in summary["machines"]
        ]

    network = _NetworkSession(timeout=LIST_NETWORK_TIMEOUT_SECONDS)
    manifest, ref = _load_vendor_manifest(vendor, slicer_exe, network=network)
    printers: List[Dict[str, Any]] = []
    for entry in _manifest_entries(manifest, "machine_list"):
        name = entry.get("name")
        sub_path = entry.get("sub_path")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(sub_path, str) or not sub_path.strip():
            continue
        if not _is_selectable_preset(name.strip(), entry):
            continue
        machine_path = _resolve_remote_preset(
            vendor, sub_path.strip(), slicer_exe=slicer_exe, ref=ref, download=False
        )
        default_process = entry.get("default_print_profile")
        printers.append(
            {
                "name": name.strip(),
                "machine_path": str(machine_path),
                "default_process": (
                    default_process if isinstance(default_process, str) else None
                ),
                "source": "orca_remote",
            }
        )
    return printers


def _local_default_process(root: Path, printer_name: str) -> Optional[str]:
    """Return the default print profile of a local machine preset, if any."""
    for preset in _scan_local_machines(root):
        if preset["name"] == printer_name:
            default_process = preset["data"].get("default_print_profile")
            if isinstance(default_process, str) and default_process.strip():
                return default_process.strip()
    return None


def _remote_default_process(
    vendor_id: str,
    slicer_exe: Any,
    printer_name: str,
    network: Optional[_NetworkSession] = None,
) -> Optional[str]:
    """Return the default print profile of a remote machine preset, if any."""
    manifest, ref = _load_vendor_manifest(vendor_id, slicer_exe, network=network)
    del manifest
    name_map = _build_remote_name_map(vendor_id, slicer_exe, ref, network=network)
    sub_path = name_map.get(printer_name)
    if sub_path is None:
        logger.debug(
            "Remote machine preset %r not found while reading its default process.",
            printer_name,
        )
        return None
    flattened = _flatten_remote_preset_at(
        vendor_id, sub_path, ref, name_map, network=network
    )
    default_process = flattened.get("default_print_profile")
    if isinstance(default_process, str) and default_process.strip():
        return default_process.strip()
    return None


_PRINTER_NAME_STOPWORDS = frozenset(
    {
        "bambu",
        "lab",
        "prusa",
        "elegoo",
        "creality",
        "anycubic",
        "sovol",
        "qidi",
        "voron",
        "flashforge",
        "artillery",
        "snapmaker",
        "printer",
        "nozzle",
        "mmu",
        "kit",
        "core",
        "one",
        "the",
        "fdm",
        "3d",
    }
)


def _filter_compatible_processes(
    processes: Sequence[Dict[str, Any]],
    printer_name: str,
    default_process: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return only process presets that appear to belong to this printer.

    A preset's own ``compatible_printers`` list (local Bambu Studio presets
    carry one) decides when it names this printer.  Otherwise the preset name's
    printer-family suffix (``@BBL X1C``, ``@MK4``, ``@CORE One``…) is matched
    by whole tokens, so ``A1`` does not match ``A1M`` and ``MK4`` does not match
    ``MK4S``.  Listing every process for a vendor makes it possible to select a
    process for a different printer, which produces mismatched projects; the
    full list is still returned when nothing matches.  The internal
    ``compatible_printers`` key is removed from the returned rows.
    """
    printer_key = str(printer_name or "").strip().lower()

    def listed(process: Mapping[str, Any]) -> Tuple[str, ...]:
        return _compatible_printer_names(process.get("compatible_printers"))

    def name_tokens(process: Mapping[str, Any]) -> set:
        return set(re.findall(r"[a-z0-9]+", str(process.get("name", "")).lower()))

    def strip(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {key: value for key, value in row.items() if key != "compatible_printers"}
            for row in rows
        ]

    use_lists = any(printer_key in listed(process) for process in processes)
    family = _printer_token(printer_name).lower()
    if family and " " not in family and any(
        family in name_tokens(process) for process in processes
    ):
        tokens = {family}
    else:
        tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", printer_key)
            if len(token) >= 2
            and token not in _PRINTER_NAME_STOPWORDS
            and not token.isdigit()
        }
    if not tokens and not use_lists:
        return strip(processes)

    def compatible(process: Mapping[str, Any]) -> bool:
        names = listed(process)
        if use_lists and names:
            return printer_key in names
        return bool(tokens & name_tokens(process))

    matching = [process for process in processes if compatible(process)]
    if not matching:
        return strip(processes)
    if default_process:
        default_item = next(
            (process for process in processes if process.get("name") == default_process),
            None,
        )
        if default_item is not None and all(
            process.get("name") != default_process for process in matching
        ):
            matching.insert(0, default_item)
    return strip(matching)


def list_processes(
    slicer_exe: Any, vendor_id: str, printer_name: str
) -> List[Dict[str, Any]]:
    """List process presets for a vendor/printer pair.

    Args:
        slicer_exe: Installed slicer executable.
        vendor_id: ``"bambu_local"``, ``"imported"`` or a remote vendor id.
        printer_name: Machine preset name used to mark the default process.

    Returns:
        ``[{"name", "process_path", "default"}]`` entries.

    Raises:
        SlicerProfilesError: If the requested vendor is unavailable.
    """
    vendor = _require_text(vendor_id, "vendor id")
    printer = _require_text(printer_name, "printer name")
    if slicer_kind(slicer_exe) == "prusa":
        from . import prusa_profiles

        return prusa_profiles.list_processes(resolve_slicer_executable(slicer_exe), vendor, printer)

    if vendor == "bambu_local":
        root = _bambu_system_dir()
        if root is None:
            raise SlicerProfilesError(
                "No Bambu Studio system profile directory was found; install Bambu "
                "Studio or choose a remote vendor instead."
            )
        default_process = _local_default_process(root, printer)
        local_processes = [
            {
                "name": preset["name"],
                "process_path": preset["path"],
                "default": preset["name"] == default_process,
                "compatible_printers": preset["data"].get("compatible_printers"),
            }
            for preset in _scan_local_processes(root)
        ]
        processes = _filter_compatible_processes(local_processes, printer, default_process)
        for process in processes:
            # The process's own infill, walls and layers: the palette shows them
            # in empty body setting fields.
            try:
                process["values"] = body_settings.process_values(
                    _flatten_local_preset(Path(process["process_path"])), "bambu")
            except (SlicerProfilesError, OSError, ValueError):
                process["values"] = {}
        return processes

    if vendor == "imported":
        summary = imported_profiles()
        return [
            {
                "name": entry["name"],
                "process_path": entry["path"],
                "default": False,
            }
            for entry in summary["processes"]
        ]

    network = _NetworkSession(timeout=LIST_NETWORK_TIMEOUT_SECONDS)
    manifest, ref = _load_vendor_manifest(vendor, slicer_exe, network=network)
    default_process = _remote_default_process(vendor, slicer_exe, printer, network=network)
    processes: List[Dict[str, Any]] = []
    for entry in _manifest_entries(manifest, "process_list"):
        name = entry.get("name")
        sub_path = entry.get("sub_path")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(sub_path, str) or not sub_path.strip():
            continue
        if not _is_selectable_preset(name.strip(), entry, skip_common=True):
            continue
        process_path = _resolve_remote_preset(
            vendor, sub_path.strip(), slicer_exe=slicer_exe, ref=ref, download=False
        )
        processes.append(
            {
                "name": name.strip(),
                "process_path": str(process_path),
                "default": name.strip() == default_process,
            }
        )
    return _filter_compatible_processes(processes, printer, default_process)


# ---------------------------------------------------------------------------
# Imported profiles
# ---------------------------------------------------------------------------


def imported_profiles() -> Dict[str, Any]:
    """Summarise the add-in's imported machine/process/filament profiles.

    The directories are created when missing so the palette can show the import
    target.  Unreadable or malformed JSON files are skipped with a logged error.

    Returns:
        ``{"machines": [...], "processes": [...], "filaments": [...], "root": str}``
        where each entry exposes ``name``, ``path`` and ``type`` (plus
        ``setting_id`` when the preset provides one).
    """
    root = _profiles_root()
    summary: Dict[str, Any] = {
        "machines": [],
        "processes": [],
        "filaments": [],
        "root": str(root),
    }
    for kind, plural in _KIND_TO_PLURAL.items():
        directory = _profile_dir(kind)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("Could not create imported profile directory %r.", str(directory))
            continue
        try:
            paths = sorted(directory.glob("*.json"))
        except OSError:
            logger.exception("Could not scan imported profile directory %r.", str(directory))
            continue
        for path in paths:
            data = _read_json_file(path)
            if data is None:
                continue
            entry: Dict[str, Any] = {
                "name": _preset_name(data, path),
                "path": str(path),
                "type": kind,
            }
            setting_id = data.get("setting_id")
            if isinstance(setting_id, str) and setting_id.strip():
                entry["setting_id"] = setting_id.strip()
            summary[plural].append(entry)
    return summary


def _existing_profile_keys() -> Dict[str, Tuple[set, set]]:
    """Return existing imported ``(names, setting_ids)`` sets per preset kind."""
    keys: Dict[str, Tuple[set, set]] = {
        kind: (set(), set()) for kind in _KIND_TO_PLURAL
    }
    for kind, (names, setting_ids) in keys.items():
        try:
            paths = sorted(_profile_dir(kind).glob("*.json"))
        except OSError:
            continue
        for path in paths:
            data = _read_json_file(path)
            if data is None:
                continue
            names.add(_preset_name(data, path))
            setting_id = data.get("setting_id")
            if isinstance(setting_id, str) and setting_id.strip():
                setting_ids.add(setting_id.strip())
    return keys


def _unique_profile_target(directory: Path, name: str) -> Path:
    """Return a non-conflicting target path for an imported preset."""
    stem = _safe_filename(name)
    candidate = directory / (stem + ".json")
    counter = 2
    while candidate.exists():
        candidate = directory / ("{}-{}.json".format(stem, counter))
        counter += 1
    return candidate


def import_profile_files(paths: Sequence[Any]) -> Dict[str, Any]:
    """Validate and copy externally supplied preset JSON files into the add-in.

    Each file must contain a JSON object with a recognised ``type`` field
    (``machine``/``printer``, ``process``/``print`` or ``filament``).  Duplicates
    are skipped by preset ``name`` or ``setting_id``.

    Args:
        paths: Paths of the JSON preset files to import.

    Returns:
        The same summary shape as :func:`imported_profiles` after importing.

    Raises:
        SlicerProfilesError: If ``paths`` is ``None`` or a file is missing,
            unreadable, not a JSON object, or has no recognised type.
    """
    if paths is None:
        raise SlicerProfilesError("Profile paths must be a sequence, got None")
    existing = _existing_profile_keys()
    imported_count = 0
    for raw_path in paths:
        source = Path(raw_path)
        try:
            if not source.is_file():
                raise SlicerProfilesError(
                    "Profile file not found: {!r}".format(str(source))
                )
        except OSError as exc:
            raise SlicerProfilesError(
                "Profile file could not be inspected: {!r}: {}".format(
                    str(source), exc
                )
            ) from exc
        data = _read_json_file(source)
        if data is None:
            raise SlicerProfilesError(
                "Profile file {!r} is unreadable or not a JSON object".format(
                    str(source)
                )
            )
        kind = _profile_kind(data)
        if kind is None:
            raise SlicerProfilesError(
                "Profile file {!r} has no recognised 'type' field (expected machine, "
                "process or filament)".format(str(source))
            )
        name = _preset_name(data, source)
        setting_id = data.get("setting_id")
        setting_id = setting_id.strip() if isinstance(setting_id, str) else ""
        names, setting_ids = existing[kind]
        if name in names or (setting_id and setting_id in setting_ids):
            logger.info(
                "Skipping duplicate imported %s preset %r (already imported).",
                kind,
                name,
            )
            continue
        directory = _profile_dir(kind)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SlicerProfilesError(
                "Could not create the imported profile directory {!r}: {}".format(
                    str(directory), exc
                )
            ) from exc
        target = _unique_profile_target(directory, name)
        try:
            _write_json_atomic(target, data)
        except OSError as exc:
            raise SlicerProfilesError(
                "Could not import profile {!r} to {!r}: {}".format(
                    str(source), str(target), exc
                )
            ) from exc
        names.add(name)
        if setting_id:
            setting_ids.add(setting_id)
        imported_count += 1
        logger.info("Imported %s preset %r to %r.", kind, name, str(target))
    logger.info(
        "Imported %d profile file(s); %d already present.",
        imported_count,
        max(0, len(list(paths)) - imported_count),
    )
    return imported_profiles()


# ---------------------------------------------------------------------------
# Filament preset selection
# ---------------------------------------------------------------------------


def _preset_type_tokens(data: Dict[str, Any]) -> set:
    """Return the filament-type tokens of a filament preset."""
    tokens: set = set()
    raw_type = data.get("filament_type")
    if isinstance(raw_type, str) and raw_type.strip():
        tokens.add(raw_type.strip().upper())
    elif isinstance(raw_type, (list, tuple)):
        for item in raw_type:
            if isinstance(item, str) and item.strip():
                tokens.add(item.strip().upper())
    name = str(data.get("name", "")).upper()
    for token in _FILAMENT_TYPE_TOKENS:
        if token in name:
            tokens.add(token)
    return tokens


def _local_filament_candidates(root: Optional[Path]) -> List[Dict[str, Any]]:
    """Return the Bambu Studio filament presets usable as CLI filament files."""
    if root is None:
        return []
    candidates: List[Dict[str, Any]] = []
    for preset in _scan_local_filaments(root):
        candidates.append(
            {
                "path": preset["path"],
                "name": preset["name"],
                "file_name": Path(preset["path"]).name,
                "types": _preset_type_tokens(preset["data"]),
                "compatible_printers": _compatible_printer_names(
                    preset["data"].get("compatible_printers")
                ),
            }
        )
    return candidates


def _imported_filament_candidates() -> List[Dict[str, Any]]:
    """Return imported filament presets usable as CLI filament files."""
    candidates: List[Dict[str, Any]] = []
    for entry in imported_profiles()["filaments"]:
        data = _read_json_file(Path(entry["path"]))
        if data is None:
            continue
        candidates.append(
            {
                "path": entry["path"],
                "name": entry["name"],
                "file_name": Path(entry["path"]).name,
                "types": _preset_type_tokens(data),
                "compatible_printers": _compatible_printer_names(
                    data.get("compatible_printers")
                ),
            }
        )
    return candidates


def _compatible_printer_names(value: Any) -> Tuple[str, ...]:
    """Return a preset's ``compatible_printers`` list, lower-cased (``()`` when absent)."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        item.strip().lower() for item in value if isinstance(item, str) and item.strip()
    )


def _has_whole_token(upper_text: str, token: str) -> bool:
    """Return whether ``token`` occurs in ``upper_text`` as whole word(s)."""
    pattern = r"(?<![A-Z0-9])" + re.escape(token) + r"(?![A-Z0-9])"
    return re.search(pattern, upper_text) is not None


#: Printers OrcaSlicer cannot slice yet (``orca_unsupported_tokens`` in ``data/printers.json``).
ORCA_UNSUPPORTED_PRINTERS: Tuple[str, ...] = tuple(printer_catalog.orca_unsupported())


def orca_supports_printer(printer_name: str) -> bool:
    """False for a printer OrcaSlicer cannot open or slice (Bambu Lab H3C)."""
    return _printer_token(printer_name) not in ORCA_UNSUPPORTED_PRINTERS


def _printer_token(printer_name: str) -> str:
    """Return a coarse printer-family token for preset compatibility filtering.

    Only used when a preset has no ``compatible_printers`` list.  Tokens are
    matched as whole words: ``Bambu Lab A1 0.4 nozzle`` is ``A1``, not ``A1M``.
    """
    upper = str(printer_name or "").upper()
    for phrase, token in _PRINTER_ALIASES:
        if _has_whole_token(upper, phrase):
            return token
    for token in _PRINTER_TOKENS:
        if _has_whole_token(upper, token):
            return token
    return ""


def _printer_tokens_compatible(target: str, candidate: str) -> bool:
    """True when a preset's printer token can be used for the target printer."""
    if not target or not candidate:
        return True
    if target == candidate:
        return True
    # X1 Carbon presets are commonly named X1C while the Target name is X1 Carbon.
    if {target, candidate} == {"X1", "X1C"}:
        return True
    return False


def _pick_filament_candidate(
    candidates: Sequence[Dict[str, Any]],
    filament_type: Any,
    printer_name: str,
    preferred_preset: str = "",
    use_compatible_lists: bool = True,
) -> Optional[str]:
    """Pick a compatible filament preset for the printer and material.

    A preset's ``compatible_printers`` list decides compatibility when it has
    one; otherwise the ``@BBL <code>`` suffix and nozzle size are compared with
    the target.  Scoring prefers the spool's own Orca preset, the target nozzle
    size and the printer, and avoids special-purpose materials such as PLA Aero
    when a generic Basic/Matte preset exists for the requested type.  When the
    lists rule out every candidate (for example because the printer preset was
    renamed), the pick is retried on names alone.
    """
    if not candidates:
        return None
    wanted = str(filament_type or "PLA").strip().upper()
    base_token = wanted.split("-")[0]
    matches = [
        candidate
        for candidate in candidates
        if base_token in candidate["types"] or wanted in candidate["types"]
    ]
    if not matches:
        return None
    printer_upper = str(printer_name or "").upper()
    nozzle_match = re.search(r"(\d+(?:\.\d+)?)\s*NOZZLE", printer_upper)
    target_nozzle = nozzle_match.group(1) if nozzle_match else ""
    preferred = str(preferred_preset or "").strip()
    preferred_upper = preferred.upper()
    printer_token = _printer_token(printer_name)
    printer_key = str(printer_name or "").strip().lower()
    scored: List[Tuple[int, int, Dict[str, Any]]] = []
    for index, candidate in enumerate(matches):
        text = (candidate["name"] + " " + candidate["file_name"]).upper()
        candidate_printer = re.search(r"@BBL\s+([A-Z0-9]+)", text)
        candidate_printer_token = candidate_printer.group(1) if candidate_printer else ""
        candidate_nozzle = re.search(r"(\d+(?:\.\d+)?)\s*NOZZLE", text)
        candidate_nozzle_text = candidate_nozzle.group(1) if candidate_nozzle else ""
        compatible_names = (
            candidate.get("compatible_printers") or () if use_compatible_lists else ()
        )
        listed = bool(compatible_names) and bool(printer_key)
        if listed and printer_key not in compatible_names:
            continue
        # Without a list, a preset bound to another machine or nozzle size is
        # not compatible with this target; generic presets (no suffix) are fine.
        if not listed and printer_token and candidate_printer_token and not _printer_tokens_compatible(
            printer_token, candidate_printer_token
        ):
            continue
        if (
            not listed
            and target_nozzle
            and candidate_nozzle_text
            and candidate_nozzle_text != target_nozzle
        ):
            continue
        score = _preferred_score(text, preferred)
        if score == 0 and "GENERIC" in text:
            # Another brand's filament gets the vendor's generic preset, not one of
            # its own brand (a Bambu PLA on a Prusa printer: "Generic PLA", not
            # "Prusament PLA").  A Bambu "Basic" preset still wins (+5 below).
            score += 3
        if target_nozzle:
            score += 80 if candidate_nozzle_text == target_nozzle else 10
        if listed or (printer_token and candidate_printer_token == printer_token):
            score += 100
        special_terms = (
            "AERO", "SUPPORT", "CF", "SILK", "WOOD", "METAL", "GALAXY",
            "MARBLE", "TRANSLUCENT", "HIGH SPEED", "SPARKLE",
        )
        for term in special_terms:
            if term in text and term not in wanted and term not in preferred_upper:
                score -= 90
        if "BASIC" in text or "MATTE" in text:
            score += 5
        scored.append((score, -index, candidate))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not scored or scored[0][0] < 0:
        if use_compatible_lists and any(
            candidate.get("compatible_printers") for candidate in matches
        ):
            logger.debug(
                "No %s preset lists %r as compatible; retrying on preset names.",
                wanted,
                printer_name,
            )
            return _pick_filament_candidate(
                candidates,
                filament_type,
                printer_name,
                preferred_preset,
                use_compatible_lists=False,
            )
        return None
    return scored[0][2]["path"]


def _preferred_score(text: str, preferred: str) -> int:
    """200 when a preset's upper-case name/file text holds the wanted preset
    name, 180 when it holds all its words, else 0."""
    preferred_upper = str(preferred or "").strip().upper()
    preferred_core = re.split(r"\s+@", preferred_upper, maxsplit=1)[0].strip()
    if preferred_upper and (preferred_upper in text or preferred_core in text):
        return 200
    words = [word for word in preferred_core.replace("@", " ").split() if word]
    if words and all(word in text for word in words):
        return 180
    return 0


def _read_preset_identity(path: Any) -> Tuple[str, str, str]:
    """Return ``(name, filament_id, vendor)`` for a slicer filament preset.

    Bambu system presets inherit the material identity from a ``@base`` parent,
    so the child often has only ``setting_id``.  Walk the local ``inherits``
    chain to recover the real preset name / ``filament_id`` that Orca expects in
    ``filament_settings_id`` / ``filament_ids``.
    """
    seen: set = set()

    def _load(candidate: Path) -> Dict[str, Any]:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        key = str(resolved)
        if key in seen:
            return {}
        seen.add(key)
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _walk(candidate: Path) -> Tuple[str, str, str]:
        data = _load(candidate)
        if not data:
            return "", "", ""
        name = str(data.get("name") or "").strip()
        identifier = str(data.get("filament_id") or "").strip()
        setting_id = str(data.get("setting_id") or "").strip()
        setting_identifier = setting_id.split("_", 1)[0].strip() if setting_id else ""
        vendor = data.get("filament_vendor")
        vendor_text = ""
        if isinstance(vendor, str):
            vendor_text = vendor.strip()
        elif isinstance(vendor, (list, tuple)) and vendor:
            vendor_text = str(vendor[0]).strip()
        parent_name = str(data.get("inherits") or "").strip()
        if parent_name:
            parent = candidate.parent / (parent_name + ".json")
            parent_name_from_parent, parent_id, parent_vendor = _walk(parent)
            if not name:
                name = parent_name_from_parent
            if not identifier:
                identifier = parent_id
            if not vendor_text:
                vendor_text = parent_vendor
        if not identifier:
            identifier = setting_identifier
        return name, identifier, vendor_text

    return _walk(Path(str(path)))


def _preferred_preset_for_material(material: Any, kind: str = "") -> str:
    """Return an Orca preset search hint for a detected material.

    Catalog/Bambuddy entries carry ``orca_filament_profile`` directly; Open
    Filament Database entries may name the slicer's preset in their
    ``slicer_data`` (``kind``: "bambu" or "orca"). For
    design materials that were renamed to the friendly
    ``Brand - Material - Color`` format, reconstruct the product hint from the
    display name (e.g. ``Bambu Lab - PLA Matte - Charcoal`` -> ``Bambu PLA
    Matte``) so Orca is not handed a generic or special-purpose variant.
    """
    explicit = _named_preset_hint(material, kind)
    if explicit:
        return explicit
    display = str(_material_field(material, "display_name", "") or "").strip()
    parts = [part.strip() for part in display.split(" - ") if part.strip()]
    if len(parts) >= 2:
        # Use the product/material phrase only. Brand prefixes vary between the
        # Friendly name ("Bambu Lab ...") and Orca preset names ("Bambu ...").
        product = parts[1]
        if product:
            return product
    return ""


def _named_preset_hint(material: Any, kind: str = "") -> str:
    """The preset a material names outright: its ``orca_filament_profile`` (a
    catalog entry, or the loaded spool's preset) or its Open Filament Database
    preset name; "" when only its display name hints at one."""
    explicit = str(_material_field(material, "orca_filament_profile", "") or "").strip()
    if explicit:
        return explicit
    slicer_data = _material_field(material, "slicer_data", None)
    if isinstance(slicer_data, MappingABC):
        return filament_presets.preset_hint(slicer_data, kind or "orca")
    return ""


def _remote_filament_candidates(
    target: "ProfileTarget", slicer_exe: Any, wanted_types: Sequence[str]
) -> List[Dict[str, Any]]:
    """The online vendor's own filament presets that may fit the target printer.

    For a printer from OrcaSlicer's online profiles (a Prusa XL, for example)
    Bambu Studio's filament presets carry names OrcaSlicer does not know for
    that printer, so OrcaSlicer shows other presets ("Voron Generic ABS").
    This lists the vendor's presets whose ``@`` suffix fits the printer name
    ("Generic PLA @Prusa XL", "Prusament PLA @XL", "Generic PLA @Prusa") and
    the shared library's "Generic <type> @System", for the wanted types only,
    and flattens each into the cache (downloaded once, then cached).  Network
    failures give fewer candidates, never an error.
    """
    vendor = _require_text(target.vendor, "remote vendor id")
    network = _NetworkSession()
    try:
        manifest, ref = _load_vendor_manifest(vendor, slicer_exe, network=network)
        name_map = _build_remote_name_map(vendor, slicer_exe, ref, network=network)
    except SlicerProfilesError:
        logger.warning("No online filament presets for %s; using the installed ones.", vendor, exc_info=True)
        return []
    printer_words = set(re.findall(r"[A-Z0-9.+]+", str(target.printer_name).upper()))
    types = {str(item).strip().upper() for item in wanted_types if str(item).strip()}
    tokens = types | {item.split("-")[0] for item in types}
    names: List[str] = []
    for entry in _manifest_entries(manifest, "filament_list"):
        name = str(entry.get("name") or "").strip()
        if "@" not in name:
            continue
        product, suffix = name.rsplit("@", 1)
        suffix_words = set(re.findall(r"[A-Z0-9.+]+", suffix.upper()))
        if not suffix_words or "BASE" in suffix_words or not suffix_words <= printer_words:
            continue
        if set(re.findall(r"[A-Z0-9]+", product.upper())) & tokens:
            names.append(name)
    names += [
        "Generic {} @System".format(token)
        for token in sorted(tokens)
        if "Generic {} @System".format(token) in name_map
    ]
    directory = _cache_root() / "flattened" / ref / _safe_filename(vendor) / "filament"
    candidates: List[Dict[str, Any]] = []
    for name in names:
        sub_path = name_map.get(name)
        if not sub_path:
            continue
        try:
            data = _flatten_remote_preset_at(vendor, sub_path, ref, name_map, network=network)
        except SlicerProfilesError:
            logger.debug("Skipping online filament preset %r.", name, exc_info=True)
            continue
        if str(data.get("instantiation", "true")).strip().lower() == "false":
            continue
        data["name"] = name
        preset_path = _write_flattened_profile(directory, name, data)
        candidates.append(
            {
                "path": str(preset_path),
                "name": name,
                "file_name": preset_path.name,
                "types": _preset_type_tokens(data),
                "compatible_printers": _compatible_printer_names(data.get("compatible_printers")),
            }
        )
    logger.info("%d online %s filament preset(s) may fit %r.", len(candidates), vendor, target.printer_name)
    return candidates


def _choose_filament_paths(
    materials: Sequence[Any],
    printer_name: str,
    preferred_candidates: Sequence[Dict[str, Any]] = (),
    for_bambu_studio: bool = False,
    built: Optional[List[str]] = None,
) -> List[str]:
    """Choose one filament preset file per material for ``--load-filaments``.

    ``preferred_candidates`` (the online vendor's own presets, see
    :func:`_remote_filament_candidates`) come first; then local Bambu Studio
    filament presets (matched by detected filament type and printer token);
    imported filament presets are the fallback.  When the chosen preset is
    not the one the material names, the slicers' own user presets of that
    name are used, else a preset is built from the material's Open Filament
    Database data (:mod:`filament_presets`); ``built`` collects those.  When
    a type has no exact match, the first chosen file is reused (passing the
    same file for multiple slots is verified to work).
    """
    bambu_candidates = _local_filament_candidates(_bambu_system_dir())
    imported_candidates = _imported_filament_candidates()
    if not preferred_candidates and not bambu_candidates and not imported_candidates:
        raise SlicerProfilesError(
            "No filament profiles are available. Install Bambu Studio (its system "
            "filament presets are used) or import filament JSON profiles with the "
            "palette's profile importer."
        )
    kind = "bambu" if for_bambu_studio else "orca"
    own_candidates: Optional[List[Dict[str, Any]]] = None
    selected: List[Optional[str]] = []
    wanted_types: List[str] = []
    for material in materials:
        filament_type = _material_field(material, "filament_type", "PLA")
        wanted_types.append(str(filament_type or "PLA").strip().upper())
        preferred = _preferred_preset_for_material(material, kind)
        chosen = (
            _pick_filament_candidate(list(preferred_candidates), filament_type, printer_name, preferred)
            or _pick_filament_candidate(bambu_candidates, filament_type, printer_name, preferred)
            or _pick_filament_candidate(imported_candidates, filament_type, printer_name, preferred)
        )
        named = _named_preset_hint(material, kind)
        has_data = bool(_material_field(material, "slicer_data", None))
        if (named or has_data) and not (preferred and _is_material_preset(chosen, material, preferred, named)):
            own = None
            if named:
                # Only a name the material gives outright picks a user preset.
                if own_candidates is None:
                    own_candidates = _own_filament_candidates(kind, printer_name)
                own = _pick_filament_candidate(own_candidates, filament_type, printer_name, named)
                if own is not None and not _names_preset(own, named):
                    own = None
            copy = _cli_preset_copy(Path(own), for_bambu_studio, "own") if own is not None else None
            if copy is not None:
                chosen = copy
                logger.info("Using the slicer user preset %r.", Path(own).stem)
            else:
                made = _database_preset(
                    material, chosen, list(preferred_candidates) + bambu_candidates, printer_name, kind
                )
                if made is not None:
                    chosen = made
                    if built is not None:
                        built.append(made)
        selected.append(chosen)
    fallback = next((path for path in selected if path is not None), None)
    if fallback is None:
        raise SlicerProfilesError(
            "None of the available filament profiles match the detected filament "
            "types ({}). Import matching filament profiles and retry.".format(
                ", ".join(sorted(set(wanted_types)))
            )
        )
    resolved = [path if path is not None else fallback for path in selected]
    logger.info(
        "Selected %d filament preset file(s) for the slicer CLI.", len(resolved)
    )
    return resolved


def _names_preset(path: Optional[str], preferred: str) -> bool:
    """True when the preset file at ``path`` is the one ``preferred`` names."""
    if path is None:
        return False
    data = _read_json_file(Path(path)) or {}
    text = (str(data.get("name") or "") + " " + Path(path).name).upper()
    return _preferred_score(text, preferred) > 0


def _is_material_preset(path: Optional[str], material: Any, preferred: str, named: str) -> bool:
    """True when the preset at ``path`` is the material's own filament.

    It must name the preferred preset.  For a database filament found by its
    product name only ("PETG" from "Voxel PLA - PETG - Red") it must also be
    of the material's brand, or a Voxel PETG would get Bambu PETG Basic.
    """
    if not _names_preset(path, preferred):
        return False
    if named or not _material_field(material, "slicer_data", None):
        return True
    name, _identifier, vendor = _read_preset_identity(path)
    return _same_brand(str(_material_field(material, "manufacturer", "") or ""), vendor, name or Path(str(path)).stem)


def _same_brand(brand: str, vendor: str, preset_name: str) -> bool:
    """True when a preset is of ``brand``: by its vendor, or by the brand's
    first word in its name ("Bambu PETG Basic" for "Bambu Lab").  An empty or
    generic brand fits every preset."""

    def key(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", text.lower())

    wanted = key(brand)
    if wanted in ("", "generic", "unknown", "unknownbrand"):
        return True
    have = key(vendor)
    if have and (have.startswith(wanted) or wanted.startswith(have)):
        return True
    first = key(brand.split()[0]) if brand.split() else ""
    return bool(first) and first in key(preset_name)


def _own_filament_candidates(kind: str, printer_name: str) -> List[Dict[str, Any]]:
    """The slicers' own user filament presets for ``printer_name``, this slicer's first.

    Only presets that list the printer (or list none): a user preset made for
    another printer never stands in, not even on a name match.
    """
    printer_key = str(printer_name or "").strip().lower()
    candidates: List[Dict[str, Any]] = []
    for slicer in (kind, "orca" if kind == "bambu" else "bambu"):
        for preset in filament_presets.user_presets(slicer, all_folders=True):
            data = preset["data"]
            listed = _compatible_printer_names(data.get("compatible_printers"))
            if listed and printer_key not in listed:
                continue
            candidates.append(
                {
                    "path": preset["path"],
                    "name": preset["name"],
                    "file_name": Path(preset["path"]).name,
                    "types": _preset_type_tokens(data),
                    "compatible_printers": _compatible_printer_names(data.get("compatible_printers")),
                }
            )
    return candidates


def _cli_preset_copy(path: Path, for_bambu_studio: bool, folder: str, data: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """A stand-alone copy of a user or built preset in the form the slicer CLI reads.

    A user preset that inherits a Bambu Studio system preset is merged with
    it; one whose parent is not installed is skipped (None).  The copy says
    ``"type": "filament"`` (user presets made in Bambu Studio's filament
    wizard lack it and OrcaSlicer's CLI then refuses them) and, for Bambu
    Studio, names itself in ``inherits`` like :func:`_standalone_bambu_preset`.
    """
    if data is None:
        data = _read_json_file(path)
        if data is None:
            return None
        parent = str(data.get("inherits") or "").strip()
        if parent:
            system = _bambu_system_dir()
            try:
                if system is None:
                    raise SlicerProfilesError("no Bambu Studio system presets")
                merged = _flatten_local_preset(_local_preset_path(system / "filament", parent))
            except SlicerProfilesError:
                logger.info("Skipping the user preset %r: its parent %r is not installed.", str(path), parent)
                return None
            merged.update({key: value for key, value in data.items() if key not in ("inherits", "include")})
            data = merged
    data = dict(data)
    for key in ("inherits", "include", "setting_id", "base_id"):
        data.pop(key, None)
    name = _preset_name(data, path)
    data.update(name=name, type="filament", instantiation="true")
    data["from"] = "user"
    if for_bambu_studio:
        data["inherits"] = name
    directory = _cache_root() / "prepare" / folder
    return str(_write_flattened_profile(directory, name, data))


def _database_preset(
    material: Any,
    chosen: Optional[str],
    candidates: Sequence[Dict[str, Any]],
    printer_name: str,
    kind: str,
) -> Optional[str]:
    """Build a preset from the material's Open Filament Database data.

    The base is the slicer's generic preset of the database's
    ``generic_id`` (Bambu Lab ids: GFB99 is Generic ABS), else the generic
    preset for the type, else the chosen preset.  None when the material
    has no database data or no base was found.
    """
    slicer_data = _material_field(material, "slicer_data", None)
    if not isinstance(slicer_data, MappingABC) or not slicer_data:
        return None
    filament_type = str(_material_field(material, "filament_type", "PLA") or "PLA")
    wanted = filament_type.strip().upper()
    base_path: Optional[str] = None
    generic_id = str(filament_presets.database_settings(slicer_data, kind).get("generic_id") or "").strip()
    if generic_id:
        same_id = [
            candidate
            for candidate in candidates
            if (wanted.split("-")[0] in candidate["types"] or wanted in candidate["types"])
            and _read_preset_identity(candidate["path"])[1] == generic_id
        ]
        base_path = _pick_filament_candidate(same_id, filament_type, printer_name)
    if base_path is None:
        generic = "Generic " + filament_type.strip().upper()
        base_path = _pick_filament_candidate(list(candidates), filament_type, printer_name, generic)
    base_path = base_path or chosen
    if base_path is None:
        return None
    system = _bambu_system_dir()
    inside_system = False
    if system is not None:
        try:
            Path(base_path).resolve().relative_to(system.resolve())
            inside_system = True
        except (OSError, ValueError):
            pass
    try:
        base = _flatten_local_preset(Path(base_path)) if inside_system else (_read_json_file(Path(base_path)) or {})
    except SlicerProfilesError:
        logger.warning("Could not read the base preset %r.", base_path, exc_info=True)
        return None
    if not base:
        return None
    preset = filament_presets.build_preset(
        base,
        slicer_data,
        kind,
        str(_material_field(material, "manufacturer", "") or ""),
        str(_material_field(material, "product_line", "") or _material_field(material, "display_name", "") or ""),
        printer_name,
    )
    logger.info("Built the filament preset %r from %r and the Open Filament Database.", preset["name"], Path(base_path).stem)
    return _cli_preset_copy(Path(base_path), kind == "bambu", "built", data=preset)


def _filament_preset_identities(paths: Sequence[str]) -> List[Tuple[str, str, str]]:
    """Resolve identity metadata for every selected filament preset path."""
    identities: List[Tuple[str, str, str]] = []
    for raw_path in paths:
        try:
            identities.append(_read_preset_identity(raw_path))
        except Exception:
            logger.debug("Could not read filament preset identity from %r", raw_path, exc_info=True)
            identities.append(("", "", ""))
    return identities


# ---------------------------------------------------------------------------
# ZIP helpers and project_settings post-processing
# ---------------------------------------------------------------------------


def _clone_zip_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    """Return a fresh ``ZipInfo`` preserving the original entry metadata.

    Keeps the same fields as the injector's ``_write_entry``: name, date_time,
    compress_type, internal/external attributes, create_system and comment.
    """
    clone = zipfile.ZipInfo(info.filename, info.date_time)
    clone.compress_type = info.compress_type
    clone.internal_attr = info.internal_attr
    clone.external_attr = info.external_attr
    clone.create_system = info.create_system
    clone.comment = info.comment
    return clone


def _new_zip_info(name: str) -> zipfile.ZipInfo:
    """Return metadata for an entry that did not exist in the source archive."""
    info = zipfile.ZipInfo(name, time.localtime()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    info.create_system = 3
    return info


def _copy_zip_entries(
    source: zipfile.ZipFile,
    target: zipfile.ZipFile,
    replacements: Mapping[str, Optional[bytes]],
) -> None:
    """Copy ``source`` into ``target``, applying ``replacements``.

    A ``bytes`` value replaces the first entry of that name in place (later
    duplicates are dropped) or is appended when the entry is new; ``None``
    drops every entry of that name.
    """
    written: set = set()
    target.comment = source.comment
    for info in source.infolist():
        name = info.filename
        if name in replacements:
            payload = replacements[name]
            if payload is None or name in written:
                continue
            target.writestr(_clone_zip_info(info), payload)
            written.add(name)
            continue
        target.writestr(_clone_zip_info(info), b"" if info.is_dir() else source.read(info))
    for name, payload in replacements.items():
        if payload is not None and name not in written:
            target.writestr(_new_zip_info(name), payload)


def rewrite_zip_entries(
    path: Path,
    replacements: Mapping[str, Optional[bytes]],
    required: Sequence[str] = (),
) -> None:
    """Atomically replace, add or drop several archive entries in one pass.

    Every other entry is copied with its metadata.  ``None`` values drop an
    entry; names in ``required`` must already exist in the archive.  The new
    archive is built next to ``path`` and moved into place with
    :func:`os.replace`.

    Raises:
        SlicerProfilesError: If a required entry is missing or the archive
            cannot be read or written.
    """
    path = Path(path)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".filamatch_zip_", suffix=".3mf", dir=str(path.parent)
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        with zipfile.ZipFile(str(path), "r") as source:
            names = set(source.namelist())
            missing = [name for name in required if name not in names]
            if missing:
                raise SlicerProfilesError(
                    "Cannot rewrite missing archive entry {!r} in {!r}".format(
                        missing[0], str(path)
                    )
                )
            with zipfile.ZipFile(str(temporary_path), "w") as target:
                _copy_zip_entries(source, target, replacements)
        # mkstemp files are owner-only; the 3MF is a normal user document.
        os.chmod(str(temporary_path), 0o644)
        os.replace(str(temporary_path), str(path))
    except (OSError, zipfile.BadZipFile) as exc:
        raise SlicerProfilesError(
            "Could not rewrite the archive {!r}: {}".format(str(path), exc)
        ) from exc
    finally:
        _remove_file_quietly(temporary_path)


def _rewrite_zip_entry(path: Path, entry_name: str, new_bytes: bytes) -> None:
    """Atomically rewrite one existing archive entry, preserving every other entry."""
    rewrite_zip_entries(path, {entry_name: new_bytes}, required=(entry_name,))


def _transform_point(matrix: Sequence[float], point: Tuple[float, float, float]) -> Tuple[float, float, float]:
    """Apply a 3MF ``transform`` (row-major 3x4, translation last) to a point."""
    x, y, z = point
    return (
        x * matrix[0] + y * matrix[3] + z * matrix[6] + matrix[9],
        x * matrix[1] + y * matrix[4] + z * matrix[7] + matrix[10],
        x * matrix[2] + y * matrix[5] + z * matrix[8] + matrix[11],
    )


def _parse_transform(text: Optional[str]) -> Tuple[float, ...]:
    """Parse a 3MF transform attribute, defaulting to identity."""
    try:
        values = tuple(float(value) for value in str(text or "").split())
    except ValueError:
        values = ()
    return values if len(values) == 12 else (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0)


def _base_body_boxes(base_path: Path) -> List[Tuple[float, float, float, float, float, float]]:
    """Return one world bounding box ``(x0, y0, z0, x1, y1, z1)`` per build item.

    Only the main model part is read (Fusion's raw export keeps every mesh
    there).  Returns an empty list when the file cannot be read.
    """
    try:
        with zipfile.ZipFile(str(base_path)) as archive:
            root = ET.fromstring(archive.read("3D/3dmodel.model"))
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError):
        logger.debug("Could not read body boxes from %r.", str(base_path), exc_info=True)
        return []
    meshes: Dict[str, List[Tuple[float, float, float]]] = {}
    children: Dict[str, List[Tuple[str, Tuple[float, ...]]]] = {}
    for element in root.iter():
        if not element.tag.endswith("}object"):
            continue
        object_id = element.get("id", "")
        points: List[Tuple[float, float, float]] = []
        for vertex in element.iter():
            if vertex.tag.endswith("}vertex"):
                try:
                    points.append(
                        (float(vertex.get("x")), float(vertex.get("y")), float(vertex.get("z")))
                    )
                except (TypeError, ValueError):
                    continue
            elif vertex.tag.endswith("}component"):
                children.setdefault(object_id, []).append(
                    (vertex.get("objectid", ""), _parse_transform(vertex.get("transform")))
                )
        if points:
            xs, ys, zs = zip(*points)
            meshes[object_id] = [
                (x, y, z) for x in (min(xs), max(xs)) for y in (min(ys), max(ys)) for z in (min(zs), max(zs))
            ]

    def corners(object_id: str, matrix: Tuple[float, ...], depth: int = 0) -> List[Tuple[float, float, float]]:
        found = [_transform_point(matrix, point) for point in meshes.get(object_id, [])]
        if depth < 8:
            for child_id, child_matrix in children.get(object_id, []):
                found.extend(
                    _transform_point(matrix, point)
                    for point in corners(child_id, child_matrix, depth + 1)
                )
        return found

    boxes: List[Tuple[float, float, float, float, float, float]] = []
    for item in root.iter():
        if item.tag.endswith("}item"):
            points = corners(item.get("objectid", ""), _parse_transform(item.get("transform")))
            if points:
                xs, ys, zs = zip(*points)
                boxes.append((min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)))
    return boxes


def _boxes_form_one_group(boxes: Sequence[Tuple[float, ...]]) -> bool:
    """Return whether every box touches the others through a chain of contacts.

    Known limit: this compares axis-aligned bounding boxes, not the meshes, so
    bodies whose boxes overlap without the bodies touching (a peg standing
    free inside a ring, an L-shape around a block) still count as touching and
    are assembled into one object.  They keep their exported positions, so the
    result prints correctly; only the automatic arrangement is skipped.
    """
    if len(boxes) < 2:
        return False
    group = {0}
    changed = True
    while changed:
        changed = False
        for index, box in enumerate(boxes):
            if index in group:
                continue
            if any(
                all(
                    box[axis] <= boxes[other][axis + 3] + TOUCH_TOLERANCE_MM
                    and boxes[other][axis] <= box[axis + 3] + TOUCH_TOLERANCE_MM
                    for axis in range(3)
                )
                for other in group
            ):
                group.add(index)
                changed = True
    return len(group) == len(boxes)


def _layout_flags(base_path: Path) -> List[str]:
    """Return the slicer CLI layout flags for the bodies in ``base_path``.

    Bodies that touch (a multi-colour part split into one body per colour) are
    merged into one object with one part per body (``--assemble``), so they
    keep their places; the per-part extruders survive.  Separate bodies are
    arranged, with several colours allowed on one plate (without that flag the
    slicer puts each colour on its own plate).
    """
    boxes = _base_body_boxes(base_path)
    if len(boxes) < 2:
        return []
    if _boxes_form_one_group(boxes):
        logger.info("All %d bodies touch; exporting them as one multi-colour object.", len(boxes))
        return ["--assemble"]
    return ["--allow-multicolor-oneplate"]


_CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_PRODUCTION_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"


def _join_touching_bodies(base_path: Path, work_dir: Path) -> Optional[Path]:
    """Join bodies that touch into one object with one part per body; a temporary copy.

    The slicer CLI's ``--assemble`` centres every body on its own middle, so
    bodies lose their places: four cubes in a row end up in one spot, and an
    inlay moves up inside its body.  Given one object whose parts are the
    bodies (each placed by its build-item transform), the slicers keep every
    part where it is.  The copy has no project_settings, and its
    ``model_settings.config`` gives every part the slot its body had.

    Returns None when the bodies do not all touch or the file cannot be joined
    safely (a body that is an assembly itself, or lives in another model
    part); the caller then uses the slicer's own layout flags.
    """
    if not _boxes_form_one_group(_base_body_boxes(base_path)):
        return None
    with zipfile.ZipFile(str(base_path), "r") as archive:
        model_text = archive.read("3D/3dmodel.model").decode("utf-8")
        settings_text = (
            archive.read(_MODEL_SETTINGS_PART).decode("utf-8")
            if _MODEL_SETTINGS_PART in archive.namelist() else "<config/>"
        )
    root = ET.fromstring(model_text)
    objects = {
        element.get("id"): element
        for element in root.iter("{%s}object" % _CORE_NS)
    }
    items = list(root.iter("{%s}item" % _CORE_NS))
    for item in items:
        body = objects.get(item.get("objectid"))
        if (
            body is None
            or item.get("{%s}path" % _PRODUCTION_NS)
            or body.find("{%s}mesh" % _CORE_NS) is None
        ):
            logger.info("Bodies touch but cannot be joined safely; the slicer assembles them.")
            return None
    extruders: Dict[str, str] = {}
    body_names: Dict[str, str] = {}
    # Print settings of single bodies (body_settings) move with them onto their parts.
    body_settings_by_id: Dict[str, Dict[str, str]] = {}
    for element in ET.fromstring(settings_text).findall("object"):
        for meta in element.findall("metadata"):
            if meta.get("key") == "extruder":
                extruders[str(element.get("id"))] = str(meta.get("value"))
            elif meta.get("key") == "name":
                body_names[str(element.get("id"))] = str(meta.get("value"))
            elif meta.get("key"):
                body_settings_by_id.setdefault(str(element.get("id")), {})[str(meta.get("key"))] = str(meta.get("value"))
    joined_id = str(max(int(key) for key in objects if str(key).isdigit()) + 1)
    components = "".join(
        '<component objectid="{}" transform="{}"/>'.format(
            item.get("objectid"), " ".join("{:g}".format(value) for value in _parse_transform(item.get("transform")))
        )
        for item in items
    )
    joined_object = '<object id="{}" name="Assembly" type="model"><components>{}</components></object>'.format(
        joined_id, components
    )
    model_text = model_text.replace("</resources>", joined_object + "</resources>", 1)
    model_text = re.sub(
        r"<build\b[^>]*>.*?</build>|<build\b[^>]*/>",
        '<build><item objectid="{}"/></build>'.format(joined_id),
        model_text,
        count=1,
        flags=re.S,
    )
    def metadata(values: Mapping[str, str]) -> str:
        return "".join('<metadata key="{}" value="{}"/>'.format(_xml_escape(key), _xml_escape(value))
                       for key, value in sorted(values.items()))

    parts = "".join(
        '<part id="{0}" subtype="normal_part"><metadata key="name" value="{1}"/>'
        '<metadata key="extruder" value="{2}"/>{3}</part>'.format(
            item.get("objectid"),
            # The slot record's name first: the slot check matches bodies by it.
            _xml_escape(body_names.get(str(item.get("objectid"))) or str(objects[item.get("objectid")].get("name") or "Body")),
            extruders.get(str(item.get("objectid")), "1"),
            metadata(body_settings_by_id.get(str(item.get("objectid")), {})),
        )
        for item in items
    )
    settings = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config><object id="{0}">'
        '<metadata key="name" value="Assembly"/><metadata key="extruder" value="{1}"/>{2}</object></config>\n'
    ).format(joined_id, extruders.get(str(items[0].get("objectid")), "1"), parts)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix="filamatch_base_", suffix=".3mf", dir=str(work_dir))
    os.close(file_descriptor)
    joined = Path(temporary_name)
    try:
        with zipfile.ZipFile(str(base_path), "r") as source, zipfile.ZipFile(str(joined), "w") as target:
            _copy_zip_entries(
                source,
                target,
                {
                    _PROJECT_SETTINGS_PART: None,
                    "3D/3dmodel.model": model_text.encode("utf-8"),
                    _MODEL_SETTINGS_PART: settings.encode("utf-8"),
                },
            )
    except Exception:
        _remove_file_quietly(joined)
        raise
    logger.info("Joined %d touching bodies into one object with one part each.", len(items))
    return joined


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _prepare_cli_base(base_path: Path, work_dir: Path) -> Tuple[Path, bool]:
    """Return a base 3MF without project_settings, copying it when necessary."""
    with zipfile.ZipFile(str(base_path), "r") as archive:
        has_project_settings = _PROJECT_SETTINGS_PART in archive.namelist()
    if not has_project_settings:
        return base_path, False
    logger.info(
        "Base 3MF contains project_settings.config; using a temporary copy without it."
    )
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix="filamatch_base_", suffix=".3mf", dir=str(work_dir)
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        with zipfile.ZipFile(str(base_path), "r") as source, zipfile.ZipFile(
            str(temporary_path), "w"
        ) as target:
            _copy_zip_entries(source, target, {_PROJECT_SETTINGS_PART: None})
    except Exception:
        _remove_file_quietly(temporary_path)
        raise
    return temporary_path, True


def _format_density(value: Any) -> str:
    """Format a density as a slicer-compatible string such as ``"1.24"``."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return "1.24"
    density = float(value)
    if not math.isfinite(density) or density <= 0.0:
        return "1.24"
    text = "{:.4f}".format(density).rstrip("0").rstrip(".")
    return text or "1.24"


def _normalize_color_hex(value: Any) -> str:
    """Validate and normalise a ``#RRGGBB`` colour string."""
    if not isinstance(value, str):
        raise SlicerProfilesError(
            "Material colour must be a '#RRGGBB' string, got {!r}".format(value)
        )
    text = value.strip()
    if re.match(r"^#[0-9A-Fa-f]{6}$", text) is None:
        raise SlicerProfilesError(
            "Material colour must be a '#RRGGBB' string, got {!r}".format(value)
        )
    return text.upper()


def _brand_from_display_name(display_name: str) -> str:
    """Derive a probable brand from a display name (first 1-2 words)."""
    text = str(display_name or "").strip()
    if not text:
        return ""
    words = text.replace(" - ", " ").split()
    if not words:
        return ""
    return " ".join(words[:2]) if len(words) > 1 else words[0]


def _is_type_variant(preset_type: str, material_type: str) -> bool:
    """True when ``preset_type`` is ``material_type`` or a variant of it (ABS-GF of ABS)."""
    preset, material = preset_type.strip().upper(), material_type.strip().upper()
    return bool(material) and (preset == material or preset.startswith((material + "-", material + " ")))


def _preset_filament_types(paths: Sequence[str]) -> List[str]:
    """The ``filament_type`` of every filament preset file ("" when it has none)."""
    types: List[str] = []
    for path in paths:
        value = (_read_json_file(Path(path)) or {}).get("filament_type")
        if isinstance(value, list):
            value = value[0] if value else ""
        types.append(str(value or "").strip())
    return types


def _postprocess_project_settings(
    output_path: Path,
    materials: Sequence[Any],
    preset_identities: Optional[Sequence[Tuple[str, str, str]]] = None,
    extra_entries: Optional[Mapping[str, Optional[bytes]]] = None,
    preset_types: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Rewrite the prepared 3MF's project_settings with the detected materials.

    ``extra_entries`` (name -> bytes, or ``None`` to drop) are applied in the
    same archive rewrite, e.g. the slot manifest.  ``preset_types`` are the
    filament presets' own types: a preset's type that is the material's type
    or a variant of it ("ABS-GF" for "ABS") is kept, because the printer
    matches AMS slots by the exact type.

    The slicer generates the project with the requested number of filaments; this
    function makes the colour/type/density arrays exactly describe the detected
    materials (preventing Orca's "filament_colour size" mismatch errors) while
    preserving all other project settings and archive entries.

    Returns:
        The updated project settings dictionary.

    Raises:
        SlicerProfilesError: If the slicer did not write project_settings, the
            JSON is unusable, or ``printer_settings_id`` is empty.
    """
    with zipfile.ZipFile(str(output_path), "r") as archive:
        if _PROJECT_SETTINGS_PART not in archive.namelist():
            raise SlicerProfilesError(
                "The slicer did not write {!r} into {!r}; verify the printer/process "
                "target and slicer CLI output.".format(
                    _PROJECT_SETTINGS_PART, str(output_path)
                )
            )
        raw_settings = archive.read(_PROJECT_SETTINGS_PART)
    try:
        settings = json.loads(raw_settings.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SlicerProfilesError(
            "The slicer wrote invalid JSON to {!r}: {}".format(
                _PROJECT_SETTINGS_PART, exc
            )
        ) from exc
    if not isinstance(settings, dict):
        raise SlicerProfilesError(
            "The slicer's project_settings.config is not a JSON object"
        )
    printer_settings_id = settings.get("printer_settings_id")
    if not isinstance(printer_settings_id, str) or not printer_settings_id.strip():
        raise SlicerProfilesError(
            "The prepared project has an empty 'printer_settings_id'; the selected "
            "printer preset did not apply correctly (check the machine preset)."
        )

    colors = [
        _normalize_color_hex(_material_field(material, "color_hex", "#FFFFFF"))
        for material in materials
    ]
    types = [
        str(_material_field(material, "filament_type", "PLA") or "PLA").strip() or "PLA"
        for material in materials
    ]
    for index, preset_type in enumerate(preset_types or ()):
        if index < len(types) and preset_type and _is_type_variant(preset_type, types[index]):
            types[index] = preset_type
    densities = [
        _format_density(_material_field(material, "density_g_cm3"))
        for material in materials
    ]
    settings["filament_colour"] = colors
    settings["filament_type"] = types
    settings["filament_density"] = densities

    # OrcaSlicer/Bambu Studio identify a filament by its *installed preset
    # name* and setting id.  Writing our friendly "Brand - Material - Color"
    # display name here makes Orca show a blocking "Customized Preset" warning
    # and can prevent the live AMS match.  Keep the real preset identity; the
    # friendly name and spool/slot association live in the palette and in
    # Metadata/filamatch.json.
    original_names = settings.get("filament_settings_id")
    original_ids = settings.get("filament_ids")
    original_vendor = settings.get("filament_vendor")
    identities = list(preset_identities or [])
    preset_names: List[str] = []
    preset_ids: List[str] = []
    preset_vendors: List[str] = []
    for index, material in enumerate(materials):
        identity = identities[index] if index < len(identities) else ("", "", "")
        name = str(identity[0] or "").strip()
        identifier = str(identity[1] or "").strip()
        vendor = str(identity[2] or "").strip()
        if isinstance(original_names, list) and index < len(original_names):
            name = name or str(original_names[index] or "").strip()
        if isinstance(original_ids, list) and index < len(original_ids):
            identifier = identifier or str(original_ids[index] or "").strip()
        if isinstance(original_vendor, list) and index < len(original_vendor):
            vendor = vendor or str(original_vendor[index] or "").strip()
        preset_names.append(name)
        preset_ids.append(identifier)
        preset_vendors.append(vendor)
    # Only replace when every slot has a real preset identity; otherwise keep
    # whatever the slicer produced rather than fabricating a name.
    if preset_names and all(name.strip() for name in preset_names):
        settings["filament_settings_id"] = preset_names
    if preset_ids and any(identifier.strip() for identifier in preset_ids):
        settings["filament_ids"] = preset_ids
    if preset_vendors and all(vendor.strip() for vendor in preset_vendors):
        settings["filament_vendor"] = preset_vendors

    # Per-filament map arrays must have one entry per filament. The slicer CLI
    # can leave them at the base project's single entry when it is fed multiple
    # --load-filaments files; Orca then shows only the first filament slot.
    filament_count = len(materials)
    for key, default in (
        ("filament_map", "1"),
        ("filament_map_2", "1"),
        ("filament_nozzle_map", "0"),
        ("filament_volume_map", "0"),
    ):
        current = settings.get(key)
        if not isinstance(current, list):
            continue
        if len(current) == filament_count:
            continue
        if not current:
            current = [default]
        settings[key] = [
            str(current[index]) if index < len(current) else str(current[-1])
            for index in range(filament_count)
        ]
    _fit_wipe_tower(settings)
    _fit_bed_type(settings)
    _fit_nozzle_volume_types(settings)
    payload = (json.dumps(settings, indent=2) + "\n").encode("utf-8")
    replacements: Dict[str, Optional[bytes]] = dict(extra_entries or {})
    replacements[_PROJECT_SETTINGS_PART] = payload
    rewrite_zip_entries(output_path, replacements, required=(_PROJECT_SETTINGS_PART,))
    logger.info(
        "Post-processed project_settings for %d filament(s): %s", len(colors), colors
    )
    return settings


def _bed_supports_all(settings: Mapping[str, Any], bed_type: str) -> bool:
    """Return whether every filament has a non-zero temperature for ``bed_type``."""
    key = BED_TYPE_TEMPERATURE_KEYS.get(bed_type)
    temperatures = settings.get(key) if key else None
    if not isinstance(temperatures, list) or not temperatures:
        return False
    try:
        return all(float(value) > 0 for value in temperatures)
    except (TypeError, ValueError):
        return False


def _fit_bed_type(settings: Dict[str, Any]) -> None:
    """Switch to a build plate every filament can print on.

    The printer preset's default plate (Bambu: Cool Plate) has a 0 degree
    temperature for ABS/ASA/PA filaments, and Orca then refuses the plate.  The
    plate is changed only when it does not suit every filament.
    """
    current = str(settings.get("curr_bed_type") or "").strip()
    if not current or current not in BED_TYPE_TEMPERATURE_KEYS:
        return
    if _bed_supports_all(settings, current):
        return
    for bed_type in BED_TYPE_PREFERENCE:
        if _bed_supports_all(settings, bed_type):
            logger.info(
                "Build plate %r does not suit every filament; using %r.", current, bed_type
            )
            settings["curr_bed_type"] = bed_type
            return


# OrcaSlicer 2.5 words these Bambu Studio settings its own way; Bambu Studio
# 2.9 translates them on load and shows "Some values have been replaced".
# The pairs are what OrcaSlicer writes for Bambu Studio's values and what
# Bambu Studio writes back.
_BAMBU_STUDIO_WORDS = {
    "enable_arc_fitting": {"enabled": "1", "disabled": "0"},
    "ensure_vertical_shell_thickness": {"ensure_all": "enabled"},
    "ironing_pattern": {"rectilinear": "zig-zag", "concentric": "concentric"},
    "support_ironing_pattern": {"rectilinear": "zig-zag", "concentric": "concentric"},
}


def use_bambu_studio_words(project_3mf: Path, process_path: Any = None) -> Dict[str, str]:
    """Write Bambu Studio's words for the settings OrcaSlicer words its own way.

    A project prepared by OrcaSlicer and opened in Bambu Studio then loads
    without the "Some values have been replaced" notice.  The value in the
    Bambu Studio process preset (``process_path``, when it is one of Bambu
    Studio's system presets) wins, because OrcaSlicer turns every Bambu
    "ensure vertical shell thickness" value into "ensure_all"; otherwise the
    OrcaSlicer word is translated.  Unknown words are left for Bambu Studio.

    Returns:
        The changed settings (key -> new value).
    """
    preset: Dict[str, Any] = {}
    system = _bambu_system_dir()
    if process_path and system is not None:
        try:
            Path(str(process_path)).resolve().relative_to(system.resolve())
            preset = _flatten_local_preset(Path(str(process_path)))
        except (OSError, ValueError, SlicerProfilesError):
            preset = {}
    with zipfile.ZipFile(str(project_3mf), "r") as archive:
        if _PROJECT_SETTINGS_PART not in archive.namelist():
            return {}
        settings = json.loads(archive.read(_PROJECT_SETTINGS_PART).decode("utf-8"))
    changed: Dict[str, str] = {}
    for key, words in _BAMBU_STUDIO_WORDS.items():
        value = settings.get(key)
        if not isinstance(value, str) or value in words.values():
            continue
        own = preset.get(key)
        word = own.strip() if isinstance(own, str) and own.strip() else words.get(value, value)
        if word != value:
            changed[key] = word
    if changed:
        settings.update(changed)
        rewrite_zip_entries(
            project_3mf,
            {_PROJECT_SETTINGS_PART: (json.dumps(settings, indent=2) + "\n").encode("utf-8")},
        )
        logger.info("Wrote Bambu Studio's words for %s.", changed)
    return changed


def _cli_error_line(output: str) -> str:
    """The slicer's own reason for a failed slice, from its command-line output.

    OrcaSlicer prints the reason on the line before "run found error", for
    example "Selected nozzle temperatures are incompatible. ..." (PLA and
    PETG on one MMU3 nozzle).  Log lines ("[2026-...] [error] ...") are
    skipped.  Empty when there is no such line.
    """
    lines = [line.strip() for line in str(output or "").splitlines()]
    for index, line in enumerate(lines):
        if line.lower().startswith("run found error"):
            # The reason goes to stderr, which can come after stdout's "run found error".
            reasons = [item for item in reversed(lines[:index]) if item and not item.startswith("[")]
            reasons += [item for item in lines[index + 1:] if item and not item.startswith("[")]
            return reasons[0][:300] if reasons else ""
    return ""


def estimate_print(project_3mf: Path, slicer_exe: Any) -> Dict[str, Any]:
    """Slice a copy of a prepared project and return its print time and filament.

    The copy is sliced by ``slicer_exe`` (the slicer that prepared it) in a
    temporary folder; the project itself is not changed.  On a printer with
    more than one nozzle the copy gets ``extruder_nozzle_stats`` (see
    :func:`_nozzle_stats`), which the slicer otherwise reads from the printer:
    without it Bambu Studio's command line stops with "No valid nozzle found".

    Returns:
        ``{"minutes": int, "grams": float, "plates": int, "cost": float or
        None, "filament_grams": [float, ...]}`` for all plates together;
        ``filament_grams`` has one entry per filament slot (0 when unused),
        ``cost`` is the filament cost at the presets' prices per kg.

    Raises:
        SlicerProfilesError: If the slicer fails or reports no estimate.
    """
    slicer_exe = resolve_slicer_executable(slicer_exe)
    if slicer_kind(slicer_exe) == "prusa":
        from . import prusa_profiles

        try:
            return prusa_profiles.estimate_print(Path(project_3mf), slicer_exe)
        except prusa_profiles.PrusaProfilesError as exc:
            raise SlicerProfilesError(str(exc)) from exc
    _cache_root().mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="estimate_", dir=str(_cache_root())))
    try:
        copy = work / "project.3mf"
        shutil.copy2(str(project_3mf), str(copy))
        with zipfile.ZipFile(str(copy), "r") as archive:
            settings = json.loads(archive.read(_PROJECT_SETTINGS_PART).decode("utf-8"))
        stats = _nozzle_stats(settings)
        if stats and not settings.get("extruder_nozzle_stats"):
            settings["extruder_nozzle_stats"] = stats
            rewrite_zip_entries(copy, {_PROJECT_SETTINGS_PART: json.dumps(settings).encode("utf-8")})
        command = [str(slicer_exe), "--slice", "0", "--export-3mf", "sliced.3mf", "--outputdir", str(work), str(copy)]
        for attempt in range(ESTIMATE_CRASH_RETRIES + 1):
            # In the temporary folder: OrcaSlicer writes "00000.log" where it runs.
            result = _run_command(command, CLI_TIMEOUT_SECONDS, cwd=str(work))
            code = _safe_get(result, "returncode", "a slicer CLI result")
            if not (isinstance(code, int) and code < 0) or attempt == ESTIMATE_CRASH_RETRIES:
                break
            # OrcaSlicer 2.5 crashes now and then on some printers (Prusa CORE One
            # INDX 8T: about half the runs); the next run usually works.
            logger.warning("The slicer crashed (signal %s) while estimating; trying again.", -code)
        sliced = work / "sliced.3mf"
        info = ""
        if sliced.is_file():
            with zipfile.ZipFile(str(sliced), "r") as archive:
                if "Metadata/slice_info.config" in archive.namelist():
                    info = archive.read("Metadata/slice_info.config").decode("utf-8", "replace")
        # Every plate counts: Bambu Studio puts bodies on plates of their own when
        # one filament's bed is too hot for another (ABS and PETG).
        seconds = [float(value) for value in re.findall(r'key="prediction" value="([\d.]+)"', info)]
        grams = [float(value) for value in re.findall(r'key="weight" value="([\d.]+)"', info)]
        if _safe_get(result, "returncode", "a slicer CLI result") not in (0, None) or not seconds or not grams:
            report = (_read_json_file(work / "result.json") if (work / "result.json").is_file() else None) or {}
            raise SlicerProfilesError(
                "The slicer gave no print estimate: {}".format(
                    report.get("error_string")
                    or _cli_error_line(_result_text(result))
                    or (
                        "the slicer crashed (signal {})".format(-code)
                        if isinstance(code, int) and code < 0
                        else "exit code {}".format(code)
                    )
                )
            )
        used: Dict[int, float] = {}
        for number, value in re.findall(r'<filament id="(\d+)"[^>]*?used_g="([\d.]+)"', info):
            used[int(number)] = used.get(int(number), 0.0) + float(value)
        return {
            "minutes": int(round(sum(seconds) / 60.0)),
            "grams": round(sum(grams), 1),
            "plates": len(seconds),
            "cost": print_cost(used, settings.get("filament_cost")),
            # One entry per project filament (slot 1, 2, ...); an unused one is 0.
            "filament_grams": [round(used.get(number, 0.0), 1) for number in range(1, max(used, default=0) + 1)],
        }
    finally:
        shutil.rmtree(str(work), ignore_errors=True)


def _prepare_prusa_project(
    base_path: Path, output_path: Path, target: ProfileTarget, slicer_exe: str, materials: Sequence[Any]
) -> PreparedProject:
    """PrusaSlicer prepares in its own way (see prusa_profiles.prepare_project)."""
    from . import prusa_profiles

    try:
        result = prusa_profiles.prepare_project(
            base_path, output_path, target.vendor, target.printer_name, target.process_name, slicer_exe, materials
        )
    except prusa_profiles.PrusaProfilesError as exc:
        raise SlicerProfilesError(str(exc)) from exc
    return PreparedProject(
        output_path=output_path,
        printer_name=target.printer_name,
        process_name=target.process_name,
        filament_names=tuple(result["filament_names"]),
        slicer_version=str(result["slicer_version"]),
        log_tail="",
        built_presets=tuple(result["built_presets"]),
    )


def printer_nozzle(slicer_exe: Any, vendor_id: str, printer_name: str) -> Optional[float]:
    """The first nozzle's size of a printer preset in mm, or None when unknown.

    PrusaSlicer's and Bambu Studio's own presets say it; otherwise the name
    does ("... 0.6 nozzle").
    """
    try:
        if slicer_kind(slicer_exe) == "prusa":
            from . import prusa_profiles

            sizes = prusa_profiles.string_list(prusa_profiles.bundle_for(
                resolve_slicer_executable(slicer_exe), vendor_id).flat("printer", printer_name).get("nozzle_diameter", ""))
            if sizes:
                return float(sizes[0])
        elif vendor_id == "bambu_local":
            root = _bambu_system_dir()
            for preset in _scan_local_machines(root) if root is not None else []:
                if preset["name"] == printer_name:
                    sizes = _flatten_local_preset(Path(preset["path"])).get("nozzle_diameter")
                    if isinstance(sizes, list) and sizes:
                        return float(sizes[0])
    except Exception:
        logger.debug("Could not read the nozzle of %r", printer_name, exc_info=True)
    found = re.search(r"(\d+(?:\.\d+)?)\s*(?:mm\s*)?nozzle", str(printer_name or ""), re.I)
    return float(found.group(1)) if found else None


def infill_patterns(slicer_exe: Any) -> List[str]:
    """The infill types a slicer accepts, by its own names, asked once per slicer version.

    PrusaSlicer lists them in its help.  Bambu Studio and OrcaSlicer do not:
    when they load a 3MF they keep an object's infill type they know and put
    their default in place of one they do not, so a 3MF with one small cube
    per known name (body_settings.INFILL_PATTERNS) is loaded and saved once
    (well under a second); the names that come back unchanged are the ones it
    accepts.  Cached per executable and its modification time.

    Raises:
        SlicerProfilesError: If the slicer gives no answer.
    """
    exe = resolve_slicer_executable(slicer_exe)
    if slicer_kind(exe) == "prusa":
        from . import prusa_profiles

        try:
            return prusa_profiles.infill_patterns(exe)
        except prusa_profiles.PrusaProfilesError as exc:
            raise SlicerProfilesError(str(exc)) from exc
    try:
        stamp = int(os.stat(exe).st_mtime)
    except OSError:
        stamp = 0
    cache = _cache_root() / "infill_patterns_{}_{}.json".format(hashlib.sha1(exe.encode("utf-8")).hexdigest()[:12], stamp)
    cached = (_read_json_file(cache) or {}).get("patterns") if cache.is_file() else None
    if isinstance(cached, list) and cached:
        return [str(name) for name in cached]
    names = list(body_settings.INFILL_PATTERNS)
    _cache_root().mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="infill_", dir=str(_cache_root())))
    try:
        probe = work / "patterns.3mf"
        _write_pattern_probe(probe, names)
        # In the temporary folder: OrcaSlicer writes "00000.log" where it runs.
        _run_command([exe, "--export-3mf", "out.3mf", "--outputdir", str(work), str(probe)],
                     CLI_TIMEOUT_SECONDS, cwd=str(work))
        found: Dict[str, str] = {}
        if (work / "out.3mf").is_file():
            with zipfile.ZipFile(str(work / "out.3mf"), "r") as archive:
                if _MODEL_SETTINGS_PART in archive.namelist():
                    for element in ET.fromstring(archive.read(_MODEL_SETTINGS_PART)).findall("object"):
                        # The slicer numbers the objects anew; their names ("p7") stay.
                        values = {meta.get("key"): str(meta.get("value")) for meta in element.findall("metadata")}
                        if values.get("name", "").startswith("p") and "sparse_infill_pattern" in values:
                            found[values["name"][1:]] = values["sparse_infill_pattern"]
        accepted = [name for number, name in enumerate(names, start=1) if found.get(str(number)) == name]
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
    if not accepted:
        raise SlicerProfilesError("{} did not say which infill types it has".format(slicer_name(exe)))
    cache.write_text(json.dumps({"patterns": accepted}), encoding="utf-8")
    return accepted


def _write_pattern_probe(path: Path, names: Sequence[str]) -> None:
    """A 3MF with one 5 mm cube per infill type name, each object asking for its name."""
    corners = [(x, y, z) for z in (0, 5) for y in (0, 5) for x in (0, 5)]
    faces = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
             (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    objects, items, settings = [], [], []
    for number, name in enumerate(names, start=1):
        dx, dy = (number - 1) % 6 * 12, (number - 1) // 6 * 12
        objects.append('<object id="{}" name="p{}" type="model"><mesh><vertices>{}</vertices><triangles>{}</triangles>'
                       '</mesh></object>'.format(
                           number, number,
                           "".join('<vertex x="{}" y="{}" z="{}"/>'.format(x + dx, y + dy, z) for x, y, z in corners),
                           "".join('<triangle v1="{}" v2="{}" v3="{}"/>'.format(*face) for face in faces)))
        items.append('<item objectid="{}"/>'.format(number))
        settings.append('<object id="{0}"><metadata key="name" value="p{0}"/><metadata key="extruder" value="1"/>'
                        '<metadata key="sparse_infill_pattern" value="{1}"/></object>'.format(number, _xml_escape(name)))
    with zipfile.ZipFile(str(path), "w") as archive:
        archive.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-'
            '3dmodel+xml"/></Types>'))
        archive.writestr("_rels/.rels", (
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.'
            'com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'))
        archive.writestr("3D/3dmodel.model", (
            '<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="{}"><resources>{}</resources>'
            '<build>{}</build></model>').format(_CORE_NS, "".join(objects), "".join(items)))
        archive.writestr(_MODEL_SETTINGS_PART, "<config>{}</config>".format("".join(settings)))


def print_cost(grams_by_filament: Mapping[int, float], prices: Any) -> Optional[float]:
    """The filament cost of a print: each filament's grams at its preset's price per kg.

    ``grams_by_filament`` by 1-based filament number; ``prices`` the
    project's ``filament_cost`` (one price per kg per filament, as the slicer
    presets give it).  None when a used filament has no price.
    """
    if not isinstance(prices, list) or not grams_by_filament:
        return None
    total = 0.0
    for number, grams in grams_by_filament.items():
        if grams <= 0:
            continue
        try:
            total += grams * float(str(prices[number - 1]).strip()) / 1000.0
        except (IndexError, ValueError):
            return None
    return round(total, 2)


def apply_printer_nozzles(project_3mf: Path, nozzles: Sequence[Any]) -> List[str]:
    """Give a prepared project the flow type of each installed nozzle.

    Like Bambu Studio's printer sync: the filament presets then use their
    High Flow values and the print estimate the High Flow speeds.
    ``nozzles`` are the printer's (``id``, ``diameter``, ``flow``, see
    printer_model.NozzleInfo); slicer extruder ``i`` is the printer's
    extruder ``physical_extruder_map[i]`` (H2D: slicer extruder 1 is the left
    nozzle, printer id 1).

    Returns:
        The flow types, one per extruder, when the printer has a nozzle of the
        project's size with a known flow for every extruder (the project is
        rewritten only when they differ); else [] and nothing is changed.
    """
    with zipfile.ZipFile(str(project_3mf), "r") as archive:
        if _PROJECT_SETTINGS_PART not in archive.namelist():
            return []
        settings = json.loads(archive.read(_PROJECT_SETTINGS_PART).decode("utf-8"))
    diameters = settings.get("nozzle_diameter")
    if not isinstance(diameters, list) or not diameters or len(nozzles) != len(diameters):
        return []
    physical = settings.get("physical_extruder_map")
    if not isinstance(physical, list) or len(physical) != len(diameters):
        physical = list(range(len(diameters)))
    by_id = {int(getattr(nozzle, "id", -1)): nozzle for nozzle in nozzles}
    flows: List[str] = []
    for index, diameter in enumerate(diameters):
        try:
            nozzle = by_id.get(int(str(physical[index]).strip()))
            size = float(str(diameter))
        except ValueError:
            return []
        flow = str(getattr(nozzle, "flow", "") or "")
        if nozzle is None or not flow or abs(float(getattr(nozzle, "diameter", 0.0) or 0.0) - size) > 0.01:
            return []
        flows.append(flow)
    current = settings.get("nozzle_volume_type")
    if current != flows:
        settings["nozzle_volume_type"] = flows
        if settings.get("extruder_nozzle_stats"):
            settings["extruder_nozzle_stats"] = _nozzle_stats(settings)
        rewrite_zip_entries(
            project_3mf, {_PROJECT_SETTINGS_PART: (json.dumps(settings, indent=2) + "\n").encode("utf-8")}
        )
        logger.info("Nozzle flow types %s -> %s (from the printer).", current, flows)
    return flows


def apply_nozzle_map(project_3mf: Path, nozzles: Sequence[Optional[str]]) -> bool:
    """Pin each filament to the nozzle its printer slot feeds.

    On a printer with two nozzles (Bambu Lab H2D) each AMS feeds one nozzle.
    Left alone, the slicer picks the nozzles itself ("Auto For Flush") and
    can put a filament on the nozzle whose AMS does not hold it.  ``nozzles``
    has one entry per filament in slot order: :data:`NOZZLE_LEFT`,
    :data:`NOZZLE_RIGHT` or ``None`` when the printer does not say.

    Returns:
        True when the map was written (``filament_map_mode`` "Manual" in the
        project and in every plate); False for a one-nozzle printer, a project
        without settings, a filament whose nozzle is unknown, or a filament
        whose type that nozzle cannot print (the slicer then decides).

    Raises:
        SlicerProfilesError: If the filament count does not match or the
            written map does not read back.
    """
    wanted = [str(nozzle or "").strip().lower() for nozzle in nozzles]
    with zipfile.ZipFile(str(project_3mf), "r") as archive:
        names = archive.namelist()
        if _PROJECT_SETTINGS_PART not in names or _MODEL_SETTINGS_PART not in names:
            return False
        settings = json.loads(archive.read(_PROJECT_SETTINGS_PART).decode("utf-8"))
        model_root = ET.fromstring(archive.read(_MODEL_SETTINGS_PART))
    diameters = settings.get("nozzle_diameter")
    if not isinstance(diameters, list) or len(diameters) != 2:
        return False
    if not wanted or any(nozzle not in _FILAMENT_MAP_VALUES for nozzle in wanted):
        logger.info("A filament's nozzle is unknown (%s); the slicer picks the nozzles.", wanted)
        return False
    colours = settings.get("filament_colour")
    if not isinstance(colours, list) or len(colours) != len(wanted):
        raise SlicerProfilesError(
            "The project has {} filament(s) but {} nozzle(s) were given".format(
                len(colours) if isinstance(colours, list) else 0, len(wanted)
            )
        )
    sides = _filament_map_values(settings)
    values = [sides[nozzle] for nozzle in wanted]
    refused = _unprintable_on_nozzle(settings, values)
    if refused:
        logger.warning("The printer cannot print %s; the slicer picks the nozzles.", ", ".join(refused))
        return False
    settings["filament_map"] = values
    settings["filament_map_mode"] = "Manual"
    for plate in model_root.findall("plate"):
        for key, value in (("filament_map_mode", "Manual"), ("filament_maps", " ".join(values))):
            item = next((meta for meta in plate.findall("metadata") if meta.get("key") == key), None)
            if item is None:
                item = ET.SubElement(plate, "metadata", {"key": key})
            item.set("value", value)
    rewrite_zip_entries(
        project_3mf,
        {
            _PROJECT_SETTINGS_PART: (json.dumps(settings, indent=2) + "\n").encode("utf-8"),
            _MODEL_SETTINGS_PART: ET.tostring(model_root, encoding="utf-8", xml_declaration=True),
        },
    )
    with zipfile.ZipFile(str(project_3mf), "r") as archive:
        written = json.loads(archive.read(_PROJECT_SETTINGS_PART).decode("utf-8"))
        plates = ET.fromstring(archive.read(_MODEL_SETTINGS_PART)).findall("plate")
    plate_maps = {
        meta.get("value")
        for plate in plates
        for meta in plate.findall("metadata")
        if meta.get("key") == "filament_maps"
    }
    if written.get("filament_map") != values or plate_maps - {" ".join(values)}:
        raise SlicerProfilesError("The nozzle map did not read back from {}".format(project_3mf))
    logger.info("Pinned the filaments to the nozzles %s in %s.", wanted, project_3mf)
    return True


def _model_slot_assignments(archive: zipfile.ZipFile) -> List[Tuple[str, int]]:
    """Return ``(name, slot)`` for every named object and part in model_settings.

    A part without its own ``extruder`` prints with its object's slot; an
    object without one prints with slot 1 (the slicers' default).
    """
    if _MODEL_SETTINGS_PART not in archive.namelist():
        return []
    try:
        root = ET.fromstring(archive.read(_MODEL_SETTINGS_PART))
    except ET.ParseError as exc:
        raise SlicerProfilesError(
            "The slicer wrote an unreadable {!r}: {}".format(_MODEL_SETTINGS_PART, exc)
        ) from exc

    def metadata(element: Any) -> Dict[str, str]:
        return {
            str(item.get("key")): str(item.get("value") or "")
            for item in element.findall("metadata")
            if item.get("key")
        }

    def slot_of(values: Mapping[str, str], fallback: int) -> int:
        try:
            slot = int(values.get("extruder", "") or 0)
        except ValueError:
            return fallback
        return slot if slot > 0 else fallback

    assignments: List[Tuple[str, int]] = []
    for element in root.findall("object"):
        values = metadata(element)
        object_slot = slot_of(values, 1)
        if values.get("name"):
            assignments.append((values["name"], object_slot))
        for part in element.findall("part"):
            part_values = metadata(part)
            if part_values.get("name"):
                assignments.append((part_values["name"], slot_of(part_values, object_slot)))
    return assignments


def check_slot_assignments(project_3mf: Path, materials: Sequence[Any]) -> int:
    """Check that every body prints with the filament Fusion gave it.

    Every slicer output is checked the same way: each body named in a
    material's ``body_names`` must use that material's ``slot`` (default: its
    1-based position) in ``Metadata/model_settings.config``, and when the
    project has ``filament_colour`` that slot must show the material's colour.

    Returns:
        The number of bodies whose slot was checked.

    Raises:
        SlicerProfilesError: If a body or colour does not match, or no named
            body was found in the 3MF at all.
    """
    with zipfile.ZipFile(str(project_3mf), "r") as archive:
        assignments = _model_slot_assignments(archive)
        colours: Optional[List[Any]] = None
        if _PROJECT_SETTINGS_PART in archive.namelist():
            try:
                settings = json.loads(archive.read(_PROJECT_SETTINGS_PART).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                settings = {}
            value = settings.get("filament_colour") if isinstance(settings, dict) else None
            colours = value if isinstance(value, list) else None

    slots_by_name: Dict[str, set] = {}
    for name, slot in assignments:
        slots_by_name.setdefault(name, set()).add(slot)
    problems: List[str] = []
    checked = 0
    expected_bodies = 0
    for index, material in enumerate(materials):
        label = _material_display_name(material, index)
        try:
            slot = int(_material_field(material, "slot", index + 1))
        except (TypeError, ValueError):
            slot = index + 1
        if colours is not None:
            wanted = _normalize_color_hex(_material_field(material, "color_hex", "#FFFFFF"))
            actual = colours[slot - 1] if 0 < slot <= len(colours) else None
            if not isinstance(actual, str) or actual.strip().upper() != wanted:
                problems.append(
                    "slot {} is {} in the slicer, but {} is {}".format(
                        slot, actual or "missing", label, wanted
                    )
                )
        for body_name in _material_field(material, "body_names", ()) or ():
            expected_bodies += 1
            found = slots_by_name.get(str(body_name))
            if not found:
                logger.info("Body %r is not in %s; its slot was not checked.", body_name, project_3mf)
                continue
            checked += 1
            if found != {slot}:
                problems.append(
                    "{!r} prints with slot {}, but Fusion gives it {} (slot {})".format(
                        body_name, "/".join(str(item) for item in sorted(found)), label, slot
                    )
                )
    if expected_bodies and not checked:
        problems.append("none of the Fusion bodies were found in the 3MF")
    if problems:
        raise SlicerProfilesError(
            "Filament assignments do not match Fusion: {}.".format("; ".join(problems))
        )
    logger.info("Slot check passed for %d bod(ies) in %s.", checked, project_3mf)
    return checked


def _read_log_tail(log_path: Path, lines: int = LOG_TAIL_LINES) -> str:
    """Return the last ``lines`` lines of a slicer log file, or ``""``."""
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tail = text.splitlines()[-lines:]
    return "\n".join(tail)


def _command_text(command: Sequence[str]) -> str:
    """Render a CLI command for error messages."""
    return " ".join(str(part) for part in command)


def _cli_error_message(
    reason: str, command: Sequence[str], result: Any, log_path: Path
) -> str:
    """Build an actionable slicer CLI error including the log tail."""
    returncode = _safe_get(result, "returncode", "a command result")
    output = _result_text(result).strip()
    parts = [
        "Slicer CLI failed: {} (exit code {}).".format(reason, returncode),
        "Command: {}".format(_command_text(command)),
    ]
    if output:
        parts.append(
            "Slicer output:\n" + "\n".join(output.splitlines()[-LOG_TAIL_LINES:])
        )
    tail = _read_log_tail(log_path)
    if tail:
        parts.append("Slicer log tail ({}):\n{}".format(str(log_path), tail))
    else:
        parts.append("No slicer log was written at {!r}.".format(str(log_path)))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Project preparation
# ---------------------------------------------------------------------------


def _find_name_sub_path(
    name_map: Dict[str, str], preset_name: str, preset_kind: str, vendor: str
) -> str:
    """Resolve a preset name to its repository sub-path, raising when missing."""
    sub_path = name_map.get(preset_name)
    if sub_path is None:
        sample = ", ".join(sorted(name_map)[:10])
        raise SlicerProfilesError(
            "Remote {} preset {!r} was not found for vendor {!r}; refresh the profile "
            "cache and pick the target again. Known presets (sample): {}".format(
                preset_kind, preset_name, vendor, sample or "<none>"
            )
        )
    return sub_path


def _prepare_remote_profiles(
    target: ProfileTarget, slicer_exe: Any
) -> Tuple[Path, Path]:
    """Flatten a remote target's machine/process presets into loadable cache files."""
    vendor = _require_text(target.vendor, "remote vendor id")
    network = _NetworkSession()
    try:
        manifest, ref = _load_vendor_manifest(vendor, slicer_exe, network=network)
        del manifest
    except SlicerProfilesError as exc:
        raise SlicerProfilesError(
            "Could not load remote profiles for vendor {!r}: {}".format(vendor, exc)
        ) from exc
    name_map = _build_remote_name_map(vendor, slicer_exe, ref, network=network)
    machine_sub_path = _find_name_sub_path(
        name_map, target.printer_name, "machine", vendor
    )
    process_sub_path = _find_name_sub_path(
        name_map, target.process_name, "process", vendor
    )
    machine_flattened = _flatten_remote_preset_at(
        vendor, machine_sub_path, ref, name_map, network=network
    )
    process_flattened = _flatten_remote_preset_at(
        vendor, process_sub_path, ref, name_map, network=network
    )
    _fit_bridge_line_width(machine_flattened, process_flattened)
    output_dir = _cache_root() / "flattened" / ref / _safe_filename(vendor)
    machine_path = _write_flattened_profile(
        output_dir, target.printer_name, machine_flattened
    )
    process_path = _write_flattened_profile(
        output_dir, target.process_name, process_flattened
    )
    logger.info(
        "Flattened remote %s presets for %r/%r at ref %r.",
        vendor,
        target.printer_name,
        target.process_name,
        ref,
    )
    return machine_path, process_path


def _prepare_profile_paths(
    target: ProfileTarget, slicer_exe: Any, for_bambu_studio: bool = False
) -> Tuple[Path, Path]:
    """Return the machine/process preset files to pass to the slicer CLI."""
    if target.source == "orca_remote":
        if for_bambu_studio:
            raise SlicerProfilesError(
                "Bambu Studio cannot prepare a project with OrcaSlicer's online "
                "profiles ({}). Pick a Bambu Lab printer from Bambu Studio's own "
                "presets, or send to OrcaSlicer.".format(target.printer_name)
            )
        return _prepare_remote_profiles(target, slicer_exe)
    machine_path = Path(str(target.machine_path))
    process_path = Path(str(target.process_path))
    for label, preset_path in (("machine", machine_path), ("process", process_path)):
        try:
            exists = preset_path.is_file()
        except OSError:
            exists = False
        if not exists:
            raise SlicerProfilesError(
                "The {} preset file does not exist: {!r}. Refresh the printer list "
                "and pick the target again.".format(label, str(preset_path))
            )
    return (
        Path(_standalone_bambu_preset(machine_path, for_bambu_studio)),
        Path(_standalone_bambu_preset(process_path, for_bambu_studio)),
    )


def _material_display_name(material: Any, index: int) -> str:
    """Return a filament display name for the PreparedProject."""
    name = _material_field(material, "display_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "Filament {}".format(index + 1)


def prepare_project(
    base_3mf: Path,
    output_3mf: Path,
    target: ProfileTarget,
    slicer_exe: Any,
    materials: Sequence[Any],
    log_dir: Optional[Path] = None,
    extra_entries: Optional[Mapping[str, Optional[bytes]]] = None,
) -> PreparedProject:
    """Prepare a slicer project from a Filamatch 3MF export.

    The slicer CLI is invoked with the verified flags (``--load-settings``,
    ``--load-filaments``, ``--export-3mf <basename>``, ``--outputdir``,
    ``--logfile``, ``--debug 4``) and the resulting project's
    ``Metadata/project_settings.config`` filament arrays are post-processed from
    the detected materials.  Bambu Studio's CLI has no ``--logfile``: it
    exports into the cache, its output becomes the log and the project is
    moved to ``output_3mf``.  Every filament preset carries its material's
    colour, so the slicer works out fitting purge volumes.

    Args:
        base_3mf: Filamatch 3MF (model_settings, optionally no
            project_settings).
        output_3mf: Destination project path.
        target: Printer/process target (local or remote presets).
        slicer_exe: Slicer executable to run.
        materials: Detected materials exposing ``color_hex``, ``filament_type``,
            ``density_g_cm3`` and ``display_name``.
        log_dir: Directory for the slicer log; defaults to ``logs`` in the
            add-in cache (never the user's output folder).  The log is
            truncated before every run so errors never show an old log.
        extra_entries: Extra archive entries (name -> bytes, ``None`` drops
            the entry) written in the same rewrite as the project settings,
            e.g. :func:`inventory_sync.manifest_entries`.

    Safe to call from a worker thread: it never touches the Fusion API.

    Returns:
        A :class:`PreparedProject` describing the prepared project.

    Raises:
        SlicerProfilesError: If profiles are unavailable, the slicer CLI fails,
            or the prepared project is unusable.
    """
    if target is None:
        raise SlicerProfilesError("prepare_project requires a ProfileTarget, got None")
    if materials is None:
        raise SlicerProfilesError("prepare_project requires a sequence of materials")
    material_list = list(materials)
    if not material_list:
        raise SlicerProfilesError(
            "prepare_project requires at least one detected material"
        )
    slicer_exe = resolve_slicer_executable(slicer_exe)
    base_path = Path(base_3mf)
    output_path = Path(output_3mf)
    try:
        if not base_path.is_file():
            raise SlicerProfilesError(
                "Base 3MF file not found: {!r}".format(str(base_path))
            )
    except OSError as exc:
        raise SlicerProfilesError(
            "Could not inspect the base 3MF {!r}: {}".format(str(base_path), exc)
        ) from exc
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SlicerProfilesError(
            "Could not create the output directory {!r}: {}".format(
                str(output_path.parent), exc
            )
        ) from exc
    if slicer_kind(slicer_exe) == "prusa":
        return _prepare_prusa_project(base_path, output_path, target, slicer_exe, material_list)
    log_directory = Path(log_dir) if log_dir is not None else _cache_root() / "logs"
    try:
        log_directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SlicerProfilesError(
            "Could not create the slicer log directory {!r}: {}".format(
                str(log_directory), exc
            )
        ) from exc
    log_path = log_directory / "slicer_cli.log"
    _remove_file_quietly(log_path)

    for_bambu_studio = _slicer_identity(slicer_exe)[1] == "bambu"
    machine_path, process_path = _prepare_profile_paths(target, slicer_exe, for_bambu_studio)
    online: List[Dict[str, Any]] = []
    # Bambu Studio's own presets already carry OrcaSlicer's Bambu Lab names.
    if target.source == "orca_remote" and str(target.vendor).strip().upper() != "BBL":
        online = _remote_filament_candidates(
            target, slicer_exe, [_material_field(material, "filament_type", "PLA") for material in material_list]
        )
    built_presets: List[str] = []
    filament_paths = _choose_filament_paths(
        material_list, target.printer_name, online, for_bambu_studio, built_presets
    )
    filament_identities = _filament_preset_identities(filament_paths)
    filament_paths = _coloured_filament_presets(
        [_standalone_bambu_preset(path, for_bambu_studio) for path in filament_paths],
        material_list,
    )

    temporary_base: Optional[Path] = None
    try:
        cli_base = _join_touching_bodies(base_path, output_path.parent)
        if cli_base is None:
            cli_base, _removed = _prepare_cli_base(base_path, output_path.parent)
        if cli_base != base_path:
            temporary_base = cli_base
        try:
            output_is_cli_base = output_path.resolve() == cli_base.resolve()
        except OSError:
            output_is_cli_base = False
        if output_is_cli_base:
            logger.debug(
                "Output path equals the CLI base path; not deleting it before the export."
            )
        else:
            _remove_file_quietly(output_path)
        # Bambu Studio has no --logfile and writes result.json next to the
        # project, so it exports into the cache and its output is the log.
        export_dir = output_path.parent
        log_flags = ["--logfile", str(log_path), "--debug", "4"]
        if for_bambu_studio:
            export_dir = _cache_root() / "prepare" / "bambu_studio_out"
            export_dir.mkdir(parents=True, exist_ok=True)
            for old in export_dir.iterdir():
                _remove_file_quietly(old)
            log_flags = ["--debug", "3"]
        command = [
            str(slicer_exe),
            "--load-settings",
            "{};{}".format(str(machine_path), str(process_path)),
            "--load-filaments",
            ";".join(filament_paths),
            "--export-3mf",
            output_path.name,
            str(cli_base),
            "--outputdir",
            str(export_dir),
        ] + log_flags + _layout_flags(cli_base)
        logger.info("Running slicer CLI: %s", _command_text(command))
        try:
            # In the cache folder: OrcaSlicer writes "00000.log" where it runs.
            _cache_root().mkdir(parents=True, exist_ok=True)
            result = _run_command(command, CLI_TIMEOUT_SECONDS, cwd=str(_cache_root()))
            if for_bambu_studio:
                log_path.write_text(_result_text(result), encoding="utf-8")
        except subprocess.TimeoutExpired as exc:
            raise SlicerProfilesError(
                "Slicer CLI timed out after {} seconds. {}".format(
                    CLI_TIMEOUT_SECONDS,
                    _cli_error_message(
                        "the slicer CLI timed out", command, None, log_path
                    ),
                )
            ) from exc
        except OSError as exc:
            raise SlicerProfilesError(
                _cli_error_message(
                    "could not start the slicer executable {!r}: {}".format(
                        str(slicer_exe), exc
                    ),
                    command,
                    None,
                    log_path,
                )
            ) from exc

        returncode = _safe_get(result, "returncode", "a slicer CLI result")
        if returncode not in (0, None):
            reason = "the slicer exited with an error"
            report = _read_json_file(export_dir / "result.json") if for_bambu_studio else None
            if report and report.get("error_string"):
                reason += ": {}".format(report["error_string"])
            raise SlicerProfilesError(_cli_error_message(reason, command, result, log_path))
        exported = export_dir / output_path.name
        if for_bambu_studio and exported.is_file():
            shutil.move(str(exported), str(output_path))
        try:
            output_exists = output_path.is_file() and output_path.stat().st_size > 0
        except OSError:
            output_exists = False
        if not output_exists:
            raise SlicerProfilesError(
                _cli_error_message(
                    "the slicer did not create the output 3MF {!r}".format(
                        str(output_path)
                    ),
                    command,
                    result,
                    log_path,
                )
            )

        _postprocess_project_settings(
            output_path,
            material_list,
            preset_identities=filament_identities,
            extra_entries=extra_entries,
            preset_types=_preset_filament_types(filament_paths),
        )
        check_slot_assignments(output_path, material_list)
        log_tail = _read_log_tail(log_path)
        filament_names = tuple(
            _material_display_name(material, index)
            for index, material in enumerate(material_list)
        )
        prepared = PreparedProject(
            output_path=output_path,
            printer_name=target.printer_name,
            process_name=target.process_name,
            filament_names=filament_names,
            slicer_version=_slicer_version(Path(str(slicer_exe))),
            log_tail=log_tail,
            built_presets=tuple(built_presets),
        )
        logger.info(
            "Prepared slicer project %r (printer=%r, process=%r, filaments=%d).",
            str(output_path),
            target.printer_name,
            target.process_name,
            len(filament_names),
        )
        return prepared
    finally:
        _remove_file_quietly(temporary_base)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL, format="%(levelname)s %(name)s: %(message)s")
    from . import slicer_profiles_selftest

    sys.exit(slicer_profiles_selftest.main())
