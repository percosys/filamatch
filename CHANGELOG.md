# Changelog

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
