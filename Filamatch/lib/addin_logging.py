"""Logging for the add-in: Fusion's TEXT COMMANDS window and a rotating log file.

Every module logs to ``Filamatch`` or a child logger (``Filamatch.<module>``).
Fusion keeps the ``logging`` module alive between Stop and Run, so
:func:`configure` first removes the handlers of an earlier run; without that
every restart would add another copy of each line.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys

from .. import config

LOGGER_NAME = config.ADDIN_NAME
_MARK = "_filamatch_handler"


def configure() -> logging.Logger:
    """Attach the console and file handlers once and return the add-in logger."""
    logger = logging.getLogger(LOGGER_NAME)
    shutdown()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if config.DEBUG else logging.WARNING)
    console.setFormatter(formatter)
    setattr(console, _MARK, True)
    logger.addHandler(console)

    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            str(config.LOG_FILE), maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        setattr(file_handler, _MARK, True)
        logger.addHandler(file_handler)
    except OSError:
        logger.warning("Could not open the log file %s", config.LOG_FILE, exc_info=True)
    return logger


def shutdown() -> None:
    """Remove and close the handlers that :func:`configure` added."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if getattr(handler, _MARK, False):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
