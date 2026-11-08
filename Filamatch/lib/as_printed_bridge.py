"""The printed values card's Python side: each body's printed mass, stiffness and strength, the slicer check and the switch.

It came from the Printegrity add-in (its palette_bridge). Filamatch's
palette_bridge hands it every palette action it does not know
(:func:`dispatch`); it uses palette_bridge's helpers (``_send``,
``_run_job``, ``_active_design``, ``_resolve_profile_target``,
``log_message``) and Filamatch's own modules.

One source for each setting (docs/development.md):

- the defaults for bodies without their own settings are the chosen print
  process's values, which the page sends with each request
  (``process_values``, ``nozzle``); without them mass_model's built-in
  defaults apply;
- a body's own infill, walls and layers are Filamatch's body settings; the
  estimate uses the design's orientation (Z up), as the slicer does;
- the slicer check slices with Filamatch's Print target, the process as it
  is, with the body's own settings on top (slicer_check.body_overrides).

Stiffness and strength (strength_model): each filament body's type comes
from Filamatch's catalog (the page sends ``filament_types``, by material
name) or, for other materials, from the material's name. The case (safe or
typical) is kept per design (a design attribute).

The switch "Fusion uses the printed values" is kept per design (a design
attribute); it is off unless the attribute says "on". While it is on, each estimate gives every filament body its own
up-to-date as-printed copy (material_copy.apply), so Fusion's mass stays the
printed mass after a body's settings, the process or its shape change. Off
puts the filaments back. Every estimate and every switch deletes the
as-printed copies no body uses (material_copy.clean_up).

Actions (request -> reply):

- ``as_printed_estimate`` {process_values, nozzle} -> ``asPrintedMass``
- ``as_printed_slicer_check`` {process_values, nozzle} -> ``asPrintedSlicer`` (a job)
- ``as_printed_set_weight`` {on, process_values, nozzle, filament_types} -> ``asPrintedWeight``
- ``as_printed_set_case`` {case} -> ``asPrintedCase``

Every request may send ``filament_types``: {material name: {type, catalog}}.

Self-test with fake Fusion objects: ``python3 -m Filamatch.lib.as_printed_bridge``.
"""

from __future__ import annotations

import json
import logging
import types
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .. import config
from . import body_geometry
from . import body_settings
from . import mass_model
from . import material_copy
from . import palette_bridge as bridge
from . import slicer_check
from . import strength_model

LOGGER = logging.getLogger(config.ADDIN_NAME + ".as_printed")

#: The design attribute that keeps the switch ("on" / "off").
WEIGHT_ATTRIBUTE = ("Filamatch", "printed_weight")
#: The design attribute that keeps the strength case ("safe" / "typical").
CASE_ATTRIBUTE = ("Filamatch", "printed_strength")

#: The last slicer check, per body token: the settings key it was sliced with,
#: the volume it had and the density for Fusion. Apply uses it while the
#: body's settings and shape are unchanged.
_LAST_SLICER: Dict[str, Any] = {"bodies": {}}

NOT_A_FILAMENT = "The material is not a filament. Give the body a filament first."


class AsPrintedError(RuntimeError):
    """A problem that explains itself (no traceback in the log)."""


def defaults_from(payload: Mapping[str, Any]) -> Tuple[mass_model.PrintSettings, str]:
    """The defaults for bodies without their own settings, and where they come from.

    Returns:
        ``(settings, source)``: source is "process" when the page sent the
        print process's values, else "built-in".
    """
    values = payload.get("process_values")
    values = values if isinstance(values, Mapping) else {}
    settings = mass_model.PrintSettings.from_process(values, payload.get("nozzle"))
    return settings, "process" if values else "built-in"


def weight_on(design: Any) -> bool:
    """Is the switch on for this design? Only when its attribute says "on" (the default is off)."""
    try:
        attribute = design.attributes.itemByName(*WEIGHT_ATTRIBUTE)
    except Exception:
        attribute = None
    return attribute is not None and str(attribute.value) == "on"


def set_weight_attribute(design: Any, on: bool) -> None:
    value = "on" if on else "off"
    attribute = design.attributes.itemByName(*WEIGHT_ATTRIBUTE)
    if attribute is None:
        design.attributes.add(WEIGHT_ATTRIBUTE[0], WEIGHT_ATTRIBUTE[1], value)
    elif str(attribute.value) != value:
        attribute.value = value


def strength_case(design: Any) -> str:
    """The design's strength case: "typical" only when its attribute says so."""
    try:
        attribute = design.attributes.itemByName(*CASE_ATTRIBUTE)
    except Exception:
        attribute = None
    value = str(attribute.value) if attribute is not None else ""
    return value if value in strength_model.CASES else strength_model.DEFAULT_CASE


def set_case_attribute(design: Any, case: str) -> None:
    attribute = design.attributes.itemByName(*CASE_ATTRIBUTE)
    if attribute is None:
        design.attributes.add(CASE_ATTRIBUTE[0], CASE_ATTRIBUTE[1], case)
    elif str(attribute.value) != case:
        attribute.value = case


def _strength_data() -> Optional[strength_model.StrengthData]:
    """The bundled stiffness and strength data (None, logged, when it cannot be read)."""
    try:
        return strength_model.load()
    except Exception:
        LOGGER.exception("Could not read the stiffness and strength data")
        return None


def _product_texts(material: Any, info: Mapping[str, Any]) -> List[str]:
    """"<maker> <product>" spellings of a filament material, without its colour, for the data sheets.

    From a Filamatch material's description ("Filamatch | PA-CF | Bambu Lab -
    PAHT-CF - Black") and the page's display name ("Bambu Lab PAHT-CF - Black").
    """
    texts: List[str] = []
    description = str(getattr(material, "description", "") or "")
    parts = [part.strip() for part in description.split("|")]
    if description.startswith("Filamatch | ") and len(parts) > 2:
        named = [piece.strip() for piece in parts[2].split(" - ")]
        texts.append(" ".join(named[:2]))
    for text in (str(info.get("name") or ""), str(getattr(material, "name", "") or "")):
        pieces = [piece.strip() for piece in text.split(" - ")]
        texts.extend([text, " ".join(pieces[:-1]), " ".join(pieces[:2])])
    return [text for text in texts if text]


def strength_basis(data: strength_model.StrengthData, material: Any,
                   filament_types: Mapping[str, Any]) -> Tuple[Any, str]:
    """What a filament's stiffness and strength come from, and how it was found.

    Its own data sheet first (tier 2: "datasheet"), else its type's generic
    values (tier 1: "catalog" or "name"); ``(None, "")`` when there is neither.
    """
    info = filament_types.get(str(getattr(material, "name", "") or ""))
    info = info if isinstance(info, Mapping) else {}
    product = data.product_for(_product_texts(material, info))
    if product is not None:
        return product, "datasheet"
    return strength_type(data, material, filament_types)


def strength_type(data: strength_model.StrengthData, material: Any,
                  filament_types: Mapping[str, Any]) -> Tuple[Optional[str], str]:
    """The data type of a filament material, and how it was found ("catalog", "name" or "").

    Filamatch's catalog type comes first (the page's ``filament_types``, or
    the category in a Filamatch material's description); other materials go
    by their name ("ABS Plastic" is ABS).
    """
    name = str(getattr(material, "name", "") or "")
    description = str(getattr(material, "description", "") or "")
    info = filament_types.get(name)
    info = info if isinstance(info, Mapping) else {}
    candidates: List[Tuple[str, str]] = []
    if info.get("catalog") or description.startswith("Filamatch | "):
        if info.get("type"):
            candidates.append((str(info["type"]), "catalog"))
        parts = description.split("|")
        if len(parts) > 2:
            candidates.append((parts[1].strip(), "catalog"))
    candidates.append((name, "name"))
    for text, how in candidates:
        found = data.match(text)
        if found:
            return found, how
    return None, ""


def _target(entry: Any, geometry: Any, settings: mass_model.PrintSettings,
            estimate: mass_model.MassEstimate) -> Tuple[float, str]:
    """The density Fusion should get: the slicer's (same settings and volume), else the model's."""
    checked = _LAST_SLICER["bodies"].get(entry.token)
    if (checked and checked.get("density") and checked.get("key") == settings.key()
            and abs(checked["volume"] - geometry.volume_cm3) <= 1e-6 * max(1.0, geometry.volume_cm3)):
        return float(checked["density"]), "slicer"
    return estimate.effective_density_g_cm3, "model"


def survey(design: Any, defaults: mass_model.PrintSettings, filament_types: Optional[Mapping[str, Any]] = None
           ) -> Tuple[List[Dict[str, Any]], List[material_copy.ApplyItem], int]:
    """Every visible solid body's row, and what Apply would give each filament body.

    Each filament body with a known type also gets its stiffness and strength
    (the design's case).

    Returns:
        ``(rows, items, skipped)``: one row per body (a component placed
        several times is one row with ``copies``), one apply item per
        filament body that can be printed, and how many bodies were not
        measured (past the limit).
    """
    entries, skipped = body_geometry.design_bodies(design)
    uses = material_copy.use_counts(body_geometry.all_bodies(design))
    data = _strength_data()
    case = strength_case(design)
    filament_types = filament_types if isinstance(filament_types, Mapping) else {}
    rows: List[Dict[str, Any]] = []
    items: List[material_copy.ApplyItem] = []
    for entry in entries:
        settings = entry.settings(defaults)
        try:
            geometry = body_geometry.measure(entry.body, settings.build_direction, design)
        except Exception:
            LOGGER.exception("Could not measure a body")
            continue
        row: Dict[str, Any] = {
            "token": entry.token,
            "copies": entry.copies,
            "name": geometry.name,
            "component": body_geometry.component_name(entry.body),
            "path": geometry.path,
            "material": geometry.material,
            "filament": geometry.looks_like_filament,
            "filament_name": geometry.filament,
            "applied": geometry.applied,
            "applied_stale": False,
            "fusion_grams": geometry.fusion_grams,
            "volume_cm3": geometry.volume_cm3,
            "own_summary": body_settings.describe(entry.own) if entry.own else "",
            "label": settings.label(),
            "key": settings.key(),
            "notes": [],
            "strength": None,
        }
        rows.append(row)
        if not geometry.looks_like_filament:
            row["notes"] = [NOT_A_FILAMENT]
            continue
        if settings.problems():
            row["notes"] = list(settings.problems())
            continue
        result = mass_model.estimate(geometry.volume_cm3, geometry.histogram, settings, geometry.density_g_cm3)
        density, origin = _target(entry, geometry, settings, result)
        row.update(grams=result.grams, solid_grams=result.solid_grams,
                   effective_density_g_cm3=result.effective_density_g_cm3, notes=list(result.notes),
                   target_density_g_cm3=density, target_origin=origin)
        if geometry.volume_cm3 <= 0:
            continue
        values: Optional[Dict[str, float]] = None
        values_key = ""
        if data is not None:
            filament = material_copy.solid_material(design, entry.body.material)
            basis, how = strength_basis(data, filament, filament_types)
            if basis:
                strength = strength_model.estimate(data, basis, case, geometry.volume_cm3, result.shell_cm3,
                                                   settings.infill_percent, settings.infill_pattern,
                                                   settings.nozzle_mm, settings.layer_height_mm)
                row.update(strength=strength.to_dict(), strength_from=how)
                values = {key: getattr(strength, key) for key in ("e_mpa", "poisson", "shear_mpa", "yield_mpa",
                                                                  "tensile_mpa")}
                values_key = strength.key()
        needs = material_copy.needs_apply(entry.body.material, geometry.filament, settings.key(), settings.label(),
                                          density, origin, geometry.volume_cm3, uses, values_key)
        row["applied_stale"] = bool(geometry.applied and needs)
        items.append(material_copy.ApplyItem(
            body=entry.body, token=entry.token, body_label=body_geometry.body_label(entry.body), path=geometry.path,
            density_g_cm3=density, origin=origin, label=settings.label(), settings_key=settings.key(),
            volume_cm3=geometry.volume_cm3, values=values, values_key=values_key) if needs else None)
    return rows, [item for item in items if item is not None], skipped


def _reply(defaults: mass_model.PrintSettings, source: str) -> Dict[str, Any]:
    return {
        "ok": True,
        "defaults": {"label": defaults.label(), "source": source, "problems": defaults.problems(),
                     "settings": defaults.to_dict()},
        "bodies": [],
        "skipped": 0,
        "total_grams": 0.0,
        "weight_on": False,
        "case": strength_model.DEFAULT_CASE,
        "strength_types": {},
        "applied_now": [],
        "deleted": [],
    }


def _strength_types(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Where the values in use come from, by basis ("PLA (generic)", a product's data sheet)."""
    data = _strength_data()
    found: Dict[str, Any] = {}
    for row in rows:
        label = (row.get("strength") or {}).get("basis")
        base = data.basis(label) if data is not None and label else None
        if base is None or label in found:
            continue
        found[label] = {"kind": base.kind, "type": base.name, "sources": list(base.sources),
                        "uncertainty_percent": base.uncertainty_percent, "poisson": base.poisson,
                        "values": {prop: _range_dict(value) for prop, value in base.values.items()},
                        "conditioned": {prop: _range_dict(value) for prop, value in base.conditioned.items()},
                        "notes": list(base.notes)}
    return found


def _range_dict(value: Any) -> Dict[str, Any]:
    return {"median": value.median, "low": value.low, "high": value.high, "count": value.count}


def mass_payload(design: Any, payload: Mapping[str, Any], keep_weight: bool = False) -> Dict[str, Any]:
    """Every visible solid body's estimated printed mass, each with its own settings.

    With ``keep_weight`` (the estimate action), unused copies are deleted
    and, while the switch is on, every filament body that needs it gets its
    up-to-date copy first. The total counts every placed copy.
    """
    defaults, source = defaults_from(payload)
    reply = _reply(defaults, source)
    if defaults.problems() or design is None:
        return reply
    filament_types = payload.get("filament_types")
    rows, items, skipped = survey(design, defaults, filament_types)
    on = weight_on(design)
    if keep_weight:
        deleted = material_copy.clean_up(design)
        if on and items:
            results = [result.to_dict() for result in material_copy.apply(
                design, items, body_geometry.all_bodies(design))]
            reply["applied_now"] = results
            deleted += material_copy.clean_up(design)
            done = [result for result in results if not result["error"]]
            if done:
                bridge.log_message("Printed weight kept up to date on {} bod{}".format(
                    len(done), "y" if len(done) == 1 else "ies"))
            rows, _items, skipped = survey(design, defaults, filament_types)
        reply["deleted"] = deleted
    reply.update(bodies=rows, skipped=skipped, weight_on=on, case=strength_case(design),
                 strength_types=_strength_types(rows),
                 total_grams=sum(row.get("grams", 0.0) * row["copies"] for row in rows))
    return reply


def _handle_estimate(payload: Mapping[str, Any], palette: Any) -> None:
    try:
        bridge._send(palette, "asPrintedMass", mass_payload(bridge._active_design(), payload, keep_weight=True))
    except Exception as exc:
        LOGGER.exception("Could not estimate the printed mass")
        bridge._send_error(palette, "asPrintedMass", bridge._error_text(exc))


def _with_slicer(model: Mapping[str, Any], checked: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The model's rows with the slicer's answer added to the bodies it sliced."""
    answers = {body["token"]: body for body in checked.get("bodies", [])}
    rows = []
    for row in model.get("bodies", []):
        row = dict(row)
        answer = answers.get(row.get("token"))
        if answer is not None:
            row["slicer"] = dict(answer)
            if answer.get("grams") and row.get("grams"):
                row["slicer"]["model_difference_percent"] = (row["grams"] / answer["grams"] - 1.0) * 100.0
        rows.append(row)
    return rows


def _handle_slicer_check(payload: Mapping[str, Any], palette: Any) -> None:
    """Slice every filament body once (in the background) with the Print target."""
    try:
        design = bridge._active_design()
        model = mass_payload(design, payload)
        defaults, _source = defaults_from(payload)
        if defaults.problems():
            bridge._send(palette, "asPrintedSlicer", dict(model, ok=False, error=" ".join(defaults.problems())))
            return
        items, left_out = body_geometry.slicing_meshes(design, defaults)
        if not items:
            bridge._send(palette, "asPrintedSlicer", dict(model, ok=False, error="No filament bodies to slice in this design."))
            return
        entries, _more = body_geometry.design_bodies(design)
        own_by_token = {entry.token: entry.own for entry in entries}
        # As Filamatch prints: the process as it is, the body's own settings on top.
        jobs = [(mesh, settings, own_by_token.get(mesh.token, {})) for mesh, settings in items]
    except Exception as exc:
        LOGGER.exception("Could not start the slicer check")
        bridge._send_error(palette, "asPrintedSlicer", bridge._error_text(exc))
        return
    bridge.log_message("Slicer check started for {} bod{}".format(len(jobs), "y" if len(jobs) == 1 else "ies"))

    def work() -> Dict[str, Any]:
        _profiles, slicer_exe, profile = bridge._resolve_profile_target({})
        checked = slicer_check.check_bodies(jobs, profile, slicer_exe)
        checked["target"] = {"printer": profile.printer_name, "process": profile.process_name,
                             "slicer": Path(str(slicer_exe)).stem}
        return checked

    def finish(checked: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            bridge._send(palette, "asPrintedSlicer", dict(model, ok=False, error=str(error) or bridge._error_text(error)))
            return
        done = [body for body in checked["bodies"] if body.get("grams")]
        volumes = {mesh.token: mesh.volume_cm3 for mesh, _settings, _own in jobs}
        _LAST_SLICER["bodies"] = {
            body["token"]: {"key": body["key"], "density": body["density_g_cm3"],
                            "volume": volumes.get(body["token"], 0.0)}
            for body in done
        }
        bridge.log_message("Slicer check: {} of {} bod{} sliced ({}, {})".format(
            len(done), len(checked["bodies"]), "y" if len(checked["bodies"]) == 1 else "ies",
            checked["target"]["printer"], checked["target"]["process"]))
        reply = dict(model)
        reply.update(bodies=_with_slicer(model, checked), slicer_target=checked["target"],
                     slicer_notes=list(checked.get("notes", [])), left_out=left_out,
                     slicer_total_grams=sum(body["grams"] for body in done))
        bridge._send(palette, "asPrintedSlicer", reply)

    bridge._run_job("as_printed_slicer_check", work, finish)


def _handle_set_weight(payload: Mapping[str, Any], palette: Any) -> None:
    """The switch: on gives every filament body its own copy; off puts the filaments back."""
    on = bool(payload.get("on"))
    try:
        design = bridge._active_design()
        if design is None:
            raise AsPrintedError("Open a Fusion design first.")
        defaults, _source = defaults_from(payload)
        if on and defaults.problems():
            raise AsPrintedError(" ".join(defaults.problems()))
        set_weight_attribute(design, on)
        skipped: List[Dict[str, Any]] = []
        if on:
            rows, items, _more = survey(design, defaults, payload.get("filament_types"))
            skipped = [{"name": row["name"], "path": row["path"], "reason": " ".join(row["notes"])}
                       for row in rows if "grams" not in row]
            results = [result.to_dict() for result in material_copy.apply(
                design, items, body_geometry.all_bodies(design))]
        else:
            entries, _more = body_geometry.design_bodies(design, include_hidden=True)
            results = [result.to_dict() for result in material_copy.restore(
                design, [(entry.body, body_geometry.body_path(entry.body)) for entry in entries])]
        deleted = material_copy.clean_up(design)
        done = [result for result in results if not result["error"]]
        bridge.log_message("Printed weight {}: {} bod{} changed, {} unused cop{} deleted".format(
            "on" if on else "off", len(done), "y" if len(done) == 1 else "ies",
            len(deleted), "y" if len(deleted) == 1 else "ies"))
        bridge._send(palette, "asPrintedWeight", {"ok": True, "on": on, "bodies": results, "skipped": skipped,
                                                   "deleted": deleted})
    except AsPrintedError as exc:
        bridge._send_error(palette, "asPrintedWeight", str(exc))
    except Exception as exc:
        LOGGER.exception("Could not switch the printed weight")
        bridge._send_error(palette, "asPrintedWeight", bridge._error_text(exc))


def _handle_set_case(payload: Mapping[str, Any], palette: Any) -> None:
    """Keep the design's strength case; the next estimate gives Fusion the new values (switch on)."""
    case = str(payload.get("case") or "")
    try:
        if case not in strength_model.CASES:
            raise AsPrintedError("Unknown strength case {!r}.".format(case))
        design = bridge._active_design()
        if design is None:
            raise AsPrintedError("Open a Fusion design first.")
        set_case_attribute(design, case)
        bridge._send(palette, "asPrintedCase", {"ok": True, "case": case})
    except AsPrintedError as exc:
        bridge._send_error(palette, "asPrintedCase", str(exc))
    except Exception as exc:
        LOGGER.exception("Could not keep the strength case")
        bridge._send_error(palette, "asPrintedCase", bridge._error_text(exc))


#: Request -> handler(payload, palette).
_ACTIONS: Dict[str, Callable[[Mapping[str, Any], Any], None]] = {
    "as_printed_estimate": _handle_estimate,
    "as_printed_slicer_check": _handle_slicer_check,
    "as_printed_set_weight": _handle_set_weight,
    "as_printed_set_case": _handle_set_case,
}

#: Request -> reply, for the page (as_printed.js registers the same pairs).
REPLIES: Dict[str, str] = {
    "as_printed_estimate": "asPrintedMass",
    "as_printed_slicer_check": "asPrintedSlicer",
    "as_printed_set_weight": "asPrintedWeight",
    "as_printed_set_case": "asPrintedCase",
}


def dispatch(action: str, payload: Mapping[str, Any], palette: Any) -> bool:
    """Handle one of the card's actions; False for an action that is not the card's."""
    handler = _ACTIONS.get(action)
    if handler is None:
        return False
    handler(payload if isinstance(payload, Mapping) else {}, palette)
    return True


# ---------------------------------------------------------------------------
# Self-test with fake Fusion objects (no Fusion, no slicer)
# ---------------------------------------------------------------------------

_PASSED = 0
_FAILED = 0


def _check(condition: bool, label: str) -> None:
    """Record and print one PASS/FAIL check."""
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print("PASS {}".format(label))
    else:
        _FAILED += 1
        print("FAIL {}".format(label))


class _FakePalette:
    def __init__(self) -> None:
        self.sent: List[Any] = []

    def sendInfoToHTML(self, action: str, data: str) -> bool:
        self.sent.append((action, json.loads(data)))
        return True


def _self_test() -> int:
    design = body_geometry._fake_design()
    design.attributes = body_geometry._FakeAttributes()
    palette = _FakePalette()
    process = {"infill": 15, "infill_pattern": "grid", "walls": 2, "top_layers": 5, "bottom_layers": 3,
               "layer_height": 0.2, "outer_wall_width": "105%", "inner_wall_width": 0.45, "top_min_mm": 1.0}
    saved = (bridge._active_design, bridge._resolve_profile_target, slicer_check.check_bodies,
             material_copy.apply, material_copy.restore, material_copy.clean_up, _strength_data)
    bridge._active_design = lambda: design
    fixture = strength_model.parse(strength_model._fixture())
    globals()["_strength_data"] = lambda: fixture
    applied: List[Any] = []
    cleaned: List[Any] = []
    restored: List[Any] = []
    material_copy.apply = lambda d, items, bodies=(): applied.append(list(items)) or [
        material_copy.BodyResult(name=item.body.name, path=item.path, material="copy",
                                 density_g_cm3=item.density_g_cm3, source=item.origin) for item in items]
    material_copy.restore = lambda d, bodies: restored.append(list(bodies)) or [
        material_copy.BodyResult(name=b.name, path=p, material="filament") for b, p in bodies]
    material_copy.clean_up = lambda d: cleaned.append(True) or []
    try:
        settings = mass_model.PrintSettings.from_process(process)
        cube_body = body_geometry.visible_solid_bodies(design)[0]
        filament = cube_body.material
        cube_token = body_geometry.body_token(cube_body)
        copy = types.SimpleNamespace(
            name="Cube copy", description=material_copy.copy_description(
                filament.name, settings.label(), material_copy.new_copy_id(filament.name, cube_token), settings.key(),
                "model", 8.0),
            materialProperties=body_geometry._FakeMaterial("x", 400.0).materialProperties)
        cube_body.material = copy
        dispatch("as_printed_estimate", {"process_values": process}, palette)
        fresh = palette.sent[-1][1]
        _check(not fresh["weight_on"] and not applied and fresh["bodies"][0]["applied"] and cleaned
               and cube_body.material is copy,
               "the switch is off without its attribute, even when a body wears a copy: nothing is applied, "
               "the copy stays; unused copies are cleaned up")
        set_weight_attribute(design, True)
        dispatch("as_printed_estimate", {"process_values": process}, palette)
        _check(palette.sent[-1][1]["weight_on"] and palette.sent[-1][1]["bodies"][0]["applied_stale"] and len(applied) == 1,
               "with the attribute \"on\", the estimate applies a copy made for another density")
        applied.clear()
        dispatch("as_printed_estimate", {"process_values": dict(process, walls=3)}, palette)
        stale = palette.sent[-1][1]
        _check(len(applied) == 1 and [item.token for item in applied[0]] == [cube_token]
               and applied[0][0].settings_key == mass_model.PrintSettings.from_process(dict(process, walls=3)).key()
               and applied[0][0].body_label == "Cube" and stale["applied_now"][0]["name"] == "Cube",
               "while the switch is on, a body applied for other settings is applied again by itself")
        cube_body.material = filament
        palette.sent.clear()
        _check(dispatch("save_settings", {}, palette) is False and not palette.sent,
               "actions that are not the card's go back to Filamatch")

        set_weight_attribute(design, False)
        applied.clear()
        dispatch("as_printed_estimate", {"process_values": process, "nozzle": "0.4"}, palette)
        action, reply = palette.sent[-1]
        cube, plate = reply["bodies"]
        expected = mass_model.estimate(8.0, mass_model.slope_histogram(*mass_model._box_mesh(2.0, 2.0, 2.0)),
                                       mass_model.PrintSettings.from_process(process, "0.4"), 1.24).grams
        _check(action == "asPrintedMass" and reply["defaults"]["source"] == "process" and not reply["weight_on"]
               and not applied and abs(cube["grams"] - expected) < 1e-9 and plate["copies"] == 2
               and not plate["filament"] and plate["notes"] == [NOT_A_FILAMENT]
               and cube["component"] == "" and plate["component"] == "Bracket" and cube["target_origin"] == "model",
               "switch off: the estimate changes nothing; a component placed twice is one row; steel gets a note")
        dispatch("as_printed_estimate", {}, palette)
        _check(palette.sent[-1][1]["defaults"] == {"label": mass_model.PrintSettings().label(),
                                                   "source": "built-in", "problems": [],
                                                   "settings": mass_model.PrintSettings().to_dict()},
               "without process values the built-in defaults are used, and the reply says so")

        known = {filament.name: {"type": "PLA Basic", "catalog": True}}
        dispatch("as_printed_estimate", {"process_values": process, "filament_types": known}, palette)
        reply = palette.sent[-1][1]
        cube = reply["bodies"][0]
        _check(cube["strength"]["type"] == "PLA" and cube["strength"]["case"] == "safe" and cube["strength_from"] == "catalog"
               and reply["case"] == "safe" and reply["strength_types"]["PLA (generic)"]["uncertainty_percent"] == 30
               and len(reply["strength_types"]["PLA (generic)"]["sources"]) == 3 and reply["bodies"][1]["strength"] is None,
               "a filament body gets stiffness and strength from its catalog type (safe by default), with sources; "
               "steel gets none")
        dispatch("as_printed_estimate", {"process_values": process}, palette)
        _check(palette.sent[-1][1]["bodies"][0]["strength"] is None,
               "a material that is not in the catalog and has no type in its name gets no strength values")
        dispatch("as_printed_set_case", {"case": "typical"}, palette)
        dispatch("as_printed_estimate", {"process_values": process, "filament_types": known}, palette)
        typical = palette.sent[-1][1]
        _check(palette.sent[-2] == ("asPrintedCase", {"ok": True, "case": "typical", "log": palette.sent[-2][1]["log"]})
               and typical["case"] == "typical" and typical["bodies"][0]["strength"]["e_mpa"] > cube["strength"]["e_mpa"],
               "the case is kept per design and changes the values")
        dispatch("as_printed_set_case", {"case": "optimistic"}, palette)
        _check(palette.sent[-1][1]["ok"] is False and strength_case(design) == "typical",
               "an unknown case is refused")
        set_case_attribute(design, "safe")
        sheet = {"schema": 1, "maker": "Bambu Lab", "filaments": [
            {"product": "PLA Basic", "type": "PLA", "datasheet_url": "https://example.com/pla.pdf", "datasheet_version": "V2.0",
             "properties": [
                 {"property": "tensile_modulus", "direction": "XY", "value": 2.68, "unit": "GPa", "standard": "ISO 527"},
                 {"property": "tensile_modulus", "direction": "Z", "value": 2160, "unit": "MPa", "standard": "ISO 527"},
                 {"property": "tensile_strength", "direction": "XY", "value": 39, "unit": "MPa", "standard": "ISO 527"},
                 {"property": "tensile_strength", "direction": "Z", "value": 35, "unit": "MPa", "standard": "ISO 527"}]}]}
        with_sheets = strength_model.dataclasses.replace(fixture, products=strength_model.parse_datasheets([sheet], fixture))
        globals()["_strength_data"] = lambda: with_sheets
        filament.description = "Filamatch | PLA | Bambu Lab - PLA Basic - Black"
        dispatch("as_printed_estimate", {"process_values": process, "filament_types": known}, palette)
        reply = palette.sent[-1][1]
        cube = reply["bodies"][0]
        label = "Bambu Lab PLA Basic (data sheet V2.0)"
        _check(cube["strength"]["basis"] == label and cube["strength"]["kind"] == "datasheet"
               and cube["strength_from"] == "datasheet" and reply["strength_types"][label]["values"]["e_xy_mpa"]["median"] == 2680
               and reply["strength_types"][label]["uncertainty_percent"] == strength_model.DATASHEET_UNCERTAINTY_PERCENT,
               "a filament with its own data sheet (maker and product from the description) uses it (tier 2)")
        filament.description = ""
        dispatch("as_printed_estimate", {"process_values": process, "filament_types": {
            filament.name: {"type": "PLA", "catalog": True, "name": "Bambu Lab PLA Basic - Black"}}}, palette)
        _check(palette.sent[-1][1]["bodies"][0]["strength"]["basis"] == label,
               "the page's display name finds the data sheet too (the colour left off)")
        dispatch("as_printed_estimate", {"process_values": process, "filament_types": {
            filament.name: {"type": "PLA", "catalog": True, "name": "Bambu Lab PLA Matte - Black"}}}, palette)
        _check(palette.sent[-1][1]["bodies"][0]["strength"]["basis"] == "PLA (generic)",
               "another product of the same maker gets the generic values (only exact names match)")
        globals()["_strength_data"] = lambda: fixture
        named = strength_type(fixture, types.SimpleNamespace(name="PLA Generic", description=""), {})
        _check(named == ("PLA", "name") and strength_type(fixture, types.SimpleNamespace(name="Steel", description=""), {})
               == (None, ""), "other materials go by their name")

        calls: List[Any] = []

        def fake_check(jobs, profile, slicer_exe):
            calls.append([(mesh.name, settings.key(), own) for mesh, settings, own in jobs])
            mesh, settings, _own = jobs[0]
            return {"bodies": [{"name": mesh.name, "path": "", "token": mesh.token, "key": settings.key(),
                                "label": settings.label(), "grams": 3.0, "total_grams": 3.3, "minutes": 20,
                                "density_g_cm3": 0.375, "error": ""}], "skipped": 0, "notes": []}

        body_settings.write_body(cube_body, {"walls": 4})
        bridge._resolve_profile_target = lambda data: (None, "/fake/BambuStudio",
                                                       types.SimpleNamespace(printer_name="X1C", process_name="0.20mm Standard"))
        slicer_check.check_bodies = fake_check
        dispatch("as_printed_slicer_check", {"process_values": process}, palette)
        action, reply = palette.sent[-1]
        cube = reply["bodies"][0]
        _check(action == "asPrintedSlicer" and reply["ok"] and calls[-1][0][2] == {"walls": 4}
               and cube["slicer"]["grams"] == 3.0 and reply["slicer_total_grams"] == 3.0
               and reply["left_out"] == ["Plate"] and reply["slicer_target"]["slicer"] == "BambuStudio",
               "the slicer check slices with the Print target, the body's own settings on top")

        dispatch("as_printed_set_weight", {"on": True, "process_values": process}, palette)
        action, reply = palette.sent[-1]
        _check(action == "asPrintedWeight" and reply["on"] and applied[-1][0].origin == "slicer"
               and applied[-1][0].density_g_cm3 == 0.375 and [s["name"] for s in reply["skipped"]] == ["Plate"]
               and weight_on(design),
               "the switch on applies every filament body (the slicer's density where it sliced the same settings)")
        dispatch("as_printed_estimate", {"process_values": dict(process, infill=40)}, palette)
        _check(applied[-1][0].origin == "model", "other defaults mean the model's density")
        dispatch("as_printed_set_weight", {"on": False}, palette)
        _check(palette.sent[-1][1]["on"] is False and [b.name for b, _p in restored[-1]] == ["Cube", "Hidden", "Plate"]
               and not weight_on(design),
               "the switch off puts the filaments back on every solid body, hidden ones too, and stays off")
        count = len(applied)
        dispatch("as_printed_estimate", {"process_values": process}, palette)
        _check(len(applied) == count, "while the switch is off, the estimate applies nothing")
        dispatch("as_printed_set_weight", {"on": True, "process_values": {"layer_height": 3.0}}, palette)
        _check(palette.sent[-1][0] == "asPrintedWeight" and palette.sent[-1][1]["ok"] is True,
               "a process value that cannot be printed is left out (the built-in value stays)")
        bridge._active_design = lambda: None
        dispatch("as_printed_set_weight", {"on": False}, palette)
        _check(palette.sent[-1][1] == {"ok": False, "error": "Open a Fusion design first.", "log": palette.sent[-1][1]["log"]},
               "with no design the card is told so in plain words")
        _check(set(REPLIES) == set(_ACTIONS), "every action has its reply")
    finally:
        (bridge._active_design, bridge._resolve_profile_target, slicer_check.check_bodies,
         material_copy.apply, material_copy.restore, material_copy.clean_up, saved_data) = saved
        globals()["_strength_data"] = saved_data
        _LAST_SLICER["bodies"] = {}

    print()
    if _FAILED:
        print("AS PRINTED BRIDGE SELF-TEST FAILED ({} passed, {} failed)".format(_PASSED, _FAILED))
        return 1
    print("AS PRINTED BRIDGE SELF-TEST PASSED ({} checks)".format(_PASSED))
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
