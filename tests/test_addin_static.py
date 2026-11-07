#!/usr/bin/env python3
"""Static checks of the add-in package (no Fusion needed).

Checks the JSON manifest Fusion reads, the version and ids in config.py,
the files Fusion needs, and that every module imports with its public API.
Run from the repo root: ``python3 tests/test_addin_static.py``.
"""

from __future__ import annotations

import importlib
import json
import sys
import traceback
import uuid
from pathlib import Path
from typing import Callable, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
ADDIN_DIR = REPO_ROOT / "Filamatch"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Filamatch import config  # noqa: E402


def lib(name: str):
    """Import ``Filamatch.lib.<name>``."""
    return importlib.import_module("Filamatch.lib." + name)


def check_manifest() -> None:
    """The manifest is JSON with the fields Fusion requires (an XML one is ignored)."""
    raw = config.MANIFEST_FILE.read_text(encoding="utf-8")
    assert not raw.lstrip().startswith("<"), "manifest must be JSON, not the legacy XML format"
    manifest = json.loads(raw)
    assert manifest.get("autodeskProduct") == "Fusion"
    assert manifest.get("type") == "addin"
    assert manifest.get("version") == config.VERSION, "manifest version must match config.VERSION"
    assert isinstance(manifest.get("description"), dict) and manifest["description"].get("")
    assert isinstance(manifest.get("runOnStartup"), bool)
    assert manifest.get("supportedOS")
    assert isinstance(manifest.get("editEnabled"), bool)
    uuid.UUID(str(manifest.get("id")))
    assert (ADDIN_DIR / manifest.get("iconFilename", "")).is_file(), "manifest icon is missing"
    assert config.MANIFEST_FILE.stem == "Filamatch" and (ADDIN_DIR / "Filamatch.py").is_file()


def check_shipped_files() -> None:
    """Every file Fusion or the palette loads is present."""
    for relative in (
        "__init__.py",
        "config.py",
        "commands/__init__.py",
        "commands/open_palette/entry.py",
        "commands/open_palette/resources/16x16.png",
        "commands/open_palette/resources/32x32.png",
        "commands/open_palette/resources/64x64.png",
        "palette/index.html",
        "palette/app.js",
        "palette/style.css",
        "palette/filamatch.svg",
        "data/filaments.json",
        "lib/__init__.py",
    ):
        assert (ADDIN_DIR / relative).is_file(), "missing " + relative


def check_ids() -> None:
    """Fusion ids use only letters, digits and '_' and carry the company prefix."""
    for value in (config.CMD_ID, config.PALETTE_ID, config.JOB_EVENT_ID):
        assert value.replace("_", "").isalnum(), value
        assert value.startswith(config.COMPANY_NAME + "_"), value
    assert config.USER_AGENT.endswith(config.VERSION)


def check_catalog() -> None:
    """The bundled catalog loads and every entry is usable."""
    entries = lib("catalog").load_catalog()
    assert entries, "the bundled catalog is empty"
    for entry in entries:
        assert entry.id and entry.color_hex.startswith("#") and entry.density_g_cm3 > 0, entry.id


def check_module_apis() -> None:
    """Every module imports and exposes the functions the palette bridge calls."""
    expected = {
        "threemf_injector": ("inject_file", "inject", "load_mapping", "load_filaments"),
        "exporter": ("export_3mf", "export_and_inject", "default_export_path"),
        "diagnostics": ("run_diagnostics",),
        "materials_workflow": ("collect_assembly_materials", "build_mapping", "apply_unique_export_names"),
        "favorites": ("ensure_favorite_filament", "import_catalog_to_favorites", "favorites_summary"),
        "slicer_profiles": ("find_installed_slicers", "list_vendors", "prepare_project"),
        "openfilament": ("ensure_dataset", "search", "load_user_catalog"),
        "custom_library": ("build_library_file", "ensure_custom_library", "custom_library_status"),
        "bambuddy": ("BambuddyClient", "parse_spool"),
        "printer_model": ("SlotInfo", "PrinterContext", "material_family"),
        "printer_sources": ("fetch_context", "selected_source_id"),
        "secret_store": ("get_secret", "set_secret", "delete_secret"),
        "inventory_sync": ("entry_for_spool", "attach_slot_manifest"),
        "slot_mapping": ("auto_match", "build_mapping"),
        "palette_bridge": ("show_palette", "close_palette", "dispatch"),
        "addin_logging": ("configure", "shutdown"),
    }
    for module_name, names in expected.items():
        module = lib(module_name)
        for name in names:
            assert callable(getattr(module, name, None)), "{}.{} is missing".format(module_name, name)
    assert lib("palette_bridge").PALETTE_HTML.is_file()


def check_data_outside_temp() -> None:
    """By default settings and user data never live in the temp folder (the OS cleans it)."""
    import os
    import tempfile

    saved = os.environ.pop(config.DATA_DIR_ENV, None)
    try:
        data_dir, _cache_dir, _log_dir = config._user_dirs()
    finally:
        if saved is not None:
            os.environ[config.DATA_DIR_ENV] = saved
    temp = Path(tempfile.gettempdir()).resolve()
    assert temp not in data_dir.resolve().parents, data_dir


CHECKS: List[Tuple[str, Callable[[], None]]] = [
    ("manifest is valid JSON for Fusion", check_manifest),
    ("shipped files are present", check_shipped_files),
    ("Fusion ids and user agent are well formed", check_ids),
    ("bundled catalog loads", check_catalog),
    ("modules import with their public API", check_module_apis),
    ("user data is not kept in the temp folder", check_data_outside_temp),
]


def main() -> int:
    failures = 0
    for label, check in CHECKS:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - report every failure
            failures += 1
            print("FAIL {}: {}".format(label, exc))
            traceback.print_exc()
        else:
            print("PASS {}".format(label))
    if failures:
        print("ADD-IN STATIC CHECKS FAILED ({} of {})".format(failures, len(CHECKS)))
        return 1
    print("ADD-IN STATIC CHECKS PASSED ({} checks)".format(len(CHECKS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
