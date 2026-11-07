"""Shared defensive helpers for duck-typed Fusion objects and catalog entries.

Used by the material modules (importer, materials_workflow, favorites,
custom_library) so every one reads Fusion collections, names, colours and
catalog entry fields the same way.  Nothing here imports ``adsk`` at module
scope, so the helpers work with the self-test fakes in a plain interpreter.

Errors are logged, never raised, except by the colour helpers, which raise
``ValueError`` for an unusable colour; each module wraps that in its own error
type.
"""

from __future__ import annotations

import logging
import math
import re
import sys
import time
import traceback
import types
from collections.abc import Mapping as MappingABC
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")


# ---------------------------------------------------------------------------
# Fusion objects and collections
# ---------------------------------------------------------------------------


def safe_get(obj: Any, attribute: str, context: str) -> Any:
    """Read ``obj.attribute``, logging failures and returning ``None``."""
    if obj is None:
        return None
    try:
        return getattr(obj, attribute)
    except AttributeError:
        logger.debug("Object %s has no %r attribute.", context, attribute)
        return None
    except Exception:
        logger.exception("Could not read %r from %s.", attribute, context)
        return None


def display_name(obj: Any, attribute: str, context: str, fallback: str) -> str:
    """Return a stripped non-empty string attribute, or ``fallback``."""
    value = safe_get(obj, attribute, context)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def collection_items(collection: Any, label: str) -> List[Any]:
    """Enumerate a Fusion ``count``/``item(index)`` collection into a list.

    Plain iteration is the fallback for test doubles or API objects that only
    support it.  Unreadable items are logged and skipped.
    """
    if collection is None:
        return []
    items: List[Any] = []
    try:
        count = int(collection.count)
    except Exception:
        logger.exception("Could not read count of %s; falling back to iteration.", label)
        count = 0
    for index in range(max(count, 0)):
        try:
            item = collection.item(index)
        except Exception:
            logger.exception("Could not read item %d of %s.", index, label)
            continue
        if item is not None:
            items.append(item)
    if items:
        return items
    try:
        return [item for item in collection if item is not None]
    except Exception:
        logger.debug("Could not iterate %s directly.", label, exc_info=True)
        return items


def item_by_name(collection: Any, name: str, label: str) -> Any:
    """Look up ``name`` in a Fusion collection, logging API failures."""
    if collection is None:
        return None
    try:
        return collection.itemByName(name)
    except Exception:
        logger.exception("Lookup of %r failed in %s.", name, label)
        return None


def item_by_id(collection: Any, identifier: str, label: str) -> Any:
    """Look up an object by id in a Fusion collection, logging API failures."""
    if collection is None:
        return None
    try:
        return collection.itemById(identifier)
    except Exception:
        logger.exception("Lookup of id %r failed in %s.", identifier, label)
        return None


def resolve_fusion_app() -> Any:
    """Return the running Fusion ``Application``, or ``None`` outside Fusion."""
    try:
        import adsk.core
    except ImportError:
        logger.debug("Fusion API (adsk.core) is unavailable; skipping application lookup.")
        return None
    except Exception:  # pragma: no cover - depends on the Fusion runtime.
        logger.exception("Unexpected failure importing adsk.core.")
        return None
    try:  # pragma: no cover - exercised inside Fusion only.
        return adsk.core.Application.get()
    except Exception:  # pragma: no cover - depends on the Fusion runtime.
        logger.exception("adsk.core.Application.get() failed.")
        return None


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


def body_key(body: Any) -> Optional[str]:
    """Return a stable identity for a body (its ``entityToken``), or ``None``.

    Fusion returns a new wrapper object on every access, so ``id()`` cannot tell
    two bodies apart; the entity token can.  A native body in a component used
    by several occurrences keeps one token.
    """
    try:
        token = getattr(body, "entityToken", None)
    except Exception:
        logger.debug("Could not read a body entityToken.", exc_info=True)
        return None
    return token if isinstance(token, str) and token else None


def same_body(left: Any, right: Any) -> bool:
    """Return whether two body wrappers refer to the same Fusion body."""
    if left is right:
        return True
    try:
        return bool(left == right)
    except Exception:
        logger.debug("Could not compare two body objects.", exc_info=True)
        return False


class BodySet:
    """Set of bodies keyed by entity token, with an ``==`` scan for the rest.

    Membership is O(1) for bodies with a token (all real Fusion bodies); only
    token-less bodies (test doubles) fall back to comparing one by one.
    """

    def __init__(self) -> None:
        self._keys: Dict[str, Any] = {}
        self._untokened: List[Any] = []

    def add(self, body: Any) -> bool:
        """Add ``body``; return ``False`` when it was already present."""
        key = body_key(body)
        if key is not None:
            if key in self._keys:
                return False
            self._keys[key] = body
            return True
        if any(same_body(body, known) for known in self._untokened):
            return False
        self._untokened.append(body)
        return True


# ---------------------------------------------------------------------------
# Catalog entries
# ---------------------------------------------------------------------------


def entry_field(entry: Any, name: str, default: Any = None) -> Any:
    """Read a field from a catalog entry dataclass object or mapping."""
    if entry is None:
        return default
    if isinstance(entry, MappingABC):
        value = entry.get(name, default)
        return default if value is None else value
    try:
        value = getattr(entry, name)
    except AttributeError:
        return default
    except Exception:
        logger.exception("Could not read catalog entry field %r.", name)
        return default
    return default if value is None else value


def entry_category(entry: Any, default: str) -> str:
    """Return the entry category, else its slicer filament type, else ``default``."""
    for field_name in ("category", "slicer_filament_type"):
        value = entry_field(entry, field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def entry_description(entry: Any, prefix: str, default_category: str) -> str:
    """Build ``<prefix><category> | <Brand - Material - Color>`` for a material."""
    parts = [
        str(entry_field(entry, field_name, "") or "").strip()
        for field_name in ("manufacturer", "product_line", "color_name")
    ]
    descriptor = " - ".join(parts).strip(" -")
    return "{}{} | {}".format(prefix, entry_category(entry, default_category), descriptor)


# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------


class FallbackColor:
    """Minimal RGBA colour used when the Fusion API is unavailable (self-tests)."""

    def __init__(self, red: int, green: int, blue: int, opacity: int = 255) -> None:
        self.red = red
        self.green = green
        self.blue = blue
        self.opacity = opacity


def rgb_from_hex(color_hex: Any) -> Tuple[int, int, int]:
    """Parse ``#RRGGBB`` into ``(r, g, b)``; raise ``ValueError`` otherwise."""
    if not isinstance(color_hex, str):
        raise ValueError("color_hex must be a string, got {!r}".format(color_hex))
    text = color_hex.strip()
    if HEX_COLOR_PATTERN.match(text) is None:
        raise ValueError("Invalid color_hex {!r}; expected '#RRGGBB'".format(color_hex))
    return int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16)


def normalized_hex(color_hex: Any) -> str:
    """Return ``#RRGGBB`` uppercase; raise ``ValueError`` for a bad colour."""
    return "#{:02X}{:02X}{:02X}".format(*rgb_from_hex(color_hex))


def hex_from_color(color: Any) -> str:
    """Convert a Fusion ``Color`` (``red``/``green``/``blue``) to ``#RRGGBB``.

    Opacity is ignored.  Raises ``ValueError`` when a channel is missing,
    non-numeric or not finite.
    """
    if color is None:
        raise ValueError("Cannot convert a null Color to a hex string")
    channels: List[int] = []
    for channel in ("red", "green", "blue"):
        try:
            raw = getattr(color, channel)
        except Exception as exc:
            raise ValueError("Color is missing the {!r} channel: {}".format(channel, exc)) from exc
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("Color channel {!r} must be numeric, got {!r}".format(channel, raw))
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("Color channel {!r} must be finite, got {!r}".format(channel, raw))
        channels.append(int(round(max(0.0, min(255.0, value)))))
    return "#{:02X}{:02X}{:02X}".format(*channels)


def new_color(color_hex: str) -> Any:
    """Build an opaque Fusion ``Color`` (a :class:`FallbackColor` outside Fusion).

    Raises ``ValueError`` for a bad colour and ``RuntimeError`` when Fusion
    cannot create the colour.
    """
    red, green, blue = rgb_from_hex(color_hex)
    try:
        import adsk.core
    except ImportError:
        return FallbackColor(red, green, blue, 255)
    except Exception as exc:  # pragma: no cover - depends on the Fusion runtime.
        logger.exception("Unexpected failure importing the Fusion API.")
        raise RuntimeError("Could not load the Fusion API to create a Color: {}".format(exc)) from exc
    try:  # pragma: no cover - exercised inside Fusion only.
        return adsk.core.Color.create(red, green, blue, 255)
    except Exception as exc:  # pragma: no cover - depends on the Fusion runtime.
        logger.exception("adsk.core.Color.create failed.")
        raise RuntimeError("Fusion could not create a Color for {}: {}".format(color_hex, exc)) from exc


def appearance_hex(appearance: Any) -> Optional[str]:
    """Return an appearance's ``#RRGGBB`` colour, or ``None`` when unreadable."""
    try:
        return hex_from_color(safe_get(appearance, "color", "an appearance"))
    except ValueError:
        return None


def clear_color_texture(appearance: Any, context: str = "an appearance") -> None:
    """Disconnect a base-colour texture so the solid appearance colour shows."""
    try:
        if bool(safe_get(appearance, "hasTexture", context)):
            appearance.colorTexture = ""
    except Exception:
        logger.debug("Could not disconnect the color texture of %s.", context, exc_info=True)


def set_color_slots(
    appearance: Any, color: Any, context: str, slots: Iterable[str]
) -> None:
    """Best-effort write of ``color`` to the named appearance properties.

    ``Appearance.color`` only sets ``opaque_albedo``; the preview also reads
    ``surface_albedo`` and transparent schemas use ``transparent_color``.
    Missing slots are skipped; failures are logged at debug level.
    """
    properties = safe_get(appearance, "appearanceProperties", context)
    item_by_id_method = getattr(properties, "itemById", None) if properties is not None else None
    if not callable(item_by_id_method):
        return
    for prop_id in slots:
        try:
            prop = item_by_id_method(prop_id)
            if prop is not None:
                prop.value = color
        except Exception:
            logger.debug(
                "Could not set appearance property %r on %s.", prop_id, context, exc_info=True
            )


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


@contextmanager
def timed(log: logging.Logger, label: str) -> Iterator[None]:
    """Log ``label`` with its wall time in seconds (INFO) when the block ends."""
    started = time.monotonic()
    try:
        yield
    finally:
        log.info("%s took %.2f s.", label, time.monotonic() - started)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _expect(condition: bool, message: str) -> None:
    """Raise AssertionError with ``message`` when ``condition`` is false."""
    if not condition:
        raise AssertionError(message)


class _Collection:
    """Minimal ``count``/``item`` collection double."""

    def __init__(self, items: List[Any]) -> None:
        self._items = items
        self.count = len(items)

    def item(self, index: int) -> Any:
        return self._items[index]

    def itemByName(self, name: str) -> Any:
        raise RuntimeError("simulated lookup failure")


class _Body:
    """Body double that is a new wrapper per access but compares equal."""

    def __init__(self, native: Any, token: Optional[str]) -> None:
        self.native = native
        if token is not None:
            self.entityToken = token

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, _Body) and other.native is self.native

    __hash__ = None  # type: ignore[assignment]


def _selftest() -> int:
    """Exercise the helpers with small doubles; return the process exit code."""
    try:
        _expect(collection_items(_Collection([1, None, 2]), "c") == [1, 2], "collection_items")
        logging.disable(logging.CRITICAL)
        try:
            _expect(item_by_name(_Collection([]), "x", "c") is None, "item_by_name must log errors")
        finally:
            logging.disable(logging.NOTSET)
        _expect(safe_get(None, "name", "x") is None, "safe_get(None)")
        _expect(display_name(types.SimpleNamespace(name="  A "), "name", "x", "f") == "A", "name")
        _expect(entry_field({"id": None}, "id", "d") == "d", "mapping None must give default")
        _expect(
            entry_description(
                {"manufacturer": "B", "product_line": "P", "color_name": "C"}, "X | ", "PLA"
            )
            == "X | PLA | B - P - C",
            "entry_description",
        )
        _expect(normalized_hex(" #a0b1c2 ") == "#A0B1C2", "normalized_hex")
        _expect(hex_from_color(FallbackColor(1, 2, 3)) == "#010203", "hex_from_color")
        for bad in ("#12345", None, "red"):
            try:
                rgb_from_hex(bad)
            except ValueError:
                continue
            raise AssertionError("rgb_from_hex must reject {!r}".format(bad))
        native = object()
        tokens = BodySet()
        _expect(tokens.add(_Body(native, "t1")), "first tokened body is new")
        _expect(not tokens.add(_Body(native, "t1")), "same token is a duplicate")
        _expect(tokens.add(_Body(object(), "t2")), "other token is new")
        plain = BodySet()
        _expect(plain.add(_Body(native, None)), "first untokened body is new")
        _expect(not plain.add(_Body(native, None)), "equal untokened body is a duplicate")
        messages: List[str] = []
        recorder = logging.getLogger("Filamatch.fusion_helpers.selftest")
        handler = logging.Handler()
        handler.emit = lambda record: messages.append(record.getMessage())  # type: ignore[assignment]
        recorder.addHandler(handler)
        recorder.setLevel(logging.INFO)
        with timed(recorder, "Step"):
            pass
        _expect(messages and messages[0].startswith("Step took "), "timed must log seconds")
        print("PASS: fusion_helpers self-test completed")
        return 0
    except Exception:
        traceback.print_exc()
        print("FAIL: fusion_helpers self-test aborted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(_selftest())
