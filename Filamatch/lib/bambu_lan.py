"""Read AMS slots straight from Bambu Lab printers on the local network.

Bambu printers run an MQTT broker on port 8883 (TLS).  The user name is
``bblp`` and the password is the access code shown on the printer screen.
This works in LAN-only mode and in cloud mode, as long as the computer can
reach the printer; developer mode is not needed.

Filamatch only reads.  It sends exactly two requests, both listed in
:data:`READ_ONLY_REQUESTS`: ``pushall`` (send the full status report) and
``get_version`` (list the modules).  It never sends a command that moves,
heats, prints or changes a setting; the self-test checks that every request
goes through that list.

TLS: the printer certificate is checked against the Bambu Lab CAs bundled in
``data/bambu_ca.pem`` and its common name must be the printer serial.  When a
printer has a certificate from an unknown CA, the user can trust that one
certificate; its SHA-256 fingerprint is then pinned in the settings.

Filament names come from the printer's filament id (``tray_info_idx``, the
same id as Orca's ``filament_id``), looked up in the installed Bambu Studio /
OrcaSlicer presets.

This module uses the standard library only and imports outside Fusion.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import ssl
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import mqtt_lite
from .printer_model import (
    EXTERNAL_UNIT_ID,
    PrinterContext,
    PrinterInfo,
    SlotInfo,
    SpoolInfo,
    normalize_color_hex,
)

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: Source id in the printer source registry and the ``printer_source`` setting.
SOURCE_ID = "bambu_lan"

#: Settings key holding the list of LAN printers.
SETTINGS_KEY = "bambu_lan_printers"

#: Keychain account prefix for access codes (see :func:`secret_account`).
SECRET_PREFIX = "bambu_lan:"

MQTT_PORT = 8883
MQTT_USER = "bblp"

#: Bambu Lab CA certificates (from ha-bambulab, MIT).
CA_FILE = Path(__file__).resolve().parent.parent / "data" / "bambu_ca.pem"

#: Seconds to wait for the TCP connection and the TLS handshake.
CONNECT_TIMEOUT_S = 5.0

#: Seconds to wait for the status report after the request.
REPORT_TIMEOUT_S = 8.0

#: Seconds to wait for the module list once the report is in.
VERSION_GRACE_S = 2.0

#: Shortest time between two full reports from one printer.  P1 and A1
#: printers slow down when asked for a full report often, so they get longer.
MIN_INTERVAL_S = 20.0
SLOW_MIN_INTERVAL_S = 300.0
SLOW_MODELS = frozenset({"P1P", "P1S", "A1", "A1 mini"})

#: The only requests Filamatch ever sends.  Both only ask for information.
READ_ONLY_REQUESTS: Dict[str, Dict[str, Any]] = {
    "pushall": {
        "pushing": {"sequence_id": "0", "command": "pushall", "version": 1, "push_target": 1}
    },
    "get_version": {"info": {"sequence_id": "0", "command": "get_version"}},
}

#: Model by the first three characters of the serial number.  The model name
#: from ``get_version`` wins when the printer sends one.
MODEL_BY_SERIAL_PREFIX: Dict[str, str] = {
    "00M": "X1C",
    "00W": "X1",
    "03W": "X1E",
    "01S": "P1P",
    "01P": "P1S",
    "030": "A1 mini",
    "039": "A1",
    "094": "H2D",
}

#: AMS HT units use ids 128-135; each has one slot.
AMS_HT_FIRST_ID = 128
AMS_HT_LAST_ID = 135

#: ``vir_slot`` ids on dual-nozzle printers: 255 feeds the right (main)
#: nozzle, 254 the left (deputy) nozzle.  Single-nozzle printers report one
#: ``vt_tray``.  As a slot, the external spool is tray ``id - 254`` of unit
#: 255 (left 0, right 1), the numbering Bambuddy uses for its spool links.
VIRTUAL_MAIN_ID = 255
VIRTUAL_DEPUTY_ID = 254

#: Extruder ids as the printer reports them (bits 8-11 of the AMS ``info``).
EXTRUDER_RIGHT = 0
EXTRUDER_LEFT = 1

_EMPTY_UUID_CHARS = frozenset("0")


class BambuLanError(Exception):
    """A printer could not be read; the message is safe to show the user."""


class BambuAuthError(BambuLanError):
    """The printer refused the access code."""


class UntrustedCertificate(BambuLanError):
    """The printer certificate is not from a known Bambu CA and is not pinned."""

    def __init__(self, message: str, fingerprint: str, serial: str = "") -> None:
        super().__init__(message)
        self.fingerprint = fingerprint
        self.serial = serial


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LanPrinter:
    """One printer the user added in Settings."""

    id: int
    name: str
    model: str
    ip: str
    serial: str
    fingerprint: str = ""
    access_code: str = ""

    @property
    def serial_tail(self) -> str:
        """Last four characters of the serial, enough to tell printers apart."""
        return self.serial[-4:]

    def to_settings(self) -> Dict[str, Any]:
        """Settings form (the palette bridge moves ``access_code`` to the keychain)."""
        return {
            "id": self.id,
            "name": self.name,
            "model": self.model,
            "ip": self.ip,
            "serial": self.serial,
            "fingerprint": self.fingerprint,
            "access_code": self.access_code,
        }

    def public(self) -> Dict[str, Any]:
        """Form safe to send to the palette: no access code, no full serial."""
        return {
            "id": self.id,
            "name": self.name,
            "model": self.model,
            "ip": self.ip,
            "serial_tail": self.serial_tail,
            "certificate": "pinned" if self.fingerprint else "bambu",
        }


def printer_id_for_serial(serial: str) -> int:
    """Stable printer id from the serial (the same after remove and add)."""
    return 1 + int(hashlib.sha1(serial.encode("utf-8")).hexdigest()[:7], 16)


def secret_account(serial: str) -> str:
    """Keychain account that holds the access code of one printer.

    A hash of the serial, so keychain warnings in the log do not show it.
    """
    return SECRET_PREFIX + hashlib.sha1(serial.encode("utf-8")).hexdigest()[:12]


def printers_from_settings(settings: Mapping[str, Any]) -> List[LanPrinter]:
    """Return the LAN printers saved in the settings (bad entries skipped)."""
    raw = settings.get(SETTINGS_KEY)
    if not isinstance(raw, list):
        return []
    printers: List[LanPrinter] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        serial = str(item.get("serial") or "").strip()
        ip = str(item.get("ip") or "").strip()
        if not serial or not ip:
            continue
        try:
            printer_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            printer_id = 0
        printers.append(
            LanPrinter(
                id=printer_id or printer_id_for_serial(serial),
                name=str(item.get("name") or "").strip() or model_for(serial, ""),
                model=str(item.get("model") or "").strip() or model_for(serial, ""),
                ip=ip,
                serial=serial,
                fingerprint=_clean_fingerprint(item.get("fingerprint")),
                access_code=str(item.get("access_code") or "").strip(),
            )
        )
    return printers


def is_configured(settings: Mapping[str, Any]) -> bool:
    """Return whether at least one LAN printer is saved."""
    return bool(printers_from_settings(settings))


def _clean_fingerprint(value: Any) -> str:
    return str(value or "").replace(":", "").strip().lower()


def model_for(serial: str, product_name: str = "") -> str:
    """Return the printer model from ``get_version`` or the serial prefix."""
    name = product_name.strip()
    if name.lower().startswith("bambu lab "):
        name = name[len("bambu lab "):].strip()
    if name:
        return name
    return MODEL_BY_SERIAL_PREFIX.get(serial[:3].upper(), "Bambu Lab printer")


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def request_payload(name: str) -> bytes:
    """Return the JSON of one allowed request; any other name raises KeyError."""
    return json.dumps(READ_ONLY_REQUESTS[name], separators=(",", ":")).encode("utf-8")


def _send_request(client: mqtt_lite.MqttClient, serial: str, name: str) -> None:
    """Send one request from :data:`READ_ONLY_REQUESTS` (the only publish)."""
    client.publish("device/{}/request".format(serial), request_payload(name))


def _tls_context(verify: bool) -> ssl.SSLContext:
    """TLS settings that work with Bambu printers.

    The printers present certificates without the extensions Python's strict
    X.509 mode wants, and some firmware fails TLS 1.3, so strict mode is off
    and TLS 1.2 is the maximum.  The host name is not checked; the serial in
    the certificate is checked instead.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    if verify:
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=str(CA_FILE))
        strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict:
            context.verify_flags &= ~strict
    else:
        context.verify_mode = ssl.CERT_NONE
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    return context


def der_common_names(der: bytes) -> List[str]:
    """Return the common names in a DER certificate (issuer first, then subject).

    A small reader so an unverified certificate can show its serial without
    an extra package.  It looks for the commonName OID (2.5.4.3) and reads the
    string after it.
    """
    names: List[str] = []
    marker = b"\x06\x03\x55\x04\x03"
    start = 0
    while True:
        index = der.find(marker, start)
        if index < 0:
            return names
        cursor = index + len(marker)
        if cursor + 2 <= len(der) and der[cursor] in (0x0C, 0x13, 0x14, 0x16):
            length = der[cursor + 1]
            if length < 0x80 and cursor + 2 + length <= len(der):
                names.append(der[cursor + 2:cursor + 2 + length].decode("utf-8", "replace"))
        start = cursor


def _subject_common_name(peer: Mapping[str, Any]) -> str:
    for rdn in peer.get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return str(value)
    return ""


def _open_tls(ip: str, verify: bool) -> ssl.SSLSocket:
    raw = socket.create_connection((ip, MQTT_PORT), timeout=CONNECT_TIMEOUT_S)
    try:
        return _tls_context(verify).wrap_socket(raw, server_hostname=ip if _is_hostname(ip) else None)
    except Exception:
        raw.close()
        raise


def _is_hostname(address: str) -> bool:
    """True for a DNS name; SNI must not carry an IP address."""
    try:
        socket.inet_pton(socket.AF_INET, address)
        return False
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, address)
        return False
    except OSError:
        return True


def connect_tls(ip: str, fingerprint: str = "") -> Tuple[ssl.SSLSocket, str, str, str]:
    """Open a checked TLS connection to the printer.

    Returns ``(socket, serial, certificate, fingerprint)``.  ``certificate``
    is ``"bambu"`` (Bambu CA) or ``"pinned"`` (fingerprint the user
    trusted).  Raises :class:`UntrustedCertificate` when neither applies.
    """
    try:
        tls = _open_tls(ip, verify=True)
    except ssl.SSLCertVerificationError:
        tls = None
    except (OSError, ssl.SSLError) as exc:
        raise BambuLanError(_network_message(ip, exc)) from exc
    if tls is not None:
        der = tls.getpeercert(binary_form=True) or b""
        serial = _subject_common_name(tls.getpeercert() or {})
        if not serial:
            tls.close()
            raise BambuLanError("The printer certificate has no serial number.")
        return tls, serial, "bambu", hashlib.sha256(der).hexdigest()

    try:
        tls = _open_tls(ip, verify=False)
    except (OSError, ssl.SSLError) as exc:
        raise BambuLanError(_network_message(ip, exc)) from exc
    der = tls.getpeercert(binary_form=True) or b""
    seen = hashlib.sha256(der).hexdigest()
    names = der_common_names(der)
    serial = names[-1] if names else ""
    if fingerprint and _clean_fingerprint(fingerprint) == seen and serial:
        return tls, serial, "pinned", seen
    tls.close()
    raise UntrustedCertificate(
        "The printer certificate is not from Bambu Lab. Trust it only if this "
        "is your printer.",
        fingerprint=seen,
        serial=serial,
    )


def _network_message(ip: str, exc: BaseException) -> str:
    if isinstance(exc, socket.timeout):
        return "The printer at {} did not answer. Check the IP address and that the printer is on.".format(ip)
    if isinstance(exc, ConnectionRefusedError):
        return "The printer at {} refused the connection. Is it a Bambu Lab printer?".format(ip)
    if isinstance(exc, ssl.SSLError):
        return "Secure connection to {} failed ({}).".format(ip, exc.__class__.__name__)
    return "Could not reach the printer at {} ({}).".format(ip, exc.__class__.__name__)


@dataclass(frozen=True)
class PrinterState:
    """One read of a printer: its full report and module list."""

    serial: str
    report: Mapping[str, Any]
    version: Mapping[str, Any]
    certificate: str
    fingerprint: str
    read_at: float = 0.0


def read_printer_state(
    ip: str,
    access_code: str,
    expected_serial: str = "",
    fingerprint: str = "",
    report_timeout: float = REPORT_TIMEOUT_S,
) -> PrinterState:
    """Connect, ask for the full report and the module list, and disconnect."""
    if not access_code:
        raise BambuAuthError("Enter the access code from the printer screen.")
    tls, serial, certificate, seen = connect_tls(ip, fingerprint)
    if expected_serial and serial != expected_serial:
        tls.close()
        raise BambuLanError(
            "The printer at {} is another printer now (serial ends in {}). "
            "Remove it and add it again.".format(ip, serial[-4:])
        )
    client = mqtt_lite.MqttClient(tls, timeout=CONNECT_TIMEOUT_S)
    try:
        return _read_session(client, serial, access_code, certificate, seen, report_timeout)
    except mqtt_lite.MqttAuthError as exc:
        raise BambuAuthError(
            "The printer refused the access code. Check it on the printer screen."
        ) from exc
    except (mqtt_lite.MqttError, OSError) as exc:
        raise BambuLanError(_network_message(ip, exc)) from exc
    finally:
        client.close()


def _read_session(
    client: mqtt_lite.MqttClient,
    serial: str,
    access_code: str,
    certificate: str,
    fingerprint: str,
    report_timeout: float,
) -> PrinterState:
    client.connect("filamatch-" + uuid.uuid4().hex[:8], MQTT_USER, access_code)
    client.subscribe("device/{}/report".format(serial))
    _send_request(client, serial, "pushall")
    _send_request(client, serial, "get_version")
    report: Optional[Mapping[str, Any]] = None
    version: Mapping[str, Any] = {}
    deadline = time.monotonic() + report_timeout
    while time.monotonic() < deadline:
        message = client.read_message(max(0.1, deadline - time.monotonic()))
        if message is None:
            break
        try:
            payload = json.loads(message[1].decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(payload, Mapping):
            continue
        body = payload.get("print")
        if isinstance(body, Mapping) and isinstance(body.get("ams"), Mapping) and report is None:
            report = body
        info = payload.get("info")
        if isinstance(info, Mapping) and info.get("command") == "get_version":
            version = info
        if report is not None and version:
            break
        if report is not None:
            # Older firmware may not answer get_version; wait only a little.
            deadline = min(deadline, time.monotonic() + VERSION_GRACE_S)
    if report is None:
        raise BambuLanError("The printer did not send its status. Try again in a few seconds.")
    return PrinterState(serial, report, version, certificate, fingerprint, time.time())


# -- cache --------------------------------------------------------------------

_CACHE: Dict[str, PrinterState] = {}
_CACHE_LOCK = threading.Lock()


def min_interval(model: str) -> float:
    """Shortest time between two full reports for this model."""
    return SLOW_MIN_INTERVAL_S if model in SLOW_MODELS else MIN_INTERVAL_S


def cached_state(printer: LanPrinter, force: bool = False) -> PrinterState:
    """Return a recent report, reading the printer only when the cache is old."""
    with _CACHE_LOCK:
        state = _CACHE.get(printer.serial)
    if state is not None and not force and time.time() - state.read_at < min_interval(printer.model):
        return state
    state = read_printer_state(printer.ip, printer.access_code, printer.serial, printer.fingerprint)
    with _CACHE_LOCK:
        _CACHE[printer.serial] = state
    return state


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# Filament names
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilamentPreset:
    """Name and vendor of one slicer filament preset."""

    filament_id: str
    name: str
    vendor: str


_PRESETS: Optional[Dict[str, FilamentPreset]] = None
_PRESETS_LOCK = threading.Lock()
_PRESET_DIRS_OVERRIDE: Optional[List[Path]] = None


def _preset_dirs() -> List[Path]:
    """Folders that hold Bambu Studio / OrcaSlicer filament presets."""
    if _PRESET_DIRS_OVERRIDE is not None:
        return list(_PRESET_DIRS_OVERRIDE)
    home = Path.home()
    if sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        apps = [Path("/Applications/OrcaSlicer.app/Contents/Resources/profiles")]
    elif os.name == "nt":
        support = Path(os.environ.get("APPDATA") or home)
        program_files = Path(os.environ.get("ProgramFiles") or "C:\\Program Files")
        apps = [program_files / "OrcaSlicer" / "resources" / "profiles"]
    else:
        support = home / ".config"
        apps = []
    dirs = [support / "BambuStudio" / "system" / "BBL" / "filament"]
    dirs += [app / "BBL" / "filament" for app in apps]
    dirs += [app / "OrcaFilamentLibrary" / "filament" for app in apps]
    for slicer in ("BambuStudio", "OrcaSlicer"):
        user_root = support / slicer / "user"
        try:
            dirs += [user / "filament" for user in sorted(user_root.iterdir()) if user.is_dir()]
        except OSError:
            pass
    return dirs


def _clean_preset_name(name: str) -> str:
    return name.split(" @", 1)[0].strip()


def _first(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def _load_presets() -> Dict[str, FilamentPreset]:
    presets: Dict[str, FilamentPreset] = {}
    for folder in _preset_dirs():
        try:
            files = sorted(folder.rglob("*.json"))
        except OSError:
            continue
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if '"filament_id"' not in text:
                continue
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if not isinstance(data, Mapping):
                continue
            filament_id = _first(data.get("filament_id"))
            name = _clean_preset_name(_first(data.get("name")))
            if not filament_id or not name or filament_id in presets:
                continue
            presets[filament_id] = FilamentPreset(filament_id, name, _first(data.get("filament_vendor")))
    logger.debug("Loaded %d filament preset names", len(presets))
    return presets


def filament_preset(filament_id: str) -> Optional[FilamentPreset]:
    """Return the slicer preset for a Bambu filament id, if one is installed."""
    global _PRESETS
    if not filament_id:
        return None
    with _PRESETS_LOCK:
        if _PRESETS is None:
            _PRESETS = _load_presets()
        return _PRESETS.get(filament_id)


def _reset_presets(dirs: Optional[List[Path]] = None) -> None:
    global _PRESETS, _PRESET_DIRS_OVERRIDE
    with _PRESETS_LOCK:
        _PRESETS = None
        _PRESET_DIRS_OVERRIDE = dirs


# ---------------------------------------------------------------------------
# Report parsing
# ---------------------------------------------------------------------------


def _int(value: Any, default: int = -1) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> Optional[float]:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number


def extruder_of_unit(info: Any) -> Optional[int]:
    """Nozzle that feeds an AMS unit, from bits 8-11 of its ``info`` hex text.

    ``None`` when the unit is not fixed to one nozzle (``0xE``: an AMS on a
    Filament Track Switch reaches both) or the value is unknown.
    """
    try:
        value = (int(str(info), 16) >> 8) & 0x0F
    except (TypeError, ValueError):
        return None
    return value if value in (EXTRUDER_RIGHT, EXTRUDER_LEFT) else None


def _material_subtype(material: str, preset: Optional[FilamentPreset], sub_brand: str) -> str:
    """Words that tell this filament apart inside its material (``Matte``, ``95A HF``)."""
    material_words = {word.upper() for word in material.replace("-", " ").split()}
    material_words.add(material.upper())
    if preset is not None:
        source = preset.name
        skip = {"BAMBU", "GENERIC"} | {word.upper() for word in preset.vendor.split()}
    else:
        source = sub_brand
        skip = set()
    words = [
        word for word in source.split()
        if word.upper() not in material_words and word.upper() not in skip
    ]
    return " ".join(words)


def _brand(filament_id: str, preset: Optional[FilamentPreset], sub_brand: str) -> str:
    if preset is not None:
        if preset.vendor:
            return preset.vendor
        if preset.name.lower().startswith("generic"):
            return "Generic"
    if filament_id.startswith("GF") and sub_brand:
        return "Bambu Lab"
    return ""


def _spool_id(filament_id: str, material: str, color_hex: str, subtype: str) -> int:
    """Stable id for one kind of filament.

    The printer has no spool inventory, so the same filament in two slots is
    one "spool"; it becomes one Fusion material.  Ids start at 10^9 so they do
    not meet Bambuddy spool ids.
    """
    key = "|".join((filament_id, material.upper(), color_hex.upper(), subtype.lower()))
    return 1_000_000_000 + int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:8], 16) % 1_000_000_000


def _tray_loaded(tray: Mapping[str, Any]) -> bool:
    return bool(str(tray.get("tray_type") or "").strip())


def _has_rfid(tray: Mapping[str, Any]) -> bool:
    uuid_text = str(tray.get("tray_uuid") or "").strip()
    return bool(uuid_text) and not set(uuid_text) <= _EMPTY_UUID_CHARS


def parse_tray(
    printer: PrinterInfo,
    tray: Mapping[str, Any],
    ams_id: int,
    tray_id: int,
    custom_label: str = "",
    extruder: Optional[int] = None,
    external: bool = False,
) -> SlotInfo:
    """Turn one AMS tray (or external spool) of the report into a slot."""
    common = dict(
        printer_id=printer.id,
        printer_name=printer.name,
        ams_id=ams_id,
        tray_id=tray_id,
        custom_label=custom_label,
        extruder=extruder,
    )
    if not _tray_loaded(tray):
        return SlotInfo(loaded=False, **common)
    material = str(tray.get("tray_type") or "").strip()
    filament_id = str(tray.get("tray_info_idx") or "").strip()
    sub_brand = str(tray.get("tray_sub_brands") or "").strip()
    color_hex = normalize_color_hex(tray.get("tray_color"))
    preset = filament_preset(filament_id)
    subtype = _material_subtype(material, preset, sub_brand)
    remain = _int(tray.get("remain"))
    if external and not _has_rfid(tray):
        remain = -1  # the external holder has no sensor
    percent = float(remain) if 0 <= remain <= 100 else None
    label_weight = _float(tray.get("tray_weight"))
    if label_weight is not None and label_weight <= 0:
        label_weight = None
    remaining_g = label_weight * percent / 100.0 if label_weight and percent is not None else None
    spool = SpoolInfo(
        id=_spool_id(filament_id, material, color_hex, subtype),
        material=material,
        brand=_brand(filament_id, preset, sub_brand),
        color_name="",
        color_hex=color_hex,
        remaining_g=remaining_g,
        label_weight_g=label_weight,
        weight_used_g=None,
        slicer_filament=filament_id,
        slicer_filament_name=preset.name if preset is not None else "",
        subtype=subtype,
        location="{} - {}".format(printer.name, custom_label or _default_label(ams_id, tray_id)),
    )
    return SlotInfo(
        spool=spool,
        filament_type=material,
        color_hex=color_hex,
        loaded=True,
        vendor_filament_id=filament_id,
        remaining_percent=percent,
        **common,
    )


def _default_label(ams_id: int, tray_id: int) -> str:
    if ams_id == EXTERNAL_UNIT_ID:
        return "External - T{}".format(tray_id + 1)
    return "AMS {} - T{}".format(ams_id + 1, tray_id + 1)


def _side(extruder: Optional[int]) -> str:
    if extruder == EXTRUDER_LEFT:
        return "L"
    if extruder == EXTRUDER_RIGHT:
        return "R"
    return ""


def is_dual_nozzle(report: Mapping[str, Any]) -> bool:
    """True when the report lists two extruders (H2D)."""
    device = report.get("device")
    extruder = device.get("extruder") if isinstance(device, Mapping) else None
    info = extruder.get("info") if isinstance(extruder, Mapping) else None
    return isinstance(info, list) and len(info) >= 2


def parse_slots(printer: PrinterInfo, report: Mapping[str, Any]) -> List[SlotInfo]:
    """Return every AMS, AMS HT and external slot in a full report."""
    dual = is_dual_nozzle(report)
    ams = report.get("ams")
    units = ams.get("ams") if isinstance(ams, Mapping) else None
    units = [unit for unit in units if isinstance(unit, Mapping)] if isinstance(units, list) else []
    extruders: Dict[int, int] = {}
    if dual:
        for unit in units:
            extruder = extruder_of_unit(unit.get("info"))
            if extruder is not None:
                extruders[_int(unit.get("id"))] = extruder
    virtual = report.get("vir_slot")
    if dual and isinstance(virtual, list):
        externals: List[Mapping[str, Any]] = [tray for tray in virtual if isinstance(tray, Mapping)]
    else:
        tray = report.get("vt_tray")
        externals = [tray] if isinstance(tray, Mapping) and tray else []
    return build_slots(printer, units, externals, extruders, dual)


def build_slots(
    printer: PrinterInfo,
    units: Iterable[Mapping[str, Any]],
    externals: Iterable[Mapping[str, Any]],
    extruders: Mapping[int, int],
    dual: bool,
) -> List[SlotInfo]:
    """Turn AMS units and external spools into slots.

    Shared by the LAN reader and the Bambuddy live status, so both name and
    number the slots the same way.  ``extruders`` maps an AMS id to the
    nozzle it feeds (dual-nozzle printers).  External spools use unit
    :data:`EXTERNAL_UNIT_ID`; on a dual-nozzle printer the left spool (id
    254) is tray 0 and the right spool (id 255) is tray 1, as in Bambuddy.
    """
    slots: List[SlotInfo] = []
    ht_index = 0
    for unit in units:
        unit_id = _int(unit.get("id"))
        if unit_id < 0:
            continue
        extruder = extruders.get(unit_id) if dual else None
        side = _side(extruder)
        is_ht = AMS_HT_FIRST_ID <= unit_id <= AMS_HT_LAST_ID
        if is_ht:
            ht_index += 1
        trays = unit.get("tray")
        for tray in trays if isinstance(trays, list) else []:
            if not isinstance(tray, Mapping):
                continue
            tray_id = _int(tray.get("id"))
            if tray_id < 0:
                continue
            if is_ht:
                label = "AMS HT {}".format(ht_index)
            elif side:
                label = "AMS {} - T{}".format(unit_id + 1, tray_id + 1)
            else:
                label = ""
            if side:
                label = "{} ({})".format(label, side)
            slots.append(parse_tray(printer, tray, unit_id, tray_id, label, extruder))
    for tray in externals:
        if dual:
            slot_id = _int(tray.get("id"))
            if slot_id not in (VIRTUAL_MAIN_ID, VIRTUAL_DEPUTY_ID):
                continue
            extruder = EXTRUDER_RIGHT if slot_id == VIRTUAL_MAIN_ID else EXTRUDER_LEFT
            tray_index = slot_id - VIRTUAL_DEPUTY_ID
            label = "External ({})".format(_side(extruder))
        else:
            if any(slot.ams_id == EXTERNAL_UNIT_ID for slot in slots):
                continue
            extruder, tray_index, label = None, 0, "External"
        slots.append(parse_tray(printer, tray, EXTERNAL_UNIT_ID, tray_index, label, extruder, external=True))
    slots.sort(key=lambda slot: (slot.ams_id == EXTERNAL_UNIT_ID, slot.ams_id, slot.tray_id))
    return slots


def product_name(version: Mapping[str, Any]) -> str:
    """Printer product name from ``get_version`` (empty on older firmware)."""
    modules = version.get("module") if isinstance(version, Mapping) else None
    for module in modules if isinstance(modules, list) else []:
        if isinstance(module, Mapping) and module.get("name") == "ota":
            return str(module.get("product_name") or "")
    return ""


def unit_summary(slots: Sequence[SlotInfo]) -> List[Dict[str, Any]]:
    """Group slots into units for the Settings test result."""
    units: List[Dict[str, Any]] = []
    by_unit: Dict[str, Dict[str, Any]] = {}
    for slot in slots:
        if slot.ams_id == EXTERNAL_UNIT_ID:
            unit_label = "External"
        elif AMS_HT_FIRST_ID <= slot.ams_id <= AMS_HT_LAST_ID:
            unit_label = slot.slot_label
        else:
            unit_label = "AMS {}".format(slot.ams_id + 1)
            side = _side(slot.extruder)
            if side:
                unit_label += " ({})".format(side)
        unit = by_unit.get(unit_label)
        if unit is None:
            unit = {"label": unit_label, "slots": []}
            by_unit[unit_label] = unit
            units.append(unit)
        unit["slots"].append(
            {
                "label": _short_slot_label(slot),
                "filament": slot_filament_label(slot) if slot.loaded else "",
                "color_hex": slot.color_hex if slot.loaded else "",
                "loaded": slot.loaded,
            }
        )
    return units


def _short_slot_label(slot: SlotInfo) -> str:
    """Slot label inside its unit group: ``T2``, ``Left``, ``Right``."""
    if slot.ams_id == EXTERNAL_UNIT_ID:
        return {EXTRUDER_LEFT: "Left", EXTRUDER_RIGHT: "Right"}.get(slot.extruder, "Spool")
    return "T{}".format(slot.tray_id + 1)


def slot_filament_label(slot: SlotInfo) -> str:
    """Short filament text for a loaded slot (``Bambu PLA Matte``)."""
    spool = slot.spool
    if spool is None:
        return slot.filament_type
    if spool.slicer_filament_name:
        return spool.slicer_filament_name
    return " ".join(part for part in (spool.brand, spool.material_label) if part) or slot.filament_type


# ---------------------------------------------------------------------------
# Probe (Settings test) and slot source
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    """What the Settings test found at one address."""

    serial: str
    model: str
    certificate: str
    fingerprint: str
    slots: Tuple[SlotInfo, ...] = field(default_factory=tuple)

    def public(self) -> Dict[str, Any]:
        """Palette form: no full serial."""
        return {
            "ok": True,
            "serial_tail": self.serial[-4:],
            "model": self.model,
            "name_suggestion": self.model,
            "certificate": self.certificate,
            "units": unit_summary(self.slots),
            "slot_count": len(self.slots),
            "loaded_count": sum(1 for slot in self.slots if slot.loaded),
        }


def probe(ip: str, access_code: str, trust_fingerprint: str = "") -> ProbeResult:
    """Read one printer for the Settings test (no cache)."""
    ip = ip.strip()
    if not ip:
        raise BambuLanError("Enter the printer IP address.")
    state = read_printer_state(ip, access_code.strip(), fingerprint=trust_fingerprint)
    model = model_for(state.serial, product_name(state.version))
    printer = PrinterInfo(id=printer_id_for_serial(state.serial), name=model, model=model)
    slots = parse_slots(printer, state.report)
    with _CACHE_LOCK:
        _CACHE[state.serial] = state
    return ProbeResult(state.serial, model, state.certificate, state.fingerprint, tuple(slots))


class BambuLanSource:
    """Slot source that reads Bambu printers directly over the LAN."""

    source_id = SOURCE_ID
    label = "Bambu printer (LAN)"

    def __init__(self, printers: Sequence[LanPrinter]) -> None:
        self.printers = list(printers)

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> "BambuLanSource":
        return cls(printers_from_settings(settings))

    @staticmethod
    def is_configured(settings: Mapping[str, Any]) -> bool:
        return is_configured(settings)

    def _printer_info(self, printer: LanPrinter) -> PrinterInfo:
        return PrinterInfo(
            id=printer.id,
            name=printer.name,
            model=printer.model,
            serial_number=printer.serial,
            ip_address=printer.ip,
            brand="Bambu Lab",
        )

    def list_printers(self) -> List[PrinterInfo]:
        return [self._printer_info(printer) for printer in self.printers]

    def test_connection(self) -> Dict[str, Any]:
        if not self.printers:
            raise BambuLanError("No Bambu printer is set up.")
        cached_state(self.printers[0], force=True)
        return {"ok": True, "printers": len(self.printers)}

    def fetch_context(self) -> PrinterContext:
        """Read every printer at the same time; one failure is a warning."""
        if not self.printers:
            raise BambuLanError("No Bambu printer is set up.")

        def read(printer: LanPrinter) -> Tuple[LanPrinter, Optional[PrinterState], str]:
            try:
                return printer, cached_state(printer), ""
            except BambuLanError as exc:
                return printer, None, str(exc)
            except Exception as exc:  # keep one bad printer from hiding the others
                logger.exception("Reading %s failed", printer.name)
                return printer, None, "{} ({})".format("Unexpected error", exc.__class__.__name__)

        with ThreadPoolExecutor(max_workers=min(4, len(self.printers))) as pool:
            results = list(pool.map(read, self.printers))
        infos: List[PrinterInfo] = []
        slots: List[SlotInfo] = []
        spools: Dict[int, SpoolInfo] = {}
        warnings: List[str] = []
        for printer, state, error in results:
            info = self._printer_info(printer)
            infos.append(info)
            if state is None:
                warnings.append("{}: {}".format(printer.name, error))
                continue
            for slot in parse_slots(info, state.report):
                slots.append(slot)
                if slot.spool is not None:
                    spools.setdefault(slot.spool.id, slot.spool)
        if not slots and warnings and len(warnings) == len(results):
            raise BambuLanError(warnings[0])
        return PrinterContext(
            printers=tuple(infos),
            spools=tuple(spools.values()),
            slots=tuple(slots),
            fetched_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            source=SOURCE_ID,
            warnings=tuple(warnings),
        )


# ---------------------------------------------------------------------------
# Self-test (no network)
# ---------------------------------------------------------------------------


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _fixture_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "bambu_lan"


def _load_fixture(name: str) -> Dict[str, Any]:
    return json.loads((_fixture_dir() / name).read_text(encoding="utf-8"))


def _check_requests_are_read_only() -> None:
    """Every publish goes through the allowlist, and the allowlist only asks."""
    import inspect

    source = inspect.getsource(sys.modules[__name__])
    marker = "client" + ".publish("
    _expect(source.count(marker) == 1, "only _send_request may publish")
    for name, request in READ_ONLY_REQUESTS.items():
        command = next(iter(request.values())).get("command")
        _expect(command in ("pushall", "get_version"), "unexpected request {}".format(name))
    try:
        request_payload("print")
    except KeyError:
        pass
    else:
        raise AssertionError("a request outside the allowlist was accepted")


def _check_session_sends_only_allowed() -> None:
    """Run a whole read against a fake broker and inspect what was sent."""
    report = json.dumps({"print": {"ams": {"ams": []}, "vt_tray": {}}}).encode()
    version = json.dumps({"info": {"command": "get_version", "module": []}}).encode()
    serial = "TESTSERIAL0001"
    topic = mqtt_lite._string("device/{}/report".format(serial))
    broker = (
        mqtt_lite._packet(mqtt_lite._CONNACK, b"\x00\x00")
        + mqtt_lite._packet(mqtt_lite._SUBACK, b"\x00\x01\x00")
        + mqtt_lite._packet(mqtt_lite._PUBLISH, topic + report)
        + mqtt_lite._packet(mqtt_lite._PUBLISH, topic + version)
    )
    fake = mqtt_lite._FakeSocket(broker)
    client = mqtt_lite.MqttClient(fake)  # type: ignore[arg-type]
    state = _read_session(client, serial, "code", "bambu", "ab", 1.0)
    _expect(state.serial == serial and "ams" in state.report, "report not read")
    sent = bytes(fake.sent)
    allowed = {request_payload(name) for name in READ_ONLY_REQUESTS}
    publishes = 0
    index = 0
    while index < len(sent):
        kind = sent[index]
        length, multiplier, cursor = 0, 1, index + 1
        while True:
            digit = sent[cursor]
            cursor += 1
            length += (digit & 0x7F) * multiplier
            multiplier *= 128
            if not digit & 0x80:
                break
        body = sent[cursor:cursor + length]
        if kind & 0xF0 == mqtt_lite._PUBLISH:
            publishes += 1
            topic_length = int.from_bytes(body[:2], "big")
            _expect(body[2:2 + topic_length] == "device/{}/request".format(serial).encode(), "wrong topic")
            _expect(body[2 + topic_length:] in allowed, "a request outside the allowlist was sent")
        index = cursor + length
    _expect(publishes == 2, "expected two requests, got {}".format(publishes))


def _check_der_common_names() -> None:
    def name(value: bytes) -> bytes:
        return b"\x06\x03\x55\x04\x03\x0c" + bytes([len(value)]) + value

    der = b"\x30\x00" + name(b"BBL CA") + b"\x00\x01" + name(b"01P00A000000001")
    _expect(der_common_names(der) == ["BBL CA", "01P00A000000001"], "CN reader")


def _check_models() -> None:
    _expect(model_for("00M00A000000001") == "X1C", "X1C prefix")
    _expect(model_for("09400A000000001", "Bambu Lab H2D") == "H2D", "product name")
    _expect(model_for("ZZZ", "") == "Bambu Lab printer", "unknown prefix")
    _expect(extruder_of_unit("10002103") == EXTRUDER_LEFT, "left AMS")
    _expect(extruder_of_unit("10001003") == EXTRUDER_RIGHT, "right AMS")
    _expect(extruder_of_unit("10000E03") is None, "an AMS on a track switch has no fixed nozzle")


def _with_test_presets() -> None:
    root = _fixture_dir() / "presets"
    _reset_presets([root])


def _check_h2d_fixture() -> None:
    data = _load_fixture("h2d_report.json")
    printer = PrinterInfo(id=7, name="Workshop H2D", model="H2D")
    slots = parse_slots(printer, data["print"])
    labels = [slot.slot_label for slot in slots]
    _expect(labels[:4] == ["AMS 1 - T1 (L)", "AMS 1 - T2 (L)", "AMS 1 - T3 (L)", "AMS 1 - T4 (L)"], labels)
    _expect(labels[4] == "AMS 2 - T1 (R)", labels)
    _expect("AMS HT 1 (L)" in labels, labels)
    _expect(labels[-2:] == ["External (L)", "External (R)"], labels)
    _expect([slot.key for slot in slots[-2:]] == ["7:255:0", "7:255:1"], "external keys as in Bambuddy")
    by_label = {slot.slot_label: slot for slot in slots}
    ht = by_label["AMS HT 1 (L)"]
    _expect(not ht.loaded and ht.spool is None, "empty AMS HT slot must not be loaded")
    left_external = by_label["External (L)"]
    _expect(not left_external.loaded, "empty external slot must not be loaded")
    right_external = by_label["External (R)"]
    _expect(right_external.loaded and right_external.filament_type == "TPU", "external TPU")
    _expect(right_external.remaining_percent is None, "external holder has no sensor")
    _expect(right_external.extruder == EXTRUDER_RIGHT, "external 255 feeds the right nozzle")
    first = by_label["AMS 1 - T1 (L)"]
    _expect(first.spool is not None and first.spool.slicer_filament == "GFB50", "filament id")
    _expect(first.spool.slicer_filament_name == "Bambu ABS-GF", first.spool.slicer_filament_name)
    _expect(first.spool.brand == "Bambu Lab", first.spool.brand)
    _expect(first.remaining_percent == 62.0 and first.spool.remaining_g == 620.0, "remaining")
    _expect(first.extruder == EXTRUDER_LEFT, "AMS 1 feeds the left nozzle")
    matte = by_label["AMS 1 - T2 (L)"]
    _expect(matte.spool.material_label == "PLA Matte", matte.spool.material_label)
    twin = by_label["AMS 1 - T3 (L)"]
    _expect(matte.spool.id == twin.spool.id, "the same filament in two slots is one spool")
    polylite = by_label["AMS 2 - T1 (R)"]
    _expect(polylite.spool.brand == "Polymaker" and polylite.remaining_percent is None, "non-RFID spool")
    _expect(polylite.spool.slicer_filament_name == "PolyLite ABS", polylite.spool.slicer_filament_name)
    unknown = by_label["AMS 2 - T2 (R)"]
    _expect(unknown.spool.material == "PPS" and unknown.spool.slicer_filament_name == "", "unknown preset id")
    summary = unit_summary(slots)
    _expect([unit["label"] for unit in summary][:2] == ["AMS 1 (L)", "AMS 2 (R)"], summary)
    _expect([row["label"] for row in summary[-1]["slots"]] == ["Left", "Right"], summary[-1])
    _expect(summary[0]["slots"][1]["label"] == "T2", summary[0])


def _check_x1c_fixture() -> None:
    data = _load_fixture("x1c_report.json")
    printer = PrinterInfo(id=9, name="Office X1C", model="X1C")
    slots = parse_slots(printer, data["print"])
    labels = [slot.slot_label for slot in slots]
    _expect(labels == ["AMS 1 - T1", "AMS 1 - T2", "AMS 1 - T3", "AMS 1 - T4", "External"], labels)
    _expect(not slots[0].loaded, "empty tray")
    _expect(all(slot.extruder is None for slot in slots), "single nozzle has no extruder")
    external = slots[-1]
    _expect(external.loaded and external.spool.brand == "Generic", external.spool.brand)
    _expect(external.spool.material_label == "TPU", external.spool.material_label)
    _expect(model_for("00M00A000000001", product_name(data["info"])) == "X1C", "X1C model")


def _check_settings_round_trip() -> None:
    printer = LanPrinter(
        id=printer_id_for_serial("00M00A000000001"), name="Office", model="X1C",
        ip="192.0.2.10", serial="00M00A000000001", fingerprint="AB:CD", access_code="12345678",
    )
    restored = printers_from_settings({SETTINGS_KEY: [printer.to_settings(), {"ip": "x"}, "bad"]})
    _expect(len(restored) == 1 and restored[0].fingerprint == "abcd", restored)
    public = restored[0].public()
    _expect("access_code" not in public and "serial" not in public, public)
    _expect(public["serial_tail"] == "0001" and public["certificate"] == "pinned", public)
    _expect(printer_id_for_serial("a") == printer_id_for_serial("a") != printer_id_for_serial("b"), "ids")


def _check_source_with_fake_reads() -> None:
    """fetch_context keeps going when one printer fails."""
    good = LanPrinter(id=1, name="Good", model="X1C", ip="192.0.2.1", serial="S-GOOD", access_code="x")
    bad = LanPrinter(id=2, name="Gone", model="X1C", ip="192.0.2.2", serial="S-BAD", access_code="x")
    report = _load_fixture("x1c_report.json")["print"]
    with _CACHE_LOCK:
        _CACHE["S-GOOD"] = PrinterState("S-GOOD", report, {}, "bambu", "", time.time())
    global read_printer_state
    original = read_printer_state

    def fake_read(ip: str, access_code: str, expected_serial: str = "", fingerprint: str = "", report_timeout: float = 0) -> PrinterState:
        raise BambuLanError("The printer at {} did not answer.".format(ip))

    read_printer_state = fake_read  # type: ignore[assignment]
    try:
        context = BambuLanSource([good, bad]).fetch_context()
        _expect(len(context.printers) == 2 and len(context.slots) == 5, context)
        _expect(context.warnings and context.warnings[0].startswith("Gone:"), context.warnings)
        try:
            BambuLanSource([bad]).fetch_context()
        except BambuLanError:
            pass
        else:
            raise AssertionError("all printers failing must raise")
    finally:
        read_printer_state = original  # type: ignore[assignment]
        clear_cache()


def _selftest() -> int:
    _with_test_presets()
    checks = (
        ("requests are read-only", _check_requests_are_read_only),
        ("a read sends only allowed requests", _check_session_sends_only_allowed),
        ("certificate common names", _check_der_common_names),
        ("model and nozzle detection", _check_models),
        ("H2D report: AMS, AMS HT, external L/R", _check_h2d_fixture),
        ("X1C report: AMS and external", _check_x1c_fixture),
        ("settings round trip hides secrets", _check_settings_round_trip),
        ("one unreachable printer is a warning", _check_source_with_fake_reads),
    )
    failures = 0
    for label, check in checks:
        try:
            check()
            print("PASS:", label)
        except Exception:
            failures += 1
            print("FAIL:", label)
            traceback.print_exc()
    _reset_presets()
    if failures:
        return 1
    print("PASS: all bambu_lan self-tests completed")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
