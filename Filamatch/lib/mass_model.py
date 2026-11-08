# Moved from Printegrity 05a4847 Printegrity/lib/mass_model.py (now part of Filamatch's As printed tab)
"""The mass of a body as it will be FDM-printed (docs/PLAN.md, "Mass first").

Pure Python, no Fusion: the add-in passes in the body's volume, its
triangle mesh and the solid density of the filament.

The model, shell plus core:

- Every surface gets a solid skin. Its thickness, measured along the
  surface normal, is the larger of
  - the walls: ``wall_count * line_spacing`` measured across the layers,
    which is ``* sin(angle)`` along the normal. A printed line has round
    sides, so the slicers space wall loops at
    ``line_width - layer_height * (1 - pi/4)``, not at the full width; and
  - the top or bottom layers: ``layers * layer_height`` measured along the
    build direction, which is ``* cos(angle)`` along the normal,
  where ``angle`` is between the surface normal and the build direction.
  Faces that point up get the top layers, faces that point down the
  bottom layers.
- The skin cannot hold more than the whole body (thin parts print solid).
- The rest, the core, is filled to the infill percentage.

So ``mass = density * (shell + infill * (volume - shell))``. Edges and
corners are counted roughly. The slicer calibration (later) replaces this
estimate with the slicer's grams.

Self-test: ``python3 -m Filamatch.lib.mass_model``.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from . import body_settings

#: Infill types, by the slicers' own names, as Filamatch knows them (body_settings).
INFILL_PATTERNS: Tuple[str, ...] = tuple(body_settings.INFILL_PATTERNS)

#: Names earlier Printegrity versions saved, and the slicer name they mean now.
OLD_PATTERN_NAMES: Dict[str, str] = {"lines": "line"}

#: The axis that points up on the bed. The estimate uses the design's own
#: orientation (Z up), as the slicer check does; the others stay for the model.
BUILD_DIRECTIONS: Dict[str, Tuple[float, float, float]] = {
    "z_up": (0.0, 0.0, 1.0),
    "y_up": (0.0, 1.0, 0.0),
    "x_up": (1.0, 0.0, 0.0),
}

#: Bins of the slope histogram over cos(angle) from -1 (faces down) to 1 (faces up).
SLOPE_BINS = 40

#: Above this share of the volume, the part prints (nearly) solid.
NEARLY_SOLID_SHARE = 0.9


@dataclasses.dataclass(frozen=True)
class PrintSettings:
    """The print settings that change the mass. Lengths in mm."""

    nozzle_mm: float = 0.4
    layer_height_mm: float = 0.2
    #: 0 means a usual default: nozzle * 1.1 (0.44 for a 0.4 nozzle; Bambu Studio
    #: uses 0.42 for the outer wall and 0.45 for the inner walls).
    line_width_mm: float = 0.0
    wall_count: int = 2
    top_layers: int = 5
    bottom_layers: int = 3
    infill_percent: float = 15.0
    infill_pattern: str = "gyroid"
    build_direction: str = "z_up"
    #: The print process's wall line widths (0 = not given: line_width_mm or the default).
    outer_wall_width_mm: float = 0.0
    inner_wall_width_mm: float = 0.0
    #: The process's minimum top and bottom skin thickness: slicers add layers to reach it.
    top_min_mm: float = 0.0
    bottom_min_mm: float = 0.0

    @property
    def line_width(self) -> float:
        """The mean wall line width in mm: the process's outer and inner widths, else
        ``line_width_mm``, else the default (nozzle * 1.1)."""
        single = self.line_width_mm if self.line_width_mm > 0 else round(self.nozzle_mm * 1.1, 3)
        outer = self.outer_wall_width_mm or single
        inner = self.inner_wall_width_mm or single
        if self.wall_count <= 1:
            return outer
        return (outer + inner * (self.wall_count - 1)) / self.wall_count

    @property
    def top_layers_used(self) -> int:
        """Top layers after the slicer adds layers for the minimum thickness."""
        return max(self.top_layers, math.ceil(self.top_min_mm / self.layer_height_mm - 1e-9))

    @property
    def bottom_layers_used(self) -> int:
        """Bottom layers after the slicer adds layers for the minimum thickness."""
        return max(self.bottom_layers, math.ceil(self.bottom_min_mm / self.layer_height_mm - 1e-9))

    @property
    def line_spacing(self) -> float:
        """The distance (mm) between the centres of two wall loops."""
        return self.line_width - self.layer_height_mm * (1.0 - math.pi / 4.0)

    def problems(self) -> List[str]:
        """Plain reasons why these settings cannot be printed (empty when they can)."""
        found = []
        if not 0.1 <= self.nozzle_mm <= 2.0:
            found.append("The nozzle must be 0.1 to 2 mm.")
        if not 0.04 <= self.layer_height_mm <= 0.8 * self.nozzle_mm:
            found.append("The layer height must be 0.04 mm to 80% of the nozzle.")
        for width in (self.line_width_mm, self.outer_wall_width_mm, self.inner_wall_width_mm):
            if width and not 0.6 * self.nozzle_mm <= width <= 2.0 * self.nozzle_mm:
                found.append("The line width must be 60% to 200% of the nozzle.")
                break
        if not (0 <= self.top_min_mm <= 10 and 0 <= self.bottom_min_mm <= 10):
            found.append("The minimum top and bottom thickness must be 0 to 10 mm.")
        if not 0 <= self.wall_count <= 20:
            found.append("Walls must be 0 to 20.")
        if not (0 <= self.top_layers <= 50 and 0 <= self.bottom_layers <= 50):
            found.append("Top and bottom layers must be 0 to 50.")
        if not 0 <= self.infill_percent <= 100:
            found.append("Infill must be 0 to 100%.")
        if self.infill_pattern not in INFILL_PATTERNS:
            found.append("Unknown infill pattern.")
        if self.build_direction not in BUILD_DIRECTIONS:
            found.append("Unknown build direction.")
        return found

    def label(self) -> str:
        """A short name, for example ``0.4/0.2 · 2 walls · 5/3 layers · 15% gyroid``
        (nozzle/layer height, walls, top/bottom layers, infill)."""
        return "{:g}/{:g} · {} wall{} · {}/{} layers · {:g}% {}".format(
            self.nozzle_mm, self.layer_height_mm, self.wall_count, "" if self.wall_count == 1 else "s",
            self.top_layers, self.bottom_layers, self.infill_percent, self.infill_pattern,
        )

    def key(self) -> str:
        """Every setting, so two bodies with the same key are printed the same way."""
        return "|".join("{}={}".format(field.name, getattr(self, field.name)) for field in dataclasses.fields(self))

    def with_body(self, own: Mapping[str, Any]) -> "PrintSettings":
        """These settings with a body's own ones on top.

        Args:
            own: the body's settings as Filamatch keeps them (body_settings keys:
                infill, infill_pattern, walls, top_layers, bottom_layers).
        """
        values: Dict[str, Any] = {}
        names = {"infill": "infill_percent", "infill_pattern": "infill_pattern", "walls": "wall_count",
                 "top_layers": "top_layers", "bottom_layers": "bottom_layers"}
        for key, value in body_settings._readable(dict(own or {})).items():
            values[names[key]] = float(value) if key == "infill" else value
        return dataclasses.replace(self, **values)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_process(cls, values: Mapping[str, Any], nozzle: Any = None) -> "PrintSettings":
        """Settings from a print process as Filamatch lists it.

        ``values`` holds body_settings keys (infill, infill_pattern, walls,
        top_layers, bottom_layers) and layer_height, outer_wall_width,
        inner_wall_width (mm, or the slicer's "105%" of the nozzle),
        top_min_mm and bottom_min_mm. What is missing or does not fit keeps
        the built-in default, so the estimate works without a slicer too.
        """
        base = cls()
        try:
            nozzle_mm = float(nozzle) if nozzle not in (None, "") else base.nozzle_mm
        except (TypeError, ValueError):
            nozzle_mm = base.nozzle_mm
        if not 0.1 <= nozzle_mm <= 2.0:
            nozzle_mm = base.nozzle_mm
        settings = dataclasses.replace(base.with_body(values), nozzle_mm=nozzle_mm)
        extra: Dict[str, Any] = {}
        for key, field_name in (("layer_height", "layer_height_mm"), ("top_min_mm", "top_min_mm"),
                                ("bottom_min_mm", "bottom_min_mm")):
            number = _number(values.get(key))
            if number is not None and number >= 0:
                extra[field_name] = number
        for key, field_name in (("outer_wall_width", "outer_wall_width_mm"), ("inner_wall_width", "inner_wall_width_mm")):
            width = _width_mm(values.get(key), nozzle_mm)
            if width is not None:
                extra[field_name] = width
        candidate = dataclasses.replace(settings, **extra)
        return candidate if not candidate.problems() else settings

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PrintSettings":
        """Build settings from a dict; missing or unreadable values keep their defaults."""
        values: Dict[str, Any] = {}
        for field in dataclasses.fields(cls):
            if field.name not in data:
                continue
            raw = data[field.name]
            default = field.default
            try:
                if isinstance(default, bool) or isinstance(default, str):
                    values[field.name] = str(raw)
                    if field.name == "infill_pattern":
                        values[field.name] = OLD_PATTERN_NAMES.get(str(raw), str(raw))
                elif isinstance(default, int):
                    values[field.name] = int(float(raw))
                else:
                    value = float(raw)
                    if not math.isfinite(value):
                        continue
                    values[field.name] = value
            except (TypeError, ValueError):
                continue
        return cls(**values)


def _number(value: Any) -> Any:
    """A finite number from text or a number (None when there is none)."""
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _width_mm(value: Any, nozzle_mm: float) -> Any:
    """A slicer line width in mm: "0.42", 0.42 or "105%" of the nozzle; None for 0 or nothing."""
    text = str(value if value is not None else "").strip()
    if text.endswith("%"):
        number = _number(text[:-1])
        number = nozzle_mm * number / 100.0 if number is not None else None
    else:
        number = _number(text)
    return round(number, 4) if number is not None and number > 0 else None


@dataclasses.dataclass(frozen=True)
class MassEstimate:
    """The estimated printed mass of one body."""

    volume_cm3: float
    shell_cm3: float
    core_cm3: float
    printed_cm3: float
    grams: float
    solid_grams: float
    effective_density_g_cm3: float
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        data = dataclasses.asdict(self)
        data["notes"] = list(self.notes)
        return data


def slope_histogram(coordinates: Sequence[float], indices: Sequence[int],
                    build_direction: str = "z_up") -> List[Tuple[float, float]]:
    """Area (cm²) of a triangle mesh per slope bin.

    Args:
        coordinates: x, y, z of every node, in cm (``nodeCoordinatesAsDouble``).
        indices: three node indices per triangle (``nodeIndices``).
        build_direction: a key of :data:`BUILD_DIRECTIONS`.

    Returns:
        :data:`SLOPE_BINS` pairs ``(area, area * cos)``; bin ``i`` holds the
        triangles whose cos(angle to the build direction) lies in
        ``[-1 + 2i/SLOPE_BINS, -1 + 2(i+1)/SLOPE_BINS)``. The second value
        gives the bin's exact mean slope.
    """
    up_x, up_y, up_z = BUILD_DIRECTIONS[build_direction]
    areas = [0.0] * SLOPE_BINS
    weighted = [0.0] * SLOPE_BINS
    for start in range(0, len(indices) - 2, 3):
        a, b, c = indices[start] * 3, indices[start + 1] * 3, indices[start + 2] * 3
        ux = coordinates[b] - coordinates[a]
        uy = coordinates[b + 1] - coordinates[a + 1]
        uz = coordinates[b + 2] - coordinates[a + 2]
        vx = coordinates[c] - coordinates[a]
        vy = coordinates[c + 1] - coordinates[a + 1]
        vz = coordinates[c + 2] - coordinates[a + 2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length <= 0.0:
            continue
        cosine = (nx * up_x + ny * up_y + nz * up_z) / length
        index = min(SLOPE_BINS - 1, max(0, int((cosine + 1.0) / 2.0 * SLOPE_BINS)))
        areas[index] += length / 2.0
        weighted[index] += length / 2.0 * cosine
    return list(zip(areas, weighted))


def total_area(histogram: Sequence[Tuple[float, float]]) -> float:
    """The surface area (cm²) of a slope histogram."""
    return sum(area for area, _weighted in histogram)


def shell_volume(histogram: Sequence[Tuple[float, float]], settings: PrintSettings) -> float:
    """The volume (cm³) of the solid skin over a slope histogram."""
    wall = settings.wall_count * settings.line_spacing / 10.0
    top = settings.top_layers_used * settings.layer_height_mm / 10.0
    bottom = settings.bottom_layers_used * settings.layer_height_mm / 10.0
    total = 0.0
    for area, weighted in histogram:
        if area <= 0.0:
            continue
        cosine = max(-1.0, min(1.0, weighted / area))
        sine = math.sqrt(max(0.0, 1.0 - cosine * cosine))
        layers = top if cosine > 0 else bottom
        total += area * max(wall * sine, layers * abs(cosine))
    return total


def estimate(volume_cm3: float, histogram: Sequence[Tuple[float, float]], settings: PrintSettings,
             solid_density_g_cm3: float) -> MassEstimate:
    """Estimate the printed mass of one body.

    Args:
        volume_cm3: the body's volume.
        histogram: its surface area per slope bin (:func:`slope_histogram`).
        settings: the print settings.
        solid_density_g_cm3: the filament's density as a solid.
    """
    volume = max(0.0, float(volume_cm3))
    shell = min(shell_volume(histogram, settings), volume)
    core = volume - shell
    printed = shell + core * settings.infill_percent / 100.0
    grams = printed * solid_density_g_cm3
    notes = []
    if volume > 0 and shell >= NEARLY_SOLID_SHARE * volume and settings.infill_percent < 100:
        notes.append("Thin part: it prints nearly solid, so infill hardly changes its mass.")
    return MassEstimate(
        volume_cm3=volume,
        shell_cm3=shell,
        core_cm3=core,
        printed_cm3=printed,
        grams=grams,
        solid_grams=volume * solid_density_g_cm3,
        effective_density_g_cm3=grams / volume if volume > 0 else 0.0,
        notes=tuple(notes),
    )


def calibrated_density(volume_cm3: float, slicer_grams: float) -> float:
    """The density (g/cm³) that makes Fusion's mass equal the slicer's grams."""
    if volume_cm3 <= 0 or slicer_grams <= 0:
        raise ValueError("volume and grams must be positive")
    return slicer_grams / volume_cm3


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


def _box_mesh(dx: float, dy: float, dz: float) -> Tuple[List[float], List[int]]:
    """An axis-aligned box from the origin as 12 outward-facing triangles (cm)."""
    corners = [(x, y, z) for x in (0.0, dx) for y in (0.0, dy) for z in (0.0, dz)]
    coordinates = [value for corner in corners for value in corner]
    # Corner index = 4*x + 2*y + z (0 or 1 each); faces wound counter-clockwise from outside.
    quads = [(0, 2, 6, 4), (1, 5, 7, 3), (0, 4, 5, 1), (2, 3, 7, 6), (0, 1, 3, 2), (4, 6, 7, 5)]
    indices: List[int] = []
    for a, b, c, d in quads:
        indices += [a, b, c, a, c, d]
    return coordinates, indices


def _close(a: float, b: float, tolerance: float = 1e-9) -> bool:
    return abs(a - b) <= tolerance * max(1.0, abs(a), abs(b))


def _self_test() -> int:
    settings = PrintSettings()
    _check(settings.line_width == 0.44 and not settings.problems(), "defaults: 0.44 mm lines and no problems")
    _check(_close(settings.line_spacing, 0.44 - 0.2 * (1 - math.pi / 4)), "wall loops are spaced closer than the line width")
    _check(settings.label() == "0.4/0.2 · 2 walls · 5/3 layers · 15% gyroid",
           "the label names the settings, top and bottom layers too")
    _check(settings.key() != PrintSettings(top_layers=6).key() and settings.key() == PrintSettings().key(),
           "the key tells every setting apart")
    _check(PrintSettings.from_dict({"infill_pattern": "lines"}).infill_pattern == "line"
           and "zig-zag" in INFILL_PATTERNS and "line" in INFILL_PATTERNS,
           "infill types are the slicers' names; an old saved name is read")
    body = settings.with_body({"infill": 40, "walls": 4, "infill_pattern": "Grid", "junk": 1, "top_layers": "x"})
    _check(body == PrintSettings(infill_percent=40.0, wall_count=4, infill_pattern="grid")
           and settings.with_body({}) == settings,
           "a body's own settings go on top of the defaults; a value that does not fit is left out")
    _check(PrintSettings.from_dict(settings.to_dict()) == settings, "settings round-trip through a dict")
    _check(
        PrintSettings.from_dict({"wall_count": "3", "infill_percent": "abc", "nozzle_mm": "nan"})
        == PrintSettings(wall_count=3),
        "unreadable values keep their defaults",
    )
    bad = PrintSettings(layer_height_mm=0.4, infill_percent=120, infill_pattern="<stars>", build_direction="sideways")
    _check(len(bad.problems()) == 4, "impossible settings are reported in plain words")

    coordinates, indices = _box_mesh(2.0, 2.0, 2.0)
    histogram = slope_histogram(coordinates, indices)
    wall_cm = 2 * settings.line_spacing / 10
    _check(_close(total_area(histogram), 24.0), "a 20 mm cube has 24 cm² of surface")
    _check(
        _close(histogram[-1][0], 4.0) and _close(histogram[0][0], 4.0) and _close(histogram[SLOPE_BINS // 2][0], 16.0)
        and _close(histogram[SLOPE_BINS // 2][1], 0.0),
        "top, bottom and sides land in the up, down and vertical bins",
    )
    # A 45-degree roof: the skin is the larger of walls * sin and top layers * cos.
    roof = [(0.0, 0.0)] * SLOPE_BINS
    roof[int((math.cos(math.radians(45)) + 1) / 2 * SLOPE_BINS)] = (10.0, 10.0 * math.cos(math.radians(45)))
    _check(_close(shell_volume(roof, settings), 10.0 * max(wall_cm, 0.1) * math.sqrt(0.5)),
           "a sloped face uses its exact mean slope")
    # 20 mm PLA cube, defaults: walls 2 loops on 16 cm², top 5 x 0.02 cm and bottom 3 x 0.02 cm on 4 cm² each.
    expected_shell = 16 * wall_cm + 4 * 0.1 + 4 * 0.06
    result = estimate(8.0, histogram, settings, 1.24)
    _check(_close(result.shell_cm3, expected_shell), "cube shell by hand: {:.3f} cm³".format(expected_shell))
    expected_grams = (expected_shell + (8.0 - expected_shell) * 0.15) * 1.24
    _check(_close(result.grams, expected_grams) and _close(result.solid_grams, 9.92),
           "cube mass by hand: {:.2f} g of 9.92 g solid".format(expected_grams))
    _check(_close(result.effective_density_g_cm3, expected_grams / 8.0), "effective density is grams per cm³")

    sideways = slope_histogram(coordinates, indices, "x_up")
    _check(sideways == histogram, "a cube is the same in every build direction")
    flat = slope_histogram(*_box_mesh(4.0, 4.0, 0.5))
    upright = slope_histogram(*_box_mesh(4.0, 4.0, 0.5), "x_up")
    _check(
        estimate(8.0, flat, settings, 1.24).grams != estimate(8.0, upright, settings, 1.24).grams,
        "a plate lying flat and standing up get different masses",
    )

    solid = estimate(8.0, histogram, PrintSettings(infill_percent=100), 1.24)
    empty = estimate(8.0, histogram, PrintSettings(infill_percent=0, wall_count=0, top_layers=0, bottom_layers=0), 1.24)
    _check(_close(solid.grams, 9.92) and _close(empty.grams, 0.0), "100% infill is solid; no shell and no infill is empty")
    thin = estimate(0.2, slope_histogram(*_box_mesh(4.0, 4.0, 0.0125)), settings, 1.24)
    _check(_close(thin.shell_cm3, 0.2) and thin.notes, "a thin plate's shell is capped at its volume and flagged")
    more_walls = estimate(8.0, histogram, PrintSettings(wall_count=4), 1.24)
    more_infill = estimate(8.0, histogram, PrintSettings(infill_percent=40), 1.24)
    _check(more_walls.grams > result.grams and more_infill.grams > result.grams, "more walls or infill weigh more")

    process = PrintSettings.from_process(
        {"infill": 20, "infill_pattern": "grid", "walls": 3, "top_layers": 4, "bottom_layers": 3, "layer_height": "0.12",
         "outer_wall_width": "105%", "inner_wall_width": 0.45, "top_min_mm": 1.0, "bottom_min_mm": "0"}, nozzle="0.4")
    _check(process.outer_wall_width_mm == 0.42 and process.inner_wall_width_mm == 0.45 and process.layer_height_mm == 0.12
           and process.wall_count == 3 and process.infill_percent == 20.0 and process.infill_pattern == "grid",
           "a process's values are read; \"105%\" is 105% of the nozzle")
    _check(_close(process.line_width, (0.42 + 2 * 0.45) / 3) and process.top_layers_used == 9
           and process.bottom_layers_used == 3,
           "walls use the outer and inner widths; the minimum top thickness adds layers (1.0 mm / 0.12 mm = 9)")
    _check(PrintSettings.from_process({}) == PrintSettings()
           and PrintSettings.from_process({"layer_height": "abc", "outer_wall_width": "0"}, nozzle="x") == PrintSettings(),
           "without process values the built-in defaults are used")
    _check(PrintSettings.from_process({"outer_wall_width": 5.0}) == PrintSettings(),
           "a process value that cannot be printed is left out")
    thick = estimate(8.0, histogram, PrintSettings(top_min_mm=1.6), 1.24)
    _check(thick.shell_cm3 > result.shell_cm3, "a minimum top thickness adds skin")

    _check(_close(calibrated_density(8.0, 3.2), 0.4), "calibration: density = slicer grams / volume")
    try:
        calibrated_density(0.0, 3.0)
        refused = False
    except ValueError:
        refused = True
    _check(refused, "calibration refuses an empty body")

    print()
    if _FAILED:
        print("MASS MODEL SELF-TEST FAILED ({} passed, {} failed)".format(_PASSED, _FAILED))
        return 1
    print("MASS MODEL SELF-TEST PASSED ({} checks)".format(_PASSED))
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
