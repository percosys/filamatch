# How it works

This page is for curious users and contributors. The [user guide](user-guide.md)
covers day-to-day use.

- [The parts](#the-parts)
- [From material to slot](#from-material-to-slot)
- [What Send to Orca writes](#what-send-to-orca-writes)
- [Filaments in Fusion](#filaments-in-fusion)
- [Printers and profiles](#printers-and-profiles)
- [File locations](#file-locations)
- [Threads and responsiveness](#threads-and-responsiveness)
- [The 3MF injector command-line tool](#the-3mf-injector-command-line-tool)
- [Upgrading from FusionFilament Bridge](#upgrading-from-fusionfilament-bridge)

## The parts

```
Fusion ──run/stop──▶ Filamatch.py ──▶ commands/open_palette (toolbar button)
                                            │
                                            ▼
                    palette/ (HTML, JS) ◀──JSON──▶ lib/palette_bridge.py
                                                     │
        ┌───────────────┬───────────────┬────────────┼──────────────┬──────────────┐
        ▼               ▼               ▼            ▼              ▼              ▼
  materials_workflow  favorites    custom_library  exporter    slicer_profiles  printer_sources
  (detect, export     (Favorites)  (.adsklib)      threemf_    (Orca CLI,       bambuddy
   appearances)                                    injector    presets)         slot_mapping
```

- **Filamatch.py** is the file Fusion runs. Fusion loads it without a Python
  package, so it loads its own folder as the package `filamatch_addin`. All
  modules live under that one name: nothing is added to Python's search path
  and module names cannot clash with other add-ins.
- **config.py** holds the names, ids, version and file locations.
- **commands/open_palette** adds the toolbar button and opens the palette.
- **palette/** is the user interface: plain HTML, CSS and JavaScript in
  Fusion's built-in browser. It talks to Python with JSON messages
  (`adsk.fusionSendData` one way, `palette.sendInfoToHTML` the other).
  Opened in a normal browser it shows a demo with sample data.
- **lib/palette_bridge.py** answers the palette's messages and calls the
  other modules. The list of messages is at the top of `palette/app.js` and
  in `dispatch()`.

## From material to slot

1. **Detect.** `materials_workflow.collect_assembly_materials` walks every
   solid body in the design (all components and occurrences) and groups them
   by physical material. A material made by Filamatch is matched to its
   catalog entry, which gives its brand, type, colour and density; for other
   materials the colour comes from the body's appearance. The order of first
   appearance gives the slot order of the export.
2. **Read the printer.** `printer_sources` asks the configured source (today
   Bambuddy) for printers, AMS slots and the spools in them, and turns the
   answer into neutral `printer_model` types (`PrinterInfo`, `SlotInfo`,
   `SpoolInfo`), so more printer brands can be added later.
3. **Match.** `slot_mapping.score_slot` scores every filament/slot pair:
   - material type: same type +120, same family (for example PLA and PLA-CF) +80,
     different −50;
   - colour distance: identical +60, very close +50, close +25;
   - same brand: a small bonus.

   The best slot above a threshold is proposed. **✓** means type and colour
   match; **≈** means a usable slot with a different shade. A choice you make
   by hand always wins and is saved per filament.

## What Send to Orca writes

A 3MF file is a zip archive. Filamatch adds or changes these parts:

| Part | Content |
|---|---|
| `3D/3dmodel.model` | Written by Fusion: one object per body. Filamatch gives duplicate body names temporary unique names during the export, because Fusion writes only body names. |
| `Metadata/model_settings.config` | The slot (extruder) for every object, in the format OrcaSlicer and Bambu Studio read. |
| `Metadata/project_settings.config` | Written by the slicer during preparation, then corrected so the filament colours, types, densities and names match the design. Filamatch also picks a build plate every filament accepts and moves the wipe tower inside the printable area. |
| `Metadata/filamatch.json` | A small record of the printer and which spool is in which slot (schema `filamatch.slot_manifest.v1`). Slicers ignore it. |

**Preparation** runs the slicer's command line:

```
orca-slicer --load-settings "<printer>;<process>" --load-filaments "<f1>;<f2>;…"
            --export-3mf <name> --outputdir <folder> [--assemble] <base.3mf>
```

Bodies that touch are kept as one multi-part object (`--assemble`), so the
parts stay in place; separate bodies are arranged on one plate. If
preparation fails, the tagged 3MF without printer settings is kept and the
palette shows the slicer's log.

## Filaments in Fusion

- **Import.** Fusion's API cannot create a physical material from scratch.
  Filamatch copies a stock Fusion material close to the filament (for example
  *ABS Plastic*), names it after the filament, sets the density and gives it
  a coloured copy of a stock appearance of a similar colour.
- **Favorites.** The material is added to Fusion's app-level Favorites, so it
  is available in every design. Fusion's API cannot remove favorites; an
  older version of a favorite is renamed "OLD - …" and can be removed in
  Fusion's UI.
- **Filamatch Library.** A Fusion material library (`.adsklib`) built from
  the Favorites snapshot, so the filaments also appear under their own
  library in the Material Browser. Fusion caches a library per file path, so
  every rebuild goes to a new stamped folder and older builds are deleted.
- **Export colours.** Before the export, every body gets the colour of its
  filament as a temporary appearance; after the export the original
  appearances come back.

## Printers and profiles

- **Bambuddy** (`lib/bambuddy.py`): HTTP API `/api/v1`, `X-API-Key` header.
  Redirects are refused, so the key is never sent to another host. Requests
  time out after a few seconds.
- **Printer, process and filament presets** (`lib/slicer_profiles.py`): from
  the installed OrcaSlicer or Bambu Studio, from profile files you import, or
  downloaded from OrcaSlicer's profile collection on GitHub at the tag that
  matches your installed OrcaSlicer version. Downloaded presets are
  "flattened" (their inheritance chain is merged), because the slicer's
  command line cannot resolve remote parents.

## File locations

| What | macOS | Windows |
|---|---|---|
| Settings, added filaments, imported profiles | `~/Library/Application Support/Filamatch` | `%APPDATA%\Filamatch` |
| Downloads (Open Filament Database, profiles) | `~/Library/Caches/Filamatch` | `%LOCALAPPDATA%\Filamatch\Cache` |
| Log file `filamatch.log` | `~/Library/Logs/Filamatch` | `%LOCALAPPDATA%\Filamatch\Logs` |
| Filamatch Library builds | `~/Documents/Filamatch` | `Documents\Filamatch` |
| Bambuddy API key | Keychain, service "Filamatch" | Credential Manager, "Filamatch:bambuddy_api_key" |

Nothing is written inside the add-in folder.

## Threads and responsiveness

Fusion calls add-in code on its user-interface thread. Printer queries,
profile downloads, the Open Filament Database download and the slicer run
therefore run on a worker thread (`_run_job` in `palette_bridge.py`). When the
work ends, a Fusion custom event brings the result back to the
user-interface thread, where the Fusion API and the palette may be used
again. Worker code never calls the Fusion API.

The palette also watches Fusion: after any command ends (assigning a
material, undo, redo) and when another design becomes active, it compares a
small fingerprint of the bodies and their materials and refreshes the list
only when something changed.

## The 3MF injector command-line tool

`Filamatch/lib/threemf_injector.py` works on its own with any Python 3.8+,
without Fusion:

```bash
# list the object names in a 3MF
python3 Filamatch/lib/threemf_injector.py --input part.3mf --list-objects

# assign objects to slots (1-based)
python3 Filamatch/lib/threemf_injector.py --input part.3mf \
    --mapping mapping.json --output part_slots.3mf
```

`mapping.json` maps object names to slots, for example
`{"Bracket": 1, "Bushing": 2}` ([example](examples/example_mapping.json)).
`--filaments filaments.json` also writes the filament colour, type and
density arrays. `--help` lists every option; `--self-test` runs its checks.

## Upgrading from FusionFilament Bridge

Filamatch was called FusionFilament Bridge before version 0.2.0, and until
0.3.0 it kept its data in the temp folder (which the operating system
cleans up). On the first start Filamatch:

- copies settings, added filaments and imported profiles from the old temp
  folders into the data folder above (once);
- moves the Bambuddy key to the new Keychain/Credential Manager entry;
- unloads the old "FusionFilament Library" at the next library build;
- still recognises favorites and appearances made by the old version.

The installers move an old `FusionFilamentBridge` add-in folder to the
Trash / Recycle Bin, so Fusion does not load both.
