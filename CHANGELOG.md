# Changelog

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
