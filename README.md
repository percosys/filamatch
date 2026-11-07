<p align="center">
  <img src="docs/design/icon/filamatch-icon.svg" width="96" height="96" alt="">
</p>

<h1 align="center">Filamatch</h1>

<p align="center">
  Give every body in your Autodesk Fusion design a real filament, match it to
  the spools loaded on your printer, and send the design to OrcaSlicer ready to print.
</p>

<p align="center">
  <a href="https://github.com/percosys/filamatch/releases/latest">Download</a> ·
  <a href="docs/user-guide.md">User guide</a>
</p>

<p align="center">
  <a href="https://buymeacoffee.com/percosys"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me a Coffee" height="40"></a>
</p>

![Filamatch in Fusion: the design's filaments matched to printer slots](docs/images/home.png)

## Why

A multi-colour print usually goes like this: you design in Fusion, export a
3MF, open it in the slicer, and then assign a filament to every part again,
by hand, every time. Filamatch keeps that information in the design:

1. In Fusion you give each body a filament material (for example
   *Bambu Lab · PLA Basic · Black*). The model shows the real colour.
2. Filamatch reads which filaments are loaded in your printer's AMS slots
   and matches each design filament to a slot.
3. **Send to Orca** exports the design with each body already assigned to its
   slot, prepares the printer and process settings, and opens OrcaSlicer.

## Features

**In Fusion**
- One palette with a live list of the filaments in the open design: body
  count, matched printer slot and a match badge (✓ match, ≈ close colour,
  "Not loaded"). Change a slot with one click.
- The list follows your changes: assigning a material, undo/redo and
  switching designs update it without a refresh button.
- A ready check before you send, with a fix button for each problem.

**Filaments**
- Add filaments from three places: the spools **on your printer**, the
  bundled **catalog**, or a **search** of the community
  [Open Filament Database](https://github.com/OpenFilamentCollective/open-filament-database).
- Each filament becomes a Fusion physical material with the right colour
  and density (so mass and weight estimates are correct), saved in your
  Fusion Favorites and in a "Filamatch Library" material library.

**Printer and slicer**
- Reads printers, AMS slots and spools (with grams left) from
  [Bambuddy](https://github.com/maziggy/bambuddy), a self-hosted Bambu Lab
  printer manager.
- Prepares a complete OrcaSlicer project: printer, process and filament
  presets from your installed OrcaSlicer (or Bambu Studio) or downloaded from
  OrcaSlicer's profile collection. Picks a build plate and wipe tower position
  the filaments accept.
- Opens the result in OrcaSlicer.

**Under the hood**
- Slow work (printer queries, profile downloads, the slicer run) happens in
  the background, so Fusion never freezes.
- The Bambuddy API key is stored in the macOS Keychain or the Windows
  Credential Manager, never in a plain settings file.
- A standalone command-line tool adds slot metadata to any 3MF (see
  [How it works](docs/how-it-works.md#the-3mf-injector-command-line-tool)).

## Requirements

| | |
|---|---|
| **Autodesk Fusion** | Current release, macOS or Windows. Filamatch uses Fusion's built-in Python; nothing else to install. |
| **OrcaSlicer** | For **Send to Orca**. Bambu Studio also works for project preparation. Without a slicer you can still export a tagged 3MF. |
| **Bambuddy** (optional) | To read the filaments loaded on Bambu Lab printers. Without it, an export puts the filaments in slots 1, 2, 3… in the order they appear in the design. A manual printer setup and direct printer connections are on the [roadmap](docs/roadmap.md). |

Windows support is written but has been tested less than macOS.

## Install

1. Download `Filamatch-<version>.zip` from the
   [releases page](https://github.com/percosys/filamatch/releases) and unzip it.
   You get a folder named `Filamatch`.
2. Move that folder into Fusion's add-ins folder:
   - **macOS:** `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/`
   - **Windows:** `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\`
3. Restart Fusion. Filamatch starts by itself. Its button is on the
   **UTILITIES** tab, **ADD-INS** panel.

From a clone of this repository you can run the installer instead:
`scripts/install_addin_macos.sh` (macOS) or
`powershell -ExecutionPolicy Bypass -File scripts\install_addin_windows.ps1` (Windows).

## Quick start

1. Open a design and click **Filamatch**.
2. The first time, connect Bambuddy (address and API key) or skip.
3. Click **+ Add filaments** and add the filaments you print with.
4. In Fusion, give each body one of those materials (Physical Material,
   or **Open Material Browser** in the palette).
5. Check the slots on Home, then click **Send to Orca**.

The [user guide](docs/user-guide.md) explains every screen.

## Privacy and network use

Filamatch has no account, no analytics and no telemetry. It only connects to:

- **your Bambuddy server**, at the address you enter;
- **raw.githubusercontent.com**, to download OrcaSlicer printer profiles you
  pick that are not installed locally;
- **api.openfilamentdatabase.org**, when you search online filaments.

Settings live in your user folder (see
[file locations](docs/how-it-works.md#file-locations)).

## Documentation

- [User guide](docs/user-guide.md): every screen, troubleshooting, slicer compatibility.
- [How it works](docs/how-it-works.md): architecture, 3MF metadata, file locations.
- [Roadmap](docs/roadmap.md): planned features.
- [Development](docs/development.md): project layout, tests, releases.
- [Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md)

## Support

Filamatch is free and open source. If it saves you time, you can buy me a
coffee:

<a href="https://buymeacoffee.com/percosys"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me a Coffee" height="48"></a>

Bug reports and test results from other printers help just as much: see
[CONTRIBUTING](CONTRIBUTING.md).

## License and credits

Filamatch is released under the [MIT License](LICENSE). © 2026 Percosys.

- Filament data from the [Open Filament Database](https://github.com/OpenFilamentCollective/open-filament-database) (MIT), downloaded when you search.
- Printer profiles from [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer) (AGPL-3.0), downloaded when you pick a printer that is not installed; they are not part of this repository.
- Printer data from [Bambuddy](https://github.com/maziggy/bambuddy), when you connect it.

Filamatch is an independent project. It is not made, endorsed or supported by
Autodesk, Bambu Lab or the OrcaSlicer project. Autodesk and Fusion are
trademarks of Autodesk, Inc.
