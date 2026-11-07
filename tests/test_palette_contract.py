"""Static contract checks for the Filamatch HTML palette.

This does not execute the JavaScript (Fusion does that); it verifies that the
palette assets exist, are self-contained (no CDN dependencies), and implement
every action in the palette <-> Python protocol used by ``palette_bridge.py``.

Run from the repository root:

    python3 tests/test_palette_contract.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
PALETTE_DIR = REPO_ROOT / "Filamatch" / "palette"
INDEX = PALETTE_DIR / "index.html"
STYLE = PALETTE_DIR / "style.css"
APP = PALETTE_DIR / "app.js"
BRIDGE = REPO_ROOT / "Filamatch" / "lib" / "palette_bridge.py"
LIB_DIR = REPO_ROOT / "Filamatch" / "lib"

FAILURES: List[str] = []
PASSED = 0


def check(condition: bool, label: str) -> None:
    """Record and print one PASS/FAIL check."""
    global PASSED
    if condition:
        PASSED += 1
        print("PASS {}".format(label))
    else:
        FAILURES.append(label)
        print("FAIL {}".format(label))


def main() -> int:
    """Validate palette files and the JS/Python message contract."""
    for path in (INDEX, STYLE, APP):
        check(path.is_file() and path.stat().st_size > 0, "{} exists and is non-empty".format(path.name))

    if FAILURES:
        print("\nPALETTE CONTRACT FAILED ({} missing file(s))".format(len(FAILURES)))
        return 1

    html = INDEX.read_text(encoding="utf-8")
    css = STYLE.read_text(encoding="utf-8")
    js = APP.read_text(encoding="utf-8")

    check("style.css" in html, "index.html references style.css")
    check("app.js" in html, "index.html references app.js")
    check('id="app"' in html or "id='app'" in html or "<main" in html, "index.html has a main app container")
    check(
        "http://" not in html and "https://" not in html
        and "@import url(http" not in css
        and "<script src=\"http" not in html,
        "palette assets are self-contained (no CDN dependencies)",
    )
    check("fusionJavaScriptHandler" in js, "app.js defines the Fusion JS message handler")
    check("fusionSendData" in js, "app.js sends messages through adsk.fusionSendData")
    check("textContent" in js, "app.js renders data with textContent (safe DOM)")

    required_actions: Tuple[str, ...] = (
        "initData",
        "importResult",
        "materialsData",
        "exportResult",
        "diagnosticsResult",
        "settingsSaved",
        "logData",
        "import_selected",
        "refresh_materials",
        "export_3mf",
        "browse_export_path",
        "browse_slicer_path",
        "run_diagnostics",
        "save_settings",
        "get_log",
        "exportPathData",
        "slicerPathData",
        "favorites",
        "get_profiles",
        "profilesData",
        "import_profiles",
        "profilesImported",
        "openfilament_search",
        "openFilamentData",
        "openfilament_import",
        "openFilamentImportResult",
        "create_library",
        "libraryCreated",
        "bambuddy_context",
        "bambuddyContextData",
        "bambuddy_import",
        "bambuddyImportResult",
        "open_material_browser",
        "materialBrowserData",
        "design_check",
        "set_printer_source",
        "bambu_lan_test",
        "bambu_lan_save",
        "bambu_lan_remove",
        "bambuLanTestResult",
        "bambuLanPrinters",
        "printer_source",
        "printer_configured",
    )
    for action in required_actions:
        check(action in js, "app.js handles palette action {!r}".format(action))

    required_ui = (
        "catalog",
        "filaments",
        "export",
        "log",
    )
    for fragment in required_ui:
        check(fragment in html.lower(), "index.html contains the {} section".format(fragment))
    check("categor" in html.lower(), "index.html contains the category organization")
    check(
        "extruder" not in html.lower(),
        "index.html has no manual extruder/mapping controls",
    )
    check("browse" in html.lower(), "index.html offers native path Browse buttons")
    check(
        "favorites" in html.lower(),
        "index.html shows the app-level Favorites status",
    )
    check(
        "prepare" in html.lower() and "printer" in html.lower(),
        "index.html offers a printer-target preparation section",
    )
    check(
        "open filament" in html.lower(),
        "index.html offers Open Filament Database search",
    )
    check(
        all('data-screen="{}"'.format(name) in html for name in ("home", "add", "settings", "welcome")),
        "index.html has the Home, Add filaments, Settings and Welcome screens",
    )
    check(
        "showScreen" in js and "renderHome" in js and "stepper" not in html,
        "app.js navigates screens instead of a step wizard",
    )
    check(
        "Send to Orca" in html and 'id="export-readiness"' in html,
        "Home has the ready check and one Send to Orca button",
    )
    materials_handler = js.split("function handleMaterialsData", 1)[-1].split("\n  }\n", 1)[0]
    refresh = js.split("function scheduleBambuddyRefresh", 1)[-1].split("\n  }\n", 1)[0]
    check(
        "scheduleBambuddyRefresh" in materials_handler
        and "requestBambuddyContext" in refresh
        and "mappedMaterialsKey" in refresh
        and "state.bambuddy.error" in refresh,
        "a design-material change refreshes the AMS slot mapping (debounced, only when the ids changed)",
    )
    collect = js.split("function collectSettings", 1)[-1].split("\n  }\n", 1)[0]
    check(
        "bambuddy_api_key" not in collect and "bambuddy_url" not in collect,
        "the settings autosave never sends the Bambuddy address or API key",
    )
    check(
        "clear_bambuddy_key" in js and 'id="bambuddy-remove-key-btn"' in html
        and 'id="bambuddy-verify-tls"' in html and "bambuddy_verify_tls" in js,
        "Settings offers Remove key and the HTTPS certificate check for Bambuddy",
    )
    check(
        "smoke" not in js.lower() and "smoke" not in html.lower(),
        "diagnostics carry no smoke-test wording",
    )
    design_check = js.split("function checkDesign", 1)[-1].split("\n  }\n", 1)[0]
    check(
        "design_check" in design_check
        and "addEventListener('focus', checkDesign)" in js,
        "the palette asks Fusion for design changes when it gets focus",
    )
    check(
        "create-library" in html.lower() or "create_library" in html.lower(),
        "index.html offers the Create Filamatch Library action",
    )
    check(
        "auto-library-update" in html.lower(),
        "index.html offers auto-update of the custom library",
    )

    check(
        re.findall(r'data-tab-panel="([^"]+)"', html) == ["printer", "catalog", "online"],
        "Add filaments offers the printer, catalog and online tabs",
    )
    check(
        'id="open-settings-btn"' in html and 'id="open-add-btn"' in html
        and 'id="advanced-section"' in html and 'id="diagnostics-btn"' in html,
        "settings and diagnostics live on their own screen, diagnostics under Advanced",
    )
    check(
        'id="bambuddy-url"' in html and 'id="bambuddy-api-key"' in html
        and 'id="bambuddy-connect-btn"' in html,
        "index.html offers the Bambuddy connection fields",
    )
    check(
        'id="bambuddy-slots-list"' in html and 'id="filament-list"' in html
        and 'id="bambuddy-import-btn"' in html,
        "index.html offers loaded printer filaments, the Home slot list and spool import",
    )
    check(
        "renderBambuddySlots" in js and "renderFilamentList" in js
        and "requestBambuddyContext" in js,
        "app.js renders Bambuddy slots and the material-to-slot list",
    )
    check(
        'id="printer-source-bambuddy"' in html and 'id="printer-source-bambu-lan"' in html
        and 'id="bambuddy-card"' in html and 'id="bambu-lan-card"' in html,
        "Settings offers the printer source choice with a Bambuddy and a LAN card",
    )
    check(
        all('id="{}"'.format(i) in html for i in (
            "bambu-lan-list", "bambu-lan-add-toggle", "bambu-lan-form", "bambu-lan-ip",
            "bambu-lan-name", "bambu-lan-test-btn", "bambu-lan-save-btn",
            "bambu-lan-trust-btn", "bambu-lan-result"))
        and re.search(r'id="bambu-lan-code"[^>]*type="password"', html) is not None,
        "the LAN card lists printers and offers Add printer (IP, password access code, name, Test, Save, Trust)",
    )
    check(
        'id="welcome-lan-btn"' in html and 'id="welcome-bambuddy-btn"' in html
        and 'id="welcome-skip-btn"' in html,
        "the welcome screen offers Bambuddy, a Bambu printer on the network, and Skip",
    )
    lan_senders = [js.split("function " + name, 1)[-1].split("\n  }\n", 1)[0]
                   for name in ("onLanTestClick", "onLanSaveClick")]
    check(
        "access_code" not in collect
        and all("access_code" in body for body in lan_senders)
        and "bambu-lan-code" not in collect,
        "the access code is sent only by bambu_lan_test / bambu_lan_save",
    )
    lan_saved = js.split("function handleBambuLanPrinters", 1)[-1].split("\n  }\n", 1)[0]
    check(
        "bambu-lan-code" in lan_saved and "input.value = ''" in lan_saved,
        "a successful LAN save clears the access code field",
    )

    # Both sides of the bridge: every request the palette sends is dispatched
    # in palette_bridge.py, and every reply it waits for is sent by Python.
    if BRIDGE.is_file():
        bridge = BRIDGE.read_text(encoding="utf-8")
        lib_text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(LIB_DIR.glob("*.py")))
        response_map = js.split("var RESPONSE_BY_REQUEST = {", 1)[-1].split("};", 1)[0]
        pairs = re.findall(r"^\s*(\w+):\s*'(\w+)'", response_map, re.MULTILINE)
        check(len(pairs) > 10, "app.js RESPONSE_BY_REQUEST lists the request -> reply actions")
        for request_action, reply_action in pairs:
            check(
                'action == "{}"'.format(request_action) in bridge,
                "palette_bridge.py dispatches palette request {!r}".format(request_action),
            )
            check(
                '"{}"'.format(reply_action) in lib_text,
                "Python sends reply {!r} (for {!r})".format(reply_action, request_action),
            )
    else:
        check(False, "palette_bridge.py exists")

    check(
        "milestone" not in html.lower() and "spoolman" not in html.lower(),
        "index.html carries no developer notes",
    )

    print()
    if FAILURES:
        print("PALETTE CONTRACT FAILED ({} check(s))".format(len(FAILURES)))
        for label in FAILURES:
            print(" -", label)
        return 1
    print("PALETTE CONTRACT PASSED ({} checks)".format(PASSED))
    return 0


if __name__ == "__main__":
    sys.exit(main())
