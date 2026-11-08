"""Filament catalog loading and validation for the Filamatch add-in.

This module is deliberately dependency-free (Python standard library only) so it
can be imported both inside Fusion 360 and from a plain Python interpreter.  It
loads the bundled ``catalog/filaments.json``, validates every entry and exposes
the result as :class:`FilamentEntry` dataclass instances for the material
importer.

The catalog file may use either of these JSON root shapes::

    [ {...entry...}, {...entry...} ]

    {"version": 1, "filaments": [ {...entry...}, {...entry...} ]}

Both are equivalent; unknown keys on the root object or on individual entries
are ignored so that newer catalogs remain loadable by older add-in builds.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
import traceback
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Dict, List, Optional, Pattern, Sequence, Tuple

from .. import config

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: Absolute path of the filament catalog bundled with the add-in.
DEFAULT_CATALOG_PATH: Path = config.CATALOG_FILE

#: Fields that must be present and valid on every catalog entry.
REQUIRED_FIELDS: Tuple[str, ...] = (
    "id",
    "manufacturer",
    "product_line",
    "color_name",
    "color_hex",
    "slicer_filament_type",
    "density_g_cm3",
    "template_material",
)

#: Optional fields; missing or ``null`` values are normalised to ``None``.
OPTIONAL_FIELDS: Tuple[str, ...] = ("category", "adsklib_path", "orca_filament_profile", "slicer_data")

#: Entry ids double as Fusion material names, so keep them identifier-safe.
ENTRY_ID_PATTERN: Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9_]*$")

#: Colours are stored as six digit RGB hex strings.
COLOR_HEX_PATTERN: Pattern[str] = re.compile(r"^#[0-9A-Fa-f]{6}$")

#: Reference ids the bundled catalog must contain (it may hold more).
_BUNDLED_IDS: Tuple[str, ...] = (
    "bambu_pla_basic_black",
    "bambu_petg_hf_white",
    "prusament_pla_orange",
    "polymaker_polylite_abs_black",
    "generic_abs_gf30",
)


class CatalogError(Exception):
    """Base class for all filament-catalog errors."""


class CatalogValidationError(CatalogError):
    """Raised when catalog data does not satisfy the filament entry schema.

    The message always names the source, the entry index and the offending
    field so users can fix ``catalog/filaments.json`` directly.
    """


@dataclass(frozen=True)
class FilamentEntry:
    """A single validated filament definition from the catalog."""

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
    #: Values a slicer preset can be built from (Open Filament Database
    #: temperatures, fans, flow and slicer preset names); see filament_presets.
    slicer_data: Optional[Dict[str, Any]] = dataclass_field(default=None, compare=False, hash=False)

    @property
    def display_name(self) -> str:
        """Return the standard label ``Brand - Material - Color``.

        Example: ``"Bambu Lab - PLA Basic - Black"``. Keeping one canonical
        format makes Favorites, the custom library and slicer project filament
        names consistent and free of catalog-id underscores.
        """
        return "{} - {} - {}".format(
            self.manufacturer, self.product_line, self.color_name
        )


def _entry_error(source: str, index: int, field: str, message: str) -> CatalogValidationError:
    """Create a validation error naming the source, entry index and field."""
    return CatalogValidationError(
        "Catalog validation failed: source={!r}, entry index={}, field={!r}: {}".format(
            source, index, field, message
        )
    )


def _catalog_error(source: str, field: str, message: str) -> CatalogValidationError:
    """Create a validation error for a problem not tied to a single entry."""
    return CatalogValidationError(
        "Catalog validation failed: source={!r}, field={!r}: {}".format(source, field, message)
    )


def _required_text(raw: Dict[str, Any], field: str, index: int, source: str) -> str:
    """Return a stripped non-empty string field, or raise CatalogValidationError."""
    if field not in raw or raw[field] is None:
        raise _entry_error(source, index, field, "required field is missing or null")
    value = raw[field]
    if not isinstance(value, str):
        raise _entry_error(
            source, index, field, "expected a string, got {}".format(type(value).__name__)
        )
    value = value.strip()
    if not value:
        raise _entry_error(source, index, field, "must not be empty")
    return value


def _optional_text(raw: Dict[str, Any], field: str, index: int, source: str) -> Optional[str]:
    """Return an optional stripped string field as ``str`` or ``None``."""
    if field not in raw or raw[field] is None:
        return None
    value = raw[field]
    if not isinstance(value, str):
        raise _entry_error(
            source,
            index,
            field,
            "expected a string or null, got {}".format(type(value).__name__),
        )
    value = value.strip()
    if not value:
        raise _entry_error(source, index, field, "must be a non-empty string or null")
    return value


def _optional_object(raw: Dict[str, Any], field: str, index: int, source: str) -> Optional[Dict[str, Any]]:
    """Return an optional JSON object field as ``dict`` or ``None``."""
    if field not in raw or raw[field] is None:
        return None
    value = raw[field]
    if not isinstance(value, dict):
        raise _entry_error(
            source, index, field, "expected a JSON object or null, got {}".format(type(value).__name__)
        )
    return dict(value)


def _validated_density(raw: Dict[str, Any], index: int, source: str) -> float:
    """Validate and return ``density_g_cm3`` as a finite positive float."""
    field = "density_g_cm3"
    if field not in raw or raw[field] is None:
        raise _entry_error(source, index, field, "required field is missing or null")
    value = raw[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _entry_error(
            source,
            index,
            field,
            "expected a number (booleans are not allowed), got {}".format(type(value).__name__),
        )
    density = float(value)
    if not math.isfinite(density):
        raise _entry_error(
            source, index, field, "must be a finite number, got {!r}".format(value)
        )
    if density <= 0.0:
        raise _entry_error(
            source, index, field, "must be greater than zero, got {!r}".format(value)
        )
    return density


def validate_entry(raw: Any, index: int, source: str) -> FilamentEntry:
    """Validate one raw catalog entry and convert it to a FilamentEntry.

    Args:
        raw: Raw JSON value for the entry; must be a mapping.
        index: Zero-based position of the entry in the catalog (used in errors).
        source: Human readable description of where the data came from.

    Returns:
        A validated, frozen :class:`FilamentEntry`.

    Raises:
        CatalogValidationError: If a required field is missing or malformed.
    """
    if not isinstance(raw, dict):
        raise _entry_error(
            source, index, "<entry>", "expected a JSON object, got {}".format(type(raw).__name__)
        )

    entry_id = _required_text(raw, "id", index, source)
    if not ENTRY_ID_PATTERN.match(entry_id):
        raise _entry_error(
            source,
            index,
            "id",
            "must match {} (lowercase letters, digits and underscores, starting with a letter "
            "or digit), got {!r}".format(ENTRY_ID_PATTERN.pattern, raw["id"]),
        )

    color_hex = _required_text(raw, "color_hex", index, source)
    if not COLOR_HEX_PATTERN.match(color_hex):
        raise _entry_error(
            source, index, "color_hex", "must match #RRGGBB, got {!r}".format(raw["color_hex"])
        )

    slicer_filament_type = _required_text(raw, "slicer_filament_type", index, source)
    category = _optional_text(raw, "category", index, source) or slicer_filament_type

    entry = FilamentEntry(
        id=entry_id,
        manufacturer=_required_text(raw, "manufacturer", index, source),
        product_line=_required_text(raw, "product_line", index, source),
        color_name=_required_text(raw, "color_name", index, source),
        color_hex=color_hex.upper(),
        slicer_filament_type=slicer_filament_type,
        density_g_cm3=_validated_density(raw, index, source),
        template_material=_required_text(raw, "template_material", index, source),
        category=category,
        adsklib_path=_optional_text(raw, "adsklib_path", index, source),
        orca_filament_profile=_optional_text(raw, "orca_filament_profile", index, source),
        slicer_data=_optional_object(raw, "slicer_data", index, source),
    )

    unknown_fields = set(raw) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS)
    if unknown_fields:
        logger.debug(
            "Ignoring unknown catalog fields %s on entry %r.", sorted(unknown_fields), entry.id
        )
    return entry


def parse_catalog(
    data: Any, source: str = "<memory>", strict: bool = True
) -> List[FilamentEntry]:
    """Validate in-memory catalog data and return the parsed entries.

    ``data`` is the deserialised JSON of a catalog file: either a bare list of
    entries or an object whose ``"filaments"`` key holds that list.  Unknown
    root keys are ignored.

    ``display_name`` keys Favorites, so it must be unique like ``id``.  In
    strict mode (the default, used for the bundled catalog) the first bad
    entry, repeated id or repeated display name raises.  In lenient mode
    (``strict=False``, meant for user and downloaded catalogs) such entries
    are logged and skipped so one bad entry cannot hide the rest.

    Args:
        data: Deserialised JSON value (list or mapping).
        source: Label used in error messages (defaults to ``"<memory>"``).
        strict: Raise on the first bad entry instead of skipping it.

    Returns:
        The validated entries in catalog order.

    Raises:
        CatalogValidationError: If the root shape is unsupported or the catalog
            is empty; in strict mode also if an entry is malformed or an id or
            display name repeats.
    """
    if isinstance(data, dict):
        if "filaments" not in data:
            raise _catalog_error(
                source,
                "filaments",
                "object root must contain a 'filaments' array (or use a bare JSON array instead)",
            )
        raw_entries = data["filaments"]
    elif isinstance(data, list):
        raw_entries = data
    else:
        raise _catalog_error(
            source,
            "<root>",
            "expected a JSON array or an object with a 'filaments' array, got {}".format(
                type(data).__name__
            ),
        )

    if not isinstance(raw_entries, list):
        raise _catalog_error(
            source,
            "filaments",
            "expected an array of filament entries, got {}".format(type(raw_entries).__name__),
        )
    if not raw_entries:
        raise _catalog_error(source, "filaments", "catalog contains no filament entries")

    entries: List[FilamentEntry] = []
    seen_ids: Dict[str, int] = {}
    seen_names: Dict[str, int] = {}
    skipped = 0
    for index, raw_entry in enumerate(raw_entries):
        try:
            entry = validate_entry(raw_entry, index, source)
            if entry.id in seen_ids:
                raise _entry_error(
                    source,
                    index,
                    "id",
                    "duplicate id {!r} (first defined at entry index {})".format(
                        entry.id, seen_ids[entry.id]
                    ),
                )
            name_key = entry.display_name.casefold()
            if name_key in seen_names:
                raise _entry_error(
                    source,
                    index,
                    "display_name",
                    "duplicate display name {!r} (first defined at entry index {})".format(
                        entry.display_name, seen_names[name_key]
                    ),
                )
        except CatalogValidationError as exc:
            if strict:
                raise
            skipped += 1
            logger.info("Skipping catalog entry: %s", exc)
            continue
        seen_ids[entry.id] = index
        seen_names[name_key] = index
        entries.append(entry)

    if skipped:
        logger.info("Skipped %d invalid catalog entries from %s.", skipped, source)
    logger.info("Parsed %d filament entries from %s.", len(entries), source)
    return entries


def merge_catalogs(
    base: Sequence[FilamentEntry], extra: Sequence[FilamentEntry], source: str = "<extra>"
) -> List[FilamentEntry]:
    """Return ``base`` plus the ``extra`` entries whose id and display name are new.

    Extra entries that repeat an id or display name already present are logged
    and dropped, so merged catalogs keep both keys unique.
    """
    merged = list(base)
    ids = {entry.id for entry in merged}
    names = {entry.display_name.casefold() for entry in merged}
    for entry in extra:
        name_key = entry.display_name.casefold()
        if entry.id in ids or name_key in names:
            logger.info(
                "Skipping %s entry %r (%s): its id or display name is already in the catalog.",
                source,
                entry.id,
                entry.display_name,
            )
            continue
        ids.add(entry.id)
        names.add(name_key)
        merged.append(entry)
    return merged


def load_catalog(path: Optional[Path] = None) -> List[FilamentEntry]:
    """Load and validate the filament catalog from disk.

    Args:
        path: Catalog file to load.  Defaults to the catalog bundled with the
            add-in (``catalog/filaments.json`` next to this module).

    Returns:
        The validated entries in catalog order.

    Raises:
        CatalogError: If the file cannot be read or does not contain valid JSON.
        CatalogValidationError: If the file content fails catalog validation.
    """
    catalog_path = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    logger.debug("Loading filament catalog from %s", catalog_path)
    try:
        text = catalog_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(
            "Unable to read filament catalog file {!r}: {}".format(str(catalog_path), exc)
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CatalogError(
            "Filament catalog file {!r} is not valid JSON: {} (line {}, column {})".format(
                str(catalog_path), exc.msg, exc.lineno, exc.colno
            )
        ) from exc
    return parse_catalog(data, source=str(catalog_path))


def find_by_id(entries: Sequence[FilamentEntry], entry_id: str) -> FilamentEntry:
    """Return the catalog entry whose ``id`` equals ``entry_id``.

    Args:
        entries: Catalog entries to search.
        entry_id: Catalog id to look up.

    Returns:
        The matching entry.

    Raises:
        CatalogError: If no entry has the requested id.
    """
    if not isinstance(entry_id, str) or not entry_id.strip():
        raise CatalogError("entry_id must be a non-empty string, got {!r}".format(entry_id))
    wanted = entry_id.strip()
    for entry in entries:
        if entry.id == wanted:
            return entry
    known_ids = [entry.id for entry in entries]
    preview = ", ".join(known_ids[:20])
    if len(known_ids) > 20:
        preview += ", ..."
    raise CatalogError(
        "No filament entry with id {!r} in catalog ({} entries). Known ids: {}".format(
            wanted, len(known_ids), preview if preview else "<none>"
        )
    )


def _expect(condition: bool, message: str) -> None:
    """Raise AssertionError with ``message`` when ``condition`` is false."""
    if not condition:
        raise AssertionError(message)


def _selftest() -> int:
    """Exercise loading and validation against the bundled catalog and bad data.

    Returns:
        Process exit code: 0 when every check passed, 1 otherwise.
    """
    try:
        entries = load_catalog()
        _expect(len(entries) >= 1, "bundled catalog must not be empty")
        print("PASS: loaded {} entries from {}".format(len(entries), DEFAULT_CATALOG_PATH))

        loaded_ids = [entry.id for entry in entries]
        missing = [entry_id for entry_id in _BUNDLED_IDS if entry_id not in loaded_ids]
        _expect(not missing, "bundled catalog is missing ids {}".format(missing))
        print("PASS: bundled catalog contains the reference entries")

        for entry in entries:
            _expect(
                isinstance(entry.density_g_cm3, float) and entry.density_g_cm3 > 0.0,
                "{!r} density must be a positive float".format(entry.id),
            )
            _expect(
                ENTRY_ID_PATTERN.match(entry.id) is not None,
                "{!r} id must match the catalog id pattern".format(entry.id),
            )
            _expect(
                COLOR_HEX_PATTERN.match(entry.color_hex) is not None
                and entry.color_hex == entry.color_hex.upper(),
                "{!r} color_hex must be normalised uppercase #RRGGBB".format(entry.id),
            )
            _expect(
                entry.adsklib_path is None,
                "{!r} bundled adsklib_path must be null".format(entry.id),
            )
            _expect(
                entry.orca_filament_profile is None
                or isinstance(entry.orca_filament_profile, str),
                "{!r} orca_filament_profile must be a string or null".format(entry.id),
            )
        print("PASS: all entries pass schema checks (id, colour, density, optional fields)")

        bambu = find_by_id(entries, "bambu_pla_basic_black")
        _expect(
            bambu.display_name == "Bambu Lab - PLA Basic - Black",
            "display_name must be 'Bambu Lab - PLA Basic - Black', got {!r}".format(
                bambu.display_name
            ),
        )
        _expect(
            find_by_id(entries, "generic_abs_gf30").orca_filament_profile is None,
            "generic_abs_gf30 must not reference an Orca profile",
        )
        print("PASS: find_by_id resolves entries and display_name formats correctly")

        valid_entry: Dict[str, Any] = {
            "id": "selftest_filament",
            "manufacturer": "Test Manufacturer",
            "product_line": "Test Line",
            "color_name": "Black",
            "color_hex": "#aabbcc",
            "slicer_filament_type": "PLA",
            "density_g_cm3": 1.24,
            "template_material": "PLA Plastic",
            "adsklib_path": None,
            "orca_filament_profile": "Test Filament @Test Printer.json",
        }
        parsed = parse_catalog([dict(valid_entry)], source="<selftest>")
        _expect(len(parsed) == 1, "bare-array catalog root must parse to one entry")
        _expect(
            parsed[0].color_hex == "#AABBCC",
            "color_hex must be normalised to uppercase, got {!r}".format(parsed[0].color_hex),
        )
        _expect(
            parsed[0].category == "PLA",
            "category must default to slicer_filament_type, got {!r}".format(parsed[0].category),
        )
        categorised = parse_catalog(
            [dict(valid_entry, category="Fiber-Reinforced")], source="<selftest>"
        )
        _expect(
            categorised[0].category == "Fiber-Reinforced",
            "explicit category must be preserved, got {!r}".format(categorised[0].category),
        )
        print("PASS: bare-array JSON root parses, colour normalised and category defaults/explicit")

        bad_cases = {
            "missing required field": {
                key: value for key, value in valid_entry.items() if key != "manufacturer"
            },
            "malformed id": dict(valid_entry, id="Bad-ID"),
            "malformed colour": dict(valid_entry, color_hex="#GGGGGG"),
            "boolean density": dict(valid_entry, density_g_cm3=True),
            "zero density": dict(valid_entry, density_g_cm3=0),
        }
        for label, bad_entry in bad_cases.items():
            try:
                validate_entry(bad_entry, 0, "<selftest>")
            except CatalogValidationError as exc:
                _expect(
                    "<selftest>" in str(exc) and "field=" in str(exc),
                    "{} error must name its source and field: {}".format(label, exc),
                )
            else:
                raise AssertionError("{} must raise CatalogValidationError".format(label))
        print(
            "PASS: {} malformed entries raise CatalogValidationError with source/field "
            "context".format(len(bad_cases))
        )

        try:
            parse_catalog([dict(valid_entry), dict(valid_entry)], source="<selftest>")
        except CatalogValidationError as exc:
            _expect(
                "duplicate" in str(exc),
                "duplicate-id error must mention 'duplicate': {}".format(exc),
            )
        else:
            raise AssertionError("duplicate ids must raise CatalogValidationError")
        print("PASS: duplicate ids raise CatalogValidationError")

        same_name = dict(valid_entry, id="selftest_filament_two")
        try:
            parse_catalog([dict(valid_entry), same_name], source="<selftest>")
        except CatalogValidationError as exc:
            _expect("display name" in str(exc), "duplicate-name error must say so: {}".format(exc))
        else:
            raise AssertionError("strict mode must reject repeated display names")
        lenient = parse_catalog(
            [
                dict(valid_entry),
                dict(valid_entry, id="Bad-ID"),
                dict(valid_entry),
                same_name,
                dict(valid_entry, id="selftest_other", color_name="White"),
            ],
            source="<selftest>",
            strict=False,
        )
        _expect(
            [entry.id for entry in lenient] == ["selftest_filament", "selftest_other"],
            "lenient mode must skip bad, repeated-id and repeated-name entries, got {}".format(
                [entry.id for entry in lenient]
            ),
        )
        extra = parse_catalog(
            [same_name, dict(valid_entry, id="selftest_new", color_name="Red")],
            source="<selftest>",
        )
        merged = merge_catalogs(lenient[:1], extra)
        _expect(
            [entry.id for entry in merged] == ["selftest_filament", "selftest_new"],
            "merge_catalogs must drop repeated display names, got {}".format(
                [entry.id for entry in merged]
            ),
        )
        print("PASS: display names are unique (strict rejects, lenient and merge skip)")

        try:
            find_by_id(entries, "does_not_exist")
        except CatalogError:
            pass
        else:
            raise AssertionError("find_by_id must raise CatalogError for unknown ids")
        print("PASS: find_by_id raises CatalogError for unknown ids")

        missing_path = Path("/definitely/not/a/real/filamatch/catalog.json")
        try:
            load_catalog(missing_path)
        except CatalogError:
            pass
        else:
            raise AssertionError("load_catalog must raise CatalogError for an unreadable path")
        print("PASS: unreadable catalog path raises CatalogError")

        print("PASS: catalog self-test completed")
        return 0
    except Exception:
        traceback.print_exc()
        print("FAIL: catalog self-test aborted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(_selftest())
