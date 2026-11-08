#!/usr/bin/env python3
"""Compare the As printed mass model with the slicer check's grams.

Usage (from the repo root; needs Bambu Studio in /Applications):

    python3 scripts/mass_vs_slicer.py               # writes dist/mass_vs_slicer/report.md
    python3 scripts/mass_vs_slicer.py --out DIR

It slices simple test shapes with the add-in's own slicer check
(``Filamatch/lib/slicer_check.py``: Filamatch's project preparation for
Bambu Studio's X1C 0.4 nozzle and 0.20mm Standard presets, then the body's
own grams without the start routine's purge) for a few print settings, and
puts the mass model's grams (``Filamatch/lib/mass_model.py``, with the
palette's default line width) next to them. Nothing else is changed. It
takes about a minute.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Filamatch.lib import mass_model, slicer_check, slicer_profiles  # noqa: E402

VENDOR = "bambu_local"
MACHINE = "Bambu Lab X1 Carbon 0.4 nozzle"
PROCESS = "0.20mm Standard @BBL X1C"
MATERIAL = "Bambu PLA Basic"
DENSITY = 1.26

Mesh = Tuple[List[Tuple[float, float, float]], List[int]]


# ---------------------------------------------------------------------------
# Test shapes (mm)
# ---------------------------------------------------------------------------


def box(dx: float, dy: float, dz: float) -> Mesh:
    coordinates, indices = mass_model._box_mesh(dx, dy, dz)
    nodes = [tuple(coordinates[i * 3:i * 3 + 3]) for i in range(len(coordinates) // 3)]
    return nodes, indices  # type: ignore[return-value]


def cylinder(radius: float, height: float, segments: int = 96) -> Mesh:
    nodes = [(0.0, 0.0, 0.0), (0.0, 0.0, height)]
    for k in range(segments):
        angle = 2 * math.pi * k / segments
        x, y = radius * math.cos(angle), radius * math.sin(angle)
        nodes += [(x, y, 0.0), (x, y, height)]
    indices: List[int] = []
    for k in range(segments):
        b0, t0 = 2 + 2 * k, 3 + 2 * k
        b1, t1 = 2 + 2 * ((k + 1) % segments), 3 + 2 * ((k + 1) % segments)
        indices += [0, b1, b0, 1, t0, t1, b0, b1, t1, b0, t1, t0]
    return nodes, indices


def wedge(length: float, width: float, height: float) -> Mesh:
    """A ramp (right-triangle prism): 45 degrees when length equals height."""
    nodes = [(0.0, 0.0, 0.0), (length, 0.0, 0.0), (0.0, 0.0, height),
             (0.0, width, 0.0), (length, width, 0.0), (0.0, width, height)]
    indices = [0, 2, 1, 3, 4, 5, 0, 1, 4, 0, 4, 3, 0, 3, 5, 0, 5, 2, 1, 2, 5, 1, 5, 4]
    return outward(nodes, indices)


def volume_mm3(nodes: Sequence[Tuple[float, float, float]], indices: Sequence[int]) -> float:
    """Signed volume: positive when the triangles face outward."""
    total = 0.0
    for t in range(0, len(indices), 3):
        a, b, c = (nodes[indices[t + k]] for k in range(3))
        total += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
                  + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6.0
    return total


def outward(nodes: List[Tuple[float, float, float]], indices: List[int]) -> Mesh:
    """Flip every triangle when the mesh faces inward (negative volume)."""
    if volume_mm3(nodes, indices) >= 0:
        return nodes, indices
    flipped: List[int] = []
    for t in range(0, len(indices), 3):
        flipped += [indices[t], indices[t + 2], indices[t + 1]]
    return nodes, flipped


SHAPES: Dict[str, Mesh] = {
    "cube 20": box(20, 20, 20),
    "plate 40x40x5 flat": box(40, 40, 5),
    "plate 40x40x5 upright": box(5, 40, 40),
    "cylinder r10 h20": cylinder(10, 20),
    "wedge 30x20x30 (45 deg)": wedge(30, 20, 30),
    "block 60x30x15": box(60, 30, 15),
}

#: Print settings per case (the palette's defaults otherwise: 0.4/0.2, top 5, bottom 3).
SETTINGS: Dict[str, mass_model.PrintSettings] = {
    "2 walls, 15% gyroid": mass_model.PrintSettings(),
    "4 walls, 15% gyroid": mass_model.PrintSettings(wall_count=4),
    "2 walls, 40% grid": mass_model.PrintSettings(infill_percent=40, infill_pattern="grid"),
}


def body_mesh(name: str, mesh: Mesh) -> slicer_check.BodyMesh:
    nodes, indices = mesh
    return slicer_check.BodyMesh(
        name=name, path="", coordinates=tuple(v / 10.0 for node in nodes for v in node), indices=tuple(indices),
        volume_cm3=volume_mm3(nodes, indices) / 1000.0, density_g_cm3=DENSITY, material=MATERIAL,
    )


def target() -> Tuple[str, slicer_profiles.ProfileTarget]:
    """Bambu Studio and its X1C presets, found the way the add-in finds them."""
    bambu = next((item.path for item in slicer_profiles.find_installed_slicers() if item.kind == "bambu"), "")
    if not bambu:
        raise SystemExit("Bambu Studio is not installed.")
    printer = next(item for item in slicer_profiles.list_printers(bambu, VENDOR) if item["name"] == MACHINE)
    process = next(item for item in slicer_profiles.list_processes(bambu, VENDOR, MACHINE) if item["name"] == PROCESS)
    return bambu, slicer_profiles.ProfileTarget(
        vendor=VENDOR, printer_name=MACHINE, machine_path=printer["machine_path"], process_name=PROCESS,
        process_path=process["process_path"], source=str(printer.get("source") or ""))


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "dist" / "mass_vs_slicer", help="output folder")
    args = parser.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    slicer_exe, profile = target()
    meshes = [body_mesh(name, mesh) for name, mesh in SHAPES.items()]

    rows = []
    started = time.monotonic()
    for setting_name, settings in SETTINGS.items():
        checked = slicer_check.check_bodies([(mesh, settings) for mesh in meshes], profile, slicer_exe)
        for mesh, body in zip(meshes, checked["bodies"]):
            if body["error"]:
                raise SystemExit("The slicer check failed for {}: {}".format(mesh.name, body["error"]))
            histogram = mass_model.slope_histogram(list(mesh.coordinates), list(mesh.indices))
            model = mass_model.estimate(mesh.volume_cm3, histogram, settings, DENSITY).grams
            error = (model / body["grams"] - 1.0) * 100.0
            rows.append((setting_name, mesh.name, body["grams"], body["total_grams"], model, error))
            print("{:<22} {:<26} body {:6.2f} g (slicer total {:5.2f})  model {:6.2f} g  {:+5.1f}%".format(
                *rows[-1]), flush=True)

    errors = [abs(row[5]) for row in rows]
    summary = "Mean absolute error {:.1f}%, largest {:.1f}% ({} slices, {:.0f} s).".format(
        sum(errors) / len(errors), max(errors), len(rows), time.monotonic() - started)
    print("\n" + summary)
    report = [
        "# Mass model vs the slicer check", "",
        "Slicer: {} {}. Printer {}, process {}, {} at {} g/cm³.".format(
            slicer_profiles.slicer_name(slicer_exe), slicer_profiles._slicer_version(Path(slicer_exe)),
            MACHINE, PROCESS, MATERIAL, DENSITY),
        "Layer 0.2 mm, top 5 and bottom 3 layers; no brim, skirt, supports or prime tower.",
        "\"Body\" leaves out the start routine's purge; the slicer's total keeps it.", "",
        "| Settings | Shape | Body (g) | Slicer total (g) | Model (g) | Model vs body |",
        "|---|---|---|---|---|---|",
    ]
    report += ["| {} | {} | {:.2f} | {:.2f} | {:.2f} | {:+.1f}% |".format(*row) for row in rows]
    report += ["", summary, ""]
    (out / "report.md").write_text("\n".join(report), encoding="utf-8")
    print("Report: {}".format(out / "report.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
