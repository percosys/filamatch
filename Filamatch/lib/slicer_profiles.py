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

#: Distance (mm) kept between the prime tower and the edge of the area every
#: nozzle can reach (covers the tower brim).
WIPE_TOWER_MARGIN_MM = 5.0
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
    r"(?:OrcaSlicer|BambuStudio|Bambu Studio|Orca)[-\s]+v?"
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
#: Spelled-out model names and the short code presets use for them.
_PRINTER_ALIASES: Tuple[Tuple[str, str], ...] = (
    ("X1 CARBON", "X1C"),
    ("A1 MINI", "A1M"),
    ("H2D PRO", "H2DP"),
)
_PRINTER_TOKENS: Tuple[str, ...] = (
    "X1C",
    "X1E",
    "X1",
    "P1S",
    "P1P",
    "A1M",
    "A1",
    "H2DP",
    "H2D",
    "H2S",
    "H2C",
    "H3C",
    "X2D",
    "XL",
    "P2S",
    "A2L",
    "MK4",
    "CORE ONE",
    "K1",
)

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
        return candidates
    candidates.append(("orca", "OrcaSlicer", Path("/usr/bin/orca-slicer")))
    candidates.append(("bambu", "BambuStudio", Path("/usr/bin/bambu-studio")))
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
    """Guess the slicer kind (``orca``/``bambu``) from its executable path."""
    lowered = Path(str(path)).name.lower()
    if "orca" in lowered:
        return "orca"
    if "bambu" in lowered:
        return "bambu"
    return ""


def slicer_kind(path: Any) -> str:
    """Return ``orca``, ``bambu`` or ``""`` for a slicer path (executable or ``.app``)."""
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
        kind = "orca" if match.group(0).lower().startswith("orca") else "bambu"
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
        return _filter_compatible_processes(
            local_processes, printer, default_process
        )

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


#: Printers OrcaSlicer cannot slice yet (OrcaSlicer 2.5; Orca issue #17218).
ORCA_UNSUPPORTED_PRINTERS: Tuple[str, ...] = ("H3C",)


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
    preferred_core = re.split(r"\s+@", preferred_upper, maxsplit=1)[0].strip()
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
        score = 0
        if preferred_upper and (preferred_upper in text or preferred_core in text):
            score += 200
        elif preferred_core:
            words = [word for word in preferred_core.replace("@", " ").split() if word]
            if words and all(word in text for word in words):
                score += 180
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


def _preferred_preset_for_material(material: Any) -> str:
    """Return an Orca preset search hint for a detected material.

    Catalog/Bambuddy entries carry ``orca_filament_profile`` directly. For
    design materials that were renamed to the friendly
    ``Brand - Material - Color`` format, reconstruct the product hint from the
    display name (e.g. ``Bambu Lab - PLA Matte - Charcoal`` -> ``Bambu PLA
    Matte``) so Orca is not handed a generic or special-purpose variant.
    """
    explicit = str(_material_field(material, "orca_filament_profile", "") or "").strip()
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
    materials: Sequence[Any], printer_name: str, preferred_candidates: Sequence[Dict[str, Any]] = ()
) -> List[str]:
    """Choose one filament preset file per material for ``--load-filaments``.

    ``preferred_candidates`` (the online vendor's own presets, see
    :func:`_remote_filament_candidates`) come first; then local Bambu Studio
    filament presets (matched by detected filament type and printer token);
    imported filament presets are the fallback.  When a type has no exact
    match, the first chosen file is reused (passing the same file for multiple
    slots is verified to work).
    """
    bambu_candidates = _local_filament_candidates(_bambu_system_dir())
    imported_candidates = _imported_filament_candidates()
    if not preferred_candidates and not bambu_candidates and not imported_candidates:
        raise SlicerProfilesError(
            "No filament profiles are available. Install Bambu Studio (its system "
            "filament presets are used) or import filament JSON profiles with the "
            "palette's profile importer."
        )
    selected: List[Optional[str]] = []
    wanted_types: List[str] = []
    for material in materials:
        filament_type = _material_field(material, "filament_type", "PLA")
        wanted_types.append(str(filament_type or "PLA").strip().upper())
        preferred = _preferred_preset_for_material(material)
        chosen = (
            _pick_filament_candidate(list(preferred_candidates), filament_type, printer_name, preferred)
            or _pick_filament_candidate(bambu_candidates, filament_type, printer_name, preferred)
            or _pick_filament_candidate(imported_candidates, filament_type, printer_name, preferred)
        )
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


def _area_bounds(polygon: Any) -> Optional[Tuple[float, float, float, float]]:
    """Return ``(x0, y0, x1, y1)`` of an Orca area such as ``['0x0', '350x0', ...]``."""
    items = polygon.split(",") if isinstance(polygon, str) else list(polygon or [])
    points = []
    for item in items:
        try:
            x_text, y_text = str(item).strip().split("x")
            points.append((float(x_text), float(y_text)))
        except ValueError:
            return None
    if not points:
        return None
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def _wipe_tower_footprint(settings: Mapping[str, Any]) -> float:
    """Return the prime tower's size on the bed, in mm.

    The tower is wider than ``prime_tower_width``: its brim (``-1`` = automatic,
    about 3 mm) and Bambu Studio's rib walls add to it.  A Bambu Lab X2D tower
    of width 35 covered 47 x 46 mm with four filaments; with only the width
    counted it was placed past the bed and Bambu Studio refused to slice.
    """

    def number(key: str, default: float) -> float:
        try:
            value = float(settings.get(key, default))
        except (TypeError, ValueError):
            return default
        return value

    width = number("prime_tower_width", 60.0) or 60.0
    brim = number("prime_tower_brim_width", -1.0)
    footprint = width + 2 * (brim if brim >= 0 else 3.0)
    if str(settings.get("prime_tower_rib_wall", "0")).strip() in ("1", "true"):
        footprint += number("prime_tower_rib_width", 8.0)
    return footprint


def _fit_wipe_tower(settings: Dict[str, Any]) -> None:
    """Move the prime tower inside the area every nozzle can reach.

    The slicer CLI keeps the generic default position (x 15 mm).  On printers
    whose nozzles cannot reach the whole bed (Bambu Lab H2D: 0-325 / 25-350 mm)
    that puts the tower partly outside the printable area and Orca refuses to
    print; the GUI would move it, the CLI does not.
    """
    if str(settings.get("enable_prime_tower", "0")).strip() not in ("1", "true"):
        return
    bounds = [
        _area_bounds(area)
        for area in (settings.get("extruder_printable_area") or [settings.get("printable_area")])
    ]
    bounds = [bound for bound in bounds if bound is not None]
    if not bounds:
        return
    size = _wipe_tower_footprint(settings)
    # The tower grows from its (x, y) corner; a rotated tower grows the other
    # way (Prusa XL presets: 90 degrees, so it grows to the left of x).
    try:
        angle = int(round(float(settings.get("wipe_tower_rotation_angle") or 0))) % 360
    except (TypeError, ValueError):
        angle = 0
    grow_x, grow_y = {0: (1, 1), 90: (-1, 1), 180: (-1, -1), 270: (1, -1)}.get(angle, (0, 0))

    def limits(low: float, high: float, grow: int) -> Tuple[float, float]:
        low, high = low + WIPE_TOWER_MARGIN_MM, high - WIPE_TOWER_MARGIN_MM
        # grow 0 (any other angle): keep a tower size free on both sides.
        return (low + (size if grow <= 0 else 0.0), high - (size if grow >= 0 else 0.0))

    x0, x1 = limits(max(bound[0] for bound in bounds), min(bound[2] for bound in bounds), grow_x)
    y0, y1 = limits(max(bound[1] for bound in bounds), min(bound[3] for bound in bounds), grow_y)
    if x1 < x0 or y1 < y0:
        return
    for key, low, high in (("wipe_tower_x", x0, x1), ("wipe_tower_y", y0, y1)):
        values = settings.get(key)
        if not isinstance(values, list):
            continue
        fitted = []
        for value in values:
            try:
                position = float(value)
            except (TypeError, ValueError):
                fitted.append(value)
                continue
            clamped = min(max(position, low), high)
            if clamped != position:
                logger.info("Moved the prime tower %s from %s to %g mm.", key, value, clamped)
            fitted.append("{:g}".format(clamped))
        settings[key] = fitted


def _fit_nozzle_volume_types(settings: Dict[str, Any]) -> None:
    """Give every nozzle its own flow type (Standard, High Flow, ...).

    OrcaSlicer writes one ``nozzle_volume_type`` for the two Bambu Lab H2D
    nozzles; Bambu Studio 2.9 then shows both nozzles as "High Flow".  Missing
    entries take the printer preset's default for that nozzle.
    """
    current = settings.get("nozzle_volume_type")
    diameters = settings.get("nozzle_diameter")
    if not isinstance(current, list) or not current or not isinstance(diameters, list):
        return
    if len(current) >= len(diameters):
        return
    defaults = settings.get("default_nozzle_volume_type")
    defaults = defaults if isinstance(defaults, list) else []
    fitted = [str(value) for value in current]
    for index in range(len(current), len(diameters)):
        fitted.append(str(defaults[index] if index < len(defaults) else current[-1]))
    logger.info("Nozzle flow types %s -> %s (one per nozzle).", current, fitted)
    settings["nozzle_volume_type"] = fitted


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


def _nozzle_stats(settings: Mapping[str, Any]) -> List[str]:
    """Return ``extruder_nozzle_stats`` for a printer with more than one nozzle.

    One entry per extruder, ``"<flow type>#<nozzle count>"``: the flow type
    from ``nozzle_volume_type`` and the count from ``extruder_max_nozzle_count``
    (a nozzle rack).  Bambu Lab H2D ``["Standard#1", "High Flow#1"]``, H2C
    ``["Standard#1", "Standard#6"]``, H3C ``["Standard#9"]``.  OrcaSlicer stops
    when an entry has no nozzle of its extruder's flow type, so the type is
    always the extruder's own.  Empty for a one-nozzle printer or when the
    flow types do not match the extruders.
    """
    flow_types = settings.get("nozzle_volume_type")
    diameters = settings.get("nozzle_diameter")
    if not isinstance(diameters, list) or not isinstance(flow_types, list) or len(flow_types) != len(diameters):
        return []
    counts = settings.get("extruder_max_nozzle_count")
    counts = counts if isinstance(counts, list) else []

    def count(index: int) -> int:
        try:
            return max(1, int(str(counts[index]).strip()))
        except (IndexError, ValueError):
            return 1

    stats = ["{}#{}".format(flow, count(index)) for index, flow in enumerate(flow_types)]
    if sum(count(index) for index in range(len(flow_types))) < 2:
        return []
    return stats


def estimate_print(project_3mf: Path, slicer_exe: Any) -> Dict[str, Any]:
    """Slice a copy of a prepared project and return its print time and filament.

    The copy is sliced by ``slicer_exe`` (the slicer that prepared it) in a
    temporary folder; the project itself is not changed.  On a printer with
    more than one nozzle the copy gets ``extruder_nozzle_stats`` (see
    :func:`_nozzle_stats`), which the slicer otherwise reads from the printer:
    without it Bambu Studio's command line stops with "No valid nozzle found".

    Returns:
        ``{"minutes": int, "grams": float, "filament_grams": [float, ...]}``;
        ``filament_grams`` has one entry per filament slot (0 when unused).

    Raises:
        SlicerProfilesError: If the slicer fails or reports no estimate.
    """
    slicer_exe = resolve_slicer_executable(slicer_exe)
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
        seconds = re.search(r'key="prediction" value="([\d.]+)"', info)
        grams = re.search(r'key="weight" value="([\d.]+)"', info)
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
        used = {
            int(number): float(value)
            for number, value in re.findall(r'<filament id="(\d+)"[^>]*?used_g="([\d.]+)"', info)
        }
        return {
            "minutes": int(round(float(seconds.group(1)) / 60.0)),
            "grams": round(float(grams.group(1)), 1),
            # One entry per project filament (slot 1, 2, ...); an unused one is 0.
            "filament_grams": [round(used.get(number, 0.0), 1) for number in range(1, max(used, default=0) + 1)],
        }
    finally:
        shutil.rmtree(str(work), ignore_errors=True)


NOZZLE_LEFT = "left"
NOZZLE_RIGHT = "right"
# ``filament_map`` numbers the nozzles 1 = left, 2 = right (Bambu Studio 2.9.1,
# Bambu Lab H2D); OrcaSlicer reads the same keys.
_FILAMENT_MAP_VALUES = {NOZZLE_LEFT: "1", NOZZLE_RIGHT: "2"}
# The printer's own extruder ids (MQTT: 0 = right/main, 1 = left/deputy).
# ``physical_extruder_map`` turns a slicer extruder index into one of these.
_PRINTER_EXTRUDER_IDS = {NOZZLE_RIGHT: "0", NOZZLE_LEFT: "1"}


def _filament_map_values(settings: Mapping[str, Any]) -> Dict[str, str]:
    """``filament_map`` value of each side, from the project's ``physical_extruder_map``.

    Bambu Lab H2D, H2C and X2D map slicer extruder 1 to the printer's left
    extruder (``["1", "0"]``).  Without the key the H2D numbering is used.
    """
    physical = settings.get("physical_extruder_map")
    if isinstance(physical, list) and len(physical) == 2:
        ids = [str(value).strip() for value in physical]
        values = {}
        for side, printer_id in _PRINTER_EXTRUDER_IDS.items():
            if printer_id in ids:
                values[side] = str(ids.index(printer_id) + 1)
        if len(values) == 2:
            return values
    return dict(_FILAMENT_MAP_VALUES)


def _unprintable_on_nozzle(settings: Mapping[str, Any], values: Sequence[str]) -> List[str]:
    """Filaments pinned to an extruder that cannot print their type.

    ``unprintable_filament_types`` has one comma list per extruder; the Bambu
    Lab H2C, for example, prints no TPU on its left extruder.  Returns lines
    such as ``"filament 3 (TPU) on extruder 1"``.
    """
    unprintable = settings.get("unprintable_filament_types")
    types = settings.get("filament_type")
    if not isinstance(unprintable, list) or not isinstance(types, list):
        return []
    problems = []
    for index, value in enumerate(values):
        try:
            extruder = int(value) - 1
        except ValueError:
            continue
        if not 0 <= extruder < len(unprintable) or index >= len(types):
            continue
        refused = {item.strip().upper() for item in str(unprintable[extruder]).split(",") if item.strip()}
        if str(types[index]).strip().upper() in refused:
            problems.append("filament {} ({}) on extruder {}".format(index + 1, types[index], extruder + 1))
    return problems


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
    filament_paths = _choose_filament_paths(material_list, target.printer_name, online)
    filament_identities = _filament_preset_identities(filament_paths)
    filament_paths = _coloured_filament_presets(
        [_standalone_bambu_preset(path, for_bambu_studio) for path in filament_paths],
        material_list,
    )

    temporary_base: Optional[Path] = None
    try:
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


# ---------------------------------------------------------------------------
# Self-test doubles and checks (only used by the __main__ self-test)
# ---------------------------------------------------------------------------


def _reset_remote_state() -> None:
    """Forget the session's negative network caches (self-test helper)."""
    _MISSING_URLS.clear()
    _MISSING_REFS.clear()
    _GOOD_REFS.clear()


class _FakeCompletedProcess:
    """Minimal ``subprocess.CompletedProcess`` stand-in for the self-test."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeRunner:
    """Replaceable command runner: answers ``--help`` and delegates CLI calls."""

    def __init__(self, help_text: str = "OrcaSlicer-2.5.0-rc") -> None:
        self.help_text = help_text
        self.commands: List[List[str]] = []
        self.cli_handler: Callable[[List[str], "_FakeRunner"], _FakeCompletedProcess] = (
            lambda command, runner: _FakeCompletedProcess()
        )

    def run(
        self, command: Sequence[str], timeout: float, cwd: Optional[str] = None
    ) -> _FakeCompletedProcess:
        """Record the command and dispatch help vs CLI calls."""
        recorded = list(command)
        self.commands.append(recorded)
        if len(recorded) >= 2 and recorded[1] == "--help":
            return _FakeCompletedProcess(0, self.help_text + "\n", "")
        return self.cli_handler(recorded, self)

    @property
    def cli_commands(self) -> List[List[str]]:
        """Return only the non-help commands that were run."""
        return [
            command
            for command in self.commands
            if not (len(command) >= 2 and command[1] == "--help")
        ]


def _flag_value(command: Sequence[str], flag: str) -> Optional[str]:
    """Return the value following ``flag`` in a command list."""
    for index, part in enumerate(command):
        if part == flag and index + 1 < len(command):
            return str(command[index + 1])
    return None


def _write_fixture_3mf(
    path: Path,
    extra_entries: Optional[Sequence[Tuple[str, bytes]]] = None,
    include_project_settings: bool = False,
    project_settings: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write a small Fusion-like 3MF fixture for the self-test."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entries: List[Tuple[str, bytes]] = [
        ("[Content_Types].xml", b"<?xml version='1.0'?><Types/>"),
        ("_rels/.rels", b"<?xml version='1.0'?><Relationships/>"),
        ("3D/3dmodel.model", b"<model><resources/><build/></model>"),
        ("Metadata/model_settings.config", b"<config><object id='2'/></config>"),
        ("Metadata/thumbnail.png", b"\x89PNG\r\n\x1a\nfixture"),
    ]
    if include_project_settings:
        settings = project_settings if project_settings is not None else {"stale": True}
        entries.append(
            (_PROJECT_SETTINGS_PART, json.dumps(settings).encode("utf-8"))
        )
    entries.extend(extra_entries or [])
    with zipfile.ZipFile(str(path), "w") as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return path


def _read_archive_entry(path: Path, entry_name: str) -> bytes:
    """Return one archive entry's bytes."""
    with zipfile.ZipFile(str(path), "r") as archive:
        return archive.read(entry_name)


def _archive_names(path: Path) -> List[str]:
    """Return archive member names in order."""
    with zipfile.ZipFile(str(path), "r") as archive:
        return archive.namelist()


def _fixture_materials() -> List[Any]:
    """Return detection-like material objects for the self-test."""
    return [
        {
            "color_hex": "#161616",
            "filament_type": "PLA",
            "density_g_cm3": 1.24,
            "display_name": "Bambu Lab PLA Basic - Black",
        },
        {
            "color_hex": "#FFFFFF",
            "filament_type": "PETG",
            "density_g_cm3": 1.27,
            "display_name": "Bambu Lab PETG HF - White",
        },
        {
            "color_hex": "#111111",
            "filament_type": "ABS",
            "density_g_cm3": 1.04,
            "display_name": "Polymaker PolyLite ABS - Black",
        },
    ]


def _write_profile_json(path: Path, data: Dict[str, Any]) -> Path:
    """Write a preset JSON fixture file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


class _TestEnvironment:
    """Owns the temporary fixtures, fake runner and fake network for the checks."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.cache_root = root / "cache"
        self.bambu_system = root / "bambu_system"
        self.slicer_exe = root / "OrcaSlicer"
        self.slicer_exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.runner = _FakeRunner()
        self.responses: Dict[str, bytes] = {}
        self.opener_calls: List[str] = []
        self._previous: Optional[Tuple[Any, ...]] = None
        self.materials = _fixture_materials()
        self._build_local_profiles()
        self._build_remote_responses()

    def install(self) -> None:
        """Install the fake hooks into the module under test."""
        global _CACHE_ROOT_OVERRIDE, _BAMBU_SYSTEM_DIR_OVERRIDE
        global _SLICER_CANDIDATES_OVERRIDE, _command_runner, _url_opener
        self._previous = (
            _CACHE_ROOT_OVERRIDE,
            _BAMBU_SYSTEM_DIR_OVERRIDE,
            _SLICER_CANDIDATES_OVERRIDE,
            _command_runner,
            _url_opener,
            dict(_VERSION_CACHE),
            (set(_MISSING_URLS), set(_MISSING_REFS), set(_GOOD_REFS)),
        )
        _reset_remote_state()
        _CACHE_ROOT_OVERRIDE = self.cache_root
        _BAMBU_SYSTEM_DIR_OVERRIDE = self.bambu_system
        _SLICER_CANDIDATES_OVERRIDE = [
            ("orca", "OrcaSlicer", self.slicer_exe),
        ]
        _command_runner = self.runner.run
        _url_opener = self.open_url
        _VERSION_CACHE.clear()

    def restore(self) -> None:
        """Restore the original module hooks."""
        global _CACHE_ROOT_OVERRIDE, _BAMBU_SYSTEM_DIR_OVERRIDE
        global _SLICER_CANDIDATES_OVERRIDE, _command_runner, _url_opener
        if self._previous is None:
            return
        (
            _CACHE_ROOT_OVERRIDE,
            _BAMBU_SYSTEM_DIR_OVERRIDE,
            _SLICER_CANDIDATES_OVERRIDE,
            _command_runner,
            _url_opener,
            version_cache,
            remote_state,
        ) = self._previous
        _VERSION_CACHE.clear()
        _VERSION_CACHE.update(version_cache)
        _reset_remote_state()
        _MISSING_URLS.update(remote_state[0])
        _MISSING_REFS.update(remote_state[1])
        _GOOD_REFS.update(remote_state[2])

    def open_url(self, url: str, timeout: float) -> bytes:
        """Fake network opener backed by ``responses``; unknown URLs 404."""
        self.opener_calls.append(url)
        payload = self.responses.get(url)
        if payload is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)  # type: ignore[arg-type]
        return payload

    # -- fixtures ---------------------------------------------------------

    def _build_local_profiles(self) -> None:
        """Create the temporary Bambu Studio system profile tree."""
        machine_dir = self.bambu_system / "machine"
        process_dir = self.bambu_system / "process"
        filament_dir = self.bambu_system / "filament"
        _write_profile_json(
            machine_dir / "Bambu Lab X1 Carbon 0.4 nozzle.json",
            {
                "name": "Bambu Lab X1 Carbon 0.4 nozzle",
                "type": "machine",
                "default_print_profile": "0.20mm Standard @BBL X1C",
                "importable": True,
            },
        )
        _write_profile_json(
            machine_dir / "fdm_machine_common.json",
            {"name": "fdm_machine_common", "type": "machine"},
        )
        _write_profile_json(
            machine_dir / "template_machine.json",
            {"name": "template machine", "type": "machine"},
        )
        _write_profile_json(
            machine_dir / "not_importable.json",
            {"name": "Bambu Lab Not Importable", "type": "machine", "importable": False},
        )
        _write_profile_json(
            process_dir / "0.20mm Standard @BBL X1C.json",
            {"name": "0.20mm Standard @BBL X1C", "type": "process"},
        )
        _write_profile_json(
            process_dir / "0.24mm Draft @BBL X1C.json",
            {"name": "0.24mm Draft @BBL X1C", "type": "process"},
        )
        _write_profile_json(
            process_dir / "fdm_process_common.json",
            {"name": "fdm_process_common", "type": "process"},
        )
        _write_profile_json(
            process_dir / "common_process.json",
            {"name": "Common Process", "type": "process"},
        )
        _write_profile_json(
            filament_dir / "Bambu PLA Basic @BBL X1C.json",
            {
                "name": "Bambu PLA Basic @BBL X1C",
                "type": "filament",
                "filament_type": "PLA",
            },
        )
        _write_profile_json(
            filament_dir / "Bambu PETG HF @BBL X1C.json",
            {
                "name": "Bambu PETG HF @BBL X1C",
                "type": "filament",
                "filament_type": "PETG",
            },
        )
        _write_profile_json(
            filament_dir / "Bambu ABS @BBL X1C.json",
            {
                "name": "Bambu ABS @BBL X1C",
                "type": "filament",
                "filament_type": "ABS",
            },
        )
        _write_profile_json(
            filament_dir / "fdm_filament_pla.json",
            {"name": "fdm_filament_pla", "type": "filament"},
        )

    def _remote_url(self, ref: str, relative_path: str) -> str:
        """Build a fake repository URL for fixtures (same encoding as production)."""
        clean = "/".join(
            part for part in str(relative_path).replace("\\", "/").split("/") if part not in ("", ".")
        )
        encoded = "/".join(quote(part, safe="") for part in clean.split("/"))
        return REMOTE_PROFILES_BASE.format(ref=ref, relative_path=encoded)

    def _add_remote_json(self, ref: str, relative_path: str, data: Any) -> None:
        """Register a fake remote JSON payload."""
        self.responses[self._remote_url(ref, relative_path)] = json.dumps(data).encode(
            "utf-8"
        )

    def _build_remote_responses(self) -> None:
        """Create the fake OrcaSlicer repository (vendor manifests and presets)."""
        ref = "v2.5.0-rc"
        self._add_remote_json(
            ref,
            "BBL.json",
            {
                "name": "Bambu Lab",
                "machine_list": [
                    {
                        "name": "Bambu Lab X1 Carbon 0.4 nozzle",
                        "sub_path": "BBL/machine/X1 Carbon 0.4 nozzle.json",
                    },
                    {
                        "name": "Bambu Lab A1 0.4 nozzle",
                        "sub_path": "BBL/machine/A1 0.4 nozzle.json",
                    },
                    {
                        "name": "fdm_machine_common",
                        "sub_path": "BBL/machine/fdm_machine_common.json",
                    },
                ],
                "process_list": [
                    {
                        "name": "0.20mm Standard @BBL X1C",
                        "sub_path": "BBL/process/0.20mm Standard @BBL X1C.json",
                    },
                    {
                        "name": "0.24mm Draft @BBL X1C",
                        "sub_path": "BBL/process/0.24mm Draft @BBL X1C.json",
                    },
                    {
                        "name": "fdm_process_common",
                        "sub_path": "BBL/process/fdm_process_common.json",
                    },
                ],
                "filament_list": [
                    {
                        "name": "Bambu PLA Basic @BBL X1C",
                        "sub_path": "BBL/filament/Bambu PLA Basic @BBL X1C.json",
                    },
                ],
            },
        )
        self._add_remote_json(
            ref,
            "OrcaFilamentLibrary.json",
            {
                "name": "Orca Filament Library",
                "filament_list": [
                    {
                        "name": "fdm_filament_pla",
                        "sub_path": "OrcaFilamentLibrary/filament/fdm_filament_pla.json",
                    },
                ],
            },
        )
        self._add_remote_json(
            ref,
            "BBL/machine/X1 Carbon 0.4 nozzle.json",
            {
                "name": "Bambu Lab X1 Carbon 0.4 nozzle",
                "type": "machine",
                "inherits": "fdm_machine_common",
                "default_print_profile": "0.20mm Standard @BBL X1C",
            },
        )
        self._add_remote_json(
            ref,
            "BBL/machine/fdm_machine_common.json",
            {
                "name": "fdm_machine_common",
                "type": "machine",
                "machine_max_acceleration_x": ["1000", "1000"],
                "from": "system",
            },
        )
        self._add_remote_json(
            ref,
            "BBL/process/0.20mm Standard @BBL X1C.json",
            {
                "name": "0.20mm Standard @BBL X1C",
                "type": "process",
                "inherits": "fdm_process_common",
                "layer_height": "0.2",
            },
        )
        self._add_remote_json(
            ref,
            "BBL/process/fdm_process_common.json",
            {
                "name": "fdm_process_common",
                "type": "process",
                "outer_wall_speed": ["200"],
            },
        )
        # Prusa is only available on the main branch: exercises the tag fallback.
        self._add_remote_json(
            "main",
            "Prusa.json",
            {
                "name": "Prusa",
                "machine_list": [
                    {
                        "name": "Prusa MK4 0.4 nozzle",
                        "sub_path": "Prusa/machine/Prusa MK4 0.4 nozzle.json",
                    }
                ],
                "process_list": [],
                "filament_list": [
                    {"name": "Generic PLA @base", "sub_path": "filament/Generic PLA @base.json"},
                    {"name": "Generic PLA @Prusa MK4", "sub_path": "filament/Generic PLA @Prusa MK4.json"},
                    {"name": "Generic PLA @Prusa XL", "sub_path": "filament/Generic PLA @Prusa XL.json"},
                ],
            },
        )
        self._add_remote_json(
            "main",
            "Prusa/filament/Generic PLA @base.json",
            {"name": "Generic PLA @base", "type": "filament", "filament_type": ["PLA"],
             "nozzle_temperature": ["215"], "instantiation": "false"},
        )
        for printer in ("MK4", "XL"):
            self._add_remote_json(
                "main",
                "Prusa/filament/Generic PLA @Prusa {}.json".format(printer),
                {"name": "Generic PLA @Prusa {}".format(printer), "type": "filament",
                 "inherits": "Generic PLA @base", "instantiation": "true",
                 "compatible_printers": ["Prusa {} 0.4 nozzle".format(printer)]},
            )
        self._add_remote_json(
            "main",
            "OrcaFilamentLibrary.json",
            {"name": "OrcaFilamentLibrary", "filament_list": [
                {"name": "Generic PETG @System", "sub_path": "filament/Generic PETG @System.json"},
            ]},
        )
        self._add_remote_json(
            "main",
            "OrcaFilamentLibrary/filament/Generic PETG @System.json",
            {"name": "Generic PETG @System", "type": "filament", "filament_type": ["PETG"],
             "nozzle_temperature": ["240"], "instantiation": "true"},
        )


def _check_find_installed_slicers(environment: _TestEnvironment) -> None:
    """Installed slicers are discovered and their versions parsed from --help."""
    global _SLICER_CANDIDATES_OVERRIDE
    installs = find_installed_slicers()
    _expect(len(installs) == 1, "expected exactly one fake installed slicer")
    install = installs[0]
    _expect(install.kind == "orca", "install kind must be orca")
    _expect(install.name == "OrcaSlicer", "install name mismatch")
    _expect(install.path == str(environment.slicer_exe), "install path mismatch")
    _expect(install.version == "2.5.0-rc", "version must be parsed from --help")

    environment.runner.help_text = "BambuStudio-02.07.01.62"
    _VERSION_CACHE.clear()
    _SLICER_CANDIDATES_OVERRIDE = [("bambu", "BambuStudio", environment.slicer_exe)]
    try:
        bambu_installs = find_installed_slicers()
    finally:
        _SLICER_CANDIDATES_OVERRIDE = [
            ("orca", "OrcaSlicer", environment.slicer_exe)
        ]
    _expect(
        bambu_installs[0].version == "02.07.01.62",
        "Bambu Studio version must be parsed",
    )
    environment.runner.help_text = "OrcaSlicer-2.5.0-rc"
    _VERSION_CACHE.clear()


def _check_local_discovery(environment: _TestEnvironment) -> None:
    """Local vendor/printer/process discovery filters base and template presets."""
    vendors = list_vendors(environment.slicer_exe)
    local_vendors = [vendor for vendor in vendors if vendor["source"] == "bambu_local"]
    _expect(len(local_vendors) == 1, "a local Bambu Studio vendor must be listed")
    _expect(local_vendors[0]["id"] == "bambu_local", "local vendor id mismatch")
    _expect(local_vendors[0]["has_printers"] is True, "local vendor must have printers")

    printers = list_printers(environment.slicer_exe, "bambu_local")
    names = [printer["name"] for printer in printers]
    _expect(
        names == ["Bambu Lab X1 Carbon 0.4 nozzle"],
        "only the instantiable local machine preset must be listed, got {}".format(names),
    )
    _expect(
        printers[0]["default_process"] == "0.20mm Standard @BBL X1C",
        "the machine default process must be reported",
    )

    processes = list_processes(
        environment.slicer_exe, "bambu_local", "Bambu Lab X1 Carbon 0.4 nozzle"
    )
    process_names = [process["name"] for process in processes]
    _expect(
        sorted(process_names) == ["0.20mm Standard @BBL X1C", "0.24mm Draft @BBL X1C"],
        "base/common process presets must be filtered, got {}".format(process_names),
    )
    defaults = [process for process in processes if process["default"]]
    _expect(len(defaults) == 1, "exactly one default process must be marked")
    _expect(
        defaults[0]["name"] == "0.20mm Standard @BBL X1C",
        "the machine default_print_profile must be marked as default",
    )


def _flatten_for_test(vendor_id: str, sub_path: str, slicer_exe: Any) -> Dict[str, Any]:
    """Load the vendor manifest and flatten one remote preset (self-test helper)."""
    _manifest, ref = _load_vendor_manifest(vendor_id, slicer_exe)
    name_map = _build_remote_name_map(vendor_id, slicer_exe, ref)
    return _flatten_remote_preset_at(vendor_id, sub_path, ref, name_map)


def _check_remote_listing_and_flattening(environment: _TestEnvironment) -> None:
    """Remote manifests list printers/processes and inheritance is flattened."""
    vendors = list_vendors(environment.slicer_exe)
    by_id = {vendor["id"]: vendor for vendor in vendors}
    _expect(by_id["BBL"]["has_printers"] is True, "BBL must be listed with printers")
    _expect(by_id["Prusa"]["has_printers"] is True, "Prusa main-branch fallback must work")
    _expect(by_id["Creality"]["has_printers"] is False, "unreachable vendors must be listed")
    _expect(by_id["imported"]["source"] == "imported", "imported vendor must be listed")

    printers = list_printers(environment.slicer_exe, "BBL")
    printer_names = [printer["name"] for printer in printers]
    _expect(
        printer_names
        == ["Bambu Lab X1 Carbon 0.4 nozzle", "Bambu Lab A1 0.4 nozzle"],
        "remote machine_list must be filtered by fdm_ prefix, got {}".format(printer_names),
    )
    _expect(
        printers[0]["machine_path"].endswith(
            "profile_cache/v2.5.0-rc/BBL/machine/X1 Carbon 0.4 nozzle.json"
        ),
        "remote machine_path must be a tag-matched cache path, got {}".format(
            printers[0]["machine_path"]
        ),
    )

    processes = list_processes(
        environment.slicer_exe, "BBL", "Bambu Lab X1 Carbon 0.4 nozzle"
    )
    process_names = [process["name"] for process in processes]
    _expect(
        process_names == ["0.20mm Standard @BBL X1C", "0.24mm Draft @BBL X1C"],
        "remote process_list must filter fdm_/common presets, got {}".format(
            process_names
        ),
    )
    defaults = [process for process in processes if process["default"]]
    _expect(
        len(defaults) == 1 and defaults[0]["name"] == "0.20mm Standard @BBL X1C",
        "remote default process must come from the flattened machine preset",
    )

    manifest = _load_vendor_manifest("BBL", environment.slicer_exe)[0]
    machine_entry = next(
        entry
        for entry in _manifest_entries(manifest, "machine_list")
        if entry["name"] == "Bambu Lab X1 Carbon 0.4 nozzle"
    )
    flattened = _flatten_for_test(
        "BBL", machine_entry["sub_path"], environment.slicer_exe
    )
    _expect("inherits" not in flattened, "flattening must remove 'inherits'")
    _expect(flattened.get("from") == "user", "flattened presets must be from=user")
    _expect(
        flattened.get("machine_max_acceleration_x") == ["1000", "1000"],
        "flattening must merge parent keys",
    )
    _expect(
        flattened.get("default_print_profile") == "0.20mm Standard @BBL X1C",
        "flattening must keep child keys",
    )
    flattened_process = _flatten_for_test(
        "BBL", "BBL/process/0.20mm Standard @BBL X1C.json", environment.slicer_exe
    )
    _expect(
        flattened_process.get("outer_wall_speed") == ["200"],
        "process flattening must merge the parent keys",
    )
    _expect(
        flattened_process.get("layer_height") == "0.2",
        "process child keys must survive",
    )


def _check_unresolved_parent_error(environment: _TestEnvironment) -> None:
    """An unresolvable inheritance parent raises with the preset chain."""
    ref = "v2.5.0-rc"
    environment._add_remote_json(
        ref,
        "BBL/machine/Broken Child.json",
        {"name": "Broken Child", "type": "machine", "inherits": "missing_parent"},
    )
    try:
        _flatten_for_test(
            "BBL", "BBL/machine/Broken Child.json", environment.slicer_exe
        )
    except SlicerProfilesError as exc:
        message = str(exc)
        _expect("missing_parent" in message, "error must name the missing parent")
        _expect("Broken Child" in message, "error must include the chain")
    else:
        raise AssertionError("unresolvable parents must raise SlicerProfilesError")


def _check_imported_profiles(environment: _TestEnvironment) -> None:
    """Profile files are validated/imported by type and deduplicated."""
    source_dir = environment.root / "import_source"
    _write_profile_json(
        source_dir / "machine.json",
        {"name": "Imported Machine", "type": "machine", "setting_id": "IM-1"},
    )
    _write_profile_json(
        source_dir / "process.json",
        {"name": "Imported Process", "type": "process", "setting_id": "IP-1"},
    )
    _write_profile_json(
        source_dir / "filament.json",
        {"name": "Imported Filament", "type": "filament", "filament_type": "PLA"},
    )
    source_paths = [
        source_dir / "machine.json",
        source_dir / "process.json",
        source_dir / "filament.json",
    ]

    first = import_profile_files(source_paths)
    _expect(len(first["machines"]) == 1, "one imported machine expected")
    _expect(len(first["processes"]) == 1, "one imported process expected")
    _expect(len(first["filaments"]) == 1, "one imported filament expected")
    _expect(
        Path(first["root"]) == environment.cache_root / "profiles",
        "imported root must be the add-in profile directory, got {}".format(first["root"]),
    )
    _expect(
        first["machines"][0]["path"].endswith(
            os.path.join("machine", "Imported Machine.json")
        ),
        "machine profile must be copied under machine/",
    )

    second = import_profile_files(source_paths)
    _expect(len(second["machines"]) == 1, "re-import must deduplicate machines")
    _expect(len(second["processes"]) == 1, "re-import must deduplicate processes")
    _expect(len(second["filaments"]) == 1, "re-import must deduplicate filaments")

    imported_printers = list_printers(environment.slicer_exe, "imported")
    _expect(
        [printer["name"] for printer in imported_printers] == ["Imported Machine"],
        "imported machines must be listed",
    )
    imported_processes = list_processes(
        environment.slicer_exe, "imported", "Imported Machine"
    )
    _expect(
        [process["name"] for process in imported_processes] == ["Imported Process"],
        "imported processes must be listed",
    )

    invalid = _write_profile_json(source_dir / "invalid.json", {"name": "No Type"})
    try:
        import_profile_files([invalid])
    except SlicerProfilesError as exc:
        _expect("type" in str(exc), "invalid profile errors must mention the type field")
    else:
        raise AssertionError("profiles without a type must raise SlicerProfilesError")


def _write_prepared_3mf(
    path: Path,
    printer_settings_id: str = "Bambu Lab X1 Carbon 0.4 nozzle",
    include_printer_id: bool = True,
    model_settings: Optional[bytes] = None,
) -> None:
    """Write a slicer-like prepared project 3MF for the self-test."""
    settings: Dict[str, Any] = {
        "filament_colour": ["#000000"],
        "filament_type": ["PLA"],
        "filament_density": ["1.00"],
        "filament_settings_id": ["slicer-chosen-1", "slicer-chosen-2"],
        "filament_diameter": ["1.75", "1.75"],
        "filament_map": ["1"],
        "filament_map_2": ["1"],
        "filament_nozzle_map": ["0"],
        "filament_volume_map": ["0"],
        "some_other_key": "preserved",
    }
    if include_printer_id:
        settings["printer_settings_id"] = printer_settings_id
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(path), "w") as archive:
        archive.writestr("[Content_Types].xml", b"<?xml version='1.0'?><Types/>")
        archive.writestr("3D/3dmodel.model", b"<model/>")
        archive.writestr(_PROJECT_SETTINGS_PART, json.dumps(settings).encode("utf-8"))
        archive.writestr("Metadata/thumbnail.png", b"slicer-thumb")
        if model_settings is not None:
            archive.writestr(_MODEL_SETTINGS_PART, model_settings)


def _prepare_command_handler(
    records: Dict[str, Any],
    include_printer_settings_id: bool = True,
    returncode: int = 0,
    write_output: bool = True,
    model_settings: Optional[bytes] = None,
) -> Callable[[List[str], _FakeRunner], _FakeCompletedProcess]:
    """Build a fake CLI handler that records the command and writes a project."""

    def handler(command: List[str], runner: _FakeRunner) -> _FakeCompletedProcess:
        output_name = _flag_value(command, "--export-3mf")
        output_dir = _flag_value(command, "--outputdir")
        logfile = _flag_value(command, "--logfile")
        base_index = command.index("--export-3mf") + 2
        base_path = Path(command[base_index])
        records["command"] = list(command)
        records["output_name"] = output_name
        records["output_dir"] = output_dir
        records["logfile"] = logfile
        records["base_path"] = str(base_path)
        records["base_has_project_settings"] = (
            _PROJECT_SETTINGS_PART in _archive_names(base_path)
        )
        if logfile is not None:
            Path(logfile).parent.mkdir(parents=True, exist_ok=True)
            Path(logfile).write_text(
                "\n".join(
                    "slicer log line {}".format(index) for index in range(1, 41)
                )
                + "\n",
                encoding="utf-8",
            )
        if write_output and returncode == 0 and output_name and output_dir:
            _write_prepared_3mf(
                Path(output_dir) / output_name,
                include_printer_id=include_printer_settings_id,
                model_settings=model_settings,
            )
        return _FakeCompletedProcess(returncode, "slicer stdout", "slicer stderr")

    return handler


def _local_target(environment: _TestEnvironment) -> ProfileTarget:
    """Return the local Bambu Studio profile target used by the checks."""
    return ProfileTarget(
        vendor="bambu_local",
        printer_name="Bambu Lab X1 Carbon 0.4 nozzle",
        machine_path=str(
            environment.bambu_system
            / "machine"
            / "Bambu Lab X1 Carbon 0.4 nozzle.json"
        ),
        process_name="0.20mm Standard @BBL X1C",
        process_path=str(
            environment.bambu_system
            / "process"
            / "0.20mm Standard @BBL X1C.json"
        ),
        source="bambu_local",
    )


def _check_prepare_local(environment: _TestEnvironment) -> None:
    """Local target: CLI command shape, base copy rule and settings post-processing."""
    records: Dict[str, Any] = {}
    environment.runner.cli_handler = _prepare_command_handler(records)
    base_3mf = _write_fixture_3mf(
        environment.root / "base.3mf", include_project_settings=True
    )
    output = environment.root / "out" / "project.3mf"
    log_dir = environment.root / "logs"
    target = _local_target(environment)

    prepared = prepare_project(
        base_3mf,
        output,
        target,
        environment.slicer_exe,
        environment.materials,
        log_dir=log_dir,
    )

    _expect(prepared.output_path == output, "prepared project path mismatch")
    _expect(prepared.printer_name == target.printer_name, "printer name mismatch")
    _expect(prepared.process_name == target.process_name, "process name mismatch")
    _expect(prepared.slicer_version == "2.5.0-rc", "slicer version mismatch")
    _expect(
        prepared.filament_names
        == tuple(material["display_name"] for material in environment.materials),
        "filament names must come from the detected materials",
    )
    _expect(
        "slicer log line 40" in prepared.log_tail,
        "log tail must include the last lines",
    )
    _expect(
        len(prepared.log_tail.splitlines()) == LOG_TAIL_LINES,
        "log tail must be capped",
    )

    command = records["command"]
    _expect(
        records["base_has_project_settings"] is False,
        "the CLI base must not contain project_settings",
    )
    _expect(
        _flag_value(command, "--export-3mf") == output.name,
        "--export-3mf must be a bare basename",
    )
    _expect(
        _flag_value(command, "--outputdir") == str(output.parent),
        "--outputdir must be the output directory",
    )
    settings_paths = (_flag_value(command, "--load-settings") or "").split(";")
    loaded = [json.loads(Path(path).read_text(encoding="utf-8")) for path in settings_paths]
    _expect(
        [preset.get("name") for preset in loaded] == [target.printer_name, target.process_name]
        and all(preset.get("from") == "user" and "inherits" not in preset for preset in loaded),
        "--load-settings must be stand-alone machine;process copies for OrcaSlicer: {}".format(loaded),
    )
    _expect(_flag_value(command, "--debug") == "4", "--debug 4 must be requested")
    _expect(command[-1] == "4", "the CLI command must end with --debug 4")
    _expect(
        _flag_value(command, "--logfile") == str(log_dir / "slicer_cli.log"),
        "--logfile must point at the requested log directory",
    )
    filament_arg = _flag_value(command, "--load-filaments") or ""
    _expect(
        filament_arg.count(";") == 2,
        "three materials must produce two separators in --load-filaments",
    )
    for filament_path in filament_arg.split(";"):
        _expect(
            Path(filament_path).is_file(),
            "filament path must exist: {}".format(filament_path),
        )
    _expect(
        "Bambu PLA Basic @BBL X1C" in filament_arg,
        "PLA material must select the matching Bambu filament preset",
    )
    preset_colours = [
        json.loads(Path(path).read_text(encoding="utf-8")).get("filament_colour")
        for path in filament_arg.split(";")
    ]
    _expect(
        preset_colours == [["#161616"], ["#FFFFFF"], ["#111111"]],
        "every filament preset must carry its material's colour (purge volumes): {}".format(preset_colours),
    )

    settings = json.loads(
        _read_archive_entry(output, _PROJECT_SETTINGS_PART).decode("utf-8")
    )
    _expect(
        settings["filament_colour"] == ["#161616", "#FFFFFF", "#111111"],
        "colours must be post-processed",
    )
    _expect(
        settings["filament_type"] == ["PLA", "PETG", "ABS"],
        "types must be post-processed",
    )
    _expect(
        settings["filament_density"] == ["1.24", "1.27", "1.04"],
        "densities must be post-processed",
    )
    _expect(
        settings["some_other_key"] == "preserved",
        "unrelated project settings must be preserved",
    )
    expected_names = [
        "Bambu PLA Basic @BBL X1C",
        "Bambu PETG HF @BBL X1C",
        "Bambu ABS @BBL X1C",
    ]
    _expect(
        settings["filament_settings_id"] == expected_names,
        "filament_settings_id must preserve the slicer preset names, got {}".format(
            settings.get("filament_settings_id")
        ),
    )
    for map_key in ("filament_map", "filament_map_2", "filament_nozzle_map", "filament_volume_map"):
        _expect(
            len(settings[map_key]) == len(environment.materials),
            "{} must have one entry per filament, got {}".format(
                map_key, settings.get(map_key)
            ),
        )
    _expect(
        _read_archive_entry(output, "Metadata/thumbnail.png") == b"slicer-thumb",
        "output entries must survive",
    )
    _expect(
        _PROJECT_SETTINGS_PART in _archive_names(base_3mf),
        "the original base 3MF must not be modified",
    )


def _check_prepare_remote(environment: _TestEnvironment) -> None:
    """Remote target: presets are flattened, written from=user and passed to the CLI."""
    records: Dict[str, Any] = {}
    environment.runner.cli_handler = _prepare_command_handler(records)
    printers = list_printers(environment.slicer_exe, "BBL")
    printer = next(
        entry
        for entry in printers
        if entry["name"] == "Bambu Lab X1 Carbon 0.4 nozzle"
    )
    processes = list_processes(
        environment.slicer_exe, "BBL", "Bambu Lab X1 Carbon 0.4 nozzle"
    )
    process = next(
        entry
        for entry in processes
        if entry["name"] == "0.20mm Standard @BBL X1C"
    )
    target = ProfileTarget(
        vendor="BBL",
        printer_name=printer["name"],
        machine_path=printer["machine_path"],
        process_name=process["name"],
        process_path=process["process_path"],
        source="orca_remote",
    )
    base_3mf = _write_fixture_3mf(environment.root / "remote_base.3mf")
    output = environment.root / "remote_out" / "project.3mf"

    prepared = prepare_project(
        base_3mf, output, target, environment.slicer_exe, environment.materials
    )
    _expect(output.is_file(), "remote preparation must write the output 3MF")
    _expect(
        prepared.printer_name == "Bambu Lab X1 Carbon 0.4 nozzle",
        "remote printer name mismatch",
    )

    settings_argument = _flag_value(records["command"], "--load-settings") or ""
    parts = settings_argument.split(";")
    _expect(
        len(parts) == 2,
        "--load-settings must contain flattened machine;process paths",
    )
    for path_text in parts:
        flattened_path = Path(path_text)
        _expect(
            flattened_path.is_file(),
            "flattened preset must exist: {}".format(path_text),
        )
        flattened = json.loads(flattened_path.read_text(encoding="utf-8"))
        _expect("inherits" not in flattened, "flattened preset must not inherit")
        _expect(flattened.get("from") == "user", "flattened preset must be from=user")
    machine_flat = json.loads(Path(parts[0]).read_text(encoding="utf-8"))
    _expect(
        machine_flat.get("machine_max_acceleration_x") == ["1000", "1000"],
        "flattened machine must contain the merged parent keys",
    )
    process_flat = json.loads(Path(parts[1]).read_text(encoding="utf-8"))
    _expect(
        process_flat.get("outer_wall_speed") == ["200"],
        "flattened process parent keys missing",
    )

    post_settings = json.loads(
        _read_archive_entry(output, _PROJECT_SETTINGS_PART).decode("utf-8")
    )
    _expect(
        post_settings["filament_colour"] == ["#161616", "#FFFFFF", "#111111"],
        "remote project colours must be post-processed",
    )


def _check_remote_filament_presets(environment: _TestEnvironment) -> None:
    """A printer from the online profiles gets its vendor's own filament presets."""
    target = ProfileTarget(
        vendor="Prusa", printer_name="Prusa MK4 0.4 nozzle", machine_path="", process_name="",
        process_path="", source="orca_remote",
    )
    candidates = _remote_filament_candidates(target, environment.slicer_exe, ["PLA", "PETG"])
    names = sorted(candidate["name"] for candidate in candidates)
    _expect(
        names == ["Generic PETG @System", "Generic PLA @Prusa MK4"],
        "the MK4's PLA and the library PETG; not the XL's, not a base preset: {}".format(names),
    )
    materials = [
        {"filament_type": "PLA", "display_name": "Prusament - PLA - Galaxy Black"},
        {"filament_type": "PETG", "display_name": "Prusament - PETG - Orange"},
    ]
    paths = _choose_filament_paths(materials, target.printer_name, candidates)
    chosen = [json.loads(Path(item).read_text(encoding="utf-8")) for item in paths]
    _expect(
        [preset["name"] for preset in chosen] == ["Generic PLA @Prusa MK4", "Generic PETG @System"],
        "each material gets the vendor's preset: {}".format([preset["name"] for preset in chosen]),
    )
    brands = [
        {"name": "Prusament PLA @MK4", "path": "prusament.json", "file_name": "prusament.json", "types": {"PLA"},
         "compatible_printers": ("prusa mk4 0.4 nozzle",)},
        {"name": "Generic PLA @Prusa MK4", "path": "generic.json", "file_name": "generic.json", "types": {"PLA"},
         "compatible_printers": ("prusa mk4 0.4 nozzle",)},
    ]
    _expect(
        _pick_filament_candidate(brands, "PLA", target.printer_name, "Bambu PLA Basic") == "generic.json"
        and _pick_filament_candidate(brands, "PLA", target.printer_name, "Prusament PLA") == "prusament.json",
        "another brand's PLA gets the generic preset; Prusament gets Prusament",
    )
    _expect(
        chosen[0].get("nozzle_temperature") == ["215"] and "inherits" not in chosen[0] and chosen[0].get("from") == "user",
        "the preset is flattened with its parent's values: {}".format(chosen[0]),
    )
    unlisted = ProfileTarget(
        vendor="Prusa", printer_name="Prusa CORE One 0.4 nozzle", machine_path="", process_name="",
        process_path="", source="orca_remote",
    )
    _expect(
        [candidate["name"] for candidate in _remote_filament_candidates(unlisted, environment.slicer_exe, ["PLA"])] == [],
        "no preset fits a printer the vendor list does not name",
    )


def _check_prepare_failure(environment: _TestEnvironment) -> None:
    """CLI failures/missing output/empty printer id raise with the log tail."""
    records: Dict[str, Any] = {}
    base_3mf = _write_fixture_3mf(environment.root / "failure_base.3mf")
    output = environment.root / "failure_out" / "project.3mf"
    target = _local_target(environment)

    environment.runner.cli_handler = _prepare_command_handler(
        records, returncode=7, write_output=False
    )
    try:
        prepare_project(
            base_3mf, output, target, environment.slicer_exe, environment.materials
        )
    except SlicerProfilesError as exc:
        message = str(exc)
        _expect(
            "exit code 7" in message,
            "error must include the exit code: {}".format(message),
        )
        _expect(
            "slicer log line 40" in message,
            "error must include the log tail: {}".format(message),
        )
        _expect("--export-3mf" in message, "error must include the command")
    else:
        raise AssertionError("a failing CLI must raise SlicerProfilesError")
    _expect(not output.exists(), "a failed preparation must not leave a project")

    environment.runner.cli_handler = _prepare_command_handler(
        records, returncode=0, write_output=False
    )
    try:
        prepare_project(
            base_3mf, output, target, environment.slicer_exe, environment.materials
        )
    except SlicerProfilesError as exc:
        _expect(
            "did not create the output" in str(exc),
            "missing output error mismatch",
        )
    else:
        raise AssertionError("a missing CLI output must raise SlicerProfilesError")

    environment.runner.cli_handler = _prepare_command_handler(
        records, include_printer_settings_id=False
    )
    try:
        prepare_project(
            base_3mf, output, target, environment.slicer_exe, environment.materials
        )
    except SlicerProfilesError as exc:
        _expect(
            "printer_settings_id" in str(exc),
            "empty printer_settings_id must be rejected",
        )
    else:
        raise AssertionError("an empty printer_settings_id must raise SlicerProfilesError")


def _check_offline_cache(environment: _TestEnvironment) -> None:
    """Remote manifests stay listable from cache when the network goes away."""
    global _url_opener

    def offline_opener(url: str, timeout: float) -> bytes:
        raise RuntimeError("simulated network outage for {}".format(url))

    previous_opener = _url_opener
    _url_opener = offline_opener
    try:
        printers = list_printers(environment.slicer_exe, "BBL")
    finally:
        _url_opener = previous_opener
    _expect(
        [printer["name"] for printer in printers]
        == ["Bambu Lab X1 Carbon 0.4 nozzle", "Bambu Lab A1 0.4 nozzle"],
        "cached manifests must keep listing printers offline",
    )


def _check_filament_fallback(environment: _TestEnvironment) -> None:
    """Filament selection matches types, prefers printer tokens and repeats files."""
    paths = _choose_filament_paths(
        environment.materials, "Bambu Lab X1 Carbon 0.4 nozzle"
    )
    _expect(len(paths) == 3, "one filament path per material expected")
    _expect("PLA" in Path(paths[0]).name, "PLA material must choose a PLA preset")
    _expect("PETG" in Path(paths[1]).name, "PETG material must choose a PETG preset")
    _expect("ABS" in Path(paths[2]).name, "ABS material must choose an ABS preset")
    _expect(
        "X1C" in Path(paths[0]).name,
        "the printer token must be preferred in filament preset names",
    )

    fallback_materials = [
        {
            "color_hex": "#123456",
            "filament_type": "TPU",
            "density_g_cm3": 1.2,
            "display_name": "TPU",
        },
        {
            "color_hex": "#654321",
            "filament_type": "PETG",
            "density_g_cm3": 1.27,
            "display_name": "PETG",
        },
    ]
    fallback_paths = _choose_filament_paths(
        fallback_materials, "Bambu Lab X1 Carbon 0.4 nozzle"
    )
    _expect(
        len(fallback_paths) == 2,
        "fallback selection must return one path per material",
    )
    _expect(
        fallback_paths[0] == fallback_paths[1],
        "an unmatched type must reuse the chosen filament file",
    )


def _expect(condition: bool, message: str) -> None:
    """Raise AssertionError with ``message`` when ``condition`` is false."""
    if not condition:
        raise AssertionError(message)


def _check_app_bundle_resolution(environment: _TestEnvironment) -> None:
    """A macOS .app bundle path resolves to its Contents/MacOS executable."""
    import stat

    with tempfile.TemporaryDirectory(prefix="filamatch_app_bundle_") as temp_dir:
        app_path = Path(temp_dir) / "FakeSlicer.app"
        macos_dir = app_path / "Contents" / "MacOS"
        macos_dir.mkdir(parents=True)
        executable = macos_dir / "FakeSlicer"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        resolved = resolve_slicer_executable(str(app_path))
        _expect(
            Path(resolved) == executable,
            "app bundle must resolve to Contents/MacOS executable, got {!r}".format(resolved),
        )
        _expect(
            resolve_slicer_executable(resolved) == resolved,
            "an already-resolved executable path must be preserved",
        )
        try:
            resolve_slicer_executable(str(app_path / "Contents" / "Missing"))
        except SlicerProfilesError:
            pass
        else:
            raise AssertionError("a missing app bundle must raise SlicerProfilesError")


def _check_fit_wipe_tower(environment: _TestEnvironment) -> None:
    """The prime tower is moved inside the area every nozzle can reach."""
    h2d = {
        "enable_prime_tower": "1",
        "wipe_tower_x": ["15"],
        "wipe_tower_y": ["220"],
        "prime_tower_width": "60",
        "printable_area": ["0x0", "350x0", "350x320", "0x320"],
        "extruder_printable_area": ["0x0,325x0,325x320,0x320", "25x0,350x0,350x320,25x320"],
    }
    _fit_wipe_tower(h2d)
    _expect(h2d["wipe_tower_x"] == ["30"], "x must move inside the shared area: {}".format(h2d))
    _expect(h2d["wipe_tower_y"] == ["220"], "a y already inside must stay")
    single = {
        "enable_prime_tower": "1",
        "wipe_tower_x": ["15"],
        "wipe_tower_y": ["300"],
        "prime_tower_width": "35",
        "printable_area": ["0x0", "256x0", "256x256", "0x256"],
    }
    _fit_wipe_tower(single)
    _expect(single["wipe_tower_x"] == ["15"], "a single-nozzle x inside the bed must stay")
    _expect(single["wipe_tower_y"] == ["210"], "y must be pulled back onto the bed, brim included: {}".format(single))
    # Bambu Lab X2D: rib walls make the tower bigger; at y 216 Bambu Studio refused to slice.
    x2d = dict(single, wipe_tower_y=["216"], prime_tower_brim_width="-1", prime_tower_rib_wall="1",
               prime_tower_rib_width="8", extruder_printable_area=["0x0,256x0,256x256,0x256", "20.5x0,256x0,256x256,20.5x256"])
    _fit_wipe_tower(x2d)
    _expect(x2d["wipe_tower_y"] == ["202"] and x2d["wipe_tower_x"] == ["25.5"],
            "the X2D tower must fit with its rib walls: {}".format(x2d))
    # Prusa XL: the tower is turned 90 degrees and grows to the left of x; at x 15 OrcaSlicer refused.
    xl = {"enable_prime_tower": "1", "wipe_tower_x": ["15"], "wipe_tower_y": ["220"], "prime_tower_width": "60",
          "prime_tower_brim_width": "3", "wipe_tower_rotation_angle": "90",
          "printable_area": ["0x0", "360x0", "360x360", "0x360"]}
    _fit_wipe_tower(xl)
    _expect(xl["wipe_tower_x"] == ["71"] and xl["wipe_tower_y"] == ["220"],
            "a turned tower keeps its size free left of x: {}".format(xl))
    _expect(
        _cli_error_line("loading\nSelected nozzle temperatures are incompatible.\nrun found error, exit\n")
        == "Selected nozzle temperatures are incompatible."
        and _cli_error_line("run found error, exit\nPrime tower is outside.\n") == "Prime tower is outside."
        and _cli_error_line("all fine") == "",
        "the slicer's own reason is read from its output (stdout or stderr first)",
    )
    off = dict(h2d, enable_prime_tower="0", wipe_tower_x=["15"])
    _fit_wipe_tower(off)
    _expect(off["wipe_tower_x"] == ["15"], "a disabled prime tower is left alone")

    abs_gf = {
        "curr_bed_type": "Cool Plate",
        "cool_plate_temp": ["0", "0"],
        "textured_plate_temp": ["90", "90"],
        "hot_plate_temp": ["90", "90"],
    }
    _fit_bed_type(abs_gf)
    _expect(abs_gf["curr_bed_type"] == "Textured PEI Plate", "ABS must leave the Cool Plate")
    pla = {"curr_bed_type": "Cool Plate", "cool_plate_temp": ["35", "35"]}
    _fit_bed_type(pla)
    _expect(pla["curr_bed_type"] == "Cool Plate", "a plate that suits every filament stays")
    mixed = {
        "curr_bed_type": "Cool Plate",
        "cool_plate_temp": ["35", "0"],
        "textured_plate_temp": ["0", "90"],
        "hot_plate_temp": ["65", "90"],
    }
    _fit_bed_type(mixed)
    _expect(mixed["curr_bed_type"] == "High Temp Plate", "the first plate suiting all filaments wins")

    orca_h2d = {
        "nozzle_diameter": ["0.4", "0.4"],
        "nozzle_volume_type": ["Standard"],
        "default_nozzle_volume_type": ["Standard", "High Flow"],
    }
    _fit_nozzle_volume_types(orca_h2d)
    _expect(
        orca_h2d["nozzle_volume_type"] == ["Standard", "High Flow"],
        "each H2D nozzle needs its own flow type: {}".format(orca_h2d),
    )
    no_default = {"nozzle_diameter": ["0.4", "0.4"], "nozzle_volume_type": ["High Flow"]}
    _fit_nozzle_volume_types(no_default)
    _expect(
        no_default["nozzle_volume_type"] == ["High Flow", "High Flow"],
        "without a preset default the last flow type repeats",
    )
    one_nozzle = {"nozzle_diameter": ["0.4"], "nozzle_volume_type": ["Standard"]}
    _fit_nozzle_volume_types(one_nozzle)
    _expect(one_nozzle["nozzle_volume_type"] == ["Standard"], "a single nozzle is left alone")


def _check_prepare_bambu_studio(environment: _TestEnvironment) -> None:
    """Bambu Studio prepares by itself: no --logfile, named presets, output moved, log kept."""
    bambu_exe = environment.root / "BambuStudio"
    bambu_exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    saved_help = environment.runner.help_text
    environment.runner.help_text = "BambuStudio-2.9.1"
    records: Dict[str, Any] = {}
    environment.runner.cli_handler = _prepare_command_handler(records)
    output = environment.root / "bambu_out" / "project.3mf"
    base_3mf = _write_fixture_3mf(environment.root / "bambu_base.3mf")
    try:
        prepared = prepare_project(
            base_3mf, output, _local_target(environment), bambu_exe, environment.materials
        )
        command = records["command"]
        _expect(output.is_file() and prepared.output_path == output, "the project must reach the requested path")
        _expect("--logfile" not in command, "Bambu Studio's CLI has no --logfile: {}".format(command))
        _expect(_flag_value(command, "--outputdir") != str(output.parent), "Bambu Studio exports into the cache")
        _expect(
            not list(Path(_flag_value(command, "--outputdir") or "").glob("*.3mf")),
            "the exported project must be moved out of the cache",
        )
        _expect("slicer stdout" in prepared.log_tail, "Bambu Studio's output is the log: {!r}".format(prepared.log_tail))
        presets = [
            json.loads(Path(path).read_text(encoding="utf-8"))
            for path in (_flag_value(command, "--load-settings") or "").split(";")
            + (_flag_value(command, "--load-filaments") or "").split(";")
        ]
        _expect(
            all(preset.get("inherits") == preset.get("name") and preset.get("from") == "user" for preset in presets),
            "Bambu Studio's presets must name themselves in inherits: {}".format(
                [(preset.get("name"), preset.get("inherits")) for preset in presets]
            ),
        )

        def failing(command: List[str], runner: _FakeRunner) -> _FakeCompletedProcess:
            out_dir = Path(_flag_value(command, "--outputdir") or "")
            (out_dir / "result.json").write_text(
                json.dumps({"error_string": "process not compatible with printer", "return_code": -17})
            )
            return _FakeCompletedProcess(239, "run found error", "")

        environment.runner.cli_handler = failing
        try:
            prepare_project(base_3mf, output, _local_target(environment), bambu_exe, environment.materials)
        except SlicerProfilesError as exc:
            _expect("process not compatible with printer" in str(exc), "result.json must explain: {}".format(exc))
        else:
            raise AssertionError("a failing Bambu Studio run must raise")

        remote = ProfileTarget(
            vendor="Prusa", printer_name="Prusa MK4", machine_path="", process_name="0.20mm", process_path="",
            source="orca_remote",
        )
        try:
            prepare_project(base_3mf, output, remote, bambu_exe, environment.materials)
        except SlicerProfilesError as exc:
            _expect("OrcaSlicer's online profiles" in str(exc), "unexpected error: {}".format(exc))
        else:
            raise AssertionError("Bambu Studio must refuse OrcaSlicer's online profiles")
    finally:
        environment.runner.help_text = saved_help


def _check_bambu_studio_words(environment: _TestEnvironment) -> None:
    """An OrcaSlicer project gets Bambu Studio's words for the settings it words its own way."""
    orca_words = {
        "enable_arc_fitting": "disabled",
        "ensure_vertical_shell_thickness": "ensure_all",
        "ironing_pattern": "rectilinear",
        "support_ironing_pattern": "concentric",
        "sparse_infill_density": "15%",
    }

    def project(name: str, settings: Dict[str, Any]) -> Path:
        path = environment.root / "words" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(str(path), "w") as archive:
            archive.writestr(_PROJECT_SETTINGS_PART, json.dumps(settings))
        return path

    def read(path: Path) -> Dict[str, Any]:
        return json.loads(_read_archive_entry(path, _PROJECT_SETTINGS_PART).decode("utf-8"))

    translated = project("translated.3mf", orca_words)
    _expect(
        use_bambu_studio_words(translated)
        == {"enable_arc_fitting": "0", "ensure_vertical_shell_thickness": "enabled", "ironing_pattern": "zig-zag"},
        "OrcaSlicer's words must be translated: {}".format(read(translated)),
    )
    _expect(read(translated)["sparse_infill_density"] == "15%", "other settings stay")
    _expect(use_bambu_studio_words(translated) == {}, "a second run changes nothing")

    process = _write_profile_json(
        environment.bambu_system / "process" / "0.20mm Words @BBL H2D.json",
        {"name": "0.20mm Words @BBL H2D", "type": "process", "ensure_vertical_shell_thickness": "partial"},
    )
    from_preset = project("preset.3mf", orca_words)
    changed = use_bambu_studio_words(from_preset, process)
    _expect(
        changed.get("ensure_vertical_shell_thickness") == "partial" and changed.get("enable_arc_fitting") == "0",
        "the Bambu Studio preset's own value wins: {}".format(changed),
    )
    process.unlink()
    unknown = project("unknown.3mf", dict(orca_words, ensure_vertical_shell_thickness="ensure_moderate"))
    _expect(
        "ensure_vertical_shell_thickness" not in use_bambu_studio_words(unknown),
        "an unknown OrcaSlicer word is left for Bambu Studio",
    )


def _check_estimate_print(environment: _TestEnvironment) -> None:
    """A copy is sliced for time and grams; two nozzles get nozzle stats; failures raise."""
    project = environment.root / "estimate" / "project.3mf"
    project.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(project), "w") as archive:
        archive.writestr(_PROJECT_SETTINGS_PART, json.dumps({
            "nozzle_diameter": ["0.4", "0.4"], "nozzle_volume_type": ["Standard", "High Flow"], "extruder_nozzle_stats": [],
        }))
    seen: Dict[str, Any] = {}

    def slicing(command: List[str], runner: _FakeRunner) -> _FakeCompletedProcess:
        out_dir = Path(_flag_value(command, "--outputdir") or "")
        with zipfile.ZipFile(command[-1], "r") as archive:
            seen["stats"] = json.loads(archive.read(_PROJECT_SETTINGS_PART))["extruder_nozzle_stats"]
        with zipfile.ZipFile(str(out_dir / "sliced.3mf"), "w") as archive:
            archive.writestr("Metadata/slice_info.config", (
                '<config><plate><metadata key="prediction" value="2563"/><metadata key="weight" value="6.99"/>'
                '<filament id="1" used_g="2.71"/><filament id="3" used_g="4.28"/></plate></config>'
            ))
        return _FakeCompletedProcess(0)

    environment.runner.cli_handler = slicing
    estimate = estimate_print(project, environment.slicer_exe)
    _expect(
        estimate == {"minutes": 43, "grams": 7.0, "filament_grams": [2.7, 0.0, 4.3]},
        "time, total and grams per slot (unused slot 2 is 0): {}".format(estimate),
    )
    _expect(seen.get("stats") == ["Standard#1", "High Flow#1"], "the copy names each nozzle: {}".format(seen))
    _expect(
        json.loads(_read_archive_entry(project, _PROJECT_SETTINGS_PART))["extruder_nozzle_stats"] == [],
        "the project itself is not changed",
    )
    # Nozzle racks: the H2C's right extruder holds 6 hotends, the H3C's one extruder 9.
    h2c = {"nozzle_diameter": ["0.4", "0.4"], "nozzle_volume_type": ["Standard", "Standard"],
           "extruder_max_nozzle_count": ["1", "6"]}
    h3c = {"nozzle_diameter": ["0.4"], "nozzle_volume_type": ["Standard"], "extruder_max_nozzle_count": ["9"]}
    x1c = {"nozzle_diameter": ["0.4"], "nozzle_volume_type": ["Standard"], "extruder_max_nozzle_count": ["1"]}
    _expect(
        _nozzle_stats(h2c) == ["Standard#1", "Standard#6"] and _nozzle_stats(h3c) == ["Standard#9"]
        and _nozzle_stats(x1c) == [] and _nozzle_stats({"nozzle_diameter": ["0.4", "0.4"]}) == [],
        "rack printers count every hotend; one nozzle or unknown flow types get no stats",
    )

    def failing(command: List[str], runner: _FakeRunner) -> _FakeCompletedProcess:
        out_dir = Path(_flag_value(command, "--outputdir") or "")
        (out_dir / "result.json").write_text(json.dumps({"error_string": "Failed slicing the model."}))
        return _FakeCompletedProcess(156)

    environment.runner.cli_handler = failing
    try:
        estimate_print(project, environment.slicer_exe)
    except SlicerProfilesError as exc:
        _expect("Failed slicing the model" in str(exc), "the error must explain: {}".format(exc))
    else:
        raise AssertionError("a failed slice must raise")

    crashes = {"count": 0}

    def crashing_twice(command: List[str], runner: _FakeRunner) -> _FakeCompletedProcess:
        crashes["count"] += 1
        return _FakeCompletedProcess(-10) if crashes["count"] <= 2 else slicing(command, runner)

    environment.runner.cli_handler = crashing_twice
    _expect(estimate_print(project, environment.slicer_exe)["minutes"] == 43 and crashes["count"] == 3,
            "a crashed slice is run again: {}".format(crashes))
    environment.runner.cli_handler = lambda command, runner: _FakeCompletedProcess(-10)
    try:
        estimate_print(project, environment.slicer_exe)
    except SlicerProfilesError as exc:
        _expect("the slicer crashed (signal 10)" in str(exc), "a crash is named: {}".format(exc))
    else:
        raise AssertionError("a slicer that always crashes must raise")
    _expect(not list((environment.cache_root).glob("estimate_*")), "the temporary folder is removed")


def _check_filament_types(environment: _TestEnvironment) -> None:
    """A preset's exact type is kept when it is a variant of the material's type."""
    _expect(_is_type_variant("ABS-GF", "ABS") and _is_type_variant("PLA", "pla"), "ABS-GF is an ABS; PLA is PLA")
    _expect(not _is_type_variant("PLA", "PETG") and not _is_type_variant("ABSX", "ABS"), "a stand-in preset is not a variant")
    project = environment.root / "types" / "project.3mf"
    _write_prepared_3mf(project)
    materials = [
        {"color_hex": "#000000", "filament_type": "ABS", "display_name": "ABS-GF Black"},
        {"color_hex": "#FFFFFF", "filament_type": "PETG", "display_name": "PETG White"},
    ]
    settings = _postprocess_project_settings(project, materials, preset_types=["ABS-GF", "PLA"])
    _expect(
        settings["filament_type"] == ["ABS-GF", "PETG"],
        "the preset's ABS-GF is kept, a PLA stand-in for PETG is not: {}".format(settings["filament_type"]),
    )


def _check_slicer_names(environment: _TestEnvironment) -> None:
    """Slicer kind and display name come from the executable or app path."""
    cases = (
        ("/Applications/BambuStudio.app/Contents/MacOS/BambuStudio", "bambu", "Bambu Studio"),
        ("/Applications/OrcaSlicer.app", "orca", "OrcaSlicer"),
        ("C:\\Program Files\\OrcaSlicer\\orca-slicer.exe", "orca", "OrcaSlicer"),
        ("/Applications/Original Prusa Drivers/PrusaSlicer.app/Contents/MacOS/PrusaSlicer", "", "PrusaSlicer"),
        ("C:\\Tools\\SuperSlicer\\superslicer.exe", "", "superslicer"),
        ("", "", "the slicer"),
    )
    for path, kind, name in cases:
        _expect(slicer_kind(path) == kind, "kind of {!r}: {!r}".format(path, slicer_kind(path)))
        _expect(slicer_name(path) == name, "name of {!r}: {!r}".format(path, slicer_name(path)))


def _check_nozzle_map(environment: _TestEnvironment) -> None:
    """Each filament is pinned to the nozzle its slot feeds (two-nozzle printers only)."""
    plates = (
        '<plate><metadata key="plater_id" value="1"/>'
        '<metadata key="filament_map_mode" value="Auto For Flush"/></plate>'
        '<plate><metadata key="plater_id" value="2"/></plate>'
    )

    def project(name: str, diameters: List[str]) -> Path:
        path = environment.root / "nozzles" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        settings = {
            "nozzle_diameter": diameters,
            "filament_colour": ["#000000", "#0ACC38"],
            "filament_map": ["1", "1"],
            "filament_map_mode": "Auto For Flush",
        }
        with zipfile.ZipFile(str(path), "w") as archive:
            archive.writestr(_PROJECT_SETTINGS_PART, json.dumps(settings))
            archive.writestr(_MODEL_SETTINGS_PART, "<config>{}</config>".format(plates))
        return path

    def read(path: Path) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
        with zipfile.ZipFile(str(path), "r") as archive:
            settings = json.loads(archive.read(_PROJECT_SETTINGS_PART).decode("utf-8"))
            root = ET.fromstring(archive.read(_MODEL_SETTINGS_PART))
        return settings, [
            {meta.get("key"): meta.get("value") for meta in plate.findall("metadata")}
            for plate in root.findall("plate")
        ]

    h2d = project("h2d.3mf", ["0.4", "0.4"])
    _expect(apply_nozzle_map(h2d, [NOZZLE_RIGHT, NOZZLE_RIGHT]), "both AMS 2 slots feed the right nozzle")
    settings, plate_values = read(h2d)
    _expect(
        settings["filament_map"] == ["2", "2"] and settings["filament_map_mode"] == "Manual",
        "the project must pin both filaments to nozzle 2: {}".format(settings),
    )
    _expect(
        all(plate.get("filament_map_mode") == "Manual" and plate.get("filament_maps") == "2 2" for plate in plate_values),
        "every plate must use the manual map: {}".format(plate_values),
    )
    mixed = project("mixed.3mf", ["0.4", "0.4"])
    _expect(apply_nozzle_map(mixed, [NOZZLE_LEFT, NOZZLE_RIGHT]), "a left and a right slot")
    _expect(read(mixed)[0]["filament_map"] == ["1", "2"], "left is 1 and right is 2")
    _expect(
        _filament_map_values({"physical_extruder_map": ["1", "0"]}) == {NOZZLE_LEFT: "1", NOZZLE_RIGHT: "2"}
        and _filament_map_values({"physical_extruder_map": ["0", "1"]}) == {NOZZLE_LEFT: "2", NOZZLE_RIGHT: "1"}
        and _filament_map_values({}) == {NOZZLE_LEFT: "1", NOZZLE_RIGHT: "2"},
        "the side numbers follow physical_extruder_map",
    )
    # Bambu Lab H2C: no TPU on the left extruder, so a TPU slot on the left is not pinned.
    tpu = project("h2c_tpu.3mf", ["0.4", "0.4"])
    with zipfile.ZipFile(str(tpu), "r") as archive:
        tpu_settings = json.loads(archive.read(_PROJECT_SETTINGS_PART))
    tpu_settings.update(filament_type=["PLA", "TPU"], unprintable_filament_types=["TPU", "PPS-CF,PPA-CF"])
    rewrite_zip_entries(tpu, {_PROJECT_SETTINGS_PART: json.dumps(tpu_settings).encode("utf-8")})
    _expect(not apply_nozzle_map(tpu, [NOZZLE_RIGHT, NOZZLE_LEFT]), "TPU on the H2C's left nozzle is refused")
    _expect(read(tpu)[0]["filament_map_mode"] == "Auto For Flush", "a refused map leaves the project as it was")
    _expect(apply_nozzle_map(tpu, [NOZZLE_LEFT, NOZZLE_RIGHT]), "TPU on the right nozzle is fine")
    _expect(
        orca_supports_printer("Bambu Lab H2C 0.4 nozzle") and not orca_supports_printer("Bambu Lab H3C 0.4 nozzle"),
        "OrcaSlicer 2.5 has no H3C",
    )

    unknown = project("unknown.3mf", ["0.4", "0.4"])
    _expect(not apply_nozzle_map(unknown, [NOZZLE_RIGHT, None]), "an unknown nozzle leaves the choice to the slicer")
    _expect(read(unknown)[0]["filament_map_mode"] == "Auto For Flush", "the project must stay unchanged")
    one_nozzle = project("x1c.3mf", ["0.4"])
    _expect(not apply_nozzle_map(one_nozzle, [NOZZLE_LEFT, NOZZLE_LEFT]), "a one-nozzle printer has no nozzle map")
    try:
        apply_nozzle_map(project("count.3mf", ["0.4", "0.4"]), [NOZZLE_RIGHT])
    except SlicerProfilesError as exc:
        _expect("2 filament(s) but 1 nozzle(s)" in str(exc), "unexpected error: {}".format(exc))
    else:
        raise AssertionError("a nozzle list of the wrong length must be refused")


def _check_standalone_bambu_presets(environment: _TestEnvironment) -> None:
    """Bambu Studio system presets reach the CLI merged with their parents."""
    filament_dir = environment.bambu_system / "filament"
    _write_profile_json(
        filament_dir / "fdm_filament_abs.json",
        {"name": "fdm_filament_abs", "type": "filament", "filament_density": ["1.04"],
         "nozzle_temperature": ["250"], "inherits": "fdm_filament_common"},
    )
    _write_profile_json(
        filament_dir / "fdm_filament_common.json",
        {"name": "fdm_filament_common", "type": "filament", "filament_diameter": ["1.75"],
         "nozzle_temperature": ["200"]},
    )
    _write_profile_json(
        filament_dir / "dual_template.json",
        {"name": "fdm_filament_template_direct_dual", "type": "filament", "filament_extruder_variant": ["A", "B"]},
    )
    leaf = _write_profile_json(
        filament_dir / "Bambu ABS-GF @BBL H2D.json",
        {"name": "Bambu ABS-GF @BBL H2D", "type": "filament", "from": "system",
         "inherits": "fdm_filament_abs", "include": ["fdm_filament_template_direct_dual"],
         "nozzle_temperature": ["270"]},
    )
    standalone = Path(_standalone_bambu_preset(leaf))
    _expect(standalone != leaf and standalone.is_file(), "an inheriting system preset must be copied")
    data = json.loads(standalone.read_text(encoding="utf-8"))
    _expect(
        data.get("filament_diameter") == ["1.75"]
        and data.get("filament_density") == ["1.04"]
        and data.get("filament_extruder_variant") == ["A", "B"]
        and data.get("nozzle_temperature") == ["270"],
        "parents, includes and the preset's own keys must merge in that order: {}".format(data),
    )
    _expect(
        data.get("from") == "user" and "inherits" not in data and "include" not in data,
        "the copy must stand alone: {}".format(data),
    )
    for_bambu = json.loads(Path(_standalone_bambu_preset(leaf, for_bambu_studio=True)).read_text(encoding="utf-8"))
    _expect(
        for_bambu.get("inherits") == "Bambu ABS-GF @BBL H2D"
        and for_bambu.get("from") == "user"
        and for_bambu.get("filament_diameter") == ["1.75"],
        "Bambu Studio's merged copy must name itself in inherits: {}".format(for_bambu),
    )
    plain = filament_dir / "Bambu PLA Basic @BBL X1C.json"
    plain_copy = json.loads(Path(_standalone_bambu_preset(plain)).read_text(encoding="utf-8"))
    _expect(
        plain_copy.get("name") == "Bambu PLA Basic @BBL X1C" and plain_copy.get("from") == "user",
        "a system preset without parents is still copied as a user preset: {}".format(plain_copy),
    )
    elsewhere = _write_profile_json(
        environment.root / "imported" / "mine.json",
        {"name": "Mine", "type": "filament", "inherits": "Somewhere Else"},
    )
    _expect(_standalone_bambu_preset(elsewhere) == str(elsewhere), "presets outside Bambu Studio are untouched")
    broken = _write_profile_json(
        filament_dir / "broken.json", {"name": "Broken", "type": "filament", "inherits": "Missing Parent"}
    )
    try:
        _standalone_bambu_preset(broken)
    except SlicerProfilesError as exc:
        _expect("Missing Parent" in str(exc), "the error must name the missing parent: {}".format(exc))
    else:
        raise AssertionError("a missing parent must be reported")
    for path in (filament_dir / "fdm_filament_abs.json", filament_dir / "fdm_filament_common.json",
                 filament_dir / "dual_template.json", leaf, broken):
        path.unlink()


def _slot_fixture(
    path: Path, assignments: str, colours: Optional[List[str]] = None
) -> Path:
    """Write a 3MF whose model_settings holds ``assignments`` (and ``colours``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(path), "w") as archive:
        archive.writestr(_MODEL_SETTINGS_PART, "<config>{}</config>".format(assignments))
        if colours is not None:
            archive.writestr(
                _PROJECT_SETTINGS_PART, json.dumps({"filament_colour": colours}).encode("utf-8")
            )
    return path


def _check_slot_assignments(environment: _TestEnvironment) -> None:
    """Every body must print with the slot and colour Fusion gave it."""

    def part(name: str, slot: Optional[int]) -> str:
        extruder = '<metadata key="extruder" value="{}"/>'.format(slot) if slot else ""
        return '<part id="1"><metadata key="name" value="{}"/>{}</part>'.format(name, extruder)

    # Two components: the main body is black ABS-GF (slot 1), the TEF part is green (slot 2).
    fusion = [
        {"display_name": "ABS-GF Black", "color_hex": "#000000", "slot": 1, "body_names": ("Body1",)},
        {"display_name": "PolyLite Green", "color_hex": "#0acc38", "slot": 2, "body_names": ("Body1 (TEF)",)},
    ]
    assembled = '<object id="3"><metadata key="name" value="Assembly"/>{}{}</object>'.format(
        part("Body1 (TEF)", 2), part("Body1", 1)
    )
    good = _slot_fixture(environment.root / "slots" / "good.3mf", assembled, ["#000000", "#0ACC38"])
    _expect(check_slot_assignments(good, fusion) == 2, "both bodies must be checked")

    swapped_colours = _slot_fixture(
        environment.root / "slots" / "colours.3mf", assembled, ["#0ACC38", "#000000"]
    )
    try:
        check_slot_assignments(swapped_colours, fusion)
    except SlicerProfilesError as exc:
        _expect("slot 1 is #0ACC38" in str(exc), "the colour error must name the slot: {}".format(exc))
    else:
        raise AssertionError("swapped slot colours must be refused")

    # The wrong test files: TEF on slot 2, but slot 2 held the black filament.
    wrong_order = [
        dict(fusion[1], slot=1, body_names=("Body1 (TEF)",)),
        dict(fusion[0], slot=2, body_names=("Body1",)),
    ]
    try:
        check_slot_assignments(good, wrong_order)
    except SlicerProfilesError as exc:
        _expect("'Body1 (TEF)' prints with slot 2" in str(exc), "body error must name the body: {}".format(exc))
    else:
        raise AssertionError("a body on the wrong slot must be refused")

    plain = _slot_fixture(
        environment.root / "slots" / "plain.3mf",
        '<object id="1"><metadata key="name" value="Body1 (TEF)"/>'
        '<metadata key="extruder" value="2"/></object>'
        '<object id="2"><metadata key="name" value="Body1"/>{}</object>'.format(part("Body1", None)),
    )
    _expect(
        check_slot_assignments(plain, fusion) == 2,
        "a plain 3MF is checked by extruder only; a part without one uses its object's slot",
    )
    renamed = _slot_fixture(environment.root / "slots" / "renamed.3mf", part("Shape 1", 1))
    try:
        check_slot_assignments(renamed, fusion)
    except SlicerProfilesError as exc:
        _expect("none of the Fusion bodies" in str(exc), "unexpected error: {}".format(exc))
    else:
        raise AssertionError("a 3MF without any Fusion body must be refused")
    _expect(
        check_slot_assignments(good, [{"color_hex": "#000000"}, {"color_hex": "#0ACC38"}]) == 0,
        "materials without body names only check the colours",
    )

    records: Dict[str, Any] = {}
    environment.runner.cli_handler = _prepare_command_handler(
        records,
        model_settings="<config>{}</config>".format(
            assembled.replace('value="2"', 'value="3"')
        ).encode("utf-8"),
    )
    materials = [
        dict(environment.materials[0], body_names=("Body1",)),
        dict(environment.materials[1], body_names=("Body1 (TEF)",)),
    ]
    try:
        prepare_project(
            _write_fixture_3mf(environment.root / "slots" / "base.3mf"),
            environment.root / "slots" / "out" / "project.3mf",
            _local_target(environment),
            environment.slicer_exe,
            materials,
        )
    except SlicerProfilesError as exc:
        _expect("'Body1 (TEF)' prints with slot 3" in str(exc), "unexpected error: {}".format(exc))
    else:
        raise AssertionError("prepare_project must refuse a slicer output with moved slots")


def _check_layout_flags(environment: _TestEnvironment) -> None:
    """Touching bodies are assembled; separate bodies may share a plate."""

    def write_model(path: Path, boxes: Sequence[Tuple[float, ...]], offset: str = "") -> Path:
        objects = []
        items = []
        for index, (x0, y0, z0, x1, y1, z1) in enumerate(boxes, start=1):
            vertices = "".join(
                '<vertex x="{}" y="{}" z="{}"/>'.format(x, y, z)
                for x in (x0, x1)
                for y in (y0, y1)
                for z in (z0, z1)
            )
            objects.append(
                '<object id="{}" type="model"><mesh><vertices>{}</vertices>'
                "<triangles/></mesh></object>".format(index, vertices)
            )
            transform = ' transform="1 0 0 0 1 0 0 0 1 {}"'.format(offset) if offset and index == 2 else ""
            items.append('<item objectid="{}"{}/>'.format(index, transform))
        model = (
            '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            "<resources>{}</resources><build>{}</build></model>".format("".join(objects), "".join(items))
        )
        with zipfile.ZipFile(str(path), "w") as archive:
            archive.writestr("3D/3dmodel.model", model)
        return path

    with tempfile.TemporaryDirectory(prefix="filamatch_layout_") as folder:
        root = Path(folder)
        plate = (-18.0, -18.0, 0.0, 18.0, 18.0, 3.0)
        boss = (-13.0, -13.0, 3.0, 13.0, 13.0, 8.0)
        far = (40.0, 40.0, 0.0, 50.0, 50.0, 5.0)
        _expect(
            _layout_flags(write_model(root / "touch.3mf", [plate, boss])) == ["--assemble"],
            "stacked bodies must be assembled into one multi-colour object",
        )
        _expect(
            _layout_flags(write_model(root / "apart.3mf", [plate, far]))
            == ["--allow-multicolor-oneplate"],
            "separate bodies must be arranged with several colours on one plate",
        )
        _expect(
            _layout_flags(write_model(root / "mixed.3mf", [plate, boss, far]))
            == ["--allow-multicolor-oneplate"],
            "bodies that do not all touch must not be merged into one object",
        )
        _expect(
            _layout_flags(write_model(root / "moved.3mf", [plate, plate], "100 0 0"))
            == ["--allow-multicolor-oneplate"],
            "item transforms must be applied before checking contact",
        )
        _expect(
            _layout_flags(write_model(root / "single.3mf", [plate])) == [],
            "a single body needs no layout flag",
        )
        _expect(_layout_flags(root / "missing.3mf") == [], "an unreadable file needs no flag")


def _check_network_failfast(environment: _TestEnvironment) -> None:
    """Listings use a short timeout, stop at the first connection failure and
    remember missing files/tags for the session."""
    global _CACHE_ROOT_OVERRIDE, _url_opener
    calls: List[Tuple[str, float]] = []

    def offline_opener(url: str, timeout: float) -> bytes:
        calls.append((url, timeout))
        raise urllib.error.URLError("simulated: no route to host")

    previous_root, previous_opener = _CACHE_ROOT_OVERRIDE, _url_opener
    _CACHE_ROOT_OVERRIDE = environment.root / "empty_cache"
    _url_opener = offline_opener
    _reset_remote_state()
    try:
        vendors = list_vendors(environment.slicer_exe)
    finally:
        _CACHE_ROOT_OVERRIDE, _url_opener = previous_root, previous_opener
    _expect(len(calls) == 1, "only one request may run after a connection failure, got {}".format(len(calls)))
    _expect(calls[0][1] == LIST_NETWORK_TIMEOUT_SECONDS, "listings must use the short timeout")
    _expect(
        not any(vendor["has_printers"] for vendor in vendors if vendor["source"] == "orca_remote"),
        "offline remote vendors must be listed as unavailable",
    )

    # A 404 is cached per URL: the second listing does not ask for the tag again.
    _reset_remote_state()
    _GOOD_REFS.add("v2.5.0-rc")
    tag_url = environment._remote_url("v2.5.0-rc", "Prusa.json")
    before = environment.opener_calls.count(tag_url)
    list_printers(environment.slicer_exe, "Prusa")
    list_printers(environment.slicer_exe, "Prusa")
    _expect(
        environment.opener_calls.count(tag_url) - before == 1,
        "a missing file must only be requested once per session",
    )

    # A tag that does not exist is skipped after the first 404.
    _reset_remote_state()
    _VERSION_CACHE.clear()
    environment.runner.help_text = "OrcaSlicer-9.9.9"
    try:
        list_vendors(environment.slicer_exe)
        missing_tag_calls = [url for url in environment.opener_calls if "/v9.9.9/" in url]
        _expect(len(missing_tag_calls) == 1, "a missing tag must only be tried once, got {}".format(missing_tag_calls))
        _expect(_candidate_refs(environment.slicer_exe) == ["main"], "a missing tag must be skipped")

        # Bambu Studio versions never have an OrcaSlicer tag.
        _VERSION_CACHE.clear()
        environment.runner.help_text = "BambuStudio-02.07.01.62"
        _expect(_candidate_refs(environment.slicer_exe) == ["main"], "Bambu Studio must not try a tag")
    finally:
        environment.runner.help_text = "OrcaSlicer-2.5.0-rc"
        _VERSION_CACHE.clear()
        _reset_remote_state()


def _check_version_cache(environment: _TestEnvironment) -> None:
    """A failed --help probe is not cached; a good one is keyed by path + mtime."""
    global _command_runner

    def failing_runner(command: Sequence[str], timeout: float, cwd: Optional[str] = None) -> Any:
        raise subprocess.TimeoutExpired(list(command), timeout)

    _VERSION_CACHE.clear()
    previous = _command_runner
    _command_runner = failing_runner
    try:
        _expect(_slicer_version(environment.slicer_exe) == "", "a failed probe has no version")
    finally:
        _command_runner = previous
    _expect(not _VERSION_CACHE, "a failed probe must not be cached")
    _expect(_slicer_version(environment.slicer_exe) == "2.5.0-rc", "the probe must be retried")
    keys = list(_VERSION_CACHE)
    _expect(
        len(keys) == 1 and keys[0][0] == str(environment.slicer_exe) and keys[0][1] is not None,
        "the cache key must be (path, mtime), got {}".format(keys),
    )


def _check_path_safety(environment: _TestEnvironment) -> None:
    """Remote paths/refs cannot escape the cache and downloads are size-capped."""
    for unsafe in ("../x.json", "BBL/../../x.json", "D:/x.json", "BBL/a:b.json", "BBL/a\x00.json"):
        try:
            _remote_cache_path("main", unsafe)
        except SlicerProfilesError:
            continue
        raise AssertionError("unsafe remote path {!r} must be rejected".format(unsafe))
    for unsafe_ref in ("..", "v1/../..", "C:", ""):
        try:
            _remote_cache_path(unsafe_ref, "BBL.json")
        except SlicerProfilesError:
            continue
        raise AssertionError("unsafe ref {!r} must be rejected".format(unsafe_ref))
    _expect(
        _remote_cache_path("main", "/BBL/machine/a b.json").parts[-3:] == ("BBL", "machine", "a b.json"),
        "a leading slash is made relative",
    )

    class _HugeResponse:
        def __enter__(self) -> "_HugeResponse":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return b"x" * (size if size >= 0 else MAX_DOWNLOAD_BYTES * 2)

    previous_urlopen = urllib.request.urlopen
    urllib.request.urlopen = lambda *_args, **_kwargs: _HugeResponse()  # type: ignore[assignment]
    try:
        _default_url_opener("https://example.invalid/huge.json", 1)
    except SlicerProfilesError as exc:
        _expect("larger than" in str(exc), "the size cap error must say why")
    else:
        raise AssertionError("an oversized download must be refused")
    finally:
        urllib.request.urlopen = previous_urlopen  # type: ignore[assignment]


def _check_windows_executable_filter(environment: _TestEnvironment) -> None:
    """On Windows only .exe files count as launchable (not OrcaSlicer.dll)."""
    folder = environment.root / "win_install"
    folder.mkdir(exist_ok=True)
    dll = folder / "OrcaSlicer.dll"
    exe = folder / "orca-slicer.exe"
    dll.write_bytes(b"MZ")
    exe.write_bytes(b"MZ")
    _expect(not _is_launchable_file(dll, windows=True), "a DLL must not be launchable on Windows")
    _expect(_is_launchable_file(exe, windows=True), "an .exe must be launchable on Windows")
    _expect(not _is_launchable_file(folder, windows=True), "a folder is never launchable")


def _check_printer_compatibility(environment: _TestEnvironment) -> None:
    """compatible_printers wins; names are matched by whole tokens."""
    _expect(_printer_token("Bambu Lab A1 0.4 nozzle") == "A1", "A1 must not become A1M")
    _expect(_printer_token("Bambu Lab A1 mini 0.4 nozzle") == "A1M", "A1 mini maps to A1M")
    _expect(_printer_token("Bambu Lab X1 Carbon 0.4 nozzle") == "X1C", "X1 Carbon maps to X1C")
    _expect(_printer_token("Bambu Lab H2D Pro 0.4 nozzle") == "H2DP", "H2D Pro maps to H2DP")
    _expect(_printer_token("Prusa MK4S 0.4 nozzle") == "", "MK4 must not match MK4S")

    processes = [
        {"name": "0.20mm Standard @BBL A1", "process_path": "a1", "default": False},
        {"name": "0.20mm Standard @BBL A1M", "process_path": "a1m", "default": False},
    ]
    a1 = [row["name"] for row in _filter_compatible_processes(processes, "Bambu Lab A1 0.4 nozzle")]
    _expect(a1 == ["0.20mm Standard @BBL A1"], "A1 must not list A1M processes, got {}".format(a1))
    mini = [row["name"] for row in _filter_compatible_processes(processes, "Bambu Lab A1 mini 0.4 nozzle")]
    _expect(mini == ["0.20mm Standard @BBL A1M"], "A1 mini must list A1M processes, got {}".format(mini))

    listed = [
        {
            "name": "0.20mm Standard @BBL X1C",
            "process_path": "x1c",
            "default": False,
            "compatible_printers": ["Bambu Lab X1 Carbon 0.4 nozzle", "Bambu Lab P1S 0.4 nozzle"],
        },
        {
            "name": "0.20mm Standard @BBL P1P",
            "process_path": "p1p",
            "default": False,
            "compatible_printers": ["Bambu Lab P1P 0.4 nozzle"],
        },
    ]
    p1s = _filter_compatible_processes(listed, "Bambu Lab P1S 0.4 nozzle")
    _expect([row["name"] for row in p1s] == ["0.20mm Standard @BBL X1C"], "compatible_printers must decide")
    _expect("compatible_printers" not in p1s[0], "the internal list must not leak into the rows")

    candidates = [
        {
            "path": "listed.json",
            "name": "Bambu PLA Basic @BBL X1C",
            "file_name": "listed.json",
            "types": {"PLA"},
            "compatible_printers": ("bambu lab x1 carbon 0.4 nozzle", "bambu lab p1s 0.4 nozzle"),
        },
        {
            "path": "a1m.json",
            "name": "Bambu PLA Basic @BBL A1M",
            "file_name": "a1m.json",
            "types": {"PLA"},
            "compatible_printers": (),
        },
    ]
    _expect(
        _pick_filament_candidate(candidates, "PLA", "Bambu Lab P1S 0.4 nozzle") == "listed.json",
        "a filament listing the printer must be chosen",
    )
    _expect(
        _pick_filament_candidate(candidates, "PLA", "Bambu Lab A1 0.4 nozzle") is None,
        "an A1M filament must not be picked for an A1",
    )


def _check_zip_rewrite_and_log(environment: _TestEnvironment) -> None:
    """One archive rewrite replaces/adds/drops entries and keeps metadata;
    the slicer log lives in the cache and is truncated before each run."""
    archive_path = environment.root / "rewrite.3mf"
    with zipfile.ZipFile(str(archive_path), "w") as archive:
        info = zipfile.ZipInfo("3D/3dmodel.model", (2024, 1, 2, 3, 4, 6))
        info.internal_attr = 1
        info.external_attr = 0o600 << 16
        archive.writestr(info, b"<model/>")
        archive.writestr(_PROJECT_SETTINGS_PART, b"{}")
        archive.writestr("Metadata/legacy.json", b"old")
    rewrite_zip_entries(
        archive_path,
        {_PROJECT_SETTINGS_PART: b'{"a": 1}', "Metadata/legacy.json": None, "Metadata/new.json": b"new"},
        required=(_PROJECT_SETTINGS_PART,),
    )
    with zipfile.ZipFile(str(archive_path)) as archive:
        names = archive.namelist()
        model_info = archive.getinfo("3D/3dmodel.model")
        _expect(
            names == ["3D/3dmodel.model", _PROJECT_SETTINGS_PART, "Metadata/new.json"],
            "rewrite must replace in place, drop and append, got {}".format(names),
        )
        _expect(model_info.internal_attr == 1, "internal_attr must be preserved")
        _expect(model_info.external_attr == 0o600 << 16, "external_attr must be preserved")
        _expect(model_info.date_time == (2024, 1, 2, 3, 4, 6), "date_time must be preserved")
        _expect(archive.read(_PROJECT_SETTINGS_PART) == b'{"a": 1}', "replacement payload mismatch")
    try:
        rewrite_zip_entries(archive_path, {"missing": b""}, required=("missing",))
    except SlicerProfilesError:
        pass
    else:
        raise AssertionError("a missing required entry must raise")
    _expect(
        not list(environment.root.glob(".filamatch_zip_*")),
        "temporary rewrite files must be cleaned up",
    )

    global _command_runner
    log_path = _cache_root() / "logs" / "slicer_cli.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("OLD LOG FROM AN EARLIER RUN\n", encoding="utf-8")
    base_3mf = _write_fixture_3mf(environment.root / "log_base.3mf")

    def cannot_start(command: Sequence[str], timeout: float, cwd: Optional[str] = None) -> Any:
        if len(command) >= 2 and command[1] == "--help":
            return _FakeCompletedProcess(0, "OrcaSlicer-2.5.0-rc\n", "")
        raise OSError("simulated: cannot start")

    previous = _command_runner
    _command_runner = cannot_start
    try:
        prepare_project(
            base_3mf,
            environment.root / "log_out" / "project.3mf",
            _local_target(environment),
            environment.slicer_exe,
            environment.materials,
        )
    except SlicerProfilesError as exc:
        _expect("OLD LOG" not in str(exc), "an old slicer log must never be shown")
        _expect(str(log_path) in str(exc), "the default log must live in the add-in cache")
    else:
        raise AssertionError("a slicer that cannot start must raise")
    finally:
        _command_runner = previous
    _expect(not (environment.root / "log_out" / "slicer_cli.log").exists(), "no log in the output folder")


def _selftest() -> int:
    """Run the slicer-profiles self-test with fake CLI/network/filesystem hooks.

    Returns:
        Process exit code: 0 when every check passed, 1 otherwise.
    """
    scenarios = (
        (
            "find_installed_slicers parses versions from --help",
            _check_find_installed_slicers,
        ),
        (
            "local vendor/printer/process discovery filters base presets",
            _check_local_discovery,
        ),
        (
            "macOS .app bundles resolve to their executable",
            _check_app_bundle_resolution,
        ),
        (
            "remote listings and inheritance flattening work",
            _check_remote_listing_and_flattening,
        ),
        (
            "unresolved inheritance parents raise with the chain",
            _check_unresolved_parent_error,
        ),
        (
            "imported profiles import, validate and deduplicate",
            _check_imported_profiles,
        ),
        (
            "prepare_project local target builds the verified CLI command",
            _check_prepare_local,
        ),
        (
            "prepare_project remote target uses flattened user presets",
            _check_prepare_remote,
        ),
        (
            "CLI failures and bad projects raise with the log tail",
            _check_prepare_failure,
        ),
        ("cached remote manifests keep working offline", _check_offline_cache),
        ("filament selection matches types and falls back", _check_filament_fallback),
        ("touching bodies are assembled, separate bodies share a plate", _check_layout_flags),
        (
            "the prime tower, build plate and nozzle flow types are fitted to the printer",
            _check_fit_wipe_tower,
        ),
        ("every body prints with the slot and colour Fusion gave it", _check_slot_assignments),
        ("Bambu Studio presets reach the CLI merged with their parents", _check_standalone_bambu_presets),
        ("each filament is pinned to the nozzle its slot feeds", _check_nozzle_map),
        ("slicer kind and name come from the app path", _check_slicer_names),
        ("a preset's exact filament type (ABS-GF) is kept", _check_filament_types),
        ("a copy is sliced for the print time and grams", _check_estimate_print),
        ("online printers get their vendor's own filament presets", _check_remote_filament_presets),
        ("projects sent to Bambu Studio use Bambu Studio's words", _check_bambu_studio_words),
        ("Bambu Studio prepares projects by itself", _check_prepare_bambu_studio),
        ("listings fail fast offline and cache missing files/tags", _check_network_failfast),
        ("failed version probes are not cached", _check_version_cache),
        ("remote paths stay in the cache and downloads are capped", _check_path_safety),
        ("Windows install folders only launch .exe files", _check_windows_executable_filter),
        ("printer compatibility uses lists and whole tokens", _check_printer_compatibility),
        ("one zip rewrite keeps metadata; slicer log is fresh and cached", _check_zip_rewrite_and_log),
    )
    try:
        with tempfile.TemporaryDirectory(prefix="filamatch_slicer_selftest_") as temp_dir:
            environment = _TestEnvironment(Path(temp_dir))
            environment.install()
            try:
                for label, scenario in scenarios:
                    scenario(environment)
                    print("PASS: {}".format(label))
            finally:
                environment.restore()
    except Exception:
        traceback.print_exc()
        print("FAIL: slicer_profiles self-test aborted", file=sys.stderr)
        return 1
    print("PASS: all slicer_profiles self-tests completed")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(_selftest())
