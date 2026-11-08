"""Filamatch 3MF post-processor.

Injects Bambu Studio / Orca Slicer per-object filament metadata
(``Metadata/model_settings.config``) into a 3MF archive exported by Autodesk
Fusion 360, so that the slicer assigns an AMS / extruder slot to every object
without manual re-assignment.

The module is standalone: it only depends on the Python standard library
(3.8+) and can be used from the Fusion 360 add-in or from a terminal::

    python3 threemf_injector.py --input model.3mf --mapping mapping.json \\
        --output model_injected.3mf

The mapping JSON maps 3MF object names to 1-based extruder numbers::

    {"Bracket": 1, "Bushing": 2, "Clamp Jaw": 3}

Object names are read from the ``name`` attribute of ``<object>`` elements in
the model part (with a ``<metadata name="name">`` child element as fallback).
Names nested inside top-level assembly objects are resolved to the build-item
ancestor object ids, because ``model_settings.config`` is object-level.  All
original archive entries are copied byte-identically, and exactly one new or
replaced ``Metadata/model_settings.config`` entry is written.

An optional filament table can be passed to also write (or merge into)
``Metadata/project_settings.config`` with Bambu Studio / Orca Slicer filament
colour, type, density and diameter arrays::

    python3 threemf_injector.py --input model.3mf --mapping mapping.json \\
        --filaments filaments.json --output model_injected.3mf

The filaments JSON is either an extruder-keyed object or a list of objects;
see :func:`load_filaments` for the accepted fields.

To discover the exact object names a Fusion 360 export contains before writing
a mapping, list them without modifying the archive::

    python3 threemf_injector.py --input model.3mf --list-objects

Run ``python3 threemf_injector.py --self-test`` for a full end-to-end
self-test of the injector.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import unquote
from xml.sax.saxutils import quoteattr

LOGGER = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

MODEL_PART = "3D/3dmodel.model"
MODEL_SETTINGS_PART = "Metadata/model_settings.config"
PROJECT_SETTINGS_PART = "Metadata/project_settings.config"
MAX_EXTRUDER = 16

RELS_PART = "_rels/.rels"
_MODEL_RELATIONSHIP_SUFFIX = "/3dmodel"
_COMPARE_CHUNK_SIZE = 64 * 1024
_FILAMENT_COLOUR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
_DEFAULT_FILAMENT_COLOUR = "#FFFFFF"
_DEFAULT_FILAMENT_TYPE = "PLA"
_DEFAULT_FILAMENT_DENSITY = "1.24"
_DEFAULT_FILAMENT_DIAMETER = "1.75"
_WRITABLE_COMPRESSION_TYPES = frozenset(
    {
        zipfile.ZIP_STORED,
        zipfile.ZIP_DEFLATED,
        zipfile.ZIP_BZIP2,
        zipfile.ZIP_LZMA,
    }
)

__all__ = [
    "MODEL_PART",
    "MODEL_SETTINGS_PART",
    "PROJECT_SETTINGS_PART",
    "MAX_EXTRUDER",
    "ThreeMFError",
    "Invalid3MFError",
    "ModelPartNotFoundError",
    "MappingError",
    "UnknownComponentError",
    "ConflictingAssignmentError",
    "FilamentSettingsError",
    "ThreeMFObject",
    "FilamentSlot",
    "InjectionResult",
    "load_mapping",
    "load_filaments",
    "extract_objects",
    "resolve_assignments",
    "build_model_settings_config",
    "build_project_settings_config",
    "inject",
    "inject_file",
    "list_objects",
    "main",
]


class ThreeMFError(Exception):
    """Base class for every error raised by this module."""


class Invalid3MFError(ThreeMFError):
    """Raised when the input is not a usable 3MF (ZIP) archive."""


class ModelPartNotFoundError(Invalid3MFError):
    """Raised when no 3D model part can be located inside the archive."""


class MappingError(ThreeMFError):
    """Raised when the name-to-extruder mapping is malformed or unusable."""


class UnknownComponentError(MappingError):
    """Raised when a mapping name does not match any 3MF object name."""


class ConflictingAssignmentError(MappingError):
    """Raised when two mapping names resolve to the same top-level 3MF object.

    ``model_settings.config`` can only assign one extruder per object, so two
    different mapping entries that both resolve to the same build-item object
    (for example two bodies nested inside one assembly object) cannot be
    represented.  The injection is aborted instead of silently writing one of
    the two values.
    """


class FilamentSettingsError(ThreeMFError):
    """Raised when a filament table or project settings payload is malformed."""


def _require_optional_text(value: object, field_name: str) -> None:
    """Validate an optional non-empty string filament field."""
    if not isinstance(value, str) or not value.strip():
        raise FilamentSettingsError(
            f"filament {field_name} must be a non-empty string when provided, got {value!r}"
        )


@dataclass(frozen=True)
class FilamentSlot:
    """One 1-based AMS / extruder filament slot for ``project_settings.config``.

    Values are validated and normalized on construction; ``color_hex`` is
    stored as uppercase ``#RRGGBB`` and an integer density is converted to
    ``float``.  Invalid values raise :class:`FilamentSettingsError` with an
    actionable message.
    """

    extruder: int
    color_hex: str
    filament_type: str
    density_g_cm3: Optional[float] = None
    filament_id: Optional[str] = None
    name: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.extruder, bool) or not isinstance(self.extruder, int):
            raise FilamentSettingsError(f"filament extruder must be an integer, got {self.extruder!r}")
        if not 1 <= self.extruder <= MAX_EXTRUDER:
            raise FilamentSettingsError(
                f"filament extruder {self.extruder} is out of range 1..{MAX_EXTRUDER}"
            )
        if not isinstance(self.color_hex, str) or not _FILAMENT_COLOUR_PATTERN.match(self.color_hex):
            raise FilamentSettingsError(
                f"filament colour must be a '#RRGGBB' hex string, got {self.color_hex!r}"
            )
        object.__setattr__(self, "color_hex", self.color_hex.upper())
        if not isinstance(self.filament_type, str) or not self.filament_type.strip():
            raise FilamentSettingsError(
                f"filament type must be a non-empty string, got {self.filament_type!r}"
            )
        if self.density_g_cm3 is not None:
            density = self.density_g_cm3
            if isinstance(density, bool) or not isinstance(density, (int, float)):
                raise FilamentSettingsError(
                    f"filament density must be a number greater than 0, got {density!r}"
                )
            if not math.isfinite(density) or density <= 0:
                raise FilamentSettingsError(
                    f"filament density must be a finite number greater than 0, got {density!r}"
                )
            object.__setattr__(self, "density_g_cm3", float(density))
        if self.filament_id is not None:
            _require_optional_text(self.filament_id, "filament_id")
        if self.name is not None:
            _require_optional_text(self.name, "name")


@dataclass(frozen=True)
class ThreeMFObject:
    """A single ``<object>`` resource parsed from the 3D model part."""

    id: int
    name: Optional[str]
    object_type: Optional[str]
    is_build_item: bool
    ancestor_ids: Tuple[int, ...]
    source_part: str
    names: Tuple[str, ...]


@dataclass(frozen=True)
class InjectionResult:
    """Summary of one successful :func:`inject` call."""

    input_path: Path
    output_path: Path
    assignments: Mapping[int, int]
    object_names: Mapping[int, str]
    mapped_count: int
    defaulted_count: int
    config_xml: str
    filament_count: int = 0
    project_settings_xml: Optional[str] = None
    #: Mapping names that matched no 3MF object and were skipped (non-strict mode).
    unmatched_names: Tuple[str, ...] = ()


def _localname(tag: object) -> str:
    """Return the local part of a possibly namespace-qualified XML tag."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _has_local_attribute(element: ET.Element, name: str) -> bool:
    """Return true when *element* has any attribute whose local name is *name*."""
    return any(_localname(attribute) == name for attribute in element.attrib)


def _find_child(element: ET.Element, local_name: str) -> Optional[ET.Element]:
    """Return the first direct child whose local name matches *local_name*."""
    for child in element:
        if _localname(child.tag) == local_name:
            return child
    return None


def _normalize_part_name(target: str) -> str:
    """Normalize an OPC relationship target into an archive member name."""
    normalized = unquote(target.strip()).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _resolve_model_part(archive: zipfile.ZipFile) -> str:
    """Locate the 3D model part: ``_rels/.rels`` first, then ``3D/3dmodel.model``."""
    names = archive.namelist()
    relationship_targets: List[str] = []
    try:
        relationships_data = archive.read(RELS_PART)
    except KeyError:
        LOGGER.debug("no '%s' part in archive; falling back to '%s'", RELS_PART, MODEL_PART)
        relationships_data = None
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        LOGGER.warning("could not read package relationships '%s': %s", RELS_PART, exc)
        relationships_data = None

    if relationships_data:
        try:
            relationships_root = ET.fromstring(relationships_data)
        except ET.ParseError as exc:
            LOGGER.warning("ignoring malformed package relationships '%s': %s", RELS_PART, exc)
        else:
            for relationship in relationships_root.iter():
                if _localname(relationship.tag) != "Relationship":
                    continue
                relationship_type = relationship.get("Type") or ""
                if not relationship_type.lower().rstrip("/").endswith(_MODEL_RELATIONSHIP_SUFFIX):
                    continue
                target = relationship.get("Target")
                if target:
                    relationship_targets.append(_normalize_part_name(target))

    for target in relationship_targets:
        if target in names:
            LOGGER.debug("resolved 3D model part '%s' from '%s'", target, RELS_PART)
            return target
    for target in relationship_targets:
        for name in names:
            if name.lower() == target.lower():
                LOGGER.debug("resolved 3D model part '%s' (case-insensitive) from '%s'", name, RELS_PART)
                return name

    if MODEL_PART in names:
        LOGGER.debug("falling back to well-known model part '%s'", MODEL_PART)
        return MODEL_PART
    for name in names:
        if name.lower() == MODEL_PART.lower():
            LOGGER.debug("falling back to well-known model part '%s' (case-insensitive)", name)
            return name

    listing = ", ".join(names[:30]) if names else "<empty archive>"
    if len(names) > 30:
        listing += f", ... ({len(names) - 30} more)"
    raise ModelPartNotFoundError(
        f"no 3D model part found in 3MF archive: expected a package relationship of type "
        f"'...{_MODEL_RELATIONSHIP_SUFFIX}' in '{RELS_PART}' or the part '{MODEL_PART}'. "
        f"Archive contains {len(names)} entr{'y' if len(names) == 1 else 'ies'}: {listing}"
    )


def _parse_model_tree(model_data: bytes, part_name: str) -> ET.Element:
    """Parse the 3D model XML, converting parser failures into clear errors."""
    try:
        root = ET.fromstring(model_data)
    except ET.ParseError as exc:
        raise Invalid3MFError(f"malformed 3D model XML in '{part_name}': {exc}") from exc
    if _localname(root.tag) != "model":
        raise Invalid3MFError(
            f"invalid 3D model XML in '{part_name}': root element is '{_localname(root.tag)}', expected 'model'"
        )
    return root


def _collect_names(object_element: ET.Element) -> Tuple[str, ...]:
    """Collect candidate object names (attribute first, then ``<metadata name="name">``)."""
    names: List[str] = []
    raw_name = object_element.get("name")
    if raw_name is not None and raw_name.strip():
        names.append(raw_name)
    for child in object_element:
        if _localname(child.tag) != "metadata" or child.get("name") != "name":
            continue
        metadata_name = (child.text or "").strip()
        if metadata_name and metadata_name not in names:
            names.append(metadata_name)
    return tuple(names)


def extract_objects(archive: zipfile.ZipFile) -> List[ThreeMFObject]:
    """Extract object resources and their build-item ancestry from a 3MF archive.

    Object names are taken from the ``<object name="...">`` attribute, falling
    back to a ``<metadata name="name">`` child element.  ``ancestor_ids`` lists
    the top-level build-item object ids that (transitively) contain the object;
    build items themselves list their own id.  Objects not referenced by any
    build item have an empty ``ancestor_ids`` tuple.

    Only the main model part is parsed.  Production-extension references to
    objects in other model parts (``p:path``) are logged and not resolved.
    """
    part_name = _resolve_model_part(archive)
    try:
        model_data = archive.read(part_name)
    except KeyError as exc:  # pragma: no cover - namelist already checked
        raise ModelPartNotFoundError(f"3D model part '{part_name}' disappeared from the archive") from exc
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise Invalid3MFError(f"could not read 3D model part '{part_name}': {exc}") from exc

    root = _parse_model_tree(model_data, part_name)
    resources = _find_child(root, "resources")
    object_elements: List[ET.Element] = []
    if resources is None:
        LOGGER.warning("3D model part '%s' has no <resources> element; no objects found", part_name)
    else:
        object_elements = [child for child in resources if _localname(child.tag) == "object"]

    elements: Dict[int, ET.Element] = {}
    names_by_id: Dict[int, Tuple[str, ...]] = {}
    types_by_id: Dict[int, Optional[str]] = {}
    object_order: List[int] = []
    for object_element in object_elements:
        raw_id = object_element.get("id")
        if raw_id is None:
            raise Invalid3MFError(f"<object> element in '{part_name}' is missing the required 'id' attribute")
        try:
            object_id = int(raw_id)
        except ValueError as exc:
            raise Invalid3MFError(f"object id {raw_id!r} in '{part_name}' is not an integer") from exc
        if object_id <= 0:
            raise Invalid3MFError(f"object id {raw_id!r} in '{part_name}' must be a positive integer")
        if object_id in elements:
            LOGGER.warning("duplicate object id %d in '%s'; keeping the first definition", object_id, part_name)
            continue
        elements[object_id] = object_element
        object_order.append(object_id)
        names_by_id[object_id] = _collect_names(object_element)
        types_by_id[object_id] = object_element.get("type")

    component_edges: Dict[int, List[int]] = {object_id: [] for object_id in object_order}
    production_reference_count = 0
    for object_id in object_order:
        components = _find_child(elements[object_id], "components")
        if components is None:
            continue
        for component in components:
            if _localname(component.tag) != "component":
                continue
            if _has_local_attribute(component, "path"):
                production_reference_count += 1
                continue
            raw_component_id = component.get("objectid")
            if raw_component_id is None:
                LOGGER.warning(
                    "object id %d in '%s' has a <component> without an 'objectid'; ignored",
                    object_id,
                    part_name,
                )
                continue
            try:
                component_id = int(raw_component_id)
            except ValueError:
                LOGGER.warning(
                    "object id %d in '%s' has a <component> with non-integer objectid %r; ignored",
                    object_id,
                    part_name,
                    raw_component_id,
                )
                continue
            if component_id not in elements:
                LOGGER.debug(
                    "object id %d references unknown component object id %d (it may live in another model part)",
                    object_id,
                    component_id,
                )
                continue
            component_edges[object_id].append(component_id)

    build = _find_child(root, "build")
    build_item_ids: List[int] = []
    seen_build_item_ids: Set[int] = set()
    if build is None:
        LOGGER.warning(
            "3D model part '%s' has no <build> section; treating unreferenced top-level objects as build items",
            part_name,
        )
        referenced_ids = {component_id for children in component_edges.values() for component_id in children}
        build_item_ids = [object_id for object_id in object_order if object_id not in referenced_ids]
    else:
        for item in build:
            if _localname(item.tag) != "item":
                continue
            if _has_local_attribute(item, "path"):
                production_reference_count += 1
                continue
            raw_object_id = item.get("objectid")
            if raw_object_id is None:
                LOGGER.warning("build item in '%s' is missing its 'objectid'; ignored", part_name)
                continue
            try:
                build_object_id = int(raw_object_id)
            except ValueError:
                LOGGER.warning(
                    "build item in '%s' has a non-integer objectid %r; ignored", part_name, raw_object_id
                )
                continue
            if build_object_id not in elements:
                LOGGER.warning(
                    "build item references unknown object id %d in '%s'; ignored", build_object_id, part_name
                )
                continue
            if build_object_id in seen_build_item_ids:
                continue
            seen_build_item_ids.add(build_object_id)
            build_item_ids.append(build_object_id)

    if production_reference_count:
        LOGGER.warning(
            "3D model part '%s' uses %d production-extension cross-file reference(s) (p:path); "
            "objects defined in other model parts are not resolved for model_settings injection",
            part_name,
            production_reference_count,
        )

    ancestors: Dict[int, Set[int]] = {object_id: set() for object_id in object_order}
    for build_object_id in build_item_ids:
        stack = [build_object_id]
        visited: Set[int] = set()
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            ancestors[current].add(build_object_id)
            stack.extend(component_edges.get(current, ()))

    build_item_set = set(build_item_ids)
    objects = [
        ThreeMFObject(
            id=object_id,
            name=names_by_id[object_id][0] if names_by_id[object_id] else None,
            object_type=types_by_id[object_id],
            is_build_item=object_id in build_item_set,
            ancestor_ids=tuple(sorted(ancestors[object_id])),
            source_part=part_name,
            names=names_by_id[object_id],
        )
        for object_id in object_order
    ]
    objects.sort(key=lambda obj: obj.id)
    LOGGER.debug(
        "extracted %d object(s) from '%s' (%d build item(s))",
        len(objects),
        part_name,
        len(build_item_ids),
    )
    return objects


def _validate_mapping_payload(payload: object, source: str) -> Dict[str, int]:
    """Validate a decoded mapping payload and return it as a fresh dict."""
    if isinstance(payload, bool) or not isinstance(payload, dict):
        raise MappingError(
            f"{source} must be a JSON object mapping 3MF object names to 1-based extruder numbers, "
            f"got {type(payload).__name__}"
        )
    if not payload:
        raise MappingError(f"{source} contains no entries")
    validated: Dict[str, int] = {}
    for name, extruder in payload.items():
        if not isinstance(name, str) or not name.strip():
            raise MappingError(f"{source} has an entry with an empty or non-string object name: {name!r}")
        if isinstance(extruder, bool) or not isinstance(extruder, int):
            raise MappingError(
                f"{source} entry {name!r} must map to an integer extruder number 1..{MAX_EXTRUDER}, "
                f"got {extruder!r}"
            )
        if not 1 <= extruder <= MAX_EXTRUDER:
            raise MappingError(
                f"{source} entry {name!r} has extruder {extruder}; out of range, expected "
                f"1..{MAX_EXTRUDER}"
            )
        validated[name] = extruder
    return validated


def load_mapping(mapping_path: Path) -> Dict[str, int]:
    """Load and validate a JSON object-name to 1-based-extruder mapping file.

    The JSON document must be an object whose keys are non-empty object names
    and whose values are integers in ``1..MAX_EXTRUDER`` (booleans rejected).
    """
    path = Path(mapping_path)
    if not path.exists():
        raise MappingError(f"mapping file not found: {path}")
    if not path.is_file():
        raise MappingError(f"mapping path is not a regular file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MappingError(f"cannot read mapping file {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise MappingError(f"mapping file {path} is not valid UTF-8: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MappingError(
            f"mapping file {path} is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc
    return _validate_mapping_payload(payload, source=f"mapping file {path}")


def _validate_mapping_argument(mapping: Mapping[str, int]) -> Dict[str, int]:
    """Validate a programmatic mapping argument and return it as a fresh dict."""
    try:
        payload = dict(mapping)
    except (TypeError, ValueError) as exc:
        raise MappingError(
            f"mapping must be a mapping of 3MF object names to 1-based extruder numbers: {exc}"
        ) from exc
    return _validate_mapping_payload(payload, source="provided mapping")


def _filament_field(entry: Mapping[str, Any], field_names: Sequence[str]) -> Any:
    """Return the first present value of *field_names* from a filament entry."""
    for field_name in field_names:
        if field_name in entry:
            return entry[field_name]
    return None


def _parse_extruder_key(key: object, source: str) -> int:
    """Parse an extruder number from an extruder-keyed filament JSON object."""
    if not isinstance(key, str) or not key.strip().isdigit():
        raise FilamentSettingsError(
            f"{source} has an invalid extruder key {key!r}; keys must be integer extruder numbers "
            f"1..{MAX_EXTRUDER}"
        )
    return int(key.strip())


def _parse_filament_extruder_field(value: object, label: str) -> int:
    """Parse the required ``extruder`` field of a list-form filament entry."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise FilamentSettingsError(
            f"{label} must provide an integer 'extruder' number, got {value!r}"
        )
    return value


def _parse_filament_entry(
    entry: object,
    label: str,
    extruder: Optional[int],
) -> Tuple[int, FilamentSlot]:
    """Parse one filament JSON object into ``(extruder, FilamentSlot)``."""
    if not isinstance(entry, dict):
        raise FilamentSettingsError(
            f"{label} must be a JSON object describing one filament, got {type(entry).__name__}"
        )
    if extruder is None:
        extracted = _filament_field(entry, ("extruder",))
        if extracted is None:
            raise FilamentSettingsError(f"{label} is missing the required 'extruder' field")
        extruder = _parse_filament_extruder_field(extracted, label)
    else:
        declared = entry.get("extruder")
        if declared is not None:
            declared_extruder = _parse_filament_extruder_field(declared, label)
            if declared_extruder != extruder:
                raise FilamentSettingsError(
                    f"{label} declares extruder {declared_extruder} but its JSON key says {extruder}"
                )
    colour = _filament_field(entry, ("color", "colour", "color_hex"))
    if colour is None:
        raise FilamentSettingsError(f"{label} is missing the required 'color' field")
    filament_type = _filament_field(entry, ("type", "filament_type", "slicer_filament_type"))
    if filament_type is None:
        raise FilamentSettingsError(f"{label} is missing the required 'type' field")
    density = _filament_field(entry, ("density", "density_g_cm3"))
    filament_id = _filament_field(entry, ("filament_id",))
    name = _filament_field(entry, ("name",))
    try:
        slot = FilamentSlot(
            extruder=extruder,
            color_hex=colour,
            filament_type=filament_type,
            density_g_cm3=density,
            filament_id=filament_id,
            name=name,
        )
    except FilamentSettingsError as exc:
        raise FilamentSettingsError(f"{label}: {exc}") from exc
    return extruder, slot


def _add_filament_slot(slots: Dict[int, FilamentSlot], slot: FilamentSlot, source: str) -> None:
    """Add one slot to *slots*, rejecting duplicate extruder numbers."""
    if slot.extruder in slots:
        raise FilamentSettingsError(f"{source} contains duplicate extruder {slot.extruder}")
    slots[slot.extruder] = slot


def load_filaments(filament_path: Path) -> Dict[int, FilamentSlot]:
    """Load and validate a JSON filament table used for project settings.

    Two JSON shapes are accepted::

        {"1": {"color": "#161616", "type": "PLA", "density": 1.24,
               "filament_id": "GFB98A2CA6", "name": "Bambu PLA Basic"}}

        [{"extruder": 1, "color": "#161616", "type": "PLA", "density": 1.24}]

    ``color`` (aliases ``colour``/``color_hex``) and ``type`` (aliases
    ``filament_type``/``slicer_filament_type``) are required; ``density``
    (alias ``density_g_cm3``), ``filament_id`` and ``name`` are optional.
    Extruder numbers are 1-based and must be in ``1..MAX_EXTRUDER``.  Malformed
    JSON, an empty table, duplicate extruders or invalid field values raise
    :class:`FilamentSettingsError` with an actionable message.
    """
    path = Path(filament_path)
    if not path.exists():
        raise FilamentSettingsError(f"filament settings file not found: {path}")
    if not path.is_file():
        raise FilamentSettingsError(f"filament settings path is not a regular file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FilamentSettingsError(f"cannot read filament settings file {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise FilamentSettingsError(f"filament settings file {path} is not valid UTF-8: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FilamentSettingsError(
            f"filament settings file {path} is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc

    source = f"filament settings file {path}"
    slots: Dict[int, FilamentSlot] = {}
    if isinstance(payload, dict):
        for key, entry in payload.items():
            extruder = _parse_extruder_key(key, source)
            _parsed_extruder, slot = _parse_filament_entry(entry, f"{source} entry {key!r}", extruder)
            _add_filament_slot(slots, slot, source)
    elif isinstance(payload, list):
        for index, entry in enumerate(payload):
            _parsed_extruder, slot = _parse_filament_entry(
                entry, f"{source} entry #{index + 1}", None
            )
            _add_filament_slot(slots, slot, source)
    else:
        raise FilamentSettingsError(
            f"{source} must be a JSON object keyed by extruder number or a list of filament "
            f"objects, got {type(payload).__name__}"
        )
    if not slots:
        raise FilamentSettingsError(f"{source} contains no filament entries")
    return {extruder: slots[extruder] for extruder in sorted(slots)}


def _validate_filaments_argument(
    filaments: Mapping[int, FilamentSlot],
) -> Dict[int, FilamentSlot]:
    """Validate a programmatic filament mapping and return it as a fresh dict."""
    try:
        raw = dict(filaments)
    except (TypeError, ValueError) as exc:
        raise FilamentSettingsError(
            f"filaments must be a mapping of extruder number to FilamentSlot: {exc}"
        ) from exc
    if not raw:
        raise FilamentSettingsError("filaments contains no entries")
    validated: Dict[int, FilamentSlot] = {}
    for key, slot in raw.items():
        if isinstance(key, bool) or not isinstance(key, int):
            raise FilamentSettingsError(
                f"filaments has a non-integer extruder key {key!r}; keys must be integers"
            )
        if not isinstance(slot, FilamentSlot):
            raise FilamentSettingsError(
                f"filaments entry {key!r} is not a FilamentSlot, got {type(slot).__name__}"
            )
        if key != slot.extruder:
            raise FilamentSettingsError(
                f"filaments key {key!r} does not match the slot extruder {slot.extruder}"
            )
        validated[key] = slot
    return {extruder: validated[extruder] for extruder in sorted(validated)}


def _validate_extruder(value: object, context: str) -> int:
    """Validate one extruder number (integer in ``1..MAX_EXTRUDER``)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise MappingError(f"{context}: extruder must be an integer, got {value!r}")
    if not 1 <= value <= MAX_EXTRUDER:
        raise MappingError(f"{context}: extruder {value} is out of range 1..{MAX_EXTRUDER}")
    return value


def build_model_settings_config(
    assignments: Mapping[int, int],
    object_names: Mapping[int, str],
    object_settings: Optional[Mapping[int, Mapping[str, str]]] = None,
) -> str:
    """Build the ``Metadata/model_settings.config`` XML for the given assignments.

    Returns the exact XML text written into the 3MF archive; each object gets a
    ``<metadata key="name">`` entry when its name is known and always a
    ``<metadata key="extruder">`` entry with a 1-based extruder number.
    ``object_settings`` adds print settings of an object (Bambu Studio /
    OrcaSlicer keys such as ``sparse_infill_density``).
    """
    lines: List[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<config>",
    ]
    for object_id in sorted(assignments):
        if isinstance(object_id, bool) or not isinstance(object_id, int) or object_id <= 0:
            raise MappingError(f"invalid object id {object_id!r} in model settings assignments")
        extruder = _validate_extruder(assignments[object_id], f"object id {object_id}")
        object_name = object_names.get(object_id)
        lines.append(f'  <object id="{object_id}">')
        if isinstance(object_name, str) and object_name:
            lines.append(f'    <metadata key="name" value={quoteattr(object_name)}/>')
        lines.append(f'    <metadata key="extruder" value="{extruder}"/>')
        for key, value in sorted((object_settings or {}).get(object_id, {}).items()):
            if key in ("name", "extruder") or not re.fullmatch(r"[A-Za-z0-9_]+", str(key)):
                raise MappingError(f"invalid print setting {key!r} for object id {object_id}")
            lines.append(f"    <metadata key={quoteattr(str(key))} value={quoteattr(str(value))}/>")
        lines.append("  </object>")
    lines.append("</config>")
    return "\n".join(lines) + "\n"


def _format_filament_density(value: Optional[float]) -> str:
    """Format a density for project settings (``"1.24"`` style strings)."""
    if value is None:
        return _DEFAULT_FILAMENT_DENSITY
    return f"{value:g}"


def build_project_settings_config(
    filaments: Mapping[int, FilamentSlot],
    existing: Optional[Mapping[str, Any]] = None,
) -> str:
    """Build (or merge) the Bambu Studio / Orca Slicer project settings JSON.

    The returned JSON (``indent=2``) contains parallel filament arrays sized to
    the highest supplied extruder index, with gap slots filled with defaults
    (colour ``#FFFFFF``, type ``PLA``, density ``1.24``, diameter ``1.75``):
    ``filament_colour``, ``filament_type``, ``filament_density`` and
    ``filament_diameter``.  ``filament_ids`` and ``filament_settings_id`` are
    added only when at least one slot supplies a ``filament_id`` or ``name``
    (gaps use ``""``).  When *existing* is given (the parsed JSON of an
    existing ``Metadata/project_settings.config``), all unrelated keys are
    preserved and only the filament arrays are replaced.  An empty filament
    mapping raises :class:`FilamentSettingsError`.
    """
    validated = _validate_filaments_argument(filaments)
    highest_extruder = max(validated)
    colours: List[str] = []
    types: List[str] = []
    densities: List[str] = []
    filament_ids: List[str] = []
    settings_ids: List[str] = []
    has_metadata = False
    for extruder in range(1, highest_extruder + 1):
        slot = validated.get(extruder)
        if slot is None:
            colours.append(_DEFAULT_FILAMENT_COLOUR)
            types.append(_DEFAULT_FILAMENT_TYPE)
            densities.append(_DEFAULT_FILAMENT_DENSITY)
            filament_ids.append("")
            settings_ids.append("")
            continue
        colours.append(slot.color_hex)
        types.append(slot.filament_type)
        densities.append(_format_filament_density(slot.density_g_cm3))
        filament_ids.append(slot.filament_id or "")
        settings_ids.append(slot.name or "")
        if slot.filament_id is not None or slot.name is not None:
            has_metadata = True

    merged: Dict[str, Any] = {}
    if existing is not None:
        if not isinstance(existing, dict):
            raise FilamentSettingsError(
                f"existing project settings must be a JSON object, got {type(existing).__name__}"
            )
        merged = dict(existing)
        # Keep the ids arrays present and parallel when merging a real slicer
        # file that already carries them, even if this filament table omits ids.
        if "filament_ids" in merged or "filament_settings_id" in merged:
            has_metadata = True

    generated: Dict[str, Any] = {
        "filament_colour": colours,
        "filament_type": types,
        "filament_density": densities,
        "filament_diameter": [_DEFAULT_FILAMENT_DIAMETER] * highest_extruder,
    }
    if has_metadata:
        generated["filament_ids"] = filament_ids
        generated["filament_settings_id"] = settings_ids
    merged.update(generated)
    return json.dumps(merged if existing is not None else generated, indent=2)


def _build_unknown_component_error(
    unmatched_names: Sequence[str],
    available_names: Sequence[str],
) -> UnknownComponentError:
    """Build an actionable error for mapping names that match no 3MF object."""
    quoted = ", ".join(repr(name) for name in list(unmatched_names)[:10])
    if len(unmatched_names) > 10:
        quoted += f", ... ({len(unmatched_names) - 10} more)"
    shown_names = list(available_names)[:20]
    names_text = ", ".join(shown_names) if shown_names else "(no named objects found in the 3D model)"
    if len(available_names) > 20:
        names_text += f", ... ({len(available_names) - 20} more)"
    return UnknownComponentError(
        f"mapping name(s) not found in the 3MF model: {quoted}. "
        'Matching is exact and by 3MF object name (the <object name="..."> attribute '
        'or a <metadata name="name"> value). '
        f"Available object names: {names_text}"
    )


def _resolve_assignments_with_stats(
    objects: Sequence[ThreeMFObject],
    mapping: Mapping[str, int],
    default_extruder: int,
    only_mapped: bool,
    strict: bool = True,
) -> Tuple[Dict[int, int], int, int, Tuple[str, ...]]:
    """Resolve object-id to extruder assignments.

    Returns ``(assignments, mapped, defaulted, unmatched_names)``.  With
    *strict* a mapping name that matches no object raises
    :class:`UnknownComponentError`; otherwise it is skipped with a warning
    (Fusion does not export surfaces or hidden bodies, so the palette's
    mapping can legitimately name bodies that are absent from the 3MF).
    """
    default_value = _validate_extruder(default_extruder, "default extruder")
    object_list = list(objects)

    name_to_objects: Dict[str, List[ThreeMFObject]] = {}
    for obj in object_list:
        for name in obj.names:
            name_to_objects.setdefault(name, []).append(obj)
    unmatched = [name for name in mapping if name not in name_to_objects]
    if unmatched:
        if strict:
            raise _build_unknown_component_error(unmatched, sorted(name_to_objects))
        LOGGER.warning(
            "skipping %d mapping name(s) not found in the 3MF model: %s",
            len(unmatched),
            ", ".join(repr(name) for name in unmatched[:10]),
        )

    assignments: Dict[int, int] = {}
    assigned_by: Dict[int, str] = {}
    mapped_ids: Set[int] = set()
    conflicts: List[Tuple[int, str, int, str, int]] = []
    for obj in sorted(object_list, key=lambda item: item.id):
        matching_names = [name for name in obj.names if name in mapping]
        if not matching_names:
            continue
        if len(matching_names) > 1:
            LOGGER.warning(
                "object id %d matches multiple mapping entries %s; using %r",
                obj.id,
                matching_names,
                matching_names[0],
            )
        mapping_name = matching_names[0]
        extruder = mapping[mapping_name]
        if not obj.ancestor_ids:
            LOGGER.warning(
                "object id %d (%r) matches mapping entry %r but is not reachable from any build item; skipping",
                obj.id,
                obj.name,
                mapping_name,
            )
            continue
        for ancestor_id in obj.ancestor_ids:
            previous = assignments.get(ancestor_id)
            if previous is None:
                assignments[ancestor_id] = extruder
                assigned_by[ancestor_id] = mapping_name
                mapped_ids.add(ancestor_id)
                LOGGER.debug(
                    "mapped %r -> object id %d -> extruder %d",
                    mapping_name,
                    ancestor_id,
                    extruder,
                )
            elif previous != extruder:
                conflicts.append(
                    (
                        ancestor_id,
                        assigned_by.get(ancestor_id, "?"),
                        previous,
                        mapping_name,
                        extruder,
                    )
                )

    if conflicts:
        details = "; ".join(
            "object id {} already assigned extruder {} via {!r}, but {!r} maps to extruder {}".format(
                object_id,
                previous,
                first_name,
                second_name,
                second_extruder,
            )
            for object_id, first_name, previous, second_name, second_extruder in conflicts[:10]
        )
        if len(conflicts) > 10:
            details += "; ... ({} more conflict(s))".format(len(conflicts) - 10)
        raise ConflictingAssignmentError(
            "conflicting extruder assignments for the same top-level 3MF object: {}. "
            "Metadata/model_settings.config can only assign one extruder per object, so "
            "this mapping cannot be represented. This usually means Fusion exported "
            "several bodies/components nested inside a single top-level object; export "
            "one printable object per body, or map only one of the conflicting names.".format(
                details
            )
        )

    defaulted_count = 0
    if only_mapped:
        LOGGER.debug("only-mapped mode: unmapped build items are excluded from the output")
    else:
        for obj in sorted(object_list, key=lambda item: item.id):
            if not obj.is_build_item or obj.id in assignments:
                continue
            assignments[obj.id] = default_value
            defaulted_count += 1
            LOGGER.debug("defaulted object id %d (%r) to extruder %d", obj.id, obj.name, default_value)
        if defaulted_count:
            LOGGER.info("applied default extruder %d to %d unmapped build item(s)", default_value, defaulted_count)

    ordered_assignments = {object_id: assignments[object_id] for object_id in sorted(assignments)}
    return ordered_assignments, len(mapped_ids), defaulted_count, tuple(unmatched)


def resolve_assignments(
    objects: Sequence[ThreeMFObject],
    mapping: Mapping[str, int],
    default_extruder: int = 1,
    only_mapped: bool = False,
    strict: bool = True,
) -> Dict[int, int]:
    """Resolve 3MF object ids to 1-based extruder numbers.

    Mapping names are matched exactly against every object name.  When a name
    belongs to a nested component, the extruder is assigned to the containing
    build-item object id(s).  Unmapped build items receive *default_extruder*
    unless *only_mapped* is set.  Raises :class:`UnknownComponentError` when a
    mapping name matches no object name (only with *strict*; otherwise such
    names are skipped with a warning), and
    :class:`ConflictingAssignmentError` when two mapping names that resolve to
    the same top-level object request different extruders.
    """
    validated_mapping = _validate_mapping_argument(mapping)
    assignments, _, _, _ = _resolve_assignments_with_stats(
        objects, validated_mapping, default_extruder, only_mapped, strict=strict
    )
    return assignments


def _read_entry_payload(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """Read one archive entry, converting low-level ZIP failures into clear errors."""
    if info.is_dir():
        # ZIP directory entries conventionally carry no payload; avoid reading them.
        return b""
    try:
        return archive.read(info)
    except NotImplementedError as exc:
        raise Invalid3MFError(
            f"archive entry {info.filename!r} uses unsupported compression method "
            f"{info.compress_type}; it cannot be preserved byte-identically"
        ) from exc
    except RuntimeError as exc:
        raise Invalid3MFError(f"archive entry {info.filename!r} is encrypted or unreadable: {exc}") from exc
    except zipfile.BadZipFile as exc:
        raise Invalid3MFError(f"archive entry {info.filename!r} is corrupt: {exc}") from exc


def _write_entry(
    output_archive: zipfile.ZipFile,
    source_info: Optional[zipfile.ZipInfo],
    data: bytes,
    entry_name: Optional[str] = None,
) -> None:
    """Write one entry, preserving ZIP metadata from *source_info* when given.

    *entry_name* names a newly added entry and is required when *source_info*
    is ``None``; replaced entries keep their original name.
    """
    if source_info is not None:
        compress_type = source_info.compress_type
        info = zipfile.ZipInfo(filename=source_info.filename, date_time=source_info.date_time)
        info.compress_type = compress_type
        info.external_attr = source_info.external_attr
        info.internal_attr = source_info.internal_attr
        info.create_system = source_info.create_system
        info.comment = source_info.comment
    else:
        if entry_name is None:
            raise ThreeMFError("cannot add an archive entry without a part name")
        info = zipfile.ZipInfo(entry_name, time.localtime()[:6])
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        info.create_system = 3
    try:
        output_archive.writestr(info, data)
    except NotImplementedError:
        LOGGER.warning(
            "compression method %d is not writable for entry %r; recompressing with DEFLATE",
            info.compress_type,
            info.filename,
        )
        info.compress_type = zipfile.ZIP_DEFLATED
        output_archive.writestr(info, data)
    except (zipfile.BadZipFile, ValueError, OSError, RuntimeError) as exc:
        raise ThreeMFError(f"failed to write archive entry {info.filename!r}: {exc}") from exc


def _read_existing_project_settings(archive: zipfile.ZipFile) -> Optional[Dict[str, Any]]:
    """Return the parsed existing project_settings.config, or ``None`` when absent."""
    matching_infos = [info for info in archive.infolist() if info.filename == PROJECT_SETTINGS_PART]
    if not matching_infos:
        return None
    try:
        raw = _read_entry_payload(archive, matching_infos[0])
    except Invalid3MFError as exc:
        raise FilamentSettingsError(f"could not read existing '{PROJECT_SETTINGS_PART}': {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FilamentSettingsError(
            f"existing '{PROJECT_SETTINGS_PART}' in the input archive is not valid UTF-8: {exc}"
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FilamentSettingsError(
            f"existing '{PROJECT_SETTINGS_PART}' in the input archive is not valid JSON: "
            f"{exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc
    if not isinstance(payload, dict):
        raise FilamentSettingsError(
            f"existing '{PROJECT_SETTINGS_PART}' in the input archive must be a JSON object, "
            f"got {type(payload).__name__}"
        )
    LOGGER.info("merging existing '%s' from the input archive", PROJECT_SETTINGS_PART)
    return payload


def _repack_archive(
    archive: zipfile.ZipFile,
    generated_entries: Mapping[str, bytes],
    output_path: Path,
) -> None:
    """Copy every input entry to *output_path* and add/replace generated entries.

    *generated_entries* maps managed part names to their new UTF-8 payloads.
    Each managed part is written exactly once: at the position of the first
    existing entry with that name, or appended after the copied entries when
    the input archive does not contain it.
    """
    generated = dict(generated_entries)
    written: Set[str] = set()
    with zipfile.ZipFile(output_path, "w") as output_archive:
        output_archive.comment = archive.comment
        for source_info in archive.infolist():
            if source_info.filename in generated:
                if source_info.filename in written:
                    LOGGER.debug("dropping duplicate '%s' entry from the input archive", source_info.filename)
                    continue
                LOGGER.info("replacing existing '%s' entry in the output archive", source_info.filename)
                _write_entry(output_archive, source_info, generated[source_info.filename])
                written.add(source_info.filename)
                continue
            _write_entry(output_archive, source_info, _read_entry_payload(archive, source_info))
        for entry_name, entry_data in generated.items():
            if entry_name in written:
                continue
            LOGGER.info("adding '%s' entry to the output archive", entry_name)
            _write_entry(output_archive, None, entry_data, entry_name=entry_name)


def _entry_payload_equal(
    source_archive: zipfile.ZipFile,
    source_info: zipfile.ZipInfo,
    output_archive: zipfile.ZipFile,
    output_info: zipfile.ZipInfo,
) -> bool:
    """Return true when two archive entries carry byte-identical payloads."""
    if source_info.is_dir() or output_info.is_dir():
        return (
            source_info.is_dir()
            and output_info.is_dir()
            and source_info.file_size == output_info.file_size
        )
    try:
        with source_archive.open(source_info, "r") as source_handle, output_archive.open(
            output_info, "r"
        ) as output_handle:
            while True:
                source_chunk = source_handle.read(_COMPARE_CHUNK_SIZE)
                output_chunk = output_handle.read(_COMPARE_CHUNK_SIZE)
                if source_chunk != output_chunk:
                    return False
                if not source_chunk:
                    return True
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ThreeMFError(f"could not compare archive entry {source_info.filename!r}: {exc}") from exc


def _parse_model_settings_config(xml_text: str, source: str) -> Dict[int, int]:
    """Parse a model_settings.config payload back into object-id to extruder pairs."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ThreeMFError(f"generated '{MODEL_SETTINGS_PART}' for {source} is not valid XML: {exc}") from exc
    if _localname(root.tag) != "config":
        raise ThreeMFError(
            f"generated '{MODEL_SETTINGS_PART}' for {source} has root element "
            f"'{_localname(root.tag)}', expected 'config'"
        )
    assignments: Dict[int, int] = {}
    for object_element in root:
        if _localname(object_element.tag) != "object":
            continue
        raw_object_id = object_element.get("id")
        try:
            object_id = int(raw_object_id)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ThreeMFError(
                f"generated '{MODEL_SETTINGS_PART}' for {source} has an object with invalid id "
                f"{raw_object_id!r}"
            ) from exc
        extruder_text: Optional[str] = None
        for metadata in object_element:
            if _localname(metadata.tag) == "metadata" and metadata.get("key") == "extruder":
                extruder_text = metadata.get("value")
        if extruder_text is None:
            raise ThreeMFError(
                f"generated '{MODEL_SETTINGS_PART}' for {source} is missing extruder metadata for object id {object_id}"
            )
        try:
            extruder = int(extruder_text)
        except ValueError as exc:
            raise ThreeMFError(
                f"generated '{MODEL_SETTINGS_PART}' for {source} has non-integer extruder "
                f"{extruder_text!r} for object id {object_id}"
            ) from exc
        if object_id in assignments:
            raise ThreeMFError(
                f"generated '{MODEL_SETTINGS_PART}' for {source} contains duplicate object id {object_id}"
            )
        assignments[object_id] = extruder
    return assignments


def _verify_repacked_archive(
    input_path: Path,
    output_path: Path,
    expected_assignments: Mapping[int, int],
    expected_project_settings: Optional[Mapping[str, Any]] = None,
) -> None:
    """Verify the repacked archive: entry list, byte identity and contents.

    Managed parts are the always-generated ``Metadata/model_settings.config``
    and, when *expected_project_settings* is given, the generated
    ``Metadata/project_settings.config``.  Every other original entry must be
    present, in order, with byte-identical payloads; an existing project
    settings entry is preserved byte-identically when it is not managed.
    """
    managed_parts = {MODEL_SETTINGS_PART}
    if expected_project_settings is not None:
        managed_parts.add(PROJECT_SETTINGS_PART)
    try:
        with zipfile.ZipFile(input_path, "r") as source_archive, zipfile.ZipFile(
            output_path, "r"
        ) as output_archive:
            source_infos = source_archive.infolist()
            output_infos = output_archive.infolist()

            # No testzip(): the byte compare below reads (and CRC-checks) every
            # copied entry, and the managed entries are parsed back afterwards.
            for managed_part in sorted(managed_parts):
                managed_infos = [info for info in output_infos if info.filename == managed_part]
                if len(managed_infos) != 1:
                    raise ThreeMFError(
                        f"repacked 3MF must contain exactly one '{managed_part}' entry, "
                        f"found {len(managed_infos)}"
                    )

            source_payload_infos = [
                info for info in source_infos if info.filename not in managed_parts
            ]
            output_payload_infos = [
                info for info in output_infos if info.filename not in managed_parts
            ]
            if len(source_payload_infos) != len(output_payload_infos):
                raise ThreeMFError(
                    f"repacked 3MF lost or added archive entries: input had "
                    f"{len(source_payload_infos)} unmanaged entries, output has "
                    f"{len(output_payload_infos)}"
                )
            for source_info, output_info in zip(source_payload_infos, output_payload_infos):
                if source_info.filename != output_info.filename:
                    raise ThreeMFError(
                        f"archive entry order changed: expected {source_info.filename!r}, "
                        f"found {output_info.filename!r}"
                    )
                if not _entry_payload_equal(source_archive, source_info, output_archive, output_info):
                    raise ThreeMFError(
                        f"archive entry {source_info.filename!r} was not preserved byte-identically"
                    )

            model_settings_infos = [
                info for info in output_infos if info.filename == MODEL_SETTINGS_PART
            ]
            config_text = output_archive.read(model_settings_infos[0]).decode("utf-8")
            parsed_assignments = _parse_model_settings_config(config_text, source=str(output_path))
            expected = {int(object_id): int(extruder) for object_id, extruder in expected_assignments.items()}
            if parsed_assignments != expected:
                raise ThreeMFError(
                    "repacked model_settings.config does not match the resolved assignments: "
                    f"expected {expected}, found {parsed_assignments}"
                )

            if expected_project_settings is not None:
                project_infos = [
                    info for info in output_infos if info.filename == PROJECT_SETTINGS_PART
                ]
                try:
                    project_text = output_archive.read(project_infos[0]).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ThreeMFError(
                        f"generated '{PROJECT_SETTINGS_PART}' is not valid UTF-8: {exc}"
                    ) from exc
                try:
                    parsed_project = json.loads(project_text)
                except json.JSONDecodeError as exc:
                    raise ThreeMFError(
                        f"generated '{PROJECT_SETTINGS_PART}' is not valid JSON: {exc.msg} "
                        f"(line {exc.lineno}, column {exc.colno})"
                    ) from exc
                if not isinstance(parsed_project, dict):
                    raise ThreeMFError(
                        f"generated '{PROJECT_SETTINGS_PART}' must be a JSON object, "
                        f"got {type(parsed_project).__name__}"
                    )
                if parsed_project != dict(expected_project_settings):
                    raise ThreeMFError(
                        "repacked project_settings.config does not match the generated filament table"
                    )
    except (zipfile.BadZipFile, OSError) as exc:
        raise ThreeMFError(f"could not verify repacked 3MF output: {exc}") from exc


def _resolve_object_settings(
    objects: Sequence[ThreeMFObject],
    object_settings: Mapping[str, Mapping[str, str]],
    assignments: Mapping[int, int],
) -> Dict[int, Dict[str, str]]:
    """Print settings by top-level object id, matched by name like the extruders.

    When several named bodies end up in one object, the first one's settings count.
    """
    resolved: Dict[int, Dict[str, str]] = {}
    for obj in sorted(objects, key=lambda item: item.id):
        names = [name for name in obj.names if name in object_settings and object_settings[name]]
        if not names:
            continue
        for ancestor_id in obj.ancestor_ids:
            if ancestor_id in assignments and ancestor_id not in resolved:
                resolved[ancestor_id] = {str(key): str(value) for key, value in object_settings[names[0]].items()}
    return resolved


def _build_object_names(
    objects: Sequence[ThreeMFObject],
    assignments: Mapping[int, int],
) -> Dict[int, str]:
    """Map each assigned object id to its known display name."""
    objects_by_id = {obj.id: obj for obj in objects}
    names: Dict[int, str] = {}
    for object_id in sorted(assignments):
        obj = objects_by_id.get(object_id)
        if obj is not None and obj.name:
            names[object_id] = obj.name
    return names


def _remove_file_quietly(path: Path) -> None:
    """Delete *path* when it exists, ignoring filesystem errors."""
    try:
        path.unlink()
    except OSError:
        pass


def _validate_input_path(input_path: Path) -> None:
    """Validate that the input path exists and is a regular file."""
    if not input_path.exists():
        raise Invalid3MFError(f"input 3MF file not found: {input_path}")
    if not input_path.is_file():
        raise Invalid3MFError(f"input path is not a regular file: {input_path}")


def _open_input_archive(input_path: Path) -> zipfile.ZipFile:
    """Validate and open an input 3MF archive, converting ZIP failures into clear errors."""
    _validate_input_path(input_path)
    try:
        return zipfile.ZipFile(input_path, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise Invalid3MFError(f"not a valid 3MF (ZIP) file: {input_path}: {exc}") from exc


def inject(
    input_path: Path,
    mapping: Mapping[str, int],
    output_path: Path,
    default_extruder: int = 1,
    only_mapped: bool = False,
    filaments: Optional[Mapping[int, FilamentSlot]] = None,
    strict: bool = False,
    object_settings: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> InjectionResult:
    """Inject slicer metadata into a 3MF archive.

    Always writes ``Metadata/model_settings.config``.  When *filaments* is
    given, also writes (or merges into) ``Metadata/project_settings.config``
    with the filament colour/type/density/diameter arrays; an existing project
    settings entry in the input archive has its unrelated JSON keys preserved.

    Every original archive entry is preserved byte-identically and only the
    managed settings entries are added or replaced.  The output is written
    atomically: the archive is built in a temporary file next to
    *output_path* and moved into place with :func:`os.replace` only after the
    repacked archive has been verified.  ``output_path`` may equal
    ``input_path`` for an in-place update.

    Raises :class:`Invalid3MFError` for unusable input archives,
    :class:`MappingError` for malformed mappings,
    :class:`UnknownComponentError` when a mapping name matches no object and
    *strict* is set (by default such names are skipped with a warning and
    listed in :attr:`InjectionResult.unmatched_names`),
    :class:`ConflictingAssignmentError` when two mapping names resolve to the
    same top-level object with different extruders, and
    :class:`FilamentSettingsError` for malformed filament tables or existing
    project settings payloads.

    *object_settings* maps body names to print settings of their object
    (Bambu Studio / OrcaSlicer keys, see body_settings.to_bambu).
    """
    source_path = Path(input_path)
    destination_path = Path(output_path)
    validated_mapping = _validate_mapping_argument(mapping)
    validated_default = _validate_extruder(default_extruder, "default extruder")
    validated_filaments: Optional[Dict[int, FilamentSlot]] = None
    if filaments is not None:
        validated_filaments = _validate_filaments_argument(filaments)
    archive = _open_input_archive(source_path)

    temp_path: Optional[Path] = None
    try:
        with archive:
            objects = extract_objects(archive)
            assignments, mapped_count, defaulted_count, unmatched_names = (
                _resolve_assignments_with_stats(
                    objects, validated_mapping, validated_default, only_mapped, strict=strict
                )
            )
            object_names = _build_object_names(objects, assignments)
            config_xml = build_model_settings_config(
                assignments, object_names, _resolve_object_settings(objects, object_settings or {}, assignments)
            )

            project_settings_xml: Optional[str] = None
            project_settings_payload: Optional[Dict[str, Any]] = None
            generated_entries: Dict[str, bytes] = {MODEL_SETTINGS_PART: config_xml.encode("utf-8")}
            if validated_filaments is not None:
                existing_project_settings = _read_existing_project_settings(archive)
                project_settings_xml = build_project_settings_config(
                    validated_filaments, existing=existing_project_settings
                )
                project_settings_payload = json.loads(project_settings_xml)
                generated_entries[PROJECT_SETTINGS_PART] = project_settings_xml.encode("utf-8")

            destination_path.parent.mkdir(parents=True, exist_ok=True)
            file_descriptor, temp_name = tempfile.mkstemp(
                prefix=".threemf_inject_", suffix=".3mf", dir=str(destination_path.parent)
            )
            os.close(file_descriptor)
            temp_path = Path(temp_name)
            _repack_archive(archive, generated_entries, temp_path)

        _verify_repacked_archive(
            source_path,
            temp_path,
            assignments,
            expected_project_settings=project_settings_payload,
        )
        # mkstemp files are owner-only; the 3MF is a normal user document.
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, destination_path)
        temp_path = None
    finally:
        if temp_path is not None:
            _remove_file_quietly(temp_path)

    LOGGER.info(
        "wrote %d object assignment(s) to '%s' (mapped=%d, defaulted=%d)",
        len(assignments),
        destination_path,
        mapped_count,
        defaulted_count,
    )
    if validated_filaments is not None:
        LOGGER.info(
            "wrote %d filament slot(s) into '%s'",
            len(validated_filaments),
            PROJECT_SETTINGS_PART,
        )
    return InjectionResult(
        input_path=source_path,
        output_path=destination_path,
        assignments=dict(assignments),
        object_names=object_names,
        mapped_count=mapped_count,
        defaulted_count=defaulted_count,
        config_xml=config_xml,
        filament_count=len(validated_filaments) if validated_filaments is not None else 0,
        project_settings_xml=project_settings_xml,
        unmatched_names=unmatched_names,
    )


def inject_file(
    input_path: Path,
    mapping_path: Path,
    output_path: Path,
    default_extruder: int = 1,
    only_mapped: bool = False,
    filaments_path: Optional[Path] = None,
    strict: bool = False,
) -> InjectionResult:
    """Load mapping (and optional filaments) files and inject them into *input_path*.

    Writes *output_path* atomically; see :func:`inject` for the exact metadata
    entries produced and the error cases.
    """
    mapping = load_mapping(mapping_path)
    filaments: Optional[Dict[int, FilamentSlot]] = None
    if filaments_path is not None:
        filaments = load_filaments(filaments_path)
    return inject(
        Path(input_path),
        mapping,
        Path(output_path),
        default_extruder=default_extruder,
        only_mapped=only_mapped,
        filaments=filaments,
        strict=strict,
    )


def _quoted_name(name: Optional[str]) -> str:
    """Quote an object name for the listing; mark unnamed objects explicitly."""
    if name:
        return json.dumps(name)
    return "<unnamed>"


def _format_object_list(objects: Sequence[ThreeMFObject]) -> str:
    """Format one line per build item, with nested component names indented below it."""
    object_list = list(objects)
    build_items = sorted((obj for obj in object_list if obj.is_build_item), key=lambda obj: obj.id)
    nested_objects = [obj for obj in object_list if not obj.is_build_item and obj.ancestor_ids]
    lines: List[str] = []
    for build_item in build_items:
        lines.append(f"id={build_item.id}  name={_quoted_name(build_item.name)}")
        children = sorted(
            (obj for obj in nested_objects if build_item.id in obj.ancestor_ids),
            key=lambda obj: obj.id,
        )
        for child in children:
            lines.append(f"    component id={child.id}  name={_quoted_name(child.name)}")
    return "\n".join(lines)


def list_objects(input_path: Path) -> str:
    """Return a human-readable object-name listing for a 3MF archive.

    Build-item (top-level printable) objects are listed one per line as
    ``id=<N>  name=<quoted name or <unnamed>>``; nested component names are
    listed indented under every build-item ancestor they resolve to, so the
    user can tell which name maps to which object id.  The listing ends with a
    hint showing the shape of the mapping JSON.  The archive is not modified.

    Raises :class:`Invalid3MFError` (or its subclass
    :class:`ModelPartNotFoundError`) for unusable input, exactly like
    :func:`inject`.
    """
    source_path = Path(input_path)
    archive = _open_input_archive(source_path)
    with archive:
        objects = extract_objects(archive)
    body = _format_object_list(objects) or "(no build-item objects found)"
    header = f"Build-item objects in '{source_path}':"
    hint = 'Use these names as keys in your mapping JSON: {"<object name>": <extruder 1-16>}'
    return f"{header}\n{body}\n{hint}\n"


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="threemf_injector.py",
        description=(
            "Inject Bambu Studio / Orca Slicer per-object extruder assignments "
            "(Metadata/model_settings.config) into a 3MF archive, optionally with "
            "project filament arrays (Metadata/project_settings.config). "
            "Use --list-objects to discover the object names of an export first."
        ),
        epilog=(
            "Exit codes: 0 success, 1 processing error, 2 usage error. "
            "Examples: python3 threemf_injector.py --input model.3mf "
            "--mapping mapping.json --output model_injected.3mf ; "
            "python3 threemf_injector.py --input model.3mf --mapping mapping.json "
            "--filaments filaments.json --output model_injected.3mf ; "
            "python3 threemf_injector.py --input model.3mf --list-objects"
        ),
    )
    parser.add_argument(
        "-i",
        "--input",
        metavar="PATH",
        help="input .3mf file exported by Fusion 360",
    )
    parser.add_argument(
        "-m",
        "--mapping",
        metavar="PATH",
        help='JSON object mapping 3MF object names to 1-based extruder numbers, e.g. {"Bracket": 1}',
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="output .3mf path; parent directories are created and --output may equal --input",
    )
    parser.add_argument(
        "--default-extruder",
        type=int,
        default=1,
        metavar="N",
        help=f"extruder for unmapped build-item objects (1..{MAX_EXTRUDER}, default: 1)",
    )
    parser.add_argument(
        "--only-mapped",
        action="store_true",
        help="only write objects that were explicitly mapped; never default unmapped objects",
    )
    parser.add_argument(
        "--skip-unknown",
        action="store_true",
        help=(
            "skip mapping names that match no 3MF object (with a warning) instead of failing; "
            "the add-in always skips them"
        ),
    )
    parser.add_argument(
        "--filaments",
        metavar="PATH",
        help=(
            "optional JSON filament table (extruder-keyed object or list of objects) used to "
            "write Metadata/project_settings.config with filament colour/type/density arrays"
        ),
    )
    parser.add_argument(
        "--list-objects",
        action="store_true",
        help=(
            "list build-item and nested component object names of --input and exit; "
            "--mapping and --output are not required in this mode"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run the built-in end-to-end self-test and exit",
    )
    return parser


def _configure_logging(verbose: bool) -> None:
    """Apply the requested log level to the root logger and this module's logger."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.getLogger().setLevel(level)
    LOGGER.setLevel(level)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry point; returns the process exit code."""
    parser = _build_arg_parser()
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    arguments = parser.parse_args(raw_arguments)
    _configure_logging(arguments.verbose)

    if arguments.self_test:
        try:
            return _run_self_test(verbose=arguments.verbose)
        except Exception as exc:  # pragma: no cover - defensive last resort
            print(f"ERROR: self-test crashed: {exc}", file=sys.stderr)
            traceback.print_exc()
            return 1

    if not raw_arguments:
        parser.print_help(sys.stdout)
        return 2

    if arguments.list_objects:
        if not arguments.input:
            parser.error("--input is required with --list-objects")
        if arguments.filaments:
            parser.error("--filaments cannot be combined with --list-objects")
        try:
            listing = list_objects(Path(arguments.input))
        except (ThreeMFError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(listing, end="")
        return 0

    missing_arguments = [
        name
        for name, value in (
            ("--input", arguments.input),
            ("--mapping", arguments.mapping),
            ("--output", arguments.output),
        )
        if not value
    ]
    if missing_arguments:
        parser.error(f"the following arguments are required: {', '.join(missing_arguments)}")
    if not 1 <= arguments.default_extruder <= MAX_EXTRUDER:
        parser.error(f"--default-extruder must be between 1 and {MAX_EXTRUDER}")

    try:
        result = inject_file(
            Path(arguments.input),
            Path(arguments.mapping),
            Path(arguments.output),
            default_extruder=arguments.default_extruder,
            only_mapped=arguments.only_mapped,
            filaments_path=Path(arguments.filaments) if arguments.filaments else None,
            strict=not arguments.skip_unknown,
        )
    except ThreeMFError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive last resort
        print(f"ERROR: unexpected failure: {exc}", file=sys.stderr)
        LOGGER.debug("unexpected failure", exc_info=True)
        return 1

    LOGGER.info("injection complete: '%s' -> '%s'", result.input_path, result.output_path)
    return 0


_SELF_TEST_ENTRY_DATE = (2024, 1, 2, 3, 4, 6)
_SELF_TEST_THUMBNAIL = b"\x89PNG\r\n\x1a\n" + bytes(range(64)) + b"\x00\xff"
_SELF_TEST_MESH = (
    '<mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>'
    '<vertex x="0" y="10" z="0"/></vertices>'
    '<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh>'
)
_SELF_TEST_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
  <Default Extension="png" ContentType="image/png"/>
</Types>
"""
_SELF_TEST_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""
_SELF_TEST_MODEL_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<model unit="millimeter" xml:lang="en-US" '
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
    '  <metadata name="Application">Filamatch self-test</metadata>\n'
    "  <resources>\n"
    f'    <object id="2" type="model" name="Bracket">{_SELF_TEST_MESH}</object>\n'
    f'    <object id="3" type="model" name="Bushing">{_SELF_TEST_MESH}</object>\n'
    '    <object id="4" type="model" name="Clamp Assembly">\n'
    '      <components><component objectid="5"/></components>\n'
    "    </object>\n"
    f'    <object id="5" type="model" name="Clamp Jaw">{_SELF_TEST_MESH}</object>\n'
    f'    <object id="6" type="model" name="Unmapped Plate">{_SELF_TEST_MESH}</object>\n'
    "  </resources>\n"
    "  <build>\n"
    '    <item objectid="2"/>\n'
    '    <item objectid="3"/>\n'
    '    <item objectid="4"/>\n'
    '    <item objectid="6"/>\n'
    "  </build>\n"
    "</model>\n"
)


def _self_test_write_3mf(path: Path, entries: Sequence[Tuple[str, bytes, int]]) -> None:
    """Write a synthetic 3MF archive used by the self-test."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for entry_name, entry_data, compress_type in entries:
            info = zipfile.ZipInfo(entry_name, _SELF_TEST_ENTRY_DATE)
            info.compress_type = compress_type
            info.create_system = 3
            if entry_name.endswith("/"):
                info.external_attr = (0o40755 & 0xFFFF) << 16
            else:
                info.external_attr = (0o644 & 0xFFFF) << 16
            archive.writestr(info, entry_data)


def _self_test_write_json(path: Path, payload: object) -> None:
    """Write a JSON fixture file for the self-test."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _self_test_zip_names(path: Path) -> List[str]:
    """Return archive member names in central-directory order."""
    with zipfile.ZipFile(path, "r") as archive:
        return archive.namelist()


def _self_test_config_metadata(path: Path) -> Dict[int, Dict[str, str]]:
    """Independently parse a repacked model_settings.config from *path*."""
    with zipfile.ZipFile(path, "r") as archive:
        members = archive.namelist()
        if MODEL_SETTINGS_PART not in members:
            raise AssertionError(f"'{MODEL_SETTINGS_PART}' missing from {path}")
        raw_config = archive.read(MODEL_SETTINGS_PART)
    root = ET.fromstring(raw_config)
    if _localname(root.tag) != "config":
        raise AssertionError(f"model_settings.config root element is {_localname(root.tag)!r}, expected 'config'")
    config: Dict[int, Dict[str, str]] = {}
    for element in root:
        if _localname(element.tag) != "object":
            continue
        object_id = int(element.get("id", ""))
        metadata: Dict[str, str] = {}
        for item in element:
            if _localname(item.tag) != "metadata":
                continue
            metadata[item.get("key", "")] = item.get("value", "")
        config[object_id] = metadata
    return config


def _self_test_fixture_entries() -> List[Tuple[str, bytes, int]]:
    """Build the main synthetic Fusion-like 3MF entry list for the self-test."""
    return [
        ("[Content_Types].xml", _SELF_TEST_CONTENT_TYPES.encode("utf-8"), zipfile.ZIP_DEFLATED),
        (RELS_PART, _SELF_TEST_RELS.encode("utf-8"), zipfile.ZIP_DEFLATED),
        (MODEL_PART, _SELF_TEST_MODEL_XML.encode("utf-8"), zipfile.ZIP_DEFLATED),
        ("Metadata/", b"", zipfile.ZIP_STORED),
        ("Metadata/thumbnail.png", _SELF_TEST_THUMBNAIL, zipfile.ZIP_STORED),
    ]


def _self_test_json_member(path: Path, part_name: str) -> Dict[str, Any]:
    """Independently parse a JSON archive member from a repacked 3MF file."""
    with zipfile.ZipFile(path, "r") as archive:
        members = archive.namelist()
        if part_name not in members:
            raise AssertionError(f"'{part_name}' missing from {path}")
        raw_member = archive.read(part_name)
    payload = json.loads(raw_member.decode("utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"'{part_name}' in {path} is not a JSON object")
    return payload


def _self_test_cases(tmp_dir: Path) -> List[Tuple[str, Callable[[], None]]]:
    """Create self-test fixtures and return ``(description, check)`` pairs."""
    fixture_path = tmp_dir / "input.3mf"
    _self_test_write_3mf(fixture_path, _self_test_fixture_entries())
    mapping_path = tmp_dir / "mapping.json"
    main_mapping = {"Bracket": 1, "Bushing": 2, "Clamp Jaw": 3}
    _self_test_write_json(mapping_path, main_mapping)
    output_dir = tmp_dir / "out"
    filaments_dict_path = tmp_dir / "filaments_dict.json"
    _self_test_write_json(
        filaments_dict_path,
        {
            "1": {
                "color": "#161616",
                "type": "PLA",
                "density": 1.24,
                "filament_id": "GFB98A2CA6",
                "name": "Bambu PLA Basic",
            },
            "2": {"color": "#FFFFFF", "type": "PLA", "density": 1.24},
            "3": {"color": "#FF0000", "type": "PETG", "density": 1.27},
        },
    )
    sample_filaments: Dict[int, FilamentSlot] = {
        1: FilamentSlot(1, "#161616", "PLA", 1.24, "GFB98A2CA6", "Bambu PLA Basic"),
        2: FilamentSlot(2, "#FFFFFF", "PLA", 1.24),
        3: FilamentSlot(3, "#FF0000", "PETG", 1.27),
    }

    def check_basic_injection() -> None:
        output = output_dir / "basic.3mf"
        result = inject(fixture_path, main_mapping, output)
        assert output.is_file(), "output 3MF was not created"
        assert dict(result.assignments) == {2: 1, 3: 2, 4: 3, 6: 1}, result.assignments
        assert result.mapped_count == 3, result.mapped_count
        assert result.defaulted_count == 1, result.defaulted_count
        assert result.object_names.get(4) == "Clamp Assembly", result.object_names
        assert result.object_names.get(2) == "Bracket", result.object_names
        assert _self_test_zip_names(output) == _self_test_zip_names(fixture_path) + [MODEL_SETTINGS_PART]
        with zipfile.ZipFile(fixture_path, "r") as source, zipfile.ZipFile(output, "r") as written:
            for name in source.namelist():
                if name.endswith("/"):
                    continue
                assert source.read(name) == written.read(name), f"entry {name!r} was not preserved"
        config = _self_test_config_metadata(output)
        assert set(config) == {2, 3, 4, 6}, config
        assert config[2]["extruder"] == "1", config[2]
        assert config[3]["extruder"] == "2", config[3]
        assert config[4]["extruder"] == "3", config[4]
        assert config[6]["extruder"] == "1", config[6]
        assert config[2]["name"] == "Bracket", config[2]
        assert config[4]["name"] == "Clamp Assembly", config[4]
        assert 5 not in config, "nested object id 5 must resolve to its build-item ancestor 4"

    def check_object_settings() -> None:
        output = output_dir / "settings.3mf"
        inject(fixture_path, main_mapping, output, object_settings={
            "Bracket": {"sparse_infill_density": "40%", "top_shell_layers": "6"},
            "Clamp Jaw": {"wall_loops": "5"},
            "Bushing": {},
        })
        config = _self_test_config_metadata(output)
        assert config[2] == {"name": "Bracket", "extruder": "1", "top_shell_layers": "6",
                             "sparse_infill_density": "40%"}, config[2]
        assert config[4]["wall_loops"] == "5" and "wall_loops" not in config[3], config
        try:
            build_model_settings_config({2: 1}, {}, {2: {"extruder": "3"}})
        except MappingError:
            pass
        else:
            raise AssertionError("a print setting may not replace the extruder")

    def check_extract_objects() -> None:
        with zipfile.ZipFile(fixture_path, "r") as archive:
            objects = extract_objects(archive)
        by_id = {obj.id: obj for obj in objects}
        assert sorted(by_id) == [2, 3, 4, 5, 6], sorted(by_id)
        assert by_id[2].name == "Bracket", by_id[2]
        assert by_id[2].source_part == MODEL_PART, by_id[2]
        assert by_id[2].is_build_item, by_id[2]
        assert not by_id[5].is_build_item, by_id[5]
        assert by_id[4].ancestor_ids == (4,), by_id[4]
        assert by_id[5].ancestor_ids == (4,), by_id[5]
        assert by_id[5].names == ("Clamp Jaw",), by_id[5]
        assert by_id[6].is_build_item, by_id[6]

    def check_only_mapped() -> None:
        output = output_dir / "only_mapped.3mf"
        result = inject(fixture_path, main_mapping, output, only_mapped=True)
        assert dict(result.assignments) == {2: 1, 3: 2, 4: 3}, result.assignments
        assert result.defaulted_count == 0, result.defaulted_count
        assert set(_self_test_config_metadata(output)) == {2, 3, 4}

    def check_default_extruder() -> None:
        output = output_dir / "default5.3mf"
        result = inject(fixture_path, main_mapping, output, default_extruder=5)
        assert dict(result.assignments) == {2: 1, 3: 2, 4: 3, 6: 5}, result.assignments
        assert result.defaulted_count == 1, result.defaulted_count
        assert _self_test_config_metadata(output)[6]["extruder"] == "5"

    def check_repeated_names_and_metadata_fallback() -> None:
        repeated_fixture = tmp_dir / "repeated.3mf"
        repeated_model = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
            "  <resources>\n"
            f'    <object id="2" type="model" name="Dup">{_SELF_TEST_MESH}</object>\n'
            f'    <object id="3" type="model"><metadata name="name">Dup</metadata>{_SELF_TEST_MESH}</object>\n'
            "  </resources>\n"
            "  <build>\n"
            '    <item objectid="2"/>\n'
            '    <item objectid="3"/>\n'
            "  </build>\n"
            "</model>\n"
        )
        _self_test_write_3mf(
            repeated_fixture,
            [
                ("[Content_Types].xml", _SELF_TEST_CONTENT_TYPES.encode("utf-8"), zipfile.ZIP_DEFLATED),
                (RELS_PART, _SELF_TEST_RELS.encode("utf-8"), zipfile.ZIP_DEFLATED),
                (MODEL_PART, repeated_model.encode("utf-8"), zipfile.ZIP_DEFLATED),
            ],
        )
        with zipfile.ZipFile(repeated_fixture, "r") as archive:
            extracted = {obj.id: obj for obj in extract_objects(archive)}
        assert extracted[3].name == "Dup", extracted[3]
        output = output_dir / "repeated_out.3mf"
        result = inject(repeated_fixture, {"Dup": 2}, output)
        assert dict(result.assignments) == {2: 2, 3: 2}, result.assignments
        assert result.mapped_count == 2, result.mapped_count
        config = _self_test_config_metadata(output)
        assert config[2]["name"] == "Dup", config[2]
        assert config[3]["name"] == "Dup", config[3]

    def check_conflicting_assignments() -> None:
        conflict_fixture = tmp_dir / "conflict.3mf"
        conflict_model = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
            "  <resources>\n"
            '    <object id="2" type="model" name="Assembly">\n'
            '      <components><component objectid="3"/><component objectid="4"/></components>\n'
            "    </object>\n"
            f'    <object id="3" type="model" name="Left">{_SELF_TEST_MESH}</object>\n'
            f'    <object id="4" type="model" name="Right">{_SELF_TEST_MESH}</object>\n'
            "  </resources>\n"
            "  <build><item objectid=\"2\"/></build>\n"
            "</model>\n"
        )
        _self_test_write_3mf(
            conflict_fixture,
            [
                ("[Content_Types].xml", _SELF_TEST_CONTENT_TYPES.encode("utf-8"), zipfile.ZIP_DEFLATED),
                (RELS_PART, _SELF_TEST_RELS.encode("utf-8"), zipfile.ZIP_DEFLATED),
                (MODEL_PART, conflict_model.encode("utf-8"), zipfile.ZIP_DEFLATED),
            ],
        )
        conflict_output = output_dir / "conflict_out.3mf"
        try:
            inject(conflict_fixture, {"Left": 1, "Right": 2}, conflict_output)
        except ConflictingAssignmentError as exc:
            message = str(exc)
            assert isinstance(exc, MappingError), exc
            for fragment in ("Left", "Right", "one extruder per object", "nested"):
                assert fragment in message, f"expected {fragment!r} in {message!r}"
        else:
            raise AssertionError("conflicting nested assignments did not raise ConflictingAssignmentError")
        assert not conflict_output.exists(), "conflicting injection must not create an output file"

        # The same extruder for both nested names is representable and must succeed.
        shared_output = output_dir / "conflict_shared.3mf"
        shared_result = inject(conflict_fixture, {"Left": 2, "Right": 2}, shared_output)
        assert dict(shared_result.assignments) == {2: 2}, shared_result.assignments

    def check_unknown_name() -> None:
        output = output_dir / "unknown.3mf"
        try:
            inject(fixture_path, {"Nope": 1}, output, strict=True)
        except UnknownComponentError as exc:
            message = str(exc)
            assert "Nope" in message, message
            assert "Bracket" in message, message
            assert "exact" in message.lower(), message
            assert isinstance(exc, MappingError), exc
        else:
            raise AssertionError("unknown mapping name did not raise UnknownComponentError")
        assert not output.exists(), "failed injection must not create an output file"
        if output.parent.exists():
            leftovers = list(output.parent.glob(".threemf_inject_*.3mf"))
            assert not leftovers, f"temporary files left behind: {leftovers}"

        # The default (add-in) mode skips unknown names and reports them.
        lenient = inject(fixture_path, {"Nope": 1, "Bracket": 2}, output)
        assert lenient.unmatched_names == ("Nope",), lenient.unmatched_names
        assert lenient.assignments.get(2) == 2, lenient.assignments
        assert output.exists(), "lenient injection must write the output"
        try:
            with zipfile.ZipFile(fixture_path, "r") as fixture_archive:
                fixture_objects = extract_objects(fixture_archive)
            resolve_assignments(fixture_objects, {"Nope": 1})
        except UnknownComponentError:
            pass
        else:
            raise AssertionError("resolve_assignments must stay strict by default")

    def check_invalid_inputs() -> None:
        not_a_zip = tmp_dir / "not_a_zip.3mf"
        not_a_zip.write_bytes(b"this is definitely not a zip archive")
        try:
            inject(not_a_zip, {"Bracket": 1}, output_dir / "bad1.3mf")
        except Invalid3MFError as exc:
            assert "not a valid 3MF (ZIP) file" in str(exc), str(exc)
        else:
            raise AssertionError("non-ZIP input did not raise Invalid3MFError")

        missing_model = tmp_dir / "missing_model.3mf"
        _self_test_write_3mf(
            missing_model,
            [("[Content_Types].xml", _SELF_TEST_CONTENT_TYPES.encode("utf-8"), zipfile.ZIP_DEFLATED)],
        )
        try:
            inject(missing_model, {"Bracket": 1}, output_dir / "bad2.3mf")
        except ModelPartNotFoundError as exc:
            message = str(exc)
            assert MODEL_PART in message, message
            assert "[Content_Types].xml" in message, message
        else:
            raise AssertionError("archive without a model part did not raise ModelPartNotFoundError")

        try:
            inject(tmp_dir, {"Bracket": 1}, output_dir / "bad3.3mf")
        except Invalid3MFError as exc:
            assert "not a regular file" in str(exc), str(exc)
        else:
            raise AssertionError("directory input did not raise Invalid3MFError")

        try:
            inject(tmp_dir / "does_not_exist.3mf", {"Bracket": 1}, output_dir / "bad4.3mf")
        except Invalid3MFError as exc:
            assert "not found" in str(exc), str(exc)
        else:
            raise AssertionError("missing input did not raise Invalid3MFError")

    def check_malformed_model() -> None:
        malformed_fixture = tmp_dir / "malformed.3mf"
        _self_test_write_3mf(
            malformed_fixture,
            [
                (RELS_PART, _SELF_TEST_RELS.encode("utf-8"), zipfile.ZIP_DEFLATED),
                (MODEL_PART, b"<model><resources><object id='2'>", zipfile.ZIP_DEFLATED),
            ],
        )
        try:
            inject(malformed_fixture, {"Bracket": 1}, output_dir / "malformed_out.3mf")
        except Invalid3MFError as exc:
            message = str(exc).lower()
            assert "malformed" in message, message
            assert "line" in message, message
        else:
            raise AssertionError("malformed model XML did not raise Invalid3MFError")

    def check_bad_mapping_json() -> None:
        cases: List[Tuple[str, str, Tuple[str, ...]]] = [
            ("zero.json", '{"Bracket": 0}', ("Bracket", "out of range")),
            ("seventeen.json", '{"Bracket": 17}', ("Bracket", "out of range")),
            ("list.json", "[]", ("JSON object",)),
            ("empty.json", "{}", ("contains no entries",)),
            ("bool.json", '{"Bracket": true}', ("Bracket", "integer")),
            ("broken.json", '{"Bracket": ', ("not valid JSON",)),
            ("blank_key.json", '{"": 1}', ("empty or non-string object name",)),
            ("float.json", '{"Bracket": 1.5}', ("Bracket", "integer")),
        ]
        for file_name, file_contents, expected_fragments in cases:
            mapping_file = tmp_dir / file_name
            mapping_file.write_text(file_contents, encoding="utf-8")
            try:
                load_mapping(mapping_file)
            except MappingError as exc:
                message = str(exc)
                for fragment in expected_fragments:
                    assert fragment in message, f"{file_name}: expected {fragment!r} in {message!r}"
            else:
                raise AssertionError(f"{file_name}: load_mapping did not raise MappingError")

    def check_rels_fallback() -> None:
        no_rels_fixture = tmp_dir / "no_rels.3mf"
        _self_test_write_3mf(
            no_rels_fixture,
            [(MODEL_PART, _SELF_TEST_MODEL_XML.encode("utf-8"), zipfile.ZIP_DEFLATED)],
        )
        output = output_dir / "no_rels_out.3mf"
        result = inject(no_rels_fixture, {"Bracket": 4}, output)
        assert dict(result.assignments) == {2: 4, 3: 1, 4: 1, 6: 1}, result.assignments
        with zipfile.ZipFile(no_rels_fixture, "r") as archive:
            objects = extract_objects(archive)
        assert objects[0].source_part == MODEL_PART, objects[0]

    def check_replace_existing() -> None:
        first_output = output_dir / "replace_first.3mf"
        inject(fixture_path, main_mapping, first_output)
        second_output = output_dir / "replace_second.3mf"
        result = inject(first_output, {"Bracket": 4}, second_output)
        assert dict(result.assignments) == {2: 4, 3: 1, 4: 1, 6: 1}, result.assignments
        assert _self_test_zip_names(second_output) == _self_test_zip_names(first_output), (
            "replacing model_settings.config must not change the archive entry list"
        )
        with zipfile.ZipFile(first_output, "r") as previous, zipfile.ZipFile(second_output, "r") as written:
            for name in previous.namelist():
                if name == MODEL_SETTINGS_PART:
                    continue
                assert previous.read(name) == written.read(name), f"entry {name!r} changed during replacement"
            config_entries = [name for name in written.namelist() if name == MODEL_SETTINGS_PART]
            assert len(config_entries) == 1, config_entries
        assert _self_test_config_metadata(second_output)[2]["extruder"] == "4"

    def check_in_place_and_parent_dirs() -> None:
        in_place = tmp_dir / "in_place.3mf"
        in_place.write_bytes(fixture_path.read_bytes())
        result = inject(in_place, main_mapping, in_place)
        assert result.output_path == in_place, result.output_path
        assert dict(result.assignments) == {2: 1, 3: 2, 4: 3, 6: 1}, result.assignments
        assert MODEL_SETTINGS_PART in _self_test_zip_names(in_place)
        with zipfile.ZipFile(fixture_path, "r") as source, zipfile.ZipFile(in_place, "r") as written:
            for name in source.namelist():
                if name.endswith("/"):
                    continue
                assert source.read(name) == written.read(name), f"in-place entry {name!r} was not preserved"
        nested_output = tmp_dir / "new" / "deeper" / "nested.3mf"
        inject(fixture_path, main_mapping, nested_output)
        assert nested_output.is_file(), "output parent directories were not created"

    def check_zip_integrity_and_preservation() -> None:
        output = output_dir / "integrity.3mf"
        inject(fixture_path, main_mapping, output)
        with zipfile.ZipFile(fixture_path, "r") as source, zipfile.ZipFile(output, "r") as written:
            assert written.testzip() is None, "output archive failed testzip()"
            for source_info in source.infolist():
                if source_info.filename == MODEL_SETTINGS_PART:
                    continue
                output_info = written.getinfo(source_info.filename)
                assert output_info.compress_type == source_info.compress_type, source_info.filename
                assert output_info.date_time == source_info.date_time, source_info.filename
                assert output_info.external_attr == source_info.external_attr, source_info.filename
                assert output_info.create_system == source_info.create_system, source_info.filename
                assert output_info.comment == source_info.comment, source_info.filename
            config_members = [name for name in written.namelist() if name == MODEL_SETTINGS_PART]
            assert len(config_members) == 1, config_members
            assert written.getinfo("Metadata/thumbnail.png").compress_type == zipfile.ZIP_STORED

    def check_list_objects() -> None:
        fixture_bytes_before = fixture_path.read_bytes()
        listing = list_objects(fixture_path)
        assert fixture_path.read_bytes() == fixture_bytes_before, "list mode must not modify the input archive"
        for expected_fragment in (
            "id=2",
            "id=3",
            "id=4",
            "id=6",
            'name="Bracket"',
            'name="Clamp Assembly"',
            'name="Clamp Jaw"',
            'name="Unmapped Plate"',
            "component id=5",
            "mapping JSON",
        ):
            assert expected_fragment in listing, f"{expected_fragment!r} missing from listing:\n{listing}"
        assert listing.index('name="Clamp Jaw"') > listing.index('name="Clamp Assembly"'), listing

        script_path = Path(__file__).resolve()
        completed = subprocess.run(
            [sys.executable, str(script_path), "--input", str(fixture_path), "--list-objects"],
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, f"CLI --list-objects failed: {completed.stderr!r}"
        assert 'name="Bracket"' in completed.stdout, completed.stdout
        assert "component id=5" in completed.stdout, completed.stdout

        bad_input = tmp_dir / "list_bad_input.3mf"
        bad_input.write_bytes(b"not a zip")
        failed = subprocess.run(
            [sys.executable, str(script_path), "--input", str(bad_input), "--list-objects"],
            capture_output=True,
            text=True,
        )
        assert failed.returncode == 1, f"CLI --list-objects failure exit code was {failed.returncode}"
        assert "ERROR:" in failed.stderr, failed.stderr

        missing_input = subprocess.run(
            [sys.executable, str(script_path), "--list-objects"],
            capture_output=True,
            text=True,
        )
        assert missing_input.returncode == 2, f"CLI --list-objects without --input exit code was {missing_input.returncode}"

    def check_load_filaments_and_slots() -> None:
        slots = load_filaments(filaments_dict_path)
        assert sorted(slots) == [1, 2, 3], sorted(slots)
        assert slots[1].color_hex == "#161616", slots[1]
        assert slots[1].filament_type == "PLA", slots[1]
        assert slots[1].density_g_cm3 == 1.24, slots[1]
        assert slots[1].filament_id == "GFB98A2CA6", slots[1]
        assert slots[1].name == "Bambu PLA Basic", slots[1]
        assert slots[2].filament_id is None and slots[2].name is None, slots[2]
        assert slots[3].density_g_cm3 == 1.27, slots[3]

        list_filaments_path = tmp_dir / "filaments_list.json"
        _self_test_write_json(
            list_filaments_path,
            [
                {"extruder": 2, "color": "#00ff00", "type": "ABS", "density": 1.05},
                {"extruder": 4, "colour": "#abcdef", "filament_type": "PETG", "density_g_cm3": 1.27},
            ],
        )
        slots = load_filaments(list_filaments_path)
        assert sorted(slots) == [2, 4], sorted(slots)
        assert slots[2].color_hex == "#00FF00", slots[2]
        assert slots[2].filament_type == "ABS", slots[2]
        assert slots[4].color_hex == "#ABCDEF", slots[4]
        assert slots[4].filament_type == "PETG", slots[4]

        direct_slot = FilamentSlot(3, "#aabbcc", "PLA", 1)
        assert direct_slot.color_hex == "#AABBCC", direct_slot
        assert direct_slot.density_g_cm3 == 1.0, direct_slot

    def check_invalid_filament_files() -> None:
        invalid_cases: List[Tuple[str, str, Tuple[str, ...]]] = [
            ("broken_filaments.json", '{"1": ', ("not valid JSON",)),
            ("root_number_filaments.json", "42", ("JSON object", "list")),
            ("empty_filaments.json", "{}", ("no filament entries",)),
            ("zero_key_filaments.json", '{"0": {"color": "#161616", "type": "PLA"}}', ("out of range",)),
            ("high_key_filaments.json", '{"17": {"color": "#161616", "type": "PLA"}}', ("out of range",)),
            (
                "bool_extruder_filaments.json",
                '[{"extruder": true, "color": "#161616", "type": "PLA"}]',
                ("integer",),
            ),
            (
                "duplicate_filaments.json",
                '{"1": {"color": "#161616", "type": "PLA"}, '
                '"01": {"color": "#FFFFFF", "type": "PLA"}}',
                ("duplicate extruder",),
            ),
            ("bad_key_filaments.json", '{"abc": {"color": "#161616", "type": "PLA"}}', ("invalid extruder key",)),
            ("bad_colour_filaments.json", '{"1": {"color": "#12345", "type": "PLA"}}', ("colour",)),
            ("bad_type_filaments.json", '{"1": {"color": "#161616", "type": "  "}}', ("type",)),
            (
                "bad_density_filaments.json",
                '{"1": {"color": "#161616", "type": "PLA", "density": -1}}',
                ("density",),
            ),
            (
                "nan_density_filaments.json",
                '{"1": {"color": "#161616", "type": "PLA", "density": NaN}}',
                ("finite",),
            ),
            (
                "string_density_filaments.json",
                '{"1": {"color": "#161616", "type": "PLA", "density": "1.24"}}',
                ("density",),
            ),
            (
                "bad_filament_id_filaments.json",
                '{"1": {"color": "#161616", "type": "PLA", "filament_id": 5}}',
                ("filament_id",),
            ),
            (
                "blank_name_filaments.json",
                '{"1": {"color": "#161616", "type": "PLA", "name": "   "}}',
                ("name",),
            ),
            ("missing_colour_filaments.json", '{"1": {"type": "PLA"}}', ("color",)),
            ("missing_type_filaments.json", '{"1": {"color": "#161616"}}', ("type",)),
            ("non_object_entry_filaments.json", '{"1": "PLA"}', ("JSON object",)),
            ("missing_extruder_filaments.json", '[{"color": "#161616", "type": "PLA"}]', ("extruder",)),
            (
                "conflicting_extruder_filaments.json",
                '{"1": {"extruder": 2, "color": "#161616", "type": "PLA"}}',
                ("declares extruder",),
            ),
        ]
        for file_name, file_contents, expected_fragments in invalid_cases:
            filament_file = tmp_dir / file_name
            filament_file.write_text(file_contents, encoding="utf-8")
            try:
                load_filaments(filament_file)
            except FilamentSettingsError as exc:
                message = str(exc)
                for fragment in expected_fragments:
                    assert fragment in message, f"{file_name}: expected {fragment!r} in {message!r}"
            else:
                raise AssertionError(f"{file_name}: load_filaments did not raise FilamentSettingsError")

        try:
            load_filaments(tmp_dir / "missing_filaments.json")
        except FilamentSettingsError as exc:
            assert "not found" in str(exc), str(exc)
        else:
            raise AssertionError("missing filaments file did not raise FilamentSettingsError")
        try:
            load_filaments(tmp_dir)
        except FilamentSettingsError as exc:
            assert "not a regular file" in str(exc), str(exc)
        else:
            raise AssertionError("directory filaments path did not raise FilamentSettingsError")

        invalid_slot_factories = (
            lambda: FilamentSlot(0, "#161616", "PLA"),
            lambda: FilamentSlot(17, "#161616", "PLA"),
            lambda: FilamentSlot(True, "#161616", "PLA"),
            lambda: FilamentSlot(1, "red", "PLA"),
            lambda: FilamentSlot(1, "#161616", ""),
            lambda: FilamentSlot(1, "#161616", "PLA", float("inf")),
            lambda: FilamentSlot(1, "#161616", "PLA", 0),
            lambda: FilamentSlot(1, "#161616", "PLA", True),
            lambda: FilamentSlot(1, "#161616", "PLA", None, 5),
            lambda: FilamentSlot(1, "#161616", "PLA", None, None, "  "),
        )
        for factory in invalid_slot_factories:
            try:
                factory()
            except FilamentSettingsError:
                continue
            raise AssertionError(f"invalid FilamentSlot was accepted: {factory}")

    def check_build_project_settings_config() -> None:
        payload = json.loads(build_project_settings_config(sample_filaments))
        assert payload["filament_colour"] == ["#161616", "#FFFFFF", "#FF0000"], payload
        assert payload["filament_type"] == ["PLA", "PLA", "PETG"], payload
        assert payload["filament_density"] == ["1.24", "1.24", "1.27"], payload
        assert payload["filament_diameter"] == ["1.75", "1.75", "1.75"], payload
        assert payload["filament_ids"] == ["GFB98A2CA6", "", ""], payload
        assert payload["filament_settings_id"] == ["Bambu PLA Basic", "", ""], payload

        no_metadata = {
            2: FilamentSlot(2, "#FFFFFF", "PLA", 1.24),
        }
        payload = json.loads(build_project_settings_config(no_metadata))
        assert "filament_ids" not in payload, payload
        assert "filament_settings_id" not in payload, payload
        gap_payload = json.loads(build_project_settings_config({1: sample_filaments[1], 3: sample_filaments[3]}))
        assert gap_payload["filament_colour"] == ["#161616", "#FFFFFF", "#FF0000"], gap_payload
        assert gap_payload["filament_type"] == ["PLA", "PLA", "PETG"], gap_payload
        assert gap_payload["filament_density"] == ["1.24", "1.24", "1.27"], gap_payload
        assert gap_payload["filament_diameter"] == ["1.75", "1.75", "1.75"], gap_payload

        merged = json.loads(
            build_project_settings_config(
                sample_filaments,
                existing={
                    "printer_settings_id": "P1S 0.4 nozzle",
                    "filament_colour": ["#000000"],
                    "custom_key": {"nested": [1, 2, 3]},
                },
            )
        )
        assert merged["printer_settings_id"] == "P1S 0.4 nozzle", merged
        assert merged["custom_key"] == {"nested": [1, 2, 3]}, merged
        assert merged["filament_colour"] == ["#161616", "#FFFFFF", "#FF0000"], merged
        assert merged["filament_diameter"] == ["1.75", "1.75", "1.75"], merged

        try:
            build_project_settings_config({})
        except FilamentSettingsError as exc:
            assert "no entries" in str(exc), str(exc)
        else:
            raise AssertionError("empty filaments did not raise FilamentSettingsError")
        try:
            build_project_settings_config({1: FilamentSlot(2, "#161616", "PLA")})
        except FilamentSettingsError as exc:
            assert "does not match" in str(exc), str(exc)
        else:
            raise AssertionError("mismatched filament key did not raise FilamentSettingsError")

    def check_inject_with_filaments() -> None:
        filaments = load_filaments(filaments_dict_path)
        output = output_dir / "with_filaments.3mf"
        result = inject(fixture_path, main_mapping, output, filaments=filaments)
        assert result.filament_count == 3, result.filament_count
        assert result.project_settings_xml is not None, "project_settings_xml was not populated"
        expected_names = _self_test_zip_names(fixture_path) + [
            MODEL_SETTINGS_PART,
            PROJECT_SETTINGS_PART,
        ]
        assert _self_test_zip_names(output) == expected_names, _self_test_zip_names(output)
        with zipfile.ZipFile(fixture_path, "r") as source, zipfile.ZipFile(output, "r") as written:
            assert written.testzip() is None, "output archive failed testzip()"
            for name in source.namelist():
                if name.endswith("/"):
                    continue
                assert source.read(name) == written.read(name), f"entry {name!r} was not preserved"
            assert written.namelist().count(PROJECT_SETTINGS_PART) == 1
        config = _self_test_config_metadata(output)
        assert {object_id: entry["extruder"] for object_id, entry in config.items()} == {
            2: "1",
            3: "2",
            4: "3",
            6: "1",
        }, config
        payload = _self_test_json_member(output, PROJECT_SETTINGS_PART)
        assert payload["filament_colour"] == ["#161616", "#FFFFFF", "#FF0000"], payload
        assert payload["filament_type"] == ["PLA", "PLA", "PETG"], payload
        assert payload["filament_density"] == ["1.24", "1.24", "1.27"], payload
        assert payload["filament_diameter"] == ["1.75", "1.75", "1.75"], payload
        assert payload["filament_ids"] == ["GFB98A2CA6", "", ""], payload
        assert payload["filament_settings_id"] == ["Bambu PLA Basic", "", ""], payload

        second_output = output_dir / "with_filaments_second.3mf"
        second_result = inject(output, main_mapping, second_output, filaments=filaments)
        assert _self_test_zip_names(second_output) == _self_test_zip_names(output), (
            "replacing both managed entries must not change the archive entry list"
        )
        assert second_result.filament_count == 3
        assert _self_test_zip_names(second_output).count(MODEL_SETTINGS_PART) == 1
        assert _self_test_zip_names(second_output).count(PROJECT_SETTINGS_PART) == 1

        third_output = output_dir / "with_filaments_stripped.3mf"
        third_result = inject(output, main_mapping, third_output)
        assert third_result.filament_count == 0, third_result.filament_count
        assert third_result.project_settings_xml is None, third_result.project_settings_xml
        with zipfile.ZipFile(output, "r") as source, zipfile.ZipFile(third_output, "r") as written:
            assert source.read(PROJECT_SETTINGS_PART) == written.read(PROJECT_SETTINGS_PART), (
                "an existing project_settings.config must be preserved when no filaments are injected"
            )

    def check_existing_project_settings_merge() -> None:
        merge_fixture = tmp_dir / "merge_input.3mf"
        existing_project_settings = {
            "printer_settings_id": "Custom Printer",
            "filament_colour": ["#000000"],
            "filament_type": ["ABS"],
            "filament_density": ["1.00"],
            "filament_diameter": ["2.85"],
            "filament_ids": ["OLDFILAMENTID"],
            "custom_key": {"nested": [1, 2, 3]},
        }
        merge_entries = _self_test_fixture_entries()
        merge_entries.insert(
            3,
            (
                PROJECT_SETTINGS_PART,
                json.dumps(existing_project_settings).encode("utf-8"),
                zipfile.ZIP_DEFLATED,
            ),
        )
        _self_test_write_3mf(merge_fixture, merge_entries)
        output = output_dir / "merge_out.3mf"
        result = inject(merge_fixture, main_mapping, output, filaments=sample_filaments)
        assert result.filament_count == 3, result.filament_count
        input_names = _self_test_zip_names(merge_fixture)
        output_names = _self_test_zip_names(output)
        assert output_names == input_names + [MODEL_SETTINGS_PART], output_names
        assert output_names.index(PROJECT_SETTINGS_PART) == input_names.index(PROJECT_SETTINGS_PART), (
            "the replaced project_settings.config must keep its archive position"
        )
        with zipfile.ZipFile(merge_fixture, "r") as source, zipfile.ZipFile(output, "r") as written:
            for name in input_names:
                if name in (PROJECT_SETTINGS_PART,) or name.endswith("/"):
                    continue
                assert source.read(name) == written.read(name), f"entry {name!r} was not preserved"
        payload = _self_test_json_member(output, PROJECT_SETTINGS_PART)
        assert payload["printer_settings_id"] == "Custom Printer", payload
        assert payload["custom_key"] == {"nested": [1, 2, 3]}, payload
        assert payload["filament_colour"] == ["#161616", "#FFFFFF", "#FF0000"], payload
        assert payload["filament_type"] == ["PLA", "PLA", "PETG"], payload
        assert payload["filament_density"] == ["1.24", "1.24", "1.27"], payload
        assert payload["filament_diameter"] == ["1.75", "1.75", "1.75"], payload
        assert payload["filament_ids"] == ["GFB98A2CA6", "", ""], payload

        no_metadata_output = output_dir / "merge_no_metadata.3mf"
        inject(
            merge_fixture,
            main_mapping,
            no_metadata_output,
            filaments={1: FilamentSlot(1, "#112233", "ABS", 1.05)},
        )
        payload = _self_test_json_member(no_metadata_output, PROJECT_SETTINGS_PART)
        assert payload["printer_settings_id"] == "Custom Printer", payload
        assert payload["custom_key"] == {"nested": [1, 2, 3]}, payload
        assert payload["filament_ids"] == [""], payload
        assert payload["filament_settings_id"] == [""], payload

    def check_cli_filaments_end_to_end() -> None:
        script_path = Path(__file__).resolve()
        output = output_dir / "cli_filaments.3mf"
        completed = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--input",
                str(fixture_path),
                "--mapping",
                str(mapping_path),
                "--filaments",
                str(filaments_dict_path),
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, f"CLI --filaments failed: {completed.stderr!r}"
        assert output.is_file(), "CLI --filaments did not create the output file"
        members = _self_test_zip_names(output)
        assert MODEL_SETTINGS_PART in members and PROJECT_SETTINGS_PART in members, members
        payload = _self_test_json_member(output, PROJECT_SETTINGS_PART)
        assert payload["filament_colour"] == ["#161616", "#FFFFFF", "#FF0000"], payload

        help_output = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            capture_output=True,
            text=True,
        )
        assert "--filaments" in help_output.stdout, help_output.stdout

        rejected = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--input",
                str(fixture_path),
                "--list-objects",
                "--filaments",
                str(filaments_dict_path),
            ],
            capture_output=True,
            text=True,
        )
        assert rejected.returncode == 2, rejected.returncode
        assert "--filaments" in rejected.stderr, rejected.stderr

        bad_filaments = tmp_dir / "cli_bad_filaments.json"
        bad_filaments.write_text('{"1": {"color": "red", "type": "PLA"}}', encoding="utf-8")
        failed = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--input",
                str(fixture_path),
                "--mapping",
                str(mapping_path),
                "--filaments",
                str(bad_filaments),
                "--output",
                str(output_dir / "cli_bad_filaments.3mf"),
            ],
            capture_output=True,
            text=True,
        )
        assert failed.returncode == 1, f"CLI bad filaments exit code was {failed.returncode}"
        assert "ERROR:" in failed.stderr, failed.stderr
        assert not (output_dir / "cli_bad_filaments.3mf").exists(), "failed CLI run must not create output"

    def check_cli_end_to_end() -> None:
        script_path = Path(__file__).resolve()
        output = output_dir / "cli.3mf"
        completed = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--input",
                str(fixture_path),
                "--mapping",
                str(mapping_path),
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, f"CLI failed: stdout={completed.stdout!r} stderr={completed.stderr!r}"
        assert output.is_file(), "CLI did not create the output file"
        config = _self_test_config_metadata(output)
        assert {object_id: entry["extruder"] for object_id, entry in config.items()} == {
            2: "1",
            3: "2",
            4: "3",
            6: "1",
        }, config

        only_mapped_output = output_dir / "cli_only_mapped.3mf"
        completed = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--input",
                str(fixture_path),
                "--mapping",
                str(mapping_path),
                "--output",
                str(only_mapped_output),
                "--only-mapped",
            ],
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, f"CLI --only-mapped failed: {completed.stderr!r}"
        assert set(_self_test_config_metadata(only_mapped_output)) == {2, 3, 4}

        bad_input = tmp_dir / "cli_bad_input.3mf"
        bad_input.write_bytes(b"not a zip")
        failed = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--input",
                str(bad_input),
                "--mapping",
                str(mapping_path),
                "--output",
                str(output_dir / "cli_failed.3mf"),
            ],
            capture_output=True,
            text=True,
        )
        assert failed.returncode == 1, f"CLI failure exit code was {failed.returncode}"
        assert "ERROR:" in failed.stderr, failed.stderr
        assert not (output_dir / "cli_failed.3mf").exists(), "failed CLI run must not create output"

        no_arguments = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
        )
        assert no_arguments.returncode == 2, f"no-argument exit code was {no_arguments.returncode}"

    return [
        ("injects mapped, nested and defaulted object ids while preserving entries", check_basic_injection),
        ("extract_objects reports build items, names and ancestor ids", check_extract_objects),
        ("--only-mapped excludes unmapped build items", check_only_mapped),
        ("default extruder applies to unmapped build items", check_default_extruder),
        ("repeated names (attr and metadata fallback) map all instances", check_repeated_names_and_metadata_fallback),
        ("conflicting nested assignments raise ConflictingAssignmentError", check_conflicting_assignments),
        ("unknown mapping name raises UnknownComponentError with available names", check_unknown_name),
        ("invalid 3MF inputs raise Invalid3MFError / ModelPartNotFoundError", check_invalid_inputs),
        ("malformed model XML raises Invalid3MFError with parse position", check_malformed_model),
        ("bad mapping JSON raises MappingError naming the offender", check_bad_mapping_json),
        ("_rels/.rels resolution falls back to 3D/3dmodel.model", check_rels_fallback),
        ("existing model_settings.config is replaced, entries preserved", check_replace_existing),
        ("in-place injection and output parent creation are safe", check_in_place_and_parent_dirs),
        ("ZIP integrity and entry metadata are preserved", check_zip_integrity_and_preservation),
        ("--list-objects reports build-item and nested component names", check_list_objects),
        ("load_filaments accepts object-keyed and list JSON forms", check_load_filaments_and_slots),
        ("invalid filament files and slots raise FilamentSettingsError", check_invalid_filament_files),
        ("build_project_settings_config fills gaps and merges existing settings", check_build_project_settings_config),
        ("inject with filaments writes both managed entries and preserves the rest", check_inject_with_filaments),
        ("print settings of a body reach its object", check_object_settings),
        ("existing project_settings.config is merged and replaced in place", check_existing_project_settings_merge),
        ("CLI --filaments end-to-end and --list-objects rejection", check_cli_filaments_end_to_end),
        ("CLI end-to-end succeeds and failure/no-arg exits are correct", check_cli_end_to_end),
    ]


def _run_self_test(verbose: bool = False) -> int:
    """Run the built-in self-test; return 0 on success and 1 on any failure."""
    previous_level = LOGGER.level
    if not verbose:
        LOGGER.setLevel(logging.WARNING)
    passed = 0
    failed = 0
    try:
        with tempfile.TemporaryDirectory(prefix="threemf_injector_selftest_") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            try:
                cases = _self_test_cases(temp_dir)
            except Exception as exc:  # pragma: no cover - fixture setup failure
                print(f"FAIL self-test fixture setup: {exc}")
                traceback.print_exc()
                return 1
            for description, check in cases:
                try:
                    check()
                except Exception as exc:
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
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
