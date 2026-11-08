"""Fit a slicer project's settings to the printer: prime tower, nozzles, line widths.

Pure functions on the project-settings dictionary (``project_settings.config``)
that :mod:`slicer_profiles` applies after the slicer's command line has
prepared a project, or to the copy it slices for the estimate.  Most printer
quirks land here: the prime tower's real size and turn, one flow type per
nozzle, the nozzle data a headless slice needs, the side of each filament on
two-extruder printers, filament types an extruder cannot print, and the
bridge width on printers with mixed nozzle sizes.

No Fusion, network or file access; the checks are in
:mod:`slicer_profiles_selftest`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: Distance (mm) kept between the prime tower and the edge of the area every
#: nozzle can reach (covers the tower brim).
WIPE_TOWER_MARGIN_MM = 5.0

NOZZLE_LEFT = "left"
NOZZLE_RIGHT = "right"
# ``filament_map`` numbers the nozzles 1 = left, 2 = right (Bambu Studio 2.9.1,
# Bambu Lab H2D); OrcaSlicer reads the same keys.
_FILAMENT_MAP_VALUES = {NOZZLE_LEFT: "1", NOZZLE_RIGHT: "2"}
# The printer's own extruder ids (MQTT: 0 = right/main, 1 = left/deputy).
# ``physical_extruder_map`` turns a slicer extruder index into one of these.
_PRINTER_EXTRUDER_IDS = {NOZZLE_RIGHT: "0", NOZZLE_LEFT: "1"}


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


def _fit_bridge_line_width(machine: Mapping[str, Any], process: Dict[str, Any]) -> None:
    """Keep the bridge line width within the smallest nozzle (mixed nozzle sizes).

    OrcaSlicer 2.5 works out a percentage (its default is 100%) over the
    largest nozzle but checks it against the smallest one, so it refuses a
    printer with mixed nozzles: the Snapmaker U1 (0.4+0.6 nozzle) preset
    stops with "Bridge line width must not exceed nozzle diameter".  The width
    is then set to the smallest nozzle, in mm.
    """
    try:
        diameters = [float(value) for value in machine.get("nozzle_diameter") or []]
    except (TypeError, ValueError):
        return
    if len(set(diameters)) < 2:
        return
    value = str(process.get("bridge_line_width", "100%")).strip()
    try:
        width = max(diameters) * float(value[:-1]) / 100.0 if value.endswith("%") else float(value)
    except ValueError:
        return
    if width > min(diameters):
        process["bridge_line_width"] = "{:g}".format(min(diameters))
        logger.info("Bridge line width %s -> %g mm (nozzles %s).", value, min(diameters), diameters)
