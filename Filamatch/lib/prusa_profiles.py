"""PrusaSlicer presets, projects and print estimates.

PrusaSlicer keeps a vendor's presets in one INI bundle (``PrusaResearch.ini``
in the app's ``profiles`` folder): ``[printer:...]``, ``[print:...]`` and
``[filament:...]`` sections that inherit from one another (``inherits = A;
*B*``) and say which printers they fit with a small expression
(``compatible_printers_condition = printer_model=="MK4" and
nozzle_diameter[0]==0.4``).  This module reads a bundle, works out which
prints and filaments fit a printer (:func:`compatible`), picks one filament
preset per design material, prepares a project with PrusaSlicer's command
line in a settings folder of its own (the user's PrusaSlicer settings are
never touched) and slices a copy for the print estimate.

Files and the PrusaSlicer command line only; no Fusion API, no network.
``python3 -m Filamatch.lib.prusa_profiles`` runs the self-test.
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
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import body_settings

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: Section kinds of a PrusaSlicer bundle that Filamatch uses.
KINDS: Tuple[str, ...] = ("printer", "print", "filament")


class PrusaProfilesError(RuntimeError):
    """A PrusaSlicer bundle, preset or command-line run could not be used."""


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------


class Bundle:
    """One vendor's INI bundle: raw sections by kind and name, flattened on demand."""

    def __init__(self, vendor: str, sections: Mapping[str, Mapping[str, Dict[str, str]]],
                 path: Optional[Path] = None, name: str = "") -> None:
        self.vendor = vendor
        #: The vendor's display name (``[vendor] name``), else its id.
        self.name = name or vendor
        self.sections = {kind: dict(sections.get(kind, {})) for kind in KINDS}
        self.path = path
        #: Bundles whose presets this one's may inherit from (the user's own
        #: presets inherit PrusaSlicer's); the first ones also offer their
        #: prints and filaments (:meth:`all_names`).
        self.fallbacks: List["Bundle"] = []
        self.offered_fallbacks = 0
        self._flat: Dict[Tuple[str, str], Dict[str, str]] = {}

    def names(self, kind: str) -> List[str]:
        """Presets a user can pick (abstract ``*name*`` presets left out), in bundle order."""
        return [name for name in self.sections.get(kind, {}) if not name.startswith("*")]

    def all_names(self, kind: str) -> List[str]:
        """This bundle's presets, then those its first fallbacks offer."""
        names = list(self.names(kind))
        for bundle in self.fallbacks[: self.offered_fallbacks]:
            names += [name for name in bundle.names(kind) if name not in self.sections.get(kind, {})]
        return list(dict.fromkeys(names))

    def has(self, kind: str, name: str) -> bool:
        """True when this bundle or a fallback has the preset."""
        return name in self.sections.get(kind, {}) or any(bundle.has(kind, name) for bundle in self.fallbacks)

    def flat(self, kind: str, name: str) -> Dict[str, str]:
        """A preset with everything it inherits: parents left to right, then its own keys."""
        key = (kind, name)
        if key not in self._flat:
            self._flat[key] = self._flatten(kind, name, ())
        return self._flat[key]

    def _flatten(self, kind: str, name: str, chain: Tuple[str, ...]) -> Dict[str, str]:
        raw = self.sections.get(kind, {}).get(name)
        if raw is None:
            for bundle in self.fallbacks:
                if bundle.has(kind, name):
                    return bundle.flat(kind, name)
            raise PrusaProfilesError("{} preset {!r} is not in the {} bundle".format(kind, name, self.vendor))
        if name in chain:
            raise PrusaProfilesError("Circular inheritance: {}".format(" -> ".join(chain + (name,))))
        merged: Dict[str, str] = {}
        for parent in [item.strip() for item in raw.get("inherits", "").split(";") if item.strip()]:
            merged.update(self._flatten(kind, parent, chain + (name,)))
        merged.update({key: value for key, value in raw.items() if key != "inherits"})
        return merged


def parse_bundle(text: str, vendor: str = "", path: Optional[Path] = None) -> Bundle:
    """Read a PrusaSlicer INI bundle (``[kind:name]`` sections of ``key = value`` lines)."""
    sections: Dict[str, Dict[str, Dict[str, str]]] = {kind: {} for kind in KINDS}
    header: Dict[str, str] = {}
    current: Optional[Dict[str, str]] = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            kind, _sep, name = stripped[1:-1].partition(":")
            if kind == "vendor" and not name:
                current = header
            else:
                current = sections[kind].setdefault(name, {}) if kind in sections and name else None
            continue
        if current is not None and "=" in stripped:
            key, _sep, value = stripped.partition("=")
            current[key.strip()] = value.strip()
    vendor = vendor or (path.stem if path else "")
    return Bundle(vendor, sections, path, unquote(header.get("name", "")))


def load_bundle(path: Path) -> Bundle:
    """Read the bundle at ``path`` (cached by path and modification time)."""
    path = Path(path)
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError as exc:
        raise PrusaProfilesError("Could not read the PrusaSlicer bundle {}: {}".format(path, exc)) from exc
    if key not in _BUNDLE_CACHE:
        for old in [item for item in _BUNDLE_CACHE if item[0] == key[0]]:
            del _BUNDLE_CACHE[old]
        _BUNDLE_CACHE[key] = parse_bundle(path.read_text(encoding="utf-8", errors="replace"), path.stem, path)
    return _BUNDLE_CACHE[key]


_BUNDLE_CACHE: Dict[Tuple[str, float], Bundle] = {}


def unquote(value: str) -> str:
    """A bundle string value: quotes removed and ``\\n`` escapes turned into new lines."""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return text.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")


def string_list(value: str) -> List[str]:
    """A list of quoted strings (``"A";"B"``) or plain ``;``/``,`` separated values."""
    text = value.strip()
    if not text:
        return []
    if text.startswith('"'):
        return [unquote(item) for item in re.findall(r'"(?:[^"\\]|\\.)*"', text)]
    return [item.strip() for item in re.split(r"[;,]", text) if item.strip()]


# ---------------------------------------------------------------------------
# Compatibility conditions
# ---------------------------------------------------------------------------


_TOKEN = re.compile(
    r"\s*(?:(?P<number>\d+(?:\.\d+)?)|(?P<string>\"(?:[^\"\\]|\\.)*\")|(?P<regex>/(?:[^/\\]|\\.)*/)"
    r"|(?P<op>&&|\|\||=~|!~|==|!=|<=|>=|<|>|!|\(|\)|\[|\])|(?P<word>[A-Za-z_][A-Za-z0-9_]*))"
)


def _tokens(text: str) -> List[Tuple[str, str]]:
    tokens: List[Tuple[str, str]] = []
    position = 0
    text = text.strip()
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            raise PrusaProfilesError("Cannot read the condition at {!r}".format(text[position:position + 20]))
        position = match.end()
        kind = match.lastgroup or ""
        tokens.append((kind, match.group(kind)))
    return tokens


class _Condition:
    """Recursive-descent evaluator for PrusaSlicer's compatibility expressions.

    ``or``/``||`` binds weakest, then ``and``/``&&``, then ``!``/``not``, then the
    comparisons ``== != < > <= >=`` and the regular expression matches
    ``=~ !~`` (a whole-string match, as in PrusaSlicer).
    """

    def __init__(self, text: str, lookup: Callable[[str, Optional[int]], Any]) -> None:
        self.tokens = _tokens(text)
        self.position = 0
        self.lookup = lookup

    def evaluate(self) -> bool:
        if not self.tokens:
            return True
        value = self._or()
        if self.position != len(self.tokens):
            raise PrusaProfilesError("Unexpected {!r} in the condition".format(self.tokens[self.position][1]))
        return bool(value)

    def _peek(self) -> Tuple[str, str]:
        return self.tokens[self.position] if self.position < len(self.tokens) else ("", "")

    def _take(self) -> Tuple[str, str]:
        token = self._peek()
        self.position += 1
        return token

    def _or(self) -> Any:
        value = self._and()
        while self._peek() in (("word", "or"), ("op", "||")):
            self._take()
            right = self._and()
            value = bool(value) or bool(right)
        return value

    def _and(self) -> Any:
        value = self._not()
        while self._peek() in (("word", "and"), ("op", "&&")):
            self._take()
            right = self._not()
            value = bool(value) and bool(right)
        return value

    def _not(self) -> Any:
        if self._peek() in (("op", "!"), ("word", "not")):
            self._take()
            return not bool(self._not())
        return self._compare()

    def _compare(self) -> Any:
        left = self._primary()
        kind, op = self._peek()
        if kind == "op" and op in ("==", "!=", "<", ">", "<=", ">=", "=~", "!~"):
            self._take()
            right = self._primary()
            if op in ("=~", "!~"):
                found = re.fullmatch(str(right), str(left), re.DOTALL) is not None
                return found if op == "=~" else not found
            left_value, right_value = _comparable(left), _comparable(right)
            if type(left_value) is not type(right_value):
                left_value, right_value = str(left), str(right)
            return {
                "==": left_value == right_value, "!=": left_value != right_value,
                "<": left_value < right_value, ">": left_value > right_value,
                "<=": left_value <= right_value, ">=": left_value >= right_value,
            }[op]
        return left

    def _primary(self) -> Any:
        kind, text = self._take()
        if kind == "number":
            return float(text)
        if kind == "string":
            return unquote(text)
        if kind == "regex":
            return text[1:-1]
        if (kind, text) == ("op", "("):
            value = self._or()
            if self._take() != ("op", ")"):
                raise PrusaProfilesError("A ')' is missing in the condition")
            return value
        if kind == "word":
            if text in ("true", "false"):
                return text == "true"
            index: Optional[int] = None
            if self._peek() == ("op", "["):
                self._take()
                number = self._take()
                if number[0] != "number" or self._take() != ("op", "]"):
                    raise PrusaProfilesError("Bad index after {!r}".format(text))
                index = int(float(number[1]))
            return self.lookup(text, index)
        raise PrusaProfilesError("Unexpected {!r} in the condition".format(text))


def _comparable(value: Any) -> Any:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return str(value)


def config_lookup(config: Mapping[str, str]) -> Callable[[str, Optional[int]], Any]:
    """Variables for a condition from a flattened preset: vectors by index, ``num_extruders``."""

    def lookup(name: str, index: Optional[int]) -> Any:
        if name == "num_extruders":
            return float(len(string_list(config.get("nozzle_diameter", "")) or [""]))
        raw = config.get(name)
        if raw is None:
            return ""
        if index is not None:
            items = string_list(raw)
            raw = items[index] if index < len(items) else ""
        text = unquote(raw)
        if text in ("0", "1") and name not in ("printer_variant",):
            # Booleans are written as 0/1; a bare variable is true when it is 1.
            return text == "1"
        return text

    return lookup


def matches(condition: str, config: Mapping[str, str]) -> bool:
    """True when ``condition`` holds for ``config`` (empty conditions always hold)."""
    return _Condition(unquote(condition), config_lookup(config)).evaluate()


def compatible(bundle: Bundle, kind: str, name: str, printer: Mapping[str, str], printer_name: str,
               print_config: Optional[Mapping[str, str]] = None) -> bool:
    """True when a print or filament preset fits the printer (and a filament the print).

    A preset's ``compatible_printers`` list wins over its condition; an
    unreadable condition counts as not fitting.
    """
    config = bundle.flat(kind, name)
    listed = string_list(config.get("compatible_printers", ""))
    try:
        if listed:
            fits = printer_name in listed
        else:
            fits = matches(config.get("compatible_printers_condition", ""), printer)
        if fits and print_config is not None and kind == "filament":
            fits = matches(config.get("compatible_prints_condition", ""), dict(printer, **print_config))
    except PrusaProfilesError:
        logger.debug("Unreadable condition in %s %r", kind, name, exc_info=True)
        return False
    return fits


# ---------------------------------------------------------------------------
# Installed bundles: vendors, printers, prints
# ---------------------------------------------------------------------------

#: Vendor ids in the palette's lists are ``prusa:<bundle file stem>``.
VENDOR_PREFIX = "prusa:"
#: Listed first: the vendor most PrusaSlicer users print with.
FIRST_VENDOR = "PrusaResearch"

_PROFILES_DIR_OVERRIDE: Optional[Path] = None


def profiles_dir(slicer_exe: Any) -> Optional[Path]:
    """The bundles folder of an installed PrusaSlicer (``.../Resources/profiles``)."""
    if _PROFILES_DIR_OVERRIDE is not None:
        return Path(_PROFILES_DIR_OVERRIDE)
    exe = Path(str(slicer_exe or ""))
    for candidate in (exe.parent.parent / "Resources" / "profiles", exe.parent / "resources" / "profiles"):
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def _bundle_paths(slicer_exe: Any) -> List[Path]:
    folder = profiles_dir(slicer_exe)
    if folder is None:
        return []
    try:
        paths = sorted(folder.glob("*.ini"))
    except OSError:
        return []
    return sorted(paths, key=lambda path: (path.stem != FIRST_VENDOR, path.stem.lower()))


#: The palette's vendor id of the user's own PrusaSlicer presets.
USER_VENDOR = VENDOR_PREFIX + "user"
USER_VENDOR_NAME = "My presets (PrusaSlicer)"
_DATADIR_OVERRIDE: Optional[Path] = None


def user_datadir() -> Path:
    """PrusaSlicer's own settings folder (where its user presets are)."""
    if _DATADIR_OVERRIDE is not None:
        return Path(_DATADIR_OVERRIDE)
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "PrusaSlicer"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or home) / "PrusaSlicer"
    return home / ".config" / "PrusaSlicer"


def user_bundle(slicer_exe: Any) -> Bundle:
    """The user's own presets (``printer/``, ``print/``, ``filament/`` in PrusaSlicer's settings folder).

    They inherit from PrusaSlicer's bundles; Prusa Research's and the
    generic templates' prints and filaments are offered with them.
    """
    sections: Dict[str, Dict[str, Dict[str, str]]] = {kind: {} for kind in KINDS}
    for kind in KINDS:
        try:
            paths = sorted((user_datadir() / kind).glob("*.ini"))
        except OSError:
            continue
        for path in paths:
            try:
                parsed = parse_bundle("[{}:{}]\n{}".format(kind, path.stem, path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
            sections[kind].update(parsed.sections[kind])
    bundle = Bundle("user", sections, user_datadir(), USER_VENDOR_NAME)
    system = []
    for path in _bundle_paths(slicer_exe):
        try:
            system.append(load_bundle(path))
        except (PrusaProfilesError, OSError, UnicodeError):
            continue
    offered = [item for item in system if item.vendor in (FIRST_VENDOR, "Templates")]
    bundle.fallbacks = offered + [item for item in system if item not in offered]
    bundle.offered_fallbacks = len(offered)
    return bundle


def bundle_for(slicer_exe: Any, vendor_id: str) -> Bundle:
    """The bundle of a palette vendor id (``prusa:PrusaResearch``, or :data:`USER_VENDOR`)."""
    if vendor_id == USER_VENDOR:
        return user_bundle(slicer_exe)
    stem = str(vendor_id or "")[len(VENDOR_PREFIX):] if str(vendor_id or "").startswith(VENDOR_PREFIX) else ""
    for path in _bundle_paths(slicer_exe):
        if path.stem == stem:
            return load_bundle(path)
    raise PrusaProfilesError("PrusaSlicer has no presets of {!r}; choose the vendor again.".format(vendor_id))


def fff_printers(bundle: Bundle) -> List[str]:
    """The filament printers of a bundle a user can pick (resin printers left out)."""
    printers = []
    for name in bundle.names("printer"):
        try:
            config = bundle.flat("printer", name)
        except PrusaProfilesError:
            logger.info("Skipping the PrusaSlicer printer %r: what it inherits is not installed.", name)
            continue
        # A user's own printer has no printer_model (it is not a vendor's printer).
        if unquote(config.get("printer_technology", "FFF")) != "SLA" and (
            bundle.vendor == "user" or unquote(config.get("printer_model", ""))
        ):
            printers.append(name)
    return printers


def extruder_count(printer: Mapping[str, str]) -> int:
    """Tools of a printer preset (one per ``nozzle_diameter`` entry)."""
    return max(1, len(string_list(printer.get("nozzle_diameter", ""))))


def list_vendors(slicer_exe: Any) -> List[Dict[str, Any]]:
    """PrusaSlicer's vendors with filament printers: ``[{"id", "name", "source", "has_printers"}]``."""
    vendors = []
    if fff_printers(user_bundle(slicer_exe)):
        vendors.append({"id": USER_VENDOR, "name": USER_VENDOR_NAME, "source": "prusa", "has_printers": True})
    for path in _bundle_paths(slicer_exe):
        try:
            bundle = load_bundle(path)
            if fff_printers(bundle):
                vendors.append({"id": VENDOR_PREFIX + path.stem, "name": bundle.name, "source": "prusa",
                                "has_printers": True})
        except (PrusaProfilesError, OSError, UnicodeError):
            logger.debug("Skipping the PrusaSlicer bundle %s", path, exc_info=True)
    return vendors


def list_printers(slicer_exe: Any, vendor_id: str) -> List[Dict[str, Any]]:
    """A vendor's printers: ``[{"name", "machine_path", "default_process", "source", "extruders"}]``."""
    bundle = bundle_for(slicer_exe, vendor_id)
    rows = []
    for name in fff_printers(bundle):
        config = bundle.flat("printer", name)
        rows.append({
            "name": name,
            "machine_path": str(bundle.path or ""),
            "default_process": unquote(config.get("default_print_profile", "")),
            "source": "prusa",
            "extruders": extruder_count(config),
        })
    return rows


def list_processes(slicer_exe: Any, vendor_id: str, printer_name: str) -> List[Dict[str, Any]]:
    """The prints that fit a printer: ``[{"name", "process_path", "default"}]``, the printer's default first."""
    bundle = bundle_for(slicer_exe, vendor_id)
    printer = bundle.flat("printer", printer_name)
    default = unquote(printer.get("default_print_profile", ""))
    names = [name for name in bundle.all_names("print") if compatible(bundle, "print", name, printer, printer_name)]
    names.sort(key=lambda name: name != default)
    # "values": the print's own infill, walls and layers (shown in empty body setting fields).
    return [{"name": name, "process_path": "", "default": name == default,
             "values": body_settings.process_values(bundle.flat("print", name), "prusa")} for name in names]


# ---------------------------------------------------------------------------
# Filaments
# ---------------------------------------------------------------------------


def alias(name: str) -> str:
    """The filament name PrusaSlicer shows: the preset name before " @"."""
    return str(name).split(" @", 1)[0].strip()


def _words(text: str) -> List[str]:
    return [word for word in re.split(r"[^a-z0-9+]+", str(text or "").lower()) if word]


def _brand_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _field(material: Any, name: str, default: Any = "") -> Any:
    if isinstance(material, Mapping):
        return material.get(name, default)
    value = getattr(material, name, default)
    return default if value is None else value


def pick_filament(bundle: Bundle, candidates: Sequence[str], material: Any, named: str = "") -> Tuple[Optional[str], bool]:
    """The filament preset for a design material among ``candidates`` (presets that fit).

    Returns ``(name, own)``: ``own`` is True when the preset is the material's
    own filament (the name it is given outright, or its brand and product),
    False for a generic stand-in of the same type, and the name is None when
    no preset of the type fits.
    """
    wanted = str(_field(material, "filament_type", "PLA") or "PLA").strip().upper()
    base = wanted.split("-")[0]
    typed = [name for name in candidates if unquote(bundle.flat("filament", name).get("filament_type", "")).upper() == wanted]
    if not typed:
        typed = [name for name in candidates
                 if unquote(bundle.flat("filament", name).get("filament_type", "")).upper().split("-")[0] == base]
    if not typed:
        return None, False
    brand = _brand_key(_field(material, "manufacturer", ""))
    product = _words(_field(material, "product_line", ""))
    hint = str(named or "").split(" @")[0].strip().lower()
    best: Tuple[int, int] = (-1, 0)
    chosen = None
    for index, name in enumerate(typed):
        config = bundle.flat("filament", name)
        shown = alias(name).lower()
        words = set(_words(shown))
        vendor = _brand_key(unquote(config.get("filament_vendor", "")))
        score = 0
        if hint and shown == hint:
            score = 300
        elif hint and all(word in words for word in _words(hint)):
            score = 200
        elif brand and product and all(word in words for word in product) and (
            (vendor and (vendor.startswith(brand) or brand.startswith(vendor))) or brand in _brand_key(shown)
        ):
            # The brand is the preset's vendor ("Prusa Polymers") or in its name ("Prusament PLA").
            score = 150
        elif shown == "generic " + wanted.lower():
            score = 20
        elif shown.startswith("generic"):
            score = 10
        if (score, -index) > best:
            best, chosen = (score, -index), name
    return chosen, best[0] >= 150


def printer_default_filament(bundle: Bundle, printer: Mapping[str, str], printer_name: str) -> Optional[str]:
    """The printer's default filament preset (for tools no design filament uses)."""
    for name in string_list(printer.get("default_filament_profile", "")):
        if bundle.has("filament", name) and compatible(bundle, "filament", name, printer, printer_name):
            return name
    return None


# ---------------------------------------------------------------------------
# The full configuration
# ---------------------------------------------------------------------------

#: PrusaSlicer 2.9's command line accepts ``--printer-profile`` and friends
#: but applies only the print preset, so Filamatch builds the configuration
#: from the bundle and gives it with ``--load``.
CLI_TIMEOUT_SECONDS = 600.0
#: The G-code block in which PrusaSlicer writes its full configuration.
CONFIG_BEGIN = "; prusaslicer_config = begin"
CONFIG_END = "; prusaslicer_config = end"
PROJECT_CONFIG_PART = "Metadata/Slic3r_PE.config"

_CACHE_ROOT_OVERRIDE: Optional[Path] = None
#: Replaceable command runner ``(command, timeout, cwd) -> CompletedProcess`` (the self-test fakes it).
_command_runner: Optional[Callable[..., Any]] = None


def _cache_root() -> Path:
    if _CACHE_ROOT_OVERRIDE is not None:
        return Path(_CACHE_ROOT_OVERRIDE)
    from .. import config

    return Path(config.CACHE_DIR) / "prusaslicer"


def _run(command: Sequence[str], cwd: Path) -> Any:
    if _command_runner is not None:
        return _command_runner(list(command), timeout=CLI_TIMEOUT_SECONDS, cwd=str(cwd))
    import subprocess

    return subprocess.run(list(command), capture_output=True, text=True, timeout=CLI_TIMEOUT_SECONDS, cwd=str(cwd))


def _output(result: Any) -> str:
    return "{}\n{}".format(getattr(result, "stdout", "") or "", getattr(result, "stderr", "") or "")


def _exe_key(slicer_exe: Any) -> str:
    path = Path(str(slicer_exe))
    try:
        stamp = int(path.stat().st_mtime)
    except OSError:
        stamp = 0
    return re.sub(r"[^A-Za-z0-9]+", "_", "{}_{}".format(path.name, stamp))


def _empty_datadir() -> Path:
    """A settings folder of Filamatch's own: the user's PrusaSlicer settings are never read or written."""
    folder = _cache_root() / "datadir"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def option_kinds(slicer_exe: Any) -> Tuple[Dict[str, str], frozenset]:
    """``(types, keys)`` of PrusaSlicer's settings, read once per PrusaSlicer version.

    ``types`` from ``--help-fff``: "ABCD" text, "N" a number, "bool" on/off
    (lists of these are joined with ``;``, ``,`` and ``,``); ``keys`` every
    setting PrusaSlicer saves (``--save`` of its defaults).
    """
    import json

    cache = _cache_root() / "options_{}.json".format(_exe_key(slicer_exe))
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        return dict(data["types"]), frozenset(data["keys"])
    except (OSError, ValueError, KeyError):
        pass
    work = Path(tempfile.mkdtemp(prefix="options_", dir=str(_empty_datadir().parent)))
    try:
        types: Dict[str, str] = {}
        for line in _output(_run([str(slicer_exe), "--help-fff"], work)).splitlines():
            match = re.match(r"^ --([a-z0-9-]+)(?: (ABCD|N|X,Y|[A-Z]+))?(?:\s|$)", line)
            if match:
                types[match.group(1).replace("-", "_")] = match.group(2) or "bool"
        _run([str(slicer_exe), "--datadir", str(_empty_datadir()), "--save", str(work / "defaults.ini")], work)
        keys = set()
        try:
            for line in (work / "defaults.ini").read_text(encoding="utf-8").splitlines():
                if " = " in line and not line.startswith("#"):
                    keys.add(line.split(" = ", 1)[0].strip())
        except OSError:
            pass
        if not types or not keys:
            raise PrusaProfilesError("PrusaSlicer did not list its settings; is {} PrusaSlicer?".format(slicer_exe))
        cache.write_text(json.dumps({"types": types, "keys": sorted(keys)}), encoding="utf-8")
        return types, frozenset(keys)
    finally:
        shutil.rmtree(str(work), ignore_errors=True)


def infill_patterns(slicer_exe: Any) -> List[str]:
    """PrusaSlicer's infill types (``--fill-pattern`` in ``--help-fff``), read once per version."""
    import json

    cache = _cache_root() / "infill_patterns_{}.json".format(_exe_key(slicer_exe))
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if isinstance(cached, list) and cached:
            return [str(name) for name in cached]
    except (OSError, ValueError):
        pass
    work = Path(tempfile.mkdtemp(prefix="options_", dir=str(_empty_datadir().parent)))
    try:
        text = _output(_run([str(slicer_exe), "--help-fff"], work))
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
    found = re.search(r"^ --fill-pattern\b[^(]*\(([^;)]*);\s*default", text, re.M)
    names = [name.strip() for name in found.group(1).split(",") if name.strip()] if found else []
    if not names:
        raise PrusaProfilesError("PrusaSlicer did not list its infill types")
    cache.write_text(json.dumps(names), encoding="utf-8")
    return names


def _quoted(value: str) -> str:
    text = value.strip()
    if text.startswith('"'):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_config(bundle: Bundle, printer_name: str, print_name: str, filaments: Sequence[Tuple[str, Mapping[str, str]]],
                 colours: Sequence[str], types: Mapping[str, str], keys: frozenset) -> Dict[str, str]:
    """The full configuration for a printer, a print and one filament per tool.

    ``filaments`` are ``(settings id, flattened filament preset)`` per tool;
    their settings become per-tool lists in PrusaSlicer's own list format.
    ``colours`` (one per design filament) colour the tools in order.
    """
    printer = bundle.flat("printer", printer_name)
    config = {key: value for key, value in printer.items() if key in keys}
    config.update({key: value for key, value in bundle.flat("print", print_name).items() if key in keys})
    filament_keys = [key for key in dict.fromkeys(key for _name, flat in filaments for key in flat) if key in keys]
    for key in filament_keys:
        values = [str(flat.get(key, filaments[0][1].get(key, ""))) for _name, flat in filaments]
        kind = types.get(key)
        if kind == "ABCD":
            config[key] = ";".join(_quoted(value) for value in values)
        elif kind in ("N", "bool"):
            config[key] = ",".join(value.strip() for value in values)
        else:
            # Not a command-line option: one value for every tool.
            config[key] = values[0].strip()
    names = [name for name, _flat in filaments]
    tool_colours = [str(colour) for colour in colours] + [
        unquote(string_list(flat.get("filament_colour", "#FFFFFF"))[0] if flat.get("filament_colour") else "#FFFFFF")
        for _name, flat in filaments[len(colours):]
    ]
    config.update(
        printer_settings_id=printer_name,
        print_settings_id=print_name,
        filament_settings_id=";".join(_quoted(name) for name in names),
        filament_colour=";".join(tool_colours[: len(filaments)]),
        extruder_colour=";".join(tool_colours[: len(filaments)]),
    )
    return config


def _cube_stl() -> str:
    corners = [("0 0 0", "10 0 0", "10 10 0"), ("0 0 0", "10 10 0", "0 10 0"), ("0 0 10", "10 10 10", "10 0 10"),
               ("0 0 10", "0 10 10", "10 10 10"), ("0 0 0", "10 0 10", "10 0 0"), ("0 0 0", "0 0 10", "10 0 10"),
               ("0 10 0", "10 10 0", "10 10 10"), ("0 10 0", "10 10 10", "0 10 10"), ("0 0 0", "0 10 0", "0 10 10"),
               ("0 0 0", "0 10 10", "0 0 10"), ("10 0 0", "10 10 10", "10 10 0"), ("10 0 0", "10 0 10", "10 10 10")]
    facets = "".join(
        "facet normal 0 0 0\nouter loop\n" + "".join("vertex {}\n".format(v) for v in triangle) + "endloop\nendfacet\n"
        for triangle in corners
    )
    return "solid cube\n" + facets + "endsolid cube\n"


def _slice(slicer_exe: Any, model: Path, config_ini: Optional[Path], work: Path) -> str:
    """Slice ``model`` to text G-code and return it; PrusaSlicer's own settings folder is never used.

    PrusaSlicer 2.9.7 sometimes crashes while it closes, after the G-code is
    written; a G-code that ends with its configuration block counts.
    """
    gcode = work / "out.gcode"
    command = [str(slicer_exe), "--datadir", str(_empty_datadir())]
    if config_ini is not None:
        command += ["--load", str(config_ini)]
    command += ["--no-binary-gcode", "--export-gcode", "-o", str(gcode), str(model)]
    result = _run(command, work)
    text = ""
    try:
        text = gcode.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    if CONFIG_END not in text:
        lines = [line for line in _output(result).splitlines() if line.strip() and not line.startswith("[20")]
        raise PrusaProfilesError("PrusaSlicer did not slice: {}".format(
            next((line for line in reversed(lines) if "rror" in line or "nvalid" in line), lines[-1] if lines else
                 "exit code {}".format(getattr(result, "returncode", "?")))))
    code = getattr(result, "returncode", 0)
    if code not in (0, None):
        logger.debug("PrusaSlicer ended with code %s after writing the G-code.", code)
    return text


def normalized_config(slicer_exe: Any, config: Mapping[str, str], work: Path) -> List[str]:
    """``; key = value`` lines of ``config`` as PrusaSlicer writes them (slices a 10 mm cube once)."""
    ini = work / "config.ini"
    ini.write_text("".join("{} = {}\n".format(key, value) for key, value in sorted(config.items())), encoding="utf-8")
    cube = work / "cube.stl"
    cube.write_text(_cube_stl(), encoding="utf-8")
    text = _slice(slicer_exe, cube, ini, work)
    block = text[text.index(CONFIG_BEGIN) + len(CONFIG_BEGIN):text.index(CONFIG_END)]
    return [line for line in block.splitlines() if line.startswith("; ") and " = " in line]


def slicer_version(slicer_exe: Any) -> str:
    """"2.9.7" from PrusaSlicer's ``--help`` (or "")."""
    work = _empty_datadir().parent
    match = re.search(r"PrusaSlicer-(\d+(?:\.\d+)+)", _output(_run([str(slicer_exe), "--help"], work)))
    return match.group(1) if match else ""


# ---------------------------------------------------------------------------
# Projects and estimates
# ---------------------------------------------------------------------------


def choose_filaments(bundle: Bundle, printer_name: str, print_name: str, materials: Sequence[Any]
                     ) -> Tuple[List[Tuple[str, Dict[str, str]]], List[Dict[str, str]]]:
    """One filament preset per tool: the design filaments in slot order, then the printer's default.

    A database filament with no preset of its own gets one made from the
    generic preset of its type (see filament_presets.prusa_values).  Returns
    ``(tools, built)``: ``(settings id, flattened preset)`` per tool, and the
    presets made from the database (each with its ``name``).
    """
    from . import filament_presets
    from . import slicer_profiles

    printer = bundle.flat("printer", printer_name)
    print_config = bundle.flat("print", print_name)
    tools_count = extruder_count(printer)
    if len(materials) > tools_count:
        raise PrusaProfilesError("The {} has {} tool(s); this design uses {} filaments.".format(
            printer_name, tools_count, len(materials)))
    candidates = [name for name in bundle.all_names("filament")
                  if compatible(bundle, "filament", name, printer, printer_name, print_config)]
    tools: List[Tuple[str, Dict[str, str]]] = []
    built: List[Dict[str, str]] = []
    for material in materials:
        named = slicer_profiles._named_preset_hint(material, "prusa")
        name, own = pick_filament(bundle, candidates, material, named)
        if name is None:
            raise PrusaProfilesError("No {} filament preset of PrusaSlicer fits the {}.".format(
                _field(material, "filament_type", "PLA"), printer_name))
        flat = dict(bundle.flat("filament", name))
        settings_id = name
        data = _field(material, "slicer_data", None)
        if isinstance(data, Mapping) and data and not own:
            brand = str(_field(material, "manufacturer", "") or "")
            product = str(_field(material, "product_line", "") or _field(material, "display_name", "") or "")
            settings_id = filament_presets.filament_title(data, "prusa", brand, product)
            flat.update(filament_presets.prusa_values(data, brand, flat))
            built.append(dict(flat, name=settings_id))
            logger.info("Built the PrusaSlicer filament %r from %r and the Open Filament Database.", settings_id, name)
        tools.append((settings_id, flat))
    filler = printer_default_filament(bundle, printer, printer_name)
    while len(tools) < tools_count:
        tools.append((filler, dict(bundle.flat("filament", filler))) if filler else tools[0])
    return tools, built


def prepare_project(base_3mf: Path, output_3mf: Path, vendor_id: str, printer_name: str, print_name: str,
                    slicer_exe: Any, materials: Sequence[Any]) -> Dict[str, Any]:
    """Write a PrusaSlicer project: each body on its tool, and the full printer, print and filament settings.

    The bodies go in PrusaSlicer's own layout (prusa_project); the settings
    are built from the bundle and written back by PrusaSlicer itself, then
    stored as ``Metadata/Slic3r_PE.config`` as in PrusaSlicer's own projects.
    Returns ``{"filament_names", "built_presets", "slicer_version"}``.
    """
    import json
    import zipfile

    from . import prusa_project
    from . import slicer_profiles

    bundle = bundle_for(slicer_exe, vendor_id)
    if printer_name not in fff_printers(bundle):
        raise PrusaProfilesError("PrusaSlicer has no printer {!r}; choose the printer again.".format(printer_name))
    if not bundle.has("print", print_name) or not compatible(
        bundle, "print", print_name, bundle.flat("printer", printer_name), printer_name
    ):
        raise PrusaProfilesError("The print {!r} does not fit the {}.".format(print_name, printer_name))
    slicer_profiles.check_slot_assignments(Path(base_3mf), materials)
    tools, built = choose_filaments(bundle, printer_name, print_name, materials)
    types, keys = option_kinds(slicer_exe)
    colours = [slicer_profiles._normalize_color_hex(_field(material, "color_hex", "#FFFFFF")) for material in materials]
    config = build_config(bundle, printer_name, print_name, tools, colours, types, keys)
    _cache_root().mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="prepare_", dir=str(_cache_root())))
    try:
        lines = normalized_config(slicer_exe, config, work)
        layout = work / "layout.3mf"
        placed = prusa_project.to_prusa_layout(Path(base_3mf), layout, bed_center(config.get("bed_shape", "")))
        version = slicer_version(slicer_exe) or "2"
        header = "; generated by PrusaSlicer {} on {}\n\n".format(version, time.strftime("%Y-%m-%d at %H:%M:%S UTC", time.gmtime()))
        entries = {PROJECT_CONFIG_PART: (header + "\n".join(lines) + "\n").encode("utf-8")}
        spot = wipe_tower_spot(config, placed["box"]) if unquote(config.get("wipe_tower", "0")) == "1" else None
        if spot is not None:
            entries[WIPE_TOWER_PART] = (
                '<?xml version="1.0" encoding="utf-8"?>\n<wipe_tower_information bed_idx="0" position_x="{:g}" '
                'position_y="{:g}" rotation_deg="0"/>\n'.format(*spot)
            ).encode("utf-8")
        slicer_profiles.rewrite_zip_entries(layout, entries)
        output_3mf = Path(output_3mf)
        output_3mf.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(layout), str(output_3mf))
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
    built_paths = []
    folder = _cache_root() / "built"
    folder.mkdir(parents=True, exist_ok=True)
    for preset in built:
        path = folder / (re.sub(r'[\\/:*?"<>|]+', "_", preset["name"]) + ".json")
        path.write_text(json.dumps(dict(preset, filamatch_slicer="prusa"), indent=1), encoding="utf-8")
        built_paths.append(str(path))
    logger.info("Prepared the PrusaSlicer project %s (%s, %s, filaments %s).", output_3mf, printer_name, print_name,
                [name for name, _flat in tools])
    return {"filament_names": tuple(name for name, _flat in tools[: len(materials)]),
            "built_presets": tuple(built_paths), "slicer_version": version}


#: Where PrusaSlicer 2.9 keeps the wipe tower's place (front-left corner, mm).
WIPE_TOWER_PART = "Metadata/Prusa_Slicer_wipe_tower_information.xml"
#: Space between the bodies and the wipe tower, and from the bed's edge (mm).
WIPE_TOWER_GAP = 10.0
#: The tower's depth depends on the purge volumes; this much room is kept (mm).
WIPE_TOWER_DEPTH = 60.0


def wipe_tower_spot(config: Mapping[str, str], box: Sequence[float]) -> Optional[Tuple[float, float]]:
    """A place for the wipe tower next to the bodies (``box``: min x, min y, max x, max y).

    Right of the bodies, else left, behind, in front; None when none fits on
    the bed (PrusaSlicer then places it).
    """
    points = re.findall(r"(-?[\d.]+)x(-?[\d.]+)", str(config.get("bed_shape", "")))
    if len(points) < 3 or len(box) != 4:
        return None
    bed = (min(float(x) for x, _y in points), min(float(y) for _x, y in points),
           max(float(x) for x, _y in points), max(float(y) for _x, y in points))
    try:
        brim = float(config.get("wipe_tower_brim_width", "0") or 0)
        width = float(config.get("wipe_tower_width", "60") or 60) + 2 * brim
    except ValueError:
        return None
    depth = WIPE_TOWER_DEPTH + 2 * brim
    gap = WIPE_TOWER_GAP
    min_x, min_y, max_x, max_y = box
    for x, y in (
        (max_x + gap + brim, min_y + brim),
        (min_x - gap - width + brim, min_y + brim),
        (min_x + brim, max_y + gap + brim),
        (min_x + brim, min_y - gap - depth + brim),
    ):
        left, front = x - brim, y - brim
        if left >= bed[0] + gap and front >= bed[1] + gap and left + width <= bed[2] - gap and front + depth <= bed[3] - gap:
            return (round(x, 1), round(y, 1))
    return None


def bed_center(bed_shape: str) -> Optional[Tuple[float, float]]:
    """The middle of a ``bed_shape`` ("0x0,350x0,350x320,0x320" -> (175, 160)), or None."""
    points = re.findall(r"(-?[\d.]+)x(-?[\d.]+)", str(bed_shape or ""))
    if len(points) < 3:
        return None
    xs, ys = [float(x) for x, _y in points], [float(y) for _x, y in points]
    return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)


def _minutes(text: str) -> int:
    """Minutes of PrusaSlicer's "1d 2h 3m 4s"."""
    total = 0.0
    for number, unit in re.findall(r"(\d+)\s*([dhms])", text):
        total += int(number) * {"d": 1440.0, "h": 60.0, "m": 1.0, "s": 1.0 / 60.0}[unit]
    return int(round(total))


#: A longer estimate is PrusaSlicer's own fault (MMU3 printers, now and then
#: "2147483647d ..."): slice again, up to ESTIMATE_TRIES times.
IMPLAUSIBLE_MINUTES = 14 * 24 * 60
ESTIMATE_TRIES = 3


def estimate_print(project_3mf: Path, slicer_exe: Any) -> Dict[str, Any]:
    """Slice a PrusaSlicer project with its own settings for the time and grams.

    Returns ``{"minutes", "grams", "plates", "filament_grams", "cost"}`` like
    slicer_profiles.estimate_print, one gram entry per tool (0 when unused);
    ``minutes`` is None when PrusaSlicer's time stays broken.
    """
    _cache_root().mkdir(parents=True, exist_ok=True)
    minutes: Optional[int] = None
    text = ""
    for attempt in range(ESTIMATE_TRIES):
        work = Path(tempfile.mkdtemp(prefix="estimate_", dir=str(_cache_root())))
        try:
            text = _slice(slicer_exe, Path(project_3mf), None, work)
        finally:
            shutil.rmtree(str(work), ignore_errors=True)
        found = re.search(r"^; estimated printing time \(normal mode\) = (.+)$", text, re.M)
        minutes = _minutes(found.group(1)) if found else None
        if minutes is not None and minutes <= IMPLAUSIBLE_MINUTES:
            break
        logger.warning("PrusaSlicer gave a broken print time (%s); slicing again.", found.group(1) if found else "none")
        minutes = None
    per_tool = re.search(r"^; filament used \[g\] = (.+)$", text, re.M)
    total = re.search(r"^; total filament used \[g\] = ([\d.]+)", text, re.M)
    cost = re.search(r"^; total filament cost = ([\d.]+)", text, re.M)
    if not total:
        raise PrusaProfilesError("PrusaSlicer gave no print estimate")
    grams = [round(float(value), 1) for value in re.findall(r"[\d.]+", per_tool.group(1))] if per_tool else []
    return {"minutes": minutes, "grams": round(float(total.group(1)), 1), "plates": 1, "filament_grams": grams,
            # PrusaSlicer works it out from the filament presets' prices per kg.
            "cost": round(float(cost.group(1)), 2) if cost else None}


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


_TEST_BUNDLE = r"""
# A small bundle in PrusaSlicer's format.
[vendor]
name = Test Vendor
config_version = 1.0.0

[printer:*common*]
printer_technology = FFF
bed_shape = 0x0,250x0,250x210,0x210
printer_notes = Keep these keywords!\nPRINTER_VENDOR_TEST\nPRINTER_MODEL_TOOLS
default_print_profile = 0.20mm QUALITY @TOOLS
default_filament_profile = "Generic PLA @TOOLS"
thumbnails = 16x16/PNG

[printer:Test Tools 0.4 nozzle]
inherits = *common*
printer_model = TOOLS
printer_variant = 0.4
nozzle_diameter = 0.4,0.4,0.4

[printer:Test Single 0.6 nozzle]
inherits = *common*
printer_model = SINGLE
printer_variant = 0.6
nozzle_diameter = 0.6
printer_notes = PRINTER_VENDOR_TEST

[printer:Test Resin]
printer_technology = SLA
printer_model = RESIN

[print:*0.20*]
layer_height = 0.2

[print:0.20mm QUALITY @TOOLS]
inherits = *0.20*
compatible_printers_condition = printer_notes=~/.*PRINTER_MODEL_TOOLS.*/ and nozzle_diameter[0]==0.4

[print:0.30mm DRAFT @SINGLE]
layer_height = 0.3
compatible_printers_condition = printer_model=="SINGLE" && num_extruders==1

[filament:*PLA*]
filament_type = PLA
filament_vendor = Generic
temperature = 215
first_layer_temperature = 215
bed_temperature = 60
first_layer_bed_temperature = 60
filament_colour = #FF8000
start_filament_gcode = "M572 S0.03 ; pressure advance"

[filament:Generic PLA @TOOLS]
inherits = *PLA*
compatible_printers_condition = printer_notes=~/.*PRINTER_MODEL_TOOLS.*/

[filament:Prusament PLA @TOOLS]
inherits = *PLA*
filament_vendor = Prusa Polymers
temperature = 220
compatible_printers_condition = printer_notes=~/.*PRINTER_MODEL_TOOLS.*/ and ! (nozzle_diameter[0]==0.6)

[filament:Generic PETG @TOOLS]
inherits = *PLA*
filament_type = PETG
temperature = 240
compatible_printers_condition = printer_notes=~/.*TOOLS.*/
compatible_prints_condition = layer_height > 0.1

[filament:Generic PLA @SINGLE]
inherits = *PLA*
compatible_printers = "Test Single 0.6 nozzle"
"""


def _check_bundle_and_conditions() -> None:
    bundle = parse_bundle(_TEST_BUNDLE, "Test")
    _expect(bundle.name == "Test Vendor" and bundle.names("printer") == ["Test Tools 0.4 nozzle",
            "Test Single 0.6 nozzle", "Test Resin"], "vendor name and pickable printers: {}".format(bundle.names("printer")))
    tools = bundle.flat("printer", "Test Tools 0.4 nozzle")
    _expect(tools["bed_shape"] == "0x0,250x0,250x210,0x210" and extruder_count(tools) == 3, "inherited printer keys")
    _expect(fff_printers(bundle) == ["Test Tools 0.4 nozzle", "Test Single 0.6 nozzle"], "resin printers are left out")
    cases = (
        ('printer_notes=~/.*PRINTER_MODEL_TOOLS.*/', True),
        ('printer_notes!~/.*PRINTER_MODEL_TOOLS.*/', False),
        ('printer_model=="TOOLS" and nozzle_diameter[0]==0.4', True),
        ('printer_model=~/(TOOL|TOOLSX)/', False),
        ('num_extruders>1 && ! (nozzle_diameter[0]==0.6)', True),
        ('nozzle_diameter[0]>=0.6 or printer_model!="TOOLS"', False),
        ('', True),
    )
    for condition, expected in cases:
        _expect(matches(condition, tools) is expected, "{!r} should be {}".format(condition, expected))
    try:
        matches("nozzle_diameter[0]==", tools)
    except PrusaProfilesError:
        pass
    else:
        raise AssertionError("an unreadable condition must raise")
    single = bundle.flat("printer", "Test Single 0.6 nozzle")
    _expect(not compatible(bundle, "filament", "Prusament PLA @TOOLS", single, "Test Single 0.6 nozzle")
            and compatible(bundle, "filament", "Generic PLA @SINGLE", single, "Test Single 0.6 nozzle")
            and not compatible(bundle, "filament", "Generic PLA @SINGLE", tools, "Test Tools 0.4 nozzle"),
            "compatible_printers lists win over conditions")
    draft = {"layer_height": "0.05"}
    _expect(not compatible(bundle, "filament", "Generic PETG @TOOLS", tools, "Test Tools 0.4 nozzle", draft),
            "a filament's print condition counts")
    _expect(unquote(r'"a\nb \"c\""') == 'a\nb "c"' and string_list('"A";"B"') == ["A", "B"], "quoting")
    _expect(_minutes("1d 2h 3m 20s") == 1563 and _minutes("5m 29s") == 5, "PrusaSlicer times")
    _expect(bed_center("0x0,350x0,350x320,0x320") == (175.0, 160.0) and bed_center("") is None, "bed middle")
    h2d = {"bed_shape": "0x0,350x0,350x320,0x320", "wipe_tower_width": "60", "wipe_tower_brim_width": "3"}
    _expect(wipe_tower_spot(h2d, (140.0, 140.0, 210.0, 180.0)) == (223.0, 143.0), "right of the bodies")
    _expect(wipe_tower_spot(h2d, (100.0, 140.0, 300.0, 180.0)) == (27.0, 143.0), "left when the right is full")
    _expect(wipe_tower_spot(h2d, (20.0, 20.0, 330.0, 300.0)) is None, "none fits: PrusaSlicer places it")


def _check_listing() -> None:
    global _PROFILES_DIR_OVERRIDE, _DATADIR_OVERRIDE
    with tempfile.TemporaryDirectory(prefix="filamatch-prusa-test-") as temp:
        root = Path(temp)
        _DATADIR_OVERRIDE = root / "user"
        (root / "Test.ini").write_text(_TEST_BUNDLE, encoding="utf-8")
        (root / "ResinOnly.ini").write_text("[vendor]\nname = Resin\n[printer:R]\nprinter_technology = SLA\nprinter_model = R\n",
                                            encoding="utf-8")
        (root / (FIRST_VENDOR + ".ini")).write_text(
            _TEST_BUNDLE.replace("Test Vendor", "Prusa Research"), encoding="utf-8")
        _PROFILES_DIR_OVERRIDE = root
        try:
            vendors = list_vendors("unused")
            _expect([vendor["id"] for vendor in vendors] == [VENDOR_PREFIX + FIRST_VENDOR, VENDOR_PREFIX + "Test"],
                    "Prusa first, resin-only bundles left out: {}".format(vendors))
            printers = list_printers("unused", VENDOR_PREFIX + "Test")
            _expect([(row["name"], row["extruders"], row["default_process"]) for row in printers] == [
                ("Test Tools 0.4 nozzle", 3, "0.20mm QUALITY @TOOLS"), ("Test Single 0.6 nozzle", 1, "0.20mm QUALITY @TOOLS")],
                "printers: {}".format(printers))
            prints = list_processes("unused", VENDOR_PREFIX + "Test", "Test Single 0.6 nozzle")
            _expect([row["name"] for row in prints] == ["0.30mm DRAFT @SINGLE"], "prints that fit: {}".format(prints))
            try:
                bundle_for("unused", "bambu_local")
            except PrusaProfilesError:
                pass
            else:
                raise AssertionError("another slicer's vendor id is refused")
            # The user's own presets: a printer of their own and one that inherits a vendor printer.
            for kind, name, text in (
                ("printer", "My H2D", "printer_technology = FFF\nnozzle_diameter = 0.4,0.4\nbed_shape = 0x0,350x0,350x320,0x320\n"
                 "printer_notes = mine\ndefault_print_profile = My 0.20mm\ndefault_filament_profile = Generic PLA @TOOLS\n"),
                ("printer", "My Tools", "inherits = Test Tools 0.4 nozzle\nbed_shape = 0x0,300x0,300x300,0x300\n"),
                ("print", "My 0.20mm", 'inherits = 0.20mm QUALITY @TOOLS\ncompatible_printers = "My H2D"\ncompatible_printers_condition = \n'),
                ("filament", "My PLA", 'inherits = Generic PLA @TOOLS\ntemperature = 205\ncompatible_printers = "My H2D"\n'),
            ):
                folder = root / "user" / kind
                folder.mkdir(parents=True, exist_ok=True)
                (folder / (name + ".ini")).write_text(text, encoding="utf-8")
            vendors = list_vendors("unused")
            _expect(vendors[0]["id"] == USER_VENDOR, "the user's own presets come first: {}".format(vendors[0]))
            mine = [row["name"] for row in list_printers("unused", USER_VENDOR)]
            _expect(mine == ["My H2D", "My Tools"], "a printer of their own and an inherited one: {}".format(mine))
            _expect([row["name"] for row in list_processes("unused", USER_VENDOR, "My H2D")] == ["My 0.20mm"]
                    and "0.20mm QUALITY @TOOLS" in [row["name"] for row in list_processes("unused", USER_VENDOR, "My Tools")],
                    "own prints, and the vendor's prints that fit an inherited printer")
            bundle = bundle_for("unused", USER_VENDOR)
            _expect(bundle.flat("printer", "My Tools")["nozzle_diameter"] == "0.4,0.4,0.4"
                    and bundle.flat("print", "My 0.20mm")["layer_height"] == "0.2", "own presets inherit the vendor's")
            tools, _built = choose_filaments(bundle, "My H2D", "My 0.20mm", [{"filament_type": "PLA", "product_line": "PLA"}])
            _expect(tools[0][0] == "My PLA" and tools[0][1]["temperature"] == "205",
                    "an own filament that lists the printer is picked: {}".format(tools[0][0]))
        finally:
            _PROFILES_DIR_OVERRIDE = None
            _DATADIR_OVERRIDE = None


def _check_filaments_and_config() -> None:
    bundle = parse_bundle(_TEST_BUNDLE, "Test")
    tools = "Test Tools 0.4 nozzle"
    candidates = [name for name in bundle.names("filament")
                  if compatible(bundle, "filament", name, bundle.flat("printer", tools), tools)]
    pick = lambda material, named="": pick_filament(bundle, candidates, material, named)
    _expect(pick({"filament_type": "PLA", "manufacturer": "Prusament", "product_line": "PLA"}) == ("Prusament PLA @TOOLS", True)
            and pick({"filament_type": "PLA", "manufacturer": "Voxel", "product_line": "PLA"}) == ("Generic PLA @TOOLS", False)
            and pick({"filament_type": "PLA"}, "Prusament PLA") == ("Prusament PLA @TOOLS", True)
            and pick({"filament_type": "TPU"}) == (None, False),
            "brand and product, a generic stand-in, a name given outright, no fitting type")
    materials = [
        {"filament_type": "PLA", "color_hex": "#000000", "manufacturer": "Prusament", "product_line": "PLA"},
        {"filament_type": "PETG", "color_hex": "#FFFFFF", "manufacturer": "addnorth", "product_line": "PETG Economy",
         "slicer_data": {"min_print_temperature": 230, "max_print_temperature": 250}},
    ]
    chosen, built = choose_filaments(bundle, tools, "0.20mm QUALITY @TOOLS", materials)
    _expect([name for name, _flat in chosen] == ["Prusament PLA @TOOLS", "addnorth PETG Economy", "Generic PLA @TOOLS"],
            "one preset per tool, the printer's default filament on the spare tool: {}".format([n for n, _f in chosen]))
    _expect(chosen[1][1]["temperature"] == "240" and built[0]["name"] == "addnorth PETG Economy"
            and built[0]["filament_vendor"] == "addnorth", "a database filament gets a made preset")
    try:
        choose_filaments(bundle, "Test Single 0.6 nozzle", "0.30mm DRAFT @SINGLE", materials)
    except PrusaProfilesError as exc:
        _expect("1 tool(s)" in str(exc), "the error names the tools: {}".format(exc))
    else:
        raise AssertionError("more filaments than tools must be refused")
    types = {"temperature": "N", "start_filament_gcode": "ABCD", "filament_type": "ABCD", "filament_colour": "ABCD"}
    keys = frozenset(["temperature", "start_filament_gcode", "filament_type", "bed_shape", "layer_height",
                      "nozzle_diameter", "filament_colour", "filament_vendor"])
    config = build_config(bundle, tools, "0.20mm QUALITY @TOOLS", chosen, ["#000000", "#FFFFFF"], types, keys)
    _expect(config["temperature"] == "220,240,215" and config["filament_type"] == '"PLA";"PETG";"PLA"'
            and config["start_filament_gcode"].count('"M572 S0.03 ; pressure advance"') == 3,
            "per-tool lists in PrusaSlicer's formats: {}".format(config))
    _expect(config["filament_colour"] == "#000000;#FFFFFF;#FF8000" and config["layer_height"] == "0.2"
            and config["filament_settings_id"] == '"Prusament PLA @TOOLS";"addnorth PETG Economy";"Generic PLA @TOOLS"'
            and config["printer_settings_id"] == tools and "printer_notes" not in config and config["filament_vendor"] == "Prusa Polymers",
            "colours, ids, only PrusaSlicer's settings, one value for a setting with no list type")


def _check_estimate_retries() -> None:
    global _CACHE_ROOT_OVERRIDE, _command_runner
    times = ["2147483647d 2147483647h 2147483647m 2147483647s", "1h 2m 3s"]

    class Result:
        def __init__(self, code: int) -> None:
            self.returncode, self.stdout, self.stderr = code, "", ""

    def runner(command: List[str], timeout: float, cwd: str) -> Result:
        out = Path(command[command.index("-o") + 1])
        out.write_text("G1 X1\n; filament used [g] = 1.50, 2.25\n; total filament used [g] = 3.75\n; total filament cost = 0.09\n"
                       "; estimated printing time (normal mode) = {}\n{}\n; temperature = 215\n{}\n".format(
                           times.pop(0) if times else "1h 2m 3s", CONFIG_BEGIN, CONFIG_END), encoding="utf-8")
        return Result(-11)  # PrusaSlicer 2.9.7 crashes now and then after writing the G-code.

    with tempfile.TemporaryDirectory(prefix="filamatch-prusa-test-") as temp:
        _CACHE_ROOT_OVERRIDE, _command_runner = Path(temp), runner
        try:
            estimate = estimate_print(Path(temp) / "project.3mf", "PrusaSlicer")
        finally:
            _CACHE_ROOT_OVERRIDE, _command_runner = None, None
    _expect(estimate == {"minutes": 62, "grams": 3.8, "plates": 1, "filament_grams": [1.5, 2.2], "cost": 0.09},
            "a broken time is sliced again; a crash after a complete G-code counts: {}".format(estimate))


def _check_infill_patterns() -> None:
    global _CACHE_ROOT_OVERRIDE, _command_runner
    calls: List[List[str]] = []

    def runner(command: List[str], timeout: float, cwd: str) -> Any:
        calls.append(command)
        help_text = (" --fill-density      Density of internal infill. (default: 15%)\n"
                     " --fill-pattern      Fill pattern for general low-density infill. (rectilinear, alignedrectilinear,\n"
                     "                     grid, gyroid,\n                     lightning, zigzag; default: stars)\n"
                     " --top-fill-pattern  Fill pattern for top infill. (rectilinear, monotonic; default: monotonic)\n")
        return type("Result", (), {"returncode": 0, "stdout": help_text, "stderr": ""})()

    with tempfile.TemporaryDirectory(prefix="filamatch-prusa-test-") as temp:
        _CACHE_ROOT_OVERRIDE, _command_runner = Path(temp), runner
        try:
            first = infill_patterns("PrusaSlicer")
            again = infill_patterns("PrusaSlicer")
        finally:
            _CACHE_ROOT_OVERRIDE, _command_runner = None, None
    _expect(first == ["rectilinear", "alignedrectilinear", "grid", "gyroid", "lightning", "zigzag"] and again == first
            and len(calls) == 1, "the types come from --fill-pattern, once per version: {} {}".format(first, calls))


def _selftest() -> int:
    failures = 0
    for label, check in (
        ("the infill types come from PrusaSlicer's help", _check_infill_patterns),
        ("bundles, inheritance and compatibility conditions", _check_bundle_and_conditions),
        ("vendors, printers and prints that fit", _check_listing),
        ("filament choice and the full configuration", _check_filaments_and_config),
        ("the estimate slices again when PrusaSlicer's time is broken", _check_estimate_retries),
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
        print("{} prusa_profiles self-test(s) FAILED".format(failures))
        return 1
    print("PASS: all prusa_profiles self-tests completed")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
