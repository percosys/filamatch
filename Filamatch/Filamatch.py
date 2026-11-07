"""Filamatch: the entry point that Autodesk Fusion loads.

Fusion calls :func:`run` when the add-in starts and :func:`stop` when it
stops. Everything else lives in the add-in package:

- ``config.py``            names, ids, version and file locations
- ``commands/``            one folder per toolbar command
- ``lib/``                 palette bridge, materials, printers, export pipeline
- ``palette/``             the HTML palette (index.html, app.js, style.css)
- ``data/filaments.json``  the bundled filament catalog

Fusion runs this file without a package (``__package__`` is None), so it
imports its own folder as the package :data:`PACKAGE` by file location. That
keeps the add-in's modules under one unique name: nothing is added to
``sys.path`` and module names cannot clash with other add-ins.
"""

import importlib
import importlib.util
import sys
import traceback
from pathlib import Path

_ADDIN_DIR = Path(__file__).resolve().parent

#: Package name for the add-in folder inside Fusion's shared Python process.
PACKAGE = "filamatch_addin"


def _load_package() -> None:
    """Import the add-in folder as :data:`PACKAGE`, replacing an earlier copy.

    Fusion keeps modules loaded between Stop and Run, so a restart would
    otherwise keep running the old code after an update.
    """
    for name in [n for n in sys.modules if n == PACKAGE or n.startswith(PACKAGE + ".")]:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        str(_ADDIN_DIR / "__init__.py"),
        submodule_search_locations=[str(_ADDIN_DIR)],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = package
    spec.loader.exec_module(package)


def _report(stage: str) -> None:
    """Show a start/stop failure in Fusion (and on stdout for the log)."""
    message = "Filamatch could not {}:\n\n{}".format(stage, traceback.format_exc())
    print(message)
    try:
        import adsk.core

        adsk.core.Application.get().userInterface.messageBox(message, "Filamatch")
    except Exception:
        pass


def run(context):
    """Start the add-in: set up logging and add the toolbar command."""
    try:
        _load_package()
        importlib.import_module(PACKAGE + ".commands").start()
    except Exception:
        _report("start")


def stop(context):
    """Stop the add-in: close the palette and remove the toolbar command."""
    try:
        commands = sys.modules.get(PACKAGE + ".commands")
        if commands is not None:
            commands.stop()
    except Exception:
        _report("stop")
