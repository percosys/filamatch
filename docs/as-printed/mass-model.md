# The mass model

The "Printed weight" card in the Print view estimates how much each body
weighs **as printed**. The
code is `Filamatch/lib/mass_model.py` (it came from the Printegrity add-in).
It is plain Python with no Fusion.

## How it works

A printed part is a solid skin around a partly filled core.

1. **Skin.** Every surface gets a solid skin. Its thickness is the larger of
   two values:
   - **Walls**, measured across the layers:
     `walls × line spacing`. A printed line has round sides, so slicers
     space wall loops at `line width − layer height × (1 − π/4)`. That is
     0.397 mm for a 0.44 mm line at 0.2 mm layers, not the full 0.44 mm.
   - **Top or bottom layers**, measured up the build direction:
     `layers × layer height`. Faces that point up get the top layers. Faces
     that point down get the bottom layers.

   A sloped face gets both values turned toward its normal (`sin` and `cos`
   of its angle to the build direction), and the larger one wins.
2. **Thin parts.** The skin can never hold more than the whole body. A part
   that is mostly skin prints nearly solid, and the palette says so.
3. **Core.** The rest of the body is filled to the infill percentage.

So `mass = density × (skin + infill × (volume − skin))`.

Fusion gives the body's exact volume. The surface comes from Fusion's
triangle mesh, sorted into 40 slope groups. The groups keep the exact
average slope of their triangles, so vertical walls stay exactly vertical.
Bodies inside turned components are measured in the design's own
coordinates. The density is the solid density of the body's filament.

The settings come from the chosen print process (infill, walls, layers,
layer height, wall line widths and the minimum top and bottom thickness,
which adds layers as the slicers do), with each body's own settings on top.
Without process values the built-in defaults apply: 0.4 mm nozzle, 0.2 mm
layers, 2 walls, 5 top and 3 bottom layers, 15% gyroid.

## How good it is

The comparison script (`scripts/mass_vs_slicer.py`, still in the archived
Printegrity repository) slices simple shapes with the slicer check (see below) and compares the body's grams with the model's. Results on
2026-11-08 (Bambu Studio 2.9.1, X1C 0.4 nozzle, 0.20 mm Standard, Bambu PLA
Basic, 0.2 mm layers, top 5, bottom 3, no brim, skirt, supports or prime
tower; the model with its default 0.44 mm line width):

| Settings | Cube 20 | Plate flat | Plate upright | Cylinder | 45° wedge | Block 60×30×15 |
|---|---|---|---|---|---|---|
| 2 walls, 15% gyroid | +2.6% | +2.3% | +2.7% | +1.7% | −6.3% | +3.1% |
| 4 walls, 15% gyroid | +6.5% | +7.2% | +4.7% | +6.3% | +0.2% | +4.3% |
| 2 walls, 40% grid | −1.0% | +1.3% | +6.1% | −0.4% | −0.9% | +0.9% |

Mean error 3.3%, largest 7.2%, over 18 slices. The model is mostly a little
heavy.

Two lessons from getting here:

- With the full line width for walls, the model was 8–13% too heavy with 4
  walls. Line spacing fixed most of it.
- An earlier version of this table compared against the slicer's total
  grams. Those include the printer's start routine (a purge line, about
  0.33 g per print on the X1C), which is not part of the body. That made the
  model look better than it is.

## The slicer check

The model is a quick estimate. The slicer check gives the exact grams for
the chosen printer and process (`Filamatch/lib/slicer_check.py`):

1. Fusion's fine triangle mesh of each filament body is read on Fusion's
   main thread.
2. In a background job, each body is written as a plain 3MF, in the
   design's orientation (Z up), as Filamatch sends it to the slicer.
3. Filamatch's slicer code (copied unchanged) prepares a project for the
   chosen printer and process, with the body's filament type and density.
4. As Filamatch prints: the process stays as it is, and the body's own
   settings go on top (under the slicer's own names). Brim, skirt, supports
   and the prime tower are turned off: they are not part of the body.
5. The slicer slices it. The body's grams are the filament its own G-code
   features use, times the slicer's grams per millimetre of filament. The
   start routine's purge is left out.
6. The density that makes Fusion's mass equal those grams is
   `grams / volume`.

It works with OrcaSlicer and Bambu Studio, not yet PrusaSlicer. It takes
about 2 seconds a body. In Fusion, an ABS cube gave 2.91 g; the same cube in
PLA, sliced outside Fusion, gave 3.47 g, which is the same after the density
change.

## Known limits

- **Slopes.** On a 45° roof with 2 walls the model is 6% light. Slicers add
  extra solid fill under sloped top surfaces. The model does not.
- **Many walls.** With 4 walls the model is 4–7% heavy. Edges and corners
  are counted twice.
- **Not tested yet:** other slicers, other nozzles and layer heights,
  lightning or adaptive infill, real parts with holes and fillets, and
  real printed weights.
- The model needs the body's material to be a filament. A body with a
  metal material gets a note, not grams.
- **Build direction.** The estimate uses the design's orientation (Z up), as
  the slicer does. To estimate another orientation, turn the body in Fusion.
- **Fusion uses the printed weight** (the switch, off until you turn it on
  in a design) gives each filament body
  its own "… · as printed" copy of its filament with its printed density (the
  slicer check's when it ran with the same settings and shape, else the
  estimate). The density depends on the body's shape, so bodies never share
  a copy. While the switch is on, a change of settings or shape updates the
  copy. Off puts the filament back. Copies no body uses are deleted. Filamatch
  still sees the filament through the copy's description
  (`filamatch_source=`).
