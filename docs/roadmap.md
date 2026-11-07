# Roadmap

Planned work, roughly in order. Nothing here has a date. Ideas and help are
welcome: see [CONTRIBUTING](../CONTRIBUTING.md).

## Printer connections

Today Filamatch reads loaded filaments from Bambu Lab printers, directly on
the network or through Bambuddy. The plan is to read them from more places;
the design is in [design/printer-connections.md](design/printer-connections.md).
The shared printer, spool and slot model behind it is already in place.

| Step | What you get |
|---|---|
| **Manual printer** | Set up your printer's slots by hand. Works with any printer. |
| ~~Bambu Lab direct~~ | Done in 0.4: read the AMS straight from the printer on your network (IP address and access code), without Bambuddy. |
| **Klipper / Moonraker** | Voron, QIDI, Snapmaker, Creality (Klipper), multi-material units such as Happy Hare and AFC. |
| **Creality K1 / K2 with CFS** | The Creality Filament System on stock firmware. |
| **OctoPrint, Spoolman, Prusa** | Slots where the API has them; spool inventory from Spoolman. Prusa printers fall back to Manual. |
| **Others** | A go / no-go check for Flashforge, Elegoo and Anycubic. |

## Slicers

- **PrusaSlicer and SuperSlicer:** also write the slot metadata these slicers
  read, so an exported 3MF keeps its slots there.
- **Cura and the OrcaSlicer family** (Creality Print, Elegoo Slicer,
  Snapmaker Orca, Anycubic Slicer Next): test and document.
- **Bambu Studio** as a first-class target next to OrcaSlicer.

## Palette

- A light theme to match Fusion's light UI.
- A full accessibility pass (screen readers, keyboard-only use).
- Highlight a filament's bodies in Fusion when you click its row.

## Platform and distribution

- Test the Windows build end to end (installer, Credential Manager, slicer
  detection).
- Publish on the Autodesk App Store.

## Housekeeping

- Move the module self-tests out of the shipped add-in into `tests/`, so the
  release is smaller.
- One shared helper for zip rewrites and colour handling.
