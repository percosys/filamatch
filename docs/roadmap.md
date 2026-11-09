# Roadmap

Planned work, roughly in order. Nothing here has a date. Ideas and help are
welcome: see [CONTRIBUTING](../CONTRIBUTING.md).

## Printer connections

Today Filamatch reads loaded filaments from Bambu Lab printers, directly on
the network or through Bambuddy, and any other printer can be set up by
hand (Manual). The plan is to read them from more places;
the design is in [design/printer-connections.md](design/printer-connections.md).
The shared printer, spool and slot model behind it is already in place.

| Step | What you get |
|---|---|
| ~~Manual printer~~ | Done in 0.10: set up your printer's slots by hand. Works with any printer. |
| ~~Bambu Lab direct~~ | Done in 0.4: read the AMS straight from the printer on your network (IP address and access code), without Bambuddy. |
| **Klipper / Moonraker** | Voron, QIDI, Snapmaker, Creality (Klipper), multi-material units such as Happy Hare and AFC. |
| **Creality K1 / K2 with CFS** | The Creality Filament System on stock firmware. |
| **OctoPrint, Spoolman, Prusa** | Slots where the API has them; spool inventory from Spoolman. Prusa printers fall back to Manual. |
| **Others** | A go / no-go check for Flashforge, Elegoo and Anycubic. |

## Slicers

Done: Bambu Studio prepares projects by itself (0.5), printers with several
nozzles or tools (0.6, see [design/multi-nozzle.md](design/multi-nozzle.md)),
and Send to PrusaSlicer prepares the project (0.8).

- **Cura and the OrcaSlicer family** (Creality Print, Elegoo Slicer,
  Snapmaker Orca, Anycubic Slicer Next): test and document.
- **IDEX printers** (two independent heads).
- **Body settings in more slicers:** more settings per body (layer height,
  modifiers), and seams.

## Palette

- A light theme to match Fusion's light UI.
- A full accessibility pass (screen readers, keyboard-only use).
- Spool prices from Spoolman or Bambuddy for the filament cost.

## Platform and distribution

- Test the Windows build end to end (installer, Credential Manager, slicer
  detection).
- Publish on the Autodesk App Store.

## Housekeeping

- Move the module self-tests out of the shipped add-in into `tests/`, so the
  release is smaller.
- One shared helper for zip rewrites and colour handling.
