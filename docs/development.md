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
└── lib/                        all Python modules
    ├── palette_bridge.py       palette messages, settings, background jobs
    ├── materials_workflow.py   detect materials, export appearances
    ├── favorites.py            Fusion Favorites materials
    ├── custom_library.py       the Filamatch Library (.adsklib)
    ├── importer.py, catalog.py catalog and material import helpers
    ├── exporter.py             Fusion 3MF export + injection
    ├── threemf_injector.py     3MF slot metadata (also a standalone CLI)
    ├── slicer_profiles.py      slicer presets and the slicer CLI
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
2. Install your working copy into Fusion: `scripts/install_addin_macos.sh`
   (or the Windows PowerShell script). Run it again after each change.
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

This runs every module's self-test (`python3 -m Filamatch.lib.<module>`), the
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

A self-test that needs a fake sibling module uses `lib/testing.py`
(`sibling_set` / `sibling_pop`).

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
   [changelog](../CHANGELOG.md).
2. `python3 tests/run_all.py`
3. `python3 scripts/build_release.py` writes `dist/Filamatch-<version>.zip`
   from the files tracked by git.
4. Tag the commit `v<version>` and attach the zip to the release.
