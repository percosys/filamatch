"""Open Filament Database client, cache and catalog-entry mapping.

This module downloads the Open Filament Database bulk dataset, caches it on disk with a TTL, searches it
and maps individual filament variants to entries compatible with
:mod:`catalog` so they can be imported into Favorites and the active Fusion
design.

The module is deliberately dependency-free beyond the Python standard library
(Python 3.8+) so it can be imported both inside Fusion 360 and from a plain
interpreter.  It never imports ``adsk`` and imports no third-party packages.

Data source and attribution
---------------------------
The filament data is provided by the Open Filament Database, licensed under
the MIT license.  Source repository:
https://github.com/OpenFilamentCollective/open-filament-database

The public bulk dataset is a single JSON document served from
``https://api.openfilamentdatabase.org/json/all.json`` (a pre-gzipped copy is
available at ``/json/all.json.gz`` and is preferred because it is much smaller
on the wire).  The document contains ``version``, ``generated_at`` plus the
``brands``, ``materials``, ``filaments``, ``variants``, ``sizes``, ``stores``
and ``purchase_links`` arrays.  This module joins:

* ``brands.id`` -> ``materials.brand_id`` (``filaments.brand_id`` when present)
* ``materials.id`` -> ``filaments.material_id``
* ``filaments.id`` -> ``variants.filament_id``

Quick start::

    from . import openfilament

    summary = openfilament.ensure_dataset()       # download/cache if needed
    results = openfilament.search("prusament pla", limit=10)
    entries = openfilament.get_entries([r.key for r in results])
    added = openfilament.append_user_catalog(entries)

Run ``python3 openfilament.py`` to execute the offline self-test (it uses fake
HTTP responses and temporary cache directories, never the network).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .. import config
from .printer_model import (
    default_density,
    default_template_material,
    normalize_color_hex,
)

LOGGER = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: Base URL of the Open Filament Database HTTP API.
API_BASE = "https://api.openfilamentdatabase.org"
#: Canonical (uncompressed) bulk dataset URL.
DATASET_URL = API_BASE + "/json/all.json"
#: Preferred pre-gzipped bulk dataset URL (smaller transfer).
DATASET_GZ_URL = API_BASE + "/json/all.json.gz"
#: Repository the data comes from (used in catalog entries and docs).
SOURCE_REPOSITORY = "https://github.com/OpenFilamentCollective/open-filament-database"
#: License of the Open Filament Database data.
DATA_LICENSE = "MIT"
#: Human readable attribution sentence included in generated catalog entries.
ATTRIBUTION = "Data from the Open Filament Database (MIT): " + SOURCE_REPOSITORY

#: Root of the on-disk download cache (see config.py for the location).
CACHE_ROOT: Path = config.CACHE_DIR / "openfilament"
#: File name of the cached bulk JSON document.
CACHE_FILE = "all.json"
#: File name of the cache metadata document.
META_FILE = "meta.json"
#: Hours a cached dataset stays valid before an explicit refresh is attempted.
CACHE_TTL = 24
#: Persistent user catalog written by :func:`append_user_catalog`.
USER_CATALOG_PATH: Path = config.USER_CATALOG_FILE
#: HTTP ``User-Agent`` sent for dataset downloads.
USER_AGENT = config.USER_AGENT + " (+https://github.com/OpenFilamentCollective/open-filament-database)"

#: HTTP timeout in seconds for dataset downloads (per socket operation).
_REQUEST_TIMEOUT = 15.0
#: Largest download accepted, compressed or not (bytes).
_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
#: Largest dataset accepted after gzip decompression (bytes).
_MAX_DATASET_BYTES = 400 * 1024 * 1024
#: Read size used while downloading and decompressing.
_READ_CHUNK = 1024 * 1024
#: Maximum desired length of a generated catalog entry id.
_MAX_CATALOG_ID_LENGTH = 80
#: Colour used when a dataset variant has no usable ``color_hex``.
_DEFAULT_COLOR_HEX = "#808080"
# Density and Fusion template fallbacks come from printer_model (one table
# for spools and Open Filament Database entries).
#: Regex used to sanitise generated entry ids.
_ID_UNSAFE_PATTERN = re.compile(r"[^a-z0-9_]+")
_ID_COLLAPSE_PATTERN = re.compile(r"_+")

__all__ = [
    "API_BASE",
    "DATASET_URL",
    "DATASET_GZ_URL",
    "CACHE_ROOT",
    "CACHE_FILE",
    "META_FILE",
    "CACHE_TTL",
    "USER_CATALOG_PATH",
    "USER_AGENT",
    "SOURCE_REPOSITORY",
    "DATA_LICENSE",
    "ATTRIBUTION",
    "OpenFilamentError",
    "OnlineFilament",
    "ensure_dataset",
    "dataset_summary",
    "cache_clear",
    "search",
    "search_materials",
    "get_entries",
    "load_user_catalog",
    "append_user_catalog",
]

# ---------------------------------------------------------------------------
# Errors and data model
# ---------------------------------------------------------------------------


class OpenFilamentError(RuntimeError):
    """Raised for Open Filament Database download, cache or lookup failures."""


@dataclass(frozen=True)
class OnlineFilament:
    """One searchable filament variant from the Open Filament Database.

    The database stores filament definitions (one per product) and colour
    variants below them; this dataclass flattens one joined ``brand ->
    material -> filament -> variant`` chain into a single search result.  The
    ``key`` uniquely identifies the result and can be resolved back to a
    catalog entry with :func:`get_entries`.
    """

    key: str
    brand: str
    material: str
    filament: str
    variant: str
    color_hex: str
    density_g_cm3: Optional[float] = None
    min_print_temp: Optional[int] = None
    max_print_temp: Optional[int] = None
    min_bed_temp: Optional[int] = None
    max_bed_temp: Optional[int] = None
    brand_slug: str = ""
    material_slug: str = ""
    filament_slug: str = ""
    variant_slug: str = ""

    def __post_init__(self) -> None:
        """Fill slugs and the result key from the joined names when missing."""
        brand_slug = (self.brand_slug or "").strip() or _slugify(self.brand)
        material_slug = (self.material_slug or "").strip() or _slugify(self.material)
        filament_slug = (self.filament_slug or "").strip() or _slugify(self.filament)
        variant_slug = (self.variant_slug or "").strip() or _slugify(self.variant)
        object.__setattr__(self, "brand_slug", brand_slug)
        object.__setattr__(self, "material_slug", material_slug)
        object.__setattr__(self, "filament_slug", filament_slug)
        object.__setattr__(self, "variant_slug", variant_slug)
        if not isinstance(self.key, str) or not self.key.strip():
            object.__setattr__(
                self,
                "key",
                "/".join((brand_slug, material_slug, filament_slug, variant_slug)),
            )

    def to_catalog_entry(self) -> Dict[str, Any]:
        """Return a ``catalog.FilamentEntry``-compatible dictionary.

        The returned mapping passes :func:`catalog.parse_catalog` validation:
        required fields are always non-empty, ``color_hex`` is an uppercase
        ``#RRGGBB`` string (falling back to ``#808080``) and
        ``density_g_cm3`` is a positive float (falling back to a material
        specific default).  Extra fields such as ``source``,
        ``source_url``, ``source_version``, ``source_repository`` and
        ``attribution`` are ignored by the catalog loader and record provenance.
        """
        material_name = _text_value(self.material).strip()
        material_upper = material_name.upper() or "PLA"
        color_hex = normalize_color_hex(self.color_hex, _DEFAULT_COLOR_HEX)
        return {
            "id": _catalog_id(self),
            "manufacturer": _text_value(self.brand).strip() or "Unknown Brand",
            "product_line": _text_value(self.filament).strip() or "Filament",
            "color_name": _text_value(self.variant).strip() or "Default",
            "color_hex": color_hex,
            "slicer_filament_type": material_upper,
            "density_g_cm3": _catalog_density(material_upper, self.density_g_cm3),
            "template_material": default_template_material(material_upper),
            "category": material_name or material_upper,
            "source": "openfilament",
            "source_url": DATASET_URL,
            "source_version": _DATASET_VERSION or "",
            "source_repository": SOURCE_REPOSITORY,
            "source_license": DATA_LICENSE,
            "attribution": ATTRIBUTION,
        }


# ---------------------------------------------------------------------------
# Small value helpers
# ---------------------------------------------------------------------------


def _text_value(value: object) -> str:
    """Return *value* as a string (``""`` for ``None``/non-strings)."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _id_key(value: object) -> str:
    """Normalise a dataset id so integer/string join keys compare equal."""
    return str(value).strip()


def _slugify(text: object) -> str:
    """Convert a display name into a lowercase ``[a-z0-9_]`` slug."""
    lowered = _text_value(text).strip().lower()
    slug = _ID_UNSAFE_PATTERN.sub("_", lowered)
    slug = _ID_COLLAPSE_PATTERN.sub("_", slug).strip("_")
    return slug or "item"


def _coerce_float(value: object) -> Optional[float]:
    """Return a finite positive float from a dataset value, or ``None``."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number <= 0.0:
        return None
    return number


def _coerce_int(value: object) -> Optional[int]:
    """Return an integer from a dataset value, or ``None`` when unusable."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def _catalog_density(material_upper: str, density: Optional[float]) -> float:
    """Return a valid catalog density, using per-material defaults as fallback."""
    if isinstance(density, (int, float)) and not isinstance(density, bool):
        number = float(density)
        if number == number and number not in (float("inf"), float("-inf")) and number > 0.0:
            return number
    return default_density(material_upper)


def _catalog_id(filament: OnlineFilament) -> str:
    """Build the sanitised ``ofd_...`` catalog id for a search result."""
    raw = "_".join(
        (
            "ofd",
            filament.brand_slug,
            filament.material_slug,
            filament.filament_slug,
            filament.variant_slug,
        )
    )
    cleaned = _ID_COLLAPSE_PATTERN.sub("_", _ID_UNSAFE_PATTERN.sub("_", raw.lower())).strip("_")
    if not cleaned:
        cleaned = "ofd_item"
    if len(cleaned) <= _MAX_CATALOG_ID_LENGTH:
        return cleaned
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    prefix = cleaned[: _MAX_CATALOG_ID_LENGTH - len(digest) - 1].rstrip("_")
    return prefix + "_" + digest


# ---------------------------------------------------------------------------
# Cache paths, atomic writes and dataset fetching
# ---------------------------------------------------------------------------


def _cache_dir() -> Path:
    """Return the configured cache directory."""
    return Path(CACHE_ROOT)


def _cache_file_path() -> Path:
    """Return the path of the cached bulk dataset file."""
    return _cache_dir() / CACHE_FILE


def _meta_file_path() -> Path:
    """Return the path of the cache metadata file."""
    return _cache_dir() / META_FILE


def _user_catalog_path() -> Path:
    """Return the default persistent user catalog path."""
    return Path(USER_CATALOG_PATH)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write *data* to *path* atomically (temporary file + :func:`os.replace`)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temp_name = tempfile.mkstemp(
            prefix=".openfilament_", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise OpenFilamentError(f"cannot write {path}: {exc}") from exc


def _atomic_write_json(path: Path, payload: object) -> None:
    """Serialise *payload* as pretty JSON and write it atomically to *path*."""
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    _atomic_write_bytes(path, text.encode("utf-8"))


def _read_json_file(path: Path) -> Optional[Any]:
    """Read one JSON file, returning ``None`` when missing or unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _read_meta_safe() -> Optional[Dict[str, Any]]:
    """Return the parsed cache metadata, or ``None`` when unavailable."""
    meta = _read_json_file(_meta_file_path())
    return meta if isinstance(meta, dict) else None


def _read_capped(response: Any, limit: int) -> bytes:
    """Read *response* in chunks; raise :class:`OpenFilamentError` beyond *limit* bytes."""
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = response.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise OpenFilamentError(
                "Open Filament Database download is larger than {} MB; refusing it".format(
                    limit // (1024 * 1024)
                )
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _gunzip_capped(data: bytes, limit: Optional[int] = None) -> bytes:
    """Decompress gzip *data*; raise :class:`OpenFilamentError` beyond *limit* bytes.

    *limit* defaults to :data:`_MAX_DATASET_BYTES`.
    """
    if limit is None:
        limit = _MAX_DATASET_BYTES
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    output: List[bytes] = []
    total = 0
    pending = data
    try:
        while pending:
            piece = decompressor.decompress(pending, _READ_CHUNK)
            pending = decompressor.unconsumed_tail
            total += len(piece)
            if total > limit:
                raise OpenFilamentError(
                    "Open Filament Database dataset is larger than {} MB after "
                    "decompression; refusing it".format(limit // (1024 * 1024))
                )
            output.append(piece)
            if decompressor.eof or (not piece and not pending):
                break
        tail = decompressor.flush()
    except zlib.error as exc:
        raise OpenFilamentError(f"Open Filament Database download is not valid gzip: {exc}") from exc
    if total + len(tail) > limit:
        raise OpenFilamentError(
            "Open Filament Database dataset is larger than {} MB after "
            "decompression; refusing it".format(limit // (1024 * 1024))
        )
    if not decompressor.eof:
        raise OpenFilamentError("Open Filament Database download is a truncated gzip file")
    output.append(tail)
    return b"".join(output)


def _http_get_bytes(url: str) -> bytes:
    """Fetch *url* and return the decoded body (HTTP layer, patchable in tests).

    No ``Accept-Encoding`` is sent, so the server returns the file as is; a
    ``Content-Encoding: gzip`` reply is still decoded.  The body is capped at
    :data:`_MAX_DOWNLOAD_BYTES`.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT) as response:
        body = _read_capped(response, _MAX_DOWNLOAD_BYTES)
        headers = getattr(response, "headers", None)
        encoding = ""
        if headers is not None:
            try:
                encoding = str(headers.get("Content-Encoding") or "").strip().lower()
            except Exception:  # noqa: BLE001 - unusual header objects
                encoding = ""
    if encoding == "gzip":
        body = _gunzip_capped(body)
    return body


def _download_dataset_bytes() -> Tuple[bytes, str]:
    """Download the bulk dataset, preferring the gzipped endpoint.

    Returns ``(dataset_json_bytes, source_url)``.  Raises
    :class:`OpenFilamentError` only when both endpoints fail.
    """
    failures: List[str] = []
    for url, is_gzipped in ((DATASET_GZ_URL, True), (DATASET_URL, False)):
        try:
            payload = _http_get_bytes(url)
            if is_gzipped:
                payload = _gunzip_capped(payload)
            LOGGER.info("downloaded Open Filament Database dataset from %s", url)
            return payload, url
        except Exception as exc:  # noqa: BLE001 - try the next endpoint, report all
            failures.append(f"{url}: {exc}")
            LOGGER.warning("Open Filament Database download failed from %s: %s", url, exc)
    raise OpenFilamentError(
        "could not download the Open Filament Database dataset: " + "; ".join(failures)
    )


def _parse_dataset_bytes(raw: bytes) -> Dict[str, Any]:
    """Parse and minimally validate the bulk dataset JSON."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OpenFilamentError(f"Open Filament Database dataset is not valid UTF-8: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OpenFilamentError(
            "Open Filament Database dataset is not valid JSON: "
            f"{exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc
    if not isinstance(payload, dict):
        raise OpenFilamentError(
            "Open Filament Database dataset must be a JSON object, "
            f"got {type(payload).__name__}"
        )
    for required_key in ("brands", "materials", "filaments", "variants"):
        if not isinstance(payload.get(required_key), list):
            raise OpenFilamentError(
                f"Open Filament Database dataset is missing the '{required_key}' array"
            )
    return payload


def _read_dataset_file(path: Path) -> Dict[str, Any]:
    """Read and parse the cached dataset file."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise OpenFilamentError(f"cannot read cached Open Filament Database dataset {path}: {exc}") from exc
    return _parse_dataset_bytes(raw)


def _parse_fetched_at(value: object) -> Optional[float]:
    """Interpret a stored ``fetched_at`` value (epoch seconds or ISO 8601)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return float(text)
        except ValueError:
            pass
        try:
            from datetime import datetime

            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _cache_is_fresh(cache_path: Path, meta: Optional[Mapping[str, Any]]) -> bool:
    """Return true when the cache exists and is younger than :data:`CACHE_TTL`."""
    try:
        if not cache_path.is_file():
            return False
        fetched_at: Optional[float] = None
        if meta is not None:
            fetched_at = _parse_fetched_at(meta.get("fetched_at"))
        if fetched_at is None:
            fetched_at = cache_path.stat().st_mtime
        age_seconds = time.time() - fetched_at
        return 0 <= age_seconds < float(CACHE_TTL) * 3600.0
    except OSError:
        return False


def _dataset_summary_payload(payload: Mapping[str, Any], cache_path: Path) -> Dict[str, Any]:
    """Build the :func:`ensure_dataset` return value for one parsed dataset."""
    filaments = payload.get("filaments")
    return {
        "version": _text_value(payload.get("version")),
        "generated_at": _text_value(payload.get("generated_at")),
        "path": str(cache_path),
        "count": len(filaments) if isinstance(filaments, list) else 0,
    }


# ---------------------------------------------------------------------------
# In-process dataset cache
# ---------------------------------------------------------------------------

#: Parsed dataset kept in memory for the lifetime of the process.
_DATASET_CACHE: Optional[Dict[str, Any]] = None
#: Version string of the in-memory dataset.
_DATASET_VERSION: str = ""
#: ``generated_at`` string of the in-memory dataset.
_DATASET_GENERATED_AT: str = ""
#: Path the in-memory dataset was read from.
_DATASET_CACHE_PATH: str = ""
#: Flattened searchable records for the in-memory dataset.
_ONLINE_CACHE: Optional[List[OnlineFilament]] = None
#: Keys of results whose filament or variant is marked discontinued.
_DISCONTINUED_KEYS: Set[str] = set()
#: ``time.time()`` when the in-memory dataset was installed (0 = never).
_DATASET_LOADED_AT: float = 0.0


def _reset_memory_state() -> None:
    """Forget the in-memory dataset and derived search records."""
    global _DATASET_CACHE, _DATASET_VERSION, _DATASET_GENERATED_AT, _DATASET_CACHE_PATH
    global _ONLINE_CACHE, _DISCONTINUED_KEYS, _DATASET_LOADED_AT
    _DATASET_CACHE = None
    _DATASET_VERSION = ""
    _DATASET_GENERATED_AT = ""
    _DATASET_CACHE_PATH = ""
    _ONLINE_CACHE = None
    _DISCONTINUED_KEYS = set()
    _DATASET_LOADED_AT = 0.0


def _activate_dataset(payload: Dict[str, Any], cache_path: Path) -> None:
    """Install *payload* as the in-memory dataset and reset derived caches."""
    global _DATASET_CACHE, _DATASET_VERSION, _DATASET_GENERATED_AT, _DATASET_CACHE_PATH
    global _ONLINE_CACHE, _DISCONTINUED_KEYS, _DATASET_LOADED_AT
    _DATASET_CACHE = payload
    _DATASET_VERSION = _text_value(payload.get("version"))
    _DATASET_GENERATED_AT = _text_value(payload.get("generated_at"))
    _DATASET_CACHE_PATH = str(cache_path)
    _ONLINE_CACHE = None
    _DISCONTINUED_KEYS = set()
    _DATASET_LOADED_AT = time.time()


def _memory_dataset_expired() -> bool:
    """Return true when the in-memory dataset is older than :data:`CACHE_TTL`.

    The age counts from when it was installed, so a stale cache served while
    offline is retried at most once per TTL instead of on every search.
    """
    age_seconds = time.time() - _DATASET_LOADED_AT
    return not 0 <= age_seconds < float(CACHE_TTL) * 3600.0


# ---------------------------------------------------------------------------
# Public dataset/cache API
# ---------------------------------------------------------------------------


def ensure_dataset(force: bool = False) -> Dict[str, Any]:
    """Ensure the Open Filament Database dataset is cached and return a summary.

    The cache is reused when it is younger than :data:`CACHE_TTL` hours unless
    *force* is true.  A stale cache is refreshed from the network; if that
    fails, the stale cache is used with a warning so callers stay functional
    offline.  Only when there is no usable cache at all does a network failure
    raise :class:`OpenFilamentError`.

    Args:
        force: Download the dataset even when the cache is still fresh.

    Returns:
        ``{"version": str, "generated_at": str, "path": str, "count": int}``
        where ``count`` is the number of filament definitions in the dataset.

    Raises:
        OpenFilamentError: When the dataset cannot be downloaded, is corrupt
            and no usable cache exists, or when the cache cannot be written.
    """
    cache_path = _cache_file_path()
    meta = _read_meta_safe()
    cached_payload: Optional[Dict[str, Any]] = None
    cached_error: Optional[OpenFilamentError] = None
    if cache_path.is_file():
        try:
            cached_payload = _read_dataset_file(cache_path)
        except OpenFilamentError as exc:
            cached_error = exc
            LOGGER.warning("ignoring unreadable Open Filament Database cache: %s", exc)

    if not force and cached_payload is not None and _cache_is_fresh(cache_path, meta):
        LOGGER.debug("reusing fresh Open Filament Database cache at %s", cache_path)
        _activate_dataset(cached_payload, cache_path)
        return _dataset_summary_payload(cached_payload, cache_path)

    stale_summary: Optional[Dict[str, Any]] = None
    if cached_payload is not None:
        stale_summary = _dataset_summary_payload(cached_payload, cache_path)

    try:
        raw_payload, source_url = _download_dataset_bytes()
        payload = _parse_dataset_bytes(raw_payload)
    except OpenFilamentError as exc:
        if stale_summary is not None:
            LOGGER.warning(
                "Open Filament Database refresh failed; using the stale cached dataset: %s", exc
            )
            _activate_dataset(cached_payload, cache_path)  # type: ignore[arg-type]
            return stale_summary
        if cached_error is not None:
            raise OpenFilamentError(
                "Open Filament Database dataset is unavailable: {}. Cached copy is corrupt: {}".format(
                    exc, cached_error
                )
            ) from exc
        raise

    _atomic_write_bytes(cache_path, raw_payload)
    _atomic_write_json(
        _meta_file_path(),
        {
            "version": _text_value(payload.get("version")),
            "generated_at": _text_value(payload.get("generated_at")),
            "fetched_at": time.time(),
            "bytes": len(raw_payload),
        },
    )
    _activate_dataset(payload, cache_path)
    LOGGER.info(
        "cached Open Filament Database dataset %s (%d bytes) from %s",
        _DATASET_VERSION or "<unknown version>",
        len(raw_payload),
        source_url,
    )
    return _dataset_summary_payload(payload, cache_path)


def dataset_summary() -> Dict[str, Any]:
    """Return a lightweight dataset summary without forcing a download.

    The in-memory dataset is used when available, otherwise the cached file and
    metadata are read.  When nothing is cached the counts are zero and
    ``cached`` is ``False``.

    Returns:
        ``{"version": str, "generated_at": str, "brands": int,
        "materials": int, "filaments": int, "variants": int, "cached": bool}``
    """
    cache_path = _cache_file_path()
    payload = _DATASET_CACHE
    if payload is None and cache_path.is_file():
        try:
            payload = _read_dataset_file(cache_path)
        except OpenFilamentError as exc:
            LOGGER.warning("could not read the cached Open Filament Database dataset: %s", exc)
            payload = None
    if payload is None:
        meta = _read_meta_safe() or {}
        return {
            "version": _text_value(meta.get("version")),
            "generated_at": _text_value(meta.get("generated_at")),
            "brands": 0,
            "materials": 0,
            "filaments": 0,
            "variants": 0,
            "cached": cache_path.is_file(),
        }
    return {
        "version": _DATASET_VERSION if _DATASET_CACHE is payload else _text_value(payload.get("version")),
        "generated_at": (
            _DATASET_GENERATED_AT if _DATASET_CACHE is payload else _text_value(payload.get("generated_at"))
        ),
        "brands": len(payload.get("brands") or []),
        "materials": len(payload.get("materials") or []),
        "filaments": len(payload.get("filaments") or []),
        "variants": len(payload.get("variants") or []),
        "cached": cache_path.is_file(),
    }


def cache_clear() -> bool:
    """Delete the cached dataset and forget the in-memory copy.

    Returns:
        ``True`` when at least one cache file was removed, ``False`` when there
        was nothing to clear.
    """
    removed = False
    for path in (_cache_file_path(), _meta_file_path()):
        try:
            if path.is_file():
                path.unlink()
                removed = True
        except OSError as exc:
            LOGGER.warning("could not remove Open Filament Database cache file %s: %s", path, exc)
    _reset_memory_state()
    return removed


# ---------------------------------------------------------------------------
# Dataset joins and search
# ---------------------------------------------------------------------------


def _load_dataset() -> Dict[str, Any]:
    """Return the in-memory dataset, downloading/caching it when needed.

    A long Fusion session re-checks the in-memory copy against
    :data:`CACHE_TTL`; when the refresh fails the old copy keeps serving.
    """
    if _DATASET_CACHE is not None:
        if not _memory_dataset_expired():
            return _DATASET_CACHE
        try:
            ensure_dataset()
        except OpenFilamentError as exc:
            LOGGER.warning("Open Filament Database refresh failed; keeping the loaded dataset: %s", exc)
            _mark_memory_dataset_checked()
        return _DATASET_CACHE  # type: ignore[return-value]
    ensure_dataset()
    if _DATASET_CACHE is None:  # pragma: no cover - defensive
        raise OpenFilamentError("Open Filament Database dataset could not be loaded")
    return _DATASET_CACHE


def _mark_memory_dataset_checked() -> None:
    """Restart the TTL clock of the in-memory dataset after a failed refresh."""
    global _DATASET_LOADED_AT
    _DATASET_LOADED_AT = time.time()


def _variant_discontinued(*records: object) -> bool:
    """Return true when any dataset record is marked discontinued."""
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("discontinued") is True:
            return True
        status = record.get("status")
        if isinstance(status, str) and status.strip().casefold() == "discontinued":
            return True
    return False


def _build_online_filaments(dataset: Mapping[str, Any]) -> List[OnlineFilament]:
    """Join the dataset arrays into flattened :class:`OnlineFilament` records."""
    brand_by_id: Dict[str, Dict[str, Any]] = {}
    for brand in dataset.get("brands") or []:
        if isinstance(brand, dict) and brand.get("id") is not None:
            brand_by_id[_id_key(brand.get("id"))] = brand
    material_by_id: Dict[str, Dict[str, Any]] = {}
    for material in dataset.get("materials") or []:
        if isinstance(material, dict) and material.get("id") is not None:
            material_by_id[_id_key(material.get("id"))] = material
    variants_by_filament: Dict[str, List[Dict[str, Any]]] = {}
    for variant in dataset.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        filament_id = variant.get("filament_id")
        if filament_id is None:
            continue
        variants_by_filament.setdefault(_id_key(filament_id), []).append(variant)

    records: List[OnlineFilament] = []
    for filament in dataset.get("filaments") or []:
        if not isinstance(filament, dict) or filament.get("id") is None:
            continue
        filament_id = _id_key(filament.get("id"))
        variants = variants_by_filament.get(filament_id)
        if not variants:
            continue
        material = material_by_id.get(_id_key(filament.get("material_id"))) or {}
        brand_id = filament.get("brand_id")
        if brand_id is None:
            brand_id = material.get("brand_id")
        brand = brand_by_id.get(_id_key(brand_id)) or {}
        brand_name = _text_value(brand.get("name")).strip() or _text_value(filament.get("brand")).strip()
        material_name = (
            _text_value(material.get("material")).strip()
            or _text_value(filament.get("material")).strip()
        )
        filament_name = _text_value(filament.get("name")).strip()
        brand_slug = _text_value(brand.get("slug")).strip() or _slugify(brand_name)
        material_slug = _text_value(material.get("slug")).strip() or _slugify(material_name)
        filament_slug = _text_value(filament.get("slug")).strip() or _slugify(filament_name)
        density = _coerce_float(filament.get("density"))
        min_print = _coerce_int(filament.get("min_print_temperature"))
        max_print = _coerce_int(filament.get("max_print_temperature"))
        min_bed = _coerce_int(filament.get("min_bed_temperature"))
        max_bed = _coerce_int(filament.get("max_bed_temperature"))
        discontinued = _variant_discontinued(filament)
        for variant in variants:
            variant_name = _text_value(variant.get("name")).strip()
            variant_slug = _text_value(variant.get("slug")).strip() or _slugify(variant_name)
            key = "/".join((brand_slug, material_slug, filament_slug, variant_slug))
            record = OnlineFilament(
                key=key,
                brand=brand_name,
                material=material_name,
                filament=filament_name,
                variant=variant_name,
                color_hex=_text_value(variant.get("color_hex")).strip(),
                density_g_cm3=density,
                min_print_temp=min_print,
                max_print_temp=max_print,
                min_bed_temp=min_bed,
                max_bed_temp=max_bed,
                brand_slug=brand_slug,
                material_slug=material_slug,
                filament_slug=filament_slug,
                variant_slug=variant_slug,
            )
            records.append(record)
            if discontinued or _variant_discontinued(variant):
                _DISCONTINUED_KEYS.add(key)
    return records


def _online_filaments() -> List[OnlineFilament]:
    """Return flattened search records for the current dataset (cached)."""
    global _ONLINE_CACHE, _DISCONTINUED_KEYS
    if _ONLINE_CACHE is not None and not _memory_dataset_expired():
        return _ONLINE_CACHE
    dataset = _load_dataset()
    if _ONLINE_CACHE is not None:  # the refresh failed and kept the old dataset
        return _ONLINE_CACHE
    _DISCONTINUED_KEYS = set()
    _ONLINE_CACHE = _build_online_filaments(dataset)
    return _ONLINE_CACHE


def _match_quality(record: OnlineFilament, query_cf: str, tokens: Sequence[str]) -> int:
    """Score how well *record* matches the query (higher is better)."""
    names = [
        record.brand.casefold(),
        record.filament.casefold(),
        record.variant.casefold(),
        record.material.casefold(),
    ]
    names = [name for name in names if name]
    words: List[str] = []
    for name in names:
        words.extend(token for token in re.split(r"[^a-z0-9]+", name) if token)
    quality = 0
    if query_cf:
        if any(name == query_cf for name in names):
            quality = 4
        elif any(name.startswith(query_cf) for name in names):
            quality = 3
        elif any(word.startswith(query_cf) for word in words):
            quality = 2
        elif any(query_cf in name for name in names):
            quality = 1
    for token in tokens:
        if any(word.startswith(token) for word in words):
            quality = max(quality, 2)
        elif any(token in word for word in words):
            quality = max(quality, 1)
        elif token in " ".join(names):
            quality = max(quality, 1)
    return quality


def search(query: str, material: Optional[str] = None, limit: int = 50) -> List[OnlineFilament]:
    """Search the dataset for filament variants matching *query*.

    Matching is case-insensitive and token based over the brand, material,
    filament and variant names and slugs (all query tokens must match).  The
    optional *material* filter is an exact, case-insensitive material-name
    match (for example ``"PLA"``).  Results are ranked with exact/prefix name
    matches before substring matches and with active filaments before
    discontinued ones; duplicate result keys are collapsed.

    Args:
        query: Free-text search terms; an empty query returns all filaments.
        material: Optional material name to restrict results to.
        limit: Maximum number of results to return (``<= 0`` returns none).

    Returns:
        Up to *limit* :class:`OnlineFilament` results, best match first.

    Raises:
        OpenFilamentError: When the dataset cannot be loaded or an argument is
            malformed.
    """
    if not isinstance(query, str):
        raise OpenFilamentError("search query must be a string")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise OpenFilamentError("search limit must be an integer")
    if limit <= 0:
        return []
    records = _online_filaments()

    material_filter: Optional[str] = None
    if material is not None:
        if not isinstance(material, str):
            raise OpenFilamentError("search material filter must be a string or None")
        material_filter = material.strip().casefold() or None

    query_cf = query.strip().casefold()
    tokens = [token for token in re.split(r"\s+", query_cf) if token]

    ranked: List[Tuple[Tuple[int, int, str, str, str, str], OnlineFilament]] = []
    seen_keys: Set[str] = set()
    for record in records:
        if material_filter is not None and record.material.strip().casefold() != material_filter:
            continue
        haystack = " ".join(
            (
                record.brand.casefold(),
                record.filament.casefold(),
                record.variant.casefold(),
                record.material.casefold(),
                record.brand_slug.casefold(),
                record.filament_slug.casefold(),
                record.variant_slug.casefold(),
            )
        )
        if tokens and not all(token in haystack for token in tokens):
            continue
        if record.key in seen_keys:
            continue
        seen_keys.add(record.key)
        quality = _match_quality(record, query_cf, tokens)
        sort_key = (
            1 if record.key in _DISCONTINUED_KEYS else 0,
            -quality,
            record.brand.casefold(),
            record.filament.casefold(),
            record.variant.casefold(),
            record.key,
        )
        ranked.append((sort_key, record))
    ranked.sort(key=lambda item: item[0])
    return [record for _sort_key, record in ranked[:limit]]


def search_materials() -> List[str]:
    """Return the sorted distinct material names available in the dataset.

    The dataset is loaded (downloading/caching it if needed) the same way
    :func:`search` loads it.
    """
    dataset = _load_dataset()
    names: Set[str] = set()
    for material in dataset.get("materials") or []:
        if isinstance(material, dict):
            name = _text_value(material.get("material")).strip()
            if name:
                names.add(name)
    if not names:
        for filament in dataset.get("filaments") or []:
            if isinstance(filament, dict):
                name = _text_value(filament.get("material")).strip()
                if name:
                    names.add(name)
    return sorted(names, key=lambda item: item.casefold())


def get_entries(keys: Sequence[str]) -> List[Dict[str, Any]]:
    """Resolve search result keys to catalog-entry dictionaries.

    Unknown keys are logged and skipped; the surviving entries keep the order
    of *keys*.

    Args:
        keys: Result keys from :class:`OnlineFilament` objects (a single string
            is accepted and treated as one key).

    Returns:
        Catalog-entry dictionaries ready for ``catalog.parse_catalog``.

    Raises:
        OpenFilamentError: When the dataset cannot be loaded.
    """
    if isinstance(keys, str):
        key_sequence: Sequence[str] = [keys]
    elif isinstance(keys, Sequence):
        key_sequence = keys
    else:
        raise OpenFilamentError("get_entries expects a sequence of result keys")
    by_key = {record.key: record for record in _online_filaments()}
    entries: List[Dict[str, Any]] = []
    for key in key_sequence:
        if not isinstance(key, str):
            LOGGER.warning("ignoring non-string Open Filament Database key %r", key)
            continue
        record = by_key.get(key)
        if record is None:
            LOGGER.warning("unknown Open Filament Database result key %r", key)
            continue
        entries.append(record.to_catalog_entry())
    return entries


# ---------------------------------------------------------------------------
# Persistent user catalog
# ---------------------------------------------------------------------------


def _validated_user_entries(payload: object, path: Path) -> List[Dict[str, Any]]:
    """Validate the minimal user-catalog entry shape, dropping bad entries."""
    if isinstance(payload, dict):
        raw_entries: object = payload.get("filaments")
    elif isinstance(payload, list):
        raw_entries = payload
    else:
        LOGGER.warning("ignoring user filament catalog %s: unsupported JSON root", path)
        return []
    if not isinstance(raw_entries, list):
        LOGGER.warning("ignoring user filament catalog %s: 'filaments' is not a list", path)
        return []
    entries: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            LOGGER.warning("ignoring user catalog entry #%d: not a JSON object", index + 1)
            continue
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            LOGGER.warning("ignoring user catalog entry #%d: missing a string 'id'", index + 1)
            continue
        if entry_id in seen_ids:
            LOGGER.warning("ignoring duplicate user catalog entry id %r", entry_id)
            continue
        seen_ids.add(entry_id)
        entries.append(dict(entry))
    return entries


def load_user_catalog(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Load the persistent user filament catalog.

    The default location is ``$TMPDIR/Filamatch/user_catalog.json``.
    Both a bare JSON array and ``{"filaments": [...]}`` are accepted.  The
    function never raises: missing, unreadable or malformed files simply return
    an empty list after a warning, and entries without a string ``id`` are
    skipped.

    Args:
        path: Optional alternative catalog path (defaults to
            :data:`USER_CATALOG_PATH`).

    Returns:
        The validated catalog-entry dictionaries in file order.
    """
    target = Path(path) if path is not None else _user_catalog_path()
    try:
        raw_text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        LOGGER.warning("could not read user filament catalog %s: %s", target, exc)
        return []
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        LOGGER.warning(
            "user filament catalog %s is not valid JSON: %s (line %d, column %d)",
            target,
            exc.msg,
            exc.lineno,
            exc.colno,
        )
        return []
    return _validated_user_entries(payload, target)


def append_user_catalog(
    entries: Sequence[Mapping[str, Any]],
    path: Optional[Path] = None,
) -> int:
    """Merge catalog entries into the persistent user catalog.

    Entries are de-duplicated by their ``id`` against the existing file and
    within the supplied sequence, and the result is written atomically.

    Args:
        entries: Catalog-entry dictionaries (normally from :func:`get_entries`).
        path: Optional alternative catalog path (defaults to
            :data:`USER_CATALOG_PATH`).

    Returns:
        The number of newly added entries.

    Raises:
        OpenFilamentError: When an entry is not a mapping or has no string
            ``id``, or when the catalog cannot be written.
    """
    if isinstance(entries, (str, bytes)) or entries is None:
        raise OpenFilamentError("append_user_catalog expects a sequence of catalog entry dicts")
    target = Path(path) if path is not None else _user_catalog_path()
    existing = load_user_catalog(target)
    known_ids = {entry["id"] for entry in existing}
    added = 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise OpenFilamentError(f"catalog entry #{index + 1} is not a mapping")
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise OpenFilamentError(f"catalog entry #{index + 1} is missing a string 'id'")
        if entry_id in known_ids:
            continue
        existing.append(dict(entry))
        known_ids.add(entry_id)
        added += 1
    if added:
        _atomic_write_json(target, {"filaments": existing})
    return added


# ---------------------------------------------------------------------------
# Self-test (offline, uses fake HTTP responses)
# ---------------------------------------------------------------------------

#: Small synthetic dataset used by the self-test (2 brands, 2 materials,
#: 2 filaments, 3 variants: Black + Orange PLA and White PETG).
_SELFTEST_DATASET: Dict[str, Any] = {
    "version": "test-2026-11-05",
    "generated_at": "2026-11-05T12:00:00Z",
    "brands": [
        {
            "id": 1,
            "name": "Prusa Polymers",
            "slug": "prusa-polymers",
            "origin": "CZ",
            "website": "https://example.invalid/prusa",
        },
        {
            "id": 2,
            "name": "Generic Filaments",
            "slug": "generic-filaments",
            "origin": "US",
            "website": "https://example.invalid/generic",
        },
    ],
    "materials": [
        {"id": 10, "brand_id": 1, "material": "PLA", "material_class": "PLA"},
        {"id": 11, "brand_id": 2, "material": "PETG", "material_class": "PETG"},
    ],
    "filaments": [
        {
            "id": 100,
            "uuid": "u-100",
            "name": "Prusament PLA",
            "density": 1.24,
            "min_print_temperature": 215,
            "max_print_temperature": 230,
            "min_bed_temperature": 60,
            "max_bed_temperature": 65,
            "brand_id": 1,
            "material_id": 10,
            "material": "PLA",
            "slug": "prusament-pla",
        },
        {
            "id": 101,
            "uuid": "u-101",
            "name": "Generic PETG",
            "density": 1.27,
            "min_print_temperature": 230,
            "max_print_temperature": 250,
            "min_bed_temperature": 70,
            "max_bed_temperature": 80,
            "brand_id": 2,
            "material_id": 11,
            "material": "PETG",
            "slug": "generic-petg",
        },
    ],
    "variants": [
        {
            "id": 1000,
            "name": "Galaxy Black",
            "color_hex": "#161616",
            "filament_id": 100,
            "slug": "galaxy-black",
        },
        {
            "id": 1001,
            "name": "Orange",
            "color_hex": "#FF7F00",
            "filament_id": 100,
            "slug": "orange",
            "discontinued": True,
        },
        {
            "id": 1002,
            "name": "White",
            "color_hex": "#FFFFFF",
            "filament_id": 101,
            "slug": "white",
        },
    ],
    "sizes": [],
    "stores": [],
    "purchase_links": [],
}

_SELFTEST_JSON_BYTES: bytes = json.dumps(_SELFTEST_DATASET).encode("utf-8")


class _FakeHttpResponse:
    """Minimal stand-in for :class:`http.client.HTTPResponse` in the self-test."""

    def __init__(self, payload: bytes, headers: Optional[Dict[str, str]] = None) -> None:
        self._payload = payload
        self._offset = 0
        self.headers = dict(headers or {})

    def read(self, size: int = -1) -> bytes:
        """Return the next *size* bytes of the fake response body."""
        if size is None or size < 0:
            size = len(self._payload) - self._offset
        chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _SelfTestHarness:
    """Context manager installing fake network access and temporary cache paths.

    It saves and restores every module-level global the dataset code touches so
    self-test cases stay isolated from each other and from the real caches.
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.calls: List[str] = []
        self.request_headers: List[Dict[str, str]] = []
        self.plain_response: Optional[_FakeHttpResponse] = None
        self.fail_all = False
        self.fail_urls: Set[str] = set()
        self._saved: Dict[str, Any] = {}

    def __enter__(self) -> "_SelfTestHarness":
        global CACHE_ROOT, USER_CATALOG_PATH, _DATASET_CACHE, _DATASET_VERSION
        global _DATASET_GENERATED_AT, _DATASET_CACHE_PATH, _ONLINE_CACHE, _DISCONTINUED_KEYS
        global _DATASET_LOADED_AT
        import urllib.request

        self._saved = {
            "_DATASET_LOADED_AT": _DATASET_LOADED_AT,
            "CACHE_ROOT": CACHE_ROOT,
            "USER_CATALOG_PATH": USER_CATALOG_PATH,
            "_DATASET_CACHE": _DATASET_CACHE,
            "_DATASET_VERSION": _DATASET_VERSION,
            "_DATASET_GENERATED_AT": _DATASET_GENERATED_AT,
            "_DATASET_CACHE_PATH": _DATASET_CACHE_PATH,
            "_ONLINE_CACHE": _ONLINE_CACHE,
            "_DISCONTINUED_KEYS": set(_DISCONTINUED_KEYS),
            "urlopen": urllib.request.urlopen,
        }
        self.base_dir.mkdir(parents=True, exist_ok=True)
        CACHE_ROOT = self.base_dir / "openfilament"
        USER_CATALOG_PATH = self.base_dir / "user_catalog.json"
        _DATASET_CACHE = None
        _DATASET_VERSION = ""
        _DATASET_GENERATED_AT = ""
        _DATASET_CACHE_PATH = ""
        _ONLINE_CACHE = None
        _DISCONTINUED_KEYS = set()
        _DATASET_LOADED_AT = 0.0
        urllib.request.urlopen = self._fake_urlopen
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        global CACHE_ROOT, USER_CATALOG_PATH, _DATASET_CACHE, _DATASET_VERSION
        global _DATASET_GENERATED_AT, _DATASET_CACHE_PATH, _ONLINE_CACHE, _DISCONTINUED_KEYS
        global _DATASET_LOADED_AT
        import urllib.request

        _DATASET_LOADED_AT = self._saved["_DATASET_LOADED_AT"]
        CACHE_ROOT = self._saved["CACHE_ROOT"]
        USER_CATALOG_PATH = self._saved["USER_CATALOG_PATH"]
        _DATASET_CACHE = self._saved["_DATASET_CACHE"]
        _DATASET_VERSION = self._saved["_DATASET_VERSION"]
        _DATASET_GENERATED_AT = self._saved["_DATASET_GENERATED_AT"]
        _DATASET_CACHE_PATH = self._saved["_DATASET_CACHE_PATH"]
        _ONLINE_CACHE = self._saved["_ONLINE_CACHE"]
        _DISCONTINUED_KEYS = self._saved["_DISCONTINUED_KEYS"]
        urllib.request.urlopen = self._saved["urlopen"]
        return False

    def _fake_urlopen(self, request: object, timeout: object = None) -> _FakeHttpResponse:
        """Fake ``urllib.request.urlopen`` returning gzip/plain synthetic data."""
        url = getattr(request, "full_url", None) or str(request)
        self.calls.append(url)
        self.request_headers.append({k.lower(): v for k, v in getattr(request, "header_items", lambda: [])()})
        if self.fail_all or url in self.fail_urls:
            raise urllib.error.URLError("simulated network failure")
        if self.plain_response is not None and not url.endswith(".gz"):
            return self.plain_response
        if url.endswith(".gz"):
            return _FakeHttpResponse(gzip.compress(_SELFTEST_JSON_BYTES))
        return _FakeHttpResponse(_SELFTEST_JSON_BYTES)

    def cache_path(self) -> Path:
        """Return the cache file path used by this harness."""
        return self.base_dir / "openfilament" / CACHE_FILE

    def meta_path(self) -> Path:
        """Return the cache metadata path used by this harness."""
        return self.base_dir / "openfilament" / META_FILE

    def read_meta(self) -> Dict[str, Any]:
        """Read the cache metadata JSON written by :func:`ensure_dataset`."""
        payload = json.loads(self.meta_path().read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise AssertionError("cache metadata is not a JSON object")
        return payload

    def make_cache_stale(self, age_seconds: float = float(CACHE_TTL) * 3600.0 + 300.0) -> None:
        """Rewrite ``meta.json`` so the cache appears older than the TTL."""
        meta = self.read_meta()
        meta["fetched_at"] = time.time() - age_seconds
        self.meta_path().write_text(json.dumps(meta), encoding="utf-8")


def _expect_open_filament_error(callable_: Callable[[], Any], fragment: str = "") -> OpenFilamentError:
    """Run *callable_* and assert it raises :class:`OpenFilamentError`."""
    try:
        callable_()
    except OpenFilamentError as exc:
        if fragment and fragment not in str(exc):
            raise AssertionError(f"{fragment!r} not found in error message {str(exc)!r}") from exc
        return exc
    raise AssertionError("expected OpenFilamentError but no error was raised")


def _self_test_cases(base_dir: Path) -> List[Tuple[str, Callable[[Path], None]]]:
    """Return the self-test ``(description, case)`` pairs."""

    def case_ensure_dataset_download_and_cache(case_dir: Path) -> None:
        with _SelfTestHarness(case_dir) as harness:
            summary = ensure_dataset()
            assert set(summary) == {"version", "generated_at", "path", "count"}, summary
            assert summary["version"] == _SELFTEST_DATASET["version"], summary
            assert summary["generated_at"] == _SELFTEST_DATASET["generated_at"], summary
            assert summary["count"] == 2, summary
            cache_path = Path(summary["path"])
            assert cache_path.is_file(), "cache file was not written"
            cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
            assert cached_payload["variants"] == _SELFTEST_DATASET["variants"]
            meta = harness.read_meta()
            assert set(meta) == {"version", "generated_at", "fetched_at", "bytes"}, meta
            assert meta["version"] == _SELFTEST_DATASET["version"], meta
            assert meta["generated_at"] == _SELFTEST_DATASET["generated_at"], meta
            assert isinstance(meta["fetched_at"], (int, float)), meta
            assert meta["bytes"] == cache_path.stat().st_size, meta
            assert len(harness.calls) == 1, harness.calls
            assert harness.calls[0] == DATASET_GZ_URL, harness.calls
            reused = ensure_dataset()
            assert reused["version"] == summary["version"], reused
            assert len(harness.calls) == 1, "fresh cache must not trigger a download"

    def case_ttl_expiry_and_force(case_dir: Path) -> None:
        with _SelfTestHarness(case_dir) as harness:
            ensure_dataset()
            assert len(harness.calls) == 1, harness.calls
            harness.make_cache_stale()
            ensure_dataset()
            assert len(harness.calls) == 2, "stale cache must be refreshed"
            ensure_dataset(force=True)
            assert len(harness.calls) == 3, "force=True must always download"

    def case_stale_offline_fallback(case_dir: Path) -> None:
        with _SelfTestHarness(case_dir) as harness:
            summary = ensure_dataset()
            calls_after_first = len(harness.calls)
            harness.make_cache_stale()
            harness.fail_all = True
            fallback = ensure_dataset()
            assert fallback["version"] == summary["version"], fallback
            assert len(harness.calls) == calls_after_first + 2, harness.calls
            assert harness.calls[-2:] == [DATASET_GZ_URL, DATASET_URL], harness.calls
            assert harness.cache_path().is_file(), "stale cache file must be preserved"
            offline_summary = dataset_summary()
            assert offline_summary["cached"] is True, offline_summary
            assert offline_summary["version"] == summary["version"], offline_summary

    def case_network_errors_and_json_fallback(case_dir: Path) -> None:
        with _SelfTestHarness(case_dir / "no_cache") as harness:
            harness.fail_all = True
            _expect_open_filament_error(ensure_dataset, "Open Filament Database")
            assert not harness.cache_path().exists()
        with _SelfTestHarness(case_dir / "json_fallback") as harness:
            harness.fail_urls.add(DATASET_GZ_URL)
            summary = ensure_dataset()
            assert summary["version"] == _SELFTEST_DATASET["version"], summary
            assert harness.calls == [DATASET_GZ_URL, DATASET_URL], harness.calls
        with _SelfTestHarness(case_dir / "corrupt") as harness:
            harness.fail_urls.add(DATASET_GZ_URL)
            harness.fail_urls.add(DATASET_URL)
            _expect_open_filament_error(ensure_dataset, "Open Filament Database")

    def case_search_ranking_and_filters(case_dir: Path) -> None:
        with _SelfTestHarness(case_dir):
            ensure_dataset()
            prusa_results = search("prusa")
            assert len(prusa_results) == 2, prusa_results
            assert {record.material for record in prusa_results} == {"PLA"}, prusa_results
            assert [record.variant for record in prusa_results] == ["Galaxy Black", "Orange"], prusa_results
            petg_results = search("", material="petg", limit=10)
            assert len(petg_results) == 1, petg_results
            assert petg_results[0].variant == "White", petg_results
            assert petg_results[0].material == "PETG", petg_results
            assert search("zzz-not-a-thing") == []
            assert search("", limit=0) == []
            assert len(search("", limit=2)) == 2
            assert search("galaxy")[0].variant == "Galaxy Black"
            assert search("black")[0].variant == "Galaxy Black"
            slug_results = search("galaxy-black", limit=5)
            assert len(slug_results) == 1 and slug_results[0].variant == "Galaxy Black", slug_results
            token_results = search("prusament pla")
            assert len(token_results) == 2, token_results
            assert token_results[0].variant == "Galaxy Black", "active variants rank before discontinued ones"
            all_results = search("", limit=50)
            keys = [record.key for record in all_results]
            assert len(keys) == len(set(keys)) == 3, keys

    def case_search_materials(case_dir: Path) -> None:
        with _SelfTestHarness(case_dir) as harness:
            materials = search_materials()
            assert materials == ["PETG", "PLA"], materials
            assert len(harness.calls) == 1, "search_materials must load the dataset"

    def case_to_catalog_entry_mapping(case_dir: Path) -> None:
        with _SelfTestHarness(case_dir):
            ensure_dataset()
            records = search("galaxy black", limit=1)
            assert records, "expected the Galaxy Black variant"
            record = records[0]
            assert record.density_g_cm3 == 1.24, record
            assert record.min_print_temp == 215 and record.max_print_temp == 230, record
            assert record.min_bed_temp == 60 and record.max_bed_temp == 65, record
            entry = record.to_catalog_entry()
            assert re.fullmatch(r"[a-z0-9_]+", entry["id"]), entry["id"]
            assert len(entry["id"]) <= _MAX_CATALOG_ID_LENGTH, entry["id"]
            assert entry["id"].startswith("ofd_prusa_polymers_pla_prusament_pla_galaxy_black"), entry["id"]
            assert entry["manufacturer"] == "Prusa Polymers", entry
            assert entry["product_line"] == "Prusament PLA", entry
            assert entry["color_name"] == "Galaxy Black", entry
            assert entry["color_hex"] == "#161616", entry
            assert entry["slicer_filament_type"] == "PLA", entry
            assert entry["density_g_cm3"] == 1.24, entry
            assert entry["template_material"] == "ABS Plastic", entry
            assert entry["category"] == "PLA", entry
            assert entry["source"] == "openfilament", entry
            assert entry["source_url"] == DATASET_URL, entry
            assert entry["source_version"] == _SELFTEST_DATASET["version"], entry
            assert entry["source_repository"] == SOURCE_REPOSITORY, entry
            assert entry["source_license"] == DATA_LICENSE, entry

            fallback = OnlineFilament(
                key="fallback",
                brand="Weird Brand",
                material="PETG",
                filament="Filament: Special!",
                variant="",
                color_hex="not-a-colour",
                density_g_cm3=None,
            )
            fallback_entry = fallback.to_catalog_entry()
            assert fallback_entry["color_hex"] == _DEFAULT_COLOR_HEX, fallback_entry
            assert fallback_entry["density_g_cm3"] == 1.27, fallback_entry
            assert fallback_entry["template_material"] == "PET Plastic", fallback_entry
            assert fallback_entry["slicer_filament_type"] == "PETG", fallback_entry
            assert fallback_entry["color_name"] == "Default", fallback_entry
            assert re.fullmatch(r"[a-z0-9_]+", fallback_entry["id"]), fallback_entry["id"]

            expected = {
                "PLA": (1.24, "ABS Plastic"),
                "PETG": (1.27, "PET Plastic"),
                "ABS": (1.04, "ABS Plastic"),
                "ASA": (1.05, "ABS Plastic"),
                "TPU": (1.21, "ABS Plastic"),
                "PC": (1.21, "ABS Plastic"),
                "ABS-GF": (1.08, "ABS Plastic"),
                "PA-CF": (1.15, "Nylon Plastic"),
                "NYLON": (1.24, "ABS Plastic"),
            }
            for material, (density, template) in expected.items():
                mapped = OnlineFilament(
                    key="k",
                    brand="B",
                    material=material,
                    filament="F",
                    variant="V",
                    color_hex="#112233",
                ).to_catalog_entry()
                assert mapped["density_g_cm3"] == density, (material, mapped)
                assert mapped["template_material"] == template, (material, mapped)

            long_record = OnlineFilament(
                key="long",
                brand="Very Long Brand " * 10,
                material="PLA",
                filament="Extremely Long Filament " * 10,
                variant="Unbelievably Long Variant " * 10,
                color_hex="#ABCDEF",
            )
            long_entry = long_record.to_catalog_entry()
            assert len(long_entry["id"]) <= _MAX_CATALOG_ID_LENGTH, len(long_entry["id"])
            assert re.fullmatch(r"[a-z0-9_]+", long_entry["id"]), long_entry["id"]
            assert long_entry["id"].startswith("ofd_"), long_entry["id"]

    def case_download_limits_and_encoding(case_dir: Path) -> None:
        global _MAX_DATASET_BYTES, _MAX_DOWNLOAD_BYTES
        with _SelfTestHarness(case_dir / "headers") as harness:
            harness.fail_urls.add(DATASET_GZ_URL)
            ensure_dataset()
            plain_headers = harness.request_headers[-1]
            assert "accept-encoding" not in plain_headers, plain_headers
            assert harness.request_headers[-1].get("user-agent") == USER_AGENT, plain_headers
        with _SelfTestHarness(case_dir / "content_encoding") as harness:
            harness.fail_urls.add(DATASET_GZ_URL)
            harness.plain_response = _FakeHttpResponse(
                gzip.compress(_SELFTEST_JSON_BYTES), {"Content-Encoding": "gzip"}
            )
            summary = ensure_dataset()
            assert summary["version"] == _SELFTEST_DATASET["version"], summary
        saved_limits = (_MAX_DATASET_BYTES, _MAX_DOWNLOAD_BYTES)
        try:
            _MAX_DATASET_BYTES = len(_SELFTEST_JSON_BYTES) - 1
            with _SelfTestHarness(case_dir / "too_big_unzipped") as harness:
                harness.fail_urls.add(DATASET_URL)
                _expect_open_filament_error(ensure_dataset, "after decompression")
            _MAX_DATASET_BYTES = saved_limits[0]
            _MAX_DOWNLOAD_BYTES = 16
            with _SelfTestHarness(case_dir / "too_big_download"):
                _expect_open_filament_error(ensure_dataset, "larger than")
        finally:
            _MAX_DATASET_BYTES, _MAX_DOWNLOAD_BYTES = saved_limits
        assert _gunzip_capped(gzip.compress(b"x" * 5000)) == b"x" * 5000
        _expect_open_filament_error(lambda: _gunzip_capped(b"not gzip"), "gzip")
        _expect_open_filament_error(lambda: _gunzip_capped(gzip.compress(b"abc")[:-6]), "gzip")
        assert _REQUEST_TIMEOUT <= 15.0, "downloads must fail fast"

    def case_memory_dataset_ttl(case_dir: Path) -> None:
        global _DATASET_LOADED_AT
        with _SelfTestHarness(case_dir) as harness:
            assert len(search("", limit=10)) == 3
            assert len(harness.calls) == 1, harness.calls
            search("", limit=10)
            assert len(harness.calls) == 1, "a fresh in-memory dataset is reused"
            harness.make_cache_stale()
            _DATASET_LOADED_AT = time.time() - float(CACHE_TTL) * 3600.0 - 60.0
            assert len(search("", limit=10)) == 3
            assert len(harness.calls) == 2, "an expired in-memory dataset is refreshed"
            harness.make_cache_stale()
            harness.fail_all = True
            _DATASET_LOADED_AT = time.time() - float(CACHE_TTL) * 3600.0 - 60.0
            assert len(search("", limit=10)) == 3, "a failed refresh keeps the old dataset"
            calls = len(harness.calls)
            search("", limit=10)
            assert len(harness.calls) == calls, "a failed refresh is not retried on every search"

    def case_brand_from_material(case_dir: Path) -> None:
        dataset = json.loads(_SELFTEST_JSON_BYTES)
        for filament in dataset["filaments"]:
            filament.pop("brand_id", None)
        records = _build_online_filaments(dataset)
        assert {record.brand for record in records} == {"Prusa Polymers", "Generic Filaments"}, records

    def case_get_entries(case_dir: Path) -> None:
        with _SelfTestHarness(case_dir):
            ensure_dataset()
            records = search("", limit=10)
            assert len(records) == 3, records
            keys = [record.key for record in records]
            entries = get_entries(keys)
            assert len(entries) == 3, entries
            assert [entry["color_name"] for entry in entries] == [r.variant for r in records], entries
            assert len({entry["id"] for entry in entries}) == 3, entries
            assert get_entries(["missing/key"]) == []
            assert get_entries([]) == []

    def case_user_catalog(case_dir: Path) -> None:
        with _SelfTestHarness(case_dir):
            assert load_user_catalog() == []
            ensure_dataset()
            entries = get_entries([record.key for record in search("", limit=3)])
            assert len(entries) == 3, entries
            assert append_user_catalog(entries[:2]) == 2
            assert append_user_catalog(entries[:2]) == 0, "re-appending must be idempotent"
            loaded = load_user_catalog()
            assert len(loaded) == 2, loaded
            assert [entry["id"] for entry in loaded] == [entry["id"] for entry in entries[:2]], loaded
            assert append_user_catalog([entries[2]]) == 1
            assert len(load_user_catalog()) == 3
            assert Path(USER_CATALOG_PATH).is_file(), "user catalog file was not written"

            dict_path = case_dir / "dict_catalog.json"
            dict_path.write_text(json.dumps({"filaments": [{"id": "dict_entry"}]}), encoding="utf-8")
            assert load_user_catalog(dict_path) == [{"id": "dict_entry"}]
            list_path = case_dir / "list_catalog.json"
            list_path.write_text(
                json.dumps([{"id": "list_entry", "manufacturer": "M"}]), encoding="utf-8"
            )
            assert load_user_catalog(list_path) == [{"id": "list_entry", "manufacturer": "M"}]
            broken_path = case_dir / "broken_catalog.json"
            broken_path.write_text("{not json", encoding="utf-8")
            assert load_user_catalog(broken_path) == [], "load_user_catalog must never raise"
            scalar_path = case_dir / "scalar_catalog.json"
            scalar_path.write_text("42", encoding="utf-8")
            assert load_user_catalog(scalar_path) == []
            mixed_path = case_dir / "mixed_catalog.json"
            mixed_path.write_text(
                json.dumps({"filaments": [{"no_id": 1}, {"id": ""}, {"id": "valid"}]}),
                encoding="utf-8",
            )
            assert load_user_catalog(mixed_path) == [{"id": "valid"}]
            _expect_open_filament_error(lambda: append_user_catalog([{"no": "id"}]), "string 'id'")
            assert append_user_catalog([]) == 0

    def case_dataset_summary_and_cache_clear(case_dir: Path) -> None:
        with _SelfTestHarness(case_dir) as harness:
            empty = dataset_summary()
            assert empty == {
                "version": "",
                "generated_at": "",
                "brands": 0,
                "materials": 0,
                "filaments": 0,
                "variants": 0,
                "cached": False,
            }, empty
            ensure_dataset()
            summary = dataset_summary()
            assert summary["cached"] is True, summary
            assert summary["version"] == _SELFTEST_DATASET["version"], summary
            assert summary["generated_at"] == _SELFTEST_DATASET["generated_at"], summary
            assert (summary["brands"], summary["materials"], summary["filaments"], summary["variants"]) == (
                2,
                2,
                2,
                3,
            ), summary
            assert cache_clear() is True, "cache_clear must report removal"
            cleared = dataset_summary()
            assert cleared["cached"] is False, cleared
            assert cleared["filaments"] == 0, cleared
            assert cache_clear() is False, "cache_clear must report nothing to clear"
            ensure_dataset()
            assert len(harness.calls) == 2, "dataset must be downloadable after cache_clear"

    return [
        ("ensure_dataset downloads, caches and reuses a fresh cache", case_ensure_dataset_download_and_cache),
        ("stale cache TTL expiry and force=True trigger a refresh", case_ttl_expiry_and_force),
        ("stale cache is used when the network is unavailable", case_stale_offline_fallback),
        ("network failures raise OpenFilamentError and JSON endpoint is the fallback", case_network_errors_and_json_fallback),
        ("search ranks, filters by material and prefers active variants", case_search_ranking_and_filters),
        ("search_materials returns sorted distinct material names", case_search_materials),
        ("to_catalog_entry maps fields, sanitises ids and applies fallbacks", case_to_catalog_entry_mapping),
        ("get_entries resolves result keys in order and skips unknown keys", case_get_entries),
        ("downloads are size-capped and gzip encoding is handled", case_download_limits_and_encoding),
        ("the in-memory dataset is re-checked against the TTL", case_memory_dataset_ttl),
        ("brand falls back to materials.brand_id", case_brand_from_material),
        ("user catalog append is idempotent and load never raises", case_user_catalog),
        ("dataset_summary works offline and cache_clear resets it", case_dataset_summary_and_cache_clear),
    ]


def _run_self_test() -> int:
    """Run the offline self-test; return ``0`` on success and ``1`` on failure."""
    passed = 0
    failed = 0
    previous_level = LOGGER.level
    # Expected failure-path warnings are noisy; keep PASS/FAIL output readable.
    LOGGER.setLevel(logging.ERROR)
    try:
        with tempfile.TemporaryDirectory(prefix="openfilament_selftest_") as temp_dir_name:
            base_dir = Path(temp_dir_name)
            try:
                cases = _self_test_cases(base_dir)
            except Exception as exc:  # pragma: no cover - fixture setup failure
                print(f"FAIL self-test fixture setup: {exc}")
                traceback.print_exc()
                return 1
            for index, (description, case) in enumerate(cases):
                case_dir = base_dir / f"case_{index:02d}"
                try:
                    case(case_dir)
                except Exception as exc:  # noqa: BLE001 - self-test reports every failure
                    failed += 1
                    print(f"FAIL {description}: {exc}")
                    traceback.print_exc()
                else:
                    passed += 1
                    print(f"PASS {description}")
    finally:
        LOGGER.setLevel(previous_level)
    if failed:
        print(f"SELF-TEST FAILED ({failed} check(s) failed, {passed} passed)")
        return 1
    print(f"SELF-TEST PASSED ({passed} checks)")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(_run_self_test())
