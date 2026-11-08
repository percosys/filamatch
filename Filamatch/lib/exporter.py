"""Fusion 360 3MF export and atomic injector orchestration.

This module is the bridge between a Fusion design and the standalone
:mod:`threemf_injector` post-processor:

* :func:`default_export_path` picks a safe ``*_AMS.3mf`` file name for a design.
* :func:`collect_design_objects` enumerates bodies/components for the palette's
  mapping table.
* :func:`export_3mf` wraps ``ExportManager.createC3MFExportOptions`` plus
  ``ExportManager.execute`` and verifies that Fusion really wrote a file.
* :func:`export_and_inject` exports once, injects ``Metadata/model_settings.config``
  (and optionally ``Metadata/project_settings.config`` when filament slots are
  supplied) into a sibling temporary archive, and only then atomically replaces
  the raw export.  If injection fails the raw Fusion export is kept on disk so
  the user still has a usable 3MF, and :class:`InjectionFailedError` exposes its
  path via ``raw_export_path``.

The module imports cleanly outside Fusion 360: ``adsk`` is never imported and
the sibling injector is imported lazily.  Every Fusion object is duck-typed, so
the whole module can be exercised with fakes by the ``__main__`` self-test.
"""

from __future__ import annotations

import inspect
import logging
import os
import re
import sys
import tempfile
import traceback
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: Fallback document name used when Fusion does not report one.
DEFAULT_DOCUMENT_NAME = "Filamatch_Export"

#: Suffix appended to the derived file name.
EXPORT_SUFFIX = "_AMS.3mf"

#: Maximum component nesting level walked by :func:`collect_design_objects`.
MAX_COMPONENT_DEPTH = 8

#: Label used as the first path segment for bodies in the root component.
ROOT_PATH_LABEL = "Root"

_MODEL_SETTINGS_PART = "Metadata/model_settings.config"
_PROJECT_SETTINGS_PART = "Metadata/project_settings.config"
_TEMP_SUFFIX = ".filamatch_tmp.3mf"
_MAX_FILENAME_STEM = 100
_UNSAFE_FILENAME_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_PATTERN = re.compile(r"\s+")
_GLOB_TEMP_PATTERN = "*" + _TEMP_SUFFIX

__all__ = [
    "DEFAULT_DOCUMENT_NAME",
    "EXPORT_SUFFIX",
    "MAX_COMPONENT_DEPTH",
    "ROOT_PATH_LABEL",
    "ExportError",
    "DesignExportError",
    "InjectionFailedError",
    "DesignObject",
    "ExportResult",
    "default_export_path",
    "collect_design_objects",
    "export_3mf",
    "export_and_inject",
]


class ExportError(RuntimeError):
    """Base class for every error raised by this module."""


class DesignExportError(ExportError):
    """Raised when a Fusion design cannot be exported to 3MF."""


class InjectionFailedError(ExportError):
    """Raised when the 3MF injector fails after a successful raw export.

    Attributes:
        raw_export_path: Path of the unmodified Fusion 3MF export that was kept
            on disk so the user still has a usable file.
    """

    def __init__(self, message: str, raw_export_path: Path) -> None:
        super().__init__(message)
        self.raw_export_path = Path(raw_export_path)


@dataclass(frozen=True)
class DesignObject:
    """One row for the palette mapping table.

    Attributes:
        name: Body name, or component name for a component with no bodies.
        path: Component containment path, e.g. ``"Root/Component:1"``.
        material: Assigned Fusion material name, or ``None`` when unassigned.
        body_count: ``1`` for a body row, ``0`` for a body-less component row.
    """

    name: str
    path: str
    material: Optional[str]
    body_count: int


@dataclass(frozen=True)
class ExportResult:
    """Summary of one successful :func:`export_and_inject` call."""

    output_path: Path
    mapping: Mapping[str, int]
    assignments: Mapping[int, int]
    mapped_count: int
    defaulted_count: int
    project_settings_written: bool
    filament_count: int
    #: Mapping names the injector skipped because the 3MF has no such object
    #: (surfaces, hidden bodies and other things Fusion does not export).
    unmatched_names: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Fusion API access helpers (all defensive: failures are logged, never fatal
# unless the module raises an explicit ExportError subclass)
# ---------------------------------------------------------------------------


def _safe_attribute(obj: Any, attribute: str, context: str) -> Any:
    """Read ``obj.attribute``, logging and returning ``None`` on API failure."""
    if obj is None:
        return None
    try:
        return getattr(obj, attribute)
    except Exception:
        logger.exception("Could not read %r from %s.", attribute, context)
        return None


def _collection_items(collection: Any, label: str) -> List[Any]:
    """Enumerate a Fusion ``count``/``item(index)`` collection into a list.

    Plain iteration is attempted as a fallback for test doubles or API objects
    that support it; failures are logged and produce an empty/partial list.
    """
    if collection is None:
        return []
    items: List[Any] = []
    try:
        count = int(collection.count)
    except Exception:
        logger.exception("Could not read count of %s; falling back to iteration.", label)
        count = 0
    for index in range(max(count, 0)):
        try:
            item = collection.item(index)
        except Exception:
            logger.exception("Could not read item %d of %s.", index, label)
            continue
        if item is not None:
            items.append(item)
    if items:
        return items
    try:
        return [item for item in collection if item is not None]
    except Exception:
        logger.debug("Could not iterate %s directly.", label, exc_info=True)
        return items


def _display_name(obj: Any, attribute: str, context: str, fallback: str) -> str:
    """Return a stripped non-empty ``obj.attribute`` string, or ``fallback``."""
    value = _safe_attribute(obj, attribute, context)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _remove_file_quietly(path: Path) -> None:
    """Delete ``path`` when it exists, ignoring filesystem errors."""
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# default_export_path
# ---------------------------------------------------------------------------


def _document_name(design: Any) -> str:
    """Return the parent document name of ``design``, or the fallback name."""
    if design is None:
        logger.warning("No Fusion design supplied; using the fallback export name.")
        return DEFAULT_DOCUMENT_NAME
    document = _safe_attribute(design, "parentDocument", "the active design")
    if document is None:
        logger.warning(
            "The design has no readable parentDocument; using the fallback export name."
        )
        return DEFAULT_DOCUMENT_NAME
    name = _safe_attribute(document, "name", "the parent document")
    if isinstance(name, str) and name.strip():
        return name.strip()
    logger.warning("The parent document has no usable name; using the fallback export name.")
    return DEFAULT_DOCUMENT_NAME


def _sanitize_filename_stem(name: str) -> str:
    """Convert a document name into a cross-platform safe file name stem."""
    stem = name.strip()
    if stem.lower().endswith(".f3d"):
        stem = stem[:-4]
    stem = _UNSAFE_FILENAME_PATTERN.sub("_", stem)
    stem = _WHITESPACE_PATTERN.sub(" ", stem)
    stem = stem.strip(" ._")
    if len(stem) > _MAX_FILENAME_STEM:
        stem = stem[:_MAX_FILENAME_STEM].rstrip(" ._")
    return stem or DEFAULT_DOCUMENT_NAME


def _default_export_directory() -> Path:
    """Return ``~/Documents`` when it exists, otherwise the home directory."""
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        logger.exception("Could not resolve the user home directory; using the current directory.")
        return Path.cwd()
    documents = home / "Documents"
    try:
        if documents.is_dir():
            return documents
    except OSError:
        logger.exception("Could not inspect %s; using the home directory.", documents)
    return home


def default_export_path(design: Any) -> Path:
    """Derive the default 3MF export path for a design.

    The document name (``design.parentDocument.name``) is sanitised into a safe
    file name, any trailing ``.f3d`` is removed and ``_AMS.3mf`` is appended.
    The file is placed in ``~/Documents`` when that directory exists, otherwise
    directly in the user's home directory.  The directory is not created here.

    Args:
        design: Active Fusion design (anything exposing ``parentDocument.name``).

    Returns:
        The suggested output :class:`~pathlib.Path`.
    """
    document_name = _document_name(design)
    stem = _sanitize_filename_stem(document_name)
    path = _default_export_directory() / (stem + EXPORT_SUFFIX)
    logger.debug("Default export path for %r is %s", document_name, path)
    return path


# ---------------------------------------------------------------------------
# collect_design_objects
# ---------------------------------------------------------------------------


def _component_name(component: Any, path: str) -> str:
    """Return a component's name, or its path label when unreadable/empty."""
    return _display_name(component, "name", "component %r" % path, path.rsplit("/", 1)[-1])


def _body_material_name(body: Any, path: str) -> Optional[str]:
    """Return the material name assigned to ``body``, or ``None``."""
    material = _safe_attribute(body, "material", "body in %r" % path)
    if material is None:
        return None
    name = _safe_attribute(material, "name", "material of body in %r" % path)
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _component_bodies(component: Any, path: str) -> List[Any]:
    """Return a component's bodies across the Fusion API variants.

    Real Fusion components expose ``bRepBodies`` (solid bodies) and
    ``meshBodies``; some API facades/tests only expose ``bodies``.  All present
    collections are merged with identity de-duplication.

    Args:
        component: Fusion component.
        path: Component path label used in log/error messages.

    Returns:
        The list of body objects.
    """
    label = "component %r" % path
    bodies: List[Any] = []
    seen_ids: Set[int] = set()
    for attribute in ("bRepBodies", "meshBodies", "bodies"):
        collection = _safe_attribute(component, attribute, "bodies of " + label)
        if collection is None:
            continue
        for body in _collection_items(collection, "%s of %s" % (attribute, label)):
            marker = id(body)
            if marker in seen_ids:
                continue
            seen_ids.add(marker)
            bodies.append(body)
    return bodies


def _append_object_row(
    rows: List[DesignObject],
    seen: Set[Tuple[str, str]],
    name: str,
    path: str,
    material: Optional[str],
    body_count: int,
) -> None:
    """Append a row unless an identical ``(name, path)`` row already exists."""
    key = (name, path)
    if key in seen:
        return
    seen.add(key)
    rows.append(DesignObject(name=name, path=path, material=material, body_count=body_count))


def _collect_component_rows(
    component: Any,
    path: str,
    depth: int,
    rows: List[DesignObject],
    seen: Set[Tuple[str, str]],
) -> None:
    """Recursively collect body rows for ``component`` and its occurrences."""
    label = "component %r" % path
    bodies = _component_bodies(component, path)
    for body in bodies:
        body_name = _display_name(body, "name", "body in " + label, "<unnamed body>")
        _append_object_row(
            rows, seen, body_name, path, _body_material_name(body, path), body_count=1
        )
    if not bodies:
        _append_object_row(
            rows, seen, _component_name(component, path), path, material=None, body_count=0
        )

    if depth >= MAX_COMPONENT_DEPTH:
        logger.warning(
            "Reached the component nesting limit (%d) at %r; deeper components are not collected.",
            MAX_COMPONENT_DEPTH,
            path,
        )
        return

    occurrences = _collection_items(
        _safe_attribute(component, "occurrences", label), "occurrences of " + label
    )
    for occurrence in occurrences:
        occurrence_name = _display_name(
            occurrence, "name", "occurrence in " + label, "<unnamed occurrence>"
        )
        child = _safe_attribute(occurrence, "component", "occurrence %r" % occurrence_name)
        if child is None:
            logger.warning(
                "Occurrence %r under %r has no accessible component; skipping it.",
                occurrence_name,
                path,
            )
            continue
        _collect_component_rows(child, path + "/" + occurrence_name, depth + 1, rows, seen)


def collect_design_objects(design: Any) -> List[DesignObject]:
    """Enumerate the design bodies for the palette mapping table.

    The root component and every nested occurrence component are walked
    recursively (up to :data:`MAX_COMPONENT_DEPTH` levels).  BRep and mesh bodies
    are included.  Each body produces
    one row with the body name, its component path (``Root``,
    ``Root/Component:1``, ...), its material name when readable, and
    ``body_count=1``.  A component or occurrence with no bodies produces one
    row using the component name and ``body_count=0``.  Identical
    ``(name, path)`` duplicates are removed and the result is sorted by path
    then name.

    Args:
        design: Active Fusion design (anything exposing ``rootComponent``).

    Returns:
        The de-duplicated, sorted rows.

    Raises:
        DesignExportError: If ``design`` is ``None``.
    """
    if design is None:
        raise DesignExportError("collect_design_objects requires a Fusion design, got None")
    root_component = _safe_attribute(design, "rootComponent", "the active design")
    if root_component is None:
        logger.warning("The design has no root component; no objects to collect.")
        return []

    rows: List[DesignObject] = []
    seen: Set[Tuple[str, str]] = set()
    _collect_component_rows(root_component, ROOT_PATH_LABEL, 0, rows, seen)
    rows.sort(key=lambda item: (item.path, item.name))
    logger.info("Collected %d design object row(s).", len(rows))
    return rows


# ---------------------------------------------------------------------------
# export_3mf
# ---------------------------------------------------------------------------


def _create_c3mf_options(export_manager: Any, root_component: Any, destination: Path) -> Any:
    """Create Fusion ``C3MFExportOptions``, tolerating the one-argument form."""
    try:
        return export_manager.createC3MFExportOptions(root_component, str(destination))
    except TypeError:
        logger.debug(
            "createC3MFExportOptions(geometry, filename) is unsupported; "
            "trying the one-argument form.",
            exc_info=True,
        )
    except Exception as exc:
        logger.exception("Fusion could not create 3MF export options.")
        raise DesignExportError(
            "Fusion could not create 3MF export options for %r: %s" % (str(destination), exc)
        ) from exc

    try:
        options = export_manager.createC3MFExportOptions(root_component)
    except Exception as exc:
        logger.exception("Fusion could not create 3MF export options (one-argument form).")
        raise DesignExportError(
            "Fusion could not create 3MF export options for %r: %s" % (str(destination), exc)
        ) from exc
    if options is not None:
        try:
            options.filename = str(destination)
        except Exception as exc:
            logger.exception("Could not set the C3MF export options filename.")
            raise DesignExportError(
                "Could not set the 3MF output filename on the Fusion export options: %s" % exc
            ) from exc
        logger.debug("Applied the output filename to the 3MF export options via its property.")
    return options


def export_3mf(design: Any, output_path: Path) -> Path:
    """Export the active design to a 3MF file using Fusion's export manager.

    A parent directory is created when missing.  The export is considered
    successful only when ``ExportManager.execute`` reports success and the
    output file exists with a non-zero size.

    Args:
        design: Active Fusion design (anything exposing ``exportManager`` and
            ``rootComponent``).
        output_path: Destination ``.3mf`` path.

    Returns:
        The destination path that was written.

    Raises:
        DesignExportError: If the design, export manager, options, export call,
            or resulting file are unusable.
    """
    if design is None:
        raise DesignExportError("export_3mf requires a Fusion design, got None")
    destination = Path(output_path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.exception("Could not create the 3MF output directory.")
        raise DesignExportError(
            "Could not create the 3MF output directory %r: %s"
            % (str(destination.parent), exc)
        ) from exc

    export_manager = _safe_attribute(design, "exportManager", "the active design")
    if export_manager is None:
        raise DesignExportError(
            "The active design does not expose an exportManager; 3MF export is unavailable."
        )
    root_component = _safe_attribute(design, "rootComponent", "the active design")
    if root_component is None:
        raise DesignExportError("The active design has no root component; nothing to export.")

    options = _create_c3mf_options(export_manager, root_component, destination)
    if options is None:
        raise DesignExportError(
            "Fusion returned null C3MF export options for %r; the export was not started."
            % str(destination)
        )
    is_valid = _safe_attribute(options, "isValid", "the C3MF export options")
    if is_valid is False:
        raise DesignExportError(
            "Fusion reports the C3MF export options are invalid (isValid is False); "
            "the export was not started."
        )

    # Remove any previous export first: otherwise a stale file from an earlier
    # run would pass the "exists and is not empty" check below even when
    # Fusion silently wrote nothing this time.
    try:
        destination.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.exception("Could not replace the existing 3MF at %r.", str(destination))
        raise DesignExportError(
            "Could not replace the existing 3MF at %r (is it open in another program?): %s"
            % (str(destination), exc)
        ) from exc

    try:
        executed = export_manager.execute(options)
    except Exception as exc:
        logger.exception("Fusion failed while exporting 3MF to %r.", str(destination))
        raise DesignExportError(
            "Fusion failed while exporting 3MF to %r: %s" % (str(destination), exc)
        ) from exc
    if executed is False:
        logger.error("Fusion's ExportManager.execute returned False for %r.", str(destination))
        raise DesignExportError(
            "Fusion did not complete the 3MF export (ExportManager.execute returned False) "
            "for %r." % str(destination)
        )

    try:
        output_size = destination.stat().st_size
    except OSError as exc:
        logger.exception("Fusion did not produce the 3MF output file %r.", str(destination))
        raise DesignExportError(
            "Fusion reported a successful export but did not write %r: %s"
            % (str(destination), exc)
        ) from exc
    if output_size <= 0:
        raise DesignExportError(
            "Fusion created an empty 3MF file at %r; the export is unusable."
            % str(destination)
        )

    logger.info("Exported the design to %r (%d byte(s)).", str(destination), output_size)
    return destination


# ---------------------------------------------------------------------------
# export_and_inject
# ---------------------------------------------------------------------------


def _resolve_injector(injector: Any) -> Any:
    """Return the explicit injector or import the sibling ``threemf_injector``."""
    if injector is not None:
        return injector
    try:
        from . import threemf_injector
    except ImportError as exc:
        logger.exception("The threemf_injector module is not importable.")
        raise ExportError(
            "The 3MF injector module (threemf_injector.py) is not available: %s" % exc
        ) from exc
    return threemf_injector


def _coerce_mapping(mapping: Any) -> Dict[str, int]:
    """Copy and lightly validate the name-to-extruder mapping."""
    if mapping is None:
        raise ExportError("mapping must be a name-to-extruder mapping, got None")
    try:
        return dict(mapping)
    except (TypeError, ValueError) as exc:
        raise ExportError("mapping is not a valid name-to-extruder mapping: %s" % exc) from exc


def _injector_accepts(injector: Any, keyword: str) -> bool:
    """Return ``True`` when ``injector.inject`` accepts the keyword (``filaments``, ``object_settings``)."""
    inject = getattr(injector, "inject", None)
    if not callable(inject):
        return False
    try:
        parameters = inspect.signature(inject).parameters
    except (TypeError, ValueError):
        logger.debug("Could not introspect the injector's inject() signature.", exc_info=True)
        return False
    if keyword in parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def _read_result_attribute(result: Any, attribute: str) -> Any:
    """Read an optional attribute from an injector result.

    A missing attribute is normal when the installed injector is older or
    newer than this module, so it is logged at debug level instead of as an
    error.  Genuine attribute-access failures are still logged with a traceback.
    """
    if result is None:
        return None
    try:
        return getattr(result, attribute)
    except AttributeError:
        logger.debug("The injection result has no %r attribute.", attribute)
        return None
    except Exception:
        logger.exception("Could not read %r from the injection result.", attribute)
        return None


def _result_int(result: Any, attribute: str) -> int:
    """Read an integer count from an injection result, defaulting to ``0``."""
    value = _read_result_attribute(result, attribute)
    if isinstance(value, bool) or not isinstance(value, int):
        if value is not None:
            logger.warning(
                "Injection result attribute %r is not an integer: %r", attribute, value
            )
        return 0
    return value


def _result_assignments(result: Any) -> Dict[int, int]:
    """Read the object-id to extruder assignments from an injection result."""
    raw_assignments = _read_result_attribute(result, "assignments")
    if raw_assignments is None:
        return {}
    try:
        return {
            int(object_id): int(extruder)
            for object_id, extruder in dict(raw_assignments).items()
        }
    except (TypeError, ValueError):
        logger.exception("The injection result assignments are malformed: %r", raw_assignments)
        return {}


def _filament_count(filaments: Any, result: Any) -> int:
    """Determine how many filament slots were written."""
    count = _result_int(result, "filament_count")
    if count > 0 or filaments is None:
        return count
    try:
        return len(filaments)
    except TypeError:
        logger.warning("Could not determine the filament count from %r.", filaments)
        return 0


def _project_settings_written(passed_filaments: bool, result: Any) -> bool:
    """Determine whether ``Metadata/project_settings.config`` was written."""
    if not passed_filaments:
        return False
    explicit = _read_result_attribute(result, "project_settings_written")
    if explicit is not None:
        return bool(explicit)
    project_settings_xml = _read_result_attribute(result, "project_settings_xml")
    return project_settings_xml is not None


def export_and_inject(
    design: Any,
    output_path: Path,
    mapping: Mapping[str, int],
    filaments: Optional[Mapping[int, Any]] = None,
    write_project_settings: bool = False,
    injector: Any = None,
    object_settings: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> ExportResult:
    """Export the design and inject slicer metadata atomically.

    The Fusion export always goes directly to ``output_path`` (the raw file).
    The injector then writes a sibling temporary archive named
    ``<name>.filamatch_tmp.3mf`` in the same directory, which replaces ``output_path``
    with :func:`os.replace` only after injection succeeded.  If injection fails,
    the temporary file is removed and the raw Fusion export is preserved; the
    raised :class:`InjectionFailedError` exposes it as ``raw_export_path``.

    Args:
        design: Active Fusion design.
        output_path: Final ``.3mf`` path (overwritten on success).
        mapping: Object-name to 1-based extruder slot mapping.
        filaments: Optional filament slot payload forwarded to the injector when
            ``write_project_settings`` is true.
        write_project_settings: Request ``Metadata/project_settings.config``
            injection (only effective when ``filaments`` is supplied and the
            installed injector supports it).
        injector: Optional module/object exposing ``inject``; defaults to the
            imported :mod:`threemf_injector` module.
        object_settings: Print settings of single bodies by body name
            (Bambu Studio / OrcaSlicer keys, see body_settings.to_bambu).

    Returns:
        An :class:`ExportResult` populated from the injector's result.

    Raises:
        ExportError: If the mapping is malformed or the injector is unavailable.
        DesignExportError: If the raw Fusion export fails.
        InjectionFailedError: If injection (or the final atomic replace) fails;
            the raw export remains at ``output_path``.
    """
    injector_module = _resolve_injector(injector)
    mapping_copy = _coerce_mapping(mapping)
    destination = Path(output_path)

    raw_output = export_3mf(design, destination)
    temp_path = destination.with_suffix(_TEMP_SUFFIX)
    _remove_file_quietly(temp_path)

    filaments_supported = _injector_accepts(injector_module, "filaments")
    pass_filaments = bool(write_project_settings and filaments is not None)
    if pass_filaments and not filaments_supported:
        logger.warning(
            "The 3MF injector does not support filament/project-settings injection; "
            "writing model_settings.config only."
        )
        pass_filaments = False

    inject_kwargs: Dict[str, Any] = {
        "input_path": raw_output,
        "mapping": mapping_copy,
        "output_path": temp_path,
    }
    if pass_filaments:
        inject_kwargs["filaments"] = filaments
    if object_settings:
        if _injector_accepts(injector_module, "object_settings"):
            inject_kwargs["object_settings"] = object_settings
        else:
            logger.warning("The 3MF injector cannot write print settings of single bodies; they are left out.")

    try:
        try:
            injection_result = injector_module.inject(**inject_kwargs)
        except Exception as exc:
            logger.exception(
                "3MF injection failed for %r; the raw export was preserved.", str(destination)
            )
            raise InjectionFailedError(
                "3MF injection failed for %r; the raw Fusion export was preserved: %s"
                % (str(destination), exc),
                raw_export_path=destination,
            ) from exc
        try:
            os.replace(str(temp_path), str(destination))
        except OSError as exc:
            logger.exception(
                "Could not move the injected 3MF into place at %r; raw export preserved.",
                str(destination),
            )
            raise InjectionFailedError(
                "Could not move the injected 3MF into place at %r; the raw Fusion export "
                "was preserved: %s" % (str(destination), exc),
                raw_export_path=destination,
            ) from exc
    finally:
        _remove_file_quietly(temp_path)

    assignments = _result_assignments(injection_result)
    filament_count = _filament_count(filaments if pass_filaments else None, injection_result)
    project_settings_written = _project_settings_written(pass_filaments, injection_result)
    mapped_count = _result_int(injection_result, "mapped_count")
    defaulted_count = _result_int(injection_result, "defaulted_count")
    unmatched_names = tuple(
        str(name) for name in (_read_result_attribute(injection_result, "unmatched_names") or ())
    )
    if unmatched_names:
        logger.warning(
            "Skipped %d mapping name(s) that are not in the exported 3MF: %s",
            len(unmatched_names),
            ", ".join(repr(name) for name in unmatched_names[:10]),
        )

    logger.info(
        "Exported and injected %r (mapped=%d, defaulted=%d, project_settings=%s, "
        "filaments=%d).",
        str(destination),
        mapped_count,
        defaulted_count,
        project_settings_written,
        filament_count,
    )
    return ExportResult(
        output_path=destination,
        mapping=dict(mapping_copy),
        assignments=assignments,
        mapped_count=mapped_count,
        defaulted_count=defaulted_count,
        project_settings_written=project_settings_written,
        filament_count=filament_count,
        unmatched_names=unmatched_names,
    )


# ---------------------------------------------------------------------------
# Self-test doubles and checks (only used by the __main__ self-test)
# ---------------------------------------------------------------------------


class FakeMaterial:
    """Test double for a Fusion material."""

    def __init__(self, name: str) -> None:
        self.name = name


class FakeBody:
    """Test double for ``adsk.fusion.BRepBody``."""

    def __init__(self, name: str, material: Any = None) -> None:
        self.name = name
        self.material = material


class FakeCollection:
    """Test double for a Fusion ``count``/``item(index)`` collection."""

    def __init__(self, items: Optional[Sequence[Any]] = None) -> None:
        self._items = list(items or [])

    @property
    def count(self) -> int:
        """Number of items in the collection."""
        return len(self._items)

    def item(self, index: int) -> Any:
        """Return the item at ``index``, or ``None`` when out of range."""
        if 0 <= index < len(self._items):
            return self._items[index]
        return None


class FakeComponent:
    """Test double for ``adsk.fusion.Component``."""

    def __init__(
        self,
        name: str,
        bodies: Optional[Sequence[FakeBody]] = None,
        occurrences: Optional[Sequence["FakeOccurrence"]] = None,
    ) -> None:
        self.name = name
        self.bRepBodies = FakeCollection(bodies)
        self.meshBodies = FakeCollection([])
        # Legacy/test-facade alias; the production code prefers bRepBodies.
        self.bodies = self.bRepBodies
        self.occurrences = FakeCollection(occurrences)


class FakeOccurrence:
    """Test double for ``adsk.fusion.Occurrence``."""

    def __init__(self, name: str, component: FakeComponent) -> None:
        self.name = name
        self.component = component


class FakeParentDocument:
    """Test double for ``adsk.core.Document``."""

    def __init__(self, name: Optional[str]) -> None:
        self.name = name


class FakeExportOptions:
    """Test double for ``adsk.fusion.C3MFExportOptions``."""

    def __init__(
        self, root_component: Any = None, filename: Optional[str] = None, is_valid: bool = True
    ) -> None:
        self.geometry = root_component
        self.filename = filename
        self.isValid = is_valid


class FakeExportManager:
    """Test double for ``adsk.fusion.ExportManager``.

    When ``fixture_path`` is set, ``execute`` writes a copy of that fixture to
    ``options.filename``, simulating a successful Fusion 3MF export.
    """

    def __init__(
        self,
        fixture_path: Optional[Path] = None,
        options_none: bool = False,
        options_invalid: bool = False,
        write_output: bool = True,
        execute_result: Any = True,
        raise_on_execute: bool = False,
        one_argument_options: bool = False,
    ) -> None:
        self._fixture_path = Path(fixture_path) if fixture_path is not None else None
        self._options_none = options_none
        self._options_invalid = options_invalid
        self._write_output = write_output
        self._execute_result = execute_result
        self._raise_on_execute = raise_on_execute
        self._one_argument_options = one_argument_options
        self.options: Optional[FakeExportOptions] = None
        self.executed = False

    def createC3MFExportOptions(self, root_component: Any, filename: Any = None) -> Any:
        """Create fake 3MF export options; mirrors both Fusion call signatures."""
        if self._one_argument_options and filename is not None:
            raise TypeError("simulated one-argument createC3MFExportOptions")
        if self._options_none:
            return None
        self.options = FakeExportOptions(
            root_component=root_component,
            filename=str(filename) if filename is not None else None,
            is_valid=not self._options_invalid,
        )
        return self.options

    def execute(self, options: FakeExportOptions) -> Any:
        """Simulate running the export; optionally write the fixture file."""
        if self._raise_on_execute:
            raise RuntimeError("simulated Fusion export failure")
        self.executed = True
        if self._write_output and self._fixture_path is not None:
            destination = Path(str(options.filename))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(self._fixture_path.read_bytes())
        return self._execute_result


class FakeDesign:
    """Test double for ``adsk.fusion.Design``."""

    def __init__(
        self,
        root_component: Any = None,
        export_manager: Any = None,
        document_name: Optional[str] = "Exporter Self-Test",
    ) -> None:
        self.rootComponent = root_component
        self.exportManager = export_manager
        self.parentDocument = (
            FakeParentDocument(document_name) if document_name is not None else None
        )


class FakeFilamentSlot:
    """Test double for ``threemf_injector.FilamentSlot``."""

    def __init__(self, extruder: int, color_hex: str, filament_type: str = "PLA") -> None:
        self.extruder = extruder
        self.color_hex = color_hex
        self.filament_type = filament_type


class FakeInjectionResult:
    """Minimal stand-in for ``threemf_injector.InjectionResult``."""

    def __init__(
        self,
        assignments: Optional[Mapping[int, int]] = None,
        mapped_count: int = 0,
        defaulted_count: int = 0,
        config_xml: str = "<config/>",
        filament_count: int = 0,
        project_settings_xml: Optional[str] = None,
    ) -> None:
        self.assignments = dict(assignments or {})
        self.mapped_count = mapped_count
        self.defaulted_count = defaulted_count
        self.config_xml = config_xml
        self.filament_count = filament_count
        self.project_settings_xml = project_settings_xml


class RecordingInjector:
    """Test double for the injector module that records call arguments."""

    def __init__(
        self,
        support_filaments: bool = True,
        result: Optional[FakeInjectionResult] = None,
    ) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._support_filaments = support_filaments
        self._result = result or FakeInjectionResult(
            assignments={2: 1, 3: 2}, mapped_count=2, defaulted_count=0
        )
        if support_filaments:
            self.inject = self._inject_with_filaments  # type: ignore[assignment]
        else:
            self.inject = self._inject_without_filaments  # type: ignore[assignment]

    def _record(self, input_path: Any, mapping: Any, output_path: Any, filaments: Any) -> Any:
        """Record the call and copy the input archive to the output path."""
        self.calls.append(
            {
                "input_path": Path(input_path),
                "mapping": dict(mapping),
                "output_path": Path(output_path),
                "filaments": filaments,
            }
        )
        Path(output_path).write_bytes(Path(input_path).read_bytes())
        return self._result

    def _inject_with_filaments(
        self,
        input_path: Any,
        mapping: Any,
        output_path: Any,
        default_extruder: int = 1,
        only_mapped: bool = False,
        filaments: Any = None,
    ) -> FakeInjectionResult:
        """Record a call from an injector that supports the filaments keyword."""
        return self._record(input_path, mapping, output_path, filaments)

    def _inject_without_filaments(
        self,
        input_path: Any,
        mapping: Any,
        output_path: Any,
        default_extruder: int = 1,
        only_mapped: bool = False,
    ) -> FakeInjectionResult:
        """Record a call from an older injector without filaments support."""
        return self._record(input_path, mapping, output_path, None)


_FIXTURE_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
  <Default Extension="png" ContentType="image/png"/>
</Types>
"""

_FIXTURE_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
    '  <Relationship Id="rel0" Target="/3D/3dmodel.model" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
    "</Relationships>\n"
)

_FIXTURE_MODEL_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<model unit="millimeter" xml:lang="en-US" '
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
    '  <metadata name="Application">Filamatch exporter self-test</metadata>\n'
    "  <resources>\n"
    '    <object id="2" type="model" name="Bracket">'
    '<mesh><vertices><vertex x="0" y="0" z="0"/></vertices>'
    '<triangles><triangle v1="0" v2="0" v3="0"/></triangles></mesh></object>\n'
    '    <object id="3" type="model" name="Bushing">'
    '<mesh><vertices><vertex x="0" y="0" z="0"/></vertices>'
    '<triangles><triangle v1="0" v2="0" v3="0"/></triangles></mesh></object>\n'
    "  </resources>\n"
    "  <build>\n"
    '    <item objectid="2"/>\n'
    '    <item objectid="3"/>\n'
    "  </build>\n"
    "</model>\n"
)

_FIXTURE_THUMBNAIL = b"\x89PNG\r\n\x1a\nFilamatch exporter self-test thumbnail"


def _write_fixture_3mf(path: Path) -> Path:
    """Write the synthetic Fusion-style 3MF fixture used by the self-test."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(path), "w") as archive:
        archive.writestr("[Content_Types].xml", _FIXTURE_CONTENT_TYPES)
        archive.writestr("_rels/.rels", _FIXTURE_RELS)
        archive.writestr("3D/3dmodel.model", _FIXTURE_MODEL_XML)
        archive.writestr("Metadata/thumbnail.png", _FIXTURE_THUMBNAIL)
    return path


def _fixture_design(fixture_path: Path, document_name: str = "Exporter Self-Test") -> FakeDesign:
    """Build a fake design whose export manager writes the 3MF fixture."""
    root_component = FakeComponent("Root Component")
    return FakeDesign(
        root_component=root_component,
        export_manager=FakeExportManager(fixture_path=fixture_path),
        document_name=document_name,
    )


def _read_model_settings(path: Path) -> Dict[int, int]:
    """Parse object-id to extruder assignments from an injected 3MF archive."""
    with zipfile.ZipFile(str(path), "r") as archive:
        raw_config = archive.read(_MODEL_SETTINGS_PART)
    root = ET.fromstring(raw_config)
    assignments: Dict[int, int] = {}
    for element in root:
        if element.tag != "object":
            continue
        object_id = int(element.get("id", ""))
        for metadata in element:
            if metadata.tag == "metadata" and metadata.get("key") == "extruder":
                assignments[object_id] = int(metadata.get("value", ""))
    return assignments


def _zip_names(path: Path) -> List[str]:
    """Return archive member names in central-directory order."""
    with zipfile.ZipFile(str(path), "r") as archive:
        return archive.namelist()


def _expect(condition: bool, message: str) -> None:
    """Raise AssertionError with ``message`` when ``condition`` is false."""
    if not condition:
        raise AssertionError(message)


def _check_default_export_path(tmp_dir: Path) -> None:
    """default_export_path strips .f3d, sanitises names and picks a directory."""
    design = FakeDesign(document_name="Bracket Assembly.f3d")
    path = default_export_path(design)
    home = Path.home()
    documents = home / "Documents"
    expected_directory = documents if documents.is_dir() else home
    _expect(
        path == expected_directory / "Bracket Assembly_AMS.3mf",
        "unexpected default path: {}".format(path),
    )
    _expect(not tmp_dir.exists() or tmp_dir.is_dir(), "tmp_dir sanity check")

    fallback = default_export_path(FakeDesign(document_name=None))
    _expect(
        fallback.name == DEFAULT_DOCUMENT_NAME + EXPORT_SUFFIX,
        "missing document name must fall back to {}, got {}".format(
            DEFAULT_DOCUMENT_NAME + EXPORT_SUFFIX, fallback.name
        ),
    )

    unsafe = default_export_path(FakeDesign(document_name='bad/name:*?"<>| .f3d'))
    _expect(
        not any(character in unsafe.name for character in '<>:"/\\|?*'),
        "unsafe document name was not sanitised: {}".format(unsafe.name),
    )
    _expect(
        unsafe.name.endswith(EXPORT_SUFFIX),
        "sanitised path must keep the {} suffix, got {}".format(EXPORT_SUFFIX, unsafe.name),
    )


def _check_collect_design_objects(tmp_dir: Path) -> None:
    """collect_design_objects walks nested components and reads material names."""
    _expect(tmp_dir.is_dir(), "self-test scratch directory must exist")
    nested_empty = FakeComponent("Nested Empty")
    sub_component = FakeComponent(
        "Bracket Assembly",
        bodies=[FakeBody("Bushing", FakeMaterial("PETG Plastic"))],
        occurrences=[FakeOccurrence("Sub:1", nested_empty)],
    )
    root_component = FakeComponent(
        "Root Component",
        bodies=[FakeBody("Bracket", FakeMaterial("PLA Plastic"))],
        occurrences=[FakeOccurrence("Component:1", sub_component)],
    )
    rows = collect_design_objects(FakeDesign(root_component=root_component))

    actual = [(row.name, row.path, row.material, row.body_count) for row in rows]
    expected = [
        ("Bracket", "Root", "PLA Plastic", 1),
        ("Bushing", "Root/Component:1", "PETG Plastic", 1),
        ("Nested Empty", "Root/Component:1/Sub:1", None, 0),
    ]
    _expect(actual == expected, "unexpected design object rows: {}".format(actual))

    class UnreadableMaterial:
        @property
        def name(self) -> str:
            raise RuntimeError("simulated unreadable material name")

    broken_root = FakeComponent(
        "Broken Root", bodies=[FakeBody("Mystery Body", UnreadableMaterial())]
    )
    broken_rows = collect_design_objects(FakeDesign(root_component=broken_root))
    _expect(
        len(broken_rows) == 1 and broken_rows[0].material is None,
        "an unreadable material must produce a row with material=None",
    )

    try:
        collect_design_objects(None)
    except DesignExportError:
        pass
    else:
        raise AssertionError("collect_design_objects(None) must raise DesignExportError")


def _check_export_3mf_success(tmp_dir: Path) -> None:
    """export_3mf writes the file, creates directories and verifies output."""
    fixture_path = _write_fixture_3mf(tmp_dir / "fixture.3mf")
    design = _fixture_design(fixture_path)
    destination = tmp_dir / "nested" / "deep" / "Bracket_AMS.3mf"

    returned = export_3mf(design, destination)

    _expect(returned == destination, "export_3mf must return the destination path")
    _expect(destination.is_file(), "export_3mf must leave a 3MF file on disk")
    _expect(destination.stat().st_size > 0, "exported 3MF must not be empty")
    _expect("3D/3dmodel.model" in _zip_names(destination), "exported fixture is incomplete")

    one_arg_fixture = _write_fixture_3mf(tmp_dir / "fixture_one_arg.3mf")
    one_arg_design = FakeDesign(
        root_component=FakeComponent("Root Component"),
        export_manager=FakeExportManager(
            fixture_path=one_arg_fixture, one_argument_options=True
        ),
    )
    one_arg_destination = tmp_dir / "one_arg.3mf"
    export_3mf(one_arg_design, one_arg_destination)
    _expect(
        one_arg_design.exportManager.options.filename == str(one_arg_destination),
        "one-argument export options must receive the filename property",
    )


def _check_export_3mf_failures(tmp_dir: Path) -> None:
    """export_3mf converts every Fusion failure into DesignExportError."""
    fixture_path = _write_fixture_3mf(tmp_dir / "failure_fixture.3mf")

    def manager_design(**manager_kwargs: Any) -> FakeDesign:
        return FakeDesign(
            root_component=FakeComponent("Root Component"),
            export_manager=FakeExportManager(fixture_path=fixture_path, **manager_kwargs),
        )

    failure_cases = {
        "null options": (manager_design(options_none=True), "null C3MF"),
        "invalid options": (manager_design(options_invalid=True), "isValid"),
        "missing output": (manager_design(write_output=False), "did not write"),
        "execute false": (manager_design(execute_result=False), "returned False"),
        "execute raises": (manager_design(raise_on_execute=True), "failed while exporting"),
    }
    # A stale file from an earlier export must not satisfy the output check.
    stale_destination = tmp_dir / "failure_missing_output.3mf"
    stale_destination.write_bytes(b"stale export from an earlier run")
    for label, (design, expected_fragment) in failure_cases.items():
        try:
            export_3mf(design, tmp_dir / ("failure_" + label.replace(" ", "_") + ".3mf"))
        except DesignExportError as exc:
            _expect(
                expected_fragment in str(exc),
                "{} error should mention {!r}: {}".format(label, expected_fragment, exc),
            )
        else:
            raise AssertionError("{} must raise DesignExportError".format(label))

    try:
        export_3mf(None, tmp_dir / "none.3mf")
    except DesignExportError:
        pass
    else:
        raise AssertionError("export_3mf(None) must raise DesignExportError")


def _check_export_and_inject_success(tmp_dir: Path) -> None:
    """export_and_inject replaces the raw export with an injected archive."""
    from . import threemf_injector

    fixture_path = _write_fixture_3mf(tmp_dir / "inject_fixture.3mf")
    design = _fixture_design(fixture_path)
    destination = tmp_dir / "out" / "Bracket_AMS.3mf"
    mapping = {"Bracket": 1, "Bushing": 2}

    result = export_and_inject(design, destination, mapping)

    _expect(isinstance(result, ExportResult), "export_and_inject must return ExportResult")
    _expect(result.output_path == destination, "result output path mismatch")
    _expect(dict(result.mapping) == mapping, "result mapping must echo the input mapping")
    _expect(
        result.assignments == {2: 1, 3: 2},
        "unexpected assignments: {}".format(result.assignments),
    )
    _expect(
        result.mapped_count == 2,
        "expected 2 mapped objects, got {}".format(result.mapped_count),
    )
    _expect(result.defaulted_count == 0, "expected 0 defaulted objects")
    _expect(not result.project_settings_written, "project settings must not be written by default")
    _expect(result.filament_count == 0, "filament count must be zero without filaments")
    _expect(destination.is_file(), "injected output must exist")
    _expect(
        _read_model_settings(destination) == {2: 1, 3: 2},
        "model_settings.config assignments are wrong",
    )
    _expect(
        _MODEL_SETTINGS_PART in _zip_names(destination),
        "injected archive must contain model_settings.config",
    )
    source_names = _zip_names(fixture_path)
    injected_names = _zip_names(destination)
    _expect(
        all(name in injected_names for name in source_names),
        "injected archive must preserve the original entries",
    )
    _expect(
        not destination.with_suffix(_TEMP_SUFFIX).exists(),
        "the temporary .filamatch_tmp.3mf sibling must be cleaned up",
    )
    _expect(
        list(destination.parent.glob(_GLOB_TEMP_PATTERN)) == [],
        "temporary .filamatch_tmp.3mf files must be cleaned up",
    )
    _expect(
        isinstance(result.assignments, dict),
        "assignments should be a plain dict",
    )
    _expect(
        threemf_injector.MODEL_SETTINGS_PART == _MODEL_SETTINGS_PART,
        "fixture constant mismatch",
    )


def _check_export_and_inject_failure(tmp_dir: Path) -> None:
    """A failed injection keeps the raw export and exposes its path."""
    from . import threemf_injector

    fixture_path = _write_fixture_3mf(tmp_dir / "failed_inject_fixture.3mf")
    design = _fixture_design(fixture_path)
    destination = tmp_dir / "out" / "raw_preserved.3mf"

    # Unknown names are skipped and reported, not fatal.
    skipped = export_and_inject(design, destination, {"No Such Object": 1})
    _expect(
        skipped.unmatched_names == ("No Such Object",),
        "unmatched mapping names must be reported, got {!r}".format(skipped.unmatched_names),
    )

    class StrictInjector:
        """Injector stand-in that keeps the old strict unknown-name behaviour."""

        @staticmethod
        def inject(input_path: Path, mapping: Mapping[str, int], output_path: Path) -> Any:
            return threemf_injector.inject(input_path, mapping, output_path, strict=True)

    try:
        export_and_inject(
            design, destination, {"No Such Object": 1}, injector=StrictInjector()
        )
    except InjectionFailedError as exc:
        _expect(
            exc.raw_export_path == destination,
            "raw_export_path must point at the raw export",
        )
        _expect(
            isinstance(exc.__cause__, threemf_injector.UnknownComponentError),
            "InjectionFailedError must chain the injector error",
        )
    else:
        raise AssertionError("unknown component must raise InjectionFailedError")

    _expect(destination.is_file(), "raw Fusion export must be preserved after a failure")
    _expect(destination.stat().st_size > 0, "preserved raw export must not be empty")
    _expect(
        _MODEL_SETTINGS_PART not in _zip_names(destination),
        "preserved raw export must not contain injected metadata",
    )
    _expect(
        list(destination.parent.glob(_GLOB_TEMP_PATTERN)) == [],
        "temporary .filamatch_tmp.3mf files must be cleaned up after a failure",
    )


def _check_write_project_settings(tmp_dir: Path) -> None:
    """write_project_settings forwards filaments and populates result fields."""
    fixture_path = _write_fixture_3mf(tmp_dir / "settings_fixture.3mf")
    design = _fixture_design(fixture_path)
    destination = tmp_dir / "out" / "settings_AMS.3mf"
    filaments = {
        1: FakeFilamentSlot(1, "#161616", "PLA"),
        2: FakeFilamentSlot(2, "#FFFFFF", "PETG"),
    }
    stub_result = FakeInjectionResult(
        assignments={2: 1, 3: 2},
        mapped_count=2,
        defaulted_count=0,
        filament_count=2,
        project_settings_xml='{"filament_colour": ["#161616", "#FFFFFF"]}',
    )
    injector = RecordingInjector(result=stub_result)

    result = export_and_inject(
        design,
        destination,
        {"Bracket": 1, "Bushing": 2},
        filaments=filaments,
        write_project_settings=True,
        injector=injector,
    )

    _expect(len(injector.calls) == 1, "the stub injector must be called exactly once")
    call = injector.calls[0]
    _expect(
        call["filaments"] is filaments,
        "filaments must be forwarded to the injector unchanged",
    )
    _expect(call["input_path"] == destination, "injector input must be the raw export")
    _expect(
        call["output_path"].name.endswith(_TEMP_SUFFIX),
        "injector output must be the temp path",
    )
    _expect(result.project_settings_written, "project_settings_written must be True")
    _expect(result.filament_count == 2, "filament_count must come from the injector result")

    no_settings_destination = tmp_dir / "out" / "no_settings_AMS.3mf"
    no_settings_injector = RecordingInjector()
    no_settings_result = export_and_inject(
        design,
        no_settings_destination,
        {"Bracket": 1},
        filaments=filaments,
        write_project_settings=False,
        injector=no_settings_injector,
    )
    _expect(
        no_settings_injector.calls[0]["filaments"] is None,
        "filaments must not be forwarded when write_project_settings is False",
    )
    _expect(
        not no_settings_result.project_settings_written,
        "project settings must not be reported when not requested",
    )

    legacy_destination = tmp_dir / "out" / "legacy_AMS.3mf"
    legacy_injector = RecordingInjector(support_filaments=False)
    legacy_result = export_and_inject(
        design,
        legacy_destination,
        {"Bracket": 1},
        filaments=filaments,
        write_project_settings=True,
        injector=legacy_injector,
    )
    _expect(
        len(legacy_injector.calls) == 1,
        "an injector without filaments support must still be called",
    )
    _expect(
        not legacy_result.project_settings_written,
        "project settings must be reported as not written for a legacy injector",
    )


def _selftest() -> int:
    """Run the exporter self-test against fake Fusion objects.

    Returns:
        Process exit code: 0 when every check passed, 1 otherwise.
    """
    scenarios = (
        ("default_export_path derives safe *_AMS.3mf paths", _check_default_export_path),
        (
            "collect_design_objects walks nested components and materials",
            _check_collect_design_objects,
        ),
        (
            "export_3mf writes the design and supports one-argument options",
            _check_export_3mf_success,
        ),
        ("export_3mf wraps every Fusion failure in DesignExportError", _check_export_3mf_failures),
        ("export_and_inject exports then injects atomically", _check_export_and_inject_success),
        (
            "injection failure preserves the raw export and raises InjectionFailedError",
            _check_export_and_inject_failure,
        ),
        (
            "write_project_settings forwards filaments and populates the result",
            _check_write_project_settings,
        ),
    )
    try:
        with tempfile.TemporaryDirectory(prefix="filamatch_exporter_selftest_") as temp_dir:
            base_dir = Path(temp_dir)
            for index, (label, scenario) in enumerate(scenarios):
                scenario_dir = base_dir / "case_{:02d}".format(index)
                scenario_dir.mkdir(parents=True, exist_ok=True)
                scenario(scenario_dir)
                print("PASS: {}".format(label))
    except Exception:
        traceback.print_exc()
        print("FAIL: exporter self-test aborted", file=sys.stderr)
        return 1
    print("PASS: all exporter self-tests completed")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(_selftest())
