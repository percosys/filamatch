# User guide

Filamatch is one palette in Autodesk Fusion. Click **Filamatch** on the
**UTILITIES** tab, **ADD-INS** panel to open it. It opens as a floating window
that fits its content; you can dock it like any Fusion palette.

- [First start](#first-start)
- [Home](#home)
- [Add filaments](#add-filaments)
- [Settings](#settings)
- [Which slicers can open the files](#which-slicers-can-open-the-files)
- [Troubleshooting](#troubleshooting)
- [Known limits](#known-limits)

## First start

When nothing is set up yet, Filamatch shows a short welcome:

1. **Connect Bambuddy.** Enter the address of your Bambuddy server (for
   example `http://192.168.1.20:8000`) and an API key created in Bambuddy.
   Filamatch then reads your printers and the spools in their AMS slots.
2. **Or skip.** You can still add filaments from the catalog. Without a printer
   connection, an export puts the filaments in slots 1, 2, 3… in the order
   they appear in the design. Connect later in Settings.

## Home

![Home](images/home.png)

**Top bar.** The printer pill shows the selected printer and whether it is
reachable. The gear opens Settings.

**This design.** One row for each filament used in the open design:

- the filament's colour, name and how many bodies use it;
- the printer slot it is matched to, with that slot's colour;
- a badge: **✓** the slot holds this filament, **≈** a close colour,
  **Not loaded** no slot holds it.

Click a slot to choose another one; your choice is remembered for that
filament. Rows that need attention are listed first.

The list updates by itself when you assign a material, undo, redo or switch
designs. The **↻** button reloads it by hand.

**Links.** **+ Add filaments** opens the add page. **Open Material Browser**
opens Fusion's Physical Material browser, where you assign the filaments to
bodies.

**Print settings.** One line with the printer, process and plate. **Change**
opens the printer, process and plate lists. They come from your installed
OrcaSlicer or Bambu Studio, or from OrcaSlicer's online profile collection.

**Ready check and Send to Orca.** Above the button you see "Ready to send" or
a short list of problems, each with a button that fixes it. Warnings never
block sending; only real blockers do (no open design, no filament assigned,
no slicer found). **Send to Orca**:

1. exports the design as a 3MF with each body assigned to its slot;
2. prepares a complete project for the chosen printer and process;
3. adds a small slot record (which spool is in which slot);
4. opens the file in OrcaSlicer.

The line below the button shows where the file is saved; **Change** picks
another place.

## Add filaments

![Add filaments](images/add.png)

Three tabs. Everything you add becomes a Fusion physical material with the
filament's colour and density, saved in your Fusion **Favorites** (available
in every design) and in the **Filamatch Library**.

- **On printer:** the spools loaded in the selected printer. Filaments that
  are already in Fusion are shown but skipped.
- **Catalog:** filaments that ship with Filamatch plus everything you added
  before. Search by name, filter by material.
- **Search online:** search the community Open Filament Database (thousands
  of filaments with colours, densities and temperatures). The first search
  downloads the database (about 16 MB); later searches also work offline.

Select filaments and click **Add to Fusion**. You return to Home.

## Settings

![Settings](images/settings.png)

- **Bambuddy:** address and API key. **Edit** changes them; **Remove key**
  deletes the saved key. Turn off **Verify the HTTPS certificate** only for a
  Bambuddy on your own network with a self-signed certificate.
- **OrcaSlicer:** where the slicer is installed (found automatically when it
  is in the usual place).
- **Export folder:** where **Send to Orca** saves files.
- **Fusion library:** keep the Filamatch Library up to date after adding
  filaments, or rebuild it now.
- **Advanced:**
  - export only the 3MF without opening the slicer;
  - your own OrcaSlicer profile folder, or import profile files;
  - **Run diagnostics:** a report of versions, file locations and checks.
    Paste it into a bug report.
  - the session log.

## Which slicers can open the files

Tested with Filamatch 0.2 on macOS:

| File | OrcaSlicer | Bambu Studio | PrusaSlicer / SuperSlicer |
|---|---|---|---|
| **Send to Orca** (prepared project) | Opens with slots | Needs version 2.8 or newer | Does not open |
| **Export only** (Advanced) | Opens with slots | Opens with slots | Opens, but slots are not kept |

PrusaSlicer support is on the [roadmap](roadmap.md).

## Troubleshooting

First run **Settings > Advanced > Run diagnostics** and look for lines that
are not OK. Log files: see [file locations](how-it-works.md#file-locations).

| Problem | What to do |
|---|---|
| No Filamatch button | Check the folder is named `Filamatch` and holds `Filamatch.py` and `Filamatch.manifest`. Restart Fusion. In **Scripts and Add-Ins** (Shift+S), Add-Ins tab, Filamatch should be running. |
| The palette shows "No printer" or "0 bodies" and nothing loads | Close the palette and open it again from the button. If it stays empty, stop and run Filamatch in Scripts and Add-Ins, or restart Fusion. |
| "Bambuddy not reachable" | Check the address in Settings (including `http://` or `https://` and the port) and that Bambuddy runs. Filamatch gives up after a few seconds, so Fusion does not freeze. |
| A filament shows **Not loaded** | No slot holds that filament. Load it, or click the slot chip and pick a slot by hand. |
| Colours in the Material Browser look white or grey | Fusion draws the material preview once and cannot redraw it through its API. The model, the palette and the export use the right colour. Editing the appearance once in Fusion refreshes the preview. |
| `Template material ... was not found` | Filamatch copies a stock Fusion material. Make sure the *Fusion Material Library* is available in the Material Browser. |
| Printer preparation failed | The tagged 3MF is still saved (the error names the file). Check that OrcaSlicer is installed and that the printer and process exist in **Print settings > Change**. |
| The slicer refuses to slice mixed materials | A slicer safety rule for very different print temperatures. Turn on the slicer's option to allow mixed temperatures, or print the materials separately. |
| The profile list is empty | The online profile list needs internet. Installed OrcaSlicer/Bambu Studio profiles work offline, and you can import your own profile files in Advanced. |
| An unwanted favorite | Fusion's API cannot remove favorites. Remove it in Fusion: Physical Material > Favorites > right-click > Remove. |

## Known limits

- One filament per body. Painting faces of one body in different colours is
  not supported.
- Up to 16 different filaments in one design (the slicer's limit).
- The Filamatch Library is a snapshot of your Fusion Favorites; Fusion's API
  cannot create library categories.
- Bodies that are not solid (surfaces) are not printed and are left out.
- Material previews in Fusion's Material Browser can show a stock swatch (see
  Troubleshooting).
