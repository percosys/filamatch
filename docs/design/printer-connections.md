# Plan: direct printer connections (multi-brand)

Status: Phase 0 is done (in 0.2.0), Phase 2 is built, Phase 1 (Manual) is built. Phases 3-6 are planned; see the
[roadmap](../roadmap.md). This is the design note for contributors.

## 1. Goal

- **Bambuddy stays the primary source.** It is the default. It gives the most
  detail (spool inventory, grams left, spool IDs).
- **Add "Direct to printer" as a second source.** The add-in reads the loaded
  filament slots from the printer itself, on the local network.
- **Support more brands than Bambu Lab.** Each brand family gets its own
  connector. Bambu needs MQTT over TLS with a printer certificate. Most other
  brands use plain HTTP or WebSocket APIs.
- **Add a "Manual printer" source.** The user types in what is loaded. This
  works for every printer, with no network at all.
- The palette flow does not change: printer -> slots -> import into Fusion ->
  map materials to slots -> export to Orca.

Non-goals (for now): starting or controlling prints, camera, cloud accounts
(Bambu cloud, Prusa Connect, Creality Cloud). Decided 2026-11-07: Bambu is
LAN only; no Bambu cloud login (the API is not public, Bambu objects to
clients that act like Bambu Studio, and accounts were blocked for many
connections). We never ask for a cloud
password.

## 2. Where the code is today

| Module | Role today | Change |
|---|---|---|
| `bambuddy.py` (859 lines) | HTTP client **and** the data types `PrinterInfo`, `SpoolInfo`, `SlotInfo`, `BambuddyContext`, plus helpers `material_family`, `default_density`, `default_template_material`, `normalize_color_hex` | Move the types and helpers to a neutral module. Keep the client. |
| `slot_mapping.py` | Auto-match Fusion materials to slots (type, then colour, then brand) | Use the neutral types. Add a score bonus for an exact vendor filament ID. |
| `inventory_sync.py` | Turn slots/spools into Fusion favourites | Use the neutral types. Accept slots that have no spool record. |
| `palette_bridge.py` | `_bambuddy_context_payload`, `_handle_bambuddy_context`, `_handle_bambuddy_import`, `_attach_bambuddy_slot_manifest`; settings in `settings.json` (API key in plain text) | Route through a source picker. Rename actions with aliases so old palettes keep working. Move secrets to the OS keychain. |
| `palette/app.js`, `index.html` | Bambuddy setup fields, Slots step | Add a "Printer source" choice and a per-brand "Add printer" form. Slot labels per brand. |
| 3MF `Metadata/filamatch.json` | `slot_manifest.v1` | v2: add `source`, `brand`, `unit_label`, `vendor_filament_id`. Readers accept v1 and v2. |

## 3. Design

### 3.1 Neutral data model (`printer_model.py`, built in Phase 0)

The add-in keeps its flat module layout, so the new code is in flat files,
not a `printers/` package.

- `PrinterInfo`: `id` (int, unique within one source), `name`, `model`,
  `serial_number`, `ip_address`, `location`, `is_active`, `brand`.
  Direct printers will get their `id` from their saved config entry.
- `SpoolInfo`: unchanged fields (inventory sources only: Bambuddy, Spoolman).
- `SlotInfo`: `printer_id`, `printer_name`, `ams_id` (unit), `tray_id`
  (slot), `spool`, `filament_type`, `color_hex`, `loaded`, plus new fields
  `custom_label` ("CFS 1 - B", "Gate 3"), `extruder` (nozzle that feeds the
  slot), `vendor_filament_id` (Bambu `tray_info_idx`, Orca `filament_id`),
  `remaining_percent` (printer-reported). `slot.remaining` uses the
  inventory spool first, then the printer value.
- `PrinterContext`: `printers`, `spools`, `slots`, `fetched_at`, `source`,
  `warnings`.
- Field order is fixed. New fields are only appended, with defaults.
- `slot.key` stays `"<printer>:<unit>:<slot>"`, so saved slot maps
  (`1:0:0`, `1:1:3`) keep working.
- `bambuddy.py` re-exports these types (`bambuddy.SlotInfo` is
  `printer_model.SlotInfo`; `BambuddyContext` is `PrinterContext`).

### 3.2 Source interface (`printer_sources.py`, built in Phase 0)

```python
class SlotSource(Protocol):
    source_id: str            # "bambuddy", later "bambu_lan", "moonraker", ...
    label: str
    def test_connection(self) -> Dict[str, Any]: ...
    def list_printers(self) -> List[PrinterInfo]: ...
    def fetch_context(self) -> PrinterContext: ...
```

- A registry maps `source_id` to a factory and an `is_configured` check.
  The setting `printer_source` picks one. Empty or unknown means
  `bambuddy`.
- The palette, the export slot manifest and the spool import call only
  `printer_sources.fetch_context(settings)` / `is_configured(settings)`.
- Every source is read-only. No source sends a command that moves, heats
  or prints.
- **Moved to Phase 2:** run network work on a worker thread with a time
  limit, so a slow printer never blocks Fusion. Bambuddy calls still run on
  the palette event, as before (20 s timeout). This needs Fusion custom
  events to send results back to the palette, so it lands together with the
  first direct connector.

### 3.3 Picking a preset for a slot

- Bambu slots carry a vendor filament ID (`tray_info_idx`). Orca's system
  filament profiles carry the same ID as `filament_id`. Checked on this Mac:
  the exported project has `filament_settings_id` "Bambu ABS-GF @BBL H2D"
  and `filament_ids` "GFB50".
- Orca 2.5 stores its system profiles in a packed binary file
  (`system/BBL.opc`, header `ZCRO`), not as loose JSON. So we cannot just
  read the files. Two ways to build the lookup `filament_id -> preset name`:
  1. Ask the Orca CLI, as `slicer_profiles` already does for presets, and
     cache the result per Orca version.
  2. Ship a small table made from the public Orca profile JSON on GitHub,
     and refresh it with each add-in release.
  Pick 1, with 2 as the fallback when Orca is not installed.
- Filter the lookup by printer model (`@BBL H2D`). This gives an exact
  preset, not a guess.
- Other brands give a type and a name. The existing scoring in
  `slot_mapping` handles those.

### 3.4 Secrets

Built in Phase 0 (`secret_store.py`):

- macOS: login keychain, generic-password items, service
  `Filamatch`, account = settings key (`bambuddy_api_key`).
  Uses the Security framework through `ctypes`, so the secret never shows
  in a command line. Checked inside Fusion: write, read and delete work.
- `settings.json` stores the key as `""` when the keychain holds it.
- Migration: the first time the new add-in code loads settings inside
  Fusion, a plain-text Bambuddy key moves into the keychain.
- Only the Fusion process uses the keychain. Self-tests and scripts run
  outside Fusion keep the plain file. Reason: an item made by Fusion makes
  macOS ask for a password when another program reads it.
- Any keychain failure falls back to the plain file, as before.
- **Not built yet:** Windows Credential Manager (`CredWriteW` /
  `CredReadW`). On Windows the key stays in `settings.json` for now.
- Later phases add printer access codes as accounts like
  `printer:<id>:access_code`. Secrets are never logged.

### 3.5 Palette changes

- Setup drawer: "Printer source" with three choices:
  **Bambuddy (recommended)**, **Direct to printer**, **Manual**.
- Direct: "Add printer" form. Pick a brand, then fill only the fields that
  brand needs (Section 4). Buttons: "Find printers" (discovery, where the
  brand supports it) and "Test connection".
- Slots step: same table. Columns that a source cannot fill show "-"
  (for example, grams left on a Klipper printer without Spoolman).
- If a direct read fails, the palette says why in one line and offers
  "Use Manual instead".

## 4. Brand connectors

Research date: 2026-11-07. Tags: **[V]** = verified in a source (Section 9),
**[?]** = not verified yet, needs a test on real hardware.

### 4.0 Shared building blocks

Python's standard library has no MQTT client, no WebSocket client and no
AES. Fusion add-ins can ship extra code, but each package adds risk.

- **MQTT:** write a small MQTT 3.1.1 client (connect, subscribe, publish,
  ping) over `ssl`/`socket`. We only need read-only messages, so it stays
  about 200 lines. Used by Bambu (and Elegoo CC2 later).
- **WebSocket:** write a small text-frame client (handshake, masked text
  frames, ping/pong). Used by Creality CFS (and Elegoo CC1 later).
  Moonraker does not need it: plain HTTP polling is enough.
- **AES:** only Anycubic needs it. Decide in its spike (Phase 6).
- **Read-only by design:** no brand has a read-only login. The same access
  code or key can also control the printer. So our code sends only
  "give me status" requests. Each connector has a test that fails if any
  other command can be sent.
- **Licences:** the add-in has no licence file yet. Bambuddy, OrcaSlicer,
  heydemoura/creality-orca-integration and anycubic_ha_local are AGPL-3.0,
  and Happy Hare and AFC are GPL-3.0. We read them only to learn the
  protocol. We do not copy their code. ha-bambulab is MIT: we may reuse
  code from it with credit. We also take the Bambu CA certificates from it.

### 4.1 Bambu Lab (Phase 2): X1/X1C/X1E, P1P/P1S, P2S, A1/A1 mini, H2D/H2D Pro/H2C/H2S, X2D, A2L

- **Connection:** MQTT over TLS, port 8883. User `bblp`, password = LAN
  access code. Topics `device/<serial>/report` and
  `device/<serial>/request`. [V]
- **User enters:** IP, serial number, access code. "Find printers" can
  fill in IP and serial: SSDP on **UDP 2021** (not 1900), search target
  `urn:bambulab-com:device:3dprinter:1`, `USN` = serial, headers
  `DevModel.bambu.com`, `DevName.bambu.com`. [V]
- **Requests we send (only these two):**
  - `{"pushing":{"sequence_id":"0","command":"pushall","version":1,"push_target":1}}`
    for the full state. P1 printers send only changes after that, so we
    keep the last full state and merge. Do not send `pushall` more than
    once every 5 minutes on P1. [V]
  - `{"info":{"sequence_id":"0","command":"get_version"}}` for the module
    list. AMS models by name prefix: `ams/` AMS, `ams_f1/` AMS Lite,
    `n3f/` AMS 2 Pro, `n3s/` AMS HT. [V]
- **Slot fields** in `print.ams.ams[].tray[]`: `tray_type`, `tray_color`
  (RRGGBBAA), `tray_info_idx` (Bambu filament ID, same as Orca
  `filament_id`), `remain` (% , -1 = unknown), `tray_sub_brands`,
  `tray_id_name`, `tag_uid`/`tray_uuid` (zeros if not a Bambu RFID spool),
  `tray_weight`, `cols[]` (multi-colour), `nozzle_temp_min/max`. Per unit:
  `id`, `humidity`, `temp`, `info`. [V]
- **Units and nozzles:**
  - AMS unit IDs 0-3. AMS HT unit IDs 128-135. [V]
  - External spool: `vt_tray` (ID 254) on single-nozzle printers. On H2D,
    `vir_slot[]` with IDs 254 and 255, one per nozzle. [V]
  - Bits 8-11 of `ams[].info` = the nozzle this unit feeds: 0 = right,
    1 = left. [V] **This answers "which AMS feeds which nozzle".** The
    add-in can then warn when Orca's filament grouping puts a filament on
    the other nozzle.
- **Certificate:** the printer certificate is signed by Bambu's own CA
  chain, not a public CA. The certificate name (CN) is the serial number,
  so we send the serial as SNI. Newer models add new intermediate CAs
  (P2S, H2C, X2C). [V]
  - ha-bambulab: checks the chain against bundled Bambu CAs, turns off
    the host-name check, clears `VERIFY_X509_STRICT` (Python 3.13+ rejects
    Bambu's CA without this) and caps at TLS 1.2 (P2S firmware
    01.02.00.00 hangs on TLS 1.3). [V]
  - Bambuddy: no check at all (`CERT_NONE`). [V]
  - **Our plan:** check the chain against bundled Bambu CAs, check CN =
    serial ourselves, cap at TLS 1.2. If the chain is unknown (a new
    model), show the certificate fingerprint and ask the user once to
    "Trust this printer". Store the fingerprint (pinning). Never skip
    the check silently.
- **Printer modes (firmware with Authorization Control, Jan 2025):**
  - Reading status is still allowed for third parties. Bambu's own
    announcement lists "MQTT status push" as still working. [V]
  - Cloud mode + local MQTT: read works (per Bambu, NOT tested by us).
    LAN-only: read works (tested on an H2D and an X1C, 2026-11-07).
    LAN-only + Developer Mode: read and control. [V]
  - **Open:** one P2S forum report says local reads failed while the
    printer was in cloud mode. Test on an H2D in cloud mode and in
    LAN-only mode. [?]
  - Bambuddy needs Developer Mode because it also controls prints. We only
    read, so we should not need it. [V for Bambuddy, ? for H2D]
- **Reference:** greghesp/ha-bambulab (MIT, Python, best match);
  Doridian/OpenBambuAPI (protocol notes).

### 4.2 Klipper / Moonraker family (Phase 3)

One connector covers many brands: Voron and other Klipper machines,
QIDI Plus4/Q2 with QIDI Box, Snapmaker U1, Creality Hi with CFS, and
Klipper printers with Happy Hare or AFC (Box Turtle). Sovol and Elegoo
Neptune 4 run Klipper too, but they have one nozzle and no slot unit. [?]

- **Connection:** HTTP, port 7125, no TLS. Trusted IP needs no login,
  else an `X-Api-Key`. [V] Discovery: mDNS `_moonraker._tcp`, only if the
  printer has zeroconf on. [V/?]
- **User enters:** IP (and API key if needed).
- **Slot readers, tried in this order** (same order Orca uses):
  1. `GET /server/database/item?namespace=lane_data` -> per lane:
     `lane`, `color`, `material`, `filament_id`, `bed_temp`,
     `nozzle_temp`, often `spool_id`, `weight`. Written by AFC, and by
     Happy Hare since Feb 2026. [V]
  2. Happy Hare: `GET /printer/objects/query?mmu` -> arrays per gate:
     `gate_status`, `gate_material`, `gate_color`, `gate_filament_name`,
     `gate_vendor`, `gate_spool_id`, `gate_remaining_weight`; `ttg_map`
     (tool to gate). [V]
  3. AFC: printer object `AFC` -> `lanes[]`; per lane `material`, `color`,
     `filament_name`, `spool_id`, `weight`, `map` (tool). [V] Lane object
     name prefix [?].
  4. QIDI Box: `save_variables` (`color_slotN`, `filament_slotN`,
     `vendor_slotN`, numbers into `officiall_filas_list.cfg`); newer
     firmware uses `multi_color_controller.box...filament_type`. Loaded
     when `runout_button == 0`. Up to 16 slots. [V]
  5. Snapmaker U1: `print_task_config` + `filament_detect` arrays
     `filament_exist`, `filament_type`, `filament_color_rgba`,
     `filament_vendor`. [V]
  6. Creality Hi CFS: `box.T1..T4` where `state == "connect"`;
     `material_type[i]`, `color_value[i]`. [V]
  7. None of these: one slot per tool from the current extruder; use
     Spoolman (Section 4.5) if set up, else Manual.
- **Remaining amount:** only from Happy Hare, AFC or Spoolman.
- **Reference:** OrcaSlicer `src/slic3r/Utils/MoonrakerPrinterAgent.cpp`,
  `QidiPrinterAgent.cpp`, `SnapmakerPrinterAgent.cpp`, `CrealityCfs.cpp`
  (AGPL, read only).

### 4.3 Creality K1 / K2 with CFS, stock firmware (Phase 4)

- **Connection:** WebSocket `ws://<ip>:9999/`, JSON text frames, no login,
  no TLS. Answer the printer's `heart_beat` with `ok`. Model:
  `GET http://<ip>/info`. [V]
- **User enters:** IP only. Discovery: mDNS `_Creality-<mac>._udp`. [V]
- **Request:** `{"method":"get","params":{"boxsInfo":1}}`. [V]
- **Slot fields:** `boxsInfo.materialBoxs[]` (box `type` 0 = CFS,
  1 = external holder, 2 = CFS Mini) -> `materials[]`: `id` (0-3 = A-D),
  `state`, `vendor`, `type`, `name`, `color` ("#0RRGGBB", drop the extra
  0), `rfid`, `percent`. [V]
- **Caution:** sources disagree on `state` values. Treat any non-zero as
  loaded. [V]
- **Security note:** no login means anyone on the network can control the
  printer. We only send the `get` request.
- **Reference:** heydemoura/creality-orca-integration `docs/protocol.md`
  (AGPL, read only).

### 4.4 OctoPrint (Phase 5)

- Core OctoPrint has no slot data. Auth: `X-Api-Key`. [?]
- With the **SpoolManager** plugin (maintained):
  `GET /plugin/SpoolManager/loadSpoolsByQuery` -> `selectedSpools[]` per
  tool: `material`, `color`, `colorName`, `vendor`, `remainingWeight`. [V]
- FilamentManager plugin: old, unmaintained. Support only if asked.

### 4.5 Spoolman as an inventory source (Phase 5)

- Not a printer. It replaces the "spool" part of Bambuddy for people who
  use Spoolman (the palette already has a Spoolman placeholder).
- REST `/api/v1/spool`, `/filament`, `/vendor`: `remaining_weight`,
  `location`, `material`, `color_hex`, `name`. [V] No login by default. [?]
- No live slot state. Spools link to slots through an MMU's `spool_id`
  (Section 4.2), or through the spool's `location` field.

### 4.6 Prusa (Phase 5: status only)

- PrusaLink (local HTTP, Digest login) does **not** list MMU3 slots, XL
  tools or INDX slots. Only the current tool's material. [V]
- The full slot data goes only to Prusa Connect (cloud, needs the user's
  Prusa account). Out of scope. [V]
- **Plan:** Prusa printers use the Manual source. Revisit if PrusaLink
  adds slot data.

### 4.7 Spikes (Phase 6): one short go / no-go note each

| Brand | What we know | Confidence |
|---|---|---|
| Flashforge AD5X + IFS | `POST http://<ip>:8898/detail` with `serialNumber` + `checkCode`; LAN mode on. Slots: `slotId`, `hasFilament`, `materialName`, `materialColor`. No remaining amount. Simple HTTP, so a good first spike. | Medium-high |
| Elegoo Centauri Carbon 2 + CANVAS | MQTT port 1883 with the screen access code; "LAN Only" mode on firmware 2.0. Trays: name, colour, temps. | Medium-low |
| Elegoo Centauri Carbon 1 + CANVAS | SDCP WebSocket port 3030, no login. Sources disagree on whether slot data is readable. | Low |
| Anycubic Kobra S1/3 + ACE Pro | LAN mode -> HTTP handshake on port 18910 -> AES-encrypted MQTT login -> MQTT/TLS port 9883. Slots: `type`, `color`, `sku`, `consumables_percent`. Needs AES. | Medium |

## 5. Phases

| Phase | Work | Done when |
|---|---|---|
| 0 | Neutral model + `SlotSource`. Bambuddy behind it. Keychain for the Bambuddy key. | **Done 2026-11-07.** All module self-tests and the 3 test suites pass. In Fusion, the new code read the same 6 slots and made the same mapping as the old code. |
| 1 | Manual source. | **Built 2026-11-08** (`lib/manual_printer.py`, Settings > Printer > Manual). One printer: name, nozzle size and material, slots with a Filamatch catalog filament each (its fields are kept with the slot). In Fusion: a real design's filaments matched to manual slots (auto), slot presets and nozzle hardness reached the palette. Still open: a full export with a manual printer in the running add-in. |
| 2 | Bambu LAN direct. | **Built 2026-11-07** (`lib/bambu_lan.py`, `lib/mqtt_lite.py`). Live reads of an H2D and an X1C, both in LAN-only mode without developer mode: 11 and 5 slots, Bambu CA check passed, filament names from the installed presets. Still open: compare with the Bambuddy slot list on the same printer, and a printer in cloud mode. |
| 3 | Klipper / Moonraker family (Section 4.2). | Fixture tests for each slot reader pass. One live printer test if one is available. |
| 4 | Creality K1/K2 CFS, stock firmware (Section 4.3). | Fixture tests pass. Live test if a K1/K2 + CFS is available. |
| 5 | OctoPrint + SpoolManager, Spoolman, Prusa status (Sections 4.4-4.6). | Slots read where the API has them. Prusa falls back to Manual. |
| 6 | Spikes: Flashforge, Elegoo, Anycubic (Section 4.7). | A short go / no-go note per brand. |

Each phase is its own branch and pull request. Phase 0 must land first.
Phases 1 to 5 do not depend on each other.

## 6. Testing

- **Fixtures:** recorded messages per brand, cleaned of serial numbers,
  IPs and access codes. Stored in `tests/fixtures/printers/<brand>/`.
- **Fake transports:** same pattern as `bambuddy.FakeTransport`. Every
  connector has a self-test that runs without a network.
- **Cross-check oracle:** for Bambu, run Bambuddy and Direct against the
  same printer and compare the slot lists. They must match.
- **Existing suites:** `tests/test_fusion_lifecycle_simulation.py`,
  `tests/test_palette_contract.py`, `tests/test_palette_export_flow.py` and
  every module self-test stay green each phase.
- **Live checks:** full palette run in Fusion, export to Orca, check the 3MF
  (parts -> extruders, presets, colours, slot manifest), as done for the
  Bambuddy path.

## 7. Risks

| Risk | Effect | What we do |
|---|---|---|
| Bambu firmware blocks local reads in some mode (P2S report) | Direct mode fails for some users | Test cloud mode and LAN-only on H2D first. Clear error text. Bambuddy stays the default. |
| New Bambu model with an unknown CA | TLS check fails | "Trust this printer" fingerprint prompt. Update the CA bundle each release. |
| Vendors change protocols | A connector breaks after a firmware update | Fixtures per firmware version. One-line error in the palette. Manual stays available. |
| A slow printer blocks Fusion | Fusion freezes | All network work on a worker thread with a time limit (Section 3.2). |
| An access code can also control the printer | Misuse if our code has a bug | Send only status requests. A test checks the allowed request list. Secrets in the keychain. |
| Licence mix-up | Legal risk | Read AGPL/GPL code only to learn the protocol. Reuse only MIT code, with credit. |
| Brands without local slot data (Prusa) | Users expect it to work | Say so in the brand picker and offer Manual. |

## 8. Open questions

1. **Which brands after Bambu?** Proposal: Moonraker next. One connector
   covers Voron, QIDI, Snapmaker, Creality Hi, Happy Hare and AFC. Then
   Creality CFS.
2. **Test printers:** each connector needs a real printer for a live test.
   Contributors with Klipper, OctoPrint or Creality CFS printers are welcome.
3. **Bambu certificate check:** check with bundled CAs plus a "Trust this
   printer" prompt (proposal), or skip the check like Bambuddy does
   (simpler, less safe).
4. **Licences:** read AGPL/GPL code only to learn a protocol; reuse only
   MIT/BSD/Apache code, with credit (Filamatch is MIT).

## 9. Sources

- Bambu: https://github.com/Doridian/OpenBambuAPI (`mqtt.md`, `tls.md`);
  https://github.com/greghesp/ha-bambulab (`pybambu/models.py`,
  `const.py`, `bambu_client.py`, `pybambu/certs/`, `docs/index.mdx`);
  https://github.com/maziggy/bambuddy (README, `bambu_mqtt.py`,
  `discovery.py`);
  https://blog.bambulab.com/firmware-update-introducing-new-authorization-control-system-2;
  https://forum.bambulab.com/t/p2s-lan-only-liveview-while-in-cloud-mode/203500
- Moonraker: https://github.com/Arksine/moonraker (`docs/configuration.md`,
  `docs/external_api/`); https://github.com/moggieuk/Happy-Hare;
  https://github.com/AFCProject/AFC-Klipper-Add-On;
  https://github.com/OrcaSlicer/OrcaSlicer (`src/slic3r/Utils/`)
- Creality: https://github.com/heydemoura/creality-orca-integration
  (`docs/protocol.md`)
- Prusa: https://github.com/prusa3d/Prusa-Firmware-Buddy;
  https://github.com/prusa3d/Prusa-Link-Web (`spec/openapi.yaml`)
- OctoPrint: https://github.com/OllisGit/OctoPrint-SpoolManager
- Spoolman: https://github.com/Donkie/Spoolman
- Spikes: https://github.com/Parallel-7/flashforge-api-docs;
  https://github.com/bjan/pycentauri; https://github.com/zant0p/CentuariMCP;
  https://github.com/chrisfore/anycubic_ha_local
- Orca plugins (why we do not use an Orca plugin for this):
  https://www.orcaslicer.com/wiki/developer_reference/plugin_development/api_reference/printer_agent
  and https://github.com/OrcaSlicer/OrcaSlicer/issues/16961. A plugin cannot
  read the AMS from Orca's built-in Bambu connection.
