# As printed – research (2026-11-08)

Written for the Printegrity add-in, now Filamatch's As printed tab.

The research was quick: a few web searches, and no pages opened. Items marked
**[unverified]** came from memory. Check every number and claim before use.

## Strength data sources

**Data sheets for printed specimens, with XY and Z values (best):**
- BASF Forward AM / Ultrafuse: ISO 527, flat and upright. PLA Tough: 40 MPa
  flat and 28 MPa upright (ZX).
  - https://cdn.shopify.com/s/files/1/0765/4369/files/Ultrafuse_PLA_Tough_TDS_EN.pdf
  - Comparison: https://move.forward-am.com/hubfs/AES%20Documentation/General%20Resources/Filament%20Comparison.pdf
- BCN3D Omega: XY and ZX. https://www.bcn3d.com/wp-content/uploads/2024/02/BCN3D-Filaments-Omega-Technical-Data-Sheets-TDS-EN-OTDS-V2.pdf
- Bambu Lab: data sheets with XY and Z values (PLA Silk about 35 and
  18 MPa). https://forum.bambulab.com/t/request-of-additional-technical-data-for-filaments/259565
- Fiberlogy: https://fiberlogy.com/en_US/p/file/8155c1da1791d89e2516617d17591044/FIBERLOGY_ASA-AF_TDS.pdf
  (whether it gives values per direction is not checked).
- Polymaker, Prusament: printed ISO 527 bars, probably with Z values
  [unverified].
- colorFabb, Kimya, 3DXTech, Extrudr, Fillamentum: often mix molded and printed
  values, so check the specimen type [unverified].
- eSUN, SUNLU, Elegoo: usually one value with no direction given. Low
  reliability [unverified].
- Every source found is a PDF. None is machine-readable.

**Testers:** CNC Kitchen (layer adhesion, walls vs infill, annealing) and My
Tech Fun (Dr. Igor Gaspar). Good for relative trends and calibration, not as
design values. No public dataset with a license found.

**Open data:**
- Open material database for tensile tests on printed parts
  (Garcia-Granada 2023): https://dau.url.edu/handle/20.500.14342/4760
  (license to check).
- The Open Filament Database (MIT; Filamatch uses it) appears to have no
  mechanical fields. Check this.
- MatWeb and SpecialChem: their terms don't allow bulk use, and they are
  mostly molded values.

**Generic values, printed at 100% infill [unverified]:**

| Material | Density | E XY GPa | UTS XY MPa | UTS Z MPa | Elongation % |
|---|---|---|---|---|---|
| PLA | 1.24 | 2.3–3.5 | 45–60 | 20–40 | 2–6 |
| PETG | 1.27 | 1.7–2.1 | 40–50 | 20–35 | 5–20 |
| ABS | 1.04 | 1.8–2.3 | 30–40 | 15–28 | 3–10 |
| ASA | 1.07 | 1.8–2.2 | 35–45 | 20–30 | 3–10 |
| TPU 95A | 1.21 | 0.02–0.05 | 25–40 | 15–30 | 300–600 |
| PA dry | 1.06–1.14 | 1.0–2.0 | 40–60 | 20–40 | 10–50 |
| PA-CF dry | 1.1–1.2 | 3.5–8 | 60–100 | 25–50 | 2–5 |
| PC | 1.20 | 2.0–2.4 | 55–65 | 25–45 | 3–10 |
| PETG-CF | 1.29–1.32 | 3–5 | 40–55 | 15–30 | 2–5 |
| PLA-CF | 1.21–1.29 | 3.5–5 | 35–50 | 15–25 | 1.5–3 |

Checked in search results: Elefilament PLA, 47 MPa XY and 21 MPa Z
(https://reprapltd.com/wp-content/uploads/2018/04/TDS-3d-print-works-elefilament-pla.pdf).

**Models for infill and layer height:**
- Gibson-Ashby scaling: E ≈ Es·ρ^n, with n ≈ 2 for bending-dominated infill
  (gyroid, grid) and ≈ 1 for stretch-dominated infill (triangles). Strength
  scales with ρ^1.5.
- Shell plus core, mixed by area fraction or by bending stiffness.
- Regression papers: infill density matters most (about 28%) and layer height
  much less (about 6%).
  - https://czasopisma.uwm.edu.pl/index.php/ts/article/view/6297
  - https://centralukraine-tech.kntu.kr.ua/tech/article/view/510
  - https://ieomsociety.org/proceedings/orlando2025/293.pdf

**Licensing:** numbers are facts, so type them in with their sources. In the
EU, copying a large part of a database can be protected, so never copy whole
databases. Repeat the makers' "typical values" disclaimers.

## Competing tools

- **Fusion itself:**
  - Its simulation uses one value for every direction (isotropic only), and
    there is no printed-material option.
  - On the forums, people type in PLA values by hand:
    https://forums.autodesk.com/t5/fusion-design-validate-document/simulation-of-3d-printed-plastic-strength/m-p/9542360
  - The Manufacturing/Additive workspace gives time and material, but no
    strength.
- **Netfabb:** metal printing. **Digimat-AM (Hexagon):** accurate printed-part
  simulation, but enterprise software that runs outside the CAD program.
- **Markforged Eiger Simulation:** Markforged printers only, an enterprise
  plan, in its slicer. https://markforged.com/simulation
- **Teton Smart Slice (Cura):** tuned walls and infill to a load target. The
  closest idea. Bought by Markforged and probably discontinued.
  https://3dprint.com/273098/teton-offers-smart-slice-3d-printing-simulation-for-ultimaker-essentials/
- **GrabCAD Print Pro:** Stratasys printers only, early access.
- **SolidWorks Additive, Ansys, Altair Inspire Print3D, nTop, SimScale,
  Dyndrite:** enterprise, mostly metal or general lattices [unverified].
- **Fusion360PrinterMaterials** (free): fixed printed-material values, with no
  infill or direction settings. https://github.com/alecGraves/Fusion360PrinterMaterials
- **Web calculators:** rough relative scores, not connected to the model and
  not validated.
- **Slicers:** time and grams only.

**Verdict:** nothing found does per-body as-printed properties and a live
what-if panel inside Fusion, for any printer, for hobby and prosumer users.
Lead with mass and design comparisons, and present strength as a careful,
sourced estimate.
