"""The Filamatch toolbar button (UTILITIES tab, ADD-INS panel) that opens the palette."""

from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Any, List, Optional, Tuple

from ... import config
from ...lib import palette_bridge

try:  # pragma: no cover - the adsk modules only exist inside Fusion.
    import adsk.core
except ImportError:  # pragma: no cover - tests and command-line runs.
    adsk = None  # type: ignore[assignment]

LOGGER = logging.getLogger(config.ADDIN_NAME)

CMD_NAME = config.ADDIN_NAME
CMD_TOOLTIP = (
    "Filamatch: match the filaments in your design to the slots on your "
    "printer, then send the design to OrcaSlicer."
)
RESOURCES_DIR = Path(__file__).resolve().parent / "resources"

#: Toolbar panel ids that have hosted the ADD-INS panel across Fusion versions.
PANEL_IDS: Tuple[str, ...] = ("SolidScriptsAddinsPanel", "AddInsPanel")

#: Command ids of earlier versions, removed so an upgrade never shows two buttons.
LEGACY_CMD_IDS: Tuple[str, ...] = ("FilamatchCmd", "FusionFilamentBridgeSmokeTestCmd")

#: Fusion keeps only weak references to Python handlers.
_HANDLERS: List[Any] = []
#: The execute handler of the current command run (replaced on every click).
_EXECUTE_HANDLER: List[Any] = []


def _find_addins_panel(ui: Any) -> Optional[Any]:
    """Return the ADD-INS toolbar panel, or None when this workspace has none."""
    for panel_id in PANEL_IDS:
        try:
            panel = ui.allToolbarPanels.itemById(panel_id)
        except Exception:
            LOGGER.exception("Looking up toolbar panel %r failed", panel_id)
            continue
        if panel is not None:
            return panel
    # Newer Fusion builds have renamed panels before: look for an ADD-INS-like one.
    try:
        count = int(ui.allToolbarPanels.count)
    except Exception:
        count = 0
    for index in range(count):
        try:
            panel = ui.allToolbarPanels.item(index)
        except Exception:
            continue
        if panel is None:
            continue
        haystack = "{} {}".format(getattr(panel, "id", ""), getattr(panel, "name", ""))
        if "addin" in haystack.lower().replace(" ", "").replace("-", ""):
            LOGGER.warning("Using the fallback ADD-INS panel %r", getattr(panel, "id", ""))
            return panel
    return None


def _remove_ui(ui: Any) -> None:
    """Remove this add-in's buttons and command definitions (current and legacy ids)."""
    if ui is None:
        return
    command_ids = (config.CMD_ID,) + LEGACY_CMD_IDS
    panels = [ui.allToolbarPanels.itemById(panel_id) for panel_id in PANEL_IDS]
    panels.append(_find_addins_panel(ui))
    for panel in panels:
        if panel is None:
            continue
        for command_id in command_ids:
            try:
                control = panel.controls.itemById(command_id)
                if control is not None:
                    control.deleteMe()
            except Exception:
                LOGGER.exception("Could not remove toolbar control %r", command_id)
    for command_id in command_ids:
        try:
            definition = ui.commandDefinitions.itemById(command_id)
            if definition is not None:
                definition.deleteMe()
        except Exception:
            LOGGER.exception("Could not remove command definition %r", command_id)


if adsk is not None:  # pragma: no branch - always true inside Fusion.

    class _CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):  # type: ignore[misc]
        """Attach the execute handler to each new run of the command."""

        def notify(self, args: Any) -> None:
            try:
                command = args.command
                command.isAutoExecute = True
                handler = _CommandExecuteHandler()
                command.execute.add(handler)
                _EXECUTE_HANDLER[:] = [handler]
            except Exception:
                LOGGER.exception("Could not set up the Filamatch command")

    class _CommandExecuteHandler(adsk.core.CommandEventHandler):  # type: ignore[misc]
        """Open the palette; show a message box when that fails."""

        def notify(self, args: Any) -> None:
            ui = adsk.core.Application.get().userInterface
            try:
                palette_bridge.show_palette(ui)
            except Exception:
                LOGGER.exception("Could not open the palette")
                try:
                    ui.messageBox(
                        "Could not open the Filamatch palette:\n\n" + traceback.format_exc(),
                        config.ADDIN_NAME,
                    )
                except Exception:
                    pass


def start() -> None:
    """Add the Filamatch button to the ADD-INS panel."""
    if adsk is None:
        raise RuntimeError("Filamatch must be started by Autodesk Fusion.")
    LOGGER.info("Starting %s %s", config.ADDIN_NAME, config.VERSION)
    ui = adsk.core.Application.get().userInterface
    _remove_ui(ui)  # leftovers of an aborted load or an earlier version

    definitions = ui.commandDefinitions
    if RESOURCES_DIR.is_dir():
        definition = definitions.addButtonDefinition(
            config.CMD_ID, CMD_NAME, CMD_TOOLTIP, str(RESOURCES_DIR)
        )
    else:
        definition = definitions.addButtonDefinition(config.CMD_ID, CMD_NAME, CMD_TOOLTIP)
    handler = _CommandCreatedHandler()
    definition.commandCreated.add(handler)
    _HANDLERS.append(handler)

    panel = _find_addins_panel(ui)
    if panel is None:
        raise RuntimeError(
            "Could not find the ADD-INS toolbar panel (tried {}).".format(", ".join(PANEL_IDS))
        )
    control = panel.controls.itemById(config.CMD_ID)
    if control is None:
        control = panel.controls.addCommand(definition)
    try:
        control.isPromotedByDefault = True
        control.isPromoted = True
    except Exception:
        LOGGER.debug("Could not promote the Filamatch button", exc_info=True)
    LOGGER.info("Add-in loaded")


def stop() -> None:
    """Close the palette and remove the button."""
    ui = adsk.core.Application.get().userInterface if adsk is not None else None
    try:
        palette_bridge.close_palette(ui)
    except Exception:
        LOGGER.exception("Could not close the palette")
    try:
        _remove_ui(ui)
    finally:
        del _HANDLERS[:]
        del _EXECUTE_HANDLER[:]
    LOGGER.info("Add-in stopped")
