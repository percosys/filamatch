"""Self-test of :mod:`slicer_profiles`: fake slicers, a fake network and temporary folders.

Run ``python3 -m Filamatch.lib.slicer_profiles`` (or this module) from the
repository root.  The checks use the module's names directly, private ones
included; the hooks they swap (the command runner, the URL opener and the
folder overrides) are set on :mod:`slicer_profiles` itself, so the code under
test sees them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from . import filament_presets
from . import slicer_profiles as _module

globals().update({name: value for name, value in vars(_module).items() if not name.startswith("__")})


def _reset_remote_state() -> None:
    """Forget the session's negative network caches (self-test helper)."""
    _MISSING_URLS.clear()
    _MISSING_REFS.clear()
    _GOOD_REFS.clear()


class _FakeCompletedProcess:
    """Minimal ``subprocess.CompletedProcess`` stand-in for the self-test."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeRunner:
    """Replaceable command runner: answers ``--help`` and delegates CLI calls."""

    def __init__(self, help_text: str = "OrcaSlicer-2.5.0-rc") -> None:
        self.help_text = help_text
        self.commands: List[List[str]] = []
        self.cli_handler: Callable[[List[str], "_FakeRunner"], _FakeCompletedProcess] = (
            lambda command, runner: _FakeCompletedProcess()
        )

    def run(
        self, command: Sequence[str], timeout: float, cwd: Optional[str] = None
    ) -> _FakeCompletedProcess:
        """Record the command and dispatch help vs CLI calls."""
        recorded = list(command)
        self.commands.append(recorded)
        if len(recorded) >= 2 and recorded[1] == "--help":
            return _FakeCompletedProcess(0, self.help_text + "\n", "")
        return self.cli_handler(recorded, self)

    @property
    def cli_commands(self) -> List[List[str]]:
        """Return only the non-help commands that were run."""
        return [
            command
            for command in self.commands
            if not (len(command) >= 2 and command[1] == "--help")
        ]


def _flag_value(command: Sequence[str], flag: str) -> Optional[str]:
    """Return the value following ``flag`` in a command list."""
    for index, part in enumerate(command):
        if part == flag and index + 1 < len(command):
            return str(command[index + 1])
    return None


def _write_fixture_3mf(
    path: Path,
    extra_entries: Optional[Sequence[Tuple[str, bytes]]] = None,
    include_project_settings: bool = False,
    project_settings: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write a small Fusion-like 3MF fixture for the self-test."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entries: List[Tuple[str, bytes]] = [
        ("[Content_Types].xml", b"<?xml version='1.0'?><Types/>"),
        ("_rels/.rels", b"<?xml version='1.0'?><Relationships/>"),
        ("3D/3dmodel.model", b"<model><resources/><build/></model>"),
        ("Metadata/model_settings.config", b"<config><object id='2'/></config>"),
        ("Metadata/thumbnail.png", b"\x89PNG\r\n\x1a\nfixture"),
    ]
    if include_project_settings:
        settings = project_settings if project_settings is not None else {"stale": True}
        entries.append(
            (_PROJECT_SETTINGS_PART, json.dumps(settings).encode("utf-8"))
        )
    entries.extend(extra_entries or [])
    with zipfile.ZipFile(str(path), "w") as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return path


def _read_archive_entry(path: Path, entry_name: str) -> bytes:
    """Return one archive entry's bytes."""
    with zipfile.ZipFile(str(path), "r") as archive:
        return archive.read(entry_name)


def _archive_names(path: Path) -> List[str]:
    """Return archive member names in order."""
    with zipfile.ZipFile(str(path), "r") as archive:
        return archive.namelist()


def _fixture_materials() -> List[Any]:
    """Return detection-like material objects for the self-test."""
    return [
        {
            "color_hex": "#161616",
            "filament_type": "PLA",
            "density_g_cm3": 1.24,
            "display_name": "Bambu Lab PLA Basic - Black",
        },
        {
            "color_hex": "#FFFFFF",
            "filament_type": "PETG",
            "density_g_cm3": 1.27,
            "display_name": "Bambu Lab PETG HF - White",
        },
        {
            "color_hex": "#111111",
            "filament_type": "ABS",
            "density_g_cm3": 1.04,
            "display_name": "Polymaker PolyLite ABS - Black",
        },
    ]


def _write_profile_json(path: Path, data: Dict[str, Any]) -> Path:
    """Write a preset JSON fixture file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


class _TestEnvironment:
    """Owns the temporary fixtures, fake runner and fake network for the checks."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.cache_root = root / "cache"
        self.bambu_system = root / "bambu_system"
        self.slicer_exe = root / "OrcaSlicer"
        self.slicer_exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.runner = _FakeRunner()
        self.responses: Dict[str, bytes] = {}
        self.opener_calls: List[str] = []
        self._previous: Optional[Tuple[Any, ...]] = None
        self.materials = _fixture_materials()
        self._build_local_profiles()
        self._build_remote_responses()

    def install(self) -> None:
        """Install the fake hooks into the module under test."""
        self._previous = (
            _module._CACHE_ROOT_OVERRIDE,
            _module._BAMBU_SYSTEM_DIR_OVERRIDE,
            _module._SLICER_CANDIDATES_OVERRIDE,
            _module._command_runner,
            _module._url_opener,
            dict(_VERSION_CACHE),
            (set(_MISSING_URLS), set(_MISSING_REFS), set(_GOOD_REFS)),
            filament_presets._CONF_ROOT_OVERRIDE,
        )
        _reset_remote_state()
        # The slicers' user presets come from the fixture folder, never this computer.
        filament_presets._CONF_ROOT_OVERRIDE = self.root / "slicer_support"
        _module._CACHE_ROOT_OVERRIDE = self.cache_root
        _module._BAMBU_SYSTEM_DIR_OVERRIDE = self.bambu_system
        _module._SLICER_CANDIDATES_OVERRIDE = [
            ("orca", "OrcaSlicer", self.slicer_exe),
        ]
        _module._command_runner = self.runner.run
        _module._url_opener = self.open_url
        _VERSION_CACHE.clear()

    def restore(self) -> None:
        """Restore the original module hooks."""
        if self._previous is None:
            return
        (
            _module._CACHE_ROOT_OVERRIDE,
            _module._BAMBU_SYSTEM_DIR_OVERRIDE,
            _module._SLICER_CANDIDATES_OVERRIDE,
            _module._command_runner,
            _module._url_opener,
            version_cache,
            remote_state,
            filament_presets._CONF_ROOT_OVERRIDE,
        ) = self._previous
        _VERSION_CACHE.clear()
        _VERSION_CACHE.update(version_cache)
        _reset_remote_state()
        _MISSING_URLS.update(remote_state[0])
        _MISSING_REFS.update(remote_state[1])
        _GOOD_REFS.update(remote_state[2])

    def open_url(self, url: str, timeout: float) -> bytes:
        """Fake network opener backed by ``responses``; unknown URLs 404."""
        self.opener_calls.append(url)
        payload = self.responses.get(url)
        if payload is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)  # type: ignore[arg-type]
        return payload

    # -- fixtures ---------------------------------------------------------

    def _build_local_profiles(self) -> None:
        """Create the temporary Bambu Studio system profile tree."""
        machine_dir = self.bambu_system / "machine"
        process_dir = self.bambu_system / "process"
        filament_dir = self.bambu_system / "filament"
        _write_profile_json(
            machine_dir / "Bambu Lab X1 Carbon 0.4 nozzle.json",
            {
                "name": "Bambu Lab X1 Carbon 0.4 nozzle",
                "type": "machine",
                "default_print_profile": "0.20mm Standard @BBL X1C",
                "importable": True,
            },
        )
        _write_profile_json(
            machine_dir / "fdm_machine_common.json",
            {"name": "fdm_machine_common", "type": "machine"},
        )
        _write_profile_json(
            machine_dir / "template_machine.json",
            {"name": "template machine", "type": "machine"},
        )
        _write_profile_json(
            machine_dir / "not_importable.json",
            {"name": "Bambu Lab Not Importable", "type": "machine", "importable": False},
        )
        _write_profile_json(
            process_dir / "0.20mm Standard @BBL X1C.json",
            {"name": "0.20mm Standard @BBL X1C", "type": "process"},
        )
        _write_profile_json(
            process_dir / "0.24mm Draft @BBL X1C.json",
            {"name": "0.24mm Draft @BBL X1C", "type": "process"},
        )
        _write_profile_json(
            process_dir / "fdm_process_common.json",
            {"name": "fdm_process_common", "type": "process"},
        )
        _write_profile_json(
            process_dir / "common_process.json",
            {"name": "Common Process", "type": "process"},
        )
        _write_profile_json(
            filament_dir / "Bambu PLA Basic @BBL X1C.json",
            {
                "name": "Bambu PLA Basic @BBL X1C",
                "type": "filament",
                "filament_type": "PLA",
            },
        )
        _write_profile_json(
            filament_dir / "Bambu PETG HF @BBL X1C.json",
            {
                "name": "Bambu PETG HF @BBL X1C",
                "type": "filament",
                "filament_type": "PETG",
            },
        )
        _write_profile_json(
            filament_dir / "Bambu ABS @BBL X1C.json",
            {
                "name": "Bambu ABS @BBL X1C",
                "type": "filament",
                "filament_type": "ABS",
            },
        )
        _write_profile_json(
            filament_dir / "fdm_filament_pla.json",
            {"name": "fdm_filament_pla", "type": "filament"},
        )

    def _remote_url(self, ref: str, relative_path: str) -> str:
        """Build a fake repository URL for fixtures (same encoding as production)."""
        clean = "/".join(
            part for part in str(relative_path).replace("\\", "/").split("/") if part not in ("", ".")
        )
        encoded = "/".join(quote(part, safe="") for part in clean.split("/"))
        return REMOTE_PROFILES_BASE.format(ref=ref, relative_path=encoded)

    def _add_remote_json(self, ref: str, relative_path: str, data: Any) -> None:
        """Register a fake remote JSON payload."""
        self.responses[self._remote_url(ref, relative_path)] = json.dumps(data).encode(
            "utf-8"
        )

    def _build_remote_responses(self) -> None:
        """Create the fake OrcaSlicer repository (vendor manifests and presets)."""
        ref = "v2.5.0-rc"
        self._add_remote_json(
            ref,
            "BBL.json",
            {
                "name": "Bambu Lab",
                "machine_list": [
                    {
                        "name": "Bambu Lab X1 Carbon 0.4 nozzle",
                        "sub_path": "BBL/machine/X1 Carbon 0.4 nozzle.json",
                    },
                    {
                        "name": "Bambu Lab A1 0.4 nozzle",
                        "sub_path": "BBL/machine/A1 0.4 nozzle.json",
                    },
                    {
                        "name": "fdm_machine_common",
                        "sub_path": "BBL/machine/fdm_machine_common.json",
                    },
                ],
                "process_list": [
                    {
                        "name": "0.20mm Standard @BBL X1C",
                        "sub_path": "BBL/process/0.20mm Standard @BBL X1C.json",
                    },
                    {
                        "name": "0.24mm Draft @BBL X1C",
                        "sub_path": "BBL/process/0.24mm Draft @BBL X1C.json",
                    },
                    {
                        "name": "fdm_process_common",
                        "sub_path": "BBL/process/fdm_process_common.json",
                    },
                ],
                "filament_list": [
                    {
                        "name": "Bambu PLA Basic @BBL X1C",
                        "sub_path": "BBL/filament/Bambu PLA Basic @BBL X1C.json",
                    },
                ],
            },
        )
        self._add_remote_json(
            ref,
            "OrcaFilamentLibrary.json",
            {
                "name": "Orca Filament Library",
                "filament_list": [
                    {
                        "name": "fdm_filament_pla",
                        "sub_path": "OrcaFilamentLibrary/filament/fdm_filament_pla.json",
                    },
                ],
            },
        )
        self._add_remote_json(
            ref,
            "BBL/machine/X1 Carbon 0.4 nozzle.json",
            {
                "name": "Bambu Lab X1 Carbon 0.4 nozzle",
                "type": "machine",
                "inherits": "fdm_machine_common",
                "default_print_profile": "0.20mm Standard @BBL X1C",
            },
        )
        self._add_remote_json(
            ref,
            "BBL/machine/fdm_machine_common.json",
            {
                "name": "fdm_machine_common",
                "type": "machine",
                "machine_max_acceleration_x": ["1000", "1000"],
                "from": "system",
            },
        )
        self._add_remote_json(
            ref,
            "BBL/process/0.20mm Standard @BBL X1C.json",
            {
                "name": "0.20mm Standard @BBL X1C",
                "type": "process",
                "inherits": "fdm_process_common",
                "layer_height": "0.2",
            },
        )
        self._add_remote_json(
            ref,
            "BBL/process/fdm_process_common.json",
            {
                "name": "fdm_process_common",
                "type": "process",
                "outer_wall_speed": ["200"],
            },
        )
        # Prusa is only available on the main branch: exercises the tag fallback.
        self._add_remote_json(
            "main",
            "Prusa.json",
            {
                "name": "Prusa",
                "machine_list": [
                    {
                        "name": "Prusa MK4 0.4 nozzle",
                        "sub_path": "Prusa/machine/Prusa MK4 0.4 nozzle.json",
                    }
                ],
                "process_list": [],
                "filament_list": [
                    {"name": "Generic PLA @base", "sub_path": "filament/Generic PLA @base.json"},
                    {"name": "Generic PLA @Prusa MK4", "sub_path": "filament/Generic PLA @Prusa MK4.json"},
                    {"name": "Generic PLA @Prusa XL", "sub_path": "filament/Generic PLA @Prusa XL.json"},
                ],
            },
        )
        self._add_remote_json(
            "main",
            "Prusa/filament/Generic PLA @base.json",
            {"name": "Generic PLA @base", "type": "filament", "filament_type": ["PLA"],
             "nozzle_temperature": ["215"], "instantiation": "false"},
        )
        for printer in ("MK4", "XL"):
            self._add_remote_json(
                "main",
                "Prusa/filament/Generic PLA @Prusa {}.json".format(printer),
                {"name": "Generic PLA @Prusa {}".format(printer), "type": "filament",
                 "inherits": "Generic PLA @base", "instantiation": "true",
                 "compatible_printers": ["Prusa {} 0.4 nozzle".format(printer)]},
            )
        self._add_remote_json(
            "main",
            "OrcaFilamentLibrary.json",
            {"name": "OrcaFilamentLibrary", "filament_list": [
                {"name": "Generic PETG @System", "sub_path": "filament/Generic PETG @System.json"},
            ]},
        )
        self._add_remote_json(
            "main",
            "OrcaFilamentLibrary/filament/Generic PETG @System.json",
            {"name": "Generic PETG @System", "type": "filament", "filament_type": ["PETG"],
             "nozzle_temperature": ["240"], "instantiation": "true"},
        )


def _check_find_installed_slicers(environment: _TestEnvironment) -> None:
    """Installed slicers are discovered and their versions parsed from --help."""
    installs = find_installed_slicers()
    _expect(len(installs) == 1, "expected exactly one fake installed slicer")
    install = installs[0]
    _expect(install.kind == "orca", "install kind must be orca")
    _expect(install.name == "OrcaSlicer", "install name mismatch")
    _expect(install.path == str(environment.slicer_exe), "install path mismatch")
    _expect(install.version == "2.5.0-rc", "version must be parsed from --help")

    environment.runner.help_text = "BambuStudio-02.07.01.62"
    _VERSION_CACHE.clear()
    _module._SLICER_CANDIDATES_OVERRIDE = [("bambu", "BambuStudio", environment.slicer_exe)]
    try:
        bambu_installs = find_installed_slicers()
    finally:
        _module._SLICER_CANDIDATES_OVERRIDE = [
            ("orca", "OrcaSlicer", environment.slicer_exe)
        ]
    _expect(
        bambu_installs[0].version == "02.07.01.62",
        "Bambu Studio version must be parsed",
    )
    environment.runner.help_text = "OrcaSlicer-2.5.0-rc"
    _VERSION_CACHE.clear()


def _check_local_discovery(environment: _TestEnvironment) -> None:
    """Local vendor/printer/process discovery filters base and template presets."""
    vendors = list_vendors(environment.slicer_exe)
    local_vendors = [vendor for vendor in vendors if vendor["source"] == "bambu_local"]
    _expect(len(local_vendors) == 1, "a local Bambu Studio vendor must be listed")
    _expect(local_vendors[0]["id"] == "bambu_local", "local vendor id mismatch")
    _expect(local_vendors[0]["has_printers"] is True, "local vendor must have printers")

    printers = list_printers(environment.slicer_exe, "bambu_local")
    names = [printer["name"] for printer in printers]
    _expect(
        names == ["Bambu Lab X1 Carbon 0.4 nozzle"],
        "only the instantiable local machine preset must be listed, got {}".format(names),
    )
    _expect(
        printers[0]["default_process"] == "0.20mm Standard @BBL X1C",
        "the machine default process must be reported",
    )

    processes = list_processes(
        environment.slicer_exe, "bambu_local", "Bambu Lab X1 Carbon 0.4 nozzle"
    )
    process_names = [process["name"] for process in processes]
    _expect(
        sorted(process_names) == ["0.20mm Standard @BBL X1C", "0.24mm Draft @BBL X1C"],
        "base/common process presets must be filtered, got {}".format(process_names),
    )
    defaults = [process for process in processes if process["default"]]
    _expect(len(defaults) == 1, "exactly one default process must be marked")
    _expect(
        defaults[0]["name"] == "0.20mm Standard @BBL X1C",
        "the machine default_print_profile must be marked as default",
    )


def _flatten_for_test(vendor_id: str, sub_path: str, slicer_exe: Any) -> Dict[str, Any]:
    """Load the vendor manifest and flatten one remote preset (self-test helper)."""
    _manifest, ref = _load_vendor_manifest(vendor_id, slicer_exe)
    name_map = _build_remote_name_map(vendor_id, slicer_exe, ref)
    return _flatten_remote_preset_at(vendor_id, sub_path, ref, name_map)


def _check_remote_listing_and_flattening(environment: _TestEnvironment) -> None:
    """Remote manifests list printers/processes and inheritance is flattened."""
    vendors = list_vendors(environment.slicer_exe)
    by_id = {vendor["id"]: vendor for vendor in vendors}
    _expect(by_id["BBL"]["has_printers"] is True, "BBL must be listed with printers")
    _expect(by_id["Prusa"]["has_printers"] is True, "Prusa main-branch fallback must work")
    _expect(by_id["Creality"]["has_printers"] is False, "unreachable vendors must be listed")
    _expect(by_id["imported"]["source"] == "imported", "imported vendor must be listed")

    printers = list_printers(environment.slicer_exe, "BBL")
    printer_names = [printer["name"] for printer in printers]
    _expect(
        printer_names
        == ["Bambu Lab X1 Carbon 0.4 nozzle", "Bambu Lab A1 0.4 nozzle"],
        "remote machine_list must be filtered by fdm_ prefix, got {}".format(printer_names),
    )
    _expect(
        printers[0]["machine_path"].endswith(
            "profile_cache/v2.5.0-rc/BBL/machine/X1 Carbon 0.4 nozzle.json"
        ),
        "remote machine_path must be a tag-matched cache path, got {}".format(
            printers[0]["machine_path"]
        ),
    )

    processes = list_processes(
        environment.slicer_exe, "BBL", "Bambu Lab X1 Carbon 0.4 nozzle"
    )
    process_names = [process["name"] for process in processes]
    _expect(
        process_names == ["0.20mm Standard @BBL X1C", "0.24mm Draft @BBL X1C"],
        "remote process_list must filter fdm_/common presets, got {}".format(
            process_names
        ),
    )
    defaults = [process for process in processes if process["default"]]
    _expect(
        len(defaults) == 1 and defaults[0]["name"] == "0.20mm Standard @BBL X1C",
        "remote default process must come from the flattened machine preset",
    )

    manifest = _load_vendor_manifest("BBL", environment.slicer_exe)[0]
    machine_entry = next(
        entry
        for entry in _manifest_entries(manifest, "machine_list")
        if entry["name"] == "Bambu Lab X1 Carbon 0.4 nozzle"
    )
    flattened = _flatten_for_test(
        "BBL", machine_entry["sub_path"], environment.slicer_exe
    )
    _expect("inherits" not in flattened, "flattening must remove 'inherits'")
    _expect(flattened.get("from") == "user", "flattened presets must be from=user")
    _expect(
        flattened.get("machine_max_acceleration_x") == ["1000", "1000"],
        "flattening must merge parent keys",
    )
    _expect(
        flattened.get("default_print_profile") == "0.20mm Standard @BBL X1C",
        "flattening must keep child keys",
    )
    flattened_process = _flatten_for_test(
        "BBL", "BBL/process/0.20mm Standard @BBL X1C.json", environment.slicer_exe
    )
    _expect(
        flattened_process.get("outer_wall_speed") == ["200"],
        "process flattening must merge the parent keys",
    )
    _expect(
        flattened_process.get("layer_height") == "0.2",
        "process child keys must survive",
    )


def _check_unresolved_parent_error(environment: _TestEnvironment) -> None:
    """An unresolvable inheritance parent raises with the preset chain."""
    ref = "v2.5.0-rc"
    environment._add_remote_json(
        ref,
        "BBL/machine/Broken Child.json",
        {"name": "Broken Child", "type": "machine", "inherits": "missing_parent"},
    )
    try:
        _flatten_for_test(
            "BBL", "BBL/machine/Broken Child.json", environment.slicer_exe
        )
    except SlicerProfilesError as exc:
        message = str(exc)
        _expect("missing_parent" in message, "error must name the missing parent")
        _expect("Broken Child" in message, "error must include the chain")
    else:
        raise AssertionError("unresolvable parents must raise SlicerProfilesError")


def _check_imported_profiles(environment: _TestEnvironment) -> None:
    """Profile files are validated/imported by type and deduplicated."""
    source_dir = environment.root / "import_source"
    _write_profile_json(
        source_dir / "machine.json",
        {"name": "Imported Machine", "type": "machine", "setting_id": "IM-1"},
    )
    _write_profile_json(
        source_dir / "process.json",
        {"name": "Imported Process", "type": "process", "setting_id": "IP-1"},
    )
    _write_profile_json(
        source_dir / "filament.json",
        {"name": "Imported Filament", "type": "filament", "filament_type": "PLA"},
    )
    source_paths = [
        source_dir / "machine.json",
        source_dir / "process.json",
        source_dir / "filament.json",
    ]

    first = import_profile_files(source_paths)
    _expect(len(first["machines"]) == 1, "one imported machine expected")
    _expect(len(first["processes"]) == 1, "one imported process expected")
    _expect(len(first["filaments"]) == 1, "one imported filament expected")
    _expect(
        Path(first["root"]) == environment.cache_root / "profiles",
        "imported root must be the add-in profile directory, got {}".format(first["root"]),
    )
    _expect(
        first["machines"][0]["path"].endswith(
            os.path.join("machine", "Imported Machine.json")
        ),
        "machine profile must be copied under machine/",
    )

    second = import_profile_files(source_paths)
    _expect(len(second["machines"]) == 1, "re-import must deduplicate machines")
    _expect(len(second["processes"]) == 1, "re-import must deduplicate processes")
    _expect(len(second["filaments"]) == 1, "re-import must deduplicate filaments")

    imported_printers = list_printers(environment.slicer_exe, "imported")
    _expect(
        [printer["name"] for printer in imported_printers] == ["Imported Machine"],
        "imported machines must be listed",
    )
    imported_processes = list_processes(
        environment.slicer_exe, "imported", "Imported Machine"
    )
    _expect(
        [process["name"] for process in imported_processes] == ["Imported Process"],
        "imported processes must be listed",
    )

    invalid = _write_profile_json(source_dir / "invalid.json", {"name": "No Type"})
    try:
        import_profile_files([invalid])
    except SlicerProfilesError as exc:
        _expect("type" in str(exc), "invalid profile errors must mention the type field")
    else:
        raise AssertionError("profiles without a type must raise SlicerProfilesError")


def _write_prepared_3mf(
    path: Path,
    printer_settings_id: str = "Bambu Lab X1 Carbon 0.4 nozzle",
    include_printer_id: bool = True,
    model_settings: Optional[bytes] = None,
) -> None:
    """Write a slicer-like prepared project 3MF for the self-test."""
    settings: Dict[str, Any] = {
        "filament_colour": ["#000000"],
        "filament_type": ["PLA"],
        "filament_density": ["1.00"],
        "filament_settings_id": ["slicer-chosen-1", "slicer-chosen-2"],
        "filament_diameter": ["1.75", "1.75"],
        "filament_map": ["1"],
        "filament_map_2": ["1"],
        "filament_nozzle_map": ["0"],
        "filament_volume_map": ["0"],
        "some_other_key": "preserved",
    }
    if include_printer_id:
        settings["printer_settings_id"] = printer_settings_id
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(path), "w") as archive:
        archive.writestr("[Content_Types].xml", b"<?xml version='1.0'?><Types/>")
        archive.writestr("3D/3dmodel.model", b"<model/>")
        archive.writestr(_PROJECT_SETTINGS_PART, json.dumps(settings).encode("utf-8"))
        archive.writestr("Metadata/thumbnail.png", b"slicer-thumb")
        if model_settings is not None:
            archive.writestr(_MODEL_SETTINGS_PART, model_settings)


def _prepare_command_handler(
    records: Dict[str, Any],
    include_printer_settings_id: bool = True,
    returncode: int = 0,
    write_output: bool = True,
    model_settings: Optional[bytes] = None,
) -> Callable[[List[str], _FakeRunner], _FakeCompletedProcess]:
    """Build a fake CLI handler that records the command and writes a project."""

    def handler(command: List[str], runner: _FakeRunner) -> _FakeCompletedProcess:
        output_name = _flag_value(command, "--export-3mf")
        output_dir = _flag_value(command, "--outputdir")
        logfile = _flag_value(command, "--logfile")
        base_index = command.index("--export-3mf") + 2
        base_path = Path(command[base_index])
        records["command"] = list(command)
        records["output_name"] = output_name
        records["output_dir"] = output_dir
        records["logfile"] = logfile
        records["base_path"] = str(base_path)
        records["base_has_project_settings"] = (
            _PROJECT_SETTINGS_PART in _archive_names(base_path)
        )
        if logfile is not None:
            Path(logfile).parent.mkdir(parents=True, exist_ok=True)
            Path(logfile).write_text(
                "\n".join(
                    "slicer log line {}".format(index) for index in range(1, 41)
                )
                + "\n",
                encoding="utf-8",
            )
        if write_output and returncode == 0 and output_name and output_dir:
            _write_prepared_3mf(
                Path(output_dir) / output_name,
                include_printer_id=include_printer_settings_id,
                model_settings=model_settings,
            )
        return _FakeCompletedProcess(returncode, "slicer stdout", "slicer stderr")

    return handler


def _local_target(environment: _TestEnvironment) -> ProfileTarget:
    """Return the local Bambu Studio profile target used by the checks."""
    return ProfileTarget(
        vendor="bambu_local",
        printer_name="Bambu Lab X1 Carbon 0.4 nozzle",
        machine_path=str(
            environment.bambu_system
            / "machine"
            / "Bambu Lab X1 Carbon 0.4 nozzle.json"
        ),
        process_name="0.20mm Standard @BBL X1C",
        process_path=str(
            environment.bambu_system
            / "process"
            / "0.20mm Standard @BBL X1C.json"
        ),
        source="bambu_local",
    )


def _check_prepare_local(environment: _TestEnvironment) -> None:
    """Local target: CLI command shape, base copy rule and settings post-processing."""
    records: Dict[str, Any] = {}
    environment.runner.cli_handler = _prepare_command_handler(records)
    base_3mf = _write_fixture_3mf(
        environment.root / "base.3mf", include_project_settings=True
    )
    output = environment.root / "out" / "project.3mf"
    log_dir = environment.root / "logs"
    target = _local_target(environment)

    prepared = prepare_project(
        base_3mf,
        output,
        target,
        environment.slicer_exe,
        environment.materials,
        log_dir=log_dir,
    )

    _expect(prepared.output_path == output, "prepared project path mismatch")
    _expect(prepared.printer_name == target.printer_name, "printer name mismatch")
    _expect(prepared.process_name == target.process_name, "process name mismatch")
    _expect(prepared.slicer_version == "2.5.0-rc", "slicer version mismatch")
    _expect(
        prepared.filament_names
        == tuple(material["display_name"] for material in environment.materials),
        "filament names must come from the detected materials",
    )
    _expect(
        "slicer log line 40" in prepared.log_tail,
        "log tail must include the last lines",
    )
    _expect(
        len(prepared.log_tail.splitlines()) == LOG_TAIL_LINES,
        "log tail must be capped",
    )

    command = records["command"]
    _expect(
        records["base_has_project_settings"] is False,
        "the CLI base must not contain project_settings",
    )
    _expect(
        _flag_value(command, "--export-3mf") == output.name,
        "--export-3mf must be a bare basename",
    )
    _expect(
        _flag_value(command, "--outputdir") == str(output.parent),
        "--outputdir must be the output directory",
    )
    settings_paths = (_flag_value(command, "--load-settings") or "").split(";")
    loaded = [json.loads(Path(path).read_text(encoding="utf-8")) for path in settings_paths]
    _expect(
        [preset.get("name") for preset in loaded] == [target.printer_name, target.process_name]
        and all(preset.get("from") == "user" and "inherits" not in preset for preset in loaded),
        "--load-settings must be stand-alone machine;process copies for OrcaSlicer: {}".format(loaded),
    )
    _expect(_flag_value(command, "--debug") == "4", "--debug 4 must be requested")
    _expect(command[-1] == "4", "the CLI command must end with --debug 4")
    _expect(
        _flag_value(command, "--logfile") == str(log_dir / "slicer_cli.log"),
        "--logfile must point at the requested log directory",
    )
    filament_arg = _flag_value(command, "--load-filaments") or ""
    _expect(
        filament_arg.count(";") == 2,
        "three materials must produce two separators in --load-filaments",
    )
    for filament_path in filament_arg.split(";"):
        _expect(
            Path(filament_path).is_file(),
            "filament path must exist: {}".format(filament_path),
        )
    _expect(
        "Bambu PLA Basic @BBL X1C" in filament_arg,
        "PLA material must select the matching Bambu filament preset",
    )
    preset_colours = [
        json.loads(Path(path).read_text(encoding="utf-8")).get("filament_colour")
        for path in filament_arg.split(";")
    ]
    _expect(
        preset_colours == [["#161616"], ["#FFFFFF"], ["#111111"]],
        "every filament preset must carry its material's colour (purge volumes): {}".format(preset_colours),
    )

    settings = json.loads(
        _read_archive_entry(output, _PROJECT_SETTINGS_PART).decode("utf-8")
    )
    _expect(
        settings["filament_colour"] == ["#161616", "#FFFFFF", "#111111"],
        "colours must be post-processed",
    )
    _expect(
        settings["filament_type"] == ["PLA", "PETG", "ABS"],
        "types must be post-processed",
    )
    _expect(
        settings["filament_density"] == ["1.24", "1.27", "1.04"],
        "densities must be post-processed",
    )
    _expect(
        settings["some_other_key"] == "preserved",
        "unrelated project settings must be preserved",
    )
    expected_names = [
        "Bambu PLA Basic @BBL X1C",
        "Bambu PETG HF @BBL X1C",
        "Bambu ABS @BBL X1C",
    ]
    _expect(
        settings["filament_settings_id"] == expected_names,
        "filament_settings_id must preserve the slicer preset names, got {}".format(
            settings.get("filament_settings_id")
        ),
    )
    for map_key in ("filament_map", "filament_map_2", "filament_nozzle_map", "filament_volume_map"):
        _expect(
            len(settings[map_key]) == len(environment.materials),
            "{} must have one entry per filament, got {}".format(
                map_key, settings.get(map_key)
            ),
        )
    _expect(
        _read_archive_entry(output, "Metadata/thumbnail.png") == b"slicer-thumb",
        "output entries must survive",
    )
    _expect(
        _PROJECT_SETTINGS_PART in _archive_names(base_3mf),
        "the original base 3MF must not be modified",
    )


def _check_prepare_remote(environment: _TestEnvironment) -> None:
    """Remote target: presets are flattened, written from=user and passed to the CLI."""
    records: Dict[str, Any] = {}
    environment.runner.cli_handler = _prepare_command_handler(records)
    printers = list_printers(environment.slicer_exe, "BBL")
    printer = next(
        entry
        for entry in printers
        if entry["name"] == "Bambu Lab X1 Carbon 0.4 nozzle"
    )
    processes = list_processes(
        environment.slicer_exe, "BBL", "Bambu Lab X1 Carbon 0.4 nozzle"
    )
    process = next(
        entry
        for entry in processes
        if entry["name"] == "0.20mm Standard @BBL X1C"
    )
    target = ProfileTarget(
        vendor="BBL",
        printer_name=printer["name"],
        machine_path=printer["machine_path"],
        process_name=process["name"],
        process_path=process["process_path"],
        source="orca_remote",
    )
    base_3mf = _write_fixture_3mf(environment.root / "remote_base.3mf")
    output = environment.root / "remote_out" / "project.3mf"

    prepared = prepare_project(
        base_3mf, output, target, environment.slicer_exe, environment.materials
    )
    _expect(output.is_file(), "remote preparation must write the output 3MF")
    _expect(
        prepared.printer_name == "Bambu Lab X1 Carbon 0.4 nozzle",
        "remote printer name mismatch",
    )

    settings_argument = _flag_value(records["command"], "--load-settings") or ""
    parts = settings_argument.split(";")
    _expect(
        len(parts) == 2,
        "--load-settings must contain flattened machine;process paths",
    )
    for path_text in parts:
        flattened_path = Path(path_text)
        _expect(
            flattened_path.is_file(),
            "flattened preset must exist: {}".format(path_text),
        )
        flattened = json.loads(flattened_path.read_text(encoding="utf-8"))
        _expect("inherits" not in flattened, "flattened preset must not inherit")
        _expect(flattened.get("from") == "user", "flattened preset must be from=user")
    machine_flat = json.loads(Path(parts[0]).read_text(encoding="utf-8"))
    _expect(
        machine_flat.get("machine_max_acceleration_x") == ["1000", "1000"],
        "flattened machine must contain the merged parent keys",
    )
    process_flat = json.loads(Path(parts[1]).read_text(encoding="utf-8"))
    _expect(
        process_flat.get("outer_wall_speed") == ["200"],
        "flattened process parent keys missing",
    )

    post_settings = json.loads(
        _read_archive_entry(output, _PROJECT_SETTINGS_PART).decode("utf-8")
    )
    _expect(
        post_settings["filament_colour"] == ["#161616", "#FFFFFF", "#111111"],
        "remote project colours must be post-processed",
    )


def _check_join_touching_bodies(environment: _TestEnvironment) -> None:
    """Touching bodies become one object whose parts keep their places and slots."""

    def cube(offset_x: float, offset_z: float = 0.0) -> str:
        points = [(x + offset_x, y, z + offset_z) for z in (0, 10) for y in (0, 10) for x in (0, 10)]
        faces = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
                 (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
        return "<mesh><vertices>{}</vertices><triangles>{}</triangles></mesh>".format(
            "".join('<vertex x="{:g}" y="{:g}" z="{:g}"/>'.format(*point) for point in points),
            "".join('<triangle v1="{}" v2="{}" v3="{}"/>'.format(*face) for face in faces),
        )

    def write(name: str, objects: str, items: str) -> Path:
        path = environment.root / "join" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(str(path), "w") as archive:
            archive.writestr("3D/3dmodel.model", (
                '<?xml version="1.0"?><model unit="millimeter" xmlns="{}"><resources>{}</resources>'
                '<build>{}</build></model>'
            ).format(_CORE_NS, objects, items))
            archive.writestr(_MODEL_SETTINGS_PART, (
                '<config><object id="1"><metadata key="name" value="Base"/><metadata key="extruder" value="2"/></object>'
                '<object id="2"><metadata key="name" value="Inlay"/><metadata key="extruder" value="1"/></object></config>'
            ))
            archive.writestr(_PROJECT_SETTINGS_PART, "{}")
            archive.writestr("Metadata/thumbnail.png", b"png")
        return path

    # The inlay sits on top of the base, placed by its build-item transform.
    touching = write(
        "touching.3mf",
        '<object id="1" name="Base" type="model">{}</object><object id="2" name="Inlay" type="model">{}</object>'.format(
            cube(0), cube(0)),
        '<item objectid="1"/><item objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 10"/>',
    )
    joined = _join_touching_bodies(touching, environment.root / "join")
    _expect(joined is not None and joined != touching, "touching bodies are joined into a copy")
    with zipfile.ZipFile(str(joined)) as archive:
        names = archive.namelist()
        model = ET.fromstring(archive.read("3D/3dmodel.model"))
        settings = ET.fromstring(archive.read(_MODEL_SETTINGS_PART))
    items = list(model.iter("{%s}item" % _CORE_NS))
    components = [(item.get("objectid"), item.get("transform")) for item in model.iter("{%s}component" % _CORE_NS)]
    _expect(len(items) == 1 and items[0].get("objectid") == "3", "one build item for the joined object")
    _expect(components == [("1", "1 0 0 0 1 0 0 0 1 0 0 0"), ("2", "1 0 0 0 1 0 0 0 1 0 0 10")],
            "each body is a part at its own place: {}".format(components))
    parts = [
        (part.get("id"), {meta.get("key"): meta.get("value") for meta in part.findall("metadata")})
        for part in settings.iter("part")
    ]
    _expect(parts == [("1", {"name": "Base", "extruder": "2"}), ("2", {"name": "Inlay", "extruder": "1"})],
            "each part keeps its body's name and slot: {}".format(parts))
    _expect(_PROJECT_SETTINGS_PART not in names and "Metadata/thumbnail.png" in names,
            "the copy drops project settings and keeps the rest")
    _expect(_layout_flags(joined) == [], "the joined object needs no --assemble")

    apart = write(
        "apart.3mf",
        '<object id="1" name="Base" type="model">{}</object><object id="2" name="Inlay" type="model">{}</object>'.format(
            cube(0), cube(50)),
        '<item objectid="1"/><item objectid="2"/>',
    )
    _expect(_join_touching_bodies(apart, environment.root / "join") is None, "separate bodies are not joined")
    nested = write(
        "nested.3mf",
        '<object id="1" name="Base" type="model">{}</object><object id="2" name="Inlay" type="model">'
        '<components><component objectid="1"/></components></object>'.format(cube(0)),
        '<item objectid="1"/><item objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 10"/>',
    )
    _expect(_join_touching_bodies(nested, environment.root / "join") is None,
            "a body that is an assembly itself is left to the slicer")


def _check_bridge_line_width() -> None:
    """Mixed nozzles: the bridge width stays within the smallest nozzle."""
    mixed = {"nozzle_diameter": ["0.4", "0.4", "0.6", "0.6"]}
    default = {"line_width": "105%"}
    _fit_bridge_line_width(mixed, default)
    _expect(default["bridge_line_width"] == "0.4", "OrcaSlicer's 100% default becomes 0.4 mm: {}".format(default))
    narrow = {"bridge_line_width": "60%"}
    _fit_bridge_line_width(mixed, narrow)
    _expect(narrow["bridge_line_width"] == "60%", "60% of 0.6 mm (0.36) already fits: {}".format(narrow))
    same = {}
    _fit_bridge_line_width({"nozzle_diameter": ["0.4", "0.4"]}, same)
    _expect(same == {}, "equal nozzles keep the slicer's default")


def _check_remote_filament_presets(environment: _TestEnvironment) -> None:
    """A printer from the online profiles gets its vendor's own filament presets."""
    target = ProfileTarget(
        vendor="Prusa", printer_name="Prusa MK4 0.4 nozzle", machine_path="", process_name="",
        process_path="", source="orca_remote",
    )
    candidates = _remote_filament_candidates(target, environment.slicer_exe, ["PLA", "PETG"])
    names = sorted(candidate["name"] for candidate in candidates)
    _expect(
        names == ["Generic PETG @System", "Generic PLA @Prusa MK4"],
        "the MK4's PLA and the library PETG; not the XL's, not a base preset: {}".format(names),
    )
    materials = [
        {"filament_type": "PLA", "display_name": "Prusament - PLA - Galaxy Black"},
        {"filament_type": "PETG", "display_name": "Prusament - PETG - Orange"},
    ]
    paths = _choose_filament_paths(materials, target.printer_name, candidates)
    chosen = [json.loads(Path(item).read_text(encoding="utf-8")) for item in paths]
    _expect(
        [preset["name"] for preset in chosen] == ["Generic PLA @Prusa MK4", "Generic PETG @System"],
        "each material gets the vendor's preset: {}".format([preset["name"] for preset in chosen]),
    )
    brands = [
        {"name": "Prusament PLA @MK4", "path": "prusament.json", "file_name": "prusament.json", "types": {"PLA"},
         "compatible_printers": ("prusa mk4 0.4 nozzle",)},
        {"name": "Generic PLA @Prusa MK4", "path": "generic.json", "file_name": "generic.json", "types": {"PLA"},
         "compatible_printers": ("prusa mk4 0.4 nozzle",)},
    ]
    _expect(
        _pick_filament_candidate(brands, "PLA", target.printer_name, "Bambu PLA Basic") == "generic.json"
        and _pick_filament_candidate(brands, "PLA", target.printer_name, "Prusament PLA") == "prusament.json",
        "another brand's PLA gets the generic preset; Prusament gets Prusament",
    )
    _expect(
        chosen[0].get("nozzle_temperature") == ["215"] and "inherits" not in chosen[0] and chosen[0].get("from") == "user",
        "the preset is flattened with its parent's values: {}".format(chosen[0]),
    )
    unlisted = ProfileTarget(
        vendor="Prusa", printer_name="Prusa CORE One 0.4 nozzle", machine_path="", process_name="",
        process_path="", source="orca_remote",
    )
    _expect(
        [candidate["name"] for candidate in _remote_filament_candidates(unlisted, environment.slicer_exe, ["PLA"])] == [],
        "no preset fits a printer the vendor list does not name",
    )


def _check_prepare_failure(environment: _TestEnvironment) -> None:
    """CLI failures/missing output/empty printer id raise with the log tail."""
    records: Dict[str, Any] = {}
    base_3mf = _write_fixture_3mf(environment.root / "failure_base.3mf")
    output = environment.root / "failure_out" / "project.3mf"
    target = _local_target(environment)

    environment.runner.cli_handler = _prepare_command_handler(
        records, returncode=7, write_output=False
    )
    try:
        prepare_project(
            base_3mf, output, target, environment.slicer_exe, environment.materials
        )
    except SlicerProfilesError as exc:
        message = str(exc)
        _expect(
            "exit code 7" in message,
            "error must include the exit code: {}".format(message),
        )
        _expect(
            "slicer log line 40" in message,
            "error must include the log tail: {}".format(message),
        )
        _expect("--export-3mf" in message, "error must include the command")
    else:
        raise AssertionError("a failing CLI must raise SlicerProfilesError")
    _expect(not output.exists(), "a failed preparation must not leave a project")

    environment.runner.cli_handler = _prepare_command_handler(
        records, returncode=0, write_output=False
    )
    try:
        prepare_project(
            base_3mf, output, target, environment.slicer_exe, environment.materials
        )
    except SlicerProfilesError as exc:
        _expect(
            "did not create the output" in str(exc),
            "missing output error mismatch",
        )
    else:
        raise AssertionError("a missing CLI output must raise SlicerProfilesError")

    environment.runner.cli_handler = _prepare_command_handler(
        records, include_printer_settings_id=False
    )
    try:
        prepare_project(
            base_3mf, output, target, environment.slicer_exe, environment.materials
        )
    except SlicerProfilesError as exc:
        _expect(
            "printer_settings_id" in str(exc),
            "empty printer_settings_id must be rejected",
        )
    else:
        raise AssertionError("an empty printer_settings_id must raise SlicerProfilesError")


def _check_offline_cache(environment: _TestEnvironment) -> None:
    """Remote manifests stay listable from cache when the network goes away."""

    def offline_opener(url: str, timeout: float) -> bytes:
        raise RuntimeError("simulated network outage for {}".format(url))

    previous_opener = _module._url_opener
    _module._url_opener = offline_opener
    try:
        printers = list_printers(environment.slicer_exe, "BBL")
    finally:
        _module._url_opener = previous_opener
    _expect(
        [printer["name"] for printer in printers]
        == ["Bambu Lab X1 Carbon 0.4 nozzle", "Bambu Lab A1 0.4 nozzle"],
        "cached manifests must keep listing printers offline",
    )


def _check_filament_fallback(environment: _TestEnvironment) -> None:
    """Filament selection matches types, prefers printer tokens and repeats files."""
    paths = _choose_filament_paths(
        environment.materials, "Bambu Lab X1 Carbon 0.4 nozzle"
    )
    _expect(len(paths) == 3, "one filament path per material expected")
    _expect("PLA" in Path(paths[0]).name, "PLA material must choose a PLA preset")
    _expect("PETG" in Path(paths[1]).name, "PETG material must choose a PETG preset")
    _expect("ABS" in Path(paths[2]).name, "ABS material must choose an ABS preset")
    _expect(
        "X1C" in Path(paths[0]).name,
        "the printer token must be preferred in filament preset names",
    )

    fallback_materials = [
        {
            "color_hex": "#123456",
            "filament_type": "TPU",
            "density_g_cm3": 1.2,
            "display_name": "TPU",
        },
        {
            "color_hex": "#654321",
            "filament_type": "PETG",
            "density_g_cm3": 1.27,
            "display_name": "PETG",
        },
    ]
    fallback_paths = _choose_filament_paths(
        fallback_materials, "Bambu Lab X1 Carbon 0.4 nozzle"
    )
    _expect(
        len(fallback_paths) == 2,
        "fallback selection must return one path per material",
    )
    _expect(
        fallback_paths[0] == fallback_paths[1],
        "an unmatched type must reuse the chosen filament file",
    )


def _check_database_presets(environment: _TestEnvironment) -> None:
    """A database filament with no installed preset gets a built one; a spool's
    user preset is used; an installed preset of the named filament wins."""
    filament_dir = environment.bambu_system / "filament"
    generic = _write_profile_json(filament_dir / "Generic PETG @BBL X1C.json", {
        "name": "Generic PETG @BBL X1C", "type": "filament", "filament_type": ["PETG"], "filament_id": "GFG99",
        "nozzle_temperature": ["255"], "hot_plate_temp": ["70"], "filament_vendor": ["Generic"],
    })
    own_dir = environment.root / "slicer_support" / "BambuStudio" / "user" / "default" / "filament" / "base"
    own = _write_profile_json(own_dir / "Polymaker PPS GF @Bambu Lab X1 Carbon 0.4 nozzle.json", {
        "name": "Polymaker PPS GF @Bambu Lab X1 Carbon 0.4 nozzle", "from": "User", "inherits": "",
        "filament_type": ["PPS"], "filament_id": "P7bc2bae",
        "compatible_printers": ["Bambu Lab X1 Carbon 0.4 nozzle"],
    })
    try:
        materials = [
            {"filament_type": "PETG", "display_name": "addnorth - PETG Economy - Red", "color_hex": "#FF0000",
             "manufacturer": "addnorth", "product_line": "PETG Economy",
             "slicer_data": {"min_print_temperature": 230, "max_print_temperature": 250, "slicers": {
                 "bambustudio": {"profile_name": "addnorth PETG Economy", "generic_id": "GFG99"}}}},
            {"filament_type": "PPS", "display_name": "PPS", "color_hex": "#202020",
             "orca_filament_profile": "Polymaker PPS GF"},
            {"filament_type": "PLA", "display_name": "Bambu Lab - PLA Basic - Black", "color_hex": "#000000",
             "manufacturer": "Bambu Lab", "product_line": "PLA Basic",
             "slicer_data": {"min_print_temperature": 200, "max_print_temperature": 220}},
            {"filament_type": "PETG", "display_name": "Voxel PLA - PETG - Red", "color_hex": "#E20010",
             "manufacturer": "Voxel PLA", "product_line": "PETG",
             "slicer_data": {"min_print_temperature": 230, "max_print_temperature": 260,
                             "min_bed_temperature": 60, "max_bed_temperature": 80}},
        ]
        built: List[str] = []
        paths = _choose_filament_paths(materials, "Bambu Lab X1 Carbon 0.4 nozzle", (), True, built)
        made = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
        _expect(built[0] == paths[0] and made["name"] == "addnorth PETG Economy @Bambu Lab X1 Carbon 0.4 nozzle",
                "the database filament gets a built preset: {}".format(made.get("name")))
        _expect(made["nozzle_temperature"] == ["240"] and made["hot_plate_temp"] == ["70"]
                and made["filament_vendor"] == ["addnorth"] and made["inherits"] == made["name"],
                "built on Generic PETG (GFG99) with the database temperature, in Bambu Studio's CLI form")
        mine = json.loads(Path(paths[1]).read_text(encoding="utf-8"))
        _expect(mine["filament_id"] == "P7bc2bae" and mine["type"] == "filament" and mine["from"] == "user",
                "the spool's own user preset is used, with the type the CLI needs")
        _expect(Path(paths[2]).name == "Bambu PLA Basic @BBL X1C.json", "an installed preset of the filament wins")
        voxel = json.loads(Path(paths[3]).read_text(encoding="utf-8"))
        _expect(built == [paths[0], paths[3]] and voxel["name"] == "Voxel PLA PETG @Bambu Lab X1 Carbon 0.4 nozzle"
                and voxel["nozzle_temperature"] == ["245"],
                "another brand's preset of the same type is not the filament: {}".format(voxel.get("name")))
        orca_paths = _choose_filament_paths(materials[1:2], "Bambu Lab X1 Carbon 0.4 nozzle", (), False)
        _expect("inherits" not in json.loads(Path(orca_paths[0]).read_text(encoding="utf-8")),
                "OrcaSlicer's copy inherits nothing")
        other_printer = _choose_filament_paths([materials[2], materials[1]], "Bambu Lab H2D 0.4 nozzle", (), True)
        _expect("P7bc2bae" not in Path(other_printer[1]).read_text(encoding="utf-8"),
                "a user preset made for another printer is not used")
    finally:
        generic.unlink()
        own.unlink()


def _expect(condition: bool, message: str) -> None:
    """Raise AssertionError with ``message`` when ``condition`` is false."""
    if not condition:
        raise AssertionError(message)


def _check_app_bundle_resolution(environment: _TestEnvironment) -> None:
    """A macOS .app bundle path resolves to its Contents/MacOS executable."""
    import stat

    with tempfile.TemporaryDirectory(prefix="filamatch_app_bundle_") as temp_dir:
        app_path = Path(temp_dir) / "FakeSlicer.app"
        macos_dir = app_path / "Contents" / "MacOS"
        macos_dir.mkdir(parents=True)
        executable = macos_dir / "FakeSlicer"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        resolved = resolve_slicer_executable(str(app_path))
        _expect(
            Path(resolved) == executable,
            "app bundle must resolve to Contents/MacOS executable, got {!r}".format(resolved),
        )
        _expect(
            resolve_slicer_executable(resolved) == resolved,
            "an already-resolved executable path must be preserved",
        )
        try:
            resolve_slicer_executable(str(app_path / "Contents" / "Missing"))
        except SlicerProfilesError:
            pass
        else:
            raise AssertionError("a missing app bundle must raise SlicerProfilesError")


def _check_fit_wipe_tower(environment: _TestEnvironment) -> None:
    """The prime tower is moved inside the area every nozzle can reach."""
    h2d = {
        "enable_prime_tower": "1",
        "wipe_tower_x": ["15"],
        "wipe_tower_y": ["220"],
        "prime_tower_width": "60",
        "printable_area": ["0x0", "350x0", "350x320", "0x320"],
        "extruder_printable_area": ["0x0,325x0,325x320,0x320", "25x0,350x0,350x320,25x320"],
    }
    _fit_wipe_tower(h2d)
    _expect(h2d["wipe_tower_x"] == ["30"], "x must move inside the shared area: {}".format(h2d))
    _expect(h2d["wipe_tower_y"] == ["220"], "a y already inside must stay")
    single = {
        "enable_prime_tower": "1",
        "wipe_tower_x": ["15"],
        "wipe_tower_y": ["300"],
        "prime_tower_width": "35",
        "printable_area": ["0x0", "256x0", "256x256", "0x256"],
    }
    _fit_wipe_tower(single)
    _expect(single["wipe_tower_x"] == ["15"], "a single-nozzle x inside the bed must stay")
    _expect(single["wipe_tower_y"] == ["210"], "y must be pulled back onto the bed, brim included: {}".format(single))
    # Bambu Lab X2D: rib walls make the tower bigger; at y 216 Bambu Studio refused to slice.
    x2d = dict(single, wipe_tower_y=["216"], prime_tower_brim_width="-1", prime_tower_rib_wall="1",
               prime_tower_rib_width="8", extruder_printable_area=["0x0,256x0,256x256,0x256", "20.5x0,256x0,256x256,20.5x256"])
    _fit_wipe_tower(x2d)
    _expect(x2d["wipe_tower_y"] == ["202"] and x2d["wipe_tower_x"] == ["25.5"],
            "the X2D tower must fit with its rib walls: {}".format(x2d))
    # Prusa XL: the tower is turned 90 degrees and grows to the left of x; at x 15 OrcaSlicer refused.
    xl = {"enable_prime_tower": "1", "wipe_tower_x": ["15"], "wipe_tower_y": ["220"], "prime_tower_width": "60",
          "prime_tower_brim_width": "3", "wipe_tower_rotation_angle": "90",
          "printable_area": ["0x0", "360x0", "360x360", "0x360"]}
    _fit_wipe_tower(xl)
    _expect(xl["wipe_tower_x"] == ["71"] and xl["wipe_tower_y"] == ["220"],
            "a turned tower keeps its size free left of x: {}".format(xl))
    _expect(
        _cli_error_line("loading\nSelected nozzle temperatures are incompatible.\nrun found error, exit\n")
        == "Selected nozzle temperatures are incompatible."
        and _cli_error_line("run found error, exit\nPrime tower is outside.\n") == "Prime tower is outside."
        and _cli_error_line("all fine") == "",
        "the slicer's own reason is read from its output (stdout or stderr first)",
    )
    off = dict(h2d, enable_prime_tower="0", wipe_tower_x=["15"])
    _fit_wipe_tower(off)
    _expect(off["wipe_tower_x"] == ["15"], "a disabled prime tower is left alone")

    abs_gf = {
        "curr_bed_type": "Cool Plate",
        "cool_plate_temp": ["0", "0"],
        "textured_plate_temp": ["90", "90"],
        "hot_plate_temp": ["90", "90"],
    }
    _fit_bed_type(abs_gf)
    _expect(abs_gf["curr_bed_type"] == "Textured PEI Plate", "ABS must leave the Cool Plate")
    pla = {"curr_bed_type": "Cool Plate", "cool_plate_temp": ["35", "35"]}
    _fit_bed_type(pla)
    _expect(pla["curr_bed_type"] == "Cool Plate", "a plate that suits every filament stays")
    mixed = {
        "curr_bed_type": "Cool Plate",
        "cool_plate_temp": ["35", "0"],
        "textured_plate_temp": ["0", "90"],
        "hot_plate_temp": ["65", "90"],
    }
    _fit_bed_type(mixed)
    _expect(mixed["curr_bed_type"] == "High Temp Plate", "the first plate suiting all filaments wins")

    orca_h2d = {
        "nozzle_diameter": ["0.4", "0.4"],
        "nozzle_volume_type": ["Standard"],
        "default_nozzle_volume_type": ["Standard", "High Flow"],
    }
    _fit_nozzle_volume_types(orca_h2d)
    _expect(
        orca_h2d["nozzle_volume_type"] == ["Standard", "High Flow"],
        "each H2D nozzle needs its own flow type: {}".format(orca_h2d),
    )
    no_default = {"nozzle_diameter": ["0.4", "0.4"], "nozzle_volume_type": ["High Flow"]}
    _fit_nozzle_volume_types(no_default)
    _expect(
        no_default["nozzle_volume_type"] == ["High Flow", "High Flow"],
        "without a preset default the last flow type repeats",
    )
    one_nozzle = {"nozzle_diameter": ["0.4"], "nozzle_volume_type": ["Standard"]}
    _fit_nozzle_volume_types(one_nozzle)
    _expect(one_nozzle["nozzle_volume_type"] == ["Standard"], "a single nozzle is left alone")


def _check_prepare_bambu_studio(environment: _TestEnvironment) -> None:
    """Bambu Studio prepares by itself: no --logfile, named presets, output moved, log kept."""
    bambu_exe = environment.root / "BambuStudio"
    bambu_exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    saved_help = environment.runner.help_text
    environment.runner.help_text = "BambuStudio-2.9.1"
    records: Dict[str, Any] = {}
    environment.runner.cli_handler = _prepare_command_handler(records)
    output = environment.root / "bambu_out" / "project.3mf"
    base_3mf = _write_fixture_3mf(environment.root / "bambu_base.3mf")
    try:
        prepared = prepare_project(
            base_3mf, output, _local_target(environment), bambu_exe, environment.materials
        )
        command = records["command"]
        _expect(output.is_file() and prepared.output_path == output, "the project must reach the requested path")
        _expect("--logfile" not in command, "Bambu Studio's CLI has no --logfile: {}".format(command))
        _expect(_flag_value(command, "--outputdir") != str(output.parent), "Bambu Studio exports into the cache")
        _expect(
            not list(Path(_flag_value(command, "--outputdir") or "").glob("*.3mf")),
            "the exported project must be moved out of the cache",
        )
        _expect("slicer stdout" in prepared.log_tail, "Bambu Studio's output is the log: {!r}".format(prepared.log_tail))
        presets = [
            json.loads(Path(path).read_text(encoding="utf-8"))
            for path in (_flag_value(command, "--load-settings") or "").split(";")
            + (_flag_value(command, "--load-filaments") or "").split(";")
        ]
        _expect(
            all(preset.get("inherits") == preset.get("name") and preset.get("from") == "user" for preset in presets),
            "Bambu Studio's presets must name themselves in inherits: {}".format(
                [(preset.get("name"), preset.get("inherits")) for preset in presets]
            ),
        )

        def failing(command: List[str], runner: _FakeRunner) -> _FakeCompletedProcess:
            out_dir = Path(_flag_value(command, "--outputdir") or "")
            (out_dir / "result.json").write_text(
                json.dumps({"error_string": "process not compatible with printer", "return_code": -17})
            )
            return _FakeCompletedProcess(239, "run found error", "")

        environment.runner.cli_handler = failing
        try:
            prepare_project(base_3mf, output, _local_target(environment), bambu_exe, environment.materials)
        except SlicerProfilesError as exc:
            _expect("process not compatible with printer" in str(exc), "result.json must explain: {}".format(exc))
        else:
            raise AssertionError("a failing Bambu Studio run must raise")

        remote = ProfileTarget(
            vendor="Prusa", printer_name="Prusa MK4", machine_path="", process_name="0.20mm", process_path="",
            source="orca_remote",
        )
        try:
            prepare_project(base_3mf, output, remote, bambu_exe, environment.materials)
        except SlicerProfilesError as exc:
            _expect("OrcaSlicer's online profiles" in str(exc), "unexpected error: {}".format(exc))
        else:
            raise AssertionError("Bambu Studio must refuse OrcaSlicer's online profiles")
    finally:
        environment.runner.help_text = saved_help


def _check_bambu_studio_words(environment: _TestEnvironment) -> None:
    """An OrcaSlicer project gets Bambu Studio's words for the settings it words its own way."""
    orca_words = {
        "enable_arc_fitting": "disabled",
        "ensure_vertical_shell_thickness": "ensure_all",
        "ironing_pattern": "rectilinear",
        "support_ironing_pattern": "concentric",
        "sparse_infill_density": "15%",
    }

    def project(name: str, settings: Dict[str, Any]) -> Path:
        path = environment.root / "words" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(str(path), "w") as archive:
            archive.writestr(_PROJECT_SETTINGS_PART, json.dumps(settings))
        return path

    def read(path: Path) -> Dict[str, Any]:
        return json.loads(_read_archive_entry(path, _PROJECT_SETTINGS_PART).decode("utf-8"))

    translated = project("translated.3mf", orca_words)
    _expect(
        use_bambu_studio_words(translated)
        == {"enable_arc_fitting": "0", "ensure_vertical_shell_thickness": "enabled", "ironing_pattern": "zig-zag"},
        "OrcaSlicer's words must be translated: {}".format(read(translated)),
    )
    _expect(read(translated)["sparse_infill_density"] == "15%", "other settings stay")
    _expect(use_bambu_studio_words(translated) == {}, "a second run changes nothing")

    process = _write_profile_json(
        environment.bambu_system / "process" / "0.20mm Words @BBL H2D.json",
        {"name": "0.20mm Words @BBL H2D", "type": "process", "ensure_vertical_shell_thickness": "partial"},
    )
    from_preset = project("preset.3mf", orca_words)
    changed = use_bambu_studio_words(from_preset, process)
    _expect(
        changed.get("ensure_vertical_shell_thickness") == "partial" and changed.get("enable_arc_fitting") == "0",
        "the Bambu Studio preset's own value wins: {}".format(changed),
    )
    process.unlink()
    unknown = project("unknown.3mf", dict(orca_words, ensure_vertical_shell_thickness="ensure_moderate"))
    _expect(
        "ensure_vertical_shell_thickness" not in use_bambu_studio_words(unknown),
        "an unknown OrcaSlicer word is left for Bambu Studio",
    )


def _check_estimate_print(environment: _TestEnvironment) -> None:
    """A copy is sliced for time and grams; two nozzles get nozzle stats; failures raise."""
    project = environment.root / "estimate" / "project.3mf"
    project.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(project), "w") as archive:
        archive.writestr(_PROJECT_SETTINGS_PART, json.dumps({
            "nozzle_diameter": ["0.4", "0.4"], "nozzle_volume_type": ["Standard", "High Flow"], "extruder_nozzle_stats": [],
        }))
    seen: Dict[str, Any] = {}

    def slicing(command: List[str], runner: _FakeRunner) -> _FakeCompletedProcess:
        out_dir = Path(_flag_value(command, "--outputdir") or "")
        with zipfile.ZipFile(command[-1], "r") as archive:
            seen["stats"] = json.loads(archive.read(_PROJECT_SETTINGS_PART))["extruder_nozzle_stats"]
        with zipfile.ZipFile(str(out_dir / "sliced.3mf"), "w") as archive:
            archive.writestr("Metadata/slice_info.config", (
                '<config><plate><metadata key="prediction" value="2563"/><metadata key="weight" value="6.99"/>'
                '<filament id="1" used_g="2.71"/><filament id="3" used_g="4.28"/></plate></config>'
            ))
        return _FakeCompletedProcess(0)

    environment.runner.cli_handler = slicing
    estimate = estimate_print(project, environment.slicer_exe)
    _expect(
        estimate == {"minutes": 43, "grams": 7.0, "plates": 1, "filament_grams": [2.7, 0.0, 4.3]},
        "time, total and grams per slot (unused slot 2 is 0): {}".format(estimate),
    )
    _expect(seen.get("stats") == ["Standard#1", "High Flow#1"], "the copy names each nozzle: {}".format(seen))
    _expect(
        json.loads(_read_archive_entry(project, _PROJECT_SETTINGS_PART))["extruder_nozzle_stats"] == [],
        "the project itself is not changed",
    )
    # Nozzle racks: the H2C's right extruder holds 6 hotends, the H3C's one extruder 9.
    h2c = {"nozzle_diameter": ["0.4", "0.4"], "nozzle_volume_type": ["Standard", "Standard"],
           "extruder_max_nozzle_count": ["1", "6"]}
    h3c = {"nozzle_diameter": ["0.4"], "nozzle_volume_type": ["Standard"], "extruder_max_nozzle_count": ["9"]}
    x1c = {"nozzle_diameter": ["0.4"], "nozzle_volume_type": ["Standard"], "extruder_max_nozzle_count": ["1"]}
    _expect(
        _nozzle_stats(h2c) == ["Standard#1", "Standard#6"] and _nozzle_stats(h3c) == ["Standard#9"]
        and _nozzle_stats(x1c) == [] and _nozzle_stats({"nozzle_diameter": ["0.4", "0.4"]}) == [],
        "rack printers count every hotend; one nozzle or unknown flow types get no stats",
    )

    def failing(command: List[str], runner: _FakeRunner) -> _FakeCompletedProcess:
        out_dir = Path(_flag_value(command, "--outputdir") or "")
        (out_dir / "result.json").write_text(json.dumps({"error_string": "Failed slicing the model."}))
        return _FakeCompletedProcess(156)

    environment.runner.cli_handler = failing
    try:
        estimate_print(project, environment.slicer_exe)
    except SlicerProfilesError as exc:
        _expect("Failed slicing the model" in str(exc), "the error must explain: {}".format(exc))
    else:
        raise AssertionError("a failed slice must raise")

    crashes = {"count": 0}

    def crashing_twice(command: List[str], runner: _FakeRunner) -> _FakeCompletedProcess:
        crashes["count"] += 1
        return _FakeCompletedProcess(-10) if crashes["count"] <= 2 else slicing(command, runner)

    environment.runner.cli_handler = crashing_twice
    _expect(estimate_print(project, environment.slicer_exe)["minutes"] == 43 and crashes["count"] == 3,
            "a crashed slice is run again: {}".format(crashes))
    environment.runner.cli_handler = lambda command, runner: _FakeCompletedProcess(-10)
    try:
        estimate_print(project, environment.slicer_exe)
    except SlicerProfilesError as exc:
        _expect("the slicer crashed (signal 10)" in str(exc), "a crash is named: {}".format(exc))
    else:
        raise AssertionError("a slicer that always crashes must raise")
    _expect(not list((environment.cache_root).glob("estimate_*")), "the temporary folder is removed")

    def two_plates(command: List[str], runner: _FakeRunner) -> _FakeCompletedProcess:
        out_dir = Path(_flag_value(command, "--outputdir") or "")
        with zipfile.ZipFile(str(out_dir / "sliced.3mf"), "w") as archive:
            archive.writestr("Metadata/slice_info.config", (
                '<config><plate><metadata key="prediction" value="1623"/><metadata key="weight" value="3.57"/>'
                '<filament id="1" used_g="3.57"/></plate>'
                '<plate><metadata key="prediction" value="2339"/><metadata key="weight" value="3.45"/>'
                '<filament id="2" used_g="2.95"/><filament id="1" used_g="0.5"/></plate></config>'
            ))
        return _FakeCompletedProcess(0)

    environment.runner.cli_handler = two_plates
    estimate = estimate_print(project, environment.slicer_exe)
    _expect(estimate == {"minutes": 66, "grams": 7.0, "plates": 2, "filament_grams": [4.1, 3.0]},
            "every plate counts, also for a filament on two plates: {}".format(estimate))


def _check_printer_nozzles(environment: _TestEnvironment) -> None:
    """The installed nozzles' flow types go to the right extruders, only when they fit."""
    from .printer_model import NozzleInfo

    project = environment.root / "nozzles" / "project.3mf"
    project.parent.mkdir(parents=True, exist_ok=True)

    def write(settings: Dict[str, Any]) -> None:
        with zipfile.ZipFile(str(project), "w") as archive:
            archive.writestr(_PROJECT_SETTINGS_PART, json.dumps(settings))

    h2d = {"nozzle_diameter": ["0.4", "0.4"], "physical_extruder_map": ["1", "0"],
           "nozzle_volume_type": ["Standard", "Standard"], "extruder_nozzle_stats": ["Standard#1", "Standard#1"]}
    write(h2d)
    right_high_flow = (NozzleInfo(0, 0.4, "High Flow"), NozzleInfo(1, 0.4, "Standard"))
    _expect(apply_printer_nozzles(project, right_high_flow) == ["Standard", "High Flow"],
            "slicer extruder 1 is the left nozzle (printer id 1), extruder 2 the right one")
    written = json.loads(_read_archive_entry(project, _PROJECT_SETTINGS_PART))
    _expect(written["nozzle_volume_type"] == ["Standard", "High Flow"]
            and written["extruder_nozzle_stats"] == ["Standard#1", "High Flow#1"], "the project and its stats: {}".format(written))
    for nozzles, why in (
        ((NozzleInfo(0, 0.6, "High Flow"), NozzleInfo(1, 0.4, "Standard")), "a nozzle of another size"),
        ((NozzleInfo(0, 0.4, ""), NozzleInfo(1, 0.4, "Standard")), "an unknown flow"),
        ((NozzleInfo(0, 0.4, "High Flow"),), "one nozzle for two extruders"),
    ):
        write(h2d)
        _expect(apply_printer_nozzles(project, nozzles) == []
                and json.loads(_read_archive_entry(project, _PROJECT_SETTINGS_PART)) == h2d,
                "nothing changes for {}".format(why))
    write({"nozzle_diameter": ["0.4"], "nozzle_volume_type": ["Standard"]})
    _expect(apply_printer_nozzles(project, (NozzleInfo(0, 0.4, "High Flow"),)) == ["High Flow"], "one nozzle")


def _check_filament_types(environment: _TestEnvironment) -> None:
    """A preset's exact type is kept when it is a variant of the material's type."""
    _expect(_is_type_variant("ABS-GF", "ABS") and _is_type_variant("PLA", "pla"), "ABS-GF is an ABS; PLA is PLA")
    _expect(not _is_type_variant("PLA", "PETG") and not _is_type_variant("ABSX", "ABS"), "a stand-in preset is not a variant")
    project = environment.root / "types" / "project.3mf"
    _write_prepared_3mf(project)
    materials = [
        {"color_hex": "#000000", "filament_type": "ABS", "display_name": "ABS-GF Black"},
        {"color_hex": "#FFFFFF", "filament_type": "PETG", "display_name": "PETG White"},
    ]
    settings = _postprocess_project_settings(project, materials, preset_types=["ABS-GF", "PLA"])
    _expect(
        settings["filament_type"] == ["ABS-GF", "PETG"],
        "the preset's ABS-GF is kept, a PLA stand-in for PETG is not: {}".format(settings["filament_type"]),
    )


def _check_slicer_names(environment: _TestEnvironment) -> None:
    """Slicer kind and display name come from the executable or app path."""
    cases = (
        ("/Applications/BambuStudio.app/Contents/MacOS/BambuStudio", "bambu", "Bambu Studio"),
        ("/Applications/OrcaSlicer.app", "orca", "OrcaSlicer"),
        ("C:\\Program Files\\OrcaSlicer\\orca-slicer.exe", "orca", "OrcaSlicer"),
        ("/Applications/Original Prusa Drivers/PrusaSlicer.app/Contents/MacOS/PrusaSlicer", "", "PrusaSlicer"),
        ("C:\\Tools\\SuperSlicer\\superslicer.exe", "", "superslicer"),
        ("", "", "the slicer"),
    )
    for path, kind, name in cases:
        _expect(slicer_kind(path) == kind, "kind of {!r}: {!r}".format(path, slicer_kind(path)))
        _expect(slicer_name(path) == name, "name of {!r}: {!r}".format(path, slicer_name(path)))


def _check_nozzle_map(environment: _TestEnvironment) -> None:
    """Each filament is pinned to the nozzle its slot feeds (two-nozzle printers only)."""
    plates = (
        '<plate><metadata key="plater_id" value="1"/>'
        '<metadata key="filament_map_mode" value="Auto For Flush"/></plate>'
        '<plate><metadata key="plater_id" value="2"/></plate>'
    )

    def project(name: str, diameters: List[str]) -> Path:
        path = environment.root / "nozzles" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        settings = {
            "nozzle_diameter": diameters,
            "filament_colour": ["#000000", "#0ACC38"],
            "filament_map": ["1", "1"],
            "filament_map_mode": "Auto For Flush",
        }
        with zipfile.ZipFile(str(path), "w") as archive:
            archive.writestr(_PROJECT_SETTINGS_PART, json.dumps(settings))
            archive.writestr(_MODEL_SETTINGS_PART, "<config>{}</config>".format(plates))
        return path

    def read(path: Path) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
        with zipfile.ZipFile(str(path), "r") as archive:
            settings = json.loads(archive.read(_PROJECT_SETTINGS_PART).decode("utf-8"))
            root = ET.fromstring(archive.read(_MODEL_SETTINGS_PART))
        return settings, [
            {meta.get("key"): meta.get("value") for meta in plate.findall("metadata")}
            for plate in root.findall("plate")
        ]

    h2d = project("h2d.3mf", ["0.4", "0.4"])
    _expect(apply_nozzle_map(h2d, [NOZZLE_RIGHT, NOZZLE_RIGHT]), "both AMS 2 slots feed the right nozzle")
    settings, plate_values = read(h2d)
    _expect(
        settings["filament_map"] == ["2", "2"] and settings["filament_map_mode"] == "Manual",
        "the project must pin both filaments to nozzle 2: {}".format(settings),
    )
    _expect(
        all(plate.get("filament_map_mode") == "Manual" and plate.get("filament_maps") == "2 2" for plate in plate_values),
        "every plate must use the manual map: {}".format(plate_values),
    )
    mixed = project("mixed.3mf", ["0.4", "0.4"])
    _expect(apply_nozzle_map(mixed, [NOZZLE_LEFT, NOZZLE_RIGHT]), "a left and a right slot")
    _expect(read(mixed)[0]["filament_map"] == ["1", "2"], "left is 1 and right is 2")
    _expect(
        _filament_map_values({"physical_extruder_map": ["1", "0"]}) == {NOZZLE_LEFT: "1", NOZZLE_RIGHT: "2"}
        and _filament_map_values({"physical_extruder_map": ["0", "1"]}) == {NOZZLE_LEFT: "2", NOZZLE_RIGHT: "1"}
        and _filament_map_values({}) == {NOZZLE_LEFT: "1", NOZZLE_RIGHT: "2"},
        "the side numbers follow physical_extruder_map",
    )
    # Bambu Lab H2C: no TPU on the left extruder, so a TPU slot on the left is not pinned.
    tpu = project("h2c_tpu.3mf", ["0.4", "0.4"])
    with zipfile.ZipFile(str(tpu), "r") as archive:
        tpu_settings = json.loads(archive.read(_PROJECT_SETTINGS_PART))
    tpu_settings.update(filament_type=["PLA", "TPU"], unprintable_filament_types=["TPU", "PPS-CF,PPA-CF"])
    rewrite_zip_entries(tpu, {_PROJECT_SETTINGS_PART: json.dumps(tpu_settings).encode("utf-8")})
    _expect(not apply_nozzle_map(tpu, [NOZZLE_RIGHT, NOZZLE_LEFT]), "TPU on the H2C's left nozzle is refused")
    _expect(read(tpu)[0]["filament_map_mode"] == "Auto For Flush", "a refused map leaves the project as it was")
    _expect(apply_nozzle_map(tpu, [NOZZLE_LEFT, NOZZLE_RIGHT]), "TPU on the right nozzle is fine")
    _expect(
        orca_supports_printer("Bambu Lab H2C 0.4 nozzle") and not orca_supports_printer("Bambu Lab H3C 0.4 nozzle"),
        "OrcaSlicer 2.5 has no H3C",
    )

    unknown = project("unknown.3mf", ["0.4", "0.4"])
    _expect(not apply_nozzle_map(unknown, [NOZZLE_RIGHT, None]), "an unknown nozzle leaves the choice to the slicer")
    _expect(read(unknown)[0]["filament_map_mode"] == "Auto For Flush", "the project must stay unchanged")
    one_nozzle = project("x1c.3mf", ["0.4"])
    _expect(not apply_nozzle_map(one_nozzle, [NOZZLE_LEFT, NOZZLE_LEFT]), "a one-nozzle printer has no nozzle map")
    try:
        apply_nozzle_map(project("count.3mf", ["0.4", "0.4"]), [NOZZLE_RIGHT])
    except SlicerProfilesError as exc:
        _expect("2 filament(s) but 1 nozzle(s)" in str(exc), "unexpected error: {}".format(exc))
    else:
        raise AssertionError("a nozzle list of the wrong length must be refused")


def _check_standalone_bambu_presets(environment: _TestEnvironment) -> None:
    """Bambu Studio system presets reach the CLI merged with their parents."""
    filament_dir = environment.bambu_system / "filament"
    _write_profile_json(
        filament_dir / "fdm_filament_abs.json",
        {"name": "fdm_filament_abs", "type": "filament", "filament_density": ["1.04"],
         "nozzle_temperature": ["250"], "inherits": "fdm_filament_common"},
    )
    _write_profile_json(
        filament_dir / "fdm_filament_common.json",
        {"name": "fdm_filament_common", "type": "filament", "filament_diameter": ["1.75"],
         "nozzle_temperature": ["200"]},
    )
    _write_profile_json(
        filament_dir / "dual_template.json",
        {"name": "fdm_filament_template_direct_dual", "type": "filament", "filament_extruder_variant": ["A", "B"]},
    )
    leaf = _write_profile_json(
        filament_dir / "Bambu ABS-GF @BBL H2D.json",
        {"name": "Bambu ABS-GF @BBL H2D", "type": "filament", "from": "system",
         "inherits": "fdm_filament_abs", "include": ["fdm_filament_template_direct_dual"],
         "nozzle_temperature": ["270"]},
    )
    standalone = Path(_standalone_bambu_preset(leaf))
    _expect(standalone != leaf and standalone.is_file(), "an inheriting system preset must be copied")
    data = json.loads(standalone.read_text(encoding="utf-8"))
    _expect(
        data.get("filament_diameter") == ["1.75"]
        and data.get("filament_density") == ["1.04"]
        and data.get("filament_extruder_variant") == ["A", "B"]
        and data.get("nozzle_temperature") == ["270"],
        "parents, includes and the preset's own keys must merge in that order: {}".format(data),
    )
    _expect(
        data.get("from") == "user" and "inherits" not in data and "include" not in data,
        "the copy must stand alone: {}".format(data),
    )
    for_bambu = json.loads(Path(_standalone_bambu_preset(leaf, for_bambu_studio=True)).read_text(encoding="utf-8"))
    _expect(
        for_bambu.get("inherits") == "Bambu ABS-GF @BBL H2D"
        and for_bambu.get("from") == "user"
        and for_bambu.get("filament_diameter") == ["1.75"],
        "Bambu Studio's merged copy must name itself in inherits: {}".format(for_bambu),
    )
    plain = filament_dir / "Bambu PLA Basic @BBL X1C.json"
    plain_copy = json.loads(Path(_standalone_bambu_preset(plain)).read_text(encoding="utf-8"))
    _expect(
        plain_copy.get("name") == "Bambu PLA Basic @BBL X1C" and plain_copy.get("from") == "user",
        "a system preset without parents is still copied as a user preset: {}".format(plain_copy),
    )
    elsewhere = _write_profile_json(
        environment.root / "imported" / "mine.json",
        {"name": "Mine", "type": "filament", "inherits": "Somewhere Else"},
    )
    _expect(_standalone_bambu_preset(elsewhere) == str(elsewhere), "presets outside Bambu Studio are untouched")
    broken = _write_profile_json(
        filament_dir / "broken.json", {"name": "Broken", "type": "filament", "inherits": "Missing Parent"}
    )
    try:
        _standalone_bambu_preset(broken)
    except SlicerProfilesError as exc:
        _expect("Missing Parent" in str(exc), "the error must name the missing parent: {}".format(exc))
    else:
        raise AssertionError("a missing parent must be reported")
    for path in (filament_dir / "fdm_filament_abs.json", filament_dir / "fdm_filament_common.json",
                 filament_dir / "dual_template.json", leaf, broken):
        path.unlink()


def _slot_fixture(
    path: Path, assignments: str, colours: Optional[List[str]] = None
) -> Path:
    """Write a 3MF whose model_settings holds ``assignments`` (and ``colours``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(path), "w") as archive:
        archive.writestr(_MODEL_SETTINGS_PART, "<config>{}</config>".format(assignments))
        if colours is not None:
            archive.writestr(
                _PROJECT_SETTINGS_PART, json.dumps({"filament_colour": colours}).encode("utf-8")
            )
    return path


def _check_slot_assignments(environment: _TestEnvironment) -> None:
    """Every body must print with the slot and colour Fusion gave it."""

    def part(name: str, slot: Optional[int]) -> str:
        extruder = '<metadata key="extruder" value="{}"/>'.format(slot) if slot else ""
        return '<part id="1"><metadata key="name" value="{}"/>{}</part>'.format(name, extruder)

    # Two components: the main body is black ABS-GF (slot 1), the TEF part is green (slot 2).
    fusion = [
        {"display_name": "ABS-GF Black", "color_hex": "#000000", "slot": 1, "body_names": ("Body1",)},
        {"display_name": "PolyLite Green", "color_hex": "#0acc38", "slot": 2, "body_names": ("Body1 (TEF)",)},
    ]
    assembled = '<object id="3"><metadata key="name" value="Assembly"/>{}{}</object>'.format(
        part("Body1 (TEF)", 2), part("Body1", 1)
    )
    good = _slot_fixture(environment.root / "slots" / "good.3mf", assembled, ["#000000", "#0ACC38"])
    _expect(check_slot_assignments(good, fusion) == 2, "both bodies must be checked")

    swapped_colours = _slot_fixture(
        environment.root / "slots" / "colours.3mf", assembled, ["#0ACC38", "#000000"]
    )
    try:
        check_slot_assignments(swapped_colours, fusion)
    except SlicerProfilesError as exc:
        _expect("slot 1 is #0ACC38" in str(exc), "the colour error must name the slot: {}".format(exc))
    else:
        raise AssertionError("swapped slot colours must be refused")

    # The wrong test files: TEF on slot 2, but slot 2 held the black filament.
    wrong_order = [
        dict(fusion[1], slot=1, body_names=("Body1 (TEF)",)),
        dict(fusion[0], slot=2, body_names=("Body1",)),
    ]
    try:
        check_slot_assignments(good, wrong_order)
    except SlicerProfilesError as exc:
        _expect("'Body1 (TEF)' prints with slot 2" in str(exc), "body error must name the body: {}".format(exc))
    else:
        raise AssertionError("a body on the wrong slot must be refused")

    plain = _slot_fixture(
        environment.root / "slots" / "plain.3mf",
        '<object id="1"><metadata key="name" value="Body1 (TEF)"/>'
        '<metadata key="extruder" value="2"/></object>'
        '<object id="2"><metadata key="name" value="Body1"/>{}</object>'.format(part("Body1", None)),
    )
    _expect(
        check_slot_assignments(plain, fusion) == 2,
        "a plain 3MF is checked by extruder only; a part without one uses its object's slot",
    )
    renamed = _slot_fixture(environment.root / "slots" / "renamed.3mf", part("Shape 1", 1))
    try:
        check_slot_assignments(renamed, fusion)
    except SlicerProfilesError as exc:
        _expect("none of the Fusion bodies" in str(exc), "unexpected error: {}".format(exc))
    else:
        raise AssertionError("a 3MF without any Fusion body must be refused")
    _expect(
        check_slot_assignments(good, [{"color_hex": "#000000"}, {"color_hex": "#0ACC38"}]) == 0,
        "materials without body names only check the colours",
    )

    records: Dict[str, Any] = {}
    environment.runner.cli_handler = _prepare_command_handler(
        records,
        model_settings="<config>{}</config>".format(
            assembled.replace('value="2"', 'value="3"')
        ).encode("utf-8"),
    )
    materials = [
        dict(environment.materials[0], body_names=("Body1",)),
        dict(environment.materials[1], body_names=("Body1 (TEF)",)),
    ]
    try:
        prepare_project(
            _write_fixture_3mf(environment.root / "slots" / "base.3mf"),
            environment.root / "slots" / "out" / "project.3mf",
            _local_target(environment),
            environment.slicer_exe,
            materials,
        )
    except SlicerProfilesError as exc:
        _expect("'Body1 (TEF)' prints with slot 3" in str(exc), "unexpected error: {}".format(exc))
    else:
        raise AssertionError("prepare_project must refuse a slicer output with moved slots")


def _check_layout_flags(environment: _TestEnvironment) -> None:
    """Touching bodies are assembled; separate bodies may share a plate."""

    def write_model(path: Path, boxes: Sequence[Tuple[float, ...]], offset: str = "") -> Path:
        objects = []
        items = []
        for index, (x0, y0, z0, x1, y1, z1) in enumerate(boxes, start=1):
            vertices = "".join(
                '<vertex x="{}" y="{}" z="{}"/>'.format(x, y, z)
                for x in (x0, x1)
                for y in (y0, y1)
                for z in (z0, z1)
            )
            objects.append(
                '<object id="{}" type="model"><mesh><vertices>{}</vertices>'
                "<triangles/></mesh></object>".format(index, vertices)
            )
            transform = ' transform="1 0 0 0 1 0 0 0 1 {}"'.format(offset) if offset and index == 2 else ""
            items.append('<item objectid="{}"{}/>'.format(index, transform))
        model = (
            '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            "<resources>{}</resources><build>{}</build></model>".format("".join(objects), "".join(items))
        )
        with zipfile.ZipFile(str(path), "w") as archive:
            archive.writestr("3D/3dmodel.model", model)
        return path

    with tempfile.TemporaryDirectory(prefix="filamatch_layout_") as folder:
        root = Path(folder)
        plate = (-18.0, -18.0, 0.0, 18.0, 18.0, 3.0)
        boss = (-13.0, -13.0, 3.0, 13.0, 13.0, 8.0)
        far = (40.0, 40.0, 0.0, 50.0, 50.0, 5.0)
        _expect(
            _layout_flags(write_model(root / "touch.3mf", [plate, boss])) == ["--assemble"],
            "stacked bodies must be assembled into one multi-colour object",
        )
        _expect(
            _layout_flags(write_model(root / "apart.3mf", [plate, far]))
            == ["--allow-multicolor-oneplate"],
            "separate bodies must be arranged with several colours on one plate",
        )
        _expect(
            _layout_flags(write_model(root / "mixed.3mf", [plate, boss, far]))
            == ["--allow-multicolor-oneplate"],
            "bodies that do not all touch must not be merged into one object",
        )
        _expect(
            _layout_flags(write_model(root / "moved.3mf", [plate, plate], "100 0 0"))
            == ["--allow-multicolor-oneplate"],
            "item transforms must be applied before checking contact",
        )
        _expect(
            _layout_flags(write_model(root / "single.3mf", [plate])) == [],
            "a single body needs no layout flag",
        )
        _expect(_layout_flags(root / "missing.3mf") == [], "an unreadable file needs no flag")


def _check_network_failfast(environment: _TestEnvironment) -> None:
    """Listings use a short timeout, stop at the first connection failure and
    remember missing files/tags for the session."""
    calls: List[Tuple[str, float]] = []

    def offline_opener(url: str, timeout: float) -> bytes:
        calls.append((url, timeout))
        raise urllib.error.URLError("simulated: no route to host")

    previous_root, previous_opener = _module._CACHE_ROOT_OVERRIDE, _module._url_opener
    _module._CACHE_ROOT_OVERRIDE = environment.root / "empty_cache"
    _module._url_opener = offline_opener
    _reset_remote_state()
    try:
        vendors = list_vendors(environment.slicer_exe)
    finally:
        _module._CACHE_ROOT_OVERRIDE, _module._url_opener = previous_root, previous_opener
    _expect(len(calls) == 1, "only one request may run after a connection failure, got {}".format(len(calls)))
    _expect(calls[0][1] == LIST_NETWORK_TIMEOUT_SECONDS, "listings must use the short timeout")
    _expect(
        not any(vendor["has_printers"] for vendor in vendors if vendor["source"] == "orca_remote"),
        "offline remote vendors must be listed as unavailable",
    )

    # A 404 is cached per URL: the second listing does not ask for the tag again.
    _reset_remote_state()
    _GOOD_REFS.add("v2.5.0-rc")
    tag_url = environment._remote_url("v2.5.0-rc", "Prusa.json")
    before = environment.opener_calls.count(tag_url)
    list_printers(environment.slicer_exe, "Prusa")
    list_printers(environment.slicer_exe, "Prusa")
    _expect(
        environment.opener_calls.count(tag_url) - before == 1,
        "a missing file must only be requested once per session",
    )

    # A tag that does not exist is skipped after the first 404.
    _reset_remote_state()
    _VERSION_CACHE.clear()
    environment.runner.help_text = "OrcaSlicer-9.9.9"
    try:
        list_vendors(environment.slicer_exe)
        missing_tag_calls = [url for url in environment.opener_calls if "/v9.9.9/" in url]
        _expect(len(missing_tag_calls) == 1, "a missing tag must only be tried once, got {}".format(missing_tag_calls))
        _expect(_candidate_refs(environment.slicer_exe) == ["main"], "a missing tag must be skipped")

        # Bambu Studio versions never have an OrcaSlicer tag.
        _VERSION_CACHE.clear()
        environment.runner.help_text = "BambuStudio-02.07.01.62"
        _expect(_candidate_refs(environment.slicer_exe) == ["main"], "Bambu Studio must not try a tag")
    finally:
        environment.runner.help_text = "OrcaSlicer-2.5.0-rc"
        _VERSION_CACHE.clear()
        _reset_remote_state()


def _check_version_cache(environment: _TestEnvironment) -> None:
    """A failed --help probe is not cached; a good one is keyed by path + mtime."""

    def failing_runner(command: Sequence[str], timeout: float, cwd: Optional[str] = None) -> Any:
        raise subprocess.TimeoutExpired(list(command), timeout)

    _VERSION_CACHE.clear()
    previous = _module._command_runner
    _module._command_runner = failing_runner
    try:
        _expect(_slicer_version(environment.slicer_exe) == "", "a failed probe has no version")
    finally:
        _module._command_runner = previous
    _expect(not _VERSION_CACHE, "a failed probe must not be cached")
    _expect(_slicer_version(environment.slicer_exe) == "2.5.0-rc", "the probe must be retried")
    keys = list(_VERSION_CACHE)
    _expect(
        len(keys) == 1 and keys[0][0] == str(environment.slicer_exe) and keys[0][1] is not None,
        "the cache key must be (path, mtime), got {}".format(keys),
    )


def _check_path_safety(environment: _TestEnvironment) -> None:
    """Remote paths/refs cannot escape the cache and downloads are size-capped."""
    for unsafe in ("../x.json", "BBL/../../x.json", "D:/x.json", "BBL/a:b.json", "BBL/a\x00.json"):
        try:
            _remote_cache_path("main", unsafe)
        except SlicerProfilesError:
            continue
        raise AssertionError("unsafe remote path {!r} must be rejected".format(unsafe))
    for unsafe_ref in ("..", "v1/../..", "C:", ""):
        try:
            _remote_cache_path(unsafe_ref, "BBL.json")
        except SlicerProfilesError:
            continue
        raise AssertionError("unsafe ref {!r} must be rejected".format(unsafe_ref))
    _expect(
        _remote_cache_path("main", "/BBL/machine/a b.json").parts[-3:] == ("BBL", "machine", "a b.json"),
        "a leading slash is made relative",
    )

    class _HugeResponse:
        def __enter__(self) -> "_HugeResponse":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return b"x" * (size if size >= 0 else MAX_DOWNLOAD_BYTES * 2)

    previous_urlopen = urllib.request.urlopen
    urllib.request.urlopen = lambda *_args, **_kwargs: _HugeResponse()  # type: ignore[assignment]
    try:
        _default_url_opener("https://example.invalid/huge.json", 1)
    except SlicerProfilesError as exc:
        _expect("larger than" in str(exc), "the size cap error must say why")
    else:
        raise AssertionError("an oversized download must be refused")
    finally:
        urllib.request.urlopen = previous_urlopen  # type: ignore[assignment]


def _check_windows_executable_filter(environment: _TestEnvironment) -> None:
    """On Windows only .exe files count as launchable (not OrcaSlicer.dll)."""
    folder = environment.root / "win_install"
    folder.mkdir(exist_ok=True)
    dll = folder / "OrcaSlicer.dll"
    exe = folder / "orca-slicer.exe"
    dll.write_bytes(b"MZ")
    exe.write_bytes(b"MZ")
    _expect(not _is_launchable_file(dll, windows=True), "a DLL must not be launchable on Windows")
    _expect(_is_launchable_file(exe, windows=True), "an .exe must be launchable on Windows")
    _expect(not _is_launchable_file(folder, windows=True), "a folder is never launchable")


def _check_printer_compatibility(environment: _TestEnvironment) -> None:
    """compatible_printers wins; names are matched by whole tokens."""
    _expect(_printer_token("Bambu Lab A1 0.4 nozzle") == "A1", "A1 must not become A1M")
    _expect(_printer_token("Bambu Lab A1 mini 0.4 nozzle") == "A1M", "A1 mini maps to A1M")
    _expect(_printer_token("Bambu Lab X1 Carbon 0.4 nozzle") == "X1C", "X1 Carbon maps to X1C")
    _expect(_printer_token("Bambu Lab H2D Pro 0.4 nozzle") == "H2DP", "H2D Pro maps to H2DP")
    _expect(_printer_token("Prusa MK4S 0.4 nozzle") == "", "MK4 must not match MK4S")

    processes = [
        {"name": "0.20mm Standard @BBL A1", "process_path": "a1", "default": False},
        {"name": "0.20mm Standard @BBL A1M", "process_path": "a1m", "default": False},
    ]
    a1 = [row["name"] for row in _filter_compatible_processes(processes, "Bambu Lab A1 0.4 nozzle")]
    _expect(a1 == ["0.20mm Standard @BBL A1"], "A1 must not list A1M processes, got {}".format(a1))
    mini = [row["name"] for row in _filter_compatible_processes(processes, "Bambu Lab A1 mini 0.4 nozzle")]
    _expect(mini == ["0.20mm Standard @BBL A1M"], "A1 mini must list A1M processes, got {}".format(mini))

    listed = [
        {
            "name": "0.20mm Standard @BBL X1C",
            "process_path": "x1c",
            "default": False,
            "compatible_printers": ["Bambu Lab X1 Carbon 0.4 nozzle", "Bambu Lab P1S 0.4 nozzle"],
        },
        {
            "name": "0.20mm Standard @BBL P1P",
            "process_path": "p1p",
            "default": False,
            "compatible_printers": ["Bambu Lab P1P 0.4 nozzle"],
        },
    ]
    p1s = _filter_compatible_processes(listed, "Bambu Lab P1S 0.4 nozzle")
    _expect([row["name"] for row in p1s] == ["0.20mm Standard @BBL X1C"], "compatible_printers must decide")
    _expect("compatible_printers" not in p1s[0], "the internal list must not leak into the rows")

    candidates = [
        {
            "path": "listed.json",
            "name": "Bambu PLA Basic @BBL X1C",
            "file_name": "listed.json",
            "types": {"PLA"},
            "compatible_printers": ("bambu lab x1 carbon 0.4 nozzle", "bambu lab p1s 0.4 nozzle"),
        },
        {
            "path": "a1m.json",
            "name": "Bambu PLA Basic @BBL A1M",
            "file_name": "a1m.json",
            "types": {"PLA"},
            "compatible_printers": (),
        },
    ]
    _expect(
        _pick_filament_candidate(candidates, "PLA", "Bambu Lab P1S 0.4 nozzle") == "listed.json",
        "a filament listing the printer must be chosen",
    )
    _expect(
        _pick_filament_candidate(candidates, "PLA", "Bambu Lab A1 0.4 nozzle") is None,
        "an A1M filament must not be picked for an A1",
    )


def _check_zip_rewrite_and_log(environment: _TestEnvironment) -> None:
    """One archive rewrite replaces/adds/drops entries and keeps metadata;
    the slicer log lives in the cache and is truncated before each run."""
    archive_path = environment.root / "rewrite.3mf"
    with zipfile.ZipFile(str(archive_path), "w") as archive:
        info = zipfile.ZipInfo("3D/3dmodel.model", (2024, 1, 2, 3, 4, 6))
        info.internal_attr = 1
        info.external_attr = 0o600 << 16
        archive.writestr(info, b"<model/>")
        archive.writestr(_PROJECT_SETTINGS_PART, b"{}")
        archive.writestr("Metadata/legacy.json", b"old")
    rewrite_zip_entries(
        archive_path,
        {_PROJECT_SETTINGS_PART: b'{"a": 1}', "Metadata/legacy.json": None, "Metadata/new.json": b"new"},
        required=(_PROJECT_SETTINGS_PART,),
    )
    with zipfile.ZipFile(str(archive_path)) as archive:
        names = archive.namelist()
        model_info = archive.getinfo("3D/3dmodel.model")
        _expect(
            names == ["3D/3dmodel.model", _PROJECT_SETTINGS_PART, "Metadata/new.json"],
            "rewrite must replace in place, drop and append, got {}".format(names),
        )
        _expect(model_info.internal_attr == 1, "internal_attr must be preserved")
        _expect(model_info.external_attr == 0o600 << 16, "external_attr must be preserved")
        _expect(model_info.date_time == (2024, 1, 2, 3, 4, 6), "date_time must be preserved")
        _expect(archive.read(_PROJECT_SETTINGS_PART) == b'{"a": 1}', "replacement payload mismatch")
    try:
        rewrite_zip_entries(archive_path, {"missing": b""}, required=("missing",))
    except SlicerProfilesError:
        pass
    else:
        raise AssertionError("a missing required entry must raise")
    _expect(
        not list(environment.root.glob(".filamatch_zip_*")),
        "temporary rewrite files must be cleaned up",
    )

    log_path = _cache_root() / "logs" / "slicer_cli.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("OLD LOG FROM AN EARLIER RUN\n", encoding="utf-8")
    base_3mf = _write_fixture_3mf(environment.root / "log_base.3mf")

    def cannot_start(command: Sequence[str], timeout: float, cwd: Optional[str] = None) -> Any:
        if len(command) >= 2 and command[1] == "--help":
            return _FakeCompletedProcess(0, "OrcaSlicer-2.5.0-rc\n", "")
        raise OSError("simulated: cannot start")

    previous = _module._command_runner
    _module._command_runner = cannot_start
    try:
        prepare_project(
            base_3mf,
            environment.root / "log_out" / "project.3mf",
            _local_target(environment),
            environment.slicer_exe,
            environment.materials,
        )
    except SlicerProfilesError as exc:
        _expect("OLD LOG" not in str(exc), "an old slicer log must never be shown")
        _expect(str(log_path) in str(exc), "the default log must live in the add-in cache")
    else:
        raise AssertionError("a slicer that cannot start must raise")
    finally:
        _module._command_runner = previous
    _expect(not (environment.root / "log_out" / "slicer_cli.log").exists(), "no log in the output folder")


def _selftest() -> int:
    """Run the slicer-profiles self-test with fake CLI/network/filesystem hooks.

    Returns:
        Process exit code: 0 when every check passed, 1 otherwise.
    """
    scenarios = (
        (
            "find_installed_slicers parses versions from --help",
            _check_find_installed_slicers,
        ),
        (
            "local vendor/printer/process discovery filters base presets",
            _check_local_discovery,
        ),
        (
            "macOS .app bundles resolve to their executable",
            _check_app_bundle_resolution,
        ),
        (
            "remote listings and inheritance flattening work",
            _check_remote_listing_and_flattening,
        ),
        (
            "unresolved inheritance parents raise with the chain",
            _check_unresolved_parent_error,
        ),
        (
            "imported profiles import, validate and deduplicate",
            _check_imported_profiles,
        ),
        (
            "prepare_project local target builds the verified CLI command",
            _check_prepare_local,
        ),
        (
            "prepare_project remote target uses flattened user presets",
            _check_prepare_remote,
        ),
        (
            "CLI failures and bad projects raise with the log tail",
            _check_prepare_failure,
        ),
        ("cached remote manifests keep working offline", _check_offline_cache),
        ("filament selection matches types and falls back", _check_filament_fallback),
        ("database filaments get a built preset, spools their user preset", _check_database_presets),
        ("the installed nozzles' flow types go to the right extruders", _check_printer_nozzles),
        ("touching bodies are assembled, separate bodies share a plate", _check_layout_flags),
        (
            "the prime tower, build plate and nozzle flow types are fitted to the printer",
            _check_fit_wipe_tower,
        ),
        ("every body prints with the slot and colour Fusion gave it", _check_slot_assignments),
        ("Bambu Studio presets reach the CLI merged with their parents", _check_standalone_bambu_presets),
        ("each filament is pinned to the nozzle its slot feeds", _check_nozzle_map),
        ("slicer kind and name come from the app path", _check_slicer_names),
        ("a preset's exact filament type (ABS-GF) is kept", _check_filament_types),
        ("a copy is sliced for the print time and grams", _check_estimate_print),
        ("online printers get their vendor's own filament presets", _check_remote_filament_presets),
        ("mixed nozzle sizes keep the bridge width within the smallest", lambda environment: _check_bridge_line_width()),
        ("touching bodies are joined into one object with parts", _check_join_touching_bodies),
        ("projects sent to Bambu Studio use Bambu Studio's words", _check_bambu_studio_words),
        ("Bambu Studio prepares projects by itself", _check_prepare_bambu_studio),
        ("listings fail fast offline and cache missing files/tags", _check_network_failfast),
        ("failed version probes are not cached", _check_version_cache),
        ("remote paths stay in the cache and downloads are capped", _check_path_safety),
        ("Windows install folders only launch .exe files", _check_windows_executable_filter),
        ("printer compatibility uses lists and whole tokens", _check_printer_compatibility),
        ("one zip rewrite keeps metadata; slicer log is fresh and cached", _check_zip_rewrite_and_log),
    )
    try:
        with tempfile.TemporaryDirectory(prefix="filamatch_slicer_selftest_") as temp_dir:
            environment = _TestEnvironment(Path(temp_dir))
            environment.install()
            try:
                for label, scenario in scenarios:
                    scenario(environment)
                    print("PASS: {}".format(label))
            finally:
                environment.restore()
    except Exception:
        traceback.print_exc()
        print("FAIL: slicer_profiles self-test aborted", file=sys.stderr)
        return 1
    print("PASS: all slicer_profiles self-tests completed")
    return 0


main = _selftest


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
