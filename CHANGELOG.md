# Changelog

## 0.10.0 – Manual printer and more data sheets

Filamatch now works with any printer: set up its slots by hand. The
printed values know 206 filaments from 11 makers' data sheets.

**New**
- **Manual printer.** For any printer Filamatch cannot read (Prusa,
  Creality, Klipper, single-nozzle printers): **Settings > Printer > Manual**
  takes the printer's name, nozzle size and material, and which Filamatch
  filament is in each slot (with grams left if you like). **Fill from
  design** puts the open design's filaments into the slots. Home then
  matches the design to these slots, the design check knows the nozzle,
  **Use it** works, and the slicer project gets each slot's filament preset.
- **More filament data sheets:** 167 more engineering filaments from
  Prusament, addnorth, Fillamentum, Extrudr, eSun, Elegoo, Overture, Sunlu
  and 3DXTech, with a link to each sheet (206 filaments from 11 makers in
  all). The printed values use 129 of them: the ones with values per print
  direction. A sheet with values along the layers only gets its across-the-
  layers values estimated from its type (±30 %); a sheet without a tensile
  modulus takes its stiffness from the type's generic values. Safe now
  takes the weakest bar, along or across the layers. Sheets that give one
  value without a direction, often from moulded bars, are kept with their
  links but not used.

**Changed**
- A printer slot without a filament shows "Empty" in the slot list.

**Fixed**
- **Your own presets when the slicer is signed out.** Bambu Studio and
  OrcaSlicer read your own presets from one folder: your account's when
  signed in, else a "default" folder. A preset saved while signed in was
  missing when signed out, so the slicer's printer sync replaced it with a
  generic one (for example "Polymaker PPS GF" became "Generic PPS"). With
  **Save new filament presets in the slicer** on, Filamatch now copies the
  presets a project uses into the folder the slicer uses (never replacing
  one); with it off, the log says why.
- Switching the printer source (for example from Manual back to the
  Bambu printers) no longer fails with "maps to unknown Bambuddy slot". A
  slot you chose for another source is set aside, and works again when you
  switch back.
- **Use it** and adding the printer's filaments use the first printer when
  the saved printer is not in the chosen source any more (after switching
  sources).

**What is tested:** the manual printer in the module and palette self-tests,
in the palette preview (the form, Fill from design, Save, matching on Home,
the ready check, the welcome screen), and in Fusion: set up and used by
hand, and a real design's filaments matched to manual slots (automatic)
with the right slicer presets and nozzle. Switching from Manual back to
the Bambu printers was tested in Fusion (the slot error above was found
there and fixed). The preset copy was tested in Fusion with Bambu Studio
signed out: after **Send to Bambu**, Bambu Studio's sync kept "Polymaker
PPS GF". The data sheet values were typed from the sheets and spot-checked
(Prusament PA11 CF: 17 values, Overture ASA: 9, three more by the
collectors); `scripts/datasheet_docs.py` checks every record. Not tested:
a print from a manual printer.

The slicer test matrix is unchanged against 0.9.1 (24 cases the same); the
printed weight still matches the slicer within 3.3 % on average.

## 0.9.1 – Real stiffness and strength for filament materials

Fusion Simulation now gets sensible values from every Filamatch filament,
with **Fusion uses the printed values** on or off. If you added filaments
before 0.9.1, add them again (**Add filaments**) to update them.

**Fixed**
- **Filament materials have their filament's own stiffness and strength.**
  Filamatch makes a filament's Fusion material from the stock material
  nearest in colour, and it kept that material's mechanical values: a blue
  Bambu Lab ABS was 6.89 GPa stiff (real ABS is about 2.2 GPa), a black
  PAHT-CF only 0.91 GPa. Now the material gets the values of a typical
  print at 100% infill (between along and across the layers): from the
  filament's data sheet when Filamatch has it, else the generic values for
  its type. The material's description names them ("solid values: typical
  print, Bambu Lab PAHT-CF (data sheet V3.0)"). This is what Fusion
  Simulation uses while **Fusion uses the printed values** is off. A
  filament without data, or a flexible one (TPU, TPE...), keeps the stock
  values, and its description says "no solid values (...)": do not trust a
  simulation with it. Filaments you added before get the values the next
  time Filamatch adds or updates them.
- **Printed values of a filament without data.** A material with neither a
  data sheet nor a known type (for example Fusion's "Nylon 6/6") kept its
  solid stiffness and strength next to its printed density. Now its own
  values are scaled for the infill like every other body, with the warning
  "No printed test data for this filament ... Often too strong (±60%)". A
  Filamatch filament without data gets no printed stiffness and strength
  (its values are a stock material's), and neither does a flexible
  filament: a linear simulation does not fit it. A filled type with a
  number ("ABS-GF30") no longer counts as its plain type.

**New**
- `scripts/as_printed_static_check.py`: a static check of the printed values
  with the free FEA program scikit-fem (see
  [the strength model](docs/as-printed/strength-model.md#what-is-tested)).

**What is tested:** in Fusion 2706.1.17, 23 Filamatch favorites were updated:
21 got their data sheet or generic values (for example Bambu Lab ABS 6.89 →
2.08 GPa, PAHT-CF 0.91 → 3.02 GPa), the TPU and a generic ABS-GF30 were
marked "no solid values". A Static Stress study used the "as printed"
copies with exactly Filamatch's values; Fusion solves only in the cloud, so
the study was not solved. The free FEA check instead: three test parts, the
deflections within 1.5–8 % of beam theory, the values on the safe side in
bending. Module self-tests and test files pass.

## 0.9.0 – Printed weight and strength, design check

**New**
- **Printed weight and strength.** A new card on Home, above the send
  button, estimates what each filament body weighs as printed: walls, top
  and bottom layers and infill from the print settings (a body's own
  settings count). **Check with the slicer** slices each body once with
  OrcaSlicer or Bambu Studio for its exact grams.
  - The switch **Fusion uses the printed values** (off until you turn it
    on, kept per design) gives each body its own "… · as printed" copy of
    its filament, with the printed density, stiffness and strength. Fusion's
    mass, Physical Properties and Simulation then use them. A copy follows
    when its body's settings change. Turning the switch off puts the
    filaments back and removes the copies from the design.
  - Stiffness and strength come from the filament's own data sheet when
    Filamatch has it (Bambu Lab and Polymaker engineering filaments, see
    below), else from generic values for its type (the median of 29 data
    sheets from 6 makers). Choose **Safe** (across the layers, the weakest
    direction; moist values for a nylon whose sheet has them) or
    **Typical**. Fusion Simulation takes one value for every direction.
  - **Where the values come from** names each data sheet. These are rough
    estimates (±25 % from a data sheet, ±25–40 % generic), never design
    values: check important parts with a test print.
- **Filament data sheets.** `Filamatch/data/datasheets/` has the makers'
  printed-part values for 16 Bambu Lab and 23 Polymaker engineering
  filaments: stiffness, strength, elongation, bending, impact and heat
  values along and across the layers, with the test standard, how the test
  bars were printed, and a link to each maker's data sheet. The folder's
  README has the table. Errors on a sheet are named in its notes, not
  used.
- **Design check.** The ready check above the send button also warns:
  - when the design uses more filaments than the printer has slots (all
    AMS and external slots of a connected printer) or, without a printer,
    than the PrusaSlicer printer has tools;
  - when filaments cannot share one bed: a hot filament (ABS, ASA, PC, PA,
    fibre-filled ones) with a cool one (PLA, PETG, TPU, PVA). The groups are
    Bambu Studio's own; the message gives each type's bed temperature;
  - when a carbon or glass fibre filament (CF, GF) would print with a
    stainless steel or brass nozzle. The direct LAN connection and Bambuddy
    now read the nozzle material from the printer;
  - when a filament's bodies are thinner than the nozzle (about; measured
    across, so a thin colour layer on top does not count).
  Each warning has **Show**, which selects the bodies in Fusion.
- **Click a filament's name** to select its bodies in Fusion.
- **Use it** next to "≠ Loaded: …" gives the bodies the loaded spool's
  filament in one click. The filament is added to Fusion like an import
  from the printer, and the slot stays chosen.
- **Print settings per body.** Each filament row has an arrow (›); open it
  to see the filament's bodies (with
  their component, "Body1 in TEF", so two bodies with one name are told
  apart). **Edit** gives one body, or all bodies of the filament, their own
  infill %, infill type, walls, top and bottom layers. Empty fields show the
  print process's value in grey ("15% (process)"). The settings stay on the
  bodies in the design; Bambu Studio, OrcaSlicer and PrusaSlicer get them as
  the body's own settings. The filament row shows "⚙" when some of its
  bodies have their own settings.
- **Infill type** lists the types the chosen slicer has. Filamatch asks
  each slicer once per version: PrusaSlicer lists them in its help; Bambu
  Studio and OrcaSlicer keep only the type names they know when they load a
  3MF. A type with another name in another slicer is renamed ("zig-zag" in
  Bambu Studio is "rectilinear" in OrcaSlicer and PrusaSlicer); a type the
  slicer does not have at all is named in the ready check.
- Supports are left to the slicer: they depend on how the part sits on the
  bed, and the slicers paint them where they are needed.
- **Filament cost** in the print estimate, for example "filament cost about
  $0.30". It is each filament's grams at the price per kg in its slicer
  preset (PrusaSlicer works it out itself). Set the currency symbol under
  Settings > Export; set the prices in the slicer to what you pay.
- A copy of a Filamatch filament (for example a body's "as printed" copy,
  with the printed density) stays that filament: same row, slot, colour and
  slicer preset, and the filament's own density in the slicer. The copy
  names its source in its description (`filamatch_source=<name>`).

**Changed**
- Colour matching compares colours the way people see them (CIEDE2000)
  instead of by RGB distance. Two shades of green now count as close, and a
  grey no longer counts as close to a colour. The matches on a real H2D
  with eight spools stayed the same.

**Fixed**
- An empty design (no bodies yet) no longer writes an error to the log.
- The palette no longer gets stuck on the sample data ("Preview with
  sample data") when Fusion is slow to connect it, for example right after
  the add-in starts. It waits up to 3 seconds, and connects later when
  Fusion's link shows up after that.

Checked with the Bambu Studio 2.9.1, OrcaSlicer 2.5 and PrusaSlicer 2.9.7
command lines: a body with 100% infill and 6 walls gets more filament in all
three, also when touching bodies are joined into one object, and an infill
type on a body is kept (Bambu Studio 21 types, OrcaSlicer 26, PrusaSlicer
18). In Fusion, a test design (with a component placed twice) was measured,
selected, given body settings, exported with them and sliced.

**What is tested** for the printed weight and strength: the weight model
against Bambu Studio's sliced grams for 6 shapes with 3 settings (mean error
3.3 %, largest 7.2 %); in Fusion, the switch on and off (one copy per body,
all copies removed), Safe and Typical written into the copies (stiffness,
shear modulus, yield and tensile strength, Poisson's ratio, density), a
Bambu Lab PAHT-CF body with its data sheet values, a "Polymaker PPS GF"
body found under its catalog name, and a PLA body with the generic values.
Every data sheet value was read from the sheet and checked a second time;
all 45 data sheet links open. Not tested: a Static Stress study in Fusion
Simulation with these materials (Fusion's API cannot run one).

The slicer test matrix is unchanged against 0.8.0 (24 cases the same).

## 0.8.0 – Send to PrusaSlicer

**New**
- **Send to PrusaSlicer prepares the project** (PrusaSlicer 2.9.7 or
  newer), as Send to Orca and Send to Bambu do:
  - Pick PrusaSlicer under Settings > Send to. Filamatch finds it by
    itself, also in its "Original Prusa Drivers" folder.
  - Print settings lists PrusaSlicer's own printers and prints: first
    **My presets (PrusaSlicer)** (the printers, prints and filaments you
    saved in PrusaSlicer yourself), then Prusa Research and the 31 other
    vendors PrusaSlicer ships. The list is read again each time you open
    Print settings, so a printer you just saved in PrusaSlicer is there.
  - The project gets the printer, the print, and a filament preset on
    every tool that fits the printer: the design's own filament, one made
    from the Open Filament Database, else the generic preset of its type.
    Each body is on its tool, in its colour.
  - The bodies sit together in the middle of the bed with their lowest
    point on it, and the wipe tower is placed beside them.
  - After sending, the palette shows the print time and the grams of each
    filament, with the spool check.
  - Filamatch works out which presets fit from PrusaSlicer's own rules and
    builds the settings in a settings folder of its own: your PrusaSlicer
    settings are not changed. (PrusaSlicer 2.9's command line takes a
    printer and filament preset by name but does not apply them.)
  - **Save new filament presets in the slicer** also works for PrusaSlicer.
- Advanced > **Reload slicer profiles** (was "Reload Orca profiles")
  reloads the lists of whichever slicer you pick.

**Fixed**
- The PrusaSlicer layout (Send to PrusaSlicer without preparing, and
  SuperSlicer) puts the bodies' lowest point on the bed: a design drawn
  below Fusion's origin was "Sinking" in PrusaSlicer.

**Known limits**
- PrusaSlicer's own print time for MMU3 printers is sometimes broken
  ("2147483647d"). Filamatch slices again; when it stays broken, the
  palette shows only the grams.
- Filamatch does not read loaded spools from Prusa printers yet.

**What is tested:** PrusaSlicer 2.9.7 on macOS: the command line for the
Prusa XL 5T, MK4 MMU3 and CORE One (prepare and estimate; these are cases
in the slicer test matrix), and a full send from Fusion to the PrusaSlicer
app with a printer preset of the owner's own (Bambu Lab H2D values: two
tools, 350 x 320 bed): printer, print, a filament per tool, each body on
its tool, on the bed, wipe tower beside it. Not tested: opening a project
for a Prusa printer in the PrusaSlicer app, Windows, SuperSlicer, and
printing.

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
