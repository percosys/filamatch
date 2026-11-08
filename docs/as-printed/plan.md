# As printed – plan

> This plan was written for the Printegrity add-in, which became Filamatch's
> **As printed** tab on 2026-11-08 and then the **Printed weight** card in
> the one Print view: one add-in, one copy of the slicer and settings code,
> the printer and process from Print settings, per-body settings in
> Filamatch's body editor, each body's grams in Filamatch's body lists, and
> one "<filament> · <body> · as printed" copy per body while the switch
> "Fusion uses the printed weight" is on (unused copies are deleted). The
> estimate uses the design's orientation; there is no build direction
> setting. See `docs/development.md`, "Printed weight (As printed)". Where
> this plan says "the add-in" or "Filamatch" as another tool, read "the
> Printed weight card" and "the Print view".

A Fusion add-in that gives each body the material properties of the part
**as it will be printed**: nozzle, layer height, walls, infill and print
orientation. Fusion's mass, center of mass and Simulation then use realistic
numbers. A separate project from Filamatch that shares its catalog and slicer
code.

## Goal and promise

- **Mass:** matches the slicer within a few percent.
- **Stiffness and strength:** estimates with a stated range and their source,
  never presented as design values. Every number shows where it came from.

## How it works

1. **Read the print setup** for each body, from a Filamatch project or the
   slicer preset: nozzle, layer height, wall count, top/bottom layers, infill %
   and pattern, and build direction.
2. **Look up the filament's base values** for printed solid specimens, in the
   XY and Z directions (see the data plan below).
3. **Scale them for the print settings:**
   - Shell plus core: walls and top/bottom count as solid material, and the
     infill counts as a cellular core (Gibson-Ashby):
     - stiffness ≈ Es·(ρ)^n, with n ≈ 2 for gyroid or grid and ≈ 1 for
       triangles;
     - strength ≈ (ρ)^1.5.
   - The shell and core are mixed by area fraction for mass and tension, and
     by bending stiffness for beams.
   - A layer-height factor lowers Z strength.
4. **Write a material copy per body** ("PETG · 0.4/0.2 · 3 walls · 15% gyroid ·
   Z-up"). It sets density, Young's modulus, Poisson's ratio, yield and
   tensile strength.
   - Fusion Simulation is isotropic, so the user picks a mode:
     - **Safe:** uses the Z (layer-bond) values. This is the default.
     - **Typical:** the XY and Z values are averaged.
5. **Calibrate the mass:** after a slice, set the density so that Fusion's
   mass equals the slicer's grams. This step is exact and can be checked.
6. **Report:** a panel per body with the values used, the range, and the
   sources with dates. A clear note says these are estimates.

## Data plan (three tiers)

| Tier | Filaments | Source | Accuracy |
|---|---|---|---|
| 1. Generic | PLA, PETG, ABS, ASA, TPU, PA, PC, CF blends | Ranges from several data sheets and tests; use the low end in Safe mode | ±25–40% |
| 2. Brand data sheets | Polymaker, Bambu Lab, Prusament, BASF Ultrafuse, BCN3D, Fiberlogy, colorFabb… | Data sheets with printed ISO 527 / ASTM D638 bars, XY and Z when given | ±10–20% |
| 3. Measured | The user's own or community test prints | A calibration print (tensile and layer-bond bars) entered in the add-in | Best |

Starting generic values (printed at 100%, to be checked against the sources
before use):

| Material | E XY (GPa) | UTS XY (MPa) | UTS Z (MPa) |
|---|---|---|---|
| PLA | 2.3–3.5 | 45–60 | 20–40 |
| PETG | 1.7–2.1 | 40–50 | 20–35 |
| ABS | 1.8–2.3 | 30–40 | 15–28 |
| ASA | 1.8–2.2 | 35–45 | 20–30 |
| PC | 2.0–2.4 | 55–65 | 25–45 |
| PA-CF (dry) | 3.5–8 | 60–100 | 25–50 |
| PETG-CF | 3–5 | 40–55 | 15–30 |
| PLA-CF | 3.5–5 | 35–50 | 15–25 |
| TPU 95A | 0.02–0.05 | 25–40 | 15–30 |

## Sources found

- **Data sheets with XY and Z values:**
  - [BASF Ultrafuse PLA Tough](https://cdn.shopify.com/s/files/1/0765/4369/files/Ultrafuse_PLA_Tough_TDS_EN.pdf) (40 vs 28 MPa)
  - [BASF filament comparison](https://move.forward-am.com/hubfs/AES%20Documentation/General%20Resources/Filament%20Comparison.pdf)
  - [BCN3D TDS](https://www.bcn3d.com/wp-content/uploads/2024/02/BCN3D-Filaments-Omega-Technical-Data-Sheets-TDS-EN-OTDS-V2.pdf)
  - Bambu Lab TDS (PLA Silk: 35 vs 18 MPa)
  - [Fiberlogy TDS](https://fiberlogy.com/en_US/p/file/8155c1da1791d89e2516617d17591044/FIBERLOGY_ASA-AF_TDS.pdf)
  - Polymaker and Prusament data sheets (not checked yet)
- **Testers:** CNC Kitchen (layer adhesion, walls vs infill, annealing) and My
  Tech Fun (brand comparisons). Good for relative trends and checking the
  scaling, not as design values.
- **Open data:** an [open AM tensile database (Garcia-Granada 2023)](https://dau.url.edu/handle/20.500.14342/4760)
  (license to check), and regression papers on infill and layer height.
- **Not usable in bulk:** MatWeb and SpecialChem (their terms of use), and data
  sheets for injection-molded or raw resin.
- The Open Filament Database seems to have no mechanical fields. This needs
  checking.

## Rules for the data

- Enter each value by hand, with its source URL, date, test standard,
  specimen type (printed vs molded), orientation and print settings.
- Never copy whole databases, and never ship the PDFs. Numbers are facts, but
  compiled databases can be protected.
- Leave out a value when the specimen type isn't clear. Molded values are much
  higher than printed ones.
- Data file: `materials.json` (generic plus brands) in a public repo, and
  community values through pull requests that include their source.

## Steps

1. **Mass first:** density from the print settings, then calibrated to the
   slicer's grams. Small, exact, and useful right away.
2. **Generic tier 1** with the Safe and Typical modes, the per-body material
   copies and the report panel.
3. **Brand tier 2:** the first 20 engineering filaments from data sheets
   (PA-CF, PC, PETG-CF, ASA and so on).
4. **Check against tests:** print bars, compare with the scaling model, and
   tune the exponents.
5. **Calibration prints** for users (tier 3).

## Try it: the interactive part

A "what if" panel for one selected body. Change a setting and see the effect
right away, without touching the design until you press Apply.

- **Inputs:** filament (from the Filamatch catalog), nozzle, layer height,
  walls, top/bottom layers, infill % and pattern, and build direction
  (Z-up, or lay flat on a picked face).
- **Live results**, from the fast model, not a full slice:
  - mass and filament cost;
  - stiffness and strength in XY and Z, with their range;
  - a rough print time (from volume, flow and nozzle size);
  - flags such as "Z is the weak direction for this load", "TPU: small
    deflection only", and "the wall is thinner than 2 lines at this nozzle".
- **Compare:** pin up to 3 setups side by side, for example "PLA 15%" vs
  "PETG 25%" vs "PA-CF 3 walls".
- **Suggest:** "lightest setup that keeps the safe strength above X". It
  searches the infill and wall options for the chosen filament.
- **Apply** writes the chosen setup as the body's material copy. **Check**
  runs one real slice for the exact grams and time (in the background, with
  the Filamatch code).
- **Hand-off to Filamatch:** the chosen nozzle, layer height and infill can
  become the print settings when Filamatch sends the design. This is an
  agreed field, so the two add-ins don't overwrite each other.
- **Later:** run a Fusion Simulation for each pinned setup and show the
  results side by side. Fusion's API support for running studies needs
  checking first.

The live model must be fast, under 50 ms. The slicer runs only when asked.

## Reuse from Filamatch

Copy these modules at a tagged Filamatch version, and note the source commit
at the top of each file. Keep them close to the original so fixes can flow
both ways. A shared package can come later, once both projects are stable.

| Filamatch module | Use in the new add-in |
|---|---|
| Autodesk add-in template (`Filamatch.py`, `config.py`, `commands/…/entry.py`) | The same start, stop and package loading, ids, version, and data paths (Application Support, Caches, Logs) |
| `palette_bridge` pieces: `_run_job`, the worker-thread custom event, the `_send` / `_send_error` messages, and the settings load/save | Background work that never blocks Fusion, and the palette messages |
| `palette/` patterns: screen layout, `textContent`-only rendering, demo mode, the contract test | The report panel and the settings screen |
| `fusion_helpers` | Safe Fusion API access |
| `materials_workflow` (finding body materials, appearance and description handling, restoring after changes) | Finding each body's filament, writing the material copies, and Restore |
| `catalog` + `data/filaments.json`, `openfilament` (slicer_data, density) | Filament identity, densities, and the link to the database |
| `slicer_profiles` reading (`_flatten_local_preset`, preset identity), `prusa_profiles` (bundle reader and fit check), `filament_presets.user_presets` | Reading the print settings (layer height, walls, infill) from Bambu Studio, OrcaSlicer and PrusaSlicer presets or projects |
| `slicer_profiles.estimate_print` | The slicer's grams, for the mass calibration |
| `addin_logging`, `diagnostics`, `printer_report` (redaction) | Logs, the diagnostics report, private values kept out |
| `testing`, `tests/run_all.py` (with `--changed`), `test_addin_static`, `test_fusion_lifecycle_simulation`, `scripts/install_addin_macos.sh --dev`, `scripts/publish_public.py` | Tests, the dev install and the release gate |

## Project rules (from Filamatch)

- **Never block Fusion.** Slow work goes through the worker-thread job. Code
  there doesn't call the Fusion API.
- **Never restart the add-in or open its palette from an MCP script.** Never
  call `adsk.autoTerminate(False)`.
- **Write files atomically**, never inside the add-in folder.
- **Palette:** no `innerHTML` with data.
- **Never log secrets or private values.**
- **Every fix gets a test or a self-test check first.** `run_all.py` must pass
  before every commit.
- **A real-world regression check against the last release**, like Filamatch's
  slicer matrix. Here that means a set of reference bodies with known printed
  mass and test-bar strength, compared before every release.
- **Say what is tested and what isn't** in the changelog and README for every
  release.
- **Data facts in one data file** (`materials.json`), with the README tables
  generated from it.
- **Iterate on GitLab; publish to GitHub only through the publish script**,
  with the private-data scan, after asking first.
- **Plain, short docs**: user guide, development and triage pages, and issue
  forms (here: "send your test-bar results").
- **Keep a handoff note current** at every finish or merge.

## Living next to the Print tab

- **Separate namespace.** Filamatch's materials, Favorites, its library and its
  "Filamatch Export" appearances are never changed or deleted. The tab makes
  its own design-only copies, named with a clear prefix
  ("As printed · PETG · 0.4/0.2 · 2 walls · 5/3 layers · 15% gyroid · Z-up ·
  0.434 g/cm³"), and finds them by an id in the material description, not
  by name.
- **The Print tab still sees the filament.** Each copy records the name of
  the material it was copied from, so slot matching and Send see "PETG
  Black", not an unknown material:
  - description: `As printed | filamatch_source=<source material name,
    exactly as Fusion shows it> | <settings label> | as_printed_id=<12 hex> |
    settings_id=<12 hex>`. The source name must not contain `|`; the tab
    refuses to write the copy otherwise. Filamatch reads the source with
    `(?:^|\|)\s*filamatch_source=([^|]+?)\s*(?=\||$)`. `settings_id` is a
    fingerprint of every print setting, so a body whose settings changed
    since Apply is marked "Settings changed since Apply".
  - Copies made by the Printegrity add-in (`Printegrity | …`,
    `printegrity_id=`) are still read, never written.
  - Filamatch takes density, type and colour from its own catalog entry,
    never from the copy, and groups copies with their filament (they share
    the source's material id).
  - "Use it" (put the slot's spool on the bodies) replaces the material on
    every body of that filament, copies included; the tab then shows the
    body as not applied.
- **Switch back cleanly.** "Restore filament" puts the body back on its
  original material, through Fusion's normal material assignment.
- **No library management.** Fusion's API can't reliably remove materials or
  manage Favorites. So the tab:
  - creates only design materials, never library or Favorites entries;
  - reuses a copy when the same filament, settings and density come up
    again;
  - leaves unused copies alone and says how many there are (they can be
    removed by hand in Fusion's Material browser);
  - never touches materials it didn't make.
- **One owner per setting.** The Print tab owns colour, filament type, slot,
  printer and process. The As printed tab owns only the mechanical numbers:
  density, modulus, strength and Poisson's ratio.
- **Shared per-body print settings.** A body's own infill, infill type, walls
  and top/bottom layers are Filamatch's body settings (`body_settings.py`),
  edited in the shared body editor; so the mass is estimated with the
  settings the slicer gets. The build direction is the tab's own value
  (attribute group "Filamatch", name "as_printed"), in the same editor; it
  is for the estimate only, the slicer gets the design's orientation.

## Assumptions shown to the user

- Values assume each filament is **printed under good conditions**: dry
  filament, the maker's recommended temperatures, good layer bonding and a
  calibrated printer. Wet filament, nylon especially, and poor settings give
  lower strength. The report panel and the material description say this.
- These are estimates, not design values. Safe mode is the default.

## Flexible filaments (TPU, TPE)

- They are not linear. Stiffness changes a lot with stretch, and they stretch
  300–600% before breaking. A single Young's modulus is only right for small
  strains.
- The add-in:
  - sets the real density, so mass stays right;
  - gives a small-strain modulus with a clear note: "for small deflection
    only; not valid for large stretch, snap fits or gaskets";
  - doesn't give a yield strength, because a safety factor would mislead;
  - warns in the report when a body is TPU or TPE.
- Later: hardness grade (85A, 95A) and infill have a large effect, and a
  proper treatment needs the nonlinear (hyperelastic) material models that
  only some Fusion Simulation study types support. This needs its own check.

## Open questions

- Can Fusion's API set every property on a design material copy? **Yes**
  for density, Young's modulus, Poisson's ratio, yield and tensile strength
  (checked 2026-11-08; see `docs/HANDOFF.md`). Simulation use is not checked
  yet.
- Fusion Simulation supports only isotropic materials for most study types.
  Is the "Safe" (Z) mode enough, or should the project also export to a tool
  that handles direction-dependent (orthotropic) materials? Fusion materials
  do have a Transverse Isotropic mode (one value in XY, another in Z). Check
  whether Simulation uses it.
- Nylon changes with moisture: add a dry vs conditioned choice.
