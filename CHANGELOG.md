# Changelog

## 0.7.0 – Filament presets and printer nozzles

**Fixed**
- Bodies that touch now keep their exact places in a prepared project.
  The slicer's "assemble" step centred every body on its own middle, so a
  part could move: in a real design an inlay moved 1 mm up inside its body,
  and four cubes in a row ended up in one spot. Filamatch now joins touching
  bodies into one object with one part per body before the slicer runs, so
  every part stays where it is in Fusion and keeps its filament. Checked
  with OrcaSlicer 2.5 and Bambu Studio 2.9.1 (an inlay design and stacked
  test cubes). The inlay design was opened in Bambu Studio for a real H2D
  with no problem seen; not yet opened in OrcaSlicer.
- A prepared project now uses the slicer preset of the spool that is
  loaded, not the one the design names. A design with Bambu ABS, sent to a
  slot with PolyLite ABS, gets "PolyLite ABS @BBL H2D", so Bambu Studio's
  sync window shows the same filament on both sides. Only when the spool is
  the same material type. Checked with the Bambu Studio 2.9.1 command line;
  not yet opened in the app.
- The print estimate counts every plate. Bambu Studio puts bodies on plates
  of their own when one filament's bed is too hot for another (ABS at
  90 °C and PETG); the estimate showed only plate 1's time and weight but
  every plate's grams per filament ("3.6 g of filament (3.6 g + 3.0 g)").
  It now adds the plates up and says "on 2 plates". Checked with the Bambu
  Studio 2.9.1 and OrcaSlicer 2.5 command lines and seen working in Fusion.

**New**
- The printer's nozzles. Filamatch reads the installed nozzles (size and,
  over the LAN connection, Standard or High Flow) and shows them under
  Print settings ("Printer nozzles: Left 0.4 mm High Flow · Right 0.4 mm
  High Flow"). The ready check warns when the printer settings are for
  another size and can switch to the right printer settings. A prepared
  project gets each nozzle's flow type, as Bambu Studio's printer sync
  does. Checked with a real H2D (two 0.4 High Flow nozzles, seen working in
  Fusion) and X1C over the LAN, and with the Bambu Studio 2.9.1 and
  OrcaSlicer 2.5 command lines (Bambu Studio slices with High Flow). Over
  Bambuddy only the size is known, so the flow type is left as the printer
  settings have it.
- Slicer presets for filaments from the Open Filament Database. When the
  slicer has no preset for such a filament, Filamatch makes one from the
  slicer's generic preset for that type and printer, with the database's
  temperatures (the middle of a range), bed temperatures, fan speeds,
  fastest flow, flow ratio and brand, and the project carries it (for
  example "addnorth ABS rABS @Bambu Lab H2D 0.4 nozzle" at 245 °C). Filaments
  added earlier get the data from the downloaded database. A new setting,
  **Save new filament presets in the slicer** (off by default), also saves
  it as your own preset in Bambu Studio or OrcaSlicer, never over one you
  have. A slicer preset counts as the filament only when it is the same
  brand, so a Voxel PETG gets its own "Voxel PLA PETG" preset, not Bambu
  PETG Basic. A spool set on the printer to a preset you made yourself (in
  either slicer) now gets that preset. Checked with the Bambu Studio 2.9.1
  and OrcaSlicer 2.5 command lines (prepare and estimate). A Voxel PETG
  from the database was sent from Fusion and opened in Bambu Studio with
  its own preset, and saving put it in Bambu Studio's and OrcaSlicer's
  user presets. Bambu Studio shows its usual "Customized Preset" note for a
  preset it does not ship; whether the note stays away once the preset is
  saved is not known. Not yet confirmed in the OrcaSlicer app.
- On Home, a filament row says which filament is loaded when its slot holds
  another one than the design names ("≠ Loaded: PolyLite ABS"). Point at it
  for the details. Colours are not compared here: the two swatches show
  them. Checked in the palette's demo; not yet seen in Fusion.
- **Save printer report** (Settings > Advanced): saves the connected
  printer's status as a JSON file with serial numbers, access codes, IP
  addresses, Wi-Fi names, keys and camera links removed, to attach to a
  printer-support issue. Checked with test data and in the palette's demo;
  not yet run against a real printer.
- A bug-report form on GitHub, and [docs/triage.md](docs/triage.md): how an
  issue becomes a fix and when a printer counts as tested.
- For developers: a slicer test matrix (`scripts/slicer_matrix.py`) that
  prepares and estimates every supported printer with the real slicers,
  checks that touching parts stay in place, and compares the results with
  the last release (releases include its table); printer facts in one data
  file (`Filamatch/data/printers.json`) that also writes the README's
  printer tables; a linked development install
  (`scripts/install_addin_macos.sh --dev`); `tests/run_all.py --changed`.

**What is tested:** a real Bambu Lab H2D (two 0.4 High Flow nozzles, AMS 2
Pro, AMS HT) and X1 Carbon over the LAN; Fusion on macOS; Bambu Studio
2.9.1 and OrcaSlicer 2.5 (their command lines for every change, the Bambu
Studio app for the made preset and the inlay design); the slicer test
matrix against 0.6.1 (in the release notes). Not tested: Windows, a made
preset in the OrcaSlicer app, Save printer report from the palette on a
real printer, and printing.

## 0.6.1 – Snapmaker U1

**New**
- Snapmaker U1 through OrcaSlicer: Send to Orca prepares the project with
  each body on its tool, and the estimate works, for the 0.4 nozzle set and
  the mixed 0.4 + 0.6 set. The 0.4 set gets Snapmaker's own filament
  presets; the mixed set gets OrcaSlicer's generic presets, because
  Snapmaker's presets list only the 0.4 U1.

**Fixed**
- Printers with mixed nozzle sizes: OrcaSlicer 2.5 refused them ("Bridge
  line width must not exceed nozzle diameter"), because it works out its
  default bridge width from the largest nozzle but checks it against the
  smallest. Filamatch sets the bridge width to the smallest nozzle.

**What is tested:** checked with OrcaSlicer 2.5's command line only (the
four-cube test file: prepare, then slice for the estimate). Not tested:
opening the U1 projects in the OrcaSlicer app, the Snapmaker Orca app,
reading the U1's loaded spools, and printing.

**Nothing else changed:** the same eleven printer and slicer cases as in
0.6.0 (Bambu Lab H2D, X1 Carbon, H2C, H3C and X2D in Bambu Studio 2.9.1 and
OrcaSlicer 2.5; Prusa XL 5T, CORE One INDX 4T, MK4 MMU3 and the U1 with
0.4 nozzles in OrcaSlicer 2.5) give the same project settings and the same
estimates with 0.6.0 and 0.6.1. In both versions OrcaSlicer refuses the
estimate for PLA with PETG on one nozzle (X1 Carbon, MK4 MMU3), as it
should. Only the mixed-nozzle U1 is new.

## 0.6.0 – multi-nozzle printers and PrusaSlicer

**New**
- Bambu Lab H2C, H3C and X2D: Send to Bambu prepares the project, and the
  print time and filament estimate works. The estimate copy counts every
  hotend of a nozzle rack (H2C: 1 + 6, H3C: 9). On the H2C and X2D each
  filament goes to the side its AMS feeds; on the H3C the slicer chooses
  the hotend.
- Prusa XL 5T, CORE One INDX (4T, 8T) and CORE One MMU3 through
  OrcaSlicer: each body stays on its tool, with Prusa's own filament
  presets, and the estimate works.
- **Send to PrusaSlicer keeps the slots.** PrusaSlicer and SuperSlicer get
  the file in their own layout, with each body on its extruder; bodies that
  touch become one object with one part per body. Your PrusaSlicer printer
  and filament settings are not changed.
- A filament is not pinned to a nozzle that cannot print it (the H2C's left
  nozzle prints no TPU); the slicer then chooses.
- OrcaSlicer with a Bambu Lab H3C is refused with a reason: OrcaSlicer 2.5
  cannot slice the H3C. Choose Bambu Studio 2.9.1 or newer.
- README: a list of tested printers, and a
  [printer support form](https://github.com/percosys/filamatch/issues/new?template=printer-support.yml)
  for owners of other printers. The plan for these printers is in
  [docs/design/multi-nozzle.md](https://github.com/percosys/filamatch/blob/main/docs/design/multi-nozzle.md).

**Fixed**
- The prime tower is placed with its brim and rib walls counted, and a
  turned tower (Prusa XL presets) is placed the right way. Before, Bambu
  Studio refused the X2D ("G-code outside of the printable area") and
  OrcaSlicer refused the Prusa XL ("Prime tower is partially outside").
- Printers from OrcaSlicer's online profiles (Prusa and the other vendors)
  get the vendor's own filament presets, for example "Prusament PLA @XL" or
  "Generic PETG @Prusa CORE One INDX". Before, they got Bambu Studio's
  presets, and OrcaSlicer showed other presets instead ("Voron Generic ABS"
  on a Prusa XL). Another brand's filament gets the vendor's Generic preset.
- When the print estimate fails, the palette shows the slicer's own reason,
  for example "Selected nozzle temperatures are incompatible" (PLA and PETG
  on one MMU3 nozzle). A slicer crash is retried twice.

**What is tested in this release**

None of the new printers was tested on a real printer. Everything below
was checked on macOS with a test file of four cubes, one filament each.

| Printer or slicer | Checked in the slicer app | Checked with the slicer's command line |
|---|---|---|
| Bambu Lab H2C | Bambu Studio 2.9.1: opens with Filamatch's nozzle choice ("Custom" grouping), slices (4 h 48 min, 79.5 g) | Prepare and estimate in Bambu Studio 2.9.1 and OrcaSlicer 2.5 |
| Bambu Lab H3C | Bambu Studio 2.9.1: opens with its 9 nozzles, slices (1 h 10 min, 29.5 g) | Prepare and estimate in Bambu Studio 2.9.1; OrcaSlicer is refused |
| Bambu Lab X2D | Bambu Studio 2.9.1: opens with Filamatch's nozzle choice; not sliced in the app | Prepare and estimate in Bambu Studio 2.9.1 and OrcaSlicer 2.5 |
| Prusa XL 5T | OrcaSlicer 2.5: opens with Prusament presets, slices (1 h 41 min, 35.1 g, same as the estimate) | Prepare and estimate in OrcaSlicer 2.5 |
| Prusa CORE One INDX 4T / 8T | Not opened in the app | Prepare and estimate in OrcaSlicer 2.5; the 8T estimate often fails because OrcaSlicer crashes |
| Prusa CORE One MMU3 | Not opened in the app | Prepare and estimate in OrcaSlicer 2.5 with PLA only; PLA with PETG is refused by the slicer, as it should be |
| PrusaSlicer layout | PrusaSlicer 2.6.0: one object with its four parts in place | PrusaSlicer 2.6.0 slices with tools T0 to T3 |

Not tested yet:
- Reading the loaded spools of the H2C, H3C and X2D (LAN-only mode or
  Bambuddy), printing on any of the new printers, and the spool check on
  them.
- The X2D's slot labels: Bambu Studio calls its nozzles "Main" and
  "Auxiliary"; the palette may show "(L)" and "(R)" the wrong way round.
  The nozzle choice itself follows the printer's own extruder numbers.
- PrusaSlicer 2.9 and SuperSlicer; filament presets of online vendors other
  than Prusa; the new features on Windows.

Unchanged and still validated on real printers: the Bambu Lab H2D (full
flow) and the X1 Carbon (loaded spools). Their prepared projects and
presets are the same as in 0.5.1.

## 0.5.1 – print estimate and spool check

**New**
- Print time and filament after sending: once the slicer has opened,
  Filamatch slices a copy in the background and shows, under the send
  button, for example "About 43 min and 6.9 g of filament (2.6 g + 4.3 g)".
  Your file is not changed. On a Bambu Lab H2D the copy names each nozzle,
  which the slicer otherwise reads from the printer.
- Spool check: with the estimate, each filament is compared with the grams
  left on its spool (Bambuddy, or the printer for Bambu RFID spools). A
  warning shows when a spool has less than the estimate plus 10%.
- Several spools of the same filament: Filamatch picks the fullest one, and
  the spool check counts the others as backups. It reads the printer's AMS
  auto refill setting and warns when the print needs a backup spool but
  auto refill is off.
- Settings > Export: turn the print estimate and spool check off on a slow
  computer. The background slice takes about 1 to 5 seconds on an M4 Mac.

**Fixed**
- The exact filament type: a filament such as Bambu ABS-GF was written as
  plain "ABS", because the type came from the catalog. The preset's own type
  is now kept when it is the same filament or a variant of it ("ABS-GF" for
  "ABS"), so the slicer and the printer match AMS slots by the right type.

## 0.5.0 – send to the slicer you pick

**New**
- The send button names the slicer you pick: **Send to Orca**, **Send to
  Bambu**, **Send to PrusaSlicer**. Settings > Connections > Change lists the
  installed slicers. Other slicers get the plain 3MF, because they cannot
  open prepared projects.
- Bambu Studio prepares projects by itself: OrcaSlicer is no longer needed
  to send a Bambu Lab printer's project to Bambu Studio. The file carries
  your own Bambu Studio version and opens without the "Some values have been
  replaced" notice. For printers outside Bambu Studio's own list, OrcaSlicer
  prepares the project and Filamatch writes Bambu Studio's words for the 4
  settings that OrcaSlicer words its own way.
- Slot check: after every export, Filamatch checks that each body uses the
  slot and the colour that it has in Fusion. If one does not, the export
  stops before the slicer opens, and the message names the body and the slot.
- Printers with two nozzles (Bambu Lab H2D): each filament now uses the
  nozzle that its AMS slot feeds. Before, the slicer chose the nozzles
  itself ("Auto For Flush") and could put a filament on the other nozzle.
  When a slot's nozzle is not known (for example an AMS on a filament track
  switch), the slicer still chooses. Tested in Bambu Studio 2.9.1; OrcaSlicer
  2.5 keeps the choice.

**Fixed**
- Purge volumes fit the real filament colours. The slicer works out the
  purge volumes from the filament presets' colours; Bambu Studio's presets
  have none, so OrcaSlicer used placeholder colours (orange and white) and
  Filamatch set the real colours afterwards. Each filament preset now
  carries its colour (black to green: 591 mm³ instead of 525).
- Send to Orca works again after an update to Bambu Studio 2.9. OrcaSlicer
  2.5 refused the printer and filament presets of Bambu Studio 2.9 ("Vendor
  bundle contains invalid presets"). Filamatch now merges each Bambu Studio
  preset with its parent presets and gives OrcaSlicer the merged copy.
- Bambu Studio 2.9 showed both H2D nozzles as "High Flow" in a Send to Orca
  file. Each nozzle now gets its own flow type ("Standard" by default).

**Docs**
- The user guide, How it works and the README describe the slicer choice,
  who prepares the project, and the new checks. Tested on macOS with
  OrcaSlicer 2.5 and Bambu Studio 2.9.1 on a Bambu Lab H2D.

## 0.4.0 – direct Bambu printer connection

**New**
- Read Bambu Lab printers directly on your network, without Bambuddy:
  Settings > Printer > **Bambu printer (LAN)**, with the IP address and the
  access code from the printer screen. Tested in LAN-only mode; developer
  mode is not needed. Cloud mode on the same network should also work, but
  it is not tested yet.
  - AMS, AMS 2 Pro, AMS HT and the external spool. On an H2D the slot labels
    show the nozzle: "AMS 1 - T1 (L)", "External (R)".
  - Filament names come from the installed Bambu Studio and OrcaSlicer
    presets, your own presets included.
  - Several printers at once; one printer that does not answer is a warning.
  - The printer certificate is checked against Bambu Lab's CAs. A printer
    with another certificate can be trusted once (its fingerprint is pinned).
  - Access codes are stored in the Keychain or the Credential Manager.
  - Read-only: Filamatch sends only "send your status" and "list your
    modules".
- The welcome screen offers "Connect a Bambu printer".
- Bambuddy: every loaded slot is listed, read from the printer's live
  status. Before, only slots with a Bambuddy inventory spool linked were
  listed, so loaded slots were missing. A linked spool still adds grams left
  and the preset. Bambuddy and the direct connection now show the same slots.
- Diagnostics show the number of LAN printers and whether the Bambu CA file
  is there.
- User guide: how to add a Bambu printer, and troubleshooting for printer
  connections.

**Fixed**
- A wrong access code or an offline server wrote a long error trace in the
  log. It is now one line.
- Settings > Advanced showed an old "Library … not loaded" warning from
  version 0.1. It was wrong: Filamatch builds the Filamatch Library itself.
  The real library and Favorites status stays in Settings.

## 0.3.0 – first public release

**New**
- Slow work runs in the background: printer queries, profile lists and
  downloads, the Open Filament Database download and the slicer run no longer
  freeze Fusion.
- The palette opens as a floating window that fits each screen.
- Windows: the Bambuddy API key is stored in the Credential Manager.
- Bambuddy over HTTPS with a self-signed certificate (Settings).
- Remove the saved Bambuddy API key from Settings.
- The diagnostics report also lists the data folders, the secret store and
  the printer source.
- Windows installer script and a release builder.

**Changed**
- Project layout follows Autodesk's add-in template (`config.py`,
  `commands/`, `lib/`, `data/`). The modules load under one package name, so
  they cannot clash with other add-ins.
- Settings, added filaments and imported profiles moved out of the temp
  folder (which the operating system cleans up) into the user's data folder.
  Existing data is copied over once.
- Shorter network timeouts; the Bambuddy API key is never sent on a redirect.
- Appearances made by Filamatch are named "Filamatch …" (old "FFB …" names
  are still recognised).

**Fixed**
- Log lines were repeated once for every restart of the add-in.
- A body in a component used several times could keep the temporary export
  colour after an export.
- Re-importing a filament after a Fusion restart could recolour the wrong
  appearance.
- A failed favorite replacement or relink could leave a material renamed or
  bodies on a placeholder material.
- A crash while saving could empty the settings file; an unreadable settings
  file is now kept instead of overwritten.
- Exports longer than 20 seconds showed a false timeout.
- Unticking "open in Orca" was ignored when the setting had not been saved
  yet; an empty slicer path could erase the saved one.
- A half-typed API key could be saved by a background refresh.
- Many smaller fixes from a full code review: see the git history.

## 0.2.0

- Renamed from FusionFilament Bridge to Filamatch; new icon.
- New palette: one Home screen with **Send to Orca**, an Add filaments page
  (printer, catalog, online search), Settings and a first-run welcome.
- The filament list updates by itself when materials change.
- Printer data goes through a neutral printer/slot model (ready for more
  printer brands); the Bambuddy API key moved to the macOS Keychain.
- MIT license.

## 0.1.0

- First working version (as FusionFilament Bridge): filament catalog, Fusion
  Favorites materials, Filamatch Library, Bambuddy slot matching, 3MF slot
  metadata and OrcaSlicer project preparation.
