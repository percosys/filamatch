"""Snapshot the Fusion Favorites library into a real custom material library.

Fusion 360's material *Favorites* are stored in the user's Autodesk "Common
Material Library" directory as ``Favorites.adsklib`` (a ZIP archive of XML
material/appearance definitions plus binary previews).  Copying that file
verbatim produces a library that Fusion rejects because every material,
appearance and asset identifier has to be unique.  This module:

1. Locates the current ``Favorites.adsklib`` cross-platform (newest version
   directory wins).
2. Re-identifies every UUID in the archive: text/XML UUID strings are replaced
   consistently with fresh uppercase UUIDs and UUID-named directories/files are
   renamed, while binary files are preserved byte-for-byte.
3. Writes the archive atomically into a new stamped build folder,
   ``~/Documents/Filamatch/Filamatch Library builds/<stamp>/Filamatch
   Library.adsklib`` (see :data:`BUILDS_SUFFIX` for why every build gets a new
   folder).
4. Unloads the previously loaded Filamatch library (and the legacy
   "FusionFilament Library"), loads the new build with
   ``Application.materialLibraries.load`` and reports the material count.
5. Deletes older build folders, keeping any whose library could not be
   unloaded.

At palette start an up-to-date build that is already loaded is left alone.
Fusion ignores the preview images inside an ``.adsklib`` (it draws the stock
swatch of each material's template), so they are copied unchanged.

The module imports cleanly outside Fusion (no ``adsk`` at import time), keeps all
Fusion interactions duck-typed/guarded, and ships a fake-object ``__main__``
self-test.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import fusion_helpers as _fh

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: Display name Fusion derives from the generated ``.adsklib`` file name.
LIBRARY_NAME = "Filamatch Library"

#: Library names of earlier versions (FusionFilament Bridge). A loaded library
#: with one of these names is unloaded when the Filamatch library is built.
LEGACY_LIBRARY_NAMES = ("FusionFilament Library",)

#: Environment variable that overrides the generated library path.
LIBRARY_PATH_ENV = "FILAMATCH_LIBRARY_PATH"

#: Each rebuild goes to ``<base stem> builds/<stamp>/<base name>``.  Fusion keeps
#: a loaded library in memory per file path for the whole session, so rewriting
#: the same path shows the old content until a restart; a new folder with the same
#: file name keeps the library name and shows the new content at once.
BUILDS_SUFFIX = " builds"

#: File name Fusion uses for the user's Favorites snapshot.
FAVORITES_FILENAME = "Favorites.adsklib"

#: Name pattern of a build folder (``YYYYmmdd-HHMMSS-<6 hex>``); only folders
#: with this name that hold the library file are ever deleted.
_BUILD_STAMP_PATTERN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")

#: Permissions of a written library (``mkstemp`` creates files as 0600).
_LIBRARY_FILE_MODE = 0o644

#: Maximum number of UUIDs generated per rebuild (defensive bound on odd input).
_MAX_UUID_REPLACEMENTS = 100000

_UUID_PATTERN = re.compile(
    r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f])"
)
_UUID_FULL_PATTERN = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{12}$"
)
_WRITABLE_COMPRESSION_TYPES = frozenset(
    {
        zipfile.ZIP_STORED,
        zipfile.ZIP_DEFLATED,
        zipfile.ZIP_BZIP2,
        zipfile.ZIP_LZMA,
    }
)

__all__ = [
    "LIBRARY_NAME",
    "LIBRARY_PATH_ENV",
    "FAVORITES_FILENAME",
    "CustomLibraryError",
    "CustomLibraryStatus",
    "find_favorites_library",
    "default_library_path",
    "latest_library_path",
    "build_library_file",
    "ensure_custom_library",
    "custom_library_status",
]


class CustomLibraryError(RuntimeError):
    """Raised when the custom material library cannot be built or loaded."""


@dataclass(frozen=True)
class CustomLibraryStatus:
    """Status of the generated custom material library."""

    path: str
    exists: bool
    loaded: bool
    material_count: int
    message: str


# Test hook: the self-test points this at a temporary home directory so it never
# touches the real Autodesk configuration.
_HOME_OVERRIDE: Optional[Path] = None


# ---------------------------------------------------------------------------
# Generic defensive helpers
# ---------------------------------------------------------------------------


_safe_get = _fh.safe_get
_collection_items = _fh.collection_items
_display_name = _fh.display_name


def _remove_file_quietly(path: Optional[Path]) -> None:
    """Delete ``path`` when it exists, ignoring filesystem errors."""
    if path is None:
        return
    try:
        Path(path).unlink()
    except OSError:
        pass


def _home_directory() -> Path:
    """Return the user home directory (test override aware)."""
    if _HOME_OVERRIDE is not None:
        return Path(_HOME_OVERRIDE)
    try:
        return Path.home()
    except (OSError, RuntimeError) as exc:
        raise CustomLibraryError(
            "Could not resolve the user home directory: {}".format(exc)
        ) from exc


# ---------------------------------------------------------------------------
# Autodesk Favorites discovery
# ---------------------------------------------------------------------------


def _favorites_roots() -> List[Path]:
    """Return the platform's Autodesk "Common/Material Library" roots."""
    home = _home_directory()
    if sys.platform == "darwin":
        return [
            home
            / "Library"
            / "Application Support"
            / "Autodesk"
            / "Common"
            / "Material Library"
        ]
    if os.name == "nt":
        roots: List[Path] = []
        appdata = os.environ.get("APPDATA")
        local_appdata = os.environ.get("LOCALAPPDATA")
        if appdata:
            roots.append(Path(appdata) / "Autodesk" / "Common" / "Material Library")
        if local_appdata:
            roots.append(Path(local_appdata) / "Autodesk" / "Common" / "Material Library")
        if not roots:
            roots.append(
                home
                / "AppData"
                / "Roaming"
                / "Autodesk"
                / "Common"
                / "Material Library"
            )
        return roots
    return [
        home / ".local" / "share" / "Autodesk" / "Common" / "Material Library"
    ]


def _favorites_candidates() -> List[Path]:
    """Return every ``<root>/<version>/Favorites.adsklib`` candidate on disk."""
    candidates: List[Path] = []
    for root in _favorites_roots():
        try:
            if root.is_dir():
                candidates.extend(sorted(root.glob("*/" + FAVORITES_FILENAME)))
                direct = root / FAVORITES_FILENAME
                if direct.is_file():
                    candidates.append(direct)
        except OSError:
            logger.exception("Could not scan Autodesk material library root %r.", str(root))
    return candidates


def find_favorites_library() -> Optional[Path]:
    """Locate the newest Fusion 360 ``Favorites.adsklib`` snapshot.

    Searches the platform Autodesk "Common/Material Library" directories for
    ``<version>/Favorites.adsklib`` files and returns the most recently modified
    one.

    Returns:
        The newest Favorites snapshot path, or ``None`` when none exists.
    """
    existing: List[Tuple[float, str, Path]] = []
    for candidate in _favorites_candidates():
        try:
            if candidate.is_file():
                existing.append((candidate.stat().st_mtime, str(candidate), candidate))
        except OSError:
            logger.exception("Could not inspect Favorites candidate %r.", str(candidate))
    if not existing:
        logger.info("No Fusion Favorites.adsklib snapshot was found on this machine.")
        return None
    existing.sort(key=lambda item: (item[0], item[1]))
    chosen = existing[-1][2]
    logger.info("Found Fusion Favorites snapshot at %r.", str(chosen))
    return chosen


def _builds_dir(base: Path) -> Path:
    """Return the folder holding one sub-folder per library build."""
    return base.parent / (base.stem + BUILDS_SUFFIX)


def _base_path(destination: Path) -> Path:
    """Map a build path (as stored in older settings) back to its base path."""
    build_root = destination.parent.parent
    if build_root.name == destination.stem + BUILDS_SUFFIX:
        return build_root.parent / destination.name
    return destination


def _new_build_path(base: Path) -> Path:
    """Return a fresh build path; the file name (and so the library name) stays."""
    stamp = "{}-{}".format(time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
    return _builds_dir(base) / stamp / base.name


def latest_library_path(base: Path) -> Optional[Path]:
    """Return the newest built library for ``base`` (or a legacy file at ``base``)."""
    builds: List[Path] = []
    try:
        if _builds_dir(base).is_dir():
            builds = sorted(
                candidate
                for candidate in _builds_dir(base).glob("*/" + base.name)
                if candidate.is_file()
            )
        if builds:
            return builds[-1]
        return base if base.is_file() else None
    except OSError:
        logger.exception("Could not scan the custom library builds for %r.", str(base))
        return None


def _is_build_folder(folder: Path, base: Path) -> bool:
    """Return whether ``folder`` is a stamp-named build holding the library file."""
    return (
        _BUILD_STAMP_PATTERN.match(folder.name) is not None
        and folder.is_dir()
        and (folder / base.name).is_file()
    )


def _remove_old_builds(base: Path, keep: Path, protected: Sequence[Path] = ()) -> int:
    """Delete older builds of ``base`` (and a legacy file at ``base``).

    Only stamp-named build folders that contain the library file are deleted,
    never ``keep``'s folder or a ``protected`` one (a build Fusion still has
    loaded); anything else a user put into the builds folder stays.
    """
    removed = 0
    kept = {keep.parent} | {Path(path).parent for path in protected}
    try:
        builds_dir = _builds_dir(base)
        if builds_dir.is_dir():
            for folder in builds_dir.iterdir():
                if folder in kept or not _is_build_folder(folder, base):
                    continue
                shutil.rmtree(str(folder), ignore_errors=True)
                removed += 1
        if base != keep and base.is_file():
            base.unlink()
            removed += 1
    except OSError:
        logger.exception("Could not remove old custom library builds of %r.", str(base))
    return removed


def default_library_path() -> Path:
    """Return the generated custom library path.

    Uses ``FILAMATCH_LIBRARY_PATH`` when set, otherwise
    ``~/Documents/Filamatch/Filamatch Library.adsklib``.  The
    parent directory is created when the library is built, not here.
    """
    override = os.environ.get(LIBRARY_PATH_ENV)
    if isinstance(override, str) and override.strip():
        return Path(override.strip()).expanduser()
    return (
        _home_directory()
        / "Documents"
        / "Filamatch"
        / (LIBRARY_NAME + ".adsklib")
    )


# ---------------------------------------------------------------------------
# ZIP re-identification
# ---------------------------------------------------------------------------


def _clone_zip_info(info: zipfile.ZipInfo, new_name: str) -> zipfile.ZipInfo:
    """Return a ``ZipInfo`` with ``new_name`` preserving the original metadata."""
    clone = zipfile.ZipInfo(new_name, info.date_time)
    clone.compress_type = _writable_compression_type(info)
    clone.external_attr = info.external_attr
    clone.create_system = info.create_system
    clone.comment = info.comment
    return clone


def _writable_compression_type(info: zipfile.ZipInfo) -> int:
    """Return a compression type usable when writing the regenerated archive."""
    if info.compress_type in _WRITABLE_COMPRESSION_TYPES:
        return info.compress_type
    return zipfile.ZIP_DEFLATED


def _is_safe_archive_name(name: str) -> bool:
    """Reject absolute paths and parent-directory escapes in archive entries."""
    text = name.replace("\\", "/")
    if text.startswith("/"):
        return False
    parts = [part for part in text.split("/") if part not in ("", ".")]
    return all(part != ".." for part in parts)


def _decode_text(data: bytes) -> Optional[str]:
    """Return UTF-8 text for a plausible text/XML entry, or ``None`` for binary.

    Binary files are detected by NUL bytes or a failed UTF-8 decode; those are
    copied byte-for-byte without re-identification.
    """
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _new_uuid() -> str:
    """Return a fresh uppercase UUID string."""
    return str(uuid.uuid4()).upper()


def _collect_uuid_map(
    entries: Sequence[Tuple[zipfile.ZipInfo, bytes]]
) -> Dict[str, str]:
    """Build a consistent lowercase-old-UUID to fresh-uppercase-UUID mapping."""
    mapping: Dict[str, str] = {}

    def register(value: str) -> None:
        key = value.lower()
        if key in mapping:
            return
        if len(mapping) >= _MAX_UUID_REPLACEMENTS:
            raise CustomLibraryError(
                "The Favorites library contains more than {} UUIDs; refusing to "
                "re-identify an unexpectedly large archive.".format(
                    _MAX_UUID_REPLACEMENTS
                )
            )
        mapping[key] = _new_uuid()

    for info, data in entries:
        for part in info.filename.rstrip("/").split("/"):
            if _UUID_FULL_PATTERN.match(part):
                register(part)
        text = _decode_text(data)
        if text is None:
            continue
        for match in _UUID_PATTERN.finditer(text):
            register(match.group(0))
    return mapping


def _replace_uuids(text: str, mapping: Dict[str, str]) -> str:
    """Replace every UUID in ``text`` using the consistent mapping."""

    def replace(match: "re.Match[str]") -> str:
        return mapping[match.group(0).lower()]

    return _UUID_PATTERN.sub(replace, text)


def _remap_entry_name(name: str, mapping: Dict[str, str]) -> str:
    """Rename UUID path components of an archive entry consistently."""
    is_directory = name.endswith("/")
    parts = name.rstrip("/").split("/")
    remapped = [
        mapping.get(part.lower(), part) if _UUID_FULL_PATTERN.match(part) else part
        for part in parts
    ]
    result = "/".join(remapped)
    return result + "/" if is_directory else result


def _validate_source_library(source: Path) -> None:
    """Validate that ``source`` exists and is a readable ZIP (adsklib) file."""
    try:
        if not source.is_file():
            raise CustomLibraryError(
                "Favorites material library not found: {!r}".format(str(source))
            )
    except OSError as exc:
        raise CustomLibraryError(
            "Could not inspect Favorites material library {!r}: {}".format(
                str(source), exc
            )
        ) from exc
    try:
        if not zipfile.is_zipfile(str(source)):
            raise CustomLibraryError(
                "Favorites material library {!r} is not a valid .adsklib (ZIP) "
                "archive.".format(str(source))
            )
    except OSError as exc:
        raise CustomLibraryError(
            "Could not read Favorites material library {!r}: {}".format(
                str(source), exc
            )
        ) from exc


def _read_library_entries(
    source: Path,
) -> Tuple[List[Tuple[zipfile.ZipInfo, bytes]], bytes]:
    """Read every source archive entry plus the archive comment."""
    entries: List[Tuple[zipfile.ZipInfo, bytes]] = []
    try:
        with zipfile.ZipFile(str(source), "r") as archive:
            archive_comment = archive.comment
            for info in archive.infolist():
                if not _is_safe_archive_name(info.filename):
                    raise CustomLibraryError(
                        "Favorites material library contains an unsafe entry name "
                        "{!r}; refusing to copy it.".format(info.filename)
                    )
                data = b"" if info.filename.endswith("/") else archive.read(info)
                entries.append((info, data))
    except zipfile.BadZipFile as exc:
        raise CustomLibraryError(
            "Favorites material library {!r} is not a valid .adsklib (ZIP) "
            "archive: {}".format(str(source), exc)
        ) from exc
    except OSError as exc:
        raise CustomLibraryError(
            "Could not read Favorites material library {!r}: {}".format(
                str(source), exc
            )
        ) from exc
    if not entries:
        raise CustomLibraryError(
            "Favorites material library {!r} is empty; nothing to snapshot.".format(
                str(source)
            )
        )
    return entries, archive_comment


def _verify_reidentified_library(
    source_entries: Sequence[Tuple[zipfile.ZipInfo, bytes]],
    mapping: Dict[str, str],
    destination: Path,
) -> None:
    """Verify the regenerated archive contains the expected renamed entries."""
    try:
        with zipfile.ZipFile(str(destination), "r") as archive:
            names = archive.namelist()
            if len(names) != len(source_entries):
                raise CustomLibraryError(
                    "Regenerated material library {!r} has {} entries instead of "
                    "{}.".format(str(destination), len(names), len(source_entries))
                )
            expected_names = [
                _remap_entry_name(info.filename, mapping)
                for info, _data in source_entries
            ]
            if names != expected_names:
                raise CustomLibraryError(
                    "Regenerated material library {!r} does not preserve the "
                    "expected relative paths.".format(str(destination))
                )
            expected_by_name = dict(zip(expected_names, source_entries))
            old_uuid_keys = set(mapping)
            for name in names:
                info, data = expected_by_name[name]
                written = archive.read(name)
                text = _decode_text(data)
                expected_data = (
                    data if text is None else _replace_uuids(text, mapping).encode("utf-8")
                )
                if written != expected_data:
                    raise CustomLibraryError(
                        "Regenerated material library entry {!r} does not match the "
                        "expected re-identified content.".format(name)
                    )
                if (
                    text is not None
                    and any(
                        match.group(0).lower() in old_uuid_keys
                        for match in _UUID_PATTERN.finditer(written.decode("utf-8"))
                    )
                ):
                    raise CustomLibraryError(
                        "Regenerated material library entry {!r} still contains an "
                        "old UUID.".format(name)
                    )
    except zipfile.BadZipFile as exc:
        raise CustomLibraryError(
            "Regenerated material library {!r} is not a readable ZIP archive: "
            "{}".format(str(destination), exc)
        ) from exc
    except OSError as exc:
        raise CustomLibraryError(
            "Could not verify regenerated material library {!r}: {}".format(
                str(destination), exc
            )
        ) from exc


def _reidentify_library(source: Path, destination: Path) -> Path:
    """Copy ``source`` into ``destination`` with all UUIDs re-identified.

    Text/XML entries get fresh uppercase UUIDs (consistent across the whole
    archive) and UUID path components are renamed; binary entries (including
    the preview images, which Fusion ignores) are preserved byte-for-byte.  The
    destination is written atomically and made world-readable (0644).

    Args:
        source: Existing ``Favorites.adsklib`` file.
        destination: Output ``.adsklib`` path.

    Returns:
        The destination path.

    Raises:
        CustomLibraryError: If the source is unusable or the copy fails.
    """
    source_path = Path(source)
    destination_path = Path(destination)
    _validate_source_library(source_path)
    try:
        if source_path.resolve() == destination_path.resolve():
            raise CustomLibraryError(
                "Source and destination material libraries must be different files "
                "({!r}).".format(str(source_path))
            )
    except OSError:
        logger.debug("Could not compare source/destination paths.", exc_info=True)
    entries, archive_comment = _read_library_entries(source_path)
    mapping = _collect_uuid_map(entries)

    temporary_path: Optional[Path] = None
    try:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".filamatch_adsklib_", suffix=".adsklib", dir=str(destination_path.parent)
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)
        with zipfile.ZipFile(str(temporary_path), "w") as target:
            target.comment = archive_comment
            for info, data in entries:
                new_name = _remap_entry_name(info.filename, mapping)
                text = _decode_text(data)
                new_data = data if text is None else _replace_uuids(text, mapping).encode("utf-8")
                target.writestr(_clone_zip_info(info, new_name), new_data)
        _verify_reidentified_library(entries, mapping, temporary_path)
        os.chmod(str(temporary_path), _LIBRARY_FILE_MODE)
        os.replace(str(temporary_path), str(destination_path))
    except CustomLibraryError:
        raise
    except OSError as exc:
        raise CustomLibraryError(
            "Could not write the regenerated material library {!r}: {}".format(
                str(destination_path), exc
            )
        ) from exc
    finally:
        _remove_file_quietly(temporary_path)
    logger.info(
        "Regenerated material library %r from Favorites snapshot %r (%d entries, "
        "%d UUIDs re-identified).",
        str(destination_path),
        str(source_path),
        len(entries),
        len(mapping),
    )
    return destination_path


def build_library_file(
    source: Path,
    destination: Path,
    thumbnail_colors: Optional[Dict[str, str]] = None,
) -> Path:
    """Build a re-identified custom ``.adsklib`` from a Favorites snapshot.

    The build always runs (explicit refresh semantics); use
    :func:`ensure_custom_library`'s ``force`` argument for skip logic.

    Args:
        source: Existing ``Favorites.adsklib`` snapshot.
        destination: Output library path (parent directories are created).
        thumbnail_colors: Ignored; kept for compatibility.  Fusion never shows
            the preview images inside a library, so they are not recoloured.

    Returns:
        The destination path.

    Raises:
        CustomLibraryError: If the source is missing/not a ZIP, the destination
            cannot be written, or the regenerated archive fails verification.
    """
    if source is None:
        raise CustomLibraryError("build_library_file requires a source path, got None")
    if destination is None:
        raise CustomLibraryError(
            "build_library_file requires a destination path, got None"
        )
    if thumbnail_colors:
        logger.debug("thumbnail_colors is ignored: Fusion does not show library previews.")
    return _reidentify_library(Path(source), Path(destination))


# ---------------------------------------------------------------------------
# Fusion library loading
# ---------------------------------------------------------------------------


def _library_path_text(library: Any) -> Optional[str]:
    """Return a loaded library's file path when the Fusion API exposes one."""
    for attribute in ("path", "filePath", "filename"):
        value = _safe_get(library, attribute, "a material library")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _library_matches(library: Any, destination: Path) -> bool:
    """Return whether a loaded library is the custom library at ``destination``."""
    name = _display_name(library, "name", "a material library", "")
    desired_name = destination.stem
    if name and name in (LIBRARY_NAME, desired_name):
        return True
    path_text = _library_path_text(library)
    if path_text:
        try:
            loaded_path = Path(path_text).expanduser().resolve()
            if loaded_path == destination.resolve():
                return True
            # Any build of this library (``<builds>/<stamp>/<name>``).
            if loaded_path.parent.parent == _builds_dir(_base_path(destination)).resolve():
                return True
        except OSError:
            logger.debug("Could not compare loaded library paths.", exc_info=True)
    return False


def _same_library(first: Any, second: Any) -> bool:
    """Return whether two library objects are the same Fusion library.

    Fusion returns a new Python wrapper on every access, so ``is`` is never
    true for a library read twice; Fusion objects compare with ``==``.
    """
    if first is second:
        return True
    try:
        return bool(first == second)
    except Exception:
        return False


def _unload_library(library: Any, material_libraries: Any) -> bool:
    """Unload a loaded material library, tolerating API differences."""
    # Read the name first: an unloaded library can no longer report it.
    name = _display_name(library, "name", "a material library", "<unnamed>")
    unload = _safe_get(library, "unload", "a material library")
    if callable(unload):
        try:
            unload()
            logger.info("Unloaded previously loaded material library %r.", name)
            return True
        except Exception:
            logger.exception("Could not unload a previously loaded material library.")
    collection_unload = _safe_get(material_libraries, "unload", "material libraries")
    if callable(collection_unload):
        try:
            collection_unload(library)
            return True
        except Exception:
            logger.exception(
                "Could not unload the material library through the collection."
            )
    logger.warning(
        "Unable to unload the previously loaded material library %r; Fusion may "
        "refuse to reload the regenerated file until it is unloaded manually.",
        name,
    )
    return False


def _unload_matching_libraries(
    material_libraries: Any, destination: Path, keep: Any = None
) -> Tuple[int, List[Any]]:
    """Unload every loaded library matching the custom library name/path.

    Legacy library names are unloaded too.  ``keep`` (a loaded library) is left
    alone.  Returns ``(unloaded count, libraries that could not be unloaded)``.
    """
    unloaded = 0
    failed: List[Any] = []
    for library in _collection_items(material_libraries, "material libraries"):
        if keep is not None and _same_library(library, keep):
            continue
        name = _display_name(library, "name", "a material library", "")
        if not (_library_matches(library, destination) or name in LEGACY_LIBRARY_NAMES):
            continue
        if _unload_library(library, material_libraries):
            unloaded += 1
        else:
            failed.append(library)
    return unloaded, failed


def _loaded_library(material_libraries: Any, destination: Path) -> Any:
    """Return the loaded Filamatch library when it is the build at ``destination``.

    Fusion does not expose a library's file path in every version: a library
    without one is accepted by name, which is safe because older builds are
    deleted after every successful load.
    """
    for library in _collection_items(material_libraries, "material libraries"):
        name = _display_name(library, "name", "a material library", "")
        path_text = _library_path_text(library)
        if path_text:
            try:
                if Path(path_text).expanduser().resolve() == destination.resolve():
                    return library
            except OSError:
                logger.debug("Could not compare loaded library paths.", exc_info=True)
            continue
        if name in (LIBRARY_NAME, destination.stem):
            return library
    return None


def _clear_template_cache() -> None:
    """Forget importer template lookups after the loaded libraries changed."""
    try:
        from . import importer

        importer.clear_template_cache()
    except Exception:
        logger.debug("Could not clear the importer template cache.", exc_info=True)


def _count_library_materials(library: Any) -> int:
    """Return the material count of a loaded material library."""
    materials = _safe_get(library, "materials", "a material library")
    if materials is None:
        return 0
    count_value = _safe_get(materials, "count", "material library materials")
    if isinstance(count_value, int) and not isinstance(count_value, bool):
        return max(0, count_value)
    return len(_collection_items(materials, "material library materials"))


def _library_material_names(library: Any) -> List[str]:
    """Return the names of a loaded library's materials (for the status message)."""
    materials = _safe_get(library, "materials", "a material library")
    names: List[str] = []
    for material in _collection_items(materials, "material library materials"):
        name = _display_name(material, "name", "a material", "")
        if name:
            names.append(name)
    return names


def _should_rebuild(
    source: Path, destination: Path, force: bool
) -> bool:
    """Return whether the destination snapshot should be regenerated."""
    if force:
        return True
    try:
        if not destination.is_file():
            return True
        return destination.stat().st_mtime < source.stat().st_mtime
    except OSError:
        logger.exception("Could not compare material library timestamps.")
        return True


def _failed_build_error(
    destination: Path, error: BaseException, unloaded_count: int = 0
) -> CustomLibraryError:
    """Build the actionable error for a failed rebuild (nothing was unloaded)."""
    return CustomLibraryError(
        "Could not regenerate the custom material library {!r}: {}. The previously "
        "loaded {!r} library (if any) stays loaded. Press Create again to retry; the "
        "snapshot will be rebuilt from the current Fusion Favorites library.".format(
            str(destination), error, LIBRARY_NAME
        )
    )


def ensure_custom_library(
    app: Any, destination: Optional[Path] = None, force: bool = True
) -> CustomLibraryStatus:
    """Build and load the custom material library from the Favorites snapshot.

    Order: resolve the Favorites source and destination, build a new copy in a
    fresh build folder (see :data:`BUILDS_SUFFIX`), unload the previously
    loaded custom library, load the new build, then delete older builds except
    those Fusion could not unload.  A failed build changes nothing in Fusion.
    When no rebuild is needed and that build is already loaded, nothing is
    unloaded or loaded again.

    Args:
        app: Fusion ``Application`` exposing ``materialLibraries``.
        destination: Base ``.adsklib`` path; defaults to
            :func:`default_library_path`.  Builds live next to it.
        force: Rebuild even when the destination is newer than the Favorites
            snapshot (``True`` by default — this is an explicit refresh).

    Returns:
        A :class:`CustomLibraryStatus` describing the loaded library.

    Raises:
        CustomLibraryError: If Favorites cannot be found, Fusion cannot load the
            library, or the rebuild fails.
    """
    started = time.monotonic()
    source = find_favorites_library()
    if source is None:
        raise CustomLibraryError(
            "No Fusion Favorites material library snapshot was found. Open Fusion "
            "360's Material Browser, add at least one filament material to Favorites "
            "and make sure the Favorites library has been saved (expected file: "
            "Autodesk/Common/Material Library/<version>/Favorites.adsklib), then "
            "press Create again."
        )
    base_path = _base_path(
        Path(destination) if destination is not None else default_library_path()
    )
    if app is None:
        raise CustomLibraryError(
            "The custom material library can only be loaded inside Fusion 360; the "
            "Fusion Application object is required."
        )
    material_libraries = _safe_get(app, "materialLibraries", "the Fusion application")
    if material_libraries is None:
        raise CustomLibraryError(
            "The Fusion application exposes no materialLibraries collection; custom "
            "material libraries cannot be loaded."
        )

    current = latest_library_path(base_path)
    rebuild = current is None or _should_rebuild(source, current, force)
    destination_path = _new_build_path(base_path) if rebuild else current
    unloaded_count = 0
    if rebuild:
        try:
            with _fh.timed(logger, "Custom library build"):
                build_library_file(source, destination_path)
        except CustomLibraryError as exc:
            shutil.rmtree(str(destination_path.parent), ignore_errors=True)
            raise _failed_build_error(destination_path, exc) from exc
        loaded = None
    else:
        loaded = _loaded_library(material_libraries, destination_path)

    if loaded is not None:
        logger.info(
            "The custom material library %r is current and already loaded; not reloading.",
            str(destination_path),
        )
        _unload_matching_libraries(material_libraries, base_path, keep=loaded)
        failed: List[Any] = []
    else:
        if not rebuild:
            logger.info(
                "Using the existing custom material library %r (newer than the Favorites "
                "snapshot).",
                str(destination_path),
            )
        with _fh.timed(logger, "Custom library unload"):
            # The base path matches a library loaded from the pre-builds location.
            unloaded_count, failed = _unload_matching_libraries(material_libraries, base_path)
        try:
            with _fh.timed(logger, "Custom library load"):
                loaded = material_libraries.load(str(destination_path))
        except Exception as exc:
            logger.exception(
                "Fusion could not load the regenerated material library %r.",
                str(destination_path),
            )
            raise CustomLibraryError(
                "Fusion could not load the regenerated material library {!r}: {}. If a "
                "library with this name is still loaded, unload it in the Material "
                "Browser and press Create again.".format(str(destination_path), exc)
            ) from exc
        finally:
            _clear_template_cache()
        if loaded is None:
            raise CustomLibraryError(
                "Fusion's MaterialLibraries.load returned null for {!r}; the custom "
                "material library was not loaded. Press Create again or load the "
                ".adsklib manually from the Material Browser.".format(str(destination_path))
            )
    if rebuild:
        protected = [Path(path) for path in map(_library_path_text, failed) if path]
        if len(protected) < len(failed):
            logger.warning(
                "%d old custom library build(s) could not be unloaded and Fusion does not "
                "report their path; keeping every older build.",
                len(failed),
            )
        else:
            removed = _remove_old_builds(base_path, keep=destination_path, protected=protected)
            if removed:
                logger.info("Removed %d older custom library build(s).", removed)
    material_count = _count_library_materials(loaded)
    material_names = _library_material_names(loaded)
    message_parts = [
        "Custom material library {!r} {} the current Fusion Favorites snapshot and "
        "loaded with {} material(s).".format(
            str(destination_path), "built from" if rebuild else "matches", material_count
        ),
        "Snapshot only: material categories are not preserved. Each swatch shows "
        "the colour of the stock material its favorite was copied from.",
    ]
    if unloaded_count:
        message_parts.append(
            "Replaced {} previously loaded copy/copies of the library.".format(
                unloaded_count
            )
        )
    if failed:
        message_parts.append(
            "{} older copy/copies could not be unloaded; unload them in the Material "
            "Browser.".format(len(failed))
        )
    if material_names:
        preview = ", ".join(material_names[:5])
        if len(material_names) > 5:
            preview += ", ..."
        logger.info("Custom library materials: %s", preview)
    logger.info("Custom library ready in %.2f s.", time.monotonic() - started)
    return CustomLibraryStatus(
        path=str(destination_path),
        exists=True,
        loaded=True,
        material_count=material_count,
        message=" ".join(message_parts),
    )


def custom_library_status(
    app: Any, destination: Optional[Path] = None
) -> CustomLibraryStatus:
    """Report the generated custom library without building anything.

    Args:
        app: Fusion ``Application`` (may be ``None`` outside Fusion).
        destination: Library path; defaults to :func:`default_library_path`.

    Returns:
        A :class:`CustomLibraryStatus` with ``exists``/``loaded``/``material_count``
        and a human readable message.
    """
    base_path = _base_path(
        Path(destination) if destination is not None else default_library_path()
    )
    latest = latest_library_path(base_path)
    destination_path = latest or base_path
    exists = latest is not None

    loaded = False
    material_count = 0
    if app is not None:
        material_libraries = _safe_get(app, "materialLibraries", "the Fusion application")
        for library in _collection_items(material_libraries, "material libraries"):
            if _library_matches(library, destination_path):
                loaded = True
                material_count = _count_library_materials(library)
                break

    if not exists:
        message = (
            "The Filamatch custom material library has not been built "
            "yet; press Create to snapshot the current Favorites library."
        )
    elif loaded:
        message = (
            "Custom material library is loaded with {} material(s). It is a snapshot "
            "of the current Fusion Favorites library; categories are not preserved "
            "and previews are generated by Fusion.".format(material_count)
        )
    else:
        message = (
            "Custom material library exists at {!r} but is not loaded in Fusion; "
            "press Create to rebuild and load it.".format(str(destination_path))
        )
    return CustomLibraryStatus(
        path=str(destination_path),
        exists=exists,
        loaded=loaded,
        material_count=material_count,
        message=message,
    )


# ---------------------------------------------------------------------------
# Self-test doubles and checks (only used by the __main__ self-test)
# ---------------------------------------------------------------------------

_OLD_SHARED = "1d9a2e5f-4b3c-4a6d-9e8f-0a1b2c3d4e5f"
_OLD_OTHER = "2e8b3f6a-5c4d-4b7e-8f90-1b2c3d4e5f60"
_OLD_THIRD = "3f7c4a5b-6d5e-4c8f-9a01-2c3d4e5f6071"
_OLD_DIR = "4a6b5c7d-8e9f-4d0a-b123-3d4e5f607182"


def _write_favorites_fixture(path: Path) -> Path:
    """Write a synthetic Autodesk ``Favorites.adsklib`` ZIP fixture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        (
            "{}/appearance.xml".format(_OLD_DIR),
            (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<Appearance id="{}"><Preview>{}</Preview></Appearance>'.format(
                    _OLD_SHARED.upper(), _OLD_THIRD
                )
            ).encode("utf-8"),
        ),
        (
            "{}/".format(_OLD_DIR),
            b"",
        ),
        ("previews/", b""),
        ("previews/{}/".format(_OLD_DIR), b""),
        (
            "previews/{}/thumbnail.png".format(_OLD_DIR),
            b"\x89PNG\r\n\x1a\n"
            + _OLD_SHARED.encode("utf-8")
            + b"\x00\x01"
            + _OLD_OTHER.encode("utf-8"),
        ),
        (
            "materialLibrary.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<MaterialLibrary>"
                '<Material id="{}" name="Black PLA">'
                '<Appearance id="{}"/>'
                "</Material></MaterialLibrary>".format(
                    _OLD_SHARED.lower(), _OLD_OTHER
                )
            ).encode("utf-8"),
        ),
        (
            "assets/schema.bin",
            b"\x00\xff" + _OLD_OTHER.encode("utf-8") + b"\x00",
        ),
    ]
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            info = zipfile.ZipInfo(name, (2024, 1, 2, 3, 4, 6))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return path


def _read_entries(path: Path) -> List[Tuple[str, bytes]]:
    """Read a ZIP fixture into ``(name, bytes)`` pairs."""
    with zipfile.ZipFile(str(path), "r") as archive:
        return [(info.filename, archive.read(info.filename)) for info in archive.infolist()]


def _text_of(data: bytes) -> Optional[str]:
    """Decode a fixture entry as text when it is one."""
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _uuids_in(text: str) -> List[str]:
    """Return the UUIDs found in ``text``."""
    return [match.group(0) for match in _UUID_PATTERN.finditer(text)]


def _expect(condition: bool, message: str) -> None:
    """Raise AssertionError with ``message`` when ``condition`` is false."""
    if not condition:
        raise AssertionError(message)


def _set_home(path: Optional[Path]) -> None:
    """Set the module's home-directory test hook."""
    global _HOME_OVERRIDE
    _HOME_OVERRIDE = path


class FakeMaterialEntry:
    """Test double for a material inside a loaded library."""

    def __init__(self, name: str) -> None:
        self.name = name


class FakeLibraryMaterials:
    """Test double for a material library's materials collection."""

    def __init__(self, count: int) -> None:
        self._count = count

    @property
    def count(self) -> int:
        """Number of materials in the loaded library."""
        return self._count

    def item(self, index: int) -> Any:
        """Return the material at ``index``, or ``None``."""
        if 0 <= index < self._count:
            return FakeMaterialEntry("Material {}".format(index + 1))
        return None


class FakeLoadedLibrary:
    """Test double for a loaded ``MaterialLibrary``."""

    def __init__(
        self,
        name: str,
        path: Optional[str] = None,
        material_count: int = 21,
        events: Optional[List[str]] = None,
    ) -> None:
        self.name = name
        self.path = path
        self.materials = FakeLibraryMaterials(material_count)
        self.unloaded = False
        self._events = events
        self._collection: Optional["FakeMaterialLibraries"] = None

    def unload(self) -> None:
        """Record and perform the unload."""
        self.unloaded = True
        events = (
            self._collection.events
            if self._collection is not None
            else self._events
        )
        if events is not None:
            events.append("unload:" + self.name)
        if self._collection is not None:
            self._collection.remove(self)


class FakeMaterialLibraries:
    """Test double for ``app.materialLibraries``."""

    def __init__(
        self,
        libraries: Optional[Sequence[FakeLoadedLibrary]] = None,
        events: Optional[List[str]] = None,
        material_count: int = 21,
    ) -> None:
        self._libraries = list(libraries or [])
        self.events = events if events is not None else []
        self.material_count = material_count
        self.load_calls: List[str] = []
        for library in self._libraries:
            library._collection = self

    @property
    def count(self) -> int:
        """Number of currently loaded material libraries."""
        return len(self._libraries)

    def item(self, index: int) -> Any:
        """Return the library at ``index``, or ``None``."""
        if 0 <= index < len(self._libraries):
            return self._libraries[index]
        return None

    def load(self, filename: str) -> FakeLoadedLibrary:
        """Record the load and return a fake loaded library."""
        self.events.append("load")
        self.load_calls.append(str(filename))
        library = FakeLoadedLibrary(
            Path(filename).stem,
            path=str(filename),
            material_count=self.material_count,
            events=self.events,
        )
        library._collection = self
        self._libraries.append(library)
        return library

    def remove(self, library: FakeLoadedLibrary) -> None:
        """Remove ``library`` from the collection when present."""
        if library in self._libraries:
            self._libraries.remove(library)


class FakeApp:
    """Test double for the Fusion ``Application``."""

    def __init__(self, material_libraries: Optional[FakeMaterialLibraries] = None) -> None:
        self.materialLibraries = (
            material_libraries if material_libraries is not None else FakeMaterialLibraries()
        )


class _RecordingBuild:
    """Context manager that records ``build_library_file`` calls and ordering."""

    def __init__(self, events: List[str], error: Optional[BaseException] = None) -> None:
        self.events = events
        self.error = error
        self.calls: List[Tuple[Path, Path]] = []
        self._original: Optional[Callable[[Path, Path], Path]] = None

    def __enter__(self) -> "_RecordingBuild":
        self._original = globals()["build_library_file"]
        globals()["build_library_file"] = self._build
        return self

    def _build(
        self,
        source: Path,
        destination: Path,
        thumbnail_colors: Optional[Dict[str, str]] = None,
    ) -> Path:
        """Record the build, then delegate (or raise the configured error)."""
        self.events.append("build")
        self.calls.append((Path(source), Path(destination)))
        if self.error is not None:
            raise self.error
        if self._original is None:  # pragma: no cover - defensive
            raise CustomLibraryError("build_library_file is unavailable")
        return self._original(
            source, destination, thumbnail_colors=thumbnail_colors
        )

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._original is not None:
            globals()["build_library_file"] = self._original
        return False


class _FreshWrapper:
    """Mimics Fusion: every read of a collection item is a new Python object."""

    def __init__(self, library: FakeLoadedLibrary) -> None:
        object.__setattr__(self, "_library", library)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._library, name)

    def __eq__(self, other: Any) -> bool:
        return self._library is getattr(other, "_library", other)

    __hash__ = None  # type: ignore[assignment]


class _FreshWrapperLibraries(FakeMaterialLibraries):
    """``materialLibraries`` that hands out a new wrapper on every ``item``."""

    def item(self, index: int) -> Any:
        library = super().item(index)
        return _FreshWrapper(library) if library is not None else None


def _check_keep_survives_new_wrappers(tmp_dir: Path) -> None:
    """The library to keep is recognised although Fusion returns new wrappers."""
    base = tmp_dir / "keep" / (LIBRARY_NAME + ".adsklib")
    current = _builds_dir(base) / "20261107-102039-477092" / base.name
    old = _builds_dir(base) / "20261106-101010-abcdef" / base.name
    current_library = FakeLoadedLibrary(LIBRARY_NAME, path=str(current))
    old_library = FakeLoadedLibrary(LIBRARY_NAME, path=str(old))
    libraries = _FreshWrapperLibraries([current_library, old_library])
    keep = libraries.item(0)
    unloaded, failed = _unload_matching_libraries(libraries, base, keep=keep)
    _expect(not current_library.unloaded, "the library to keep must stay loaded")
    _expect(old_library.unloaded and unloaded == 1 and not failed, "older builds are unloaded")


def _check_find_and_default_paths(tmp_dir: Path) -> None:
    """Favorites discovery and the default/env library paths work."""
    home = tmp_dir / "home"
    previous_home = _HOME_OVERRIDE
    try:
        _set_home(home)
        roots = _favorites_roots()
        _expect(bool(roots), "at least one Favorites root must be defined")
        root = roots[0]
        older = _write_favorites_fixture(root / "2024" / FAVORITES_FILENAME)
        newer = _write_favorites_fixture(root / "2025" / FAVORITES_FILENAME)
        try:
            os.utime(str(older), (1000.0, 1000.0))
            os.utime(str(newer), (2000.0, 2000.0))
        except OSError:
            logger.debug("Could not set fixture mtimes.", exc_info=True)
        found = find_favorites_library()
        _expect(found == newer, "the newest Favorites snapshot must be selected")

        empty_home = tmp_dir / "empty_home"
        _set_home(empty_home)
        _expect(
            find_favorites_library() is None,
            "find_favorites_library must return None when no snapshot exists",
        )

        _set_home(home)
        previous_override = os.environ.pop(LIBRARY_PATH_ENV, None)
        try:
            default_path = default_library_path()
            expected = (
                home
                / "Documents"
                / "Filamatch"
                / (LIBRARY_NAME + ".adsklib")
            )
            _expect(default_path == expected, "default library path mismatch")
            custom_path = tmp_dir / "custom" / "Special.adsklib"
            os.environ[LIBRARY_PATH_ENV] = str(custom_path)
            _expect(
                default_library_path() == custom_path,
                "the library path environment override must be honoured",
            )
        finally:
            os.environ.pop(LIBRARY_PATH_ENV, None)
            if previous_override is not None:
                os.environ[LIBRARY_PATH_ENV] = previous_override
    finally:
        _set_home(previous_home)


def _check_build_library(tmp_dir: Path) -> None:
    """Building re-identifies text UUIDs consistently and preserves binaries."""
    source = _write_favorites_fixture(tmp_dir / "source" / FAVORITES_FILENAME)
    destination = tmp_dir / "out" / (LIBRARY_NAME + ".adsklib")
    result = build_library_file(source, destination)
    _expect(result == destination, "build_library_file must return the destination")
    _expect(destination.is_file(), "the regenerated library must exist")
    _expect(zipfile.is_zipfile(str(destination)), "the regenerated library must be a ZIP")
    _expect(
        not list(destination.parent.glob(".ffb_adsklib_*")),
        "no temporary build files may be left behind",
    )

    second_destination = tmp_dir / "out" / "Second Library.adsklib"
    _expect(
        _reidentify_library(source, second_destination) == second_destination,
        "_reidentify_library must return the destination",
    )
    _expect(
        _read_entries(second_destination)[0][0] != _read_entries(source)[0][0],
        "a rebuild must generate fresh identifiers",
    )

    source_entries = _read_entries(source)
    destination_entries = _read_entries(destination)
    _expect(
        len(destination_entries) == len(source_entries),
        "every source entry must be preserved",
    )

    old_keys = {
        value.lower()
        for value in (_OLD_SHARED, _OLD_OTHER, _OLD_THIRD, _OLD_DIR)
    }
    material_uuids: List[str] = []
    appearance_uuids: List[str] = []
    for name, data in destination_entries:
        text = _text_of(data)
        if text is None:
            continue
        found = _uuids_in(text)
        for found_uuid in found:
            _expect(found_uuid == found_uuid.upper(), "new UUIDs must be uppercase")
            _expect(
                found_uuid.lower() not in old_keys,
                "text entry {!r} still contains an old UUID".format(name),
            )
        if "MaterialLibrary" in text:
            material_uuids = found
        elif "Appearance" in text:
            appearance_uuids = found
    _expect(len(material_uuids) == 2, "the library XML must contain two re-identified UUIDs")
    _expect(
        len(appearance_uuids) == 2,
        "the appearance XML must contain two re-identified UUIDs",
    )
    _expect(
        material_uuids[0] == appearance_uuids[0],
        "the shared old UUID must map to the same new UUID in every file",
    )
    _expect(
        material_uuids[0].lower() not in old_keys,
        "the shared UUID must actually be replaced",
    )

    binary_names = {
        "thumbnail.png": b"\x89PNG\r\n\x1a\n"
        + _OLD_SHARED.encode("utf-8")
        + b"\x00\x01"
        + _OLD_OTHER.encode("utf-8"),
        "schema.bin": b"\x00\xff" + _OLD_OTHER.encode("utf-8") + b"\x00",
    }
    for suffix, expected_bytes in binary_names.items():
        matches = [data for name, data in destination_entries if name.endswith(suffix)]
        _expect(len(matches) == 1, "expected exactly one {!r} entry".format(suffix))
        _expect(
            matches[0] == expected_bytes,
            "binary entry {!r} must be preserved byte-for-byte".format(suffix),
        )

    destination_names = [name for name, _data in destination_entries]
    for name in destination_names:
        components = name.rstrip("/").split("/")
        _expect(
            _OLD_DIR not in components,
            "the old UUID directory name must be renamed in {!r}".format(name),
        )
    renamed_directories = [
        name.rstrip("/")
        for name in destination_names
        if name.endswith("/") and _UUID_FULL_PATTERN.match(name.split("/")[0])
    ]
    _expect(
        bool(renamed_directories),
        "a renamed UUID directory entry must be present",
    )
    _expect(
        any(
            name.endswith("/appearance.xml")
            and _UUID_FULL_PATTERN.match(name.split("/")[0]) is not None
            for name in destination_names
        ),
        "the appearance file must live under a renamed UUID directory",
    )


def _check_build_errors(tmp_dir: Path) -> None:
    """build_library_file rejects missing/non-ZIP sources and aliasing paths."""
    missing = tmp_dir / "missing" / FAVORITES_FILENAME
    try:
        build_library_file(missing, tmp_dir / "out" / "x.adsklib")
    except CustomLibraryError as exc:
        _expect("not found" in str(exc), "missing source error must be actionable")
    else:
        raise AssertionError("a missing source must raise CustomLibraryError")

    not_zip = tmp_dir / "not_zip.adsklib"
    not_zip.write_text("this is not a zip", encoding="utf-8")
    try:
        build_library_file(not_zip, tmp_dir / "out" / "y.adsklib")
    except CustomLibraryError as exc:
        _expect("ZIP" in str(exc), "non-ZIP source error must mention ZIP")
    else:
        raise AssertionError("a non-ZIP source must raise CustomLibraryError")

    source = _write_favorites_fixture(tmp_dir / "alias" / FAVORITES_FILENAME)
    try:
        build_library_file(source, source)
    except CustomLibraryError as exc:
        _expect("different files" in str(exc), "same source/destination must be rejected")
    else:
        raise AssertionError("source == destination must raise CustomLibraryError")

    try:
        build_library_file(None, tmp_dir / "out" / "z.adsklib")  # type: ignore[arg-type]
    except CustomLibraryError:
        pass
    else:
        raise AssertionError("a None source must raise CustomLibraryError")


def _check_ensure_load(tmp_dir: Path) -> None:
    """ensure_custom_library unloads, rebuilds, loads and reports the count."""
    previous_home = _HOME_OVERRIDE
    try:
        home = tmp_dir / "favorites_home"
        _set_home(home)
        roots = _favorites_roots()
        _write_favorites_fixture(roots[0] / "2024" / FAVORITES_FILENAME)

        destination = tmp_dir / "library_home" / (LIBRARY_NAME + ".adsklib")
        events: List[str] = []
        stale_named = FakeLoadedLibrary(LIBRARY_NAME, events=events)
        stale_path = FakeLoadedLibrary(
            "Old Custom Library", path=str(destination), events=events
        )
        libraries = FakeMaterialLibraries([stale_named, stale_path], events=events)
        app = FakeApp(libraries)

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"legacy library written to the base path")

        recorder = _RecordingBuild(events)
        with recorder:
            status = ensure_custom_library(app, destination=destination)

        first_build = Path(status.path)
        _expect(
            first_build.name == destination.name
            and first_build.parent.parent == _builds_dir(destination),
            "the library must be built into a fresh build folder: {}".format(status.path),
        )
        _expect(first_build.is_file(), "the build file must exist")
        _expect(not destination.exists(), "the legacy base file must be removed")
        _expect(status.exists is True, "the built library must exist")
        _expect(status.loaded is True, "the library must be reported as loaded")
        _expect(status.material_count == 21, "the library material count must be reported")
        _expect("Snapshot only" in status.message, "the snapshot caveat must be reported")
        _expect(stale_named.unloaded, "the same-name loaded library must be unloaded")
        _expect(stale_path.unloaded, "the same-path loaded library must be unloaded")
        _expect(len(recorder.calls) == 1, "the library must be rebuilt exactly once")
        _expect(events[-1] == "load", "loading must be the last step")
        build_index = events.index("build")
        unload_indices = [
            index for index, event in enumerate(events) if event.startswith("unload:")
        ]
        _expect(
            unload_indices and build_index < min(unload_indices),
            "the library must be built before the loaded one is unloaded (events: {})".format(
                events
            ),
        )
        _expect(
            os.stat(str(first_build)).st_mode & 0o777 == _LIBRARY_FILE_MODE,
            "the build must be readable by everyone (0644)",
        )
        _expect(
            libraries.count == 1,
            "exactly the regenerated library must remain loaded",
        )
        _expect(
            libraries.load_calls == [status.path],
            "MaterialLibraries.load must receive the new build path",
        )

        second_events: List[str] = []
        libraries.events = second_events
        second_recorder = _RecordingBuild(second_events)
        with second_recorder:
            second_status = ensure_custom_library(app, destination=destination)
        _expect(second_status.loaded is True, "a repeated ensure must reload the library")
        _expect(
            second_events[-1] == "load",
            "a repeated ensure must end by loading the library",
        )
        _expect(
            any(event.startswith("unload:") for event in second_events),
            "a repeated ensure must unload the previously loaded copy first",
        )
        _expect(
            libraries.count == 1,
            "a repeated ensure must not accumulate loaded libraries",
        )
        second_build = Path(second_status.path)
        _expect(
            second_build != first_build and second_build.is_file(),
            "a rebuild must use a new build folder so Fusion shows the new content",
        )
        _expect(not first_build.parent.exists(), "older builds must be deleted")
        _expect(
            latest_library_path(destination) == second_build,
            "latest_library_path must find the newest build",
        )
        _expect(
            _base_path(second_build) == destination,
            "a stored build path must map back to its base path",
        )
        load_calls = list(libraries.load_calls)
        settings_style = ensure_custom_library(app, destination=second_build, force=False)
        _expect(
            settings_style.path == str(second_build),
            "a build path passed as destination must reuse the build, not nest a new one",
        )
        _expect(
            libraries.load_calls == load_calls and libraries.count == 1,
            "a current build that is already loaded must not be unloaded and reloaded",
        )
        _expect(settings_style.material_count == 21, "the skipped reload must still report")

        # Without a readable path the loaded library is matched by name.
        loaded = libraries.item(0)
        loaded.path = None
        ensure_custom_library(app, destination=destination, force=False)
        _expect(libraries.load_calls == load_calls, "a name match must also skip the reload")

        # A library that is not loaded is loaded even when the build is current.
        loaded.unload()
        ensure_custom_library(app, destination=destination, force=False)
        _expect(
            len(libraries.load_calls) == len(load_calls) + 1,
            "an unloaded current build must be loaded",
        )
    finally:
        _set_home(previous_home)


def _check_ensure_build_failure(tmp_dir: Path) -> None:
    """A failed rebuild raises retry guidance and leaves Fusion unchanged."""
    previous_home = _HOME_OVERRIDE
    try:
        home = tmp_dir / "failure_home"
        _set_home(home)
        roots = _favorites_roots()
        _write_favorites_fixture(roots[0] / "2024" / FAVORITES_FILENAME)

        destination = tmp_dir / "failure_library" / (LIBRARY_NAME + ".adsklib")
        events: List[str] = []
        stale = FakeLoadedLibrary(LIBRARY_NAME, events=events)
        libraries = FakeMaterialLibraries([stale], events=events)
        app = FakeApp(libraries)
        recorder = _RecordingBuild(events, error=CustomLibraryError("simulated disk failure"))
        try:
            with recorder:
                ensure_custom_library(app, destination=destination)
        except CustomLibraryError as exc:
            message = str(exc)
            _expect(
                "Press Create again" in message,
                "failed rebuild must tell the user to retry: {}".format(message),
            )
            _expect(
                "simulated disk failure" in message,
                "failed rebuild must include the underlying cause: {}".format(message),
            )
            _expect(
                not stale.unloaded,
                "a failed build must leave the loaded library alone",
            )
        else:
            raise AssertionError("a failed rebuild must raise CustomLibraryError")
        _expect("build" in events, "the rebuild must have been attempted")
        _expect("load" not in events, "a failed rebuild must not load anything")
        _expect(libraries.count == 1, "the old library must stay loaded after a failed rebuild")
        _expect(
            not _builds_dir(destination).exists() or not any(_builds_dir(destination).iterdir()),
            "a failed build must not leave its folder behind",
        )
    finally:
        _set_home(previous_home)


def _check_ensure_missing_favorites(tmp_dir: Path) -> None:
    """ensure_custom_library raises guidance when Favorites is absent."""
    previous_home = _HOME_OVERRIDE
    try:
        _set_home(tmp_dir / "no_favorites_home")
        events: List[str] = []
        recorder = _RecordingBuild(events)
        try:
            with recorder:
                ensure_custom_library(FakeApp(), destination=tmp_dir / "out" / "x.adsklib")
        except CustomLibraryError as exc:
            message = str(exc)
            _expect(
                "Favorites" in message,
                "the missing-Favorites guidance must mention Favorites: {}".format(message),
            )
            _expect(
                "Create again" in message,
                "the missing-Favorites guidance must tell the user to retry",
            )
        else:
            raise AssertionError("missing Favorites must raise CustomLibraryError")
        _expect(events == [], "missing Favorites must not trigger an unload/build/load")
    finally:
        _set_home(previous_home)


def _check_status(tmp_dir: Path) -> None:
    """custom_library_status reports existence/loading without building."""
    previous_home = _HOME_OVERRIDE
    try:
        _set_home(tmp_dir / "status_home")
        destination = tmp_dir / "status_library" / (LIBRARY_NAME + ".adsklib")

        missing_status = custom_library_status(None, destination=destination)
        _expect(missing_status.exists is False, "a missing library must report exists=False")
        _expect(missing_status.loaded is False, "a missing library must report loaded=False")
        _expect(
            "not been built" in missing_status.message,
            "the missing library message must explain the state",
        )

        source = _write_favorites_fixture(tmp_dir / "status_source" / FAVORITES_FILENAME)
        build_library_file(source, destination)
        unloaded_status = custom_library_status(None, destination=destination)
        _expect(unloaded_status.exists is True, "a built library must report exists=True")
        _expect(unloaded_status.loaded is False, "no app means loaded=False")
        _expect(
            unloaded_status.material_count == 0,
            "no loaded library means material_count=0",
        )

        events: List[str] = []
        named_library = FakeLoadedLibrary(LIBRARY_NAME, events=events)
        libraries = FakeMaterialLibraries([named_library], events=events)
        loaded_status = custom_library_status(FakeApp(libraries), destination=destination)
        _expect(loaded_status.loaded is True, "a loaded library must report loaded=True")
        _expect(loaded_status.material_count == 21, "the loaded material count must be reported")
        _expect(
            "snapshot" in loaded_status.message.lower(),
            "the status message must mention the snapshot nature",
        )

        path_events: List[str] = []
        path_library = FakeLoadedLibrary(
            "Renamed In UI", path=str(destination), events=path_events
        )
        path_libraries = FakeMaterialLibraries([path_library], events=path_events)
        path_status = custom_library_status(
            FakeApp(path_libraries), destination=destination
        )
        _expect(
            path_status.loaded is True,
            "a loaded library matching by path must be detected",
        )
    finally:
        _set_home(previous_home)


def _check_previews_and_cleanup(tmp_dir: Path) -> None:
    """Previews are copied unchanged; cleanup only deletes unloaded stamp builds."""
    source = tmp_dir / "thumb_favorites.adsklib"
    destination = tmp_dir / "thumb_out.adsklib"
    identifier = "11111111-2222-3333-4444-555555555555"
    with zipfile.ZipFile(str(source), "w") as archive:
        archive.writestr("core.xml", "<Asset><String id='id' val='{}'/></Asset>".format(identifier))
        archive.writestr("Resources/{}/thumbnail/0/t_Prism-374_png".format(identifier), b"\x89PNG-ish")
    build_library_file(source, destination, thumbnail_colors={identifier: "#FF6600"})
    with zipfile.ZipFile(str(destination)) as archive:
        previews = [name for name in archive.namelist() if name.endswith("_png")]
        _expect(
            len(previews) == 1 and archive.read(previews[0]) == b"\x89PNG-ish",
            "preview images must be copied byte-for-byte (thumbnail_colors is ignored)",
        )

    base = tmp_dir / "cleanup" / (LIBRARY_NAME + ".adsklib")
    builds = _builds_dir(base)

    def make_build(stamp: str, with_file: bool = True) -> Path:
        folder = builds / stamp
        folder.mkdir(parents=True)
        if with_file:
            (folder / base.name).write_bytes(b"library")
        return folder / base.name

    old = make_build("20260101-010101-aaaaaa")
    locked = make_build("20260102-010101-bbbbbb")
    empty = make_build("20260103-010101-cccccc", with_file=False)
    keep = make_build("20260104-010101-dddddd")
    (builds / "my notes").mkdir()
    (builds / "my notes" / base.name).write_bytes(b"not a build")
    removed = _remove_old_builds(base, keep=keep, protected=[locked])
    _expect(removed == 1 and not old.parent.exists(), "only the old unlocked build must go")
    _expect(locked.is_file() and keep.is_file(), "the kept and still-loaded builds must stay")
    _expect(empty.parent.is_dir(), "a stamp folder without the library file must stay")
    _expect((builds / "my notes").is_dir(), "a folder that is not a build must stay")

    home = tmp_dir / "unload_home"
    previous_home = _HOME_OVERRIDE
    try:
        _set_home(home)
        _write_favorites_fixture(_favorites_roots()[0] / "2024" / FAVORITES_FILENAME)
        stuck = FakeLoadedLibrary(LIBRARY_NAME, path=str(locked))

        def refuse() -> None:
            raise RuntimeError("simulated unload failure")

        stuck.unload = refuse  # type: ignore[assignment]
        libraries = FakeMaterialLibraries([stuck])
        status = ensure_custom_library(FakeApp(libraries), destination=base)
        _expect(locked.is_file(), "the build whose unload failed must not be deleted")
        _expect(not keep.parent.exists(), "an unloaded older build must be deleted")
        _expect("could not be unloaded" in status.message, "the failed unload must be reported")
        stuck.path = None
        newest = Path(status.path)
        ensure_custom_library(FakeApp(FakeMaterialLibraries([stuck])), destination=base)
        _expect(
            newest.is_file() and locked.is_file(),
            "with an unknown path every older build must be kept",
        )
    finally:
        _set_home(previous_home)


def _selftest() -> int:
    """Run the custom-library self-test with fake Fusion objects.

    Returns:
        Process exit code: 0 when every check passed, 1 otherwise.
    """
    scenarios = (
        (
            "Favorites discovery and default/env library paths work",
            _check_find_and_default_paths,
        ),
        (
            "build_library_file re-identifies UUIDs and preserves binaries",
            _check_build_library,
        ),
        (
            "previews are copied unchanged and cleanup keeps foreign/locked folders",
            _check_previews_and_cleanup,
        ),
        (
            "build_library_file rejects missing/non-ZIP/aliased sources",
            _check_build_errors,
        ),
        (
            "ensure_custom_library builds, unloads, loads, skips current loads",
            _check_ensure_load,
        ),
        (
            "the current library stays loaded although Fusion returns new wrappers",
            _check_keep_survives_new_wrappers,
        ),
        (
            "a failed rebuild raises retry guidance and unloads nothing",
            _check_ensure_build_failure,
        ),
        (
            "ensure_custom_library raises guidance when Favorites is missing",
            _check_ensure_missing_favorites,
        ),
        (
            "custom_library_status reports existence/loading without building",
            _check_status,
        ),
    )
    try:
        with tempfile.TemporaryDirectory(prefix="ffb_customlib_selftest_") as temp_dir:
            base = Path(temp_dir)
            for index, (label, scenario) in enumerate(scenarios):
                scenario_dir = base / "case_{:02d}".format(index)
                scenario_dir.mkdir(parents=True, exist_ok=True)
                scenario(scenario_dir)
                print("PASS: {}".format(label))
    except Exception:
        traceback.print_exc()
        print("FAIL: custom_library self-test aborted", file=sys.stderr)
        return 1
    print("PASS: all custom_library self-tests completed")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(_selftest())
