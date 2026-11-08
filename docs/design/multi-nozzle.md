# Plan: multi-nozzle and tool-changer printers

Status (2026-11-08): the slicer side of phase 3 and the PrusaSlicer layout
of phase 5 are built in 0.6.0 (see the changelog for what is tested). The
rest is a plan.

This note goes with [printer-connections.md](printer-connections.md),
which covers how Filamatch reads printers. This note covers what Filamatch
does with nozzles and tools: matching, the project file, the estimate and the
spool check.

## 1. Goal

- Support the common printers that have more than one nozzle: Bambu Lab
  H2D, X2D, H2C and H3C, Prusa XL and CORE One INDX, Snapmaker U1,
  Flashforge Creator 5, Klipper tool changers and IDEX printers.
- Keep one model in the code for all of them. No printer gets its own code
  path where a shared rule works.
- Every filament prints on a nozzle that can print it, and the slicer gets
  that choice in its own file format.
- When Filamatch does not know enough, the slicer decides (as today). A
  wrong pin is worse than no pin.

Non-goals: starting prints, sending the tool map to the printer at print
start, painting (several filaments in one body).

## 2. Words

| Word | Meaning |
|---|---|
| **Filament** | A filament index in the project (1, 2, 3 ...). Each body uses one. |
| **Feed slot** | Where a spool is loaded: an AMS tray, an MMU gate, a CFS slot, an external spool holder, the spool of one tool. |
| **Extruder** | A drive path in the slicer (`nozzle_diameter` has one entry per extruder). |
| **Nozzle** (hotend, tool, head) | The part that prints. One extruder can carry a rack of several nozzles. |
| **Reach** | The nozzles a feed slot can feed. Fixed on most printers; "any" with a filament switch. |

Filamatch already matches each design material to a feed slot. The new part
is the second step: feed slot -> nozzle -> what to write for the slicer.

## 3. Printer families

| Family | Printers | Filament -> nozzle | Slicer file |
|---|---|---|---|
| **A. One nozzle, many feed slots** | Bambu Lab X1, P1, A1, P2S, H2S, A2L with AMS; Prusa MK4S / CORE One with MMU3; Creality CFS; Anycubic ACE; Elegoo Canvas; QIDI Box; Flashforge AD5X; Klipper Happy Hare and AFC | All filaments on one nozzle | Extruder per part only |
| **B. Tool = filament index** | Prusa XL (1T, 2T, 5T presets), CORE One INDX (4T, 8T), Snapmaker U1 (4), Flashforge Creator 5 (4), Klipper tool changers, IDEX (2) | Filament *n* prints on tool *n*; one spool per tool | Extruder per part = tool |
| **C. Two extruders, explicit map** | Bambu Lab H2D, H2D Pro, X2D | Each filament gets a side; each AMS feeds one side | Extruder per part + `filament_map` |
| **D. Nozzle rack** | Bambu Lab H2C (fixed left nozzle + right rack of 6), H3C (one extruder, 9 hotends), Creality K3 (4 nozzles, not confirmed) | Side by map; the hotend in the rack is chosen by the slicer and the printer | `filament_map` + nozzle grouping keys |

### 3.1 Facts per family

**C. H2D / X2D** [Bambu Studio 2.9.1 presets and source]
- Slicer extruder index 0 is the **left** extruder. `physical_extruder_map`
  `["1","0"]` turns a slicer index into the printer's MQTT extruder id
  (MQTT 0 = right/main, 1 = left).
- `filament_map`: "1" = left, "2" = right. `filament_map_mode` values:
  "Auto For Flush", "Auto For Match", "Manual", "Nozzle Manual",
  "Auto For Quality" (new, for mixed extruder types such as the X2D).
- X2D: left extruder direct drive, right extruder Bowden
  (`extruder_type` ["Direct Drive","Bowden"]).
- Each AMS feeds one side: bits 8-11 of the AMS `info` (0xE = Filament Track
  Switch, reaches both). External spools: `vir_slot` 255 right, 254 left.

**D. H2C** [presets, source, ha-bambulab mock]
- `extruder_max_nozzle_count` ["1","6"]: the right extruder carries the
  rack. MQTT extruder 0 (right) is the rack, extruder 1 the fixed left
  hotend. Rack nozzle ids 16-21 in `device.nozzle.info`.
- `unprintable_filament_types` ["TPU","PPS-CF,PPA-CF"] (per extruder).
- `filament_map` picks the side. The rack hotend is picked by the slicer's
  groups (`filament_nozzle_map` is a group label, not a hotend) and then by
  the printer when the job is sent (`nozzle_mapping` in the print command).

**D. H3C** [presets and source; Bambu Studio 2.9.1 is the first version]
- One extruder, 9 hotends: `nozzle_diameter` 1 entry,
  `extruder_max_nozzle_count` ["9"], `handle_hotend_as_extruder` "1",
  `default_nozzle_volume_type` 9 entries (the 8th is "TPU285", for 2.85 mm
  filament).
- With `handle_hotend_as_extruder`, each hotend counts as its own tool for
  grouping and purging. `filament_map` is all "1"; the default mode is
  "Auto For Match".
- The project **cannot** pin a filament to one of the 9 hotends. Manual mode
  only chooses "AMS-fed nozzle" or "fixed nozzle" per filament, plus the
  flow type (`filament_volume_map`) and diameter (`filament_diameter_map`).
- AMS 3H: AMS type 6 ("AMS-3H"); **slot N feeds hotend N** (fixed). The
  TPU285 tool takes an external spool. Extra AMS units feed through tool 1.
- OrcaSlicer 2.5 has no H3C profile (Orca issue #17218).

**B. Prusa** [PrusaSlicer 2.9.7 source, Prusa settings bundle 2.5.14]
- XL presets exist for 1T, 2T and 5T only. INDX presets: 4T and 8T, HF0.4,
  from PrusaSlicer 2.9.6 by online profile update.
- Tool changer: `single_extruder_multi_material` = 0. MMU3: 1.
- Preset compatibility checks only `nozzle_diameter[0]`, so mixed nozzle
  sizes need PrusaSlicer 3.0.
- The printer can remap G-code tools to its loaded tools at print start
  (firmware 7.0+).

**B. Snapmaker U1** [u1-klipper source]
- 4 toolheads. Each part's extruder is a logical filament 1-32; the firmware
  maps logical -> physical tool (`extruder_map_table`). The default map is
  1:1.
- The firmware refills from another tool with the same colour, like a
  Bambu auto refill.

**B. IDEX**: copy and mirror modes print the same filament on both heads.
The slicer sees one tool; the grams double.

## 4. Where the code is today

| Place | What it assumes |
|---|---|
| `printer_model.SlotInfo.extruder` | One int, Bambu MQTT numbering (0 right, 1 left). |
| `bambu_lan.is_dual_nozzle` | Two or more `device.extruder.info` entries = H2D layout. A 9-entry report would count as "dual". |
| `bambu_lan.extruder_of_unit` | Only 0 and 1 are valid. |
| `bambu_lan.build_slots`, `_side` | Labels "(L)" / "(R)"; external spools only 254/255. Shared with `bambuddy.live_slots`. |
| `slot_mapping.backup_slots` | A backup must have the same `extruder`. |
| `palette_bridge._slot_nozzles` | Turns `extruder` into "left" / "right". |
| `slicer_profiles.apply_nozzle_map` | Writes `filament_map` only when the project has exactly 2 extruders. Safe on other printers (it does nothing). |
| `slicer_profiles.estimate_print` | Adds `extruder_nozzle_stats` "flow#1" per extruder when there are 2 or more; nothing for a rack. |
| `slicer_profiles._fit_nozzle_volume_types` | Pads `nozzle_volume_type` to the extruder count. Generic. |
| `slicer_profiles._fit_wipe_tower` | Uses every extruder's printable area. Generic. |
| `slicer_profiles._printer_token` | No tokens for X2D, H3C, XL, INDX. |
| `slicer_profiles.slicer_kind` | Knows only OrcaSlicer and Bambu Studio. Other slicers get the plain 3MF. |
| `threemf_injector` | Bambu/Orca layout only. `MAX_EXTRUDER` = 16 (Bambu and the U1 allow 32 filaments). |

Side finding: `bambu_lan._preset_dirs` reads filament presets from
`OrcaSlicer.app/Contents/Resources/profiles/BBL/filament`. OrcaSlicer 2.5
packs its system presets into `.opc` archives. That folder now holds only
`filament_id_map.json`, `filament_name_map.json` and two other lookup
files, so filament names come only from Bambu Studio's folder. The two maps
may be enough to name the filaments.

## 5. Design

### 5.1 Tool layout

A new data class, built once per printer and slicer preset:

```
ToolLayout
  family: "single" | "indexed" | "mapped" | "rack"
  extruders: [Extruder(index, label, kind, unprintable_types,
                       nozzles: [Nozzle(id, diameter, flow, label)])]
  physical_extruder_map: slicer index -> printer extruder id
```

- **From the slicer preset** (always available): `nozzle_diameter` length,
  `extruder_max_nozzle_count`, `handle_hotend_as_extruder`,
  `physical_extruder_map`, `default_nozzle_volume_type`, `extruder_type`,
  `unprintable_filament_types`, `single_extruder_multi_material`
  (PrusaSlicer). Family B needs no live data at all.
- **From the printer** (when connected): mounted nozzles
  (`device.nozzle.info`: id, `type` such as "HS01", diameter), rack type
  (`device.holder.info2`), AMS -> extruder bits, AMS 3H fixed slots.
- **SlotInfo** gets `reach`: the nozzles a slot can feed. `extruder` stays
  during the change and is filled from `reach`.

Labels come from the layout: "Left" / "Right", "Rack 1-6", "Tool 1-9",
"Head 1-5". The palette shows the nozzle of each filament.

### 5.2 Matching (feed slot -> nozzle)

Filamatch keeps its material -> feed slot match. Then per family:

- **single**: nothing more. Check that all filaments suit one nozzle
  (temperature range, abrasive filament needs a hardened nozzle).
- **indexed**: filament *n* prints on tool *n*. Filamatch orders the project
  filaments by the tool of each matched slot, so the default 1:1 map is
  right. Unused tools get a placeholder filament. Two materials on one
  tool, or more materials than tools, is an error before sending.
- **mapped**: `filament_map` from `reach` (today's H2D code), through
  `physical_extruder_map` instead of the fixed left/right table. A slot that
  reaches both sides (track switch) leaves the side to the slicer.
- **rack**: pin only the side (H2C). Never pin a hotend. Keep the slicer's
  automatic mode on the H3C. Set the flow type per filament only when the
  material needs it (TPU).

New checks for every family, per filament and nozzle:
- `unprintable_filament_types` of the extruder (H2C: no TPU on the left);
- abrasive material (CF, GF) needs a hardened nozzle;
- nozzle diameter matches the process preset;
- 2.85 mm filament only on a 2.85 mm tool (H3C tool 8).

### 5.3 Writing the slicer file

| Slicer | What Filamatch writes |
|---|---|
| **Bambu Studio** | Extruder per part; `filament_map`, `filament_map_mode` and the plate `filament_maps` (family C and the H2C side); nothing that pins a rack hotend. In the estimate copy only: `extruder_nozzle_stats` and `extruder_nozzle_stats_new` from the preset, for example H2C `["Standard#1","Standard#6"]`, H3C `["Standard#8\|TPU285#1"]` (legacy key `["Standard#9"]`). The sent file keeps them empty, so the slicer reads the real nozzles from the printer. |
| **OrcaSlicer** | The same keys, without `extruder_nozzle_stats_new`, "Auto For Quality", TPU285 and `ams_nozzle_idx`. Orca stops with "No valid nozzle found" when a stats entry has no count for the extruder's `nozzle_volume_type`. |
| **Orca-based slicers** (Creality Print, ElegooSlicer, Anycubic Slicer Next, Snapmaker Orca, QIDI Studio, Flash Studio) | The same layout as OrcaSlicer. Each needs: its app and preset folders, its CLI, and its version rules (Creality Print refuses a newer project version unless `allow_newer_file` is set). |
| **PrusaSlicer** | A separate export in the Prusa layout: one mesh per object with the parts as triangle ranges, `slic3rpe:Version3mf` = 1, `Metadata/Slic3r_PE_model.config` with `extruder` per volume (1-based). `Metadata/Slic3r_PE.config` must be complete or left out (a partial file resets the missing keys). PrusaSlicer ignores the Bambu/Orca `model_settings.config`. |

PrusaSlicer prepare and estimate:
- Its CLI cannot write presets into a project 3MF. Run it with
  `--printer-profile`, `--print-profile` and one `--material-profile` per
  extruder (2.8+), `--save full.ini`, then write those `; key = value` lines
  into `Slic3r_PE.config` ourselves.
- Estimate: `--export-gcode --no-binary-gcode`, then read
  `; filament used [g] = a, b, c` (one value per extruder) and
  `; estimated printing time (normal mode)`.

### 5.4 Estimate

- Bambu and Orca: today's background slice of a copy, with the nozzle stats
  above. Grams per filament come from `slice_info.config`, which also lists
  each nozzle group.
- PrusaSlicer: as in 5.3.
- IDEX copy / mirror: grams x 2.

### 5.5 Spool check

- Units differ: grams (Bambuddy, Spoolman, Happy Hare, AFC), percent of
  the label weight (Bambu RFID, Creality CFS, Anycubic ACE), fraction 0-1
  (UltiMaker), nothing (Elegoo, Flashforge, QIDI, U1, Prusa). Convert to
  grams when a label weight is known; else skip the spool.
- Backups: same filament **and** the same reach (today: same extruder).
- Auto refill: Bambu per printer (today); U1 refills by colour; Happy Hare
  "endless spool". Each source reports what it has; unknown stays a
  warning.

### 5.6 Printer sources

The connectors are in [printer-connections.md](printer-connections.md).
The tool data each one gives:

| Source | Tools and slots | Weight |
|---|---|---|
| Bambu LAN / Bambuddy | `device.nozzle.info`, `device.holder`, AMS `info` bits, AMS 3H (type 6) | grams (Bambuddy), RFID percent |
| PrusaLink (local) | Active tool's material only; per-tool data goes to Prusa Connect only | none; use Spoolman |
| Moonraker: Snapmaker U1 | `print_task_config` per tool (type, colour, exists), `filament_detect` (RFID) | label weight only |
| Moonraker: Happy Hare / AFC | `mmu` gates, `ttg_map`; `AFC_stepper laneN`; `lane_data` | grams |
| Creality CFS | WebSocket port 9999, `boxsInfo` | percent |
| Elegoo Canvas | MQTT, `canvas_info` trays | none |
| Flashforge | HTTP 8898 `/detail`, `matlStationInfo.slotInfos` | none |

## 6. Phases

1. **Data first.** The [printer support issue form](../../.github/ISSUE_TEMPLATE/printer-support.yml)
   (done). Add **Settings > Advanced > Save printer report**: the raw
   status of the connected printer with serial numbers, access codes, IP
   addresses and the `net` block removed. Collect public mock files as test
   fixtures (ha-bambulab `MOCK-H2C.json`, Bambuddy fixtures).
2. **Tool layout, no behaviour change.** Add `ToolLayout` and
   `SlotInfo.reach`; replace "left"/"right" with nozzle references; add the
   printer tokens (X2D, H3C, XL, INDX). Fix `_preset_dirs` for OrcaSlicer
   2.5. All H2D tests must pass unchanged.
3. **Bambu slicer side** (testable now: Bambu Studio 2.9.1 has the H2C, H3C
   and X2D presets). X2D map through `physical_extruder_map`; nozzle stats
   for the estimate copy per preset; the H2C side map; H3C left on
   "Auto For Match"; the new compatibility checks. *Built:* nozzle stats
   per extruder (`_nozzle_stats`), side map through `physical_extruder_map`,
   `unprintable_filament_types` check, prime tower footprint and rotation,
   OrcaSlicer refused for the H3C. Checked with the CLI of both slicers and
   in the Bambu Studio app (H2C, H3C, X2D).
4. **Bambu printer side** (needs testers). Parse `device.nozzle.info`,
   `device.holder`, AMS 3H; reach per slot; labels.
5. **PrusaSlicer writer, prepare and estimate.** XL, INDX, MMU3. Needs
   PrusaSlicer 2.9.7 (the installed 2.6.0 is too old for INDX and XL+).
   Weights through Spoolman. *Built:* the writer (`lib/prusa_project.py`),
   used for Send to PrusaSlicer; checked with PrusaSlicer 2.6.0. Prepare and
   estimate are still open.
6. **Orca-based slicers.** Register each app, its preset folders and CLI.
7. **Other printer sources**: U1, Klipper MMU and tool changers, Creality
   CFS, Elegoo, Flashforge. These go into the phases of
   printer-connections.md.

Phases 2 and 3 need no new hardware. Phase 5 helps the largest group of
new users.

## 7. Testing

- Without hardware: slicer CLI runs with the installed presets (Bambu
  Studio for H2C/H3C/X2D, OrcaSlicer for Prusa XL and INDX), then a GUI
  open of each file; mock printer reports as fixtures.
- With hardware: one owner per printer confirms the full flow (spools,
  send, slots and nozzles in the slicer, the print). The README's tested
  list changes only after that.

## 8. Open questions

1. A real H3C report: AMS 3H unit id, `holder.info2`, nozzle ids, length of
   `extruder.info`, external spool ids.
2. Does an H3C print need a `nozzle_mapping` in the print command, and does
   the slicer always send it?
3. X2D: which side is MQTT extruder 0; its external spool ids.
4. A Bambu Studio project saved after a printer sync, for the H2C and the
   H3C (exact `extruder_nozzle_*` and `filament_nozzle_map` values).
5. Does PrusaSlicer 2.9.7 accept a Prusa-layout 3MF without
   `Slic3r_PE.config`, and what does it do with an extruder above the tool
   count?
6. Do the PrusaSlicer CLI profile flags find system presets without the
   setup wizard?
7. Does Creality Print's CLI open a Bambu-stamped project without
   `allow_newer_file`?
8. Do Creality's and Anycubic's percent values go down as filament is used,
   or show only the RFID value?
9. Does Snapmaker Orca put the U1 tool map in the G-code or send it through
   Moonraker?
10. X2D: Bambu Studio names the nozzles "Main" and "Auxiliary", not left and
    right. The map is right (it follows the printer's extruder ids), but the
    palette's "(L)" / "(R)" labels may be wrong on this printer.
11. OrcaSlicer 2.5 crashes on about half the CORE One INDX 8T slices (more
    often with fewer filaments than tools). The estimate retries twice.
12. *Fixed:* printers from OrcaSlicer's online profiles now get the
    vendor's own filament presets (`_remote_filament_candidates`); before,
    OrcaSlicer showed other presets ("Voron Generic ABS" on a Prusa XL).

## 9. Sources

- Bambu Studio v02.09.01.70: `resources/profiles/BBL/machine/*.json`,
  `src/libslic3r/PrintConfig.cpp`, `src/libslic3r/MultiNozzleUtils.*`,
  `src/libslic3r/Format/bbs_3mf.cpp`, `src/slic3r/GUI/DeviceCore/DevNozzleSystem.cpp`,
  `src/BambuStudio.cpp`. OrcaSlicer v2.5.0: `src/libslic3r/GCode/ToolOrdering.cpp`,
  `src/libslic3r/Format/3mf.cpp`, `resources/profiles/Prusa*`.
  Both are AGPL: Filamatch uses only the key names and value formats, no code.
- PrusaSlicer 2.9.7: `src/libslic3r/Format/3mf.cpp`, `src/CLI/*`,
  `src/libslic3r/GCode/GCodeProcessor.cpp`; Prusa settings bundle 2.5.14
  (github.com/prusa3d/PrusaSlicer-settings-prusa-fff); Prusa-Firmware-Buddy
  `lib/WUI/*`, `src/connect/render.cpp`; Prusa-Link-Web `spec/openapi.yaml`.
- Bambuddy (github.com/maziggy/bambuddy), ha-bambulab `MOCK-H2C.json`
  (github.com/greghesp/ha-bambulab).
- Snapmaker u1-klipper `print_task_config.py`; Flashforge API docs;
  ha_creality_ws; anycubic_ha_local; Elegoo elegoo-link SDK; Happy Hare and
  AFC Klipper objects; Spoolman `models.py`.
- SimplyPrint H3C page (simplyprint.io/compatibility/bambu-lab-h3c).
