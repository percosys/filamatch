"""Bambuddy integration for Filamatch.

Bambuddy (https://github.com/maziggy/bambuddy) is a self-hosted Bambu Lab
command center.  Its REST API exposes:

* printers,
* the live printer status (every AMS tray and external spool),
* AMS slot assignments (printer / AMS / tray -> inventory spool),
* the inventory (spools, materials, colours, remaining weight),
* the filament preset selected for each spool.

This module is the read-only foundation for the OrcaSlicer/Bambuddy sync:
it turns those API payloads into small, dependency-free dataclasses that the
palette, the Fusion material importer and the slicer project generator can use.

The API is reachable with an API key in the ``X-API-Key`` header.  Bambuddy is
commonly published behind a reverse proxy with single sign-on (for example
Authelia); the proxy rejects API-key requests with an HTML login redirect.  In
that case use the direct LAN URL of the Bambuddy backend (normally
``http://<host>:8000``) and this module raises an actionable
:class:`BambuddyProxyError` instead of a generic JSON failure.

This module imports cleanly outside Fusion 360 (standard library only) and
includes a fake-transport self-test.
"""

from __future__ import annotations

import json
import logging
import ssl
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import config

from .printer_model import (  # noqa: F401 - re-exported for older callers
    NozzleInfo,
    PrinterContext,
    PrinterInfo,
    SlotInfo,
    SpoolInfo,
    default_density,
    default_template_material,
    material_family,
    normalize_color_hex,
    nozzle_flow,
)

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: Default request timeout (seconds).  Fusion runs handlers on its UI thread,
#: so an offline server must fail fast.
DEFAULT_TIMEOUT = 6.0

#: API prefix used by Bambuddy.
API_PREFIX = "/api/v1"

#: Bambuddy snapshots use the source-neutral context type.
BambuddyContext = PrinterContext

#: Source id stored on contexts fetched from Bambuddy.
SOURCE_ID = "bambuddy"


class BambuddyError(Exception):
    """Base class for all Bambuddy integration failures."""


class BambuddyConfigurationError(BambuddyError):
    """The Bambuddy URL or API key is missing/invalid."""


class BambuddyAuthError(BambuddyError):
    """Bambuddy rejected the API key."""


class BambuddyProxyError(BambuddyError):
    """Bambuddy is behind an HTML SSO proxy that rejects API-key requests."""


class BambuddyConnectionError(BambuddyError):
    """Bambuddy could not be reached."""


class BambuddyResponseError(BambuddyError):
    """Bambuddy returned an unexpected response."""


@dataclass(frozen=True)
class BambuddyConfig:
    """Connection details for one Bambuddy server."""

    base_url: str
    api_key: str
    timeout: float = DEFAULT_TIMEOUT
    #: ``False`` accepts a self-signed certificate (LAN Bambuddy over https).
    verify_tls: bool = True

    def normalized_base_url(self) -> str:
        """Return the base URL without trailing slash, or raise."""
        text = str(self.base_url or "").strip()
        if not text:
            raise BambuddyConfigurationError(
                "Bambuddy URL is empty. Enter the Bambuddy address, e.g. "
                "http://192.168.1.20:8000, and try again."
            )
        parsed = urllib.parse.urlparse(text)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise BambuddyConfigurationError(
                "Bambuddy URL must start with http:// or https:// and include a host, "
                "got {!r}.".format(_redact_url(text))
            )
        return text.rstrip("/")

    def normalized_api_key(self) -> str:
        """Return the stripped API key, or raise."""
        key = str(self.api_key or "").strip()
        if not key:
            raise BambuddyConfigurationError(
                "Bambuddy API key is empty. Create an API key in Bambuddy "
                "(Settings -> API Keys) and paste it here."
            )
        return key


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def parse_spool(raw: Mapping[str, Any]) -> SpoolInfo:
    """Parse one Bambuddy spool payload into :class:`SpoolInfo`."""
    label_weight = _safe_float(raw.get("label_weight"))
    weight_used = _safe_float(raw.get("weight_used"), 0.0)
    remaining: Optional[float] = None
    if label_weight is not None and weight_used is not None:
        remaining = max(0.0, label_weight - weight_used)
    material = _text(raw.get("material"), "Unknown")
    subtype = _text(raw.get("subtype"))
    color_name = _text(raw.get("color_name"))
    if not color_name:
        color_name = _text(raw.get("color"), "")
    color_hex = normalize_color_hex(raw.get("rgba") or raw.get("color_hex"))
    return SpoolInfo(
        id=_safe_int(raw.get("id")),
        material=material,
        brand=_text(raw.get("brand"), "Unknown"),
        color_name=color_name,
        color_hex=color_hex,
        remaining_g=remaining,
        label_weight_g=label_weight,
        weight_used_g=weight_used,
        slicer_filament=_text(raw.get("slicer_filament")),
        slicer_filament_name=_text(raw.get("slicer_filament_name")),
        subtype=subtype,
        location=_text(raw.get("location") or raw.get("storage_location")),
        archived=bool(raw.get("archived_at")),
    )


def parse_printer(raw: Mapping[str, Any]) -> PrinterInfo:
    """Parse one Bambuddy printer payload."""
    return PrinterInfo(
        id=_safe_int(raw.get("id")),
        name=_text(raw.get("name"), "Printer {}".format(_safe_int(raw.get("id")))),
        model=_text(raw.get("model")),
        serial_number=_text(raw.get("serial_number")),
        ip_address=_text(raw.get("ip_address")),
        location=_text(raw.get("location")),
        is_active=bool(raw.get("is_active", True)),
        brand="Bambu Lab",
    )


def parse_assignment(
    raw: Mapping[str, Any],
    spools_by_id: Mapping[int, SpoolInfo],
    printer_names: Mapping[int, str],
) -> SlotInfo:
    """Parse one Bambuddy ``/inventory/assignments`` entry."""
    printer_id = _safe_int(raw.get("printer_id"))
    ams_id = _safe_int(raw.get("ams_id"))
    tray_id = _safe_int(raw.get("tray_id"))
    raw_spool = raw.get("spool")
    spool: Optional[SpoolInfo] = None
    if isinstance(raw_spool, Mapping):
        spool = parse_spool(raw_spool)
    else:
        spool = spools_by_id.get(_safe_int(raw.get("spool_id")))
    filament_type = _text(raw.get("fingerprint_type"))
    if not filament_type and spool is not None:
        filament_type = spool.material
    color_hex = normalize_color_hex(
        raw.get("fingerprint_color") or (spool.color_hex if spool else None)
    )
    return SlotInfo(
        printer_id=printer_id,
        printer_name=_text(
            raw.get("printer_name") or printer_names.get(printer_id), "Printer {}".format(printer_id)
        ),
        ams_id=ams_id,
        tray_id=tray_id,
        spool=spool,
        filament_type=filament_type,
        color_hex=color_hex,
        loaded=spool is not None,
    )


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

#: A transport receives (method, url, headers, timeout) and returns
#: (status, body_bytes, content_type, final_url).
Transport = Callable[
    [str, str, Mapping[str, str], float],
    Tuple[int, bytes, str, str],
]


def _redact_url(url: str) -> str:
    """Return *url* without a ``user:password@`` part, for error messages."""
    try:
        parsed = urllib.parse.urlsplit(str(url))
    except ValueError:
        return "<invalid URL>"
    if "@" not in parsed.netloc:
        return str(url)
    host = parsed.netloc.rsplit("@", 1)[1]
    return urllib.parse.urlunsplit(parsed._replace(netloc=host))


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Return 3xx responses to the caller instead of following them.

    urllib would otherwise resend the ``X-API-Key`` header to the redirect
    target, even another host or plain http.  A redirect is reported as the
    SSO/proxy error by :meth:`BambuddyClient._get_json`.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _ssl_context(verify_tls: bool) -> ssl.SSLContext:
    """Return the TLS context: verified by default, unverified on request."""
    if verify_tls:
        return ssl.create_default_context()
    logger.warning(
        "TLS certificate checks are off for Bambuddy (verify_tls=False); "
        "use this only for a trusted LAN server with a self-signed certificate."
    )
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _build_opener(verify_tls: bool = True) -> urllib.request.OpenerDirector:
    """Return an opener that never follows redirects."""
    return urllib.request.build_opener(
        _NoRedirectHandler(),
        urllib.request.HTTPSHandler(context=_ssl_context(verify_tls)),
    )


def make_urllib_transport(verify_tls: bool = True) -> Transport:
    """Return the stdlib transport; ``verify_tls=False`` accepts self-signed certs."""
    opener = _build_opener(verify_tls)

    def transport(
        method: str, url: str, headers: Mapping[str, str], timeout: float
    ) -> Tuple[int, bytes, str, str]:
        return _urllib_request(opener, method, url, headers, timeout)

    return transport


def _urllib_request(
    opener: urllib.request.OpenerDirector,
    method: str,
    url: str,
    headers: Mapping[str, str],
    timeout: float,
) -> Tuple[int, bytes, str, str]:
    """Send one request through *opener* and return the transport tuple."""
    request = urllib.request.Request(url=url, method=method, headers=dict(headers))
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
            return (
                int(getattr(response, "status", 200) or 200),
                body,
                content_type,
                response.geturl(),
            )
    except urllib.error.HTTPError as exc:
        # Also reached for 3xx: the no-redirect handler leaves them unhandled.
        body = b""
        try:
            body = exc.read()
        except Exception:
            pass
        content_type = ""
        try:
            content_type = exc.headers.get("Content-Type", "")
        except Exception:
            pass
        return int(exc.code), body, content_type, str(exc.geturl() or url)
    except urllib.error.URLError as exc:
        raise BambuddyConnectionError(
            "Could not reach Bambuddy at {!r}: {}. Check the URL and that the "
            "Bambuddy server is running.".format(_redact_url(url), exc.reason)
        ) from exc
    except OSError as exc:
        raise BambuddyConnectionError(
            "Could not reach Bambuddy at {!r}: {}".format(_redact_url(url), exc)
        ) from exc


#: Default stdlib transport (TLS verified, redirects not followed).
_urllib_transport: Transport = make_urllib_transport(True)


class BambuddyClient:
    """Read-only Bambuddy REST client."""

    def __init__(
        self,
        config: BambuddyConfig,
        transport: Optional[Transport] = None,
    ) -> None:
        self.config = config
        if transport is None:
            transport = _urllib_transport if config.verify_tls else make_urllib_transport(False)
        self._transport = transport

    @property
    def base_url(self) -> str:
        """Normalised server base URL."""
        return self.config.normalized_base_url()

    def _headers(self) -> Dict[str, str]:
        return {
            "X-API-Key": self.config.normalized_api_key(),
            "Accept": "application/json",
            "User-Agent": config.USER_AGENT,
        }

    def _get_json(self, path: str) -> Any:
        url = self.base_url + path
        status, body, content_type, final_url = self._transport(
            "GET", url, self._headers(), float(self.config.timeout)
        )
        text = ""
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        lowered_type = (content_type or "").lower()
        if status in (301, 302, 303, 307, 308) or "text/html" in lowered_type:
            raise BambuddyProxyError(
                "Bambuddy is behind an HTML login/SSO proxy, so the API key cannot be "
                "used through {!r}. Use Bambuddy's direct backend URL instead, usually "
                "'http://<bambuddy-host>:8000' (the web UI may be published on another "
                "port behind the proxy).".format(_redact_url(self.base_url))
            )
        if status in (401, 403):
            raise BambuddyAuthError(
                "Bambuddy rejected the API key (HTTP {}). Create/check the key under "
                "Bambuddy Settings -> API Keys and make sure it allows inventory and "
                "printer reads.".format(status)
            )
        if status >= 400:
            detail = text.strip()
            if detail:
                try:
                    payload = json.loads(text)
                    detail = str(payload.get("detail") or payload.get("message") or detail)
                except Exception:
                    detail = detail[:300]
            raise BambuddyResponseError(
                "Bambuddy {!r} returned HTTP {}{}.".format(
                    path, status, ": " + detail if detail else ""
                )
            )
        try:
            return json.loads(text) if text else None
        except json.JSONDecodeError as exc:
            if "html" in lowered_type or "login" in text.lower():
                raise BambuddyProxyError(
                    "Bambuddy returned an HTML login page instead of JSON. Use the "
                    "direct backend URL (usually 'http://<host>:8000') instead of the "
                    "SSO-protected public URL."
                ) from exc
            raise BambuddyResponseError(
                "Bambuddy returned invalid JSON for {!r}: {}".format(path, exc)
            ) from exc

    def test_connection(self) -> Dict[str, Any]:
        """Validate the URL/key and return basic server identity."""
        me = self._get_json(API_PREFIX + "/auth/me")
        return {
            "ok": True,
            "base_url": _redact_url(self.base_url),
            "username": _text((me or {}).get("username")),
            "is_admin": bool((me or {}).get("is_admin")),
        }

    def get_printers(self) -> List[PrinterInfo]:
        """Return all configured printers."""
        data = self._get_json(API_PREFIX + "/printers/")
        if not isinstance(data, list):
            raise BambuddyResponseError("Bambuddy printers response is not a list.")
        return [parse_printer(item) for item in data if isinstance(item, Mapping)]

    def get_spools(self, include_archived: bool = False) -> List[SpoolInfo]:
        """Return inventory spools."""
        path = API_PREFIX + "/inventory/spools"
        if include_archived:
            path += "?include_archived=true"
        data = self._get_json(path)
        if not isinstance(data, list):
            raise BambuddyResponseError("Bambuddy spools response is not a list.")
        spools = [parse_spool(item) for item in data if isinstance(item, Mapping)]
        return [spool for spool in spools if include_archived or not spool.archived]

    def get_assignments(self) -> List[Dict[str, Any]]:
        """Return raw AMS slot assignments."""
        data = self._get_json(API_PREFIX + "/inventory/assignments")
        if not isinstance(data, list):
            raise BambuddyResponseError("Bambuddy assignments response is not a list.")
        return [item for item in data if isinstance(item, Mapping)]

    def get_printer_status(self, printer_id: int) -> Dict[str, Any]:
        """Return the live status of one printer (AMS units, external spools)."""
        data = self._get_json("{}/printers/{}/status".format(API_PREFIX, int(printer_id)))
        if not isinstance(data, Mapping):
            raise BambuddyResponseError("Bambuddy printer status response is not an object.")
        return dict(data)

    def fetch_context(self) -> BambuddyContext:
        """Fetch printers, inventory, spool links and the live slots.

        Every loaded slot comes from the printer's live status, so slots with
        no inventory spool linked are listed too.  A slot with a linked spool
        keeps that spool (grams left, preset).  When the live status of a
        printer cannot be read, its linked spools are still listed and the
        context carries a warning.

        Printers, spools and links are read first; their first failure (for
        example :class:`BambuddyConnectionError` for an offline server) stops
        the fetch, so the UI waits at most one timeout.
        """
        printers = self.get_printers()
        spools = self.get_spools()
        assignments = self.get_assignments()
        printer_names = {printer.id: printer.name for printer in printers}
        spools_by_id = {spool.id: spool for spool in spools}
        linked = {
            slot.key: slot
            for slot in (
                parse_assignment(item, spools_by_id, printer_names) for item in assignments
            )
        }
        slots: List[SlotInfo] = []
        live_spools: Dict[int, SpoolInfo] = {}
        warnings: List[str] = []
        for index, printer in enumerate(printers):
            if not printer.is_active:
                continue
            try:
                status = self.get_printer_status(printer.id)
            except (BambuddyConnectionError, BambuddyProxyError):
                raise
            except BambuddyError as exc:
                logger.warning("No live slots for printer %s: %s", printer.id, exc)
                warnings.append("{}: only linked spools are shown ({})".format(printer.name, exc))
                continue
            if status.get("connected") is False:
                warnings.append(
                    "{}: Bambuddy is not connected to the printer; only linked spools "
                    "are shown.".format(printer.name)
                )
                continue
            # AMS auto refill, as Bambuddy reads it from the printer.
            refill = status.get("ams_filament_backup")
            if isinstance(refill, bool):
                printer = printers[index] = replace(printer, auto_refill=refill)
            nozzles = status_nozzles(status)
            if nozzles:
                printer = printers[index] = replace(printer, nozzles=nozzles)
            for slot in live_slots(printer, status):
                link = linked.pop(slot.key, None)
                if link is not None:
                    slot = merge_linked_slot(slot, link)
                elif slot.spool is not None:
                    live_spools.setdefault(slot.spool.id, slot.spool)
                slots.append(slot)
        slots.extend(linked.values())
        slots.sort(key=lambda slot: (slot.printer_id, slot.ams_id == 255, slot.ams_id, slot.tray_id))
        fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        return BambuddyContext(
            printers=tuple(printers),
            spools=tuple(spools) + tuple(live_spools.values()),
            slots=tuple(slots),
            fetched_at=fetched_at,
            source=SOURCE_ID,
            warnings=tuple(warnings),
        )


def status_nozzles(status: Mapping[str, Any]) -> Tuple[NozzleInfo, ...]:
    """The installed nozzles in a Bambuddy printer status, by extruder id.

    Bambuddy lists two nozzles for single-nozzle printers too; only a
    dual-nozzle printer (see :func:`_status_is_dual`) keeps the second.  Its
    ``nozzle_type`` is the material ("hardened_steel"), so the flow is known
    only when a type code ("HH01") is given.
    """
    raw = [item for item in status.get("nozzles") or [] if isinstance(item, Mapping)]
    nozzles: List[NozzleInfo] = []
    for index, item in enumerate(raw[: 2 if _status_is_dual(status) else 1]):
        diameter = _safe_float(item.get("nozzle_diameter"))
        if diameter and diameter > 0:
            nozzles.append(NozzleInfo(index, diameter, nozzle_flow(item.get("nozzle_type"))))
    return tuple(nozzles)


def _status_is_dual(status: Mapping[str, Any]) -> bool:
    """True for a dual-nozzle printer: a right external spool (255) or an AMS on the left nozzle."""
    from . import bambu_lan

    raw_map = status.get("ams_extruder_map")
    on_left = any(
        _safe_int(value, -1) == bambu_lan.EXTRUDER_LEFT
        for value in (raw_map.values() if isinstance(raw_map, Mapping) else ())
    )
    externals = [tray for tray in status.get("vt_tray") or [] if isinstance(tray, Mapping)]
    return on_left or any(_safe_int(tray.get("id"), -1) == bambu_lan.VIRTUAL_MAIN_ID for tray in externals)


def live_slots(printer: PrinterInfo, status: Mapping[str, Any]) -> List[SlotInfo]:
    """Every AMS and external slot in a Bambuddy live printer status.

    Uses the LAN reader's slot builder, so slot numbers, labels and filament
    names are the same as with the direct connection.
    """
    from . import bambu_lan

    extruders: Dict[int, int] = {}
    raw_map = status.get("ams_extruder_map")
    for key, value in (raw_map.items() if isinstance(raw_map, Mapping) else ()):
        unit_id, extruder = _safe_int(key, -1), _safe_int(value, -1)
        if unit_id >= 0 and extruder in (bambu_lan.EXTRUDER_RIGHT, bambu_lan.EXTRUDER_LEFT):
            extruders[unit_id] = extruder
    units = [unit for unit in status.get("ams") or [] if isinstance(unit, Mapping)]
    externals = [tray for tray in status.get("vt_tray") or [] if isinstance(tray, Mapping)]
    return bambu_lan.build_slots(printer, units, externals, extruders, _status_is_dual(status))


def merge_linked_slot(live: SlotInfo, link: SlotInfo) -> SlotInfo:
    """A live slot with the inventory spool Bambuddy links to it.

    The inventory spool wins (it knows the grams left and the preset); the
    live slot keeps its label, nozzle and printer filament id.
    """
    return replace(
        live,
        spool=link.spool,
        filament_type=link.filament_type or live.filament_type,
        color_hex=link.color_hex if link.spool is not None else live.color_hex,
        loaded=link.loaded or live.loaded,
    )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Test transport with canned payloads, recording requests."""

    def __init__(self, responses: Mapping[str, Tuple[int, bytes, str, str]]) -> None:
        self.responses = dict(responses)
        self.requests: List[Tuple[str, str, Mapping[str, str]]] = []

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], timeout: float
    ) -> Tuple[int, bytes, str, str]:
        self.requests.append((method, url, headers))
        path = urllib.parse.urlparse(url).path
        query = urllib.parse.urlparse(url).query
        key = path + (("?" + query) if query else "")
        if key in self.responses:
            return self.responses[key]
        for candidate in (path, path.rstrip("/")):
            if candidate in self.responses:
                return self.responses[candidate]
        return 404, b'{"detail":"Not found"}', "application/json", url


def _json_response(payload: Any) -> Tuple[int, bytes, str, str]:
    return 200, json.dumps(payload).encode("utf-8"), "application/json", ""


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _check_config() -> None:
    """Configuration validation and secret-safe errors."""
    config = BambuddyConfig("http://192.168.1.20:8000/", "bb_secret")
    _expect(config.normalized_base_url() == "http://192.168.1.20:8000", "base URL must be normalised")
    try:
        BambuddyConfig("", "bb_secret").normalized_base_url()
    except BambuddyConfigurationError as exc:
        _expect("URL is empty" in str(exc), "empty URL error must be actionable")
    else:
        raise AssertionError("empty Bambuddy URL must raise")
    try:
        BambuddyConfig("http://x:8000", "").normalized_api_key()
    except BambuddyConfigurationError as exc:
        _expect("API key" in str(exc), "missing key error must mention the API key")
    else:
        raise AssertionError("empty API key must raise")


def _check_parsing() -> None:
    """Spool/slot parsing and colour/remaining normalisation."""
    spool = parse_spool(
        {
            "id": 7,
            "material": "PLA",
            "subtype": "Matte",
            "brand": "Bambu Lab",
            "color_name": "Charcoal",
            "rgba": "000000FF",
            "label_weight": 1000,
            "weight_used": 250,
            "slicer_filament": "GFA01",
            "slicer_filament_name": "Bambu PLA Matte",
        }
    )
    _expect(spool.color_hex == "#000000", "RGBA must normalise to RGB hex")
    _expect(spool.remaining_g == 750.0, "remaining weight must be label-used")
    _expect(spool.remaining_percent == 75.0, "remaining percentage mismatch")
    _expect(spool.display_name == "Bambu Lab - PLA Matte - Charcoal", "spool display name mismatch")
    _expect(default_template_material("ABS-GF") == "ABS Plastic", "ABS-GF template mismatch")
    _expect(default_density("PETG") == 1.27, "PETG density fallback mismatch")
    printer = parse_printer({"id": 2, "name": "Studio P1S", "model": "P1S"})
    _expect(printer.label == "Studio P1S - P1S", "printer label mismatch")
    slot = parse_assignment(
        {
            "printer_id": 2,
            "printer_name": "Studio P1S",
            "ams_id": 0,
            "tray_id": 1,
            "fingerprint_color": "F48438FF",
            "fingerprint_type": "ABS-GF",
            "spool": {
                "id": 8,
                "material": "ABS-GF",
                "brand": "Bambu Lab",
                "rgba": "F48438FF",
            },
        },
        {},
        {},
    )
    _expect(slot.key == "2:0:1", "slot key mismatch")
    _expect(slot.slot_label == "AMS 1 - T2", "slot label mismatch")
    _expect(slot.color_hex == "#F48438", "slot colour mismatch")
    _expect(slot.spool is not None and slot.spool.id == 8, "slot spool mismatch")


def _check_context_fetch() -> None:
    """The client fetches and joins printers, spools and assignments."""
    responses = {
        "/api/v1/auth/me": _json_response({"username": "api-key:bb_x", "is_admin": False}),
        "/api/v1/printers/": _json_response(
            [
                {"id": 1, "name": "Workshop H2D", "model": "H2D", "serial_number": "S1"},
                {"id": 2, "name": "Studio P1S", "model": "P1S", "serial_number": "S2"},
            ]
        ),
        "/api/v1/inventory/spools": _json_response(
            [
                {
                    "id": 8,
                    "material": "ABS-GF",
                    "brand": "Bambu Lab",
                    "rgba": "F48438FF",
                    "label_weight": 1000,
                    "weight_used": 520,
                    "slicer_filament": "GFB50",
                    "slicer_filament_name": "Bambu ABS-GF",
                }
            ]
        ),
        "/api/v1/inventory/assignments": _json_response(
            [
                {
                    "printer_id": 2,
                    "printer_name": "Studio P1S",
                    "ams_id": 0,
                    "tray_id": 1,
                    "fingerprint_color": "F48438FF",
                    "fingerprint_type": "ABS-GF",
                    "spool": {
                        "id": 8,
                        "material": "ABS-GF",
                        "brand": "Bambu Lab",
                        "rgba": "F48438FF",
                        "label_weight": 1000,
                        "weight_used": 520,
                        "slicer_filament": "GFB50",
                        "slicer_filament_name": "Bambu ABS-GF",
                    },
                }
            ]
        ),
    }
    for printer_id in (1, 2):  # printers Bambuddy is not connected to: links only
        responses["/api/v1/printers/{}/status".format(printer_id)] = _json_response({"connected": False})
    transport = _FakeTransport(responses)
    client = BambuddyClient(BambuddyConfig("http://192.168.1.20:8000", "bb_test"), transport)
    identity = client.test_connection()
    _expect(identity["ok"] is True, "connection test must succeed")
    context = client.fetch_context()
    _expect(len(context.printers) == 2, "both printers must be parsed")
    _expect(len(context.spools) == 1, "inventory spool must be parsed")
    _expect(len(context.slots) == 1, "one assignment must be parsed")
    slot = context.slots[0]
    _expect(slot.spool is not None and slot.spool.id == 8, "assignment spool join mismatch")
    _expect(context.slots_for_printer(2)[0].key == "2:0:1", "slots_for_printer mismatch")
    _expect(context.slot_by_key("2:0:1") is slot, "slot_by_key mismatch")
    _expect(context.fetched_at.endswith("+00:00"), "fetched_at must be UTC ISO")
    _expect(
        all(request[2].get("X-API-Key") == "bb_test" for request in transport.requests),
        "every request must carry the API key header",
    )


def _check_status_nozzles() -> None:
    """Nozzle sizes from a Bambuddy status: two for an H2D, one for a single-nozzle printer."""
    from pathlib import Path

    fixtures = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
    status = json.loads((fixtures / "bambuddy" / "h2d_status.json").read_text(encoding="utf-8"))
    _expect(status_nozzles(status) == (NozzleInfo(0, 0.4, ""), NozzleInfo(1, 0.4, "")),
            "H2D: two 0.4 nozzles, flow unknown: {}".format(status_nozzles(status)))
    single = {"nozzles": [{"nozzle_type": "HS01", "nozzle_diameter": "0.6"}, {"nozzle_diameter": "0.4"}],
              "ams_extruder_map": {"0": 0}, "vt_tray": [{"id": 254}]}
    _expect(status_nozzles(single) == (NozzleInfo(0, 0.6, "Standard"),), "one nozzle, a type code gives the flow")


def _check_live_slots() -> None:
    """Live status lists every loaded slot; linked spools win; a lost printer warns."""
    from pathlib import Path

    from . import bambu_lan

    fixtures = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
    status = json.loads((fixtures / "bambuddy" / "h2d_status.json").read_text(encoding="utf-8"))
    spool = {
        "id": 8, "material": "ABS-GF", "brand": "Bambu Lab", "rgba": "C6C6C6FF",
        "label_weight": 1000, "weight_used": 380,
        "slicer_filament": "GFB50", "slicer_filament_name": "Bambu ABS-GF",
    }
    responses = {
        "/api/v1/printers/": _json_response(
            [
                {"id": 1, "name": "Workshop H2D", "model": "H2D"},
                {"id": 2, "name": "Studio P1S", "model": "P1S"},
            ]
        ),
        "/api/v1/inventory/spools": _json_response([spool]),
        "/api/v1/inventory/assignments": _json_response(
            [
                {"printer_id": 1, "ams_id": 0, "tray_id": 0, "fingerprint_type": "ABS-GF",
                 "fingerprint_color": "C6C6C6FF", "spool": spool},
                {"printer_id": 2, "ams_id": 0, "tray_id": 2, "spool": dict(spool, id=9)},
            ]
        ),
        "/api/v1/printers/1/status": _json_response(dict(status, ams_filament_backup=True)),
        "/api/v1/printers/2/status": _json_response({"id": 2, "connected": False}),
    }
    bambu_lan._reset_presets([fixtures / "bambu_lan" / "presets"])
    try:
        client = BambuddyClient(BambuddyConfig("http://192.168.1.20:8000", "bb_test"), _FakeTransport(responses))
        context = client.fetch_context()
    finally:
        bambu_lan._reset_presets()
    h2d = context.slots_for_printer(1)
    _expect(len(h2d) == 11 and sum(1 for slot in h2d if slot.loaded) == 9, [s.slot_label for s in h2d])
    first = context.slot_by_key("1:0:0")
    _expect(first.spool is not None and first.spool.id == 8, "the linked inventory spool wins")
    _expect(first.slot_label == "AMS 1 - T1 (L)" and first.extruder == bambu_lan.EXTRUDER_LEFT, first)
    unlinked = context.slot_by_key("1:1:0")
    _expect(unlinked.loaded and unlinked.spool is not None and unlinked.slot_label == "AMS 2 - T1 (R)", unlinked)
    _expect(unlinked.spool.slicer_filament == "GFB60", "an unlinked slot gets its filament from the printer")
    _expect(context.slot_by_key("1:255:1").filament_type == "TPU", "right external spool is tray 1")
    _expect(not context.slot_by_key("1:255:0").loaded, "empty left external spool")
    _expect(not context.slot_by_key("1:128:0").loaded, "empty AMS HT")
    _expect(
        [printer.auto_refill for printer in context.printers] == [True, None],
        "AMS auto refill from Bambuddy's ams_filament_backup; unknown when not connected",
    )
    single = live_slots(
        PrinterInfo(id=3, name="Office X1C", model="X1C"),
        {
            "connected": True,
            "nozzles": [{}, {}],
            "ams_extruder_map": {"0": 0},
            "ams": [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "000000FF"}]}],
            "vt_tray": [{"id": 254, "tray_type": "TPU", "tray_color": "161616FF"}],
        },
    )
    _expect([slot.slot_label for slot in single] == ["AMS 1 - T1", "External"], [s.slot_label for s in single])
    p1s = context.slots_for_printer(2)
    _expect([slot.key for slot in p1s] == ["2:0:2"], "an offline printer still shows its linked spools")
    _expect(len(context.warnings) == 1 and context.warnings[0].startswith("Studio P1S:"), context.warnings)
    _expect(any(spool.id == unlinked.spool.id for spool in context.spools), "live spools are in the context")


def _check_auth_proxy_errors() -> None:
    """SSO HTML and rejected keys produce actionable, secret-safe errors."""
    html_transport = _FakeTransport(
        {
            "/api/v1/auth/me": (
                200,
                b"<!doctype html><html><body>Login</body></html>",
                "text/html; charset=utf-8",
                "https://auth.example.com/",
            )
        }
    )
    client = BambuddyClient(
        BambuddyConfig("https://bambuddy.example.com", "bb_TOP_SECRET"), html_transport
    )
    try:
        client.test_connection()
    except BambuddyProxyError as exc:
        _expect("direct backend URL" in str(exc), "proxy error must recommend the LAN URL")
        _expect("bb_TOP_SECRET" not in str(exc), "proxy error must not leak the API key")
    else:
        raise AssertionError("HTML login response must raise BambuddyProxyError")

    auth_transport = _FakeTransport(
        {"/api/v1/auth/me": (401, b'{"detail":"Invalid API key"}', "application/json", "")}
    )
    client = BambuddyClient(
        BambuddyConfig("http://192.168.1.20:8000", "bb_TOP_SECRET"), auth_transport
    )
    try:
        client.test_connection()
    except BambuddyAuthError as exc:
        _expect("HTTP 401" in str(exc), "auth error must report the status")
        _expect("bb_TOP_SECRET" not in str(exc), "auth error must not leak the API key")
    else:
        raise AssertionError("401 must raise BambuddyAuthError")


def _check_connection_error() -> None:
    """Unreachable servers fail fast with one BambuddyConnectionError."""
    calls: List[float] = []

    def failing(method: str, url: str, headers: Mapping[str, str], timeout: float):
        calls.append(timeout)
        raise BambuddyConnectionError("simulated unreachable server")

    client = BambuddyClient(BambuddyConfig("http://192.168.1.20:8000", "bb_test"), failing)
    try:
        client.fetch_context()
    except BambuddyConnectionError:
        pass
    else:
        raise AssertionError("connection failure must raise BambuddyConnectionError")
    _expect(len(calls) == 1, "the first connection error must stop the fetch")
    _expect(calls[0] <= 6.0, "requests must use a short timeout")


def _check_url_redaction() -> None:
    """Error messages never show a user:password@ part of the URL."""
    _expect(
        _redact_url("http://user:pw@192.168.1.20:8000/api") == "http://192.168.1.20:8000/api",
        "credentials must be stripped",
    )
    _expect(_redact_url("http://192.168.1.20:8000") == "http://192.168.1.20:8000", "plain URL kept")
    transport = make_urllib_transport(True)
    try:
        # Port 9 on loopback is normally closed: the request fails at once.
        transport("GET", "http://user:s3cret@127.0.0.1:9/api", {}, 2.0)
    except BambuddyConnectionError as exc:
        _expect("s3cret" not in str(exc), "connection error must not leak URL credentials")
    else:
        raise AssertionError("closed port must raise BambuddyConnectionError")


def _check_tls_setting() -> None:
    """verify_tls selects a verified or an unverified TLS context."""
    _expect(_ssl_context(True).verify_mode == ssl.CERT_REQUIRED, "TLS is verified by default")
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        unverified = _ssl_context(False)
        client = BambuddyClient(BambuddyConfig("https://192.168.1.20:8000", "k", verify_tls=False))
    finally:
        logger.setLevel(previous)
    _expect(unverified.verify_mode == ssl.CERT_NONE, "verify_tls=False accepts self-signed certs")
    _expect(not unverified.check_hostname, "verify_tls=False skips the host name check")
    _expect(client._transport is not _urllib_transport, "verify_tls=False uses its own transport")
    default_client = BambuddyClient(BambuddyConfig("https://192.168.1.20:8000", "k"))
    _expect(default_client._transport is _urllib_transport, "default transport verifies TLS")


def _check_redirect_not_followed() -> None:
    """A 3xx is returned to the client and the API key never reaches the target."""
    import http.server
    import threading

    seen: List[Mapping[str, str]] = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            seen.append(dict(self.headers))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):  # silence the test server
            pass

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)
    target_url = "http://127.0.0.1:{}/login".format(target.server_address[1])

    class Redirect(Target):
        def do_GET(self):  # noqa: N802 - http.server API
            self.send_response(302)
            self.send_header("Location", target_url)
            self.end_headers()

    origin = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True) for server in (target, origin)
    ]
    for thread in threads:
        thread.start()
    try:
        base = "http://127.0.0.1:{}".format(origin.server_address[1])
        client = BambuddyClient(BambuddyConfig(base, "bb_TOP_SECRET", timeout=3.0))
        try:
            client.test_connection()
        except BambuddyProxyError as exc:
            _expect("bb_TOP_SECRET" not in str(exc), "redirect error must not leak the key")
        else:
            raise AssertionError("a redirect must raise BambuddyProxyError")
        _expect(not seen, "the redirect target must never be requested")
    finally:
        for server in (target, origin):
            server.shutdown()
            server.server_close()


def _selftest() -> int:
    """Run the Bambuddy integration self-test."""
    scenarios = (
        ("configuration validation is actionable and secret-safe", _check_config),
        ("spool/printer/slot payloads normalise correctly", _check_parsing),
        ("printers, spools and assignments are fetched and joined", _check_context_fetch),
        ("live slots list every loaded tray; linked spools win", _check_live_slots),
        ("installed nozzles from the printer status", _check_status_nozzles),
        ("SSO and rejected-key responses raise actionable errors", _check_auth_proxy_errors),
        ("unreachable Bambuddy raises a connection error", _check_connection_error),
        ("error messages strip user:password from URLs", _check_url_redaction),
        ("verify_tls selects the TLS context", _check_tls_setting),
        ("redirects are not followed and never forward the API key", _check_redirect_not_followed),
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
        print("{} Bambuddy self-test(s) FAILED".format(failures))
        return 1
    print("PASS: all Bambuddy self-tests completed")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
