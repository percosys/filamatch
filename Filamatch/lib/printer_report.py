"""Save the connected printer's raw status, with private values removed.

Settings > Advanced > **Save printer report** writes one JSON file that a user
can attach to a printer-support issue: the printer's own status (the Bambu Lab
LAN ``pushall`` report and module list, or Bambuddy's printer status) with
everything about trays, nozzles, extruders and AMS units kept, and serial
numbers, access codes, IP and MAC addresses, Wi-Fi names, user ids, keys,
tokens and camera links removed (:func:`redact`).

Network work only; runs on the worker thread.  ``python3 -m
Filamatch.lib.printer_report`` runs the self-test.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .. import config

REMOVED = "<removed>"
#: Keys whose values are private wherever they appear (compared in lower case).
PRIVATE_KEYS = frozenset({
    "sn", "dev_id", "device_id", "serial", "serial_number", "serialnumber", "serial_no",
    "access_code", "accesscode", "password", "passwd", "pwd", "token", "access_token", "refresh_token",
    "api_key", "apikey", "secret", "ip", "ip_address", "ipaddress", "ip_addr", "host", "hostname",
    "mac", "mac_address", "macaddress", "ssid", "wifi_ssid", "bssid", "user_id", "userid", "uid",
    "username", "user_name", "email", "dev_name", "printer_name", "region", "certificate", "fingerprint",
})
#: Keys whose whole value is dropped (the Bambu ``net`` block holds IPs as integers).
PRIVATE_BLOCKS = frozenset({"net", "ipcam", "camera", "rtsp"})
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I)


def redact(value: Any, secrets: Optional[List[str]] = None) -> Any:
    """Return a copy of ``value`` without private values (see the module docstring).

    ``secrets`` are exact strings (for example the serial number) removed
    wherever they appear inside other values.
    """
    known = [secret for secret in (secrets or []) if secret and len(secret) >= 4]

    def clean_text(text: str) -> str:
        for secret in known:
            text = text.replace(secret, REMOVED)
        text = _URL.sub(REMOVED, text)
        text = _IPV4.sub(REMOVED, text)
        return _MAC.sub(REMOVED, text)

    def walk(item: Any) -> Any:
        if isinstance(item, Mapping):
            cleaned: Dict[str, Any] = {}
            for key, child in item.items():
                lowered = str(key).lower()
                if lowered in PRIVATE_BLOCKS:
                    cleaned[key] = REMOVED
                elif lowered in PRIVATE_KEYS or lowered.endswith(("_url", "_sn", "_serial", "_token", "_key", "_ip")):
                    cleaned[key] = REMOVED if child not in (None, "") else child
                else:
                    cleaned[key] = walk(child)
            return cleaned
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, str):
            return clean_text(item)
        return item

    return walk(value)


def _secrets_of(*values: Any) -> List[str]:
    """Serial numbers and similar ids found in the raw data, to remove them everywhere."""
    found: List[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if str(key).lower() in ("sn", "dev_id", "serial", "serial_number") and isinstance(child, str):
                    found.append(child)
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    for value in values:
        walk(value)
    return found


def collect(settings: Mapping[str, Any]) -> Dict[str, Any]:
    """Read the raw status of every printer of the selected source (network, slow)."""
    from . import printer_sources

    if not printer_sources.is_configured(settings):
        raise RuntimeError("No printer is connected. Set one up in Settings > Connections first.")
    source = printer_sources.source_from_settings(settings)
    printers: List[Dict[str, Any]] = []
    source_id = str(getattr(source, "source_id", ""))
    if source_id == "bambu_lan":
        from . import bambu_lan

        for printer in source.printers:
            state = bambu_lan.cached_state(printer, force=True)
            printers.append({"model": printer.model, "report": state.report, "version": state.version})
    elif source_id == "bambuddy":
        client = source._client()
        for printer in client.get_printers():
            if not printer.is_active:
                continue
            printers.append({"model": printer.model, "status": client.get_printer_status(printer.id)})
    else:
        raise RuntimeError("Printer reports are not available for {} yet.".format(getattr(source, "label", source_id)))
    raw = {"filamatch_version": config.VERSION, "created": time.strftime("%Y-%m-%d %H:%M"), "source": source_id,
           "printers": printers}
    return redact(raw, _secrets_of(raw) + _settings_secrets(settings))


def _settings_secrets(settings: Mapping[str, Any]) -> List[str]:
    """The printer addresses, serials and codes the user typed in, to remove them too."""
    values: List[str] = []
    for printer in settings.get("bambu_lan_printers") or []:
        if isinstance(printer, Mapping):
            values += [str(printer.get(key) or "") for key in ("ip", "serial", "access_code")]
    values += [str(settings.get(key) or "") for key in ("bambuddy_url", "bambuddy_api_key")]
    return [value for value in values if value]


def save(report: Mapping[str, Any], folder: Optional[Path] = None) -> Path:
    """Write the report as ``printer-report-<model>-<date>.json`` and return the path."""
    folder = Path(folder) if folder is not None else config.DOCUMENTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    models = [str(printer.get("model") or "printer") for printer in report.get("printers", [])]
    name = "printer-report-{}-{}.json".format(
        re.sub(r"[^A-Za-z0-9]+", "-", "-".join(models) or "printer").strip("-"), time.strftime("%Y%m%d-%H%M%S")
    )
    target = folder / name
    descriptor, temporary = tempfile.mkstemp(prefix=".printer-report-", suffix=".json", dir=str(folder))
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    os.replace(temporary, str(target))
    return target


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


FAKE_SERIAL = "0948FAKE0123456"
FAKE_CODE = "87654321"
FAKE_IP = "192.168.1.30"
FAKE_MAC = "AA:BB:CC:11:22:33"
FAKE_SSID = "FakeHomeWifi"


def _fake_report() -> Dict[str, Any]:
    return {
        "sn": FAKE_SERIAL,
        "ams": {"ams": [{"id": "0", "info": "1003", "tray": [{"id": "0", "tray_type": "PLA", "tray_color": "000000FF",
                                                                "tray_uuid": "ABCD", "remain": 80}]}]},
        "device": {"extruder": {"info": [{"id": 0, "temp": 220}, {"id": 1, "temp": 0}]},
                   "nozzle": {"info": [{"id": 0, "type": "HS01", "diameter": 0.4}]}},
        "net": {"conf": 16, "info": [{"ip": 674632714, "mask": 16777215}]},
        "ipcam": {"rtsp_url": "rtsps://bblp:{}@{}:322/streaming/live/1".format(FAKE_CODE, FAKE_IP)},
        "upgrade_state": {"sn": FAKE_SERIAL, "new_ver_list": [{"name": "ams/0", "sn": "19C06FAKE00001", "sw_ver": "00.00.06.15"}]},
        "wifi": {"ssid": FAKE_SSID, "mac": FAKE_MAC},
        "msg": "connected to {} from {}".format(FAKE_IP, FAKE_MAC),
        "user_id": "1234567",
        "home_flag": 1024,
    }


def _check_redact() -> None:
    raw = {"printers": [{"model": "H2D", "report": _fake_report(),
                         "status": {"serial_number": FAKE_SERIAL, "ip_address": FAKE_IP, "access_code": FAKE_CODE,
                                    "api_key": "bb_fakekey", "name": "Workshop H2D"}}]}
    cleaned = redact(raw, _secrets_of(raw))
    text = json.dumps(cleaned)
    for secret in (FAKE_SERIAL, FAKE_CODE, FAKE_IP, FAKE_MAC, FAKE_SSID, "19C06FAKE00001", "674632714", "bb_fakekey", "1234567"):
        _expect(secret not in text, "{} must not survive: {}".format(secret, text))
    report = cleaned["printers"][0]["report"]
    _expect(report["ams"]["ams"][0]["tray"][0]["tray_type"] == "PLA" and report["ams"]["ams"][0]["info"] == "1003",
            "tray and AMS data stay")
    _expect(report["device"]["nozzle"]["info"][0]["type"] == "HS01" and report["home_flag"] == 1024,
            "nozzle data and flags stay")
    _expect(report["net"] == REMOVED and report["ipcam"] == REMOVED, "the net block and the camera block go")
    _expect(cleaned["printers"][0]["status"]["name"] == "Workshop H2D", "the printer's name stays")


def _check_fixtures_survive() -> None:
    fixtures = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
    for path in (fixtures / "bambu_lan" / "h2d_report.json", fixtures / "bambuddy" / "h2d_status.json"):
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        cleaned = redact(data, _secrets_of(data))
        _expect(json.dumps(cleaned).count('"tray_type"') == json.dumps(data).count('"tray_type"'),
                "{}: every tray is kept".format(path.name))


def _check_save() -> None:
    with tempfile.TemporaryDirectory(prefix="filamatch-report-test-") as temp:
        path = save({"printers": [{"model": "H2D Pro"}], "source": "bambu_lan"}, Path(temp))
        _expect(path.name.startswith("printer-report-H2D-Pro-") and path.suffix == ".json", "file name: {}".format(path.name))
        _expect(json.loads(path.read_text())["source"] == "bambu_lan", "the report is written as JSON")
        _expect(len(list(Path(temp).iterdir())) == 1, "no temporary file is left")


def _selftest() -> int:
    scenarios = (
        ("private values are removed, printer data is kept", _check_redact),
        ("the test fixtures keep every tray", _check_fixtures_survive),
        ("the report is saved as one JSON file", _check_save),
    )
    failures = 0
    for label, check in scenarios:
        try:
            check()
        except Exception:
            failures += 1
            print("FAIL:", label)
            traceback.print_exc()
        else:
            print("PASS:", label)
    if failures:
        print("{} printer_report self-test(s) FAILED".format(failures))
        return 1
    print("PASS: all printer_report self-tests completed")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
