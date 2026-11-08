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

1. **Connect a Bambu printer.** Filamatch reads the printer directly on your
   network. See [Add a Bambu printer](#add-a-bambu-printer).
2. **Or connect Bambuddy.** Enter the address of your Bambuddy server (for
   example `http://192.168.1.20:8000`) and an API key created in Bambuddy.
   Filamatch then reads your printers and every loaded slot. A slot with a
   Bambuddy inventory spool linked also shows that spool (grams left,
   preset).
3. **Or skip.** You can still add filaments from the catalog. Without a printer
   connection, an export puts the filaments in slots 1, 2, 3… in the order
   they appear in the design. Connect later in Settings.

### Add a Bambu printer

You need two things from the printer screen: the **IP address** and the
**access code**. Both are on the network page of the printer settings
(**Network** or **WLAN**, depending on the model).

Filamatch is tested with printers in LAN-only mode. Developer mode is not
needed. Cloud mode should also work, because Bambu still allows other apps
to read the printer status, but it is not tested yet. If a printer in cloud
mode does not answer, please [open an issue](https://github.com/percosys/filamatch/issues).
The computer must be on the same network as the printer.

1. Open **Settings > Printer**, choose **Bambu printer (LAN)** and click
   **Add printer**.
2. Enter the IP address and the access code. Click **Test**. Filamatch shows
   the model and what is loaded in each AMS.
3. Give the printer a name (optional) and click **Save**. Add more printers
   the same way.

Filamatch checks that the printer has a certificate from Bambu Lab. If it
does not, Filamatch shows the certificate fingerprint and asks you to trust
it (**Trust this printer**). Trust it only if the IP address is your printer.

Filamatch only reads from the printer. It asks for a full status at most
every 20 seconds (every 5 minutes for P1 and A1 printers, which slow down when
asked more often).

## Home

![Home](images/home.png)

**Top bar.** The printer pill shows the selected printer and whether it is
reachable. The gear opens Settings.

**This design.** One row for each filament used in the open design:

- the filament's colour, name and how many bodies use it;
- the printer slot it is matched to, with that slot's colour;
- a badge: **✓** the slot holds this filament, **≈** a close colour,
  **Not loaded** no slot holds it;
- **≠ Loaded: …** under the name when the slot holds another filament than
  the design names, for example "≠ Loaded: PolyLite ABS" for a Bambu ABS
  design. Point at it for the details.

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
Below it, the nozzles the printer reports, for example "Printer nozzles:
Left 0.4 mm High Flow · Right 0.4 mm High Flow". When the printer
settings are for another nozzle size, the ready check says so and offers
**Use 0.6 mm** (or **Change** when the list has no printer of that size).
A prepared project gets the flow type of each installed nozzle (Standard
or High Flow), like Bambu Studio's printer sync, so the slicer and the
estimate use the right speeds. The direct LAN connection reports size and
flow; Bambuddy reports the size only.

**Ready check and Send to Orca.** Above the button you see "Ready to send" or
a short list of problems, each with a button that fixes it. Warnings never
block sending; only real blockers do (no open design, no filament assigned,
no slicer found). The button names the slicer you pick in Settings, for
example **Send to Orca** or **Send to Bambu**. It:

1. exports the design as a 3MF with each body assigned to its slot;
2. prepares a complete project for the chosen printer and process, with the
   slicer you picked (Bambu Studio prepares Bambu Lab printers itself; for
   other printers it needs OrcaSlicer installed);
3. adds a small slot record (which spool is in which slot);
4. opens the file in the slicer you picked.

**Which filament preset the project gets.** For each filament, in this
order:

1. the slicer's own preset of the filament the design names, or the preset
   of the spool loaded in its slot when that spool is the same type (a
   spool set to a generic preset does not replace a design filament with a
   brand);
2. a preset you made yourself in Bambu Studio or OrcaSlicer with that name
   (for example the one a spool is set to on the printer);
3. for a filament from the Open Filament Database: a preset Filamatch makes
   from the slicer's generic preset for that type and printer, with the
   database's temperatures, fan speeds, fastest flow and brand. Where the
   database gives a range, Filamatch uses the middle (230–260 °C becomes
   245 °C). The project carries this preset; the slicer says it is a
   "customized preset" when it opens the file;
4. else the slicer's generic preset for the type.

**Send to PrusaSlicer** (PrusaSlicer 2.9.7 or newer) prepares a PrusaSlicer
project the same way: Print settings lists PrusaSlicer's own printers and
prints (**My presets (PrusaSlicer)** first, the printers, prints and
filaments you saved in PrusaSlicer yourself; then Prusa Research and the
other vendors PrusaSlicer ships),
each body goes on its tool, and every tool gets a filament preset that fits
the printer: the design's own filament, or one made from the Open Filament
Database, else the generic preset for its type (spare tools get the
printer's default filament). The bodies move together to the middle of
the bed, keeping their places to each other, with their lowest point on
the bed, and the wipe tower (the block that cleans the nozzle between
filaments; it is not in the object list) goes beside them. Print settings re-reads
PrusaSlicer's presets every time you open it, so a printer you just saved
in PrusaSlicer is in the list. Filamatch builds the full settings from
PrusaSlicer's presets in a settings folder of its own, so your PrusaSlicer
settings are not changed. A design with more filaments than the printer
has tools is refused. After sending, the palette shows the print time and
grams; PrusaSlicer's own time is sometimes broken for MMU3 printers, and
then only the grams are shown.

Other slicers cannot open a prepared project, so they get the plain 3MF.
SuperSlicer, and PrusaSlicer with **Advanced > Prepare** turned off, get it
in PrusaSlicer's layout, so each body keeps its extruder and your printer
and filament settings in the slicer stay as they are.

The line below the button shows where the file is saved; **Change** picks
another place.

After a prepared send, the palette shows the print time and filament under
the button, for example "About 43 min and 6.9 g of filament (2.6 g + 4.3 g)".
Filamatch works it out by slicing a copy in the background after the slicer
has opened. The numbers come from the slicer, so they match what the slicer
shows when you slice. When the project has more than one plate, the line
says so ("on 2 plates") and the numbers are for all plates: Bambu Studio
puts bodies on plates of their own when one filament's bed is too hot for
another, for example ABS and PETG.

Below it, the spool check compares each filament with the grams left on its
spool (from Bambuddy, or from the printer for Bambu RFID spools). It warns
when a spool has less than the estimate plus 10%, for example "Bambu Lab -
ABS-GF - Black may run out: needs 2.5 g, 1.0 g left (AMS 2 - T4 (R))".
Spools whose weight is not known are left out.

Several spools of the same filament (same colour and type, on the same
nozzle) count together:

- Filamatch picks the fullest of them for the print.
- The other spools are backups. If the picked spool is too short, the
  check adds the grams on the backups.
- A Bambu Lab AMS only switches to a backup spool when **AMS auto refill**
  is on in the printer settings. Filamatch reads this setting from the
  printer and says if it is on or off. When it is off, or not known, the
  line is a warning.

On a slow computer, turn this off in **Settings > Export**.

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

- **Printer:** where the printer slots come from.
  - **Bambu printer (LAN):** the printers you added, each with name, model
    and IP address. **Add printer** adds one; **Remove** forgets a printer and
    its access code.
  - **Bambuddy:** address and API key. **Edit** changes them; **Remove key**
    deletes the saved key. Turn off **Verify the HTTPS certificate** only for
    a Bambuddy on your own network with a self-signed certificate.
- **Slicer:** **Change > Send to** lists the slicers installed on your
  computer; **Other app…** picks any other slicer. The send button shows the
  name of the slicer you pick.
- **Export folder:** where the send button saves files.
- **Print time, filament and spool check after sending:** on by default.
  It slices a copy in the background (about 1 to 5 seconds on a fast Mac,
  longer on a slow computer); turn it off there.
- **Save new filament presets in the slicer:** off by default. With it on,
  a preset Filamatch makes from the Open Filament Database is also saved as
  your own preset in Bambu Studio or OrcaSlicer (in the slicer's local
  presets, used when you are not signed in), so you can use it again. It
  shows after the slicer restarts. A preset you already have is never
  replaced.
- **Fusion library:** keep the Filamatch Library up to date after adding
  filaments, or rebuild it now.
- **Advanced:**
  - export only the 3MF without opening the slicer;
  - your own OrcaSlicer profile folder, or import profile files;
  - **Save printer report:** saves the connected printer's status as a JSON
    file in Documents/Filamatch, with serial numbers, access codes, IP
    addresses and other private values removed. Attach it to a printer
    support issue so your printer can be supported.
  - **Run diagnostics:** a report of versions, file locations and checks.
    Paste it into a bug report.
  - the session log.

## Which slicers can open the files

Tested on macOS:

| File | OrcaSlicer | Bambu Studio | PrusaSlicer / SuperSlicer |
|---|---|---|---|
| **Send to Orca / Send to Bambu** (prepared project) | Opens with slots | Opens with slots. Send to Bambu prepares with your own Bambu Studio; a file prepared by OrcaSlicer needs Bambu Studio 2.8 or newer | Does not open |
| **Send to PrusaSlicer** (prepared PrusaSlicer project) | Not for this slicer | Not for this slicer | PrusaSlicer 2.9.7: printer, print and filament settings and each body on its tool, checked with its command line (opening in the app not yet checked) |
| **Send to PrusaSlicer** without Prepare, or SuperSlicer (PrusaSlicer layout) | Not for this slicer | Not for this slicer | Opens with each body on its extruder (PrusaSlicer 2.6 and 2.9.7; SuperSlicer reads the same layout but is not tested) |
| **Export only** (Advanced) | Opens with slots | Opens with slots | With PrusaSlicer picked: the PrusaSlicer layout |

Bodies that touch open in PrusaSlicer as one object with one part per body,
so they keep their places.

## Troubleshooting

First run **Settings > Advanced > Run diagnostics** and look for lines that
are not OK. Log files: see [file locations](how-it-works.md#file-locations).

| Problem | What to do |
|---|---|
| No Filamatch button | Check the folder is named `Filamatch` and holds `Filamatch.py` and `Filamatch.manifest`. Restart Fusion. In **Scripts and Add-Ins** (Shift+S), Add-Ins tab, Filamatch should be running. |
| The palette shows "No printer" or "0 bodies" and nothing loads | Close the palette and open it again from the button. If it stays empty, stop and run Filamatch in Scripts and Add-Ins, or restart Fusion. |
| "Bambuddy not reachable" | Check the address in Settings (including `http://` or `https://` and the port) and that Bambuddy runs. Filamatch gives up after a few seconds, so Fusion does not freeze. |
| "The printer at … did not answer" | Check the IP address on the printer screen (it can change after a restart; give the printer a fixed address in your router). Check that the computer and the printer are on the same network. |
| "The printer refused the access code" | Read the access code again on the printer screen; it changes when it is refreshed there. Remove the printer and add it again. |
| "… is another printer now" | Two printers swapped IP addresses. Remove the printer and add it again with the new address. |
| One printer is missing slots, the others load | That printer did not answer. The log names it. The other printers still work. |
| A filament shows **Not loaded** | No slot holds that filament. Load it, or click the slot chip and pick a slot by hand. |
| Colours in the Material Browser look white or grey | Fusion draws the material preview once and cannot redraw it through its API. The model, the palette and the export use the right colour. Editing the appearance once in Fusion refreshes the preview. |
| `Template material ... was not found` | Filamatch copies a stock Fusion material. Make sure the *Fusion Material Library* is available in the Material Browser. |
| Printer preparation failed | The tagged 3MF is still saved (the error names the file). Check that OrcaSlicer is installed and that the printer and process exist in **Print settings > Change**. |
| "Filament assignments do not match Fusion" | Filamatch stopped the export because a body would print with another filament than in Fusion. The message names the body and the slot. Click Refresh and export again. If it happens again, run diagnostics and report it with the message. |
| The slicer refuses to slice mixed materials | A slicer safety rule for very different print temperatures. Turn on the slicer's option to allow mixed temperatures, or print the materials separately. |
| The profile list is empty | The online profile list needs internet. Installed OrcaSlicer/Bambu Studio profiles work offline, and you can import your own profile files in Advanced. |
| An unwanted favorite | Fusion's API cannot remove favorites. Remove it in Fusion: Physical Material > Favorites > right-click > Remove. |

## Known limits

- Direct printer connections: Bambu Lab printers only, on the same network
  as the computer. A printer that is only reachable through the Bambu cloud
  is not supported.
- One filament per body. Painting faces of one body in different colours is
  not supported.
- Up to 16 different filaments in one design (the slicer's limit).
- The Filamatch Library is a snapshot of your Fusion Favorites; Fusion's API
  cannot create library categories.
- Bodies that are not solid (surfaces) are not printed and are left out.
- Material previews in Fusion's Material Browser can show a stock swatch (see
  Troubleshooting).
