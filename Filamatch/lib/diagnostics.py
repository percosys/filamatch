"""Filamatch diagnostics: the report behind Settings > Advanced > Run diagnostics.

Each check is reported on its own line and no failure stops the others (or
Fusion). The report is meant to be pasted into a bug report: it lists the
version, file locations, every module, the secret store, the printer source
and the active design.

``python3 -m Filamatch.lib.diagnostics`` prints the report outside Fusion
(without the Fusion API checks).
"""

from __future__ import annotations

import logging
import tempfile
import traceback
from pathlib import Path
from typing import Any, List

from .. import config

APP_NAME = config.ADDIN_NAME
ADDIN_VERSION = config.VERSION

_ADDIN_DIR = config.ADDIN_DIR
_LOG_DIR = config.LOG_DIR
_LOG_FILE = config.LOG_FILE

LOGGER = logging.getLogger("Filamatch")

try:  # pragma: no cover - the adsk modules only exist inside Fusion.
    import adsk.core
    import adsk.fusion
except ImportError:  # pragma: no cover - exercised by the standalone run.
    adsk = None  # type: ignore[assignment]


class _Report:
    """Collects the report lines (and copies them to the log file)."""

    def __init__(self) -> None:
        self.lines: List[str] = []

    def add(self, line: str = "") -> None:
        """Append one report line."""
        self.lines.append(line)
        if line.strip():
            LOGGER.debug("diagnostics: %s", line)

    def text(self) -> str:
        """Return the complete report as a single string."""
        return "\n".join(self.lines)


def _print_template_availability(report: "_Report", design: Any, app: Any) -> None:
    """Report whether each catalog template material is reachable.

    Args:
        design: Active Fusion ``Design`` object.
        app: Fusion ``Application`` object.
    """
    try:
        from . import catalog
        from . import importer
    except Exception:
        report.add("Template material check: SKIPPED (catalog/importer not importable)")
        report.add(traceback.format_exc())
        return

    try:
        entries = catalog.load_catalog()
    except Exception:
        report.add("Template material check: SKIPPED (catalog failed to load)")
        report.add(traceback.format_exc())
        return

    template_names = sorted({entry.template_material for entry in entries})
    report.add("Template material availability:")
    for template_name in template_names:
        try:
            importer.find_template_material(design, template_name, app)
        except importer.TemplateMaterialNotFoundError as exc:
            report.add("  - {}: NOT FOUND ({})".format(template_name, exc))
        except Exception as exc:  # noqa: BLE001 - diagnostics must never crash.
            report.add("  - {}: ERROR ({})".format(template_name, exc))
        else:
            report.add("  - {}: found".format(template_name))


def _print_design_diagnostics(report: "_Report") -> None:
    """Report the active document."""
    if adsk is None:
        report.add("Active design: unavailable (adsk modules not loaded)")
        return

    try:
        app = adsk.core.Application.get()
    except Exception:
        report.add("Active design: ERROR obtaining the Application object")
        report.add(traceback.format_exc())
        return

    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
    except Exception:
        report.add("Active design: ERROR reading the active product")
        report.add(traceback.format_exc())
        return

    if design is None:
        report.add("Active design: none (open a design to import filament materials)")
        return

    try:
        document_name = app.activeDocument.name
    except Exception:
        document_name = "<unknown>"
    report.add("Active design: {}".format(document_name))

    try:
        report.add("  Root component: {}".format(design.rootComponent.name))
        report.add(
            "  Bodies in root component: {}".format(design.rootComponent.bRepBodies.count)
        )
    except Exception:
        report.add("  Bodies in root component: ERROR")
        report.add(traceback.format_exc())

    try:
        report.add("  Materials in document: {}".format(design.materials.count))
    except Exception:
        report.add("  Materials in document: ERROR")
        report.add(traceback.format_exc())

    _print_template_availability(report, design, app)


def run_diagnostics() -> str:
    """Run every diagnostic check and return the report text.

    The first line is always exactly ``Filamatch loaded``.  Catalog,
    importer, injector, exporter, palette files and the active design are
    checked independently; a failure in one section does not stop the others.

    Returns:
        The complete report as a newline-separated string.
    """
    report = _Report()

    report.add("Filamatch loaded")
    report.add("{} version {}".format(APP_NAME, ADDIN_VERSION))
    report.add("Add-in directory: {}".format(_ADDIN_DIR))
    report.add("Log file: {}".format(_LOG_FILE))
    report.add("Data folder: {}".format(config.DATA_DIR))
    report.add("Cache folder: {}".format(config.CACHE_DIR))
    report.add("")

    # --- catalog -----------------------------------------------------------
    try:
        from . import catalog

        entries = catalog.load_catalog()
        report.add("Catalog: OK ({} filament definitions)".format(len(entries)))
        for entry in entries:
            report.add(
                "  - {}: {} [{}] density={:.2f} g/cm3".format(
                    entry.id,
                    entry.display_name,
                    entry.slicer_filament_type,
                    entry.density_g_cm3,
                )
            )
    except Exception:
        report.add("Catalog: FAILED")
        report.add(traceback.format_exc())

    # --- importer ----------------------------------------------------------
    try:
        from . import importer

        report.add(
            "Material importer: OK (Density via {} with g/cm3 conversion)".format(
                "materialProperties/physicalProperties"
            )
        )
        del importer
    except Exception:
        report.add("Material importer: FAILED to import")
        report.add(traceback.format_exc())

    # --- 3MF injector ------------------------------------------------------
    try:
        from . import threemf_injector

        parts = [threemf_injector.MODEL_SETTINGS_PART]
        if hasattr(threemf_injector, "PROJECT_SETTINGS_PART"):
            parts.append(threemf_injector.PROJECT_SETTINGS_PART)
        report.add("3MF injector: OK (writes {})".format(", ".join(parts)))
        if not hasattr(threemf_injector, "FilamentSlot"):
            report.add("  - note: project_settings.config filament arrays unavailable")
    except Exception:
        report.add("3MF injector: FAILED to import")
        report.add(traceback.format_exc())

    # --- exporter ----------------------------------------------------------
    try:
        from . import exporter

        report.add(
            "3MF exporter: OK (collects design objects, exports and injects atomically)"
        )
        del exporter
    except Exception:
        report.add("3MF exporter: FAILED to import")
        report.add(traceback.format_exc())

    # --- materials workflow -------------------------------------------------
    try:
        from . import materials_workflow

        required_api = (
            "import_catalog_entries",
            "collect_assembly_materials",
            "build_mapping",
            "build_filament_slots",
            "apply_export_appearances",
            "restore_appearances",
            "apply_unique_export_names",
            "restore_body_names",
        )
        missing = [name for name in required_api if not callable(getattr(materials_workflow, name, None))]
        if missing:
            report.add(
                "Materials workflow: PARTIAL (missing {})".format(", ".join(missing))
            )
        else:
            report.add(
                "Materials workflow: OK (colored appearances + automatic material detection)"
            )
        del materials_workflow
    except Exception:
        report.add("Materials workflow: FAILED to import")
        report.add(traceback.format_exc())

    # --- favorites workflow -------------------------------------------------
    try:
        from . import favorites

        for name in (
            "ensure_favorite_filament",
            "import_catalog_to_favorites",
            "favorites_summary",
        ):
            if not callable(getattr(favorites, name, None)):
                raise RuntimeError("missing favorites API: {}".format(name))
        report.add("Favorites library: OK (app-level materials + colored appearances)")
        del favorites
    except Exception:
        report.add("Favorites library: FAILED to import")
        report.add(traceback.format_exc())

    # --- slicer profiles / printer targets ---------------------------------
    try:
        from . import slicer_profiles

        for name in ("find_installed_slicers", "list_vendors", "list_printers", "prepare_project"):
            if not callable(getattr(slicer_profiles, name, None)):
                raise RuntimeError("missing slicer_profiles API: {}".format(name))
        report.add("Slicer profiles: OK (installed OrcaSlicer/Bambu Studio + downloaded OrcaSlicer profiles)")
    except Exception:
        report.add("Slicer profiles: FAILED to import")
        report.add(traceback.format_exc())

    # --- Open Filament Database --------------------------------------------
    try:
        from . import openfilament

        for name in ("ensure_dataset", "search", "get_entries", "load_user_catalog"):
            if not callable(getattr(openfilament, name, None)):
                raise RuntimeError("missing openfilament API: {}".format(name))
        summary = openfilament.dataset_summary()
        state = "cached" if summary.get("cached") else "not downloaded yet"
        version = str(summary.get("version") or "").strip()
        report.add("Open Filament Database: OK ({})".format(" ".join(filter(None, (state, version)))))
    except Exception:
        report.add("Open Filament Database: FAILED to import")
        report.add(traceback.format_exc())

    # --- custom material library -------------------------------------------
    try:
        from . import custom_library

        for name in ("find_favorites_library", "build_library_file", "ensure_custom_library"):
            if not callable(getattr(custom_library, name, None)):
                raise RuntimeError("missing custom_library API: {}".format(name))
        report.add("Custom material library: OK (snapshot of Favorites -> .adsklib)")
    except Exception:
        report.add("Custom material library: FAILED to import")
        report.add(traceback.format_exc())

    # --- palette files -----------------------------------------------------
    try:
        palette_dir = _ADDIN_DIR / "palette"
        missing = [
            name
            for name in ("index.html", "style.css", "app.js")
            if not (palette_dir / name).is_file()
        ]
        if missing:
            report.add("Palette UI: MISSING files: {}".format(", ".join(missing)))
        else:
            report.add("Palette UI: OK (index.html, style.css, app.js)")
    except Exception:
        report.add("Palette UI: ERROR checking files")
        report.add(traceback.format_exc())

    # --- secrets and the printer source -----------------------------------
    try:
        from . import secret_store

        backend = secret_store.backend()
        report.add(
            "Secret store: {}".format(
                getattr(backend, "name", "unavailable (the API key stays in settings.json)")
                if backend is not None
                else "unavailable (the API key stays in settings.json)"
            )
        )
    except Exception:
        report.add("Secret store: FAILED to import")
        report.add(traceback.format_exc())
    try:
        from . import palette_bridge
        from . import printer_sources

        settings = palette_bridge.load_settings()
        report.add(
            "Printer source: {} ({})".format(
                printer_sources.selected_source_id(settings),
                "configured" if printer_sources.is_configured(settings) else "not set up",
            )
        )
    except Exception:
        report.add("Printer source: ERROR reading the settings")
        report.add(traceback.format_exc())

    report.add("")
    _print_design_diagnostics(report)
    report.add("")
    report.add("Diagnostics complete. The log file has the full details.")
    return report.text()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    print(run_diagnostics())
