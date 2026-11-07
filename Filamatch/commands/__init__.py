"""Toolbar commands of the add-in (one folder per command, as in Autodesk's template)."""

from ..lib import addin_logging
from .open_palette import entry as open_palette

#: Started in order by :func:`start`, stopped in reverse order by :func:`stop`.
COMMANDS = [open_palette]


def start() -> None:
    """Set up logging, then add every command to Fusion's toolbar."""
    addin_logging.configure()
    for command in COMMANDS:
        command.start()


def stop() -> None:
    """Remove every command, then close the log file."""
    try:
        for command in reversed(COMMANDS):
            command.stop()
    finally:
        addin_logging.shutdown()
