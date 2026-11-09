#!/usr/bin/env python3
"""Static check of the printed stiffness and strength with a free FEA program.

Usage (from the repo root; needs ``pip install scikit-fem matplotlib``, best
in a virtual environment of its own):

    python3 scripts/as_printed_static_check.py                     # the table
    python3 scripts/as_printed_static_check.py --plot check.png    # and a picture

Three test parts, each fixed at its +X end face with a force spread over
its top face (straight down), as in a Fusion Static Stress study: a plate
40x40x5 mm (Bambu Lab ABS, 10 N), a bar 100x10x5 mm (Bambu Lab PAHT-CF,
10 N) and a block 60x30x15 mm (Bambu Lab PLA Basic, 100 N). The material
values are the ones Filamatch wrote into the "as printed" copies in Fusion
(Safe; 0.4 mm nozzle, 0.08 mm layers, 2 walls, 9 top / 7 bottom layers, 15 %
gyroid). Each result is compared with beam theory (with shear). Units: mm,
N, MPa. Triquadratic hex elements (27 nodes), so bending is right.

Fusion runs Static Stress in the cloud only; this checks the values without
it. Nothing in the add-in is changed.
"""

from __future__ import annotations

import argparse
import time

import numpy as np
from skfem import Basis, ElementHex2, ElementVector, FacetBasis, LinearForm, MeshHex, asm, condense, solve
from skfem.models.elasticity import lame_parameters, linear_elasticity

# name: (L, b, h, F, mesh nx, ny, nz, E, Poisson's ratio, yield, drawn bend scale)
PARTS = (
    ("Plate · Bambu ABS · 10 N", 40.0, 40.0, 5.0, 10.0, 16, 16, 2, 829.121, 0.36, 11.591, 20),
    ("Bar · Bambu PAHT-CF · 10 N", 100.0, 10.0, 5.0, 10.0, 40, 4, 2, 1133.665, 0.36, 24.087, 1),
    ("Block · Bambu PLA Basic · 100 N", 60.0, 30.0, 15.0, 100.0, 24, 12, 6, 467.956, 0.35, 6.764, 10),
)
# Sequential blue ramp (light = low, dark = high), surface and ink colours.
RAMP = ("#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b")
SURFACE, INK, MUTED, GRID = "#fcfcfb", "#1f1f1e", "#6b6a66", "#d9d8d4"


def solve_part(L, b, h, F, nx, ny, nz, E, nu):
    """Fixed +X end, force spread over the top face. Returns the mesh, vertex
    displacements (3, n), von Mises stress per quadrature point (elements, points)
    and the quadrature points' x."""
    m = MeshHex.init_tensor(np.linspace(0, L, nx + 1), np.linspace(0, b, ny + 1), np.linspace(0, h, nz + 1))
    e = ElementVector(ElementHex2())
    basis = Basis(m, e, intorder=4)
    lam, mu = lame_parameters(E, nu)
    K = asm(linear_elasticity(lam, mu), basis)
    top = m.facets_satisfying(lambda x: np.isclose(x[2], h))
    fixed = m.facets_satisfying(lambda x: np.isclose(x[0], L))
    p = F / (L * b)

    @LinearForm
    def load(v, w):
        return -p * v.value[2]

    f = asm(load, FacetBasis(m, e, facets=top))
    u = solve(*condense(K, f, D=basis.get_dofs(fixed).all()))
    g = basis.interpolate(u).grad
    eps = 0.5 * (g + np.einsum("ij...->ji...", g))
    tr = eps[0, 0] + eps[1, 1] + eps[2, 2]
    s = 2 * mu * eps
    for i in range(3):
        s[i, i] = s[i, i] + lam * tr
    vm = np.sqrt(0.5 * ((s[0, 0] - s[1, 1]) ** 2 + (s[1, 1] - s[2, 2]) ** 2 + (s[2, 2] - s[0, 0]) ** 2)
                 + 3 * (s[0, 1] ** 2 + s[1, 2] ** 2 + s[0, 2] ** 2))
    x_qp = basis.mapping.F(basis.quadrature[0])[0]
    return m, u[basis.nodal_dofs], vm, x_qp


def beam(L, b, h, F, E, nu):
    """Cantilever with an even load: tip deflection (bending + shear) and the wall stress."""
    I, A, G = b * h ** 3 / 12, b * h, E / (2 * (1 + nu))
    return F * L ** 3 / (8 * E * I) + F * L / (2 * 5 / 6 * G * A), (F * L / 2) / (b * h ** 2 / 6)


def table() -> list:
    rows = []
    for name, L, b, h, F, nx, ny, nz, E, nu, yld, _scale in PARTS:
        start = time.time()
        m, vu, vm, _x = solve_part(L, b, h, F, nx, ny, nz, E, nu)
        deflection = np.sqrt((vu ** 2).sum(axis=0)).max()
        beam_deflection, beam_stress = beam(L, b, h, F, E, nu)
        print("{:32s} FEA {:7.3f} mm, safety factor {:5.2f} | beam {:7.3f} mm, safety factor {:5.2f} "
              "| {:+.1f} % deflection  ({:.1f} s)".format(
                  name, deflection, yld / vm.max(), beam_deflection, yld / beam_stress,
                  100 * (deflection / beam_deflection - 1), time.time() - start))
        rows.append((name, m, vu, vm.mean(axis=1), vm.max(), yld))
    return rows


def _around(flat, moved):
    """A quad's moved corners in order around the face (by angle in the unbent face)."""
    centre = flat.mean(axis=0)
    axis = np.argmin(np.ptp(flat, axis=0))
    u, v = [i for i in range(3) if i != axis]
    return moved[np.argsort(np.arctan2(flat[:, v] - centre[v], flat[:, u] - centre[u]))]


def plot(rows, out: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap, Normalize
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    cmap = LinearSegmentedColormap.from_list("seq_blue", RAMP)
    norm = Normalize(0, 60)
    fig = plt.figure(figsize=(13, 5.2), facecolor=SURFACE)
    for index, ((name, m, vu, element_vm, vm_max, yld), part) in enumerate(zip(rows, PARTS)):
        scale = part[-1]
        ax = fig.add_subplot(1, 3, index + 1, projection="3d", facecolor=SURFACE)
        boundary = m.boundary_facets()
        moved = m.p + scale * vu
        polys = [_around(m.p[:, q].T, moved[:, q].T) for q in m.facets[:, boundary].T]
        colours = cmap(norm(100 * element_vm[m.f2t[0, boundary]] / yld))
        ax.add_collection3d(Poly3DCollection(polys, facecolors=colours, edgecolors=(1, 1, 1, 0.25), linewidths=0.2))
        xs, ys, zs = m.p
        for x in (xs.min(), xs.max()):
            for y in (ys.min(), ys.max()):
                ax.plot([x, x], [y, y], [zs.min(), zs.max()], color=GRID, lw=0.8)
        for z in (zs.min(), zs.max()):
            ax.plot([xs.min(), xs.max(), xs.max(), xs.min(), xs.min()],
                    [ys.min(), ys.min(), ys.max(), ys.max(), ys.min()], [z] * 5, color=GRID, lw=0.8)
        lo = np.minimum(m.p.min(axis=1), moved.min(axis=1))
        hi = np.maximum(m.p.max(axis=1), moved.max(axis=1))
        ax.set_xlim(lo[0], hi[0]); ax.set_ylim(lo[1], hi[1]); ax.set_zlim(lo[2], hi[2])
        ax.set_box_aspect(hi - lo, zoom=0.78)
        ax.view_init(elev=22, azim=-58)
        ax.set_axis_off()
        ax.set_title(name, color=INK, fontsize=11, pad=0)
        drawn = "shape drawn true to scale" if scale == 1 else "bend drawn {}× larger".format(scale)
        ax.text2D(0.5, -0.02, "bends {:.2f} mm · safety factor {:.1f}\n{}".format(
            np.sqrt((vu ** 2).sum(axis=0)).max(), yld / vm_max, drawn),
            transform=ax.transAxes, ha="center", va="top", color=MUTED, fontsize=9.5)
    cax = fig.add_axes([0.30, 0.10, 0.40, 0.025])
    bar = fig.colorbar(plt.cm.ScalarMappable(norm=norm, cmap=cmap), cax=cax, orientation="horizontal")
    bar.set_label("Stress as % of the printed yield strength (Safe values; 100 % = yields)", color=INK)
    bar.outline.set_visible(False)
    bar.ax.tick_params(colors=MUTED, length=0)
    fig.suptitle("Static check of Filamatch's printed values (free FEA, scikit-fem) · left end free, right end fixed",
                 color=INK, fontsize=12, y=0.97)
    fig.savefig(out, dpi=170, facecolor=SURFACE, bbox_inches="tight")
    print("Saved", out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--plot", help="also save a picture (PNG) here")
    args = parser.parse_args()
    rows = table()
    if args.plot:
        plot(rows, args.plot)


if __name__ == "__main__":
    main()
