# Development

## Project layout

```
Filamatch/                      the add-in: this folder is what users install
├── Filamatch.py                entry point Fusion runs (run / stop)
├── Filamatch.manifest          Fusion add-in manifest (JSON)
├── AddInIcon.svg               icon in Fusion's Scripts and Add-Ins list
├── config.py                   names, ids, version, file locations
├── commands/open_palette/      toolbar button (+ resources/ icons)
├── palette/                    the palette UI: index.html, app.js, style.css
├── data/filaments.json         bundled filament catalog
├── data/printers.json          what Filamatch knows about printers
└── lib/                        all Python modules
    ├── palette_bridge.py       palette messages, settings, background jobs
    ├── materials_workflow.py   detect materials, export appearances
    ├── favorites.py            Fusion Favorites materials
    ├── custom_library.py       the Filamatch Library (.adsklib)
    ├── importer.py, catalog.py catalog and material import helpers
    ├── exporter.py             Fusion 3MF export + injection
    ├── threemf_injector.py     3MF slot metadata (also a standalone CLI)
    ├── slicer_profiles.py      slicer presets and the slicer CLI
    ├── slicer_profiles_selftest.py  its self-test (fake slicers and network)
    ├── project_fixes.py        fit project settings to the printer (tower, nozzles)
    ├── filament_presets.py     presets built from the Open Filament Database; slicer user presets
    ├── prusa_project.py        the PrusaSlicer 3MF layout
    ├── prusa_profiles.py       PrusaSlicer presets, projects and estimates
    ├── printer_catalog.py      reads data/printers.json
    ├── printer_model.py        neutral printer / slot / spool types
    ├── printer_sources.py      printer source registry (Bambuddy today)
    ├── bambuddy.py             Bambuddy API client
    ├── slot_mapping.py         filament → slot matching
    ├── inventory_sync.py       spools → filaments, slot manifest
    ├── openfilament.py         Open Filament Database client
    ├── secret_store.py         Keychain / Credential Manager
    ├── diagnostics.py          the diagnostics report
    └── addin_logging.py        log handlers
docs/                           documentation
scripts/                        installers, release builder
tests/                          tests (no Fusion needed)
```

The layout follows Autodesk's add-in template (thin entry file, `config.py`,
`commands/`, `lib/`). One difference: Fusion runs the entry file without a
Python package, so `Filamatch.py` loads its own folder as the package
`filamatch_addin`; inside it, modules use relative imports
(`from . import catalog`, `from .. import config`).

## Develop

1. Clone the repository.
2. Install your working copy into Fusion as a link:
   `scripts/install_addin_macos.sh --dev`. Fusion then loads the add-in from
   the repository, so you do not reinstall after a change. Run the script
   without `--dev` to go back to a normal copy. (On Windows, run the
   PowerShell script after each change.)
3. In Fusion, stop and run Filamatch in **Scripts and Add-Ins** (Shift+S), or
   restart Fusion. Starting the add-in from your own script does not work:
   Fusion removes a script's event handlers when the script ends.
4. To log more, set `DEBUG = True` in `config.py` (then debug messages also
   show in Fusion's TEXT COMMANDS window).

To work on the palette without Fusion, open `Filamatch/palette/index.html`
in a browser: it runs a demo with sample data.

## Tests

```bash
python3 tests/run_all.py
```

While you work, run only what your changes can affect:

```bash
python3 tests/run_all.py --changed          # changes against main
python3 tests/run_all.py --only slot_mapping,test_palette_contract.py,js
```

`--changed` runs the self-test of each changed module, of every module
that imports it, and every test file that names one of them; a file it
cannot place (such as `config.py`) runs everything. Run everything before
you merge.

The full run covers every module's self-test (`python3 -m Filamatch.lib.<module>`), the
test files in `tests/` and a syntax check of `app.js` (with Node.js, if
installed). Nothing needs Fusion: Fusion objects are faked, and the tests use
a temporary data folder and an in-memory secret store, so your real settings
and Keychain are never touched.

| File | Checks |
|---|---|
| `tests/test_addin_static.py` | Manifest, shipped files, ids, module APIs, file locations |
| `tests/test_fusion_lifecycle_simulation.py` | run/stop with a fake Fusion API, button, palette, repeated loads |
| `tests/test_palette_contract.py` | Every palette action has a handler on both sides |
| `tests/test_palette_export_flow.py` | The export flow with faked modules |
| `tests/test_install_script.py` | The install script: copy, `--dev` link and back (in a temporary folder) |
| `tests/test_slicer_matrix.py` | The slicer test matrix's own logic (no slicer needed) |
| `tests/test_printer_docs.py` | The README's printer tables match `data/printers.json` |

A self-test that needs a fake sibling module uses `lib/testing.py`
(`sibling_set` / `sibling_pop`).

## Slicer test matrix

The tests above fake the slicers. The slicer test matrix runs the real ones
(Bambu Studio, OrcaSlicer and PrusaSlicer installed in /Applications):

```bash
python3 scripts/slicer_matrix.py compare
```

Every case in `tests/slicer_matrix.json` prepares a neutral test model
(four 20 mm cubes, one filament each, separate or touching) for one printer
with one slicer, through the add-in's own code, and slices a copy for the
estimate. For touching bodies it also checks that every part stays where
its body was in the export. `compare` does this with the last release (a
temporary git worktree of the newest `v*` tag) and with your working tree,
and prints one line per case:

| Result | Meaning |
|---|---|
| SAME | Settings, slot assignments, part positions and estimate are equal |
| CHANGED | Something differs; the changed keys are listed. Check it is intended, then use `--accept-changes` |
| FIXED | The release failed this case; your version works |
| BROKEN | The release worked; your version fails. Fix it before you merge or release |
| NEW | The release has no such case or feature |
| SKIPPED | The slicer for the case is not installed |

It takes about a minute and writes `dist/matrix/report.md`. Run it before
you merge a branch that touches the slicer code, and add a case to
`tests/slicer_matrix.json` for every printer Filamatch supports.
`python3 tests/test_slicer_matrix.py` checks the matrix itself (no slicer
needed).

## Add a printer

1. `Filamatch/data/printers.json`: the printer's name token (and an alias
   when its presets spell the name out), its serial prefix if it is a Bambu
   Lab printer, an `orca_unsupported_tokens` entry if OrcaSlicer cannot slice
   it, and its row in `levels` (what is tested and what is not).
2. `python3 scripts/printer_docs.py` writes the README's printer tables from
   that file (`tests/test_printer_docs.py` fails when you forget).
3. A case in `tests/slicer_matrix.json`, then
   `python3 scripts/slicer_matrix.py compare`: the new case shows as NEW,
   everything else must stay SAME.
4. Code only for what the data cannot say (a new nozzle layout, a new
   printer connection); see [design/multi-nozzle.md](design/multi-nozzle.md).

## Rules for the code

- **Never block Fusion.** Network calls and subprocesses go through
  `palette_bridge._run_job`. Code that runs there must not call the Fusion API.
- **Use short timeouts** for every network call.
- **Never log secrets.** The API key lives in the secret store; logs and
  palette messages only say whether a key is set.
- **Write files atomically** (temp file, then `os.replace`) and never inside
  the add-in folder. Use the locations in `config.py`.
- **The palette never uses `innerHTML` with data**: build elements and set
  `textContent`.
- Keep `threemf_injector.py` free of package imports: it is also a
  standalone command-line tool.

## Palette messages

The palette calls `adsk.fusionSendData(action, json)`; Python answers with
`palette.sendInfoToHTML(reply, json)`. The full list of actions and replies
is at the top of `Filamatch/palette/app.js`; Python handles them in
`palette_bridge.dispatch`. `tests/test_palette_contract.py` fails when the
two sides disagree.

## Release

1. Update `VERSION` in `Filamatch/config.py` and `version` in
   `Filamatch/Filamatch.manifest` (a test checks they match), and the
   [changelog](../CHANGELOG.md), including what is tested and what is not.
2. `python3 tests/run_all.py`
3. `python3 scripts/slicer_matrix.py compare` (see above).
4. `python3 scripts/build_release.py` writes `dist/Filamatch-<version>.zip`
   from the files tracked by git.
5. Tag the commit `v<version>` and attach the zip to the release.

The maintainer's publish script runs steps 2 to 5 itself and stops on a
BROKEN case; the matrix table goes into the release notes.
