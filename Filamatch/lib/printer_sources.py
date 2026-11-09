"""Printer slot sources for Filamatch.

A *slot source* reads printers and their loaded filament slots and returns a
:class:`printer_model.PrinterContext`.  Bambuddy is the primary (default)
source; Bambu printers read directly over the LAN (``bambu_lan``) are the
second, and a printer the user describes by hand (``manual_printer``) the
third.  More connectors register here later (see
``docs/design/printer-connections.md``); the palette, the export slot
manifest and the Fusion spool import only talk to this module.

The palette setting ``printer_source`` selects the source.  An unknown or
empty value falls back to Bambuddy, so settings written by older add-in
versions keep working.

Every source is read-only: it never sends a command that moves, heats or
prints.

This module imports cleanly outside Fusion 360 (standard library only) and
includes a fake-transport self-test.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

from . import bambu_lan
from . import bambuddy
from . import manual_printer
from .printer_model import PrinterContext, PrinterInfo

try:  # Python 3.8+
    from typing import Protocol
except ImportError:  # pragma: no cover - Fusion ships Python 3.8 or newer.
    Protocol = object  # type: ignore[assignment,misc]

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: Settings key holding the selected source id.
SETTINGS_KEY = "printer_source"

#: Source id of the Bambuddy server (the default).
SOURCE_BAMBUDDY = bambuddy.SOURCE_ID

#: Source used when the setting is empty or unknown.
DEFAULT_SOURCE = SOURCE_BAMBUDDY


class SlotSource(Protocol):
    """What every printer slot source provides."""

    source_id: str
    label: str

    def test_connection(self) -> Dict[str, Any]:
        """Check the connection and return a small identity payload."""
        ...

    def list_printers(self) -> List[PrinterInfo]:
        """Return the printers this source can read."""
        ...

    def fetch_context(self) -> PrinterContext:
        """Return printers, spools and loaded slots."""
        ...


class BambuddySource:
    """Slot source backed by a Bambuddy server (spools, AMS assignments)."""

    source_id = SOURCE_BAMBUDDY
    label = "Bambuddy"

    def __init__(
        self,
        config: bambuddy.BambuddyConfig,
        transport: Optional[bambuddy.Transport] = None,
    ) -> None:
        self.config = config
        self._transport = transport

    @classmethod
    def from_settings(
        cls,
        settings: Mapping[str, Any],
        transport: Optional[bambuddy.Transport] = None,
    ) -> "BambuddySource":
        """Build the source from the palette settings.

        ``bambuddy_verify_tls`` (default ``True``) set to ``False`` accepts a
        self-signed certificate on a LAN Bambuddy served over https.
        """
        verify = settings.get("bambuddy_verify_tls", True)
        if isinstance(verify, str):
            verify = verify.strip().lower() not in ("0", "false", "no", "off")
        return cls(
            bambuddy.BambuddyConfig(
                base_url=str(settings.get("bambuddy_url") or ""),
                api_key=str(settings.get("bambuddy_api_key") or ""),
                verify_tls=bool(verify),
            ),
            transport=transport,
        )

    @staticmethod
    def is_configured(settings: Mapping[str, Any]) -> bool:
        """Return whether the settings name a Bambuddy server."""
        return bool(str(settings.get("bambuddy_url") or "").strip())

    def _client(self) -> bambuddy.BambuddyClient:
        return bambuddy.BambuddyClient(self.config, transport=self._transport)

    def test_connection(self) -> Dict[str, Any]:
        """Validate the Bambuddy URL and API key."""
        return self._client().test_connection()

    def list_printers(self) -> List[PrinterInfo]:
        """Return the printers configured in Bambuddy."""
        return self._client().get_printers()

    def fetch_context(self) -> PrinterContext:
        """Fetch printers, inventory and live AMS assignments."""
        return self._client().fetch_context()


@dataclass(frozen=True)
class SourceInfo:
    """Registry entry for one slot source."""

    source_id: str
    label: str
    factory: Callable[[Mapping[str, Any]], SlotSource]
    is_configured: Callable[[Mapping[str, Any]], bool]


_REGISTRY: Dict[str, SourceInfo] = {}


def register_source(info: SourceInfo) -> None:
    """Add or replace one slot source in the registry."""
    _REGISTRY[info.source_id] = info


def registered_sources() -> List[SourceInfo]:
    """Return the registered sources, default first."""
    return sorted(
        _REGISTRY.values(), key=lambda info: (info.source_id != DEFAULT_SOURCE, info.label)
    )


def selected_source_id(settings: Mapping[str, Any]) -> str:
    """Return the source id the settings select (default when unknown)."""
    source_id = str(settings.get(SETTINGS_KEY) or "").strip().lower()
    if source_id in _REGISTRY:
        return source_id
    if source_id:
        logger.warning("Unknown printer source %r; using %r.", source_id, DEFAULT_SOURCE)
    return DEFAULT_SOURCE


def is_configured(settings: Mapping[str, Any]) -> bool:
    """Return whether the selected source has enough settings to connect."""
    return _REGISTRY[selected_source_id(settings)].is_configured(settings)


def source_from_settings(settings: Mapping[str, Any]) -> SlotSource:
    """Build the selected slot source from the palette settings."""
    return _REGISTRY[selected_source_id(settings)].factory(settings)


def fetch_context(settings: Mapping[str, Any]) -> PrinterContext:
    """Fetch printers and slots from the selected source."""
    return source_from_settings(settings).fetch_context()


register_source(
    SourceInfo(
        source_id=SOURCE_BAMBUDDY,
        label=BambuddySource.label,
        factory=BambuddySource.from_settings,
        is_configured=BambuddySource.is_configured,
    )
)
register_source(
    SourceInfo(
        source_id=bambu_lan.SOURCE_ID,
        label=bambu_lan.BambuLanSource.label,
        factory=bambu_lan.BambuLanSource.from_settings,
        is_configured=bambu_lan.BambuLanSource.is_configured,
    )
)
register_source(
    SourceInfo(
        source_id=manual_printer.SOURCE_ID,
        label=manual_printer.LABEL,
        factory=manual_printer.ManualSource.from_settings,
        is_configured=manual_printer.ManualSource.is_configured,
    )
)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _fake_bambuddy_transport() -> bambuddy.Transport:
    """Canned Bambuddy server with one printer, one spool and one assignment."""

    def response(payload: Any) -> Any:
        return 200, json.dumps(payload).encode("utf-8"), "application/json", ""

    return bambuddy._FakeTransport(
        {
            "/api/v1/auth/me": response({"username": "api-key:bb_x", "is_admin": False}),
            "/api/v1/printers/": response(
                [{"id": 1, "name": "Workshop H2D", "model": "H2D", "is_active": True}]
            ),
            "/api/v1/inventory/spools": response(
                [
                    {
                        "id": 6,
                        "material": "ABS-GF",
                        "brand": "Bambu Lab",
                        "color_name": "Black",
                        "rgba": "000000FF",
                        "label_weight": 1000,
                        "weight_used": 760,
                        "slicer_filament": "GFB50",
                        "slicer_filament_name": "Bambu ABS-GF",
                    }
                ]
            ),
            "/api/v1/inventory/assignments": response(
                [{"printer_id": 1, "ams_id": 1, "tray_id": 3, "spool_id": 6}]
            ),
        }
    )


def _check_selection() -> None:
    """Bambuddy is the default and unknown sources fall back to it."""
    _expect(selected_source_id({}) == SOURCE_BAMBUDDY, "empty setting selects Bambuddy")
    _expect(
        selected_source_id({SETTINGS_KEY: "nope"}) == SOURCE_BAMBUDDY,
        "unknown source falls back to Bambuddy",
    )
    _expect(
        selected_source_id({SETTINGS_KEY: " Bambuddy "}) == SOURCE_BAMBUDDY,
        "source ids are case and space insensitive",
    )
    _expect(registered_sources()[0].source_id == DEFAULT_SOURCE, "default source is listed first")
    _expect(not is_configured({"bambuddy_url": " "}), "blank URL is not configured")
    _expect(is_configured({"bambuddy_url": "http://192.168.1.20:8000"}), "URL configures Bambuddy")
    source = source_from_settings({"bambuddy_url": "http://192.168.1.20:8000", "bambuddy_api_key": "k"})
    _expect(isinstance(source, BambuddySource), "settings build a Bambuddy source")
    _expect(source.config.base_url == "http://192.168.1.20:8000", "URL is passed through")
    _expect(source.config.verify_tls is True, "TLS is verified by default")
    for value in (False, "false", "0"):
        unverified = source_from_settings(
            {"bambuddy_url": "https://192.168.1.20:8000", "bambuddy_verify_tls": value}
        )
        _expect(unverified.config.verify_tls is False, "verify_tls setting is passed through")


def _check_bambuddy_source() -> None:
    """The Bambuddy source returns the neutral context with its source id."""
    transport = _fake_bambuddy_transport()
    source = BambuddySource.from_settings(
        {"bambuddy_url": "http://192.168.1.20:8000", "bambuddy_api_key": "bb_secret"},
        transport=transport,
    )
    _expect(source.test_connection()["ok"] is True, "connection test passes")
    printers = source.list_printers()
    _expect([printer.name for printer in printers] == ["Workshop H2D"], "printers are listed")
    _expect(printers[0].brand == "Bambu Lab", "Bambuddy printers are Bambu Lab")
    context = source.fetch_context()
    _expect(isinstance(context, PrinterContext), "context is the neutral type")
    _expect(context.source == SOURCE_BAMBUDDY, "context names its source")
    _expect([slot.key for slot in context.slots] == ["1:1:3"], "assignment becomes a slot")
    _expect(context.slots[0].slot_label == "AMS 2 - T4", "slot label is unchanged")
    _expect(context.slots[0].spool is not None and context.slots[0].spool.id == 6, "spool joined")
    _expect(
        all(method == "GET" for method, _url, _headers in transport.requests),
        "the source only reads",
    )


def _selftest() -> int:
    """Run the printer source self-test."""
    scenarios = (
        ("Bambuddy is the default source and unknown sources fall back", _check_selection),
        ("the Bambuddy source returns the neutral context", _check_bambuddy_source),
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
        print("{} printer source self-test(s) FAILED".format(failures))
        return 1
    print("PASS: all printer source self-tests completed")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
