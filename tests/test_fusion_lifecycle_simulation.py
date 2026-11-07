"""Regression test: load and drive the add-in the way current Fusion does.

Current Autodesk Fusion releases execute a Python add-in's entry point with
``__name__ == "__main__"`` and no package context, then call the module-level
``run(context)`` / ``stop(context)`` functions.  This simulation reproduces that
process with a minimal in-memory fake of the ``adsk`` API and verifies:

1. Loading the entry point as ``__main__`` does **not** execute the standalone
   static self-test (and therefore cannot ``sys.exit`` during add-in load).
2. ``run()`` creates the command definition and a promoted button on the
   ADD-INS panel (including the renamed-panel fallback scan).
3. Clicking the button auto-executes the command, which opens the HTML palette
   and attaches the palette message handler.  The test then drives the
   ``init`` and ``run_diagnostics`` palette actions and inspects the JSON replies.
4. ``stop()`` closes the palette and removes the button control and command
   definition, and a subsequent ``run()`` works again (stop/start cycle).

Run from the repository root:

    python3 tests/test_fusion_lifecycle_simulation.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import sys
import tempfile
import types
from pathlib import Path
from typing import Any, Dict, List

# Keep lifecycle-test settings/logs out of the real Fusion palette state.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="filamatch_lifecycle_test_")
os.environ["FILAMATCH_DATA_DIR"] = _TEST_DATA_DIR

REPO_ROOT = Path(__file__).resolve().parents[1]
ADDIN_DIR = REPO_ROOT / "Filamatch"
ENTRY_POINT = ADDIN_DIR / "Filamatch.py"

_PASSED = 0
_FAILED = 0


def check(condition: bool, label: str) -> None:
    """Record and print one PASS/FAIL check."""
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print("PASS {}".format(label))
    else:
        _FAILED += 1
        print("FAIL {}".format(label))


# ---------------------------------------------------------------------------
# Minimal fake of the adsk API surface used by the add-in
# ---------------------------------------------------------------------------


class _Event:
    def __init__(self) -> None:
        self.handlers: List[Any] = []

    def add(self, handler: Any) -> bool:
        self.handlers.append(handler)
        return True

    def remove(self, handler: Any) -> bool:
        if handler in self.handlers:
            self.handlers.remove(handler)
            return True
        return False


class _FakeControl:
    def __init__(self, command_id: str) -> None:
        self.id = command_id
        self.isPromoted = False
        self.isPromotedByDefault = False
        self.deleted = False

    def deleteMe(self) -> bool:
        self.deleted = True
        return True


class _FakeControls:
    def __init__(self) -> None:
        self._items: Dict[str, _FakeControl] = {}

    def itemById(self, command_id: str) -> Any:
        control = self._items.get(command_id)
        return None if (control is None or control.deleted) else control

    def addCommand(self, command_definition: Any) -> _FakeControl:
        control = _FakeControl(command_definition.id)
        self._items[command_definition.id] = control
        return control


class _FakeCommandDefinition:
    def __init__(self, command_id: str, name: str) -> None:
        self.id = command_id
        self.name = name
        self.commandCreated = _Event()
        self.deleted = False

    def deleteMe(self) -> bool:
        self.deleted = True
        return True


class _FakeCommandDefinitions:
    def __init__(self) -> None:
        self._items: Dict[str, _FakeCommandDefinition] = {}
        self.last_created: Any = None

    def itemById(self, command_id: str) -> Any:
        command_definition = self._items.get(command_id)
        return None if (command_definition is None or command_definition.deleted) else command_definition

    def addButtonDefinition(self, command_id: str, name: str, tooltip: str, resource_folder: str = ""):
        command_definition = _FakeCommandDefinition(command_id, name)
        self._items[command_id] = command_definition
        self.last_created = command_definition
        return command_definition


class _FakePanel:
    def __init__(self, panel_id: str, name: str) -> None:
        self.id = panel_id
        self.name = name
        self.controls = _FakeControls()


class _FakeToolbarPanels:
    def __init__(self, panels: List[_FakePanel]) -> None:
        self._panels = panels

    @property
    def count(self) -> int:
        return len(self._panels)

    def item(self, index: int) -> Any:
        return self._panels[index]

    def itemById(self, panel_id: str) -> Any:
        for panel in self._panels:
            if panel.id == panel_id:
                return panel
        return None


class _FakePalette:
    """Test double for ``adsk.core.Palette``."""

    def __init__(self, palette_id: str, name: str, html_url: str) -> None:
        self.id = palette_id
        self.name = name
        self.html_url = html_url
        self.isVisible = True
        self.deleted = False
        self.dockingState = None
        self.incomingFromHTML = _Event()
        self.sent: List[Any] = []

    def sendInfoToHTML(self, action: str, data: str) -> bool:
        """Record one Python -> HTML message."""
        try:
            payload = json.loads(data)
        except Exception:
            payload = {"_raw": data}
        self.sent.append((action, payload))
        return True

    def deleteMe(self) -> bool:
        """Simulate Fusion deleting the palette."""
        self.deleted = True
        return True


class _FakePalettes:
    """Test double for ``adsk.core.Palettes``."""

    def __init__(self) -> None:
        self._items: Dict[str, _FakePalette] = {}
        self.add_requests: List[Any] = []

    def itemById(self, palette_id: str) -> Any:
        palette = self._items.get(palette_id)
        return None if (palette is None or palette.deleted) else palette

    def add(self, palette_id: str, name: str, html_url: str, is_visible: bool,
            show_close: bool, resizable: bool, width: int, height: int) -> _FakePalette:
        palette = _FakePalette(palette_id, name, html_url)
        self._items[palette_id] = palette
        self.add_requests.append((palette_id, name, html_url))
        return palette


class _FakeCommand:
    def __init__(self) -> None:
        self.isAutoExecute = True
        self.execute = _Event()


class _FakeCommandEventArgs:
    def __init__(self, command: _FakeCommand) -> None:
        self.command = command


class _FakeUserInterface:
    def __init__(self, panel_id: str = "SolidScriptsAddinsPanel") -> None:
        self.commandDefinitions = _FakeCommandDefinitions()
        self.panel = _FakePanel(panel_id, "ADD-INS")
        self.allToolbarPanels = _FakeToolbarPanels([self.panel])
        self.palettes = _FakePalettes()
        self.messages: List[str] = []
        self.commandTerminated = _Event()

    def messageBox(self, message: str, title: str = "") -> bool:
        self.messages.append(message)
        return True


class _FakeDocument:
    name = "Lifecycle Simulation.f3d"


class _FakeApp:
    def __init__(self) -> None:
        self.userInterface = _FakeUserInterface()
        self.activeProduct = None
        self.activeDocument = _FakeDocument()
        self.documentActivated = _Event()


_INSTALLED_APP: List[_FakeApp] = []


def _install_fake_adsk() -> None:
    """Register fake ``adsk``, ``adsk.core`` and ``adsk.fusion`` modules.

    The add-in only performs module-level ``import adsk.core`` / ``import
    adsk.fusion`` and subclasses the two event-handler base classes, so the fake
    surface can stay small.
    """

    class CommandCreatedEventHandler:
        def __init__(self) -> None:
            pass

    class CommandEventHandler:
        def __init__(self) -> None:
            pass

    class HTMLEventHandler:
        def __init__(self) -> None:
            pass

    class ApplicationCommandEventHandler:
        def __init__(self) -> None:
            pass

    class DocumentEventHandler:
        def __init__(self) -> None:
            pass

    class CustomEventHandler:
        def __init__(self) -> None:
            pass

    class PaletteDockingStates:
        PaletteDockStateRight = "right"
        PaletteDockStateFloating = "floating"

    class Application:
        @staticmethod
        def get():
            if not _INSTALLED_APP:
                _INSTALLED_APP.append(_FakeApp())
            return _INSTALLED_APP[0]

    class Design:
        @staticmethod
        def cast(product: Any) -> Any:
            return None

    core = types.ModuleType("adsk.core")
    core.CommandCreatedEventHandler = CommandCreatedEventHandler
    core.CommandEventHandler = CommandEventHandler
    core.HTMLEventHandler = HTMLEventHandler
    core.ApplicationCommandEventHandler = ApplicationCommandEventHandler
    core.DocumentEventHandler = DocumentEventHandler
    core.CustomEventHandler = CustomEventHandler
    core.PaletteDockingStates = PaletteDockingStates
    core.Application = Application
    core._FakeCommand = _FakeCommand  # type: ignore[attr-defined]
    core._FakeCommandEventArgs = _FakeCommandEventArgs  # type: ignore[attr-defined]

    fusion = types.ModuleType("adsk.fusion")
    fusion.Design = Design

    adsk = types.ModuleType("adsk")
    adsk.core = core  # type: ignore[attr-defined]
    adsk.fusion = fusion  # type: ignore[attr-defined]

    sys.modules["adsk"] = adsk
    sys.modules["adsk.core"] = core
    sys.modules["adsk.fusion"] = fusion


def _load_addin_module() -> Dict[str, Any]:
    """Execute the add-in entry point exactly as Fusion does (``__main__``)."""
    return runpy.run_path(str(ENTRY_POINT), run_name="__main__")


def _invoke_button_click(module: Dict[str, Any], ui: _FakeUserInterface) -> Any:
    """Simulate the button click; return the opened palette (if any)."""
    command_definition = ui.commandDefinitions.last_created
    assert command_definition is not None, "run() did not create a command definition"
    created_handler = command_definition.commandCreated.handlers[0]
    command = sys.modules["adsk.core"]._FakeCommand()
    with contextlib.redirect_stdout(io.StringIO()):
        created_handler.notify(sys.modules["adsk.core"]._FakeCommandEventArgs(command))
        check(command.isAutoExecute is True, "dialog-less command auto-executes")
        check(len(command.execute.handlers) == 1, "execute handler attached on commandCreated")
        command.execute.handlers[0].notify(None)
    return ui.palettes.itemById(_config().PALETTE_ID)


def _config() -> Any:
    """Return the add-in's config module as loaded by run()."""
    return sys.modules["filamatch_addin.config"]


def _addin_handler_count() -> int:
    """Count the log handlers the add-in attached to the 'Filamatch' logger."""
    import logging

    return sum(1 for h in logging.getLogger("Filamatch").handlers if getattr(h, "_filamatch_handler", False))


def _send_palette_message(palette: Any, action: str, payload: Dict[str, Any]) -> None:
    """Drive one incoming palette message through the registered handler."""
    handler = palette.incomingFromHTML.handlers[0]
    args = types.SimpleNamespace(action=action, data=json.dumps(payload))
    handler.notify(args)


def main() -> int:
    if not ENTRY_POINT.is_file():
        print("FAIL add-in entry point not found: {}".format(ENTRY_POINT))
        return 1

    _install_fake_adsk()
    check(True, "fake adsk API installed")

    # --- 1. Fusion-style load: entry point executed as __main__ -----------
    module = _load_addin_module()
    check(callable(module.get("run")), "loading as __main__ defines run()")
    check(callable(module.get("stop")), "loading as __main__ defines stop()")
    check(module.get("PACKAGE") == "filamatch_addin", "the entry point names its package")

    ui = sys.modules["adsk.core"].Application.get().userInterface

    # --- 2. run() registers the ADD-INS button ----------------------------
    with contextlib.redirect_stdout(io.StringIO()):
        module["run"]({"IsApplicationStartup": False})
    check("filamatch_addin.lib.palette_bridge" in sys.modules, "run() loads the add-in package")
    module["CMD_ID"] = _config().CMD_ID
    command_definition = ui.commandDefinitions.last_created
    check(command_definition is not None, "run() creates a command definition")
    check(
        command_definition.id == module["CMD_ID"],
        "command definition id is the add-in's documented id",
    )
    control = ui.panel.controls.itemById(module["CMD_ID"])
    check(control is not None, "button control was added to the ADD-INS panel")
    check(
        control is not None and control.isPromoted and control.isPromotedByDefault,
        "button is promoted on the ADD-INS panel",
    )

    # --- 3. Button click opens the palette --------------------------------
    palette = _invoke_button_click(module, ui)
    check(palette is not None, "clicking the button opens the HTML palette")
    check(
        palette is not None and "palette/index.html" in palette.html_url,
        "palette is created with the shipped index.html",
    )
    check(
        palette is not None and len(palette.incomingFromHTML.handlers) == 1,
        "palette incoming-message handler is attached",
    )
    check(not ui.messages, "opening the palette shows no error dialog")
    app = _INSTALLED_APP[0] if _INSTALLED_APP else None
    check(
        len(ui.commandTerminated.handlers) == 1
        and app is not None and len(app.documentActivated.handlers) == 1,
        "the open palette watches finished commands and design switches",
    )

    if palette is not None:
        # Palette startup handshake.
        _send_palette_message(palette, "init", {})
        init_replies = [item for item in palette.sent if item[0] == "initData"]
        check(len(init_replies) == 1, "init action receives an initData reply")
        init_data = init_replies[-1][1] if init_replies else {}
        catalog_rows = init_data.get("catalog") or []
        bundled_ids = {
            "bambu_pla_basic_black",
            "bambu_petg_hf_white",
            "prusament_pla_orange",
            "polymaker_polylite_abs_black",
            "generic_abs_gf30",
        }
        check(
            isinstance(catalog_rows, list)
            and len(catalog_rows) >= 5
            and bundled_ids.issubset({row.get("id") for row in catalog_rows}),
            "initData contains the bundled 5-entry catalog (plus any user entries)",
        )
        check(
            init_data.get("version") == _config().VERSION,
            "initData reports the add-in version",
        )
        settings = init_data.get("settings") or {}
        check(
            "write_project_settings" in settings and "orca_path" in settings,
            "initData contains persisted settings keys",
        )
        categories = init_data.get("categories") or []
        category_names = [item.get("name") for item in categories]
        check(
            isinstance(categories, list)
            and category_names[:4] == ["PLA", "PETG", "ABS", "Fiber-Reinforced"]
            and all(name for name in category_names),
            "initData groups the bundled catalog by category in first-appearance order",
        )
        check(
            isinstance(init_data.get("materials"), list) and init_data["materials"] == [],
            "initData reports no assembly materials when no design is active",
        )
        library = init_data.get("library") or {}
        check(
            library.get("writable") is False and bool(library.get("message")),
            "initData explains the material-library limitation",
        )
        favorites = init_data.get("favorites") or {}
        check(
            isinstance(favorites.get("count"), int) and isinstance(favorites.get("names"), list),
            "initData reports the app-level Favorites summary",
        )
        check(
            isinstance(init_data.get("profiles"), dict),
            "initData reports the slicer profile summary",
        )
        check(
            isinstance(init_data.get("openfilament"), dict),
            "initData reports the Open Filament Database summary",
        )
        check(
            isinstance(init_data.get("custom_library"), dict),
            "initData reports the custom material library summary",
        )

        # Fusion echoes every sendInfoToHTML reply as a "response" action; it must
        # be ignored without generating another reply (feedback-loop guard).
        sent_before = len(palette.sent)
        _send_palette_message(palette, "response", {"value": True})
        check(
            len(palette.sent) == sent_before,
            "response messages are ignored without creating a reply loop",
        )

        # Smoke-test action (diagnostics module).
        _send_palette_message(palette, "run_diagnostics", {})
        smoke_replies = [item for item in palette.sent if item[0] == "diagnosticsResult"]
        check(len(smoke_replies) == 1, "run_diagnostics action receives a diagnosticsResult reply")
        smoke_text = smoke_replies[-1][1].get("text", "") if smoke_replies else ""
        check(
            "Filamatch loaded" in smoke_text,
            "smoke-test reply starts with the required loaded line",
        )
        check(
            "Palette UI: OK" in smoke_text,
            "smoke-test reply confirms the palette assets",
        )
        check(
            "3MF exporter: OK" in smoke_text,
            "smoke-test reply confirms the exporter module",
        )

    # --- 4. stop() cleans up and a second run() works ---------------------
    with contextlib.redirect_stdout(io.StringIO()):
        module["stop"]({"IsApplicationStartup": False})
    check(ui.panel.controls.itemById(module["CMD_ID"]) is None, "stop() removes the button control")
    check(
        ui.commandDefinitions.itemById(module["CMD_ID"]) is None,
        "stop() removes the command definition",
    )
    check(
        ui.palettes.itemById(_config().PALETTE_ID) is None,
        "stop() closes the palette",
    )
    check(_addin_handler_count() == 0, "stop() removes the add-in's log handlers")
    check(
        not ui.commandTerminated.handlers
        and app is not None and not app.documentActivated.handlers,
        "stop() removes the design change watch",
    )

    with contextlib.redirect_stdout(io.StringIO()):
        module["run"]({"IsApplicationStartup": False})
    check(
        ui.panel.controls.itemById(module["CMD_ID"]) is not None,
        "run() after stop() re-registers the button (stop/start cycle)",
    )

    # Fusion executes the entry file again on every Run: no duplicate log handlers.
    module = _load_addin_module()
    with contextlib.redirect_stdout(io.StringIO()):
        module["run"]({"IsApplicationStartup": False})
        module["stop"]({"IsApplicationStartup": False})
        module = _load_addin_module()
        module["run"]({"IsApplicationStartup": False})
    module["CMD_ID"] = _config().CMD_ID
    check(_addin_handler_count() == 2, "repeated loads keep one console and one file log handler")

    # --- 5. Renamed ADD-INS panel fallback --------------------------------
    fresh_app = _FakeApp()
    fresh_app.userInterface = _FakeUserInterface(panel_id="RenamedAddInsPanel")
    _INSTALLED_APP[0] = fresh_app
    with contextlib.redirect_stdout(io.StringIO()):
        module["run"]({"IsApplicationStartup": False})
    check(
        fresh_app.userInterface.panel.controls.itemById(module["CMD_ID"]) is not None,
        "fallback panel scan finds a renamed ADD-INS panel",
    )
    check(not fresh_app.userInterface.messages, "fallback registration shows no error dialog")

    print()
    import shutil

    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
    os.environ.pop("FILAMATCH_DATA_DIR", None)
    if _FAILED:
        print("LIFECYCLE SIMULATION FAILED ({} passed, {} failed)".format(_PASSED, _FAILED))
        return 1
    print("LIFECYCLE SIMULATION PASSED ({} checks)".format(_PASSED))
    return 0


if __name__ == "__main__":
    sys.exit(main())
