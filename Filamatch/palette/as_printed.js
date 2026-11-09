/*
 * The printed values card in the Print view: the bodies' printed weight (an
 * estimate, or the slicer check's grams), their stiffness and strength (rough
 * estimates from generic data per filament type), and the switch that gives
 * Fusion those values. It came from the Printegrity add-in and builds its content
 * into #as-printed-root through window.Filamatch (docs/development.md,
 * "Printed weight (As printed)"); the Python side is lib/as_printed_bridge.py.
 *
 * Requests (request -> reply), all registered with Filamatch.registerAction:
 *   as_printed_estimate     { process_values, nozzle }     -> asPrintedMass
 *   as_printed_slicer_check { process_values, nozzle }     -> asPrintedSlicer
 *   as_printed_set_weight   { on, process_values, nozzle } -> asPrintedWeight
 *   as_printed_set_case     { case }                       -> asPrintedCase
 * Every request also sends filament_types {material name: {type, catalog}}.
 *
 * One source for each setting: the printer, process and each body's own
 * settings are the Print view's. Each body's grams and notes go into
 * Filamatch's body lists (registerBodyDetail, registerBodyNotes). While the
 * switch is on, every estimate keeps Fusion's mass the printed mass (the
 * Python side applies what changed). All text is set with textContent.
 */
(function () {
  'use strict';

  var api = window.Filamatch;
  if (!api || api.apiVersion !== 1) {
    return;
  }

  /** Request -> [reply, label for timeouts and errors]. */
  var ACTIONS = {
    as_printed_estimate: ['asPrintedMass', 'estimate the printed weight'],
    as_printed_slicer_check: ['asPrintedSlicer', 'check with the slicer'],
    as_printed_set_weight: ['asPrintedWeight', 'switch the printed values'],
    as_printed_set_case: ['asPrintedCase', 'keep the strength case']
  };

  var CASES = [
    { value: 'safe', label: 'Safe: across the layers (weakest)' },
    { value: 'typical', label: 'Typical: between along and across the layers' }
  ];

  /** The slicer runs once per body: give it time. */
  var SLICER_TIMEOUT_MS = 600000;
  var ESTIMATE_DEBOUNCE_MS = 300;

  var state = {
    /** The last asPrintedMass reply. */
    mass: null,
    /** Its rows by body token. */
    rows: {},
    /** The last slicer check per token: {key, grams, density_g_cm3, error}; shown while the key matches. */
    slicer: {},
    /** Errors of the last automatic Apply, by "name|path". */
    applyErrors: {},
    /** The slicer check's or the switch's message, kept when the estimate after it reports too. */
    carry: null,
    /** True while the status line shows the estimate's own message. */
    estimateMessage: false,
    estimateTimer: null,
    built: false,
    /** True while this card redraws the body lists (their "body-editor" change is its own). */
    redrawing: false,
    /** Demo only. */
    demo: { on: false, applied: {}, case: 'safe' }
  };

  Object.keys(ACTIONS).forEach(function (request) {
    api.registerAction(request, ACTIONS[request][0], ACTIONS[request][1]);
  });

  function byId(id) {
    return api.byId(id);
  }

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function grams(value) {
    return (Math.round(Number(value) * 10) / 10).toFixed(1) + ' g';
  }

  function setStatus(text, kind) {
    api.setStatus(byId('ap-status'), text, kind);
  }

  /** Filamatch's filament type of each design material, by material name. */
  function filamentTypes() {
    var types = {};
    (api.getMaterials() || []).forEach(function (material) {
      if (material && material.material_name) {
        types[String(material.material_name)] = { type: String(material.filament_type || ''),
          catalog: material.in_catalog === true, name: String(material.display_name || '') };
      }
    });
    return types;
  }

  function requestPayload() {
    var printer = api.printerValues() || {};
    return { process_values: api.processValues() || {}, nozzle: printer.nozzle, filament_types: filamentTypes() };
  }

  /** "1.6–2.4 GPa" (one unit when both ends share it). */
  function modulusRange(low, high) {
    var a = modulus(low);
    var b = modulus(high);
    return a.slice(-4) === b.slice(-4) ? a.slice(0, -4) + '\u2013' + b : a + '\u2013' + b;
  }

  /** "2.3 GPa", or "30 MPa" below 1 GPa. */
  function modulus(mpa) {
    var value = Number(mpa);
    return value >= 1000 ? (value / 1000).toFixed(1) + ' GPa' : Math.round(value) + ' MPa';
  }

  function estimateNow() {
    if (state.estimateTimer !== null) {
      window.clearTimeout(state.estimateTimer);
      state.estimateTimer = null;
    }
    if (!api.isPending('as_printed_estimate')) {
      api.request('as_printed_estimate', requestPayload(), { statusEl: byId('ap-status') });
    }
  }

  function scheduleEstimate() {
    if (state.estimateTimer !== null) {
      window.clearTimeout(state.estimateTimer);
    }
    state.estimateTimer = window.setTimeout(estimateNow, ESTIMATE_DEBOUNCE_MS);
  }

  function redrawBodyLists() {
    state.redrawing = true;
    try {
      api.refreshBodyLists();
    } finally {
      state.redrawing = false;
    }
  }

  /* =========================================================================
   * The card
   * ======================================================================= */

  function build() {
    var root = byId('as-printed-root');
    if (!root || state.built) {
      return;
    }
    state.built = true;
    root.textContent = '';

    var row = element('div', 'card__row');
    var main = element('div', 'card__main');
    var label = element('div', 'card__label', 'Printed weight and strength');
    label.id = 'ap-label';
    main.appendChild(label);
    var total = element('div', 'card__value ap-total', 'Estimating…');
    total.id = 'ap-total';
    main.appendChild(total);
    var strength = element('p', 'faint ap-strength');
    strength.id = 'ap-strength';
    main.appendChild(strength);
    row.appendChild(main);
    root.appendChild(row);

    var body = element('div', 'card__body');
    var field = element('label', 'switch-field ap-switch');
    field.setAttribute('for', 'ap-weight-switch');
    field.title = 'On: each filament body gets its own “as printed” copy of its filament with its printed ' +
      'density, stiffness and strength, so Fusion’s mass and Simulation use them. Off: the filaments come back.';
    field.appendChild(element('span', '', 'Fusion uses the printed values'));
    var input = document.createElement('input');
    input.type = 'checkbox';
    input.id = 'ap-weight-switch';
    input.addEventListener('change', function () {
      var payload = requestPayload();
      payload.on = input.checked;
      api.request('as_printed_set_weight', payload, { statusEl: byId('ap-status'), button: input,
        pendingText: input.checked ? 'Giving Fusion the printed values…' : 'Putting the filaments back…' });
    });
    field.appendChild(input);
    body.appendChild(field);

    var caseField = element('div', 'field');
    var caseLabel = element('label', 'field-label', 'Stiffness and strength values');
    caseLabel.setAttribute('for', 'ap-case');
    caseField.appendChild(caseLabel);
    var select = document.createElement('select');
    select.id = 'ap-case';
    select.className = 'select-input';
    select.title = 'Fusion Simulation uses one value for every direction. Printed parts break first across the ' +
      'layers, so Safe uses those values.';
    CASES.forEach(function (item) {
      var option = document.createElement('option');
      option.value = item.value;
      option.textContent = item.label;
      select.appendChild(option);
    });
    select.addEventListener('change', function () {
      api.request('as_printed_set_case', { case: select.value }, { statusEl: byId('ap-status'), button: select });
    });
    caseField.appendChild(select);
    body.appendChild(caseField);

    var actions = element('div', 'ap-actions');
    var check = element('button', 'btn btn--ghost', 'Check with the slicer');
    check.type = 'button';
    check.id = 'ap-check-btn';
    check.title = 'Slice each filament body once with the Print settings, for its exact grams';
    check.addEventListener('click', function () {
      api.request('as_printed_slicer_check', requestPayload(), { statusEl: byId('ap-status'), button: check,
        timeoutMs: SLICER_TIMEOUT_MS, pendingText: 'Slicing… about 2 seconds a body.' });
    });
    actions.appendChild(check);
    body.appendChild(actions);
    var status = element('p', 'status status--muted');
    status.id = 'ap-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    body.appendChild(status);
    body.appendChild(element('p', 'faint', 'Stiffness and strength are rough estimates from generic data per ' +
      'filament type, not design values. Check important parts with a test print.'));
    var sources = element('details', 'advanced ap-sources');
    sources.appendChild(element('summary', 'advanced__summary', 'Where the values come from'));
    var list = element('div', 'advanced__body');
    list.id = 'ap-sources-list';
    sources.appendChild(list);
    body.appendChild(sources);
    root.appendChild(body);
    root.setAttribute('aria-labelledby', 'ap-label');
  }

  /* =========================================================================
   * Values: the total here, each body's grams and notes in the body lists
   * ======================================================================= */

  /** The slicer check's answer for a row while its settings are unchanged (else null). */
  function slicerFor(row) {
    var found = row ? state.slicer[row.token] : null;
    return found && found.key === row.key ? found : null;
  }

  function slicerGrams(row) {
    var found = slicerFor(row);
    return found && typeof found.grams === 'number' ? found.grams : null;
  }

  function allSliced(rows) {
    return rows.length > 0 && rows.every(function (row) { return slicerGrams(row) !== null; });
  }

  function sum(rows, value) {
    return rows.reduce(function (total, row) {
      var number = value(row);
      return total + (typeof number === 'number' ? number * (Number(row.copies) || 1) : 0);
    }, 0);
  }

  function estimateGrams(row) {
    return row.grams;
  }

  function weighed(rows) {
    return rows.filter(function (row) { return row && typeof row.grams === 'number'; });
  }

  /**
   * "2.4 g estimate" or "2.4 g slicer check"; with stiffness, shorter for the
   * narrow palette: "2.4 g · E 2.3 GPa" or "2.4 g sliced · E 2.3 GPa" ("" for
   * a body without a value).
   */
  function bodyDetail(row) {
    if (!row || typeof row.grams !== 'number') {
      return '';
    }
    var sliced = slicerGrams(row);
    if (row.strength) {
      return (sliced !== null ? grams(sliced) + ' sliced' : grams(row.grams)) + ' \u00b7 E ' + modulus(row.strength.e_mpa);
    }
    return sliced !== null ? grams(sliced) + ' slicer check' : grams(row.grams) + ' estimate';
  }

  function groupDetail(rows) {
    rows = weighed(rows);
    if (!rows.length) {
      return '';
    }
    return allSliced(rows) ? grams(sum(rows, slicerGrams)) + ' slicer check'
      : grams(sum(rows, estimateGrams)) + ' estimate';
  }

  function bodyNotes(row) {
    if (!row) {
      return [];
    }
    var notes = [];
    var error = state.applyErrors[row.name + '|' + (row.path || '')];
    if (error) {
      notes.push({ text: 'Fusion does not have its printed values: ' + error, kind: 'warning' });
    } else if (state.mass && state.mass.weight_on && row.applied && !row.applied_stale &&
        typeof row.fusion_grams === 'number') {
      notes.push({ text: 'Fusion uses the printed values: ' + grams(row.fusion_grams) +
        (row.strength ? ', E ' + modulus(row.strength.e_mpa) + ', tensile ' + Math.round(row.strength.tensile_mpa) +
          ' MPa' : '') + '.', kind: 'success' });
    } else if (row.applied_stale) {
      notes.push({ text: 'Fusion’s mass is for other settings.', kind: 'warning' });
    }
    var found = slicerFor(row);
    if (found && found.error) {
      notes.push({ text: 'Slicer check: ' + found.error, kind: 'warning' });
    }
    if (typeof row.grams === 'number' && !row.strength && row.strength_none === 'flexible') {
      notes.push({ text: 'Flexible filament: a linear simulation does not fit it, so it gets no printed stiffness ' +
        'and strength. Fusion keeps the material\u2019s own values.', kind: 'warning' });
    } else if (typeof row.grams === 'number' && !row.strength) {
      notes.push({ text: 'No stiffness and strength data for this filament type yet.', kind: '' });
    } else if (row.strength && row.strength.kind === 'material') {
      notes.push({ text: 'No printed test data for this filament: its own Fusion values, scaled for the infill. ' +
        'Often too strong (\u00b1' + row.strength.uncertainty_percent + '%).', kind: 'warning' });
    } else if (row.strength && row.strength.kind === 'datasheet') {
      notes.push({ text: 'Stiffness and strength from its own data sheet.', kind: '' });
    } else if (row.strength && row.strength_from === 'name') {
      notes.push({ text: 'Stiffness and strength for ' + row.strength.type + ', from the material’s name.',
        kind: '' });
    }
    (row.notes || []).forEach(function (note) { notes.push({ text: note, kind: '' }); });
    return notes;
  }

  function rowOf(body) {
    return state.rows[String(body && body.token)];
  }

  api.registerBodyDetail(
    function (body) { return bodyDetail(rowOf(body)); },
    function (bodies) { return groupDetail((bodies || []).map(rowOf)); }
  );
  api.registerBodyNotes(function (body) { return bodyNotes(rowOf(body)); });

  function renderCard() {
    var total = byId('ap-total');
    var input = byId('ap-weight-switch');
    if (!total || !state.mass) {
      return;
    }
    var rows = weighed(Array.isArray(state.mass.bodies) ? state.mass.bodies : []);
    if (!rows.length) {
      total.textContent = 'No filament bodies yet';
    } else if (allSliced(rows)) {
      total.textContent = grams(sum(rows, slicerGrams)) + ' (slicer check)';
    } else {
      total.textContent = 'About ' + grams(sum(rows, estimateGrams)) + ' (estimate)';
    }
    if (input && !api.isPending('as_printed_set_weight')) {
      input.checked = !!state.mass.weight_on;
    }
    var select = byId('ap-case');
    if (select && !api.isPending('as_printed_set_case')) {
      select.value = state.mass.case === 'typical' ? 'typical' : 'safe';
    }
    renderStrength(rows);
  }

  /** The strength line, and where the values of the types in use come from. */
  function renderStrength(rows) {
    var line = byId('ap-strength');
    var list = byId('ap-sources-list');
    var types = state.mass.strength_types || {};
    var names = Object.keys(types).sort();
    var spreads = names.map(function (name) { return Number(types[name].uncertainty_percent) || 0; });
    var low = Math.min.apply(null, spreads);
    var high = Math.max.apply(null, spreads);
    if (line) {
      line.textContent = !names.length ? (rows.length ? 'No stiffness and strength data for these filaments yet.' : '')
        : (state.mass.case === 'typical' ? 'Typical' : 'Safe') + ' stiffness and strength, rough estimates (±' +
          low + (high !== low ? '–' + high : '') + '%).';
    }
    if (!list) {
      return;
    }
    list.textContent = '';
    names.forEach(function (name) {
      list.appendChild(element('p', 'faint', sourceText(name, types[name])));
    });
    if (!names.length) {
      list.appendChild(element('p', 'faint', 'No filament with stiffness and strength data in this design.'));
    }
  }

  /** "92 MPa", or "77–109 MPa" for a range over several data sheets. */
  function stress(range) {
    return Math.round(range.low) === Math.round(range.high) ? Math.round(range.low) + ' MPa'
      : Math.round(range.low) + '\u2013' + Math.round(range.high) + ' MPa';
  }

  function stiffness(range) {
    return Math.round(range.low) === Math.round(range.high) ? modulus(range.low) : modulusRange(range.low, range.high);
  }

  /** One basis in "Where the values come from": its values along and across the layers, and its sources. */
  function sourceText(name, info) {
    var values = info.values || {};
    var moist = info.conditioned || {};
    var parts = [name + ', \u00b1' + info.uncertainty_percent + '%:'];
    if (info.kind === 'material') {
      parts.push('E ' + stiffness(values.e_xy_mpa) + ', tensile ' + stress(values.uts_xy_mpa) +
        ' in every direction, scaled for the infill.');
      (info.notes || []).forEach(function (note) { parts.push(note); });
      return parts.join(' ');
    }
    if (values.e_xy_mpa) {
      parts.push('E ' + stiffness(values.e_xy_mpa) + ' along' +
        (values.e_z_mpa ? ', ' + stiffness(values.e_z_mpa) + ' across' : '') + ';');
    }
    if (values.uts_xy_mpa) {
      parts.push('tensile ' + stress(values.uts_xy_mpa) + ' along' +
        (values.uts_z_mpa ? ', ' + stress(values.uts_z_mpa) + ' across.' : ' (no across-the-layers data).'));
    }
    if (moist.e_xy_mpa || moist.uts_z_mpa) {
      parts.push('Moist:' + (moist.e_xy_mpa ? ' E ' + stiffness(moist.e_xy_mpa) + ' along' : '') +
        (moist.uts_z_mpa ? (moist.e_xy_mpa ? ',' : '') + ' tensile ' + stress(moist.uts_z_mpa) + ' across' : '') + '.');
    }
    (info.notes || []).forEach(function (note) { parts.push(note); });
    parts.push(info.kind === 'datasheet' ? 'Printed test bars: ' + (info.sources || []).join('; ') + '.'
      : 'Median of ' + api.pluralize((info.sources || []).length, 'data sheet', 'data sheets') + ' (printed test bars): ' +
        (info.sources || []).join('; ') + '.');
    return parts.join(' ');
  }

  /* =========================================================================
   * Replies
   * ======================================================================= */

  function deletedText(names) {
    return names && names.length ? api.pluralize(names.length, 'unused “As printed” material',
      'unused “As printed” materials') + ' deleted.' : '';
  }

  function remember(payload) {
    state.mass = payload;
    state.rows = {};
    (payload.bodies || []).forEach(function (row) { state.rows[String(row.token)] = row; });
  }

  api.onMessage('asPrintedMass', function (payload) {
    if (payload.ok === false) {
      setStatus('Could not estimate the printed weight: ' + api.replyError(payload), 'error');
      return;
    }
    remember(payload);
    state.applyErrors = {};
    var failed = (payload.applied_now || []).filter(function (item) { return item.error; });
    failed.forEach(function (item) { state.applyErrors[item.name + '|' + (item.path || '')] = item.error; });
    renderCard();
    redrawBodyLists();
    var parts = [];
    var done = (payload.applied_now || []).length - failed.length;
    if (done) {
      parts.push('Fusion got the new printed values of ' + api.pluralize(done, 'body', 'bodies') + '.');
    }
    if (failed.length) {
      parts.push(api.pluralize(failed.length, 'body does', 'bodies do') + ' not have the printed values in Fusion.');
    }
    var noted = (payload.applied_now || []).filter(function (item) { return !item.error && item.note; });
    noted.forEach(function (item) { parts.push(item.name + ': ' + item.note); });
    if (deletedText(payload.deleted)) {
      parts.push(deletedText(payload.deleted));
    }
    if (payload.skipped) {
      parts.push(api.pluralize(payload.skipped, 'more body was', 'more bodies were') + ' not measured.');
    }
    var carry = state.carry;
    state.carry = null;
    if (parts.length) {
      setStatus((carry ? carry.text + ' ' : '') + api.demoSuffix(parts.join(' ')),
        failed.length || noted.length ? 'warning' : (carry ? carry.kind : 'muted'));
      state.estimateMessage = !carry;
    } else if (state.estimateMessage && !carry) {
      setStatus('', 'muted');
      state.estimateMessage = false;
    }
  });

  /** Show a reply's message; the estimate that follows keeps it in front of its own. */
  function report(text, kind) {
    setStatus(text, kind);
    state.carry = { text: text, kind: kind };
    state.estimateMessage = false;
  }

  api.onMessage('asPrintedSlicer', function (payload) {
    if (payload.ok === false) {
      setStatus('Slicer check failed: ' + api.replyError(payload), 'error');
      return;
    }
    (payload.bodies || []).forEach(function (row) {
      if (row.slicer) {
        state.slicer[row.token] = row.slicer;
      }
    });
    var target = payload.slicer_target || {};
    var parts = [api.demoSuffix('Sliced with ' + [target.slicer, target.printer, target.process]
      .filter(Boolean).join(' · ') + '.')];
    (payload.slicer_notes || []).forEach(function (note) { parts.push(note); });
    if (payload.left_out && payload.left_out.length) {
      parts.push('Not sliced: ' + payload.left_out.join(', ') + '.');
    }
    report(parts.join(' '), 'success');
    // The estimate shows the slicer's grams, and (switch on) gives Fusion its densities.
    estimateNow();
  });

  api.onMessage('asPrintedCase', function (payload) {
    if (payload.ok === false) {
      var select = byId('ap-case');
      if (select && state.mass) {
        select.value = state.mass.case === 'typical' ? 'typical' : 'safe';
      }
      setStatus('Could not change the strength values: ' + api.replyError(payload), 'error');
      return;
    }
    // The estimate shows the new values, and (switch on) gives them to Fusion.
    estimateNow();
  });

  api.onMessage('asPrintedWeight', function (payload) {
    var input = byId('ap-weight-switch');
    if (payload.ok === false) {
      if (input && state.mass) {
        input.checked = !!state.mass.weight_on;
      }
      setStatus('Could not switch the printed weight: ' + api.replyError(payload), 'error');
      return;
    }
    var bodies = payload.bodies || [];
    var failed = bodies.filter(function (body) { return body.error; });
    var done = bodies.length - failed.length;
    var parts = [payload.on
      ? 'Fusion uses the printed values of ' + api.pluralize(done, 'body', 'bodies') + '.'
      : (done ? 'Fusion uses the filament values again (' + api.pluralize(done, 'body', 'bodies') + ').'
        : 'Fusion uses the filament values.')];
    failed.forEach(function (body) { parts.push(body.name + ': ' + body.error); });
    var noted = bodies.filter(function (body) { return !body.error && body.note; });
    noted.forEach(function (body) { parts.push(body.name + ': ' + body.note); });
    if (deletedText(payload.deleted)) {
      parts.push(deletedText(payload.deleted));
    }
    report(api.demoSuffix(parts.join(' ')), failed.length || noted.length ? 'warning' : 'success');
    estimateNow();
  });

  /* =========================================================================
   * Changes elsewhere in the palette
   * ======================================================================= */

  api.onChange(function (kind) {
    if (kind === 'connected') {
      build();
      estimateNow();
    } else if (kind === 'materials' || kind === 'target' || kind === 'profiles') {
      scheduleEstimate();
    } else if (kind === 'body-editor' && !state.redrawing) {
      // A body's own settings were saved (or the editor closed).
      scheduleEstimate();
    }
  });

  /* =========================================================================
   * Demo replies (the palette opened outside Fusion)
   * ======================================================================= */

  function demoRows(payload) {
    var values = (payload && payload.process_values) || {};
    var walls = Number(values.walls) || 2;
    var infill = values.infill !== undefined ? Number(values.infill) : 15;
    var rows = [];
    (api.getMaterials() || []).forEach(function (material, index) {
      (material.body_list || []).forEach(function (body, number) {
        var solid = 6 + 9 * ((index * 3 + number) % 4);
        var shell = Math.min(1, 0.3 * walls / 2);
        var printed = solid * (shell + (1 - shell) * infill / 100);
        var key = 'demo|' + walls + '|' + infill + '|' + JSON.stringify(body.settings || {});
        var share = shell + (1 - shell) * Math.pow(infill / 100, 1.5);
        var type = String(material.filament_type || 'PLA');
        var known = DEMO_TYPES[type];
        var typical = state.demo.case === 'typical';
        // Demo: Polymaker filaments stand for a product with its own data sheet.
        var sheet = /polymaker/i.test(String(material.display_name || ''));
        var strength = known ? { type: type, case: typical ? 'typical' : 'safe',
          basis: sheet ? 'Polymaker PolyLite ' + type + ' (data sheet V5.6)' : type + ' (generic)',
          kind: sheet ? 'datasheet' : 'generic',
          e_mpa: known.e * share * (typical ? 1.05 : 0.9), poisson: 0.36, shear_mpa: 0, yield_mpa: 0,
          tensile_mpa: known.uts * share * (typical ? 0.8 : 0.55), uncertainty_percent: sheet ? 25 : 30, notes: [] } : null;
        var applied = state.demo.applied[body.token];
        rows.push({
          token: String(body.token), copies: Number(body.copies) || 1, name: body.name,
          component: body.component || '', path: body.component ? body.component + ':1' : '', filament: true,
          material: applied ? material.display_name + ' · ' + body.name + ' · as printed' : material.display_name,
          filament_name: material.display_name, applied: !!applied, applied_stale: !!applied && applied.key !== key,
          fusion_grams: applied ? applied.grams : solid, own_summary: '', label: 'demo', key: key,
          grams: printed, solid_grams: solid, notes: [], strength: strength, strength_from: 'catalog'
        });
      });
    });
    return rows;
  }

  /** Demo only: rough base values (MPa) per type, not the bundled data. */
  var DEMO_TYPES = { PLA: { e: 2600, uts: 45 }, PETG: { e: 1900, uts: 42 }, ABS: { e: 2000, uts: 35 } };

  function demoTypes(rows) {
    var found = {};
    rows.forEach(function (row) {
      if (row.strength && !found[row.strength.basis]) {
        var base = DEMO_TYPES[row.strength.type];
        var one = row.strength.kind === 'datasheet';
        found[row.strength.basis] = { kind: row.strength.kind, type: row.strength.type, poisson: 0.36, notes: [],
          uncertainty_percent: row.strength.uncertainty_percent,
          sources: one ? ['Demo data sheet V5.6 (ISO 527)'] : ['Demo data sheet A (ISO 527)', 'Demo data sheet B (ISO 527)'],
          values: { e_xy_mpa: { median: base.e, low: base.e * (one ? 1 : 0.8), high: base.e * (one ? 1 : 1.2), count: 1 },
            uts_xy_mpa: { median: base.uts, low: base.uts * (one ? 1 : 0.8), high: base.uts * (one ? 1 : 1.1), count: 1 },
            uts_z_mpa: { median: base.uts * 0.6, low: base.uts * (one ? 0.6 : 0.45), high: base.uts * (one ? 0.6 : 0.7),
              count: 1 } } };
      }
    });
    return found;
  }

  function demoApply(rows) {
    return rows.map(function (row) {
      var sliced = slicerGrams(row);
      state.demo.applied[row.token] = { key: row.key, grams: sliced !== null ? sliced : row.grams };
      return { name: row.name, path: row.path, material: row.filament_name + ' · ' + row.name + ' · as printed',
        density_g_cm3: 0.42, source: sliced !== null ? 'slicer' : 'model', error: '' };
    });
  }

  api.registerDemo('as_printed_estimate', function (payload) {
    var rows = demoRows(payload);
    var applied = [];
    if (state.demo.on) {
      applied = demoApply(rows.filter(function (row) {
        var held = state.demo.applied[row.token];
        var sliced = slicerGrams(row);
        return !held || held.key !== row.key || (sliced !== null && held.grams !== sliced);
      }));
      rows = demoRows(payload);
    }
    return { ok: true, bodies: rows, skipped: 0, weight_on: state.demo.on, applied_now: applied, deleted: [],
      case: state.demo.case, strength_types: demoTypes(rows),
      defaults: { label: 'demo', source: 'process', problems: [], settings: {} } };
  });
  api.registerDemo('as_printed_slicer_check', function (payload) {
    var rows = demoRows(payload).map(function (row) {
      row.slicer = { token: row.token, key: row.key, grams: row.grams * 0.97, density_g_cm3: 0.42, error: '' };
      return row;
    });
    var target = api.getTarget() || {};
    return { ok: true, bodies: rows, weight_on: state.demo.on, slicer_target: { slicer: 'BambuStudio',
      printer: target.printer, process: target.process }, slicer_notes: [], left_out: [] };
  });
  api.registerDemo('as_printed_set_case', function (payload) {
    state.demo.case = payload.case === 'typical' ? 'typical' : 'safe';
    return { ok: true, case: state.demo.case };
  });
  api.registerDemo('as_printed_set_weight', function (payload) {
    state.demo.on = !!payload.on;
    if (state.demo.on) {
      return { ok: true, on: true, skipped: [], deleted: [], bodies: demoApply(demoRows(payload)) };
    }
    var tokens = Object.keys(state.demo.applied);
    state.demo.applied = {};
    return { ok: true, on: false, skipped: [], deleted: tokens.map(function (token) { return 'copy ' + token; }),
      bodies: tokens.map(function (token) { return { name: token, path: '', material: 'filament', error: '' }; }) };
  });
})();
