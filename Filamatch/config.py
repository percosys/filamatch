"""Filamatch settings shared by every module: names, ids, version and file locations.

Follows Autodesk's add-in template (``config.py`` next to the entry point).
Modules in ``lib/`` import it with ``from .. import config``.

File locations (per user, never inside the add-in folder):

==========  ======================================  ==========================================
What        macOS                                   Windows
==========  ======================================  ==========================================
Data        ~/Library/Application Support/Filamatch  %APPDATA%\\Filamatch
Cache       ~/Library/Caches/Filamatch               %LOCALAPPDATA%\\Filamatch\\Cache
Logs        ~/Library/Logs/Filamatch                 %LOCALAPPDATA%\\Filamatch\\Logs
Documents   ~/Documents/Filamatch                    %USERPROFILE%\\Documents\\Filamatch
==========  ======================================  ==========================================

*Data* holds settings, filaments added from the Open Filament Database and
imported Orca profiles. *Cache* only holds files that can be downloaded
again. *Documents* holds the Filamatch material library builds.

Set ``FILAMATCH_DATA_DIR`` to put data, cache and logs under one folder
(the tests do this so they never touch the real files).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ADDIN_NAME = "Filamatch"
COMPANY_NAME = "Percosys"
VERSION = "0.6.1"

#: Writes debug messages to the TEXT COMMANDS window when True.
DEBUG = False

#: Fusion ids: company and add-in name keep them unique across add-ins.
CMD_ID = "{}_{}_OpenPalette".format(COMPANY_NAME, ADDIN_NAME)
PALETTE_ID = "{}_{}_Palette".format(COMPANY_NAME, ADDIN_NAME)
JOB_EVENT_ID = "{}_{}_JobFinished".format(COMPANY_NAME, ADDIN_NAME)

#: Sent with every HTTP request (Bambuddy, Open Filament Database, GitHub).
USER_AGENT = "{}/{}".format(ADDIN_NAME, VERSION)

ADDIN_DIR = Path(__file__).resolve().parent
PALETTE_DIR = ADDIN_DIR / "palette"
CATALOG_FILE = ADDIN_DIR / "data" / "filaments.json"
MANIFEST_FILE = ADDIN_DIR / "{}.manifest".format(ADDIN_NAME)

DATA_DIR_ENV = "FILAMATCH_DATA_DIR"


def _user_dirs() -> tuple:
    """Return ``(data, cache, logs)`` for this platform or the test override."""
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        base = Path(override)
        return base, base / "cache", base
    home = Path.home()
    if sys.platform == "darwin":
        return (
            home / "Library" / "Application Support" / ADDIN_NAME,
            home / "Library" / "Caches" / ADDIN_NAME,
            home / "Library" / "Logs" / ADDIN_NAME,
        )
    if sys.platform.startswith("win"):
        roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return roaming / ADDIN_NAME, local / ADDIN_NAME / "Cache", local / ADDIN_NAME / "Logs"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    return config_home / ADDIN_NAME, cache_home / ADDIN_NAME, cache_home / ADDIN_NAME / "logs"


DATA_DIR, CACHE_DIR, LOG_DIR = _user_dirs()
SETTINGS_FILE = DATA_DIR / "settings.json"
USER_CATALOG_FILE = DATA_DIR / "user_catalog.json"
PROFILES_DIR = DATA_DIR / "profiles"
LOG_FILE = LOG_DIR / "filamatch.log"

#: Visible output folder for library builds (and the default export folder).
DOCUMENTS_DIR = Path.home() / "Documents" / ADDIN_NAME

#: Folders used by earlier versions. Their data is copied into DATA_DIR once.
LEGACY_DATA_DIRS = (
    Path(tempfile.gettempdir()) / ADDIN_NAME,
    Path(tempfile.gettempdir()) / "FusionFilamentBridge",
)
#: Library folder of FusionFilament Bridge; a saved library path inside it is dropped.
LEGACY_DOCUMENTS_DIR = Path.home() / "Documents" / "FusionFilamentBridge"
