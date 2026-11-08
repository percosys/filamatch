"""Integration test for the automatic-material palette export flow.

Exercises ``palette_bridge.dispatch('export_3mf', ...)`` with fake
``materials_workflow`` and ``exporter`` modules injected into ``sys.modules``,
so the orchestration can be verified without Fusion:

1. assembly materials are auto-detected and mapped to filament slots
   (first-appearance order), with no user-provided mapping;
2. export-time colored appearances are applied before the export and restored
   afterwards (including on failure);
3. the injector receives the automatically built mapping + filament slots;
4. the palette reply carries the materials that were used;
5. injection failures report the preserved raw export path;
6. importing catalog entries reports materials + appearances back to the UI.

Run from the repository root:

    python3 tests/test_palette_export_flow.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from typing import Any, Dict, List

# Keep integration-test settings/logs out of the real Fusion palette state.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="filamatch_palette_test_")
os.environ["FILAMATCH_DATA_DIR"] = _TEST_DATA_DIR

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LIB = "Filamatch.lib"


def _install_module(name: str, module: Any) -> None:
    """Make ``from . import <name>`` inside Filamatch.lib return ``module``."""
    import Filamatch.lib as lib_package

    sys.modules["{}.{}".format(LIB, name)] = module
    setattr(lib_package, name, module)

PASSED = 0
FAILURES: List[str] = []


def check(condition: bool, label: str) -> None:
    """Record and print one PASS/FAIL check."""
    global PASSED
    if condition:
        PASSED += 1
        print("PASS {}".format(label))
    else:
        FAILURES.append(label)
        print("FAIL {}".format(label))


class FakePalette:
    """Records palette replies."""

    def __init__(self) -> None:
        self.sent: List[Any] = []

    def sendInfoToHTML(self, action: str, data: str) -> bool:
        """Record one reply."""
        self.sent.append((action, json.loads(data)))
        return True

    def last(self, action: str) -> Dict[str, Any]:
        """Return the most recent payload for ``action`` (or {})."""
        matches = [payload for name, payload in self.sent if name == action]
        return matches[-1] if matches else {}


class FakeMaterial:
    """Stand-in for materials_workflow.DetectedMaterial."""

    def __init__(self, material_id: str, slot: int, body_names: List[str]) -> None:
        self.material_id = material_id
        self.material_name = material_id
        self.display_name = material_id.replace("_", " ").title()
        self.color_hex = "#161616" if slot == 1 else "#FFFFFF"
        self.filament_type = "PLA" if slot == 1 else "PETG"
        self.density_g_cm3 = 1.24 if slot == 1 else 1.27
        self.category = self.filament_type
        self.body_names = tuple(body_names)
        self.slot = slot


class FakeExportResult:
    """Stand-in for exporter.ExportResult."""

    def __init__(self, output_path: Path, project_settings: bool = False) -> None:
        self.output_path = output_path
        self.mapped_count = 2
        self.defaulted_count = 0
        self.project_settings_written = project_settings
        self.filament_count = 2
        self.assignments = {1: 1, 3: 2}


def _install_fakes(design: Any) -> Dict[str, Any]:
    """Install fake modules and return state needed by the assertions."""
    from Filamatch.lib import palette_bridge

    state: Dict[str, Any] = {
        "applied": [],
        "restored": [],
        "export_calls": [],
        "prepare_calls": [],
        "events": [],
        "names_restored": [],
        "slot_checks": [],
        "bambu_words": [],
        "prusa_layouts": [],
        "estimates": [],
    }
    materials = [FakeMaterial("pla_black", 1, ["Body1"]), FakeMaterial("petg_white", 2, ["Body2"])]

    workflow = types.ModuleType("materials_workflow")
    workflow.collect_assembly_materials = lambda d, entries: (
        state["events"].append("collect") or list(materials)
    )
    workflow.apply_unique_export_names = lambda d: (
        state["events"].append("rename") or ["name-backup"]
    )
    workflow.restore_body_names = lambda backups: state["names_restored"].append(list(backups))
    workflow.build_mapping = lambda detected: {"Body1": 1, "Body2": 2}

    class Attributes:
        """Body1 has its own infill (body_settings attribute)."""

        @staticmethod
        def itemByName(group: str, name: str) -> Any:
            return types.SimpleNamespace(value='{"infill": 40}') if (group, name) == ("Filamatch", "print_settings") else None

    placed = [("pla_black", types.SimpleNamespace(name="Body1", nativeObject=None, attributes=Attributes())),
              ("petg_white", types.SimpleNamespace(name="Body2", nativeObject=None, attributes=types.SimpleNamespace(
                  itemByName=lambda group, name: None)))]
    workflow.iter_placed_bodies = lambda d: iter(placed)
    workflow.build_filament_slots = lambda detected: {1: "slot1", 2: "slot2"}
    workflow.find_source_appearance = lambda d, app: "source-appearance"
    workflow.apply_export_appearances = lambda d, detected, source_appearance=None: (
        state["applied"].append(True) or ["backup1", "backup2"]
    )
    workflow.restore_appearances = lambda backups: state["restored"].append(list(backups))
    workflow.import_catalog_entries = lambda d, selected, source_appearance=None: [
        types.SimpleNamespace(
            catalog_id=entry.id,
            material=types.SimpleNamespace(name=entry.id),
            appearance=types.SimpleNamespace(name="Filamatch " + entry.id),
        )
        for entry in selected
    ]

    exporter = types.ModuleType("exporter")

    class InjectionFailedError(Exception):
        def __init__(self, message: str, raw_export_path: Path) -> None:
            super().__init__(message)
            self.raw_export_path = raw_export_path

    exporter.InjectionFailedError = InjectionFailedError
    exporter.default_export_path = lambda design: Path('/tmp/filamatch_default_export.3mf')

    def export_and_inject(**kwargs):
        state["export_calls"].append(kwargs)
        if state.get("fail_injection"):
            raise InjectionFailedError("simulated injection failure", kwargs["output_path"])
        Path(kwargs["output_path"]).write_text("exported 3MF")
        return FakeExportResult(kwargs["output_path"], bool(kwargs.get("write_project_settings")))

    exporter.export_and_inject = export_and_inject

    favorites = types.ModuleType("favorites")

    def import_catalog_to_favorites(app, design, selected):
        return [
            types.SimpleNamespace(
                catalog_id=entry.id,
                design_material=types.SimpleNamespace(
                    name=entry.id, appearance=types.SimpleNamespace(name="Filamatch " + entry.id)
                ),
                favorite_material=types.SimpleNamespace(name=entry.id),
                favorite_appearance=types.SimpleNamespace(name="ABS (White)"),
                color_hex=entry.color_hex,
                replaced_existing=False,
                relinked_bodies=0,
            )
            for entry in selected
        ]

    favorites.import_catalog_to_favorites = import_catalog_to_favorites
    favorites.favorites_summary = lambda app: {"count": 1, "names": ["bambu_pla_basic_black"]}
    favorites.OLD_FAVORITE_PREFIX = "OLD - "
    favorites.replace_stale_favorites = lambda app, entries=None: (
        state["events"].append("replace_stale") or ["Bambu Lab - ABS - Azure"]
    )
    favorites.stale_swatch_favorites = lambda app, entries=None: ["OLD - Bambu Lab - ABS - Azure"]
    favorites.replace_and_list_stale = lambda app, entries=None: (
        favorites.replace_stale_favorites(app, entries),
        favorites.stale_swatch_favorites(app, entries),
    )

    slicer_profiles = types.ModuleType("slicer_profiles")
    # The fallback validates that the path exists, so use a real executable.
    slicer_profiles.find_installed_slicers = lambda: [
        types.SimpleNamespace(
            kind="orca", name="OrcaSlicer", path=sys.executable, version="2.5.0-rc"
        )
    ]
    slicer_profiles.list_vendors = lambda exe: [
        {"id": "bambu_local", "name": "Bambu Lab (installed)", "source": "bambu_local", "has_printers": True},
        {"id": "Prusa", "name": "Prusa", "source": "orca_remote", "has_printers": True},
    ]
    slicer_profiles.list_printers = lambda exe, vendor: [
        {
            "name": "Bambu Lab X1 Carbon 0.4 nozzle",
            "machine_path": "/tmp/machine.json",
            "default_process": "0.20mm Standard @BBL X1C",
            "source": "bambu_local",
        }
    ]
    slicer_profiles.list_processes = lambda exe, vendor, printer: [
        {
            "name": "0.20mm Standard @BBL X1C",
            "process_path": "/tmp/process.json",
            "default": True,
        }
    ]
    slicer_profiles.imported_profiles = lambda: {
        "machines": [],
        "processes": [],
        "filaments": [],
        "root": "/tmp/profiles",
    }
    slicer_profiles.import_profile_files = lambda paths: {
        "machines": [{"name": "Imported Machine"}],
        "processes": [],
        "filaments": [],
        "root": "/tmp/profiles",
    }

    class FakeProfileTarget:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    slicer_profiles.ProfileTarget = FakeProfileTarget

    def prepare_project(base, output, target, slicer_exe, detected):
        state["prepare_calls"].append(
            {
                "base": Path(base),
                "output": Path(output),
                "target": target,
                "slicer_exe": slicer_exe,
                "material_count": len(detected),
            }
        )
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text("prepared project")
        return types.SimpleNamespace(
            output_path=Path(output),
            printer_name=target.printer_name,
            process_name=target.process_name,
            filament_names=tuple(getattr(m, "display_name", "") for m in detected),
            slicer_version="2.5.0-rc",
            log_tail="prepared ok",
        )

    slicer_profiles.prepare_project = prepare_project

    def slicer_kind(path):
        name = Path(str(path or "")).name.lower()
        return "orca" if "orca" in name else "bambu" if "bambu" in name else "prusa" if "prusaslicer" in name else ""

    slicer_profiles.slicer_kind = slicer_kind
    slicer_profiles.orca_supports_printer = lambda name: "H3C" not in str(name)

    def estimate_print(project, slicer_exe):
        state["estimates"].append((Path(project), slicer_exe))
        return {"minutes": 43, "grams": 7.0, "filament_grams": [2.7, 4.3]}

    slicer_profiles.estimate_print = estimate_print
    slicer_profiles.use_bambu_studio_words = lambda path, process_path=None: (
        state["bambu_words"].append(Path(path)) or {"enable_arc_fitting": "0"}
    )
    slicer_profiles.slicer_name = lambda path: {"orca": "OrcaSlicer", "bambu": "Bambu Studio"}.get(
        slicer_kind(path), Path(str(path)).name
    )

    def check_slot_assignments(path, detected):
        state["slot_checks"].append({"path": Path(path), "material_count": len(detected)})
        if state.get("slot_mismatch"):
            raise RuntimeError("Filament assignments do not match Fusion: 'Body2' prints with slot 1")
        return len(detected)

    slicer_profiles.check_slot_assignments = check_slot_assignments

    openfilament = types.ModuleType("openfilament")
    openfilament.load_user_catalog = lambda path=None: []
    openfilament.dataset_summary = lambda: {
        "cached": False,
        "version": "test",
        "generated_at": "test",
        "counts": {},
    }
    openfilament.search = lambda query, material=None, limit=50: [
        types.SimpleNamespace(
            key="ofd_bambu_lab_pla_pla_basic_black",
            brand="Bambu Lab",
            material="PLA",
            filament="PLA Basic",
            variant="Black",
            color_hex="#161616",
            density_g_cm3=1.24,
            min_print_temp=190,
            max_print_temp=230,
            min_bed_temp=35,
            max_bed_temp=45,
        )
    ]
    openfilament.search_materials = lambda: ["PLA", "PETG"]
    openfilament.get_entries = lambda keys: [
        {
            "id": "ofd_bambu_lab_pla_pla_basic_black",
            "manufacturer": "Bambu Lab",
            "product_line": "PLA Basic",
            "color_name": "Black",
            "color_hex": "#161616",
            "slicer_filament_type": "PLA",
            "density_g_cm3": 1.24,
            "template_material": "ABS Plastic",
            "category": "PLA",
        }
    ]
    openfilament.append_user_catalog = lambda entries: len(entries)

    _install_module("materials_workflow", workflow)
    _install_module("exporter", exporter)
    _install_module("favorites", favorites)
    custom_library = types.ModuleType("custom_library")
    library_status = types.SimpleNamespace(
        path="/tmp/Filamatch Library.adsklib",
        exists=True,
        loaded=True,
        material_count=3,
        message="Snapshot of Favorites",
    )
    state["library_refresh_calls"] = []

    def ensure_custom_library(app, destination=None, force=True):
        state["events"].append("library")
        state["library_refresh_calls"].append({"force": force, "destination": destination})
        return library_status

    custom_library.custom_library_status = lambda app, destination=None: library_status
    custom_library.ensure_custom_library = ensure_custom_library
    custom_library.find_favorites_library = lambda: Path("/tmp/Favorites.adsklib")
    custom_library.build_library_file = lambda source, destination: Path(destination)

    _install_module("slicer_profiles", slicer_profiles)
    prusa_project = types.ModuleType("prusa_project")
    prusa_project.is_prusa_family = lambda path: "prusa" in str(path).lower() or "superslicer" in str(path).lower()
    prusa_project.to_prusa_layout = lambda path, output=None: (
        state["prusa_layouts"].append(Path(path)) or {"objects": 1, "parts": 2, "assembled": True}
    )
    _install_module("prusa_project", prusa_project)
    _install_module("openfilament", openfilament)
    _install_module("custom_library", custom_library)

    class FakeDialogResults:
        DialogOK = "DialogOK"

    class FakeCore:
        DialogResults = FakeDialogResults

    class FakeAdsk:
        core = FakeCore

    class FakeDialog:
        def __init__(self, path: str) -> None:
            self.filename = path
            self.title = ""
            self.filter = ""
            self.initialDirectory = ""
            self.initialFilename = ""

        def showSave(self):
            return FakeDialogResults.DialogOK

        def showOpen(self):
            return FakeDialogResults.DialogOK

    class FakeUserInterface:
        def __init__(self, path: str) -> None:
            self._path = path

        def createFileDialog(self):
            return FakeDialog(self._path)

    class FakeApp:
        def __init__(self, path: str) -> None:
            self.userInterface = FakeUserInterface(path)

    palette_bridge.adsk = FakeAdsk
    state["fake_app"] = FakeApp("/tmp/chosen_from_dialog.3mf")
    palette_bridge._active_design = lambda: design
    palette_bridge._active_app = lambda: state["fake_app"]
    state["palette_bridge"] = palette_bridge
    return state


def _cleanup(state: Dict[str, Any]) -> None:
    """Restore palette_bridge helpers and remove fake modules."""
    import Filamatch.lib as lib_package

    for name in ("materials_workflow", "exporter", "favorites", "slicer_profiles", "openfilament", "custom_library", "palette_bridge"):
        # Re-importing the real modules restores the genuine implementation.
        sys.modules.pop("{}.{}".format(LIB, name), None)
        if hasattr(lib_package, name):
            delattr(lib_package, name)
    state.clear()


def main() -> int:
    """Run the automatic-export integration checks."""
    state = _install_fakes(design=object())
    bridge = state["palette_bridge"]
    palette = FakePalette()

    # --- init reports categories + empty materials without a real design ----
    bridge.dispatch("init", "{}", palette)
    init_data = palette.last("initData")
    check(init_data.get("ok") is True, "init replies with initData")
    check(
        [item["name"] for item in init_data.get("categories", [])]
        == ["PLA", "PETG", "ABS", "Fiber-Reinforced"],
        "init groups the catalog by category",
    )
    check(isinstance(init_data.get("materials"), list), "init includes assembly materials")
    check(
        (init_data.get("favorites") or {}).get("count") == 1,
        "init includes the app-level Favorites summary",
    )
    check(
        isinstance(init_data.get("profiles"), dict)
        and init_data["profiles"].get("ok") is True,
        "init includes the slicer profile summary",
    )
    check(
        isinstance(init_data.get("openfilament"), dict),
        "init includes the OpenFilament cache summary",
    )
    check(
        (init_data.get("custom_library") or {}).get("material_count") == 3,
        "init includes the custom library summary",
    )
    library_calls_after_init = len(state["library_refresh_calls"])

    # --- import_selected creates design materials + colored appearances -----
    bridge.dispatch("import_selected", json.dumps({"ids": ["bambu_pla_basic_black"]}), palette)
    import_result = palette.last("importResult")
    check(import_result.get("ok") is True, "import_selected succeeds")
    check(
        len(state["library_refresh_calls"]) > library_calls_after_init
        and import_result.get("custom_library", {}).get("material_count") == 3,
        "import refreshes the custom library when auto-update is enabled",
    )
    imported_row = (import_result.get("imported") or [{}])[0]
    check(
        import_result.get("imported")
        and imported_row.get("appearance_name") == "Filamatch bambu_pla_basic_black"
        and imported_row.get("favorite_name") == "bambu_pla_basic_black"
        and imported_row.get("color_hex") == "#161616",
        "import result reports the Favorites and design material colors",
    )

    # --- automatic export: no mapping supplied by the UI --------------------
    out_path = Path("/tmp/filamatch_auto_export.3mf")
    bridge.dispatch(
        "export_3mf",
        json.dumps(
            {
                "output_path": str(out_path),
                "write_project_settings": True,
                "orca": {"enabled": False},
            }
        ),
        palette,
    )
    export_result = palette.last("exportResult")
    check(export_result.get("ok") is True, "automatic export replies ok")
    check(state["applied"], "colored export appearances are applied before exporting")
    check(
        state["restored"] and state["restored"][-1] == ["backup1", "backup2"],
        "original appearances are restored after export",
    )
    check(
        state["events"][-2:] == ["rename", "collect"],
        "duplicate body names are made unique before materials are collected",
    )
    check(
        state["names_restored"] == [["name-backup"]],
        "original body names are restored exactly once after export",
    )
    call = state["export_calls"][-1] if state["export_calls"] else {}
    check(
        call.get("mapping") == {"Body1": 1, "Body2": 2},
        "exporter receives the automatically detected object->slot mapping",
    )
    check(
        call.get("filaments") is None
        and call.get("write_project_settings") is False,
        "base export omits the partial project_settings.config",
    )
    used = export_result.get("materials_used") or []
    check(
        [item["slot"] for item in used] == [1, 2]
        and len(used) == 2
        and export_result.get("project_settings_written") is False
        and export_result.get("prepared") is False
        and bool(export_result.get("warning")),
        "unprepared export lists materials and warns that no printer profile was applied",
    )
    check(
        state["slot_checks"] == [{"path": out_path, "material_count": 2}],
        "the unprepared 3MF is checked against the Fusion slots",
    )
    state["slot_mismatch"] = True
    real_launch = bridge._launch_orca
    bridge._launch_orca = lambda executable, path: state.setdefault("launched", []).append(path) or True
    palette.sent.clear()
    bridge.dispatch(
        "export_3mf",
        json.dumps({"output_path": str(out_path), "orca": {"enabled": True, "executable": "/fake/OrcaSlicer"}}),
        palette,
    )
    mismatch = palette.last("exportResult")
    check(
        mismatch.get("ok") is False
        and "do not match Fusion" in str(mismatch.get("error"))
        and not state.get("launched"),
        "a slot mismatch stops the export before the slicer opens",
    )
    bridge._launch_orca = real_launch
    state["slot_mismatch"] = False

    # --- target printer preparation -----------------------------------------
    prepared_path = Path("/tmp/filamatch_auto_prepared.3mf")
    palette.sent.clear()
    bridge.dispatch(
        "export_3mf",
        json.dumps(
            {
                "output_path": str(prepared_path),
                "orca": {"enabled": False},
                "target": {
                    "prepare": True,
                    "slicer_path": "/fake/OrcaSlicer",
                    "vendor": "bambu_local",
                    "printer": "Bambu Lab X1 Carbon 0.4 nozzle",
                    "process": "0.20mm Standard @BBL X1C",
                },
            }
        ),
        palette,
    )
    prepared_result = palette.last("exportResult")
    prepare_call = state["prepare_calls"][-1] if state["prepare_calls"] else {}
    check(
        prepared_result.get("ok") is True
        and prepared_result.get("prepared") is True
        and prepared_result.get("printer_name") == "Bambu Lab X1 Carbon 0.4 nozzle"
        and prepared_result.get("process_name") == "0.20mm Standard @BBL X1C",
        "prepared export reports the target printer/process",
    )
    check(
        prepare_call.get("base") == prepared_path.with_name(prepared_path.stem + ".base.3mf")
        and prepare_call.get("output") == prepared_path
        and prepare_call.get("target").vendor == "bambu_local"
        and prepare_call.get("material_count") == 2,
        "slicer preparation receives the base 3MF, target and materials",
    )
    base_call = state["export_calls"][-1]
    check(
        str(base_call.get("output_path", "")).endswith(".base.3mf"),
        "base export before preparation omits project_settings and uses a temp name",
    )
    check(
        base_call.get("object_settings") == {"Body1": {"sparse_infill_density": "40%"}},
        "a body's own print settings go to the export by body name: {}".format(base_call.get("object_settings")),
    )
    saved = prepared_result.get("settings") or {}
    check(
        saved.get("target_vendor") == "bambu_local"
        and saved.get("target_printer") == "Bambu Lab X1 Carbon 0.4 nozzle"
        and saved.get("prepare_enabled") is True,
        "prepared export persists the target settings",
    )
    estimate = palette.last("printEstimate")
    check(
        prepared_result.get("estimating") is True
        and state["estimates"][-1] == (prepared_path, prepare_call.get("slicer_exe"))
        and estimate.get("ok") is True
        and estimate.get("minutes") == 43
        and estimate.get("filament_grams") == [2.7, 4.3],
        "a prepared export slices a copy afterwards and sends the print estimate",
    )
    check(
        export_result.get("estimating") is False,
        "an unprepared export has no print estimate",
    )
    bridge.save_settings({"estimate_enabled": False})
    estimates_before = len(state["estimates"])
    palette.sent.clear()
    bridge.dispatch(
        "export_3mf",
        json.dumps(
            {
                "output_path": str(prepared_path),
                "orca": {"enabled": False},
                "target": {
                    "prepare": True,
                    "slicer_path": "/fake/OrcaSlicer",
                    "vendor": "bambu_local",
                    "printer": "Bambu Lab X1 Carbon 0.4 nozzle",
                    "process": "0.20mm Standard @BBL X1C",
                },
            }
        ),
        palette,
    )
    check(
        palette.last("exportResult").get("estimating") is False
        and len(state["estimates"]) == estimates_before
        and not palette.last("printEstimate"),
        "with the estimate turned off in Settings, nothing is sliced in the background",
    )
    bridge.save_settings({"estimate_enabled": True})

    # --- the chosen slicer: OrcaSlicer prepares, the chosen app opens -------
    apps = Path(tempfile.mkdtemp(prefix="filamatch_apps_"))
    bambu_app, prusa_app, orca_app = apps / "BambuStudio", apps / "PrusaSlicer", apps / "OrcaSlicer"
    super_app = apps / "SuperSlicer"
    bambu_app.write_text("fake Bambu Studio")
    prusa_app.write_text("fake PrusaSlicer")
    orca_app.write_text("fake OrcaSlicer")
    super_app.write_text("fake SuperSlicer")

    def send_to(app: Path, vendor: str = "bambu_local",
                printer: str = "Bambu Lab X1 Carbon 0.4 nozzle", prepare: bool = True) -> Dict[str, Any]:
        palette.sent.clear()
        bridge.dispatch(
            "export_3mf",
            json.dumps(
                {
                    "output_path": str(prepared_path),
                    "orca": {"enabled": False, "executable": str(app)},
                    "target": {
                        "prepare": prepare,
                        "slicer_path": str(app),
                        "vendor": vendor,
                        "printer": printer,
                        "process": "0.20mm Standard @BBL X1C",
                    },
                }
            ),
            palette,
        )
        return palette.last("exportResult")

    bambu_result = send_to(bambu_app)
    check(
        bambu_result.get("ok") is True
        and bambu_result.get("prepared") is True
        and state["prepare_calls"][-1]["slicer_exe"] == str(bambu_app)
        and (bambu_result.get("settings") or {}).get("slicer_path") == str(bambu_app)
        and state["bambu_words"] == [],
        "Send to Bambu with a Bambu Lab printer: Bambu Studio prepares by itself",
    )
    other_printer = send_to(bambu_app, vendor="Prusa")
    check(
        other_printer.get("ok") is True
        and state["prepare_calls"][-1]["slicer_exe"] == sys.executable
        and state["bambu_words"] == [prepared_path]
        and (other_printer.get("settings") or {}).get("slicer_path") == str(bambu_app),
        "Send to Bambu with another printer: OrcaSlicer prepares and Bambu Studio's words are written",
    )
    prusa_result = send_to(prusa_app, vendor="prusa:PrusaResearch", printer="Original Prusa XL - 5T Input Shaper 0.4 nozzle")
    check(
        prusa_result.get("ok") is True
        and prusa_result.get("prepared") is True
        and state["prepare_calls"][-1]["slicer_exe"] == str(prusa_app)
        and state["prusa_layouts"] == [],
        "Send to PrusaSlicer: PrusaSlicer prepares its own project (printer, print and filament settings)",
    )
    prepare_count = len(state["prepare_calls"])
    plain_prusa = send_to(prusa_app, vendor="prusa:PrusaResearch", prepare=False)
    super_result = send_to(super_app)
    check(
        plain_prusa.get("ok") is True and plain_prusa.get("prepared") is False
        and super_result.get("ok") is True and super_result.get("prepared") is False
        and len(state["prepare_calls"]) == prepare_count
        and prepared_path.read_text() == "exported 3MF"
        and not prepared_path.with_name(prepared_path.stem + ".base.3mf").exists(),
        "PrusaSlicer without preparing, and SuperSlicer (cannot prepare): the plain 3MF",
    )
    check(
        state["prusa_layouts"] == [prepared_path, prepared_path]
        and any("PrusaSlicer layout: 1 object(s) with 2 part(s)" in str(line) for line in palette.sent),
        "the plain 3MF for PrusaSlicer and SuperSlicer is in their layout, so each body keeps its extruder",
    )
    fake_slicers = sys.modules["{}.slicer_profiles".format(LIB)]
    installed = fake_slicers.find_installed_slicers
    fake_slicers.find_installed_slicers = lambda: []
    try:
        no_orca = send_to(bambu_app)
        no_orca_other = send_to(bambu_app, vendor="Prusa")
    finally:
        fake_slicers.find_installed_slicers = installed
    check(
        no_orca.get("ok") is True and state["prepare_calls"][-1]["slicer_exe"] == str(bambu_app),
        "Send to Bambu needs no OrcaSlicer for a Bambu Lab printer",
    )
    check(
        no_orca_other.get("ok") is False and "needs OrcaSlicer" in str(no_orca_other.get("error")),
        "Send to Bambu with another printer and no OrcaSlicer explains why",
    )
    listed = fake_slicers.list_printers
    fake_slicers.list_printers = lambda exe, vendor: [
        {"name": "Bambu Lab H3C 0.4 nozzle", "machine_path": "/tmp/h3c.json", "source": "bambu_local"}
    ]
    try:
        prepare_count = len(state["prepare_calls"])
        h3c_orca = send_to(orca_app, printer="Bambu Lab H3C 0.4 nozzle")
        h3c_bambu = send_to(bambu_app, printer="Bambu Lab H3C 0.4 nozzle")
    finally:
        fake_slicers.list_printers = listed
    check(
        h3c_orca.get("ok") is False and "OrcaSlicer cannot slice the Bambu Lab H3C" in str(h3c_orca.get("error"))
        and h3c_bambu.get("ok") is True and len(state["prepare_calls"]) == prepare_count + 1
        and state["prepare_calls"][-1]["slicer_exe"] == str(bambu_app),
        "Bambu Lab H3C: OrcaSlicer is refused with a reason, Bambu Studio prepares",
    )
    shutil.rmtree(apps, ignore_errors=True)

    # --- printer report ---------------------------------------------------------
    fake_report = types.ModuleType("printer_report")
    fake_report.collect = lambda settings: {"printers": [{"model": "H2D"}], "source": "bambu_lan"}
    fake_report.save = lambda report: Path("/tmp/printer-report-H2D-20261108-101500.json")
    _install_module("printer_report", fake_report)
    palette.sent.clear()
    bridge.dispatch("save_printer_report", "{}", palette)
    report_reply = palette.last("printerReportResult")
    check(
        report_reply.get("ok") is True and report_reply.get("printers") == 1
        and report_reply.get("path", "").endswith("printer-report-H2D-20261108-101500.json"),
        "Save printer report answers with the saved file",
    )
    fake_report.collect = lambda settings: (_ for _ in ()).throw(RuntimeError("No printer is connected."))
    bridge.dispatch("save_printer_report", "{}", palette)
    check(
        palette.last("printerReportResult").get("ok") is False
        and "No printer is connected" in str(palette.last("printerReportResult").get("error")),
        "a failed printer report says why",
    )

    # --- profiles and Open Filament Database handlers ------------------------
    palette.sent.clear()
    bridge.dispatch("get_profiles", json.dumps({"vendor": "bambu_local"}), palette)
    profiles_data = palette.last("profilesData")
    check(
        profiles_data.get("ok") is True
        and len(profiles_data.get("vendors") or []) == 2
        and len(profiles_data.get("printers") or []) == 1
        and profiles_data.get("processes")[0].get("default") is True,
        "get_profiles lists vendors, printers and default process",
    )
    palette.sent.clear()
    bridge.dispatch("import_profiles", "{}", palette)
    imported_profiles = palette.last("profilesImported")
    check(
        imported_profiles.get("ok") is True
        and (imported_profiles.get("imported") or {}).get("machines"),
        "import_profiles returns the imported preset summary",
    )
    palette.sent.clear()
    bridge.dispatch("openfilament_search", json.dumps({"query": "pla black"}), palette)
    ofd_data = palette.last("openFilamentData")
    check(
        ofd_data.get("ok") is True
        and len(ofd_data.get("results") or []) == 1
        and ofd_data["results"][0]["color_hex"] == "#161616",
        "openfilament_search returns ranked online filament results",
    )
    palette.sent.clear()
    bridge.dispatch(
        "openfilament_import",
        json.dumps({"keys": ["ofd_bambu_lab_pla_pla_basic_black"]}),
        palette,
    )
    ofd_import = palette.last("openFilamentImportResult")
    check(
        ofd_import.get("ok") is True
        and ofd_import.get("imported")[0]["id"] == "ofd_bambu_lab_pla_pla_basic_black"
        and ofd_import.get("user_catalog_added") == 1,
        "openfilament_import adds the online filament to the design/Favorites/user catalog",
    )

    # --- create custom library action ---------------------------------------
    palette.sent.clear()
    bridge.dispatch("create_library", "{}", palette)
    library_result = palette.last("libraryCreated")
    check(
        library_result.get("ok") is True
        and (library_result.get("custom_library") or {}).get("material_count") == 3,
        "create_library refreshes and reports the custom library",
    )
    check(
        state["events"][-2:] == ["replace_stale", "library"],
        "old favorites are replaced before the library is rebuilt",
    )
    check(
        "OLD - Bambu Lab - ABS - Azure" in (library_result.get("custom_library") or {}).get("message", ""),
        "the library message lists the OLD favorites to remove by hand",
    )

    # --- injection failure: raw export path reported + appearances restored -
    state["fail_injection"] = True
    palette.sent.clear()
    bridge.dispatch(
        "export_3mf",
        json.dumps({"output_path": str(out_path), "write_project_settings": True}),
        palette,
    )
    failure = palette.last("exportResult")
    check(failure.get("ok") is False, "injection failure is reported to the palette")
    check(
        failure.get("raw_export_path") == str(out_path),
        "injection failure reports the preserved raw export path",
    )
    check(
        state["restored"] and state["restored"][-1] == ["backup1", "backup2"],
        "appearances are restored when injection fails",
    )
    check(
        state["names_restored"] and state["names_restored"][-1] == ["name-backup"],
        "body names are restored when injection fails",
    )

    # --- native Browse path dialogs ------------------------------------------
    palette.sent.clear()
    bridge.dispatch("browse_export_path", json.dumps({"current_path": ""}), palette)
    path_data = palette.last("exportPathData")
    check(
        path_data.get("ok") is True and path_data.get("path") == "/tmp/chosen_from_dialog.3mf",
        "browse_export_path returns the native save-dialog path",
    )
    palette.sent.clear()
    bridge.dispatch("browse_slicer_path", json.dumps({"current_path": ""}), palette)
    slicer_data = palette.last("slicerPathData")
    check(
        slicer_data.get("ok") is True and slicer_data.get("path") == "/tmp/chosen_from_dialog.3mf",
        "browse_slicer_path returns the native open-dialog path",
    )

    # --- refresh_materials ---------------------------------------------------
    palette.sent.clear()
    bridge.dispatch("refresh_materials", "{}", palette)
    materials_data = palette.last("materialsData")
    check(
        materials_data.get("ok") is True and len(materials_data.get("materials", [])) == 2,
        "refresh_materials returns the detected assembly materials",
    )

    # --- response echo guard --------------------------------------------------
    palette.sent.clear()
    bridge.dispatch("response", '{"value": true}', palette)
    check(palette.sent == [], "response messages never produce a reply (no feedback loop)")

    _cleanup(state)
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
    os.environ.pop("FILAMATCH_DATA_DIR", None)
    print()
    if FAILURES:
        print("AUTO-EXPORT FLOW FAILED ({} passed, {} failed)".format(PASSED, len(FAILURES)))
        for label in FAILURES:
            print(" -", label)
        return 1
    print("AUTO-EXPORT FLOW PASSED ({} checks)".format(PASSED))
    return 0


if __name__ == "__main__":
    sys.exit(main())
