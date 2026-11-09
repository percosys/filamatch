"""Estimate the stiffness and strength of a body as printed (docs/as-printed/strength-model.md).

Plain Python with no Fusion. Two parts:

1. **Base values** for printed solid test bars along the layers (XY) and
   across them (Z):

   - tier 2, the filament's own data sheet: when the body's filament is a
     product in ``data/datasheets/*.json`` (Filamatch's files; maker and
     product must match exactly), its values;
   - tier 1, generic per filament type: ``data/as_printed_materials.json``.
     Every number there is typed in by hand from a maker's data sheet, with
     its URL, access date, test standard, specimen and print settings. The
     generic value of a type is the median of its data sheets; the range is
     their lowest and highest value.
2. **Scaling** for the print settings, with the shell/core split of the mass
   model: walls and top/bottom skins are solid; the infill is a cellular
   core (Gibson-Ashby, ``E_core = E_solid · C · ρ^n``, ``σ_core = σ_solid ·
   C' · ρ^m``, ρ the infill fraction, n and m per pattern). The part's value
   mixes shell and core by volume:
   ``E = E_solid · (φ_shell + φ_core · C · ρ^n)``. The fits hold up to 75 %
   infill; from there the core goes in a straight line to solid at 100 %.

The Z values of the data sheets are for their own layer height (usually
0.2 mm with a 0.4 mm nozzle). Thicker layers bond less: the Z values are
scaled by ``k(r) / k(2)`` with ``k(r) = 1 − 0.82 · r^−1.5`` and ``r`` = nozzle
/ layer height (Kuznetsov et al. 2018, Polymers 10:313, PLA), never above 1.

Fusion Simulation uses one value for every direction, so a case is chosen:

- **safe** (the default): the low end of the Z values (across the layers,
  where printed parts break first);
- **typical**: the average of the XY and Z medians.

These are rough estimates (±25–40 %), never design values; the palette says
so.

Self-test: ``python3 -m Filamatch.lib.strength_model``.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
import statistics
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .. import config

DATA_FILE = config.ADDIN_DIR / "data" / "as_printed_materials.json"
DATASHEETS_DIR = config.ADDIN_DIR / "data" / "datasheets"

#: Tier 2 (one product's own data sheet): the model still dominates the error.
DATASHEET_UNCERTAINTY_PERCENT = 25
#: Poisson's ratio for a data-sheet product whose type has no tier-1 value.
DEFAULT_POISSON = 0.36
#: Data-sheet types left out: their test bars are not what a print is (ASA Aero foams; its bars did not).
DATASHEET_EXCLUDED_TYPES = ("ASA-AERO",)
#: Data-sheet units, as factors to MPa.
DATASHEET_UNITS = {"MPa": 1.0, "GPa": 1000.0}

CASES = ("safe", "typical")
DEFAULT_CASE = "safe"

#: The properties a data sheet can give (MPa), each along (xy) or across (z) the layers.
PROPERTIES = ("e_xy_mpa", "e_z_mpa", "uts_xy_mpa", "uts_z_mpa", "yield_xy_mpa", "yield_z_mpa")

#: What every source record must say (the rules for the data, docs/as-printed/plan.md).
SOURCE_FIELDS = ("maker", "product", "url", "accessed", "standard", "specimen", "print_settings", "values")


class StrengthDataError(ValueError):
    """The data file breaks a rule, said in plain words."""


@dataclasses.dataclass(frozen=True)
class Pattern:
    """How one infill pattern scales: ``C · ρ^n`` for stiffness, ``C' · ρ^m`` for strength."""

    stiffness_exponent: float
    strength_exponent: float
    stiffness_factor: float = 1.0
    strength_factor: float = 1.0
    source: str = ""


#: The pattern fits hold up to this infill fraction; above it the core goes linearly to solid.
FIT_MAX_DENSITY = 0.75


def core_share(factor: float, exponent: float, rho: float) -> float:
    """The core's share of the solid value: ``C · ρ^n``, then a straight line to 1 at ρ = 1."""
    if factor <= 0 or rho <= 0:
        return 0.0
    if rho <= FIT_MAX_DENSITY:
        return min(1.0, factor * rho ** exponent)
    at_max = min(1.0, factor * FIT_MAX_DENSITY ** exponent)
    return at_max + (1.0 - at_max) * (min(rho, 1.0) - FIT_MAX_DENSITY) / (1.0 - FIT_MAX_DENSITY)


#: The layer bond's nozzle/layer ratio law (Kuznetsov et al. 2018): k(r) = 1 − A · r^−1.5.
LAYER_BOND_A = 0.82
#: The ratio the data sheets' Z bars were printed at (0.4 mm nozzle, 0.2 mm layers).
LAYER_BOND_REFERENCE_RATIO = 2.0


def layer_bond_factor(nozzle_mm: float, layer_height_mm: float) -> float:
    """How much of the data sheets' Z strength these layers keep (≤ 1)."""
    if nozzle_mm <= 0 or layer_height_mm <= 0:
        return 1.0

    def bond(ratio: float) -> float:
        return max(0.0, 1.0 - LAYER_BOND_A * ratio ** -1.5)

    return min(1.0, bond(nozzle_mm / layer_height_mm) / bond(LAYER_BOND_REFERENCE_RATIO))


#: Infill patterns (the slicers' names, as mass_model.INFILL_PATTERNS). Filled
#: from the data file's "patterns"; DEFAULT_PATTERN for any other name.
DEFAULT_PATTERN = "default"


@dataclasses.dataclass(frozen=True)
class Range:
    """One property of a type over its data sheets."""

    median: float
    low: float
    high: float
    count: int


@dataclasses.dataclass(frozen=True)
class TypeData:
    """The base values of one filament type, or of one product's data sheet (printed solid)."""

    name: str
    values: Dict[str, Range]
    poisson: float
    uncertainty_percent: int
    #: "Bambu Lab PLA Basic (ISO 527)", one per data sheet.
    sources: Tuple[str, ...]
    #: How the card names the basis: "PLA (generic)" or "Bambu Lab PAHT-CF (data sheet V3.0)".
    label: str = ""
    #: "generic" (tier 1) or "datasheet" (tier 2).
    kind: str = "generic"
    #: Moist (conditioned) values of a nylon, used in the safe case.
    conditioned: Dict[str, Range] = dataclasses.field(default_factory=dict)
    notes: Tuple[str, ...] = ()

    def has(self, prop: str) -> bool:
        return prop in self.values


def _key(text: Any) -> str:
    """A name for exact matching: lower case letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


@dataclasses.dataclass(frozen=True)
class StrengthData:
    types: Dict[str, TypeData]
    aliases: Tuple[Tuple["re.Pattern[str]", str], ...]
    patterns: Dict[str, Pattern]
    version: str
    #: Tier 2: (the "maker + product" keys that name it, its values).
    products: Tuple[Tuple[Tuple[str, ...], TypeData], ...] = ()

    def basis(self, label: str) -> Optional[TypeData]:
        """A type's or a product's base values by their label ("PLA (generic)")."""
        for data in list(self.types.values()) + [product for _keys, product in self.products]:
            if data.label == label:
                return data
        return None

    def product_for(self, texts: List[str]) -> Optional[TypeData]:
        """The data sheet of a product named "<maker> <product>" (colour left off), matched exactly."""
        wanted = {_key(text) for text in texts if _key(text)}
        for keys, product in self.products:
            if wanted.intersection(keys):
                return product
        return None

    def match(self, filament_type: str) -> Optional[str]:
        """The data's type for a slicer filament type or material name (None when there is none)."""
        text = re.sub(r"[\s_]+", " ", str(filament_type or "")).strip().upper()
        if not text:
            return None
        for pattern, name in self.aliases:
            if pattern.search(text):
                return name
        return None

    def pattern(self, name: str) -> Pattern:
        return self.patterns.get(str(name or "").lower(), self.patterns[DEFAULT_PATTERN])


@dataclasses.dataclass(frozen=True)
class StrengthEstimate:
    """A body's isotropic values for Fusion (MPa), with where they came from."""

    type: str
    case: str
    #: "PLA (generic)" or "Bambu Lab PAHT-CF (data sheet V3.0)".
    basis: str
    #: "generic" or "datasheet".
    kind: str
    e_mpa: float
    poisson: float
    shear_mpa: float
    yield_mpa: float
    tensile_mpa: float
    uncertainty_percent: int
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        data = dataclasses.asdict(self)
        data["notes"] = list(self.notes)
        return data

    def key(self) -> str:
        """The values as Fusion gets them (rounded), so a copy can tell when they change."""
        return "{}|{}|{:.0f}|{:.3f}|{:.1f}|{:.1f}".format(self.basis, self.case, self.e_mpa, self.poisson,
                                                         self.yield_mpa, self.tensile_mpa)


# ---------------------------------------------------------------------------
# The data file
# ---------------------------------------------------------------------------

def _number(value: Any, where: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise StrengthDataError("{}: {!r} is not a positive number.".format(where, value))
    return float(value)


def parse(raw: Mapping[str, Any]) -> StrengthData:
    """Check the data file's rules and build the base values (median and range per property)."""
    if raw.get("schema") != 1:
        raise StrengthDataError("Unknown data file schema {!r}.".format(raw.get("schema")))
    types: Dict[str, TypeData] = {}
    aliases: List[Tuple["re.Pattern[str]", str]] = []
    for entry in raw.get("types") or []:
        name = str(entry.get("type") or "").strip()
        if not name or name in types:
            raise StrengthDataError("A type has no name, or its name is used twice: {!r}.".format(name))
        collected: Dict[str, List[float]] = {prop: [] for prop in PROPERTIES}
        labels = []
        for index, source in enumerate(entry.get("sources") or []):
            where = "{} source {}".format(name, index + 1)
            missing = [field for field in SOURCE_FIELDS if not source.get(field)]
            if missing:
                raise StrengthDataError("{} has no {}.".format(where, ", ".join(missing)))
            if str(source["specimen"]).split()[0].lower() != "printed":
                raise StrengthDataError("{}: only printed specimens are used (molded values are much higher).".format(where))
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(source["accessed"])):
                raise StrengthDataError("{}: the access date must be YYYY-MM-DD.".format(where))
            if not str(source["url"]).startswith("https://"):
                raise StrengthDataError("{}: the URL must start with https://.".format(where))
            unknown = set(source["values"]) - set(PROPERTIES)
            if unknown:
                raise StrengthDataError("{}: unknown values {}.".format(where, ", ".join(sorted(unknown))))
            for prop in PROPERTIES:
                number = _number(source["values"].get(prop), "{} {}".format(where, prop))
                if number is not None:
                    collected[prop].append(number)
            labels.append("{} {} ({})".format(source["maker"], source["product"], source["standard"]))
        values = {prop: Range(statistics.median(found), min(found), max(found), len(found))
                  for prop, found in collected.items() if found}
        for needed in ("e_xy_mpa", "uts_xy_mpa"):
            if needed not in values:
                raise StrengthDataError("{} has no {} from any source.".format(name, needed))
        poisson = entry.get("poisson") or {}
        nu = _number(poisson.get("value"), "{} Poisson's ratio".format(name))
        if nu is None or not 0.2 <= nu < 0.5 or not poisson.get("source"):
            raise StrengthDataError("{} needs a Poisson's ratio between 0.2 and 0.5, with its source.".format(name))
        uncertainty = int(entry.get("uncertainty_percent") or 0)
        if not 10 <= uncertainty <= 60:
            raise StrengthDataError("{}: uncertainty_percent must be 10–60.".format(name))
        notes = (str(entry["note"]),) if entry.get("note") else ()
        types[name] = TypeData(name=name, values=values, poisson=nu, uncertainty_percent=uncertainty,
                               sources=tuple(labels), label="{} (generic)".format(name), notes=notes)
        for alias in entry.get("match") or []:
            aliases.append((re.compile(str(alias)), name))
    patterns: Dict[str, Pattern] = {}
    for key, spec in (raw.get("patterns") or {}).items():
        if not spec.get("source"):
            raise StrengthDataError("The infill pattern {!r} needs its source.".format(key))
        pattern = Pattern(
            stiffness_exponent=float(spec["stiffness_exponent"]), strength_exponent=float(spec["strength_exponent"]),
            stiffness_factor=float(spec.get("stiffness_factor", 1.0)), strength_factor=float(spec.get("strength_factor", 1.0)),
            source=str(spec["source"]))
        for name in [key] + list(spec.get("also") or []):
            patterns[str(name).lower()] = pattern
    if DEFAULT_PATTERN not in patterns:
        raise StrengthDataError("The data file needs a \"default\" infill pattern.")
    return StrengthData(types=types, aliases=tuple(aliases), patterns=patterns, version=str(raw.get("version") or ""))


def _product_keys(maker: str, product: str, aliases: Tuple[str, ...] = ()) -> Tuple[str, ...]:
    """Every "maker + product" spelling of a data-sheet product, as :func:`_key` keys.

    "Polymaker ASA (PolyLite ASA)" is also "Polymaker PolyLite ASA" and
    "Polymaker ASA"; "Fiberon PA6-CF20" is also "Polymaker PA6-CF20" and
    "Fiberon PA6-CF20". A one-word note in brackets ("(Transparent)") is not
    a name. ``aliases`` are the record's other names of the product, without
    the maker ("PPS GF" for "Fiberon PPS-GF20").
    """
    names = {product}
    names.update(str(alias) for alias in aliases if str(alias).strip())
    bracket = re.search(r"\(([^)]*)\)", product)
    plain = re.sub(r"\s*\([^)]*\)", "", product).strip()
    names.add(plain)
    if bracket and len(bracket.group(1).split()) >= 2:
        names.add(bracket.group(1))
    for name in list(names):
        for prefix in (maker + " ", "Fiberon "):
            if name.lower().startswith(prefix.lower()):
                names.add(name[len(prefix):])
    makers = {maker}
    if product.lower().startswith("fiberon"):
        makers.add("Fiberon")
    return tuple(sorted({_key(m + n) for m in makers for n in names if _key(n)}))


def parse_datasheets(raws: List[Mapping[str, Any]], types: StrengthData) -> Tuple[Tuple[Tuple[str, ...], TypeData], ...]:
    """Tier 2: each data-sheet product with tensile values, as base values (see data/datasheets/README.md)."""
    products = []
    for raw in raws:
        if raw.get("schema") != 1:
            raise StrengthDataError("Unknown data sheet file schema {!r}.".format(raw.get("schema")))
        maker = str(raw.get("maker") or "").strip()
        for record in raw.get("filaments") or []:
            product = str(record.get("product") or "").strip()
            kind = str(record.get("type") or "").strip()
            url = str(record.get("datasheet_url") or "")
            if not maker or not product or kind in DATASHEET_EXCLUDED_TYPES or not url.startswith("https://"):
                continue
            found: Dict[str, Dict[str, float]] = {"": {}, "dry": {}, "conditioned": {}}
            standard = ""
            for prop in record.get("properties") or []:
                name = {"tensile_modulus": "e", "tensile_strength": "uts", "yield_strength": "uts"}.get(prop.get("property"))
                direction = {"XY": "xy", "Z": "z"}.get(prop.get("direction"))
                factor = DATASHEET_UNITS.get(prop.get("unit"))
                if not name or not direction or factor is None:
                    continue
                value = _number(prop.get("value"), "{} {}".format(product, prop.get("property"))) * factor
                slot = found.setdefault(str(prop.get("condition") or ""), {})
                key = "{}_{}_mpa".format(name, direction)
                slot[key] = max(slot.get(key, 0.0), value)  # strength: the highest stress (yield or break)
                standard = standard or str(prop.get("standard") or "")
            dry = dict(found[""], **found["dry"])
            if "e_xy_mpa" not in dry or "uts_xy_mpa" not in dry:
                continue
            generic = types.types.get(types.match(kind) or "")
            notes = []
            if found["conditioned"]:
                notes.append("Safe uses the sheet's moist (conditioned) values; typical the dry ones.")
            elif kind.startswith(("PA", "PPA")):
                notes.append("Dry values: moist nylon is weaker.")
            version = re.sub(r"\s*\((.*)\)$", r", \1", str(record.get("datasheet_version") or ""))
            named = product if product.lower().startswith(maker.lower()) else "{} {}".format(maker, product)
            label = "{} (data sheet{})".format(named, " " + version if version else "")
            data = TypeData(
                name=kind, values={k: Range(v, v, v, 1) for k, v in dry.items()},
                poisson=generic.poisson if generic is not None else DEFAULT_POISSON,
                uncertainty_percent=DATASHEET_UNCERTAINTY_PERCENT,
                sources=("{} {}{}".format(named, version + " " if version else "", "({})".format(standard) if standard else "").strip(),),
                label=label, kind="datasheet",
                conditioned={k: Range(v, v, v, 1) for k, v in found["conditioned"].items()},
                notes=tuple(notes))
            aliases = tuple(str(alias) for alias in record.get("aliases") or [])
            products.append((_product_keys(maker, product, aliases), data))
    return tuple(products)


#: Flexible filaments: a linear simulation does not fit them, so they get no stiffness and strength.
FLEXIBLE_PATTERN = re.compile(r"\b(TPU|TPE|TPC|PEBA|FLEX)", re.IGNORECASE)


def is_flexible(*texts: str) -> bool:
    """True when a type or name says the filament is flexible (TPU, TPE, ...)."""
    return any(FLEXIBLE_PATTERN.search(str(text or "")) for text in texts)


#: A filament without printed data: its own Fusion values (often molded), scaled for the infill.
MATERIAL_UNCERTAINTY_PERCENT = 60


def material_basis(name: str, values: Mapping[str, float]) -> TypeData:
    """Base values from a material's own Fusion values (MPa; StrengthEstimate keys), one value for every direction.

    For a filament with neither a data sheet nor a type: better than its
    solid values with a printed density. Marked as not printed data.
    """
    single = {}
    for prop, key in (("e", "e_mpa"), ("uts", "tensile_mpa"), ("yield", "yield_mpa")):
        if values.get(key):
            for direction in ("xy", "z"):
                number = float(values[key])
                single["{}_{}_mpa".format(prop, direction)] = Range(number, number, number, 1)
    nu = float(values.get("poisson") or 0.0)
    return TypeData(
        name=name, values=single, poisson=nu if 0.2 <= nu < 0.5 else DEFAULT_POISSON,
        uncertainty_percent=MATERIAL_UNCERTAINTY_PERCENT, sources=("{} (Fusion material)".format(name),),
        label="{} (its own Fusion values)".format(name), kind="material",
        notes=("No printed test data for this material: its own Fusion values (often for molded parts, "
               "stronger than printed), scaled for the infill.",))


_CACHE: Dict[str, StrengthData] = {}


def load(path: Path = DATA_FILE, datasheets: Optional[Path] = DATASHEETS_DIR) -> StrengthData:
    """The bundled data, with Filamatch's data sheets as tier 2 (read once)."""
    key = "{}|{}".format(path, datasheets)
    if key not in _CACHE:
        data = parse(json.loads(Path(path).read_text(encoding="utf-8")))
        if datasheets is not None and Path(datasheets).is_dir():
            raws = [json.loads(file.read_text(encoding="utf-8")) for file in sorted(Path(datasheets).glob("*.json"))]
            data = dataclasses.replace(data, products=parse_datasheets(raws, data))
        _CACHE[key] = data
    return _CACHE[key]


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

def base_values(data: TypeData, case: str, bond: float = 1.0) -> Tuple[float, float, float, List[str]]:
    """``(E, yield, tensile)`` of printed solid for a case (MPa), and notes.

    ``bond`` (:func:`layer_bond_factor`) scales the Z strength values. The
    safe case uses a nylon's moist (conditioned) values where its sheet has them.
    """
    notes: List[str] = list(data.notes)
    values = dict(data.values, **data.conditioned) if case == "safe" else data.values

    def pick(xy: str, z: str) -> float:
        z_factor = 1.0 if z.startswith("e_") else bond
        if case == "typical":
            return (values[xy].median + values[z].median * z_factor) / 2.0 if z in values else values[xy].median
        return values[z].low * z_factor if z in values else values[xy].low

    if case == "safe" and "uts_z_mpa" not in values:
        notes.append("No layer-bond (Z) data for {}: across the layers it can be much weaker.".format(data.name))
    e = pick("e_xy_mpa", "e_z_mpa")
    tensile = pick("uts_xy_mpa", "uts_z_mpa")
    if "yield_xy_mpa" in values:
        yield_ = min(pick("yield_xy_mpa", "yield_z_mpa"), tensile)
    else:
        yield_ = tensile
        notes.append("Yield is taken as the tensile strength (the highest stress of the printed bars).")
    return e, yield_, tensile, notes


def estimate(data: StrengthData, basis: Any, case: str, volume_cm3: float, shell_cm3: float,
             infill_percent: float, infill_pattern: str, nozzle_mm: float = 0.4,
             layer_height_mm: float = 0.2) -> StrengthEstimate:
    """A body's isotropic values for Fusion, from its basis (a type name, or a
    data-sheet product's :class:`TypeData`), shell/core split, infill and layers."""
    if case not in CASES:
        raise ValueError("Unknown case {!r}.".format(case))
    base = basis if isinstance(basis, TypeData) else data.types[basis]
    bond = layer_bond_factor(nozzle_mm, layer_height_mm)
    e_solid, yield_solid, tensile_solid, notes = base_values(base, case, bond)
    if bond < 0.995:
        notes.append("Layers this thick for the nozzle bond less: {:.0f}% of the data sheets' layer-bond "
                     "strength.".format(bond * 100))
    volume = max(float(volume_cm3), 0.0)
    shell_share = min(max(float(shell_cm3) / volume, 0.0), 1.0) if volume > 0 else 1.0
    rho = min(max(float(infill_percent) / 100.0, 0.0), 1.0)
    pattern = data.pattern(infill_pattern)
    stiffness_core = core_share(pattern.stiffness_factor, pattern.stiffness_exponent, rho)
    strength_core = core_share(pattern.strength_factor, pattern.strength_exponent, rho)
    stiffness_share = shell_share + (1.0 - shell_share) * stiffness_core
    strength_share = shell_share + (1.0 - shell_share) * strength_core
    e = e_solid * stiffness_share
    nu = base.poisson
    return StrengthEstimate(
        type=base.name, case=case, basis=base.label or base.name, kind=base.kind,
        e_mpa=e, poisson=nu, shear_mpa=e / (2.0 * (1.0 + nu)),
        yield_mpa=yield_solid * strength_share, tensile_mpa=tensile_solid * strength_share,
        uncertainty_percent=base.uncertainty_percent, notes=tuple(notes))


# ---------------------------------------------------------------------------
# Self-test (no Fusion)
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


def _source(maker: str, **values: float) -> Dict[str, Any]:
    return {"maker": maker, "product": "Test", "url": "https://example.com/tds.pdf", "accessed": "2026-11-08",
            "standard": "ISO 527", "specimen": "printed bar", "print_settings": "0.2 mm layers, 100% infill",
            "values": values}


def _fixture() -> Dict[str, Any]:
    return {
        "schema": 1, "version": "test",
        "patterns": {"default": {"stiffness_exponent": 2.0, "strength_exponent": 1.5, "source": "test"},
                     "triangles": {"stiffness_exponent": 1.0, "strength_exponent": 1.0, "source": "test",
                                   "also": ["tri-hexagon"]},
                     "lightning": {"stiffness_exponent": 1.0, "strength_exponent": 1.0, "stiffness_factor": 0.0,
                                   "strength_factor": 0.0, "source": "test"}},
        "types": [
            {"type": "PLA", "match": ["^PLA(?!.*CF)"], "uncertainty_percent": 30,
             "poisson": {"value": 0.36, "source": "test"},
             "sources": [_source("A", e_xy_mpa=2700, e_z_mpa=2500, uts_xy_mpa=50, uts_z_mpa=30),
                         _source("B", e_xy_mpa=3000, e_z_mpa=2700, uts_xy_mpa=46, uts_z_mpa=26),
                         _source("C", e_xy_mpa=2400, uts_xy_mpa=40)]},
            {"type": "PLA-CF", "match": ["^PLA.*CF"], "uncertainty_percent": 40,
             "poisson": {"value": 0.35, "source": "test"},
             "sources": [_source("A", e_xy_mpa=4000, uts_xy_mpa=40, yield_xy_mpa=38)]},
        ],
    }


def _self_test() -> int:
    data = parse(_fixture())
    pla = data.types["PLA"]
    _check(pla.values["e_xy_mpa"] == Range(2700.0, 2400.0, 3000.0, 3) and pla.values["uts_z_mpa"].low == 26.0
           and pla.values["e_z_mpa"].count == 2 and len(pla.sources) == 3,
           "a type's value is the median of its data sheets, with their range")
    _check(data.match("PLA") == "PLA" and data.match("pla basic") == "PLA" and data.match("PLA-CF") == "PLA-CF"
           and data.match("PLA CF") == "PLA-CF" and data.match("PETG") is None and data.match("") is None,
           "slicer types and material names find their data type")
    e, yield_, tensile, notes = base_values(pla, "safe")
    _check(e == 2500.0 and tensile == 26.0 and yield_ == 26.0 and any("Yield" in note for note in notes),
           "safe: the low end of the Z values; without yield data, yield is the tensile strength")
    e, _yield, tensile, _notes = base_values(pla, "typical")
    _check(e == (2700.0 + 2600.0) / 2 and tensile == (46.0 + 28.0) / 2, "typical: the average of the XY and Z medians")
    _e, yield_, tensile, notes = base_values(data.types["PLA-CF"], "safe")
    _check(tensile == 40.0 and yield_ == 38.0 and any("No layer-bond" in note for note in notes),
           "without Z data, safe uses the XY low end and says so")

    solid = estimate(data, "PLA", "safe", 10.0, 10.0, 15.0, "gyroid")
    _check(solid.e_mpa == 2500.0 and solid.tensile_mpa == 26.0 and abs(solid.shear_mpa - 2500 / (2 * 1.36)) < 1e-9,
           "a part that is all shell is the solid value; G = E / 2(1 + ν)")
    half = estimate(data, "PLA", "safe", 10.0, 5.0, 20.0, "gyroid")
    _check(abs(half.e_mpa - 2500.0 * (0.5 + 0.5 * 0.2 ** 2)) < 1e-9
           and abs(half.tensile_mpa - 26.0 * (0.5 + 0.5 * 0.2 ** 1.5)) < 1e-9,
           "shell and core mix by volume; the core scales as ρ^n (stiffness) and ρ^m (strength)")
    tri = estimate(data, "PLA", "safe", 10.0, 5.0, 20.0, "triangles")
    _check(tri.e_mpa > half.e_mpa and abs(tri.e_mpa - 2500.0 * 0.6) < 1e-9
           and estimate(data, "PLA", "safe", 10.0, 5.0, 20.0, "Tri-hexagon").e_mpa == tri.e_mpa
           and estimate(data, "PLA", "safe", 10.0, 5.0, 20.0, "lightning").e_mpa == 2500.0 * 0.5,
           "each pattern has its own exponents (other names share them; lightning's core carries nothing)")
    _check(layer_bond_factor(0.4, 0.2) == 1.0 and layer_bond_factor(0.4, 0.12) == 1.0
           and abs(layer_bond_factor(0.4, 0.28) - (1 - 0.82 * (0.4 / 0.28) ** -1.5) / (1 - 0.82 * 2 ** -1.5)) < 1e-12
           and layer_bond_factor(0.4, 0.28) < 0.8,
           "thicker layers lower the layer bond (Kuznetsov 2018); thinner ones never raise it above the data sheets")
    thick = estimate(data, "PLA", "safe", 10.0, 10.0, 15.0, "gyroid", 0.4, 0.28)
    _check(abs(thick.tensile_mpa - 26.0 * layer_bond_factor(0.4, 0.28)) < 1e-9 and thick.e_mpa == 2500.0
           and any("bond less" in note for note in thick.notes),
           "the layer bond lowers the Z strength, not the stiffness, and says so")
    full = estimate(data, "PLA", "safe", 10.0, 2.0, 100.0, "gyroid")
    _check(full.e_mpa == 2500.0 and full.tensile_mpa == 26.0 and core_share(0.3, 1.5, 0.75) == 0.3 * 0.75 ** 1.5
           and abs(core_share(0.3, 1.5, 0.875) - (0.3 * 0.75 ** 1.5 + 1.0) / 2) < 1e-12 and core_share(0.0, 1.0, 1.0) == 0.0,
           "100% infill is solid: above 75% the core goes in a straight line to solid")
    hollow = estimate(data, "PLA", "typical", 10.0, 2.0, 0.0, "gyroid")
    _check(abs(hollow.e_mpa - (2700.0 + 2600.0) / 2 * 0.2) < 1e-9 and hollow.case == "typical"
           and hollow.uncertainty_percent == 30,
           "no infill: only the shell carries")
    _check(solid.key() != half.key() and solid.key() == estimate(data, "PLA", "safe", 10.0, 10.0, 15.0, "gyroid").key(),
           "the key tells different values apart")

    for broken, words in (
            ({"specimen": "injection molded bar"}, "only printed"),
            ({"url": "http://example.com"}, "https"),
            ({"accessed": "Nov 2026"}, "YYYY-MM-DD"),
            ({"standard": ""}, "no standard"),
            ({"values": {"e_xy_mpa": -1}}, "positive"),
    ):
        raw = _fixture()
        raw["types"][0]["sources"][0].update(broken)
        try:
            parse(raw)
            message = ""
        except StrengthDataError as exc:
            message = str(exc)
        _check(words in message, "the data file refuses a source with {}".format(", ".join(broken)))

    def prop(name: str, direction: str, value: float, unit: str = "MPa", condition: str = "") -> Dict[str, Any]:
        found = {"property": name, "direction": direction, "value": value, "unit": unit, "standard": "ISO 527"}
        if condition:
            found["condition"] = condition
        return found

    sheets = [{"schema": 1, "maker": "Polymaker", "filaments": [
        {"product": "Fiberon PA6-CF20", "type": "PA-CF", "datasheet_url": "https://example.com/a.pdf", "datasheet_version": "V1.1",
         "aliases": ["PA6 CF20 dry"],
         "properties": [prop("tensile_modulus", "XY", 8.6, "GPa", "dry"), prop("tensile_modulus", "Z", 3760, "MPa", "dry"),
                        prop("tensile_strength", "XY", 109, "MPa", "dry"), prop("tensile_strength", "Z", 54, "MPa", "dry"),
                        prop("tensile_modulus", "XY", 2508, "MPa", "conditioned"),
                        prop("tensile_strength", "Z", 25.5, "MPa", "conditioned")]},
        {"product": "Polymaker ASA (PolyLite ASA)", "type": "ASA", "datasheet_url": "https://example.com/b.pdf",
         "datasheet_version": "V6.0 (2025-12-02)",
         "properties": [prop("tensile_modulus", "XY", 2379), prop("tensile_strength", "XY", 43.8),
                        prop("yield_strength", "XY", 45.0)]},
        {"product": "ASA Aero", "type": "ASA-AERO", "datasheet_url": "https://example.com/c.pdf",
         "properties": [prop("tensile_modulus", "XY", 2010), prop("tensile_strength", "XY", 32)]},
        {"product": "No sheet", "type": "PLA", "datasheet_url": "", "properties": []},
    ]}]
    with_sheets = dataclasses.replace(data, products=parse_datasheets(sheets, data))
    nylon = with_sheets.product_for(["Polymaker Fiberon PA6-CF20", "something else"])
    _check(len(with_sheets.products) == 2 and nylon is not None and nylon.values["e_xy_mpa"].median == 8600.0
           and nylon.conditioned["uts_z_mpa"].median == 25.5 and nylon.kind == "datasheet"
           and nylon.uncertainty_percent == DATASHEET_UNCERTAINTY_PERCENT and nylon.poisson == DEFAULT_POISSON
           and with_sheets.product_for(["Polymaker PA6-CF20"]) is nylon and with_sheets.product_for(["Fiberon PA6-CF20"]) is nylon
           and with_sheets.product_for(["Polymaker PA6 CF20 dry"]) is nylon and with_sheets.product_for(["PA6 CF20 dry"]) is None,
           "data sheets load (GPa in MPa, dry and moist); a Fiberon product is found as Polymaker or Fiberon; "
           "ASA Aero and sheets without values are left out")
    asa = with_sheets.product_for(["Polymaker PolyLite ASA"])
    _check(asa is not None and asa.label == "Polymaker ASA (PolyLite ASA) (data sheet V6.0, 2025-12-02)"
           and asa.values["uts_xy_mpa"].median == 45.0 and with_sheets.product_for(["Polymaker ASA"]) is asa
           and with_sheets.product_for(["Polymaker ASA-CF08"]) is None and with_sheets.basis(asa.label) is asa,
           "a product is found by any of its names, only exactly; strength is the highest stress (yield here)")
    e, _yield, tensile, notes = base_values(nylon, "safe")
    e_typ, _yield, tensile_typ, _notes = base_values(nylon, "typical")
    _check(e == 3760.0 and tensile == 25.5 and e_typ == (8600.0 + 3760.0) / 2 and tensile_typ == (109.0 + 54.0) / 2
           and any("moist" in note for note in notes),
           "safe uses a nylon's moist values where the sheet gives them; typical the dry ones")
    _check(estimate(with_sheets, nylon, "safe", 10.0, 10.0, 15.0, "gyroid").basis == nylon.label
           and estimate(data, "PLA", "safe", 10.0, 10.0, 15.0, "gyroid").basis == "PLA (generic)",
           "an estimate names its basis")

    own = material_basis("Nylon 6/6", {"e_mpa": 2930.0, "tensile_mpa": 82.75, "yield_mpa": 82.75, "poisson": 0.6})
    scaled = estimate(data, own, "safe", 10.0, 5.0, 20.0, "gyroid")
    _check(own.kind == "material" and own.poisson == DEFAULT_POISSON and own.uncertainty_percent == 60
           and abs(scaled.e_mpa - 2930.0 * (0.5 + 0.5 * 0.2 ** 2)) < 1e-9 and scaled.basis == "Nylon 6/6 (its own Fusion values)"
           and any("No printed test data" in note for note in scaled.notes),
           "a material without printed data: its own Fusion values, scaled for the infill, and marked so")

    _check(is_flexible("TPU 95A") and is_flexible("", "Bambu Lab TPU 95A HF - Grey") and is_flexible("PolyFlex TPU95")
           and not is_flexible("PLA", "PETG HF") and data.match("ABS-GF30") is None,
           "flexible filaments are told apart; a number after GF/CF still keeps a filled type out")

    real = load()
    _check(all(real.types[name].values["uts_xy_mpa"].count >= 1 for name in real.types) and real.types,
           "the bundled data file follows the rules ({} types)".format(len(real.types)))
    _check(len(real.products) >= 30 and real.product_for(["Bambu Lab PAHT-CF"]) is not None
           and real.product_for(["Bambu Lab ASA Aero"]) is None
           and real.product_for(["Polymaker PPS GF"]) is real.product_for(["Polymaker Fiberon PPS-GF20"]) is not None
           and all(real.product_for(["Polymaker " + name]) is None for name in ("PA12 CF", "PA6 CF", "PA6 GF", "PC", "PETG CF"))
           and real.match("Generic - ABS-GF30 - Black") is None and real.match("PLA-GF10") is None
           and real.match("PETG-CF10") == "PETG-CF" and real.match("ABS") == "ABS",
           "Filamatch's data sheets load as tier 2 ({} products)".format(len(real.products)))

    print()
    if _FAILED:
        print("STRENGTH MODEL SELF-TEST FAILED ({} passed, {} failed)".format(_PASSED, _FAILED))
        return 1
    print("STRENGTH MODEL SELF-TEST PASSED ({} checks)".format(_PASSED))
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
