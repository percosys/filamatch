/**
 * Filamatch - palette application logic.
 *
 * Loaded by the HTML palette shown inside Autodesk Fusion. No dependencies,
 * no build step, no network access.
 *
 * ---------------------------------------------------------------------------
 * BRIDGE PROTOCOL
 * ---------------------------------------------------------------------------
 * HTML -> Python:  adsk.fusionSendData(action, JSON.stringify(payload))
 * Python -> HTML:  palette.sendInfoToHTML(action, dataString), which Fusion
 *                  routes to `window.fusionJavaScriptHandler.handle(action, dataString)`.
 *                  The handler must return `true`; JSON parsing is guarded.
 *                  Fusion echoes that return value back as a separate
 *                  "response" event: it is ignored silently and nothing is
 *                  ever sent back for it (that would create a feedback loop).
 *
 * Requests sent by this palette (request -> reply):
 *   init                 {}                                  -> initData
 *   import_selected      { ids[] }                           -> importResult
 *   refresh_materials    {}                                  -> materialsData
 *   export_3mf           { output_path, write_project_settings,
 *                          orca{enabled, executable, profile_dir},
 *                          target{prepare, slicer_path, vendor, printer, process},
 *                          bambuddy_slot_map, bambuddy_printer_id } -> exportResult
 *   run_diagnostics      {}                                  -> diagnosticsResult
 *   save_settings        { output_path, write_project_settings, orca_enabled,
 *                          orca_path, orca_profile_dir, prepare_enabled,
 *                          target_vendor, target_printer, target_process,
 *                          slicer_path, auto_library_update,
 *                          custom_library_path, bambuddy_printer_id,
 *                          bambuddy_slot_map, bambuddy_verify_tls } -> settingsSaved
 *   clear_bambuddy_key   {}                                  -> settingsSaved
 *   get_log              {}                                  -> logData
 *   browse_export_path   { current_path }                    -> exportPathData
 *   browse_slicer_path   { current_path }                    -> slicerPathData
 *   get_profiles         { slicer_path, vendor, printer }    -> profilesData
 *   import_profiles      {}                                  -> profilesImported
 *   openfilament_search  { query, material, limit }          -> openFilamentData
 *   openfilament_import  { keys[] }                          -> openFilamentImportResult
 *   create_library       {}                                  -> libraryCreated
 *   bambuddy_context     { bambuddy_url?, bambuddy_api_key?, bambuddy_verify_tls?,
 *                          bambuddy_printer_id, bambuddy_slot_map } -> bambuddyContextData
 *                        (URL, key and TLS flag only on an explicit Connect)
 *   bambuddy_import      { slot_keys[], bambuddy_printer_id } -> bambuddyImportResult
 * Fire-and-forget requests (no pending state, no timeout):
 *   open_material_browser {}   Fusion opens its Physical Material browser and
 *                              replies materialBrowserData only on failure.
 *   design_check          {}   Fusion replies materialsData only when bodies or
 *                              materials changed.
 *   fit_height            { content, viewport }  Fusion fits the palette height.
 *
 * Replies handled by this palette (action -> payload):
 *   initData       { version, design_name, catalog[], categories[{name, filament_ids[]}],
 *                    materials[{material_id, material_name, display_name,
 *                               color_hex, filament_type, density_g_cm3,
 *                               category, body_count, bodies[], slot}],
 *                    settings{}, library{path, loaded, writable, message},
 *                    favorites{count, names[]},
 *                    profiles{slicers[], vendors[], printers[], processes[],
 *                             imported{}, selected{}},
 *                    openfilament{cached, version, generated_at, counts},
 *                    custom_library{ok, path, exists, loaded, material_count, message},
 *                    bambuddy{configured, key_set, url, printer_id, slot_map},
 *                    log[] }
 *   importResult   { ok, imported[{id, fusion_name, appearance_name,
 *                                  favorite_name, favorite_appearance_name,
 *                                  color_hex, replaced, relinked_bodies}],
 *                    errors[], materials[], library{}, favorites{},
 *                    custom_library{}, log[] }
 *   materialsData  { ok, design_name, materials[], favorites?, library?, log[] }
 *                  (also pushed unasked after the design's materials changed)
 *   exportResult   { ok, output_path, raw_export_path,
 *                    materials_used[{slot, material_id, display_name, color_hex,
 *                                    filament_type, density_g_cm3, body_count}],
 *                    prepared, printer_name, process_name,
 *                    project_settings_written, orca_launched,
 *                    error, log[], log_tail[] }
 *   diagnosticsResult { ok, text }
 *   settingsSaved  { ok, settings, log[] }
 *   logData        { log[] }
 *   exportPathData { ok, path, error?, cancelled? }
 *   slicerPathData { ok, path, error?, cancelled? }
 *   profilesData   { ok, slicers[], vendors[], printers[], processes[],
 *                    imported{}, selected{}, error }
 *   profilesImported { ok, imported{machines, processes, filaments, root},
 *                      profiles{}, error?, cancelled? }
 *   openFilamentData { ok, results[{key, brand, material, filament, variant,
 *                      color_hex, density_g_cm3, min_print_temp, max_print_temp,
 *                      min_bed_temp, max_bed_temp}],
 *                      summary{version, generated_at, counts}, error }
 *   openFilamentImportResult { ok, imported[{id, fusion_name, color_hex,
 *                      favorite_name}], errors[], favorites{}, catalog[],
 *                      categories[], custom_library{}, materials[]?,
 *                      catalog_count?, error }
 *   libraryCreated { ok, custom_library{}, favorites?, error? }
 *   bambuddyContextData { ok, configured, source, selected_printer_id,
 *                      printers[{id, name, model, label}],
 *                      slots[{key, label, display_name, filament_type,
 *                             color_hex, spool_id, remaining_percent}],
 *                      mapping[{material_id, slot_key, slot_label,
 *                               confidence, overridden}], error }
 *   bambuddyImportResult { ok, imported[], favorites{}, catalog[],
 *                      categories[], custom_library{}, materials[], log[], error }
 *   materialBrowserData { ok, error }
 *
 * Reply payloads may carry `ok: false` and an `error` string; every handler
 * surfaces that instead of swallowing it. A cancelled native file dialog comes
 * back as `{ok: false, cancelled: true}` and is informational, not an error.
 *
 * Importing catalog filaments creates colored materials in the active design
 * AND in Fusion's app-level Favorites library. The optional custom material
 * library is a single snapshot of Favorites (Fusion's API cannot create
 * category folders), rebuilt via `create_library`. Assembly materials are
 * read-only; filament slots are automatic and there are no manual extruder
 * mapping controls. Printer preparation uses profiles from the configured
 * slicer (Bambu Studio/OrcaSlicer) and imported presets; base-only exports show
 * a warning that no printer profile was applied.
 *
 * If the `adsk.fusionSendData` bridge is missing (e.g. index.html opened in a
 * normal browser), the palette switches to a built-in demo mode with sample
 * data and simulated replies, and stays in it for the whole session. It never
 * throws. All dynamic text is written with textContent/createElement; request
 * failures produce visible status lines and log entries.
 */

(function () {
  'use strict';

  /* =========================================================================
   * Constants
   * ======================================================================= */

  /** Request action -> action Python uses for its reply. */
  var RESPONSE_BY_REQUEST = {
    init: 'initData',
    import_selected: 'importResult',
    refresh_materials: 'materialsData',
    export_3mf: 'exportResult',
    run_diagnostics: 'diagnosticsResult',
    save_settings: 'settingsSaved',
    clear_bambuddy_key: 'settingsSaved',
    get_log: 'logData',
    browse_export_path: 'exportPathData',
    browse_slicer_path: 'slicerPathData',
    get_profiles: 'profilesData',
    import_profiles: 'profilesImported',
    openfilament_search: 'openFilamentData',
    openfilament_import: 'openFilamentImportResult',
    create_library: 'libraryCreated',
    bambuddy_context: 'bambuddyContextData',
    bambuddy_import: 'bambuddyImportResult'
  };

  /** Human-readable action names used in timeout/error messages. */
  var ACTION_LABELS = {
    init: 'initialise palette',
    import_selected: 'import materials',
    refresh_materials: 'refresh materials',
    export_3mf: 'export 3MF',
    run_diagnostics: 'run diagnostics',
    save_settings: 'save settings',
    clear_bambuddy_key: 'remove the Bambuddy API key',
    get_log: 'refresh log',
    browse_export_path: 'browse for the export path',
    browse_slicer_path: 'browse for the slicer executable',
    get_profiles: 'load printer profiles',
    import_profiles: 'import slicer profiles',
    openfilament_search: 'search the Open Filament Database',
    openfilament_import: 'import Open Filament Database filaments',
    create_library: 'create the Filamatch material library',
    bambuddy_context: 'load Bambuddy printers and AMS slots',
    bambuddy_import: 'import Bambuddy spool materials'
  };

  /** Milliseconds before an unanswered request is reported as a timeout. */
  var REQUEST_TIMEOUT_MS = 20000;

  /**
   * Native file dialogs are interactive: give them far longer than a normal
   * bridge round-trip before reporting a timeout.
   */
  var BROWSE_TIMEOUT_MS = 120000;

  /**
   * Slow actions run on a worker thread in Fusion, so long waits do not
   * freeze Fusion; these timeouts only bound how long the palette waits.
   */
  /** The export (plus optional printer preparation) can take minutes. */
  var EXPORT_TIMEOUT_MS = 300000;

  /** Profile scans read slicer installs, imported and remote presets. */
  var PROFILES_TIMEOUT_MS = 120000;

  /** The Open Filament Database's first search downloads the dataset. */
  var OFD_TIMEOUT_MS = 120000;

  /** No OFD search results requested per query. */
  var OFD_RESULT_LIMIT = 50;

  /** Rebuilding the custom material library can take a few seconds. */
  var CUSTOM_LIBRARY_TIMEOUT_MS = 120000;

  /** Bambuddy context/import read the printers/inventory/assignments endpoints. */
  var BAMBUDDY_TIMEOUT_MS = 45000;

  /** Quiet period before a design change refreshes the Bambuddy slot mapping. */
  var BAMBUDDY_REFRESH_DEBOUNCE_MS = 1500;

  /** Quiet period after a keystroke in the catalog search box. */
  var CATALOG_SEARCH_DEBOUNCE_MS = 150;

  /** Maximum number of log lines kept in the DOM. */
  var MAX_LOG_LINES = 1000;

  /** Delay between settings edits and the automatic `save_settings` request. */
  var SETTINGS_SAVE_DEBOUNCE_MS = 600;

  /** How many times to look for the Fusion bridge before falling back to demo. */
  var BRIDGE_RETRY_ATTEMPTS = 3;
  var BRIDGE_RETRY_DELAY_MS = 200;

  /** Fallback label for catalog entries without a category. */
  var UNCATEGORIZED = 'Uncategorized';

  /* =========================================================================
   * Demo data (standalone-browser preview only)
   * ======================================================================= */

  /* Demo profile repository + Open Filament Database fixtures (preview only). */
  var DEMO_PROFILE_VENDORS = [
    { id: 'bambu_installed', name: 'Bambu Lab (installed)', source: 'installed', has_printers: true },
    { id: 'prusa', name: 'Prusa Research', source: 'remote', has_printers: true },
    { id: 'creality', name: 'Creality', source: 'remote', has_printers: true },
    { id: 'elegoo', name: 'Elegoo', source: 'remote', has_printers: true },
    { id: 'imported', name: 'Imported profiles', source: 'imported', has_printers: true }
  ];

  var DEMO_PROFILE_PRINTERS = {
    bambu_installed: ['Bambu Lab X1 Carbon (0.4 nozzle)', 'Bambu Lab A1 (0.4 nozzle)'],
    prusa: ['Original Prusa MK4 (0.4 nozzle)', 'Original Prusa Mini+ (0.4 nozzle)'],
    creality: ['Creality Ender-3 V3 (0.4 nozzle)'],
    elegoo: ['Elegoo Neptune 4 Pro (0.4 nozzle)'],
    imported: ['Imported: My Custom Printer (0.4 nozzle)']
  };

  var DEMO_OFD_SUMMARY = {
    version: 'demo-2026.02',
    generated_at: '2026-02-01T00:00:00Z',
    counts: { filaments: 3120, brands: 148, variants: 8900 }
  };

  var DEMO_OFD_RESULTS = [
    { key: 'ofd_pla_basic_black', brand: 'Bambu Lab', material: 'PLA', filament: 'PLA Basic', variant: 'Black', color_hex: '#101010', density_g_cm3: 1.24, min_print_temp: 190, max_print_temp: 230, min_bed_temp: 35, max_bed_temp: 65 },
    { key: 'ofd_petg_hf_white', brand: 'Bambu Lab', material: 'PETG', filament: 'PETG HF', variant: 'White', color_hex: '#F5F5F5', density_g_cm3: 1.27, min_print_temp: 230, max_print_temp: 260, min_bed_temp: 60, max_bed_temp: 80 },
    { key: 'ofd_prusament_pla_orange', brand: 'Prusament', material: 'PLA', filament: 'PLA', variant: 'Orange', color_hex: '#FF6600', density_g_cm3: 1.24, min_print_temp: 200, max_print_temp: 220, min_bed_temp: 50, max_bed_temp: 60 },
    { key: 'ofd_polymaker_petg_blue', brand: 'Polymaker', material: 'PETG', filament: 'PolyLite PETG', variant: 'Blue', color_hex: '#1B4F9C', density_g_cm3: 1.27, min_print_temp: 230, max_print_temp: 250, min_bed_temp: 70, max_bed_temp: 80 },
    { key: 'ofd_esun_abs_black', brand: 'eSUN', material: 'ABS', filament: 'ABS+', variant: 'Black', color_hex: '#111111', density_g_cm3: 1.05, min_print_temp: 240, max_print_temp: 260, min_bed_temp: 90, max_bed_temp: 110 }
  ];

  /** Demo printer rows for one vendor. */
  function demoPrinterRows(vendorId) {
    var names = DEMO_PROFILE_PRINTERS[vendorId] || DEMO_PROFILE_PRINTERS.bambu_installed;
    var source = vendorId === 'bambu_installed' ? 'installed' : (vendorId === 'imported' ? 'imported' : 'remote');
    return names.map(function (name) {
      return {
        name: name,
        machine_path: '/Users/demo/Documents/FilamatchProfiles/' + name.replace(/[^A-Za-z0-9]+/g, '_') + '.json',
        default_process: '0.20mm Standard',
        source: source
      };
    });
  }

  /** Demo process rows for a printer. */
  function demoProcessRows() {
    return [
      { name: '0.20mm Standard', process_path: '/Users/demo/Documents/FilamatchProfiles/process_020_standard.json', default: true },
      { name: '0.12mm Fine', process_path: '/Users/demo/Documents/FilamatchProfiles/process_012_fine.json', default: false },
      { name: '0.28mm Draft', process_path: '/Users/demo/Documents/FilamatchProfiles/process_028_draft.json', default: false }
    ];
  }

  var DEMO_PROFILES = {
    slicers: [
      { kind: 'orca', name: 'OrcaSlicer', path: '/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer', version: '2.3.0' },
      { kind: 'bambu', name: 'Bambu Studio', path: '/Applications/BambuStudio.app/Contents/MacOS/BambuStudio', version: '02.02.00.55' }
    ],
    vendors: DEMO_PROFILE_VENDORS,
    printers: demoPrinterRows('bambu_installed'),
    processes: demoProcessRows(),
    imported: {
      machines: 2,
      processes: 3,
      filaments: 4,
      root: '/Users/demo/Documents/FilamatchProfiles'
    },
    selected: {
      slicer_path: '/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer',
      vendor: 'bambu_installed',
      printer: 'Bambu Lab X1 Carbon (0.4 nozzle)',
      process: '0.20mm Standard'
    }
  };

  var DEMO_INIT = {
    version: 'demo',
    design_name: 'Bracket assembly',
    catalog: [
      {
        id: 'bambu_pla_basic_black',
        display_name: 'Bambu Lab PLA Basic - Black',
        manufacturer: 'Bambu Lab',
        product_line: 'PLA Basic',
        color_name: 'Black',
        color_hex: '#161616',
        slicer_filament_type: 'PLA',
        density_g_cm3: 1.24,
        category: 'PLA'
      },
      {
        id: 'prusament_pla_orange',
        display_name: 'Prusament PLA - Orange',
        manufacturer: 'Prusament',
        product_line: 'PLA',
        color_name: 'Orange',
        color_hex: '#FF6600',
        slicer_filament_type: 'PLA',
        density_g_cm3: 1.24,
        category: 'PLA'
      },
      {
        id: 'bambu_petg_hf_white',
        display_name: 'Bambu Lab PETG HF - White',
        manufacturer: 'Bambu Lab',
        product_line: 'PETG HF',
        color_name: 'White',
        color_hex: '#FFFFFF',
        slicer_filament_type: 'PETG',
        density_g_cm3: 1.27,
        category: 'PETG'
      },
      {
        id: 'polymaker_polylite_abs_black',
        display_name: 'Polymaker PolyLite ABS - Black',
        manufacturer: 'Polymaker',
        product_line: 'PolyLite ABS',
        color_name: 'Black',
        color_hex: '#111111',
        slicer_filament_type: 'ABS',
        density_g_cm3: 1.04,
        category: 'ABS'
      },
      {
        id: 'generic_abs_gf30',
        display_name: 'Generic ABS-GF30 - Black',
        manufacturer: 'Generic',
        product_line: 'ABS-GF30',
        color_name: 'Black',
        color_hex: '#222222',
        slicer_filament_type: 'ABS-GF',
        density_g_cm3: 1.12,
        category: 'Fiber-Reinforced'
      }
    ],
    categories: [
      { name: 'PLA', filament_ids: ['bambu_pla_basic_black', 'prusament_pla_orange'] },
      { name: 'PETG', filament_ids: ['bambu_petg_hf_white'] },
      { name: 'ABS', filament_ids: ['polymaker_polylite_abs_black'] },
      { name: 'Fiber-Reinforced', filament_ids: ['generic_abs_gf30'] }
    ],
    materials: [
      {
        material_id: 'bambu_pla_basic_black',
        material_name: 'bambu_pla_basic_black',
        display_name: 'Bambu Lab PLA Basic - Black',
        color_hex: '#161616',
        filament_type: 'PLA',
        density_g_cm3: 1.24,
        category: 'PLA',
        body_count: 2,
        bodies: ['Bracket', 'Bracket:1'],
        slot: 1
      },
      {
        material_id: 'prusament_pla_orange',
        material_name: 'prusament_pla_orange',
        display_name: 'Prusament PLA - Orange',
        color_hex: '#FF6600',
        filament_type: 'PLA',
        density_g_cm3: 1.24,
        category: 'PLA',
        body_count: 1,
        bodies: ['Bushing'],
        slot: 2
      },
      {
        material_id: 'bambu_petg_hf_white',
        material_name: 'bambu_petg_hf_white',
        display_name: 'Bambu Lab PETG HF - White',
        color_hex: '#FFFFFF',
        filament_type: 'PETG',
        density_g_cm3: 1.27,
        category: 'PETG',
        body_count: 1,
        bodies: ['Cover'],
        slot: 3
      },
      {
        material_id: 'polymaker_polylite_abs_black',
        material_name: 'polymaker_polylite_abs_black',
        display_name: 'Polymaker PolyLite ABS - Black',
        color_hex: '#111111',
        filament_type: 'ABS',
        density_g_cm3: 1.04,
        category: 'ABS',
        body_count: 1,
        bodies: ['Housing'],
        slot: 4
      }
    ],
    settings: {
      output_path: '/Users/demo/Documents/Filamatch/assembly_AMS.3mf',
      write_project_settings: true,
      orca_enabled: true,
      orca_path: '/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer',
      orca_profile_dir: '',
      prepare_enabled: true,
      target_vendor: 'bambu_installed',
      target_printer: 'Bambu Lab X1 Carbon (0.4 nozzle)',
      target_process: '0.20mm Standard',
      slicer_path: '/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer',
      auto_library_update: true,
      custom_library_path: '/Users/demo/Documents/Filamatch/Filamatch Library.adsklib',
      bambuddy_url: 'http://192.168.1.20:8000',
      bambuddy_printer_id: 1,
      bambuddy_api_key_set: true,
      bambuddy_verify_tls: true
    },
    library: {
      path: '/Users/demo/Library/Application Support/Autodesk/Autodesk Fusion 360/API/Material Libraries/Fusion Material Library',
      loaded: true,
      writable: false,
      message: 'Stock Fusion library is read-only; imported filaments are copied into the active design.'
    },
    favorites: {
      count: 5,
      names: [
        'Bambu Lab PLA Basic - Black',
        'Prusament PLA - Orange',
        'Bambu Lab PETG HF - White',
        'Polymaker PolyLite ABS - Black',
        'Bambu Lab - PLA Basic - Blue Grey'
      ]
    },
    profiles: DEMO_PROFILES,
    bambuddy: { configured: true, key_set: true, url: 'http://192.168.1.20:8000', printer_id: 1, slot_map: {} },
    openfilament: {
      cached: true,
      version: 'demo-2026.02',
      generated_at: '2026-02-01',
      counts: { filaments: 3120, brands: 148 }
    },
    custom_library: {
      ok: true,
      path: '/Users/demo/Documents/Filamatch/Filamatch Library.adsklib',
      exists: true,
      loaded: true,
      material_count: 4,
      message: 'Demo library snapshot of the current Favorites.'
    },
    log: [
      'Palette opened outside Fusion - preview mode with sample data.',
      'Loaded catalog: 5 filaments in 4 categories.',
      'Favorites: 4 app-level materials available (demo).',
      'Profiles: 2 slicers and 5 vendors available (demo).',
      'Open Filament Database cached: 3120 filaments (demo).',
      'Custom library: 4 materials loaded from Filamatch Library.adsklib (demo).',
      'Scanned design: 4 materials on 5 bodies; slots assigned automatically (first appearance order).',
      'Import adds materials to Favorites and the design; export writes the slicer metadata.'
    ]
  };

  /** Demo printer for the preview: Bambuddy printer with six loaded slots. */
  var DEMO_BAMBUDDY = {
    printers: [
      { id: 1, name: 'Workshop H2D', model: 'H2D', label: 'Workshop H2D - H2D' },
      { id: 2, name: 'Studio P1S', model: 'P1S', label: 'Studio P1S - P1S' }
    ],
    slots: [
      { key: '1:0:0', label: 'AMS 1 - T1', display_name: 'Bambu Lab - PLA Basic - Black', filament_type: 'PLA', color_hex: '#161616', spool_id: 3, remaining_percent: 62, spool_preset: 'Bambu PLA Basic' },
      { key: '1:0:1', label: 'AMS 1 - T2', display_name: 'Bambu Lab - PLA Matte - Charcoal', filament_type: 'PLA', color_hex: '#333333', spool_id: 4, remaining_percent: 10, spool_preset: 'Bambu PLA Matte' },
      { key: '1:0:2', label: 'AMS 1 - T3', display_name: 'Bambu Lab - PLA Matte - Charcoal', filament_type: 'PLA', color_hex: '#333333', spool_id: 5, remaining_percent: 100, spool_preset: 'Bambu PLA Matte' },
      { key: '1:0:3', label: 'AMS 1 - T4', display_name: 'Bambu Lab - PETG HF - White', filament_type: 'PETG', color_hex: '#FFFFFF', spool_id: 7, remaining_percent: 80, spool_preset: 'Bambu PETG HF' },
      { key: '1:1:2', label: 'AMS 2 - T3', display_name: 'Bambu Lab - PLA Basic - Blue Grey', filament_type: 'PLA', color_hex: '#5B6579', spool_id: 9, remaining_percent: 44, spool_preset: 'Bambu PLA Basic' },
      { key: '1:1:3', label: 'AMS 2 - T4', display_name: 'Bambu Lab - ABS - Grey', filament_type: 'ABS', color_hex: '#4D4D4D', spool_id: 6, remaining_percent: 24, spool_preset: 'Bambu ABS' }
    ]
  };

  /* =========================================================================
   * Application state
   * ======================================================================= */

  var state = {
    demoMode: false,
    connectionReady: false,
    version: '',
    catalog: [],
    categories: [],
    materials: [],
    /** App-level Favorites summary {count, names} reported by Fusion. */
    favorites: { count: 0, names: [] },
    settings: normalizeSettings(null),
    settingsLoaded: false,
    /** Visible screen and Home data. */
    ui: {
      screen: 'home',
      designName: '',
      exported: false,
      lastDesignCheck: 0
    },
    /** Bambuddy printer/AMS context and manual material -> slot mapping. */
    bambuddy: {
      configured: false,
      keySet: false,
      url: '',
      selectedPrinterId: 0,
      printers: [],
      slots: [],
      mapping: [],
      slotMap: {},
      loaded: false,
      error: '',
      /** Design material ids the current slot mapping was requested for. */
      mappedMaterialsKey: '',
      /** Loaded-filament groups ticked for "Add to Fusion" (by slot key). */
      importKeys: new Set()
    },
    /** Currently active category chip (null = "All"). */
    activeCategory: null,
    selectedIds: new Set(),
    /** Cumulative log snapshots produced by the demo-mode response builders. */
    demoLog: [],
    /** Favorites names used by the standalone demo-mode import simulation. */
    demoFavoriteNames: [],
    /** Open Filament Database search/selection state. */
    openfilament: {
      results: [],
      selectedKeys: new Set(),
      cached: false,
      searched: false
    },
    /** Last reported custom material library status. */
    customLibrary: {
      path: '',
      exists: false,
      loaded: false,
      material_count: 0,
      message: '',
      ok: false
    }
  };

  /** Queue for replies that arrive before DOMContentLoaded finishes. */
  var earlyMessages = [];
  var uiReady = false;

  /** Pending debounce timer for autosaving the settings form. */
  var settingsSaveTimer = null;

  /** Pending debounce timers for the Bambuddy refresh and the catalog search. */
  var bambuddyRefreshTimer = null;
  var catalogSearchTimer = null;

  /** Category index + chip counts, rebuilt only when the catalog changes. */
  var catalogIndexCache = null;

  /** True when a Home rebuild waits for a focused slot menu to close. */
  var filamentListDeferred = false;

  /* Log-panel model (mirrors the DOM so it can be cleared/rebuilt safely). */
  var logEntries = [];
  var lastFusionLogSnapshot = [];

  /* Pending-request bookkeeping. */
  var pendingByResponse = new Map();
  var pendingActionCounts = new Map();
  var pendingCount = 0;

  /* =========================================================================
   * Small utilities
   * ======================================================================= */

  /**
   * Return a DOM element by id (null when absent).
   * @param {string} id
   * @returns {HTMLElement|null}
   */
  function byId(id) {
    return document.getElementById(id);
  }

  /**
   * Coerce a value to a string without ever producing "null"/"undefined".
   * @param {*} value
   * @returns {string}
   */
  function asString(value) {
    if (value === null || value === undefined) {
      return '';
    }
    return String(value);
  }

  /**
   * Return a human-readable message for an Error-like value.
   * @param {*} err
   * @returns {string}
   */
  function errorMessage(err) {
    if (err && err.message) {
      return asString(err.message);
    }
    return asString(err);
  }

  /**
   * Normalise `#RRGGBB` / `#RGB` into an uppercase 6-digit hex string.
   * Returns '' for anything that does not look like a CSS hex colour, which
   * keeps untrusted color values out of inline styles.
   * @param {*} value
   * @returns {string}
   */
  function normalizeHex(value) {
    var hex = asString(value).trim();
    if (/^#[0-9a-fA-F]{6}$/.test(hex)) {
      return hex.toUpperCase();
    }
    if (/^#[0-9a-fA-F]{3}$/.test(hex)) {
      return ('#' + hex.charAt(1) + hex.charAt(1) +
        hex.charAt(2) + hex.charAt(2) +
        hex.charAt(3) + hex.charAt(3)).toUpperCase();
    }
    return '';
  }

  /**
   * Format a density value as e.g. "1.24 g/cm3" (with a superscript glyph).
   * @param {*} value
   * @returns {string}
   */
  function formatDensity(value) {
    var density = Number(value);
    if (!isFinite(density) || density <= 0) {
      return '\u2014';
    }
    var rounded = Math.round(density * 100) / 100;
    return rounded + ' g/cm\u00b3';
  }

  /**
   * Zero-pad a number to two digits.
   * @param {number|string} value
   * @returns {string}
   */
  function pad2(value) {
    var text = asString(value);
    return text.length < 2 ? '0' + text : text;
  }

  /**
   * Format the current local time as HH:MM:SS for log lines.
   * @param {Date} [date]
   * @returns {string}
   */
  function formatClock(date) {
    var d = date || new Date();
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  /**
   * Pluralise a noun for a count, e.g. pluralize(1, 'object') -> '1 object'.
   * @param {number} count
   * @param {string} singular
   * @param {string} [plural]
   * @returns {string}
   */
  function pluralize(count, singular, plural) {
    return count + ' ' + (count === 1 ? singular : (plural || singular + 's'));
  }

  /**
   * Build the display label for a catalog entry.
   * @param {object} entry
   * @returns {string}
   */
  function displayName(entry) {
    if (!entry || typeof entry !== 'object') {
      return 'Unnamed filament';
    }
    var name = asString(entry.display_name);
    if (name) {
      return name;
    }
    var parts = [asString(entry.manufacturer), asString(entry.product_line), asString(entry.color_name)];
    var joined = parts.filter(function (part) { return part.length > 0; }).join(' ');
    if (joined) {
      return joined;
    }
    return asString(entry.id) || 'Unnamed filament';
  }

  /**
   * Normalise a settings payload from Python into the palette's shape.
   * @param {object|null|undefined} raw
   * @returns {{output_path: string, write_project_settings: boolean,
   *            orca_enabled: boolean, orca_path: string, orca_profile_dir: string}}
   */
  function normalizeSettings(raw) {
    var settings = (raw && typeof raw === 'object') ? raw : {};
    return {
      output_path: asString(settings.output_path),
      write_project_settings: settings.write_project_settings !== false,
      orca_enabled: settings.orca_enabled === true,
      orca_path: asString(settings.orca_path),
      orca_profile_dir: asString(settings.orca_profile_dir),
      prepare_enabled: settings.prepare_enabled === true,
      target_vendor: asString(settings.target_vendor),
      target_printer: asString(settings.target_printer),
      target_process: asString(settings.target_process),
      slicer_path: asString(settings.slicer_path),
      auto_library_update: settings.auto_library_update !== false,
      custom_library_path: asString(settings.custom_library_path),
      bambuddy_url: asString(settings.bambuddy_url),
      bambuddy_printer_id: Number(settings.bambuddy_printer_id) || 0,
      bambuddy_slot_map: (settings.bambuddy_slot_map && typeof settings.bambuddy_slot_map === 'object')
        ? settings.bambuddy_slot_map : {},
      bambuddy_api_key_set: settings.bambuddy_api_key_set === true,
      bambuddy_verify_tls: settings.bambuddy_verify_tls !== false
    };
  }

  /**
   * Normalise a Favorites summary payload ({count, names[]}) from Fusion.
   * @param {*} raw
   * @returns {{count: number, names: string[]}|null} null when not reported.
   */
  function normalizeFavorites(raw) {
    if (!raw || typeof raw !== 'object') {
      return null;
    }
    var names = [];
    if (Array.isArray(raw.names)) {
      for (var i = 0; i < raw.names.length; i += 1) {
        var name = asString(raw.names[i]).trim();
        if (name) {
          names.push(name);
        }
      }
    }
    var count = Number(raw.count);
    if (!isFinite(count) || count < 0) {
      count = names.length;
    }
    return { count: Math.floor(count), names: names };
  }

  /**
   * Count the bodies an importResult row relinked. Python currently reports an
   * integer count, but tolerate an array of body names too.
   * @param {object} row
   * @returns {number}
   */
  function relinkedBodiesCount(row) {
    if (!row || typeof row !== 'object') {
      return 0;
    }
    var value = row.relinked_bodies;
    if (typeof value === 'number' && isFinite(value) && value > 0) {
      return Math.floor(value);
    }
    if (Array.isArray(value)) {
      return value.length;
    }
    return 0;
  }

  /**
   * Create a colour swatch element for a catalog/material color_hex value.
   * @param {*} colorHex
   * @param {string} label
   * @param {string} [extraClass]
   * @returns {HTMLElement}
   */
  function createSwatch(colorHex, label, extraClass) {
    var swatch = document.createElement('span');
    swatch.className = 'swatch' + (extraClass ? ' ' + extraClass : '');
    var hex = normalizeHex(colorHex);
    if (hex) {
      swatch.style.backgroundColor = hex;
      swatch.title = label ? label + ' (' + hex + ')' : hex;
    } else {
      swatch.classList.add('swatch--unknown');
      swatch.title = label || 'Unknown color';
    }
    return swatch;
  }

  /**
   * Create a type chip element.
   * @param {string} label
   * @param {string} [title]
   * @returns {HTMLElement}
   */
  function createChip(label, title) {
    var chip = document.createElement('span');
    chip.className = 'chip';
    chip.textContent = label || '\u2014';
    if (title) {
      chip.title = title;
    }
    return chip;
  }

  /**
   * Replace a <select>'s options safely (createElement/textContent only).
   * Each entry is either a string or {value, label, title, disabled}.
   * @param {HTMLSelectElement|null} select
   * @param {Array<string|object>} entries
   * @param {{placeholder?: string, preferred?: string}} [options]
   * @returns {string} the value that ended up selected ('' when none matched)
   */
  function setSelectOptions(select, entries, options) {
    if (!select) {
      return '';
    }
    var opts = options || {};
    var previous = opts.preferred !== undefined ? opts.preferred : select.value;
    select.textContent = '';

    var placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = opts.placeholder || '\u2014';
    select.appendChild(placeholder);

    var matched = false;
    for (var i = 0; i < entries.length; i += 1) {
      var item = entries[i];
      var isObject = item !== null && typeof item === 'object';
      var value = isObject ? asString(item.value) : asString(item);
      var label = isObject ? (asString(item.label) || value) : asString(item);
      if (!value && !label) {
        continue;
      }
      var option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      if (isObject && item.title) {
        option.title = item.title;
      }
      if (isObject && item.disabled) {
        option.disabled = true;
      }
      if (value === asString(previous)) {
        option.selected = true;
        matched = true;
      }
      select.appendChild(option);
    }

    if (!matched) {
      select.value = '';
    }
    select.disabled = entries.length === 0;
    return matched ? asString(previous) : '';
  }

  /**
   * Read the trimmed value of a select by element id.
   * @param {string} id
   * @returns {string}
   */
  function selectValue(id) {
    var select = byId(id);
    return select ? asString(select.value).trim() : '';
  }

  /**
   * Remember which element inside `container` has keyboard focus (by its
   * `data-focus-key`) so focus survives a rebuild of the container.
   * @param {HTMLElement|null} container
   * @returns {function()} call after rendering to restore the focus
   */
  function keepFocus(container) {
    var active = document.activeElement;
    if (!container || !active || active === container || !container.contains(active)) {
      return function () {};
    }
    var key = active.getAttribute('data-focus-key');
    return function () {
      if (key === null || container.contains(document.activeElement)) {
        return;
      }
      var candidates = container.querySelectorAll('[data-focus-key]');
      for (var i = 0; i < candidates.length; i += 1) {
        if (candidates[i].getAttribute('data-focus-key') === key) {
          candidates[i].focus();
          return;
        }
      }
    };
  }

  /* =========================================================================
   * Status lines
   * ======================================================================= */

  /**
   * Write a status message and its severity variant.
   * @param {HTMLElement|null} element
   * @param {string} text
   * @param {'muted'|'info'|'success'|'warning'|'error'} [kind]
   */
  function setStatus(element, text, kind) {
    if (!element) {
      return;
    }
    element.textContent = asString(text);
    element.classList.remove('status--muted', 'status--info', 'status--success', 'status--warning', 'status--error');
    element.classList.add('status', 'status--' + (kind || 'muted'));
  }

  /**
   * Update the header connection line.
   * @param {'muted'|'info'|'success'|'warning'|'error'} kind
   * @param {string} text
   */
  function setConnectionState(kind, text) {
    setStatus(byId('connection-status'), text, kind);
  }

  /**
   * Show or hide the standalone-browser preview banner.
   * @param {boolean} visible
   */
  function showDemoBanner(visible) {
    var banner = byId('demo-banner');
    if (banner) {
      banner.hidden = !visible;
    }
  }

  /**
   * Suffix for status messages produced by simulated demo replies.
   * @param {string} text
   * @returns {string}
   */
  function demoSuffix(text) {
    return state.demoMode ? text + ' (demo)' : text;
  }

  /**
   * Text of a failed reply (`error` when `ok` is false).
   * @param {object} payload
   * @returns {string}
   */
  function replyError(payload) {
    return asString(payload && payload.error) || 'unknown error';
  }

  /* =========================================================================
   * Log panel
   * ======================================================================= */

  /**
   * Create a single, safely rendered log line element.
   * @param {{time: string, text: string, kind: string}} entry
   * @returns {HTMLElement}
   */
  function createLogLineElement(entry) {
    var line = document.createElement('div');
    line.className = 'log-line';
    if (entry.kind === 'warning') {
      line.classList.add('log-line--warning');
    } else if (entry.kind === 'error') {
      line.classList.add('log-line--error');
    }

    var time = document.createElement('span');
    time.className = 'log-line__time';
    time.textContent = '[' + entry.time + ']';

    var text = document.createElement('span');
    text.className = 'log-line__text';
    text.textContent = entry.text;

    line.appendChild(time);
    line.appendChild(text);
    return line;
  }

  /**
   * Update the log line counter.
   */
  function updateLogCount() {
    var count = byId('log-count');
    if (count) {
      count.textContent = pluralize(logEntries.length, 'line');
    }
  }

  /**
   * Ensure the "Log is empty." placeholder exists when there are no entries.
   */
  function syncLogPlaceholder() {
    var list = byId('log-list');
    if (!list) {
      return;
    }
    var placeholder = list.querySelector('.log-empty');
    if (logEntries.length === 0 && !placeholder) {
      var empty = document.createElement('p');
      empty.className = 'log-empty';
      empty.textContent = 'Log is empty.';
      list.appendChild(empty);
    }
  }

  /**
   * Append one already-prepared log entry (model + DOM + auto-scroll).
   * @param {{time: string, text: string, kind: string}} entry
   */
  function appendLogEntry(entry) {
    var list = byId('log-list');
    if (!list) {
      return;
    }
    var placeholder = list.querySelector('.log-empty');
    if (placeholder) {
      placeholder.remove();
    }
    logEntries.push(entry);
    list.appendChild(createLogLineElement(entry));

    while (logEntries.length > MAX_LOG_LINES) {
      logEntries.shift();
      if (list.firstChild) {
        list.removeChild(list.firstChild);
      }
    }

    updateLogCount();
    list.scrollTop = list.scrollHeight;
  }

  /**
   * Append UI-originated message(s) (newline separated) with the current clock.
   * All text goes through textContent, so untrusted data cannot inject markup.
   * @param {*} rawText
   * @param {'info'|'warning'|'error'} [kind]
   */
  function appendLog(rawText, kind) {
    var text = asString(rawText);
    if (!text) {
      return;
    }
    var lines = text.split(/\r?\n/);
    var now = formatClock(new Date());
    for (var i = 0; i < lines.length; i += 1) {
      if (lines[i].length === 0 && lines.length > 1) {
        continue;
      }
      appendLogEntry({ time: now, text: lines[i], kind: kind || 'info' });
    }
  }

  /**
   * Append one line from Python's session log. Python prefixes lines with
   * "HH:MM:SS " already; use that timestamp instead of adding a duplicate.
   * @param {*} rawLine
   * @param {'info'|'warning'|'error'} [kind]
   */
  function appendFusionLogLine(rawLine, kind) {
    var text = asString(rawLine);
    if (!text) {
      return;
    }
    var lines = text.split(/\r?\n/);
    var fallbackTime = formatClock(new Date());
    for (var i = 0; i < lines.length; i += 1) {
      var match = /^(\d{1,2}:\d{2}:\d{2})\s+(.*)$/.exec(lines[i]);
      if (match) {
        appendLogEntry({
          time: pad2(match[1].split(':')[0]) + ':' + pad2(match[1].split(':')[1]) + ':' + pad2(match[1].split(':')[2]),
          text: match[2],
          kind: kind || 'info'
        });
      } else if (lines[i].length > 0) {
        appendLogEntry({ time: fallbackTime, text: lines[i], kind: kind || 'info' });
      }
    }
  }

  /**
   * Clear the visible log without resetting the Fusion-log watermark, so the
   * palette only shows genuinely new Fusion log lines afterwards.
   */
  function clearLogDisplay() {
    var list = byId('log-list');
    if (!list) {
      return;
    }
    list.textContent = '';
    logEntries.length = 0;
    syncLogPlaceholder();
    updateLogCount();
  }

  /**
   * Replace the log panel with a full snapshot sent by Fusion.
   * @param {string[]} snapshot
   */
  function rebuildLog(snapshot) {
    lastFusionLogSnapshot = snapshot.slice();
    var list = byId('log-list');
    if (!list) {
      return;
    }
    list.textContent = '';
    logEntries.length = 0;
    for (var i = 0; i < snapshot.length; i += 1) {
      appendFusionLogLine(snapshot[i], 'info');
    }
    if (snapshot.length === 0) {
      syncLogPlaceholder();
      updateLogCount();
    }
    list.scrollTop = list.scrollHeight;
  }

  /**
   * Consume the cumulative `log` array included in most Fusion replies.
   * Appends only the new tail; rebuilds for explicit refreshes or when the
   * Python-side log shrank/changed history.
   * @param {*} rawLines
   * @param {{replace?: boolean}} [options]
   */
  function ingestLogSnapshot(rawLines, options) {
    if (!Array.isArray(rawLines)) {
      return;
    }
    var opts = options || {};
    var snapshot = [];
    for (var i = 0; i < rawLines.length; i += 1) {
      snapshot.push(asString(rawLines[i]));
    }

    if (opts.replace) {
      rebuildLog(snapshot);
      return;
    }

    var previous = lastFusionLogSnapshot;
    var isExtension = snapshot.length > previous.length;
    if (isExtension) {
      for (var j = 0; j < previous.length; j += 1) {
        if (snapshot[j] !== previous[j]) {
          isExtension = false;
          break;
        }
      }
    }

    if (isExtension) {
      lastFusionLogSnapshot = snapshot.slice();
      for (var k = previous.length; k < snapshot.length; k += 1) {
        appendFusionLogLine(snapshot[k], 'info');
      }
      return;
    }

    var isSame = snapshot.length === previous.length && snapshot.every(function (line, index) {
      return line === previous[index];
    });
    if (isSame) {
      return;
    }

    rebuildLog(snapshot);
  }

  /**
   * Append optional `log_tail` lines from a reply (array or text) to the log.
   * Unlike `log`, a tail is incremental, so it is always appended.
   * @param {*} tail
   */
  function appendLogTail(tail) {
    if (!tail) {
      return;
    }
    if (Array.isArray(tail)) {
      for (var i = 0; i < tail.length; i += 1) {
        appendFusionLogLine(tail[i], 'info');
      }
      return;
    }
    appendFusionLogLine(tail, 'info');
  }

  /* =========================================================================
   * Busy indicator + pending requests
   * ======================================================================= */

  /**
   * Show/hide the header spinner based on the number of in-flight requests.
   */
  function updateBusyIndicator() {
    var indicator = byId('busy-indicator');
    if (indicator) {
      indicator.hidden = pendingCount === 0;
    }
    if (document.body) {
      document.body.setAttribute('aria-busy', pendingCount > 0 ? 'true' : 'false');
    }
  }

  /**
   * @param {string} requestAction
   * @returns {boolean} true when at least one request of this action is pending.
   */
  function isActionPending(requestAction) {
    return (pendingActionCounts.get(requestAction) || 0) > 0;
  }

  /**
   * Register a pending request, show its loading status, and arm a timeout.
   * @param {string} requestAction
   * `context` is handed back to the reply handler; `onFail` runs when the
   * request times out or cannot be sent.
   * @param {{statusEl?: HTMLElement|null, pendingText?: string,
   *          button?: HTMLElement|null, timeoutMs?: number,
   *          context?: *, onFail?: function(string)}} options
   * @returns {object} the pending entry
   */
  function beginPending(requestAction, options) {
    var opts = options || {};
    var responseAction = RESPONSE_BY_REQUEST[requestAction] || requestAction;
    var entry = {
      requestAction: requestAction,
      responseAction: responseAction,
      statusEl: opts.statusEl || null,
      button: opts.button || null,
      timeoutMs: opts.timeoutMs || REQUEST_TIMEOUT_MS,
      context: opts.context,
      onFail: opts.onFail || null,
      timer: null
    };

    if (entry.statusEl && opts.pendingText) {
      setStatus(entry.statusEl, opts.pendingText, 'info');
    }
    if (entry.button) {
      entry.button.disabled = true;
    }

    entry.timer = window.setTimeout(function () {
      handleRequestTimeout(entry);
    }, entry.timeoutMs);

    if (!pendingByResponse.has(responseAction)) {
      pendingByResponse.set(responseAction, []);
    }
    pendingByResponse.get(responseAction).push(entry);

    pendingCount += 1;
    pendingActionCounts.set(requestAction, (pendingActionCounts.get(requestAction) || 0) + 1);
    updateBusyIndicator();
    return entry;
  }

  /**
   * Release one pending entry: drop it from its queue, stop its timer,
   * re-enable its button and update the busy counters.
   * @param {object} entry
   */
  function releasePending(entry) {
    var queue = pendingByResponse.get(entry.responseAction);
    if (queue) {
      var index = queue.indexOf(entry);
      if (index !== -1) {
        queue.splice(index, 1);
      }
      if (queue.length === 0) {
        pendingByResponse.delete(entry.responseAction);
      }
    }
    if (entry.timer !== null) {
      window.clearTimeout(entry.timer);
      entry.timer = null;
    }
    if (entry.button) {
      entry.button.disabled = false;
    }

    pendingCount = Math.max(0, pendingCount - 1);
    var actionCount = (pendingActionCounts.get(entry.requestAction) || 1) - 1;
    if (actionCount <= 0) {
      pendingActionCounts.delete(entry.requestAction);
    } else {
      pendingActionCounts.set(entry.requestAction, actionCount);
    }
    updateBusyIndicator();
  }

  /**
   * Resolve the oldest pending request for a reply action. Replies carry no
   * request id, so requests that must not overlap are refused while one of
   * the same action is pending (see isActionPending).
   * @param {string} responseAction
   * @returns {object|null} the released entry (with its `context`), or null
   */
  function endPending(responseAction) {
    var queue = pendingByResponse.get(responseAction);
    if (!queue || queue.length === 0) {
      return null;
    }
    var entry = queue[0];
    releasePending(entry);
    return entry;
  }

  /**
   * Report an unanswered request instead of leaving the UI stuck on "loading".
   * @param {object} entry
   */
  function handleRequestTimeout(entry) {
    entry.timer = null;
    releasePending(entry);

    var label = ACTION_LABELS[entry.requestAction] || entry.requestAction;
    var message = 'Fusion did not answer the "' + label + '" request within ' +
      (entry.timeoutMs / 1000) + ' s. Check the add-in log.';
    setStatus(entry.statusEl, message, 'error');
    appendLog(message, 'error');
    setConnectionState('warning', 'Fusion is not responding to palette requests.');
    if (entry.onFail) {
      entry.onFail(message);
    }
  }

  /* =========================================================================
   * Fusion bridge
   * ======================================================================= */

  /**
   * @returns {boolean} true when the `adsk.fusionSendData` bridge exists.
   */
  function hasFusionBridge() {
    return typeof adsk !== 'undefined' && adsk !== null &&
      typeof adsk.fusionSendData === 'function';
  }

  /**
   * @returns {boolean} true when requests go to real Fusion. Once the palette
   *   started in demo mode it stays there, even if a bridge appears later, so
   *   simulated data is never mixed with real Fusion actions.
   */
  function bridgeLive() {
    return !state.demoMode && hasFusionBridge();
  }

  /**
   * Send a request to Python and register it for pending/timeout bookkeeping.
   * Falls back to a simulated reply in standalone demo mode.
   * @param {string} action
   * @param {object} payload
   * @param {{statusEl?: HTMLElement|null, pendingText?: string,
   *          button?: HTMLElement|null, timeoutMs?: number,
   *          context?: *, onFail?: function(string)}} [options]
   * @returns {boolean} false when the request could not be sent
   */
  function request(action, payload, options) {
    var opts = options || {};
    var entry = beginPending(action, opts);

    var dataString;
    try {
      dataString = JSON.stringify(payload || {});
    } catch (err) {
      releasePending(entry);
      var buildMessage = 'Could not serialise the "' + action + '" request: ' + errorMessage(err);
      setStatus(opts.statusEl, buildMessage, 'error');
      appendLog(buildMessage, 'error');
      return false;
    }

    if (!bridgeLive()) {
      scheduleDemoResponse(action, payload || {});
      return true;
    }

    try {
      adsk.fusionSendData(action, dataString);
    } catch (err) {
      releasePending(entry);
      var sendMessage = 'Failed to send "' + action + '" to Fusion: ' + errorMessage(err);
      setStatus(opts.statusEl, sendMessage, 'error');
      appendLog(sendMessage, 'error');
      setConnectionState('error', 'Fusion bridge error - see the log.');
      if (opts.onFail) {
        opts.onFail(sendMessage);
      }
      return false;
    }
    return true;
  }

  /* =========================================================================
   * Incoming message dispatch
   * ======================================================================= */

  /**
   * Handle a message pushed by Python via palette.sendInfoToHTML.
   *
   * Defined as a global exactly as Fusion expects. Returns `true` in every
   * case (including malformed JSON) so Fusion never reports a JS error.
   * Never sends anything back for a reply action.
   *
   * @param {string} action
   * @param {string|object} dataString
   * @returns {boolean}
   */
  function handleFusionMessage(action, dataString) {
    var payload = {};
    if (dataString && typeof dataString === 'object') {
      payload = dataString;
    } else if (dataString !== null && dataString !== undefined && asString(dataString).trim() !== '') {
      try {
        payload = JSON.parse(asString(dataString));
      } catch (err) {
        var parseMessage = 'Could not parse the "' + asString(action) + '" message from Fusion: ' + errorMessage(err);
        appendLog(parseMessage, 'error');
        if (uiReady) {
          setConnectionState('warning', 'Received malformed data from Fusion - see the log.');
        }
        return true;
      }
    }

    if (!payload || typeof payload !== 'object') {
      payload = {};
    }

    if (!uiReady) {
      earlyMessages.push({ action: asString(action), payload: payload });
      return true;
    }

    dispatchIncoming(asString(action), payload);
    return true;
  }

  /** Install the Fusion -> HTML entry point before the palette can receive data. */
  window.fusionJavaScriptHandler = {
    handle: handleFusionMessage
  };

  /**
   * Route one parsed reply to its UI handler, always releasing the matching
   * pending request and never letting a handler exception escape.
   * @param {string} action
   * @param {object} payload
   */
  function dispatchIncoming(action, payload) {
    var entry = endPending(action);
    try {
      switch (action) {
        case 'initData':
          handleInitData(payload);
          break;
        case 'importResult':
          handleImportResult(payload);
          break;
        case 'materialsData':
          handleMaterialsData(payload);
          break;
        case 'exportResult':
          handleExportResult(payload);
          break;
        case 'diagnosticsResult':
          handleDiagnosticsResult(payload);
          break;
        case 'settingsSaved':
          handleSettingsSaved(payload, entry);
          break;
        case 'logData':
          handleLogData(payload);
          break;
        case 'exportPathData':
          handleExportPathData(payload, entry);
          break;
        case 'slicerPathData':
          handleSlicerPathData(payload);
          break;
        case 'profilesData':
          handleProfilesData(payload, entry);
          break;
        case 'profilesImported':
          handleProfilesImported(payload);
          break;
        case 'openFilamentData':
          handleOpenFilamentData(payload);
          break;
        case 'openFilamentImportResult':
          handleOpenFilamentImportResult(payload);
          break;
        case 'libraryCreated':
          handleLibraryCreated(payload);
          break;
        case 'bambuddyContextData':
          handleBambuddyContextData(payload, entry);
          break;
        case 'bambuddyImportResult':
          handleBambuddyImportResult(payload);
          break;
        case 'materialBrowserData':
          handleMaterialBrowserData(payload);
          break;
        case 'response':
          /* Fusion echoes every sendInfoToHTML return value back as a
             "response" event. Ignore it silently: replying would create an
             infinite feedback loop. */
          break;
        default:
          appendLog('Ignored unsupported message from Fusion: ' + action, 'warning');
          break;
      }
    } catch (err) {
      var message = 'Palette error while handling "' + action + '": ' + errorMessage(err);
      appendLog(message, 'error');
      setConnectionState('error', 'Palette UI error - see the log.');
    }
  }

  /**
   * Flush replies that arrived before the DOM was ready.
   */
  function flushEarlyMessages() {
    while (earlyMessages.length > 0) {
      var message = earlyMessages.shift();
      dispatchIncoming(message.action, message.payload);
    }
  }

  /* =========================================================================
   * Reply handlers
   * ======================================================================= */

  /**
   * Show (or hide, with '') the visible start-up error banner with a Retry
   * button. Without it a failed `init` would leave Home on "Loading..." with
   * the error only in the screen-reader status line.
   * @param {string} message
   */
  function showInitError(message) {
    var banner = byId('init-error');
    var text = byId('init-error-text');
    if (text) {
      text.textContent = message;
    }
    if (banner) {
      banner.hidden = !message;
    }
    if (message) {
      setStatus(byId('materials-status'), 'Filamatch could not load the design from Fusion.', 'error');
      var summary = byId('print-settings-summary');
      if (summary && !state.connectionReady) {
        summary.textContent = 'Not available until Fusion answers';
      }
    }
  }

  /**
   * `initData`: populate version, catalog, categories, assembly materials,
   * settings and library status.
   * @param {object} payload
   */
  function handleInitData(payload) {
    if (payload.ok === false) {
      var initError = 'Fusion could not initialise the palette: ' + replyError(payload);
      ingestLogSnapshot(payload.log, { replace: true });
      setConnectionState('error', initError);
      setStatus(byId('catalog-status'), initError, 'error');
      appendLog(initError, 'error');
      showInitError(initError);
      return;
    }

    showInitError('');
    state.version = asString(payload.version) || 'unknown';
    state.catalog = Array.isArray(payload.catalog) ? payload.catalog.slice() : [];
    state.categories = normalizeCategories(payload.categories);
    catalogIndexCache = null;
    state.materials = [];
    state.settings = normalizeSettings(payload.settings);
    state.activeCategory = null;
    state.selectedIds.clear();
    state.settingsLoaded = true;
    state.connectionReady = true;

    pruneSelection();
    renderVersion();
    renderCatalog();
    renderMaterials(Array.isArray(payload.materials) ? payload.materials : []);
    renderSettings(state.settings);
    updateLibraryStatus(payload.library);
    state.favorites = normalizeFavorites(payload.favorites) || { count: 0, names: [] };
    renderFavoritesStatus(payload.favorites);
    renderCustomLibrary(payload.custom_library);
    syncPrepareUi();
    if (payload.profiles && typeof payload.profiles === 'object') {
      renderProfileSelects(payload.profiles, {
        vendor: state.settings.target_vendor,
        printer: state.settings.target_printer
      });
    }
    renderOpenFilamentSummary(payload.openfilament);
    state.ui.designName = asString(payload.design_name);
    applyBambuddyState(payload.bambuddy);
    renderExportDetails(null);
    renderSlicerSummary();
    renderHome();
    showScreen(shouldShowWelcome() ? 'welcome' : state.ui.screen);

    var catalogStatus = byId('catalog-status');
    if (state.demoMode) {
      setConnectionState('warning', 'Preview mode - open this palette from Fusion to connect. Showing demo data.');
      showDemoBanner(true);
      setStatus(catalogStatus, 'Demo catalog loaded; actions below are simulated.', 'warning');
    } else {
      setConnectionState('success', 'Connected to Fusion - add-in v' + state.version + '.');
      setStatus(catalogStatus, 'Loaded ' + pluralize(state.catalog.length, 'filament') +
        ' in ' + pluralize(getCategoryIndex().categoryNames.length, 'category', 'categories') + '.', 'success');
    }

    setStatus(byId('export-status'), '', 'muted');

    var hadLog = Array.isArray(payload.log) && payload.log.length > 0;
    ingestLogSnapshot(payload.log, { replace: true });
    if (!hadLog) {
      appendLog('Connected to Fusion (add-in v' + state.version + ').', 'info');
    }

    /* Populate the printer/profile selects from the live slicer installs. */
    if (!isActionPending('get_profiles')) {
      requestProfiles({
        vendor: state.settings.target_vendor,
        printer: state.settings.target_printer
      });
    }
  }

  /**
   * Normalise the optional `categories` array from initData.
   * @param {*} raw
   * @returns {{name: string, filament_ids: string[]}[]}
   */
  function normalizeCategories(raw) {
    var categories = [];
    if (!Array.isArray(raw)) {
      return categories;
    }
    for (var i = 0; i < raw.length; i += 1) {
      var item = raw[i];
      if (typeof item === 'string') {
        categories.push({ name: item, filament_ids: [] });
      } else if (item && typeof item === 'object') {
        categories.push({
          name: asString(item.name) || UNCATEGORIZED,
          filament_ids: Array.isArray(item.filament_ids)
            ? item.filament_ids.map(asString)
            : []
        });
      }
    }
    return categories;
  }

  /**
   * `importResult`: report imported materials/appearances and refresh the
   * assembly materials + library status if the reply includes them.
   * @param {object} payload
   */
  function handleImportResult(payload) {
    ingestLogSnapshot(payload.log);

    if (Array.isArray(payload.materials)) {
      renderMaterials(payload.materials);
    }
    if (payload.library) {
      updateLibraryStatus(payload.library);
    }

    var status = byId('catalog-status');
    if (payload.ok === false) {
      var failure = demoSuffix('Import failed: ' + replyError(payload));
      setStatus(status, failure, 'error');
      appendLog(failure, 'error');
      return;
    }

    var imported = Array.isArray(payload.imported) ? payload.imported : [];
    var errors = Array.isArray(payload.errors) ? payload.errors : [];
    var favorites = normalizeFavorites(payload.favorites);
    if (favorites) {
      state.favorites = favorites;
      renderFavoritesStatus(favorites);
    }
    if (payload.custom_library) {
      renderCustomLibrary(payload.custom_library);
    }

    var favoriteCount = favorites ? favorites.count : 0;
    var replacedCount = 0;
    var relinkedCount = 0;
    for (var importedIndex = 0; importedIndex < imported.length; importedIndex += 1) {
      var importedRow = imported[importedIndex] || {};
      if (importedRow.replaced === true) {
        replacedCount += 1;
      }
      relinkedCount += relinkedBodiesCount(importedRow);
    }
    if (!favorites) {
      /* Fallback when Fusion did not send a Favorites summary: count the
         import rows that carry a favorite_name. */
      for (var favoriteIndex = 0; favoriteIndex < imported.length; favoriteIndex += 1) {
        if (asString(imported[favoriteIndex] && imported[favoriteIndex].favorite_name)) {
          favoriteCount += 1;
        }
      }
    }

    var summary;
    var kind;
    if (errors.length === 0 && imported.length > 0) {
      var summaryParts = [pluralize(imported.length, 'filament') + ' imported'];
      summaryParts.push(favoriteCount + ' in Favorites');
      if (replacedCount > 0) {
        summaryParts.push(replacedCount + ' replaced an existing material');
      }
      if (relinkedCount > 0) {
        summaryParts.push(pluralize(relinkedCount, 'body', 'bodies') + ' relinked');
      }
      summary = demoSuffix(summaryParts.join(' \u2014 ') + '.');
      kind = 'success';
    } else if (imported.length > 0) {
      summary = demoSuffix('Imported ' + imported.length + ' filament(s) (' +
        favoriteCount + ' in Favorites); ' + errors.length +
        ' failed: ' + summarizeErrors(errors));
      kind = 'warning';
    } else if (errors.length > 0) {
      summary = demoSuffix('Import failed: ' + summarizeErrors(errors));
      kind = 'error';
    } else {
      summary = demoSuffix('Fusion reported no imported materials.');
      kind = 'warning';
    }
    setStatus(status, summary, kind);
    appendLog(summary, kind === 'error' ? 'error' : (kind === 'warning' ? 'warning' : 'info'));
    updateSelectionUi();
    if (imported.length > 0 && errors.length === 0) {
      setStatus(byId('materials-status'), demoSuffix('Added ' + pluralize(imported.length, 'filament') +
        ' to Fusion. Assign them to bodies with the Material Browser.'), 'info');
      showScreen('home');
    }
  }

  /**
   * Condense an array of `{id, message}` import errors into one line.
   * @param {object[]} errors
   * @returns {string}
   */
  function summarizeErrors(errors) {
    if (!Array.isArray(errors) || errors.length === 0) {
      return '';
    }
    var messages = [];
    for (var i = 0; i < errors.length && i < 2; i += 1) {
      var error = errors[i] || {};
      messages.push(asString(error.message) || asString(error.id) || 'Unknown error');
    }
    var summary = messages.join('; ');
    if (errors.length > 2) {
      summary += '; +' + (errors.length - 2) + ' more';
    }
    return summary;
  }

  /**
   * `materialsData`: refresh the read-only assembly materials table.
   * @param {object} payload
   */
  function handleMaterialsData(payload) {
    ingestLogSnapshot(payload.log);

    if (payload.library) {
      updateLibraryStatus(payload.library);
    }
    if (payload.favorites) {
      state.favorites = normalizeFavorites(payload.favorites) || state.favorites;
      renderFavoritesStatus(payload.favorites);
    }

    var status = byId('materials-status');
    if (payload.ok === false) {
      var failure = demoSuffix('Could not read assembly materials: ' + replyError(payload));
      setStatus(status, failure, 'error');
      appendLog(failure, 'error');
      return;
    }

    if (payload.design_name !== undefined) {
      state.ui.designName = asString(payload.design_name);
    }
    renderMaterials(Array.isArray(payload.materials) ? payload.materials : []);
    /* The slot mapping is built by Fusion from the design's materials; refresh
       it when the set of materials changed. */
    scheduleBambuddyRefresh();

    if (state.materials.length === 0) {
      setStatus(status, 'No filament is assigned in this design yet.', 'muted');
    } else {
      setStatus(status, '', 'muted');
    }
  }

  /**
   * `exportResult`: report the export outcome, paths and the filament slots
   * that were written.
   * @param {object} payload
   */
  function handleExportResult(payload) {
    ingestLogSnapshot(payload.log);
    appendLogTail(payload.log_tail);
    renderExportDetails(payload);

    var status = byId('export-status');
    if (payload.ok === true) {
      var outputPath = asString(payload.output_path);
      var success = demoSuffix('Export complete' + (outputPath ? ': ' + outputPath : '.'));
      setStatus(status, success, 'success');
      appendLog(success, 'info');
      state.ui.exported = true;
      renderReadiness();
    } else {
      var failure = demoSuffix('Export failed: ' + replyError(payload));
      setStatus(status, failure, 'error');
      appendLog(failure, 'error');
    }
  }

  /**
   * Render the export result details, including the list of filament slots
   * written to the 3MF (derived from `materials_used`).
   * @param {object|null} payload
   */
  function renderExportDetails(payload) {
    var container = byId('export-details');
    if (!container) {
      return;
    }
    container.textContent = '';
    var childCount = 0;

    function addLine(text) {
      var line = document.createElement('div');
      line.className = 'result-details__line';
      line.textContent = text;
      container.appendChild(line);
      childCount += 1;
    }

    if (payload && typeof payload === 'object') {
      var outputPath = asString(payload.output_path);
      var rawExportPath = asString(payload.raw_export_path);
      var errorText = asString(payload.error);

      if (payload.ok === false && errorText) {
        addLine('Error: ' + errorText);
      }
      if (outputPath) {
        addLine('Output: ' + outputPath);
      }
      if (rawExportPath && rawExportPath !== outputPath) {
        addLine('Raw Fusion export (metadata not applied): ' + rawExportPath);
      }
      if (payload.project_settings_written !== undefined) {
        addLine('project_settings.config written: ' + (payload.project_settings_written === true ? 'yes' : 'no'));
      }
      if (payload.orca_launched !== undefined) {
        addLine('Orca Slicer launched: ' + (payload.orca_launched === true ? 'yes' : 'no'));
      }
      if (payload.prepared === true) {
        var preparedPrinter = asString(payload.printer_name) || 'unknown printer';
        var preparedProcess = asString(payload.process_name);
        addLine('Prepared for printer: ' + preparedPrinter +
          (preparedProcess ? ' / process: ' + preparedProcess : ''));
      } else if (payload.prepared === false) {
        addLine('No printer profile applied \u2014 choose a target printer for a fully configured project.');
      }

      var used = Array.isArray(payload.materials_used) ? payload.materials_used.slice() : [];
      if (used.length > 0) {
        used.sort(function (a, b) {
          return slotSortValue(a) - slotSortValue(b);
        });
        var heading = document.createElement('div');
        heading.className = 'result-details__heading';
        heading.textContent = 'Filament slots written (' + used.length + ')';
        container.appendChild(heading);
        childCount += 1;
        for (var i = 0; i < used.length; i += 1) {
          container.appendChild(createExportMaterialRow(used[i]));
          childCount += 1;
        }
      } else if (payload.ok === true) {
        addLine('No filament slots were reported as used.');
      }
    }

    container.hidden = childCount === 0;
  }

  /**
   * Build one "Slot N - material ..." row for the export details area.
   * @param {object} material
   * @returns {HTMLElement}
   */
  function createExportMaterialRow(material) {
    var row = document.createElement('div');
    row.className = 'export-material';

    var label = asString(material.display_name) || asString(material.material_id) || 'Unnamed material';
    row.appendChild(createSwatch(material.color_hex, label, 'swatch--large export-material__swatch'));

    var text = document.createElement('span');
    text.className = 'export-material__text';
    var slot = Number(material.slot);
    var parts = [];
    parts.push('Slot ' + (isFinite(slot) && slot > 0 ? slot : '?') + ': ' + label);
    var details = [];
    if (asString(material.filament_type)) {
      details.push(asString(material.filament_type).toUpperCase());
    }
    if (material.density_g_cm3 !== undefined && material.density_g_cm3 !== null) {
      details.push(formatDensity(material.density_g_cm3));
    }
    var bodyCount = Number(material.body_count);
    if (isFinite(bodyCount) && bodyCount >= 0) {
      details.push(pluralize(bodyCount, 'body', 'bodies'));
    }
    if (details.length > 0) {
      parts.push(details.join(' \u00b7 '));
    }
    text.textContent = parts.join(' - ');
    if (asString(material.material_id)) {
      text.title = 'Catalog id: ' + asString(material.material_id);
    }
    row.appendChild(text);
    return row;
  }

  /* =========================================================================
   * Bambuddy / AMS sync
   * ======================================================================= */

  /**
   * Normalise the Bambuddy summary from initData.
   * @param {*} raw
   */
  function applyBambuddyState(raw) {
    var data = (raw && typeof raw === 'object') ? raw : {};
    var settings = state.settings || {};
    var slotMap = (data.slot_map && typeof data.slot_map === 'object')
      ? data.slot_map
      : (settings.bambuddy_slot_map || {});
    state.bambuddy.configured = data.configured === true ||
      (typeof data.url === 'string' && data.url.trim() !== '');
    state.bambuddy.keySet = data.key_set === true || settings.bambuddy_api_key_set === true;
    state.settings.bambuddy_api_key_set = state.bambuddy.keySet;
    state.bambuddy.url = asString(data.url) || asString(settings.bambuddy_url);
    state.bambuddy.selectedPrinterId = Number(data.printer_id) || Number(settings.bambuddy_printer_id) || 0;
    state.bambuddy.slotMap = slotMap;
    renderBambuddySettings();
    renderBambuddySlots();
    if (state.bambuddy.configured && (state.bambuddy.keySet || state.demoMode)) {
      window.setTimeout(function () {
        requestBambuddyContext({ silent: true });
      }, 80);
    }
  }

  /**
   * Fill the Bambuddy URL control, key placeholder, "Remove key" link and the
   * printer select from state. A key typed into the field is never cleared
   * here: only a successful Connect that sent it clears it.
   */
  function renderBambuddySettings() {
    var url = byId('bambuddy-url');
    if (url && !url.value) {
      url.value = state.bambuddy.url || state.settings.bambuddy_url || '';
    }
    var key = byId('bambuddy-api-key');
    if (key) {
      key.placeholder = state.bambuddy.keySet ? 'Saved. Leave empty to keep it.' : 'bb_...';
    }
    var removeKey = byId('bambuddy-remove-key-btn');
    if (removeKey) {
      removeKey.hidden = !state.settings.bambuddy_api_key_set;
    }
    var select = byId('bambuddy-printer');
    if (!select) {
      return;
    }
    select.textContent = '';
    var printers = state.bambuddy.printers;
    if (!printers.length) {
      var placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = state.bambuddy.configured ? 'Reading printers…' : 'Connect Bambuddy first';
      select.appendChild(placeholder);
      select.disabled = true;
      return;
    }
    for (var i = 0; i < printers.length; i += 1) {
      var printer = printers[i] || {};
      var option = document.createElement('option');
      option.value = String(printer.id || '');
      option.textContent = asString(printer.label) || asString(printer.name) || ('Printer ' + printer.id);
      if (Number(printer.id) === Number(state.bambuddy.selectedPrinterId)) {
        option.selected = true;
      }
      select.appendChild(option);
    }
    select.disabled = false;
  }

  /**
   * Loaded slots grouped by filament: the same filament in several slots is
   * offered once.
   * @returns {{key: string, name: string, color: string, slots: object[], inFusion: boolean}[]}
   */
  function loadedSlotGroups() {
    var favorites = (state.favorites && state.favorites.names) || [];
    var groups = [];
    var byName = {};
    var slots = (state.bambuddy.slots || []).filter(function (slot) {
      return Number(slot && slot.spool_id) > 0;
    });
    for (var i = 0; i < slots.length; i += 1) {
      var slot = slots[i];
      var name = asString(slot.display_name) || asString(slot.label);
      if (!byName[name]) {
        byName[name] = {
          key: asString(slot.key),
          name: name,
          color: asString(slot.color_hex),
          slots: [],
          inFusion: favorites.indexOf(name) !== -1
        };
        groups.push(byName[name]);
      }
      byName[name].slots.push(slot);
    }
    return groups;
  }

  /**
   * Second line of a loaded-filament row: slots and amount left.
   * @param {{slots: object[]}} group
   * @returns {string}
   */
  function slotGroupDetails(group) {
    var labels = group.slots.map(function (slot) { return shortSlotLabel(slot.label); });
    var unit = labels[0] ? labels[0].split(' · ')[0] : '';
    var sameUnit = labels.every(function (label) { return label.split(' · ')[0] === unit; });
    var where = sameUnit && labels.length > 1
      ? unit + ' · ' + labels.map(function (label) { return label.split(' · ')[1]; }).join(', ')
      : labels.join(', ');
    if (group.slots.length > 1) {
      return where + ' · ' + pluralize(group.slots.length, 'spool');
    }
    var remaining = Number(group.slots[0] && group.slots[0].remaining_percent);
    return isFinite(remaining) ? where + ' · ' + Math.round(remaining) + '% left' : where;
  }

  /** Update the "Add N to Fusion" button from the ticked filaments. */
  function updateBambuddyImportButton() {
    var button = byId('bambuddy-import-btn');
    if (!button) {
      return;
    }
    var count = loadedSlotGroups().filter(function (group) {
      return !group.inFusion && state.bambuddy.importKeys.has(group.key);
    }).length;
    button.disabled = count === 0;
    button.textContent = count > 0 ? 'Add ' + count + ' to Fusion' : 'Add to Fusion';
  }

  /**
   * Render the filaments loaded on the selected printer (Add filaments screen).
   */
  function renderBambuddySlots() {
    var list = byId('bambuddy-slots-list');
    var empty = byId('bambuddy-empty');
    var note = byId('bambuddy-slots-note');
    var groups = loadedSlotGroups();
    if (empty) {
      empty.hidden = state.bambuddy.configured;
    }
    if (note) {
      note.hidden = groups.length === 0;
    }
    if (list) {
      list.hidden = groups.length === 0;
      list.textContent = '';
      for (var i = 0; i < groups.length; i += 1) {
        var group = groups[i];
        var row = document.createElement('label');
        row.className = 'frow' + (group.inFusion ? ' frow--done' : '');
        row.setAttribute('role', 'listitem');
        var check = document.createElement('input');
        check.type = 'checkbox';
        check.value = group.key;
        check.disabled = group.inFusion;
        check.checked = !group.inFusion && state.bambuddy.importKeys.has(group.key);
        check.setAttribute('aria-label', 'Add ' + group.name);
        row.appendChild(check);
        row.appendChild(createSwatch(group.color, group.name, 'swatch--row'));
        var main = document.createElement('span');
        main.className = 'frow__main';
        var name = document.createElement('span');
        name.className = 'frow__name';
        name.style.display = 'block';
        name.textContent = splitDisplayName(group.name).name;
        name.title = group.name;
        main.appendChild(name);
        var sub = document.createElement('span');
        sub.className = 'frow__sub';
        sub.style.display = 'block';
        sub.textContent = slotGroupDetails(group);
        main.appendChild(sub);
        row.appendChild(main);
        if (group.inFusion) {
          var tag = document.createElement('span');
          tag.className = 'chip';
          tag.textContent = 'In Fusion';
          row.appendChild(tag);
        }
        list.appendChild(row);
      }
    }
    var status = byId('bambuddy-status');
    if (state.bambuddy.loaded && groups.length === 0) {
      setStatus(status, 'No spools are assigned to slots on this printer in Bambuddy.', 'warning');
    } else if (groups.length > 0 && groups.every(function (group) { return group.inFusion; })) {
      setStatus(status, 'Everything loaded on this printer is already in Fusion.', 'success');
    } else if (state.bambuddy.loaded && !state.bambuddy.error) {
      setStatus(status, '', 'muted');
    }
    updateBambuddyImportButton();
  }

  /**
   * Key for the set of design material ids (order-independent).
   * @returns {string}
   */
  function materialIdsKey() {
    return state.materials.map(function (material) {
      return asString(material && material.material_id);
    }).sort().join('\n');
  }

  /**
   * Refresh the Bambuddy slot mapping after the design changed, debounced.
   * Skipped in demo mode, before a first successful read, while Bambuddy is
   * failing (until the user presses Connect or refresh) and when the set of
   * design materials did not change.
   */
  function scheduleBambuddyRefresh() {
    if (bambuddyRefreshTimer !== null) {
      window.clearTimeout(bambuddyRefreshTimer);
    }
    bambuddyRefreshTimer = window.setTimeout(function () {
      bambuddyRefreshTimer = null;
      if (state.demoMode || !state.bambuddy.loaded || state.bambuddy.error ||
          isActionPending('bambuddy_context') ||
          materialIdsKey() === state.bambuddy.mappedMaterialsKey) {
        return;
      }
      requestBambuddyContext({ silent: true });
    }, BAMBUDDY_REFRESH_DEBOUNCE_MS);
  }

  /**
   * Read Bambuddy printers/slots/inventory and auto-match the detected materials.
   * Only an explicit Connect click sends the address, API key and TLS choice
   * typed in the form; every other read uses the saved connection.
   * @param {{silent?: boolean, explicit?: boolean, statusEl?: HTMLElement|null,
   *          button?: HTMLElement|null}} [options]
   */
  function requestBambuddyContext(options) {
    var opts = options || {};
    var statusEl = opts.statusEl || byId('bambuddy-status');
    var payload = {
      bambuddy_printer_id: state.bambuddy.selectedPrinterId || 0,
      bambuddy_slot_map: state.bambuddy.slotMap || {}
    };
    var context = { explicit: opts.explicit === true, url: state.bambuddy.url, key: '' };
    if (context.explicit) {
      var urlInput = byId('bambuddy-url');
      var keyInput = byId('bambuddy-api-key');
      var verifyInput = byId('bambuddy-verify-tls');
      var url = urlInput ? urlInput.value.trim() : '';
      if (!url) {
        setStatus(statusEl, 'Enter the Bambuddy address first.', 'warning');
        return;
      }
      if (!/^https?:\/\//i.test(url)) {
        url = 'http://' + url;
        if (urlInput) {
          urlInput.value = url;
        }
      }
      payload.bambuddy_url = url;
      payload.bambuddy_verify_tls = verifyInput ? verifyInput.checked : true;
      if (keyInput && keyInput.value.trim()) {
        context.key = keyInput.value.trim();
        payload.bambuddy_api_key = context.key;
      }
      context.url = url;
    } else if (!state.bambuddy.configured) {
      if (!opts.silent) {
        setStatus(statusEl, 'Connect Bambuddy in Settings first.', 'warning');
      }
      return;
    }
    state.bambuddy.mappedMaterialsKey = materialIdsKey();
    request('bambuddy_context', payload, {
      statusEl: opts.silent ? null : statusEl,
      pendingText: opts.silent ? '' : 'Reading the printer from Bambuddy...',
      button: opts.button || (context.explicit ? byId('bambuddy-connect-btn') : null),
      timeoutMs: BAMBUDDY_TIMEOUT_MS,
      context: context
    });
    renderHome();
  }

  /**
   * `bambuddyContextData`: apply printers/slots and the auto-match mapping.
   * The connection counts as configured only once Bambuddy answered.
   * @param {object} payload
   * @param {object|null} entry the matching pending request
   */
  function handleBambuddyContextData(payload, entry) {
    var context = (entry && entry.context) || {};
    var statusLines = [byId('bambuddy-status'), byId('setup-bambuddy-status'), byId('welcome-status')];
    if (payload.ok === false) {
      state.bambuddy.error = replyError(payload);
      for (var e = 0; e < statusLines.length; e += 1) {
        setStatus(statusLines[e], 'Bambuddy: ' + state.bambuddy.error, 'error');
      }
      appendLog('Bambuddy context failed: ' + state.bambuddy.error, 'error');
      renderHome();
      return;
    }
    var firstLoad = !state.bambuddy.loaded;
    if (context.url) {
      state.bambuddy.url = context.url;
    }
    if (context.key) {
      state.bambuddy.keySet = true;
      state.settings.bambuddy_api_key_set = true;
      var keyInput = byId('bambuddy-api-key');
      if (keyInput && keyInput.value.trim() === context.key) {
        keyInput.value = '';
      }
    }
    state.bambuddy.error = '';
    state.bambuddy.configured = true;
    state.bambuddy.loaded = true;
    state.bambuddy.printers = Array.isArray(payload.printers) ? payload.printers : [];
    state.bambuddy.slots = Array.isArray(payload.slots) ? payload.slots : [];
    state.bambuddy.mapping = Array.isArray(payload.mapping) ? payload.mapping : [];
    state.bambuddy.selectedPrinterId = Number(payload.selected_printer_id) || state.bambuddy.selectedPrinterId;
    state.settings.bambuddy_url = state.bambuddy.url;
    if (firstLoad || state.bambuddy.importKeys.size === 0) {
      state.bambuddy.importKeys = new Set(loadedSlotGroups().filter(function (group) {
        return !group.inFusion;
      }).map(function (group) { return group.key; }));
    }
    renderBambuddySettings();
    renderBambuddySlots();
    var connected = 'Connected · ' + pluralize(state.bambuddy.slots.length, 'loaded slot') + '.';
    setStatus(byId('setup-bambuddy-status'), connected, 'success');
    setStatus(byId('welcome-status'), connected, 'success');
    appendLog('Loaded Bambuddy context: ' + state.bambuddy.printers.length + ' printer(s), ' +
      state.bambuddy.slots.length + ' slot(s).', 'info');
    if (state.ui.screen === 'welcome') {
      dismissWelcome();
      showScreen('home');
    }
    renderHome();
  }

  /**
   * Import the ticked loaded filaments into Favorites and the design.
   */
  function onBambuddyImportClick() {
    if (isActionPending('bambuddy_import')) {
      return;
    }
    var groups = loadedSlotGroups().filter(function (group) {
      return !group.inFusion && state.bambuddy.importKeys.has(group.key);
    });
    if (!groups.length) {
      setStatus(byId('bambuddy-import-status'), 'Tick at least one filament.', 'warning');
      return;
    }
    var slotKeys = groups.map(function (group) { return group.key; });
    request('bambuddy_import', {
      slot_keys: slotKeys,
      bambuddy_printer_id: state.bambuddy.selectedPrinterId || 0
    }, {
      statusEl: byId('bambuddy-import-status'),
      pendingText: 'Adding ' + pluralize(slotKeys.length, 'filament') + ' to Fusion...',
      button: byId('bambuddy-import-btn'),
      timeoutMs: BAMBUDDY_TIMEOUT_MS
    });
  }

  /**
   * `bambuddyImportResult`: report the added filaments, refresh the catalog
   * and library status, and go back Home.
   * @param {object} payload
   */
  function handleBambuddyImportResult(payload) {
    var status = byId('bambuddy-import-status');
    if (payload.ok === false) {
      setStatus(status, 'Could not add the filaments: ' + replyError(payload), 'error');
      appendLog('Bambuddy import failed: ' + replyError(payload), 'error');
      return;
    }
    var imported = Array.isArray(payload.imported) ? payload.imported : [];
    appendLog('Bambuddy import: ' + imported.length + ' spool(s) added to Favorites.', 'info');
    state.bambuddy.importKeys = new Set();
    applyLibraryRefresh(payload);
    renderBambuddySlots();
    ingestLogSnapshot(payload.log);
    if (imported.length > 0) {
      setStatus(status, '', 'muted');
      setStatus(byId('materials-status'), demoSuffix('Added ' + pluralize(imported.length, 'filament') +
        ' to Fusion. Assign them to bodies with the Material Browser.'), 'info');
      showScreen('home');
    } else {
      setStatus(status, 'Nothing was added.', 'warning');
    }
  }

  /**
   * `diagnosticsResult`: display the diagnostics text returned by the add-in.
   * @param {object} payload
   */
  function handleDiagnosticsResult(payload) {
    var output = byId('diagnostics-output');
    if (output) {
      var text = asString(payload.text);
      if (!text && payload.ok === false) {
        text = 'Diagnostics failed: ' + replyError(payload);
      }
      output.textContent = text || '(no output returned)';
      output.hidden = false;
      output.scrollTop = 0;
    }

    var status = byId('diagnostics-status');
    if (payload.ok === true) {
      setStatus(status, demoSuffix('Diagnostics passed.'), 'success');
      appendLog(demoSuffix('Diagnostics passed.'), 'info');
    } else {
      setStatus(status, demoSuffix('Diagnostics found problems - see the output below.'), 'error');
      appendLog(demoSuffix('Diagnostics failed: ' + replyError(payload)), 'error');
    }
  }

  /**
   * "Remove key" click handler: forget the saved Bambuddy API key.
   */
  function onRemoveBambuddyKeyClick() {
    request('clear_bambuddy_key', {}, {
      statusEl: byId('setup-bambuddy-status'),
      pendingText: 'Removing the saved API key...',
      button: byId('bambuddy-remove-key-btn')
    });
  }

  /**
   * `settingsSaved`: confirm (or report) the debounced settings persistence.
   * @param {object} payload
   */
  function handleSettingsSaved(payload, entry) {
    ingestLogSnapshot(payload.log);
    var clearingKey = !!(entry && entry.requestAction === 'clear_bambuddy_key');
    var status = clearingKey ? byId('setup-bambuddy-status') : byId('settings-status');
    if (payload.ok === false) {
      var failure = clearingKey
        ? 'Could not remove the API key: ' + replyError(payload)
        : 'Fusion could not save the settings: ' + replyError(payload);
      setStatus(status, demoSuffix(failure), 'error');
      appendLog(failure, 'error');
      return;
    }
    if (payload.settings && typeof payload.settings === 'object') {
      state.settings = normalizeSettings(payload.settings);
      state.bambuddy.keySet = state.settings.bambuddy_api_key_set;
      renderBambuddySettings();
      renderBambuddySummary();
    }
    if (clearingKey) {
      setStatus(status, demoSuffix('API key removed.'), 'success');
      appendLog('Removed the saved Bambuddy API key.', 'info');
      return;
    }
    setStatus(status, demoSuffix('Settings saved.'), 'success');
  }

  /**
   * `logData`: replace the log panel with the authoritative Fusion log.
   * @param {object} payload
   */
  function handleLogData(payload) {
    if (payload.ok === false) {
      appendLog('Could not refresh the log: ' + replyError(payload), 'error');
      return;
    }
    if (!Array.isArray(payload.log)) {
      appendLog('Fusion returned a log reply without a "log" array.', 'warning');
      return;
    }
    ingestLogSnapshot(payload.log, { replace: true });
  }

  /**
   * `exportPathData`: native save-dialog result for the 3MF output path.
   * Sets the input on success, reports cancellation informationally, and
   * surfaces real errors. The pending/busy state is released by dispatch.
   * Status text goes to the line of the screen that asked (Home or Settings).
   * @param {object} payload
   * @param {object|null} entry the matching pending request
   */
  function handleExportPathData(payload, entry) {
    var status = (entry && entry.statusEl) || byId('export-status');
    var input = byId('output-path');

    if (payload && payload.ok === true) {
      var path = asString(payload.path).trim();
      if (!path) {
        setStatus(status, 'Browse did not return a file path.', 'warning');
        return;
      }
      if (input) {
        input.value = path;
      }
      setStatus(status, '', 'muted');
      appendLog('Export path selected via Browse: ' + path, 'info');
      renderSaveLine();
      scheduleSettingsSave();
      return;
    }

    if (payload && payload.cancelled === true) {
      setStatus(status, 'Browse cancelled.', 'muted');
      return;
    }

    var failure = 'Browse failed: ' + replyError(payload);
    setStatus(status, failure, 'error');
    appendLog(failure, 'error');
  }

  /**
   * `slicerPathData`: native open-dialog result for the slicer executable.
   * @param {object} payload
   */
  function handleSlicerPathData(payload) {
    var status = byId('slicer-status');
    var input = byId('orca-path');

    if (payload && payload.ok === true) {
      var path = asString(payload.path).trim();
      if (!path) {
        setStatus(status, 'Browse did not return a file path.', 'warning');
        return;
      }
      if (input) {
        input.value = path;
      }
      setStatus(status, demoSuffix('Slicer executable selected: ' + path), 'success');
      appendLog('Slicer executable selected via Browse: ' + path, 'info');
      renderSlicerSummary();
      scheduleSettingsSave();
      /* The chosen install supplies the printer/process profiles. */
      if (!isActionPending('get_profiles')) {
        requestProfiles({
          vendor: selectValue('target-vendor'),
          printer: selectValue('target-printer')
        });
      }
      return;
    }

    if (payload && payload.cancelled === true) {
      setStatus(status, 'Browse cancelled.', 'muted');
      return;
    }

    var failure = 'Browse failed: ' + replyError(payload);
    setStatus(status, failure, 'error');
    appendLog(failure, 'error');
  }

  /* =========================================================================
   * Catalog rendering (grouped by category)
   * ======================================================================= */

  /**
   * Build the catalog's category index: category order plus an id -> category
   * map. Categories come from `initData.categories` first; catalog entries that
   * are not listed are grouped by their own `category` field. `counts` holds
   * the number of catalog entries per category (for the filter chips).
   * @returns {{categoryNames: string[], categoryByFilamentId: Map<string,string>,
   *            counts: Object<string, number>}}
   */
  function buildCategoryIndex() {
    var categoryByFilamentId = new Map();
    var categoryNames = [];
    var seenNames = new Set();

    function ensureName(name) {
      var clean = asString(name).trim() || UNCATEGORIZED;
      if (!seenNames.has(clean)) {
        seenNames.add(clean);
        categoryNames.push(clean);
      }
      return clean;
    }

    for (var i = 0; i < state.categories.length; i += 1) {
      var category = state.categories[i];
      var name = ensureName(category.name);
      var ids = category.filament_ids || [];
      for (var j = 0; j < ids.length; j += 1) {
        var id = asString(ids[j]);
        if (!categoryByFilamentId.has(id)) {
          categoryByFilamentId.set(id, name);
        }
      }
    }

    for (var k = 0; k < state.catalog.length; k += 1) {
      var entry = state.catalog[k];
      var entryId = asString(entry.id);
      var mapped = categoryByFilamentId.get(entryId);
      if (!mapped) {
        mapped = ensureName(entry.category);
        categoryByFilamentId.set(entryId, mapped);
      } else {
        ensureName(mapped);
      }
    }

    var counts = {};
    categoryByFilamentId.forEach(function (name) {
      counts[name] = (counts[name] || 0) + 1;
    });
    return { categoryNames: categoryNames, categoryByFilamentId: categoryByFilamentId, counts: counts };
  }

  /**
   * Cached category index; rebuilt only after the catalog or categories
   * changed (set `catalogIndexCache = null` whenever they are replaced).
   * @returns {{categoryNames: string[], categoryByFilamentId: Map<string,string>,
   *            counts: Object<string, number>}}
   */
  function getCategoryIndex() {
    if (catalogIndexCache === null) {
      catalogIndexCache = buildCategoryIndex();
    }
    return catalogIndexCache;
  }

  /**
   * @param {object} entry
   * @returns {string} category name for a catalog entry.
   */
  function categoryNameForEntry(entry) {
    return asString(entry && entry.category).trim() || UNCATEGORIZED;
  }

  /**
   * Does a catalog entry match the (already lower-cased) search query?
   * Every whitespace-separated token must appear somewhere in the entry.
   * @param {object} entry
   * @param {string} query
   * @returns {boolean}
   */
  function matchesCatalogSearch(entry, query) {
    if (!query) {
      return true;
    }
    var haystack = [
      entry.display_name,
      entry.manufacturer,
      entry.product_line,
      entry.color_name,
      entry.slicer_filament_type,
      entry.category,
      entry.id
    ].map(asString).join(' ').toLowerCase();

    var tokens = query.split(/\s+/).filter(function (token) { return token.length > 0; });
    for (var i = 0; i < tokens.length; i += 1) {
      if (haystack.indexOf(tokens[i]) === -1) {
        return false;
      }
    }
    return true;
  }

  /**
   * Filter the catalog for the current search text and category chip.
   * @param {{categoryByFilamentId: Map<string,string>}} categoryIndex
   * @returns {object[]}
   */
  function getVisibleCatalogEntries(categoryIndex) {
    var search = byId('catalog-search');
    var query = search ? search.value.trim().toLowerCase() : '';
    var active = state.activeCategory;
    return state.catalog.filter(function (entry) {
      if (!matchesCatalogSearch(entry, query)) {
        return false;
      }
      if (active === null) {
        return true;
      }
      return categoryIndex.categoryByFilamentId.get(asString(entry.id)) === active;
    });
  }

  /**
   * Render the category filter chips ("All" + one chip per category).
   * @param {{categoryNames: string[], categoryByFilamentId: Map<string,string>}} categoryIndex
   */
  function renderCategoryFilters(categoryIndex) {
    var container = byId('category-filters');
    if (!container) {
      return;
    }
    container.textContent = '';

    if (state.activeCategory !== null && categoryIndex.categoryNames.indexOf(state.activeCategory) === -1) {
      state.activeCategory = null;
    }

    function addChip(label, categoryValue, count) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'category-chip';
      var isActive = categoryValue === null ? state.activeCategory === null : state.activeCategory === categoryValue;
      if (isActive) {
        button.classList.add('is-active');
      }
      button.setAttribute('data-category', categoryValue === null ? '' : categoryValue);
      button.setAttribute('data-focus-key', 'chip:' + (categoryValue === null ? '' : categoryValue));
      button.setAttribute('aria-pressed', isActive ? 'true' : 'false');
      if (categoryValue !== null) {
        button.title = 'Show only ' + categoryValue + ' filaments';
      } else {
        button.title = 'Show filaments from every category';
      }
      var labelNode = document.createElement('span');
      labelNode.textContent = label;
      button.appendChild(labelNode);
      var countNode = document.createElement('span');
      countNode.className = 'category-chip__count';
      countNode.textContent = String(count);
      button.appendChild(countNode);
      container.appendChild(button);
    }

    addChip('All', null, state.catalog.length);
    for (var i = 0; i < categoryIndex.categoryNames.length; i += 1) {
      var name = categoryIndex.categoryNames[i];
      addChip(name, name, categoryIndex.counts[name] || 0);
    }
  }

  /**
   * Update the catalog count label ("5 filaments" / "2 of 5 filaments").
   * @param {number} visible
   * @param {number} total
   */
  function setCatalogCount(visible, total) {
    var count = byId('catalog-count');
    if (!count) {
      return;
    }
    if (total === 0) {
      count.textContent = '0 filaments';
    } else if (visible === total) {
      count.textContent = pluralize(total, 'filament');
    } else {
      count.textContent = visible + ' of ' + pluralize(total, 'filament');
    }
  }

  /**
   * Build one catalog row: checkbox, swatch, name, manufacturer/product, type
   * chip and density. Uses createElement/textContent only.
   * @param {object} entry
   * @returns {HTMLElement}
   */
  function createCatalogRow(entry) {
    var row = document.createElement('div');
    row.className = 'catalog-row';

    var label = document.createElement('label');
    label.className = 'catalog-row__label';

    var checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'catalog-row__checkbox';
    checkbox.value = asString(entry.id);
    checkbox.setAttribute('data-focus-key', 'id:' + checkbox.value);
    checkbox.checked = state.selectedIds.has(checkbox.value);
    checkbox.setAttribute('aria-label', 'Select ' + displayName(entry));
    label.appendChild(checkbox);

    label.appendChild(createSwatch(
      entry.color_hex,
      asString(entry.color_name) || displayName(entry)
    ));

    var main = document.createElement('span');
    main.className = 'catalog-row__main';

    var name = document.createElement('span');
    name.className = 'catalog-row__name';
    name.textContent = displayName(entry);
    main.appendChild(name);

    var metaParts = [];
    if (asString(entry.manufacturer)) {
      metaParts.push(asString(entry.manufacturer));
    }
    if (asString(entry.product_line)) {
      metaParts.push(asString(entry.product_line));
    }
    if (metaParts.length > 0) {
      var meta = document.createElement('span');
      meta.className = 'catalog-row__meta';
      meta.textContent = metaParts.join(' \u00b7 ');
      main.appendChild(meta);
    }
    label.appendChild(main);

    var filamentType = asString(entry.slicer_filament_type).toUpperCase();
    label.appendChild(createChip(
      filamentType || '\u2014',
      filamentType ? 'Slicer filament type: ' + filamentType : 'No slicer filament type'
    ));

    var density = document.createElement('span');
    density.className = 'catalog-row__density';
    density.textContent = formatDensity(entry.density_g_cm3);
    density.title = 'Density';
    label.appendChild(density);

    row.appendChild(label);
    return row;
  }

  /**
   * Render the catalog list grouped by category, applying the search filter
   * and the active category chip. Uses only createElement/textContent for
   * untrusted catalog data.
   */
  function renderCatalog() {
    var list = byId('catalog-list');
    if (!list) {
      return;
    }
    if (catalogSearchTimer !== null) {
      window.clearTimeout(catalogSearchTimer);
      catalogSearchTimer = null;
    }
    var categoryIndex = getCategoryIndex();
    var restoreChipFocus = keepFocus(byId('category-filters'));
    var restoreListFocus = keepFocus(list);
    renderCategoryFilters(categoryIndex);
    restoreChipFocus();
    list.textContent = '';

    var visible = getVisibleCatalogEntries(categoryIndex);
    if (visible.length === 0) {
      var empty = document.createElement('p');
      empty.className = 'empty-state';
      empty.textContent = state.catalog.length === 0
        ? 'No filament catalog loaded yet.'
        : 'No filaments match the current search and category filter.';
      list.appendChild(empty);
      setCatalogCount(0, state.catalog.length);
      updateSelectionUi();
      return;
    }

    var byCategory = {};
    for (var j = 0; j < visible.length; j += 1) {
      var entryCategory = categoryIndex.categoryByFilamentId.get(asString(visible[j].id));
      (byCategory[entryCategory] = byCategory[entryCategory] || []).push(visible[j]);
    }
    for (var i = 0; i < categoryIndex.categoryNames.length; i += 1) {
      var categoryName = categoryIndex.categoryNames[i];
      if (Object.prototype.hasOwnProperty.call(byCategory, categoryName)) {
        list.appendChild(createCatalogCategory(categoryName, byCategory[categoryName]));
      }
    }
    restoreListFocus();

    setCatalogCount(visible.length, state.catalog.length);
    updateSelectionUi();
  }

  /**
   * Re-render the catalog shortly after the last keystroke in the search box.
   */
  function scheduleCatalogRender() {
    if (catalogSearchTimer !== null) {
      window.clearTimeout(catalogSearchTimer);
    }
    catalogSearchTimer = window.setTimeout(renderCatalog, CATALOG_SEARCH_DEBOUNCE_MS);
  }

  /**
   * Build one category group (sticky heading + per-category select-all +
   * rows).
   * @param {string} categoryName
   * @param {object[]} entries
   * @returns {HTMLElement}
   */
  function createCatalogCategory(categoryName, entries) {
    var group = document.createElement('div');
    group.className = 'catalog-category';

    var title = document.createElement('h3');
    title.className = 'catalog-category__title';

    var heading = document.createElement('span');
    heading.className = 'catalog-category__heading';
    var headingName = document.createElement('span');
    headingName.textContent = categoryName;
    var headingCount = document.createElement('span');
    headingCount.className = 'catalog-category__count';
    headingCount.textContent = pluralize(entries.length, 'filament');
    heading.appendChild(headingName);
    heading.appendChild(headingCount);

    var selectAll = document.createElement('button');
    selectAll.type = 'button';
    selectAll.className = 'category-select-all';
    selectAll.setAttribute('data-category', categoryName);
    selectAll.setAttribute('data-focus-key', 'all:' + categoryName);
    selectAll.setAttribute('aria-label', 'Select all ' + categoryName + ' filaments');
    selectAll.textContent = 'Select all';
    selectAll.title = 'Select every visible ' + categoryName + ' filament';

    title.appendChild(heading);
    title.appendChild(selectAll);
    group.appendChild(title);

    for (var i = 0; i < entries.length; i += 1) {
      group.appendChild(createCatalogRow(entries[i]));
    }
    return group;
  }

  /**
   * Drop selected ids that are no longer in the catalog.
   */
  function pruneSelection() {
    var valid = new Set();
    for (var i = 0; i < state.catalog.length; i += 1) {
      valid.add(asString(state.catalog[i].id));
    }
    state.selectedIds.forEach(function (id) {
      if (!valid.has(id)) {
        state.selectedIds.delete(id);
      }
    });
  }

  /**
   * Refresh the selected-count label and the import button's availability.
   */
  function updateSelectionUi() {
    var selection = byId('catalog-selection');
    if (selection) {
      selection.textContent = state.selectedIds.size + ' selected';
    }
    var importButton = byId('import-btn');
    if (importButton) {
      importButton.disabled = state.catalog.length === 0 || isActionPending('import_selected');
    }
  }

  /**
   * Select every catalog entry currently visible through the search box and
   * category chip.
   */
  function selectAllVisible() {
    var categoryIndex = getCategoryIndex();
    var visible = getVisibleCatalogEntries(categoryIndex);
    for (var i = 0; i < visible.length; i += 1) {
      state.selectedIds.add(asString(visible[i].id));
    }
    renderCatalog();
  }

  /**
   * Select every currently visible entry of one category (used by the
   * per-category "Select all" buttons).
   * @param {string} categoryName
   */
  function selectAllInCategory(categoryName) {
    var categoryIndex = getCategoryIndex();
    var visible = getVisibleCatalogEntries(categoryIndex);
    for (var i = 0; i < visible.length; i += 1) {
      if (categoryIndex.categoryByFilamentId.get(asString(visible[i].id)) === categoryName) {
        state.selectedIds.add(asString(visible[i].id));
      }
    }
    renderCatalog();
  }

  /**
   * Clear the entire selection.
   */
  function selectNone() {
    state.selectedIds.clear();
    renderCatalog();
  }

  /**
   * Category chip click handler (delegated from #category-filters).
   * @param {Event} event
   */
  function onCategoryFilterClick(event) {
    var target = event.target;
    if (!target || typeof target.closest !== 'function') {
      return;
    }
    var button = target.closest('button.category-chip');
    if (!button) {
      return;
    }
    var raw = button.getAttribute('data-category');
    var categoryName = raw ? raw : null;
    if (categoryName !== null && state.activeCategory === categoryName) {
      state.activeCategory = null;
    } else {
      state.activeCategory = categoryName;
    }
    renderCatalog();
  }

  /**
   * Catalog list click handler for the per-category "Select all" buttons.
   * @param {Event} event
   */
  function onCatalogListClick(event) {
    var target = event.target;
    if (!target || typeof target.closest !== 'function') {
      return;
    }
    var button = target.closest('button.category-select-all');
    if (!button) {
      return;
    }
    selectAllInCategory(button.getAttribute('data-category') || UNCATEGORIZED);
  }

  /* =========================================================================
   * Assembly materials (read-only table)
   * ======================================================================= */

  /**
   * Numeric sort key for a material's automatic slot.
   * @param {object} material
   * @returns {number}
   */
  function slotSortValue(material) {
    var slot = Number(material && material.slot);
    if (isFinite(slot) && slot > 0) {
      return slot;
    }
    return 9999;
  }

  /**
   * Total body count across the assembly materials.
   * @param {object[]} materials
   * @returns {number}
   */
  function countBodies(materials) {
    var total = 0;
    for (var i = 0; i < materials.length; i += 1) {
      var material = materials[i];
      var count = Number(material && material.body_count);
      if (!isFinite(count) || count < 0) {
        count = Array.isArray(material && material.bodies) ? material.bodies.length : 0;
      }
      total += count;
    }
    return total;
  }

  /**
   * Store the design materials and redraw the Home list.
   * @param {object[]} materials
   */
  function renderMaterials(materials) {
    state.materials = Array.isArray(materials) ? materials.slice() : [];
    renderHome();
  }

  /**
   * Show the Fusion material-library status as an informational line.
   * @param {object|null|undefined} library
   */
  function updateLibraryStatus(library) {
    var element = byId('library-status');
    if (!element) {
      return;
    }
    if (!library || typeof library !== 'object') {
      setStatus(element, 'Library: status not reported by Fusion.', 'muted');
      element.removeAttribute('title');
      return;
    }

    var path = asString(library.path).trim();
    var loaded = library.loaded === true;
    var writable = library.writable === true;
    var message = asString(library.message).trim();

    var statusText = 'Library: ' + (path || '(no library path reported)');
    if (loaded) {
      statusText += ' - loaded' + (writable ? ', writable' : ', read-only');
    } else {
      statusText += ' - not loaded';
    }
    if (message) {
      statusText += '. ' + message;
    }
    setStatus(element, statusText, loaded ? 'muted' : 'warning');
    if (message || path) {
      element.title = message || path;
    } else {
      element.removeAttribute('title');
    }
  }

  /**
   * Show the app-level Favorites summary as an informational status line.
   * @param {{count: number, names: string[]}|null|undefined} favorites
   */
  function renderFavoritesStatus(favorites) {
    var element = byId('favorites-status');
    if (!element) {
      return;
    }
    var summary = normalizeFavorites(favorites);
    if (!summary) {
      setStatus(element, 'Favorites: status not reported by Fusion.', 'muted');
      element.removeAttribute('title');
      return;
    }
    setStatus(
      element,
      'Favorites: ' + pluralize(summary.count, 'material') + ' (app-level)',
      'muted'
    );
    if (summary.names.length > 0) {
      element.title = 'App-level Favorites:\n' + summary.names.join('\n');
    } else {
      element.removeAttribute('title');
    }
  }

  /* =========================================================================
   * Target printer profiles
   * ======================================================================= */

  /**
   * Label of the currently selected option, or ''.
   * @param {HTMLSelectElement|null} select
   * @returns {string}
   */
  function selectedOptionLabel(select) {
    if (!select || select.selectedIndex < 0 || !select.options[select.selectedIndex]) {
      return '';
    }
    return asString(select.options[select.selectedIndex].textContent).trim();
  }

  /**
   * Select the first non-placeholder option when nothing is selected.
   * @param {HTMLSelectElement|null} select
   * @returns {string}
   */
  function ensureSelectHasValue(select) {
    if (!select) {
      return '';
    }
    if (select.value) {
      return select.value;
    }
    for (var i = 0; i < select.options.length; i += 1) {
      if (select.options[i].value && !select.options[i].disabled) {
        select.value = select.options[i].value;
        return select.value;
      }
    }
    return '';
  }

  /**
   * Adopt a slicer install path when the user has not configured one yet.
   * Prefers an OrcaSlicer install, then the first reported slicer.
   * @param {object[]} slicers
   */
  function maybeAdoptSlicerPath(slicers) {
    if (!Array.isArray(slicers) || slicers.length === 0) {
      return;
    }
    var input = byId('orca-path');
    if (!input || asString(input.value).trim()) {
      return;
    }
    var chosen = null;
    for (var i = 0; i < slicers.length; i += 1) {
      var candidate = slicers[i];
      var identity = (asString(candidate.kind) + ' ' + asString(candidate.name)).toLowerCase();
      if (identity.indexOf('orca') !== -1) {
        chosen = candidate;
        break;
      }
    }
    if (!chosen) {
      chosen = slicers[0];
    }
    var path = asString(chosen && chosen.path).trim();
    if (path) {
      input.value = path;
      state.settings.slicer_path = path;
      appendLog('Using slicer for profile preparation: ' + path, 'info');
    }
  }

  /**
   * Fill the vendor/printer/process selects from a profilesData/profiles payload.
   * @param {object} payload
   * @param {{vendor?: string, printer?: string}} [context]
   * @returns {{vendor: string, printer: string, process: string}}
   */
  function renderProfileSelects(payload, context) {
    var data = (payload && typeof payload === 'object') ? payload : {};
    var selected = (data.selected && typeof data.selected === 'object') ? data.selected : {};
    var ctx = context || {};

    maybeAdoptSlicerPath(data.slicers);

    var vendors = Array.isArray(data.vendors) ? data.vendors : [];
    var printers = Array.isArray(data.printers) ? data.printers : [];
    var processes = Array.isArray(data.processes) ? data.processes : [];

    var vendorRows = [];
    var i;
    for (i = 0; i < vendors.length; i += 1) {
      vendorRows.push({
        value: asString(vendors[i].id),
        label: asString(vendors[i].name) || asString(vendors[i].id),
        title: asString(vendors[i].source)
      });
    }
    var desiredVendor = asString(selected.vendor) || asString(ctx.vendor) ||
      asString(state.settings.target_vendor);
    if (!desiredVendor && vendorRows.length > 0) {
      desiredVendor = vendorRows[0].value;
    }
    var vendorValue = setSelectOptions(byId('target-vendor'), vendorRows, {
      placeholder: vendorRows.length > 0 ? 'Select a vendor...' : 'No vendors found',
      preferred: desiredVendor
    });
    if (!vendorValue) {
      vendorValue = ensureSelectHasValue(byId('target-vendor'));
    }

    var printerRows = [];
    for (i = 0; i < printers.length; i += 1) {
      var titles = [];
      if (asString(printers[i].machine_path)) {
        titles.push(asString(printers[i].machine_path));
      }
      if (asString(printers[i].source)) {
        titles.push('Source: ' + asString(printers[i].source));
      }
      printerRows.push({
        value: asString(printers[i].name),
        label: asString(printers[i].name),
        title: titles.join(' - ')
      });
    }
    var desiredPrinter = asString(selected.printer) || asString(ctx.printer) ||
      asString(state.settings.target_printer);
    if (!desiredPrinter && printerRows.length > 0) {
      desiredPrinter = printerRows[0].value;
    }
    var printerValue = setSelectOptions(byId('target-printer'), printerRows, {
      placeholder: printerRows.length > 0 ? 'Select a printer...' : 'No printers found',
      preferred: desiredPrinter
    });
    if (!printerValue) {
      printerValue = ensureSelectHasValue(byId('target-printer'));
    }

    var processRows = [];
    var defaultProcessName = '';
    for (i = 0; i < processes.length; i += 1) {
      var processName = asString(processes[i].name);
      if (processes[i].default === true && !defaultProcessName) {
        defaultProcessName = processName;
      }
      processRows.push({
        value: processName,
        label: processName + (processes[i].default === true ? ' (default)' : ''),
        title: asString(processes[i].process_path)
      });
    }
    var desiredProcess = asString(selected.process) || asString(state.settings.target_process) ||
      defaultProcessName;
    if (!desiredProcess && processRows.length > 0) {
      desiredProcess = processRows[0].value;
    }
    var processValue = setSelectOptions(byId('target-process'), processRows, {
      placeholder: processRows.length > 0 ? 'Select a process...' : 'No processes found',
      preferred: desiredProcess
    });
    if (!processValue) {
      processValue = ensureSelectHasValue(byId('target-process'));
    }

    var status = byId('profiles-status');
    if (vendorRows.length === 0) {
      setStatus(status, demoSuffix('No slicer profiles found. Install Bambu Studio or OrcaSlicer, or use Import profiles.'), 'warning');
    } else if (printerRows.length === 0) {
      setStatus(status, demoSuffix('No printers found for ' + (selectedOptionLabel(byId('target-vendor')) || 'this vendor') +
        '. Choose another vendor or import profiles.'), 'warning');
    } else {
      setStatus(status, demoSuffix('Profiles: ' + selectedOptionLabel(byId('target-vendor')) + ' / ' +
        selectedOptionLabel(byId('target-printer')) + ' / ' + selectedOptionLabel(byId('target-process')) + '.'), 'success');
    }

    if (vendorValue && printerValue) {
      scheduleSettingsSave();
    }
    renderPrintSummary();
    renderReadiness();
    return { vendor: vendorValue, printer: printerValue, process: processValue };
  }

  /**
   * Send `get_profiles` for the current (or supplied) selection context.
   * @param {{vendor?: string, printer?: string}} [overrides]
   */
  function requestProfiles(overrides) {
    var ctx = overrides || {};
    var slicerInput = byId('orca-path');
    var slicerPath = asString(slicerInput && slicerInput.value).trim();
    var vendor = ctx.vendor !== undefined ? asString(ctx.vendor) : selectValue('target-vendor');
    var printer = ctx.printer !== undefined ? asString(ctx.printer) : selectValue('target-printer');
    request('get_profiles', {
      slicer_path: slicerPath,
      vendor: vendor,
      printer: printer
    }, {
      statusEl: byId('profiles-status'),
      pendingText: 'Loading printer profiles...',
      button: byId('refresh-profiles-btn'),
      timeoutMs: PROFILES_TIMEOUT_MS,
      context: { vendor: vendor, printer: printer },
      onFail: renderPrintSummary
    });
  }

  /**
   * `profilesData`: fill vendor/printer/process selects and remember what the
   * user requested, so a refresh does not lose their selection.
   * @param {object} payload
   * @param {object|null} entry the matching pending request (its context)
   */
  function handleProfilesData(payload, entry) {
    var context = (entry && entry.context) || { vendor: '', printer: '' };

    if (payload.ok === false) {
      var failure = demoSuffix('Could not load printer profiles: ' + replyError(payload));
      setStatus(byId('profiles-status'), failure, 'error');
      appendLog(failure, 'error');
      renderPrintSummary();
      return;
    }

    renderProfileSelects(payload, context);
    appendLog(demoSuffix('Loaded printer profiles from ' + asString((payload.selected || {}).slicer_path || 'the configured slicer') + '.'), 'info');
  }

  /**
   * `profilesImported`: native profile JSON import result.
   * @param {object} payload
   */
  function handleProfilesImported(payload) {
    var status = byId('profiles-status');
    if (payload.ok === false) {
      if (payload.cancelled === true) {
        setStatus(status, 'Profile import cancelled.', 'muted');
        return;
      }
      var failure = demoSuffix('Profile import failed: ' + replyError(payload));
      setStatus(status, failure, 'error');
      appendLog(failure, 'error');
      return;
    }

    var imported = (payload.imported && typeof payload.imported === 'object') ? payload.imported : {};
    var machineCount = Number(imported.machines) || 0;
    var processCount = Number(imported.processes) || 0;
    var filamentCount = Number(imported.filaments) || 0;
    var root = asString(imported.root);

    if (payload.profiles && typeof payload.profiles === 'object') {
      renderProfileSelects(payload.profiles, {
        vendor: selectValue('target-vendor'),
        printer: selectValue('target-printer')
      });
    } else {
      requestProfiles();
    }

    var summary = 'Imported ' + pluralize(machineCount, 'machine') + ', ' +
      pluralize(processCount, 'process', 'processes') + ' and ' +
      pluralize(filamentCount, 'filament') + (root ? ' from ' + root : '') + '.';
    setStatus(status, demoSuffix(summary), 'success');
    appendLog(demoSuffix(summary), 'info');
    scheduleSettingsSave();
  }

  /**
   * "Refresh profiles" click handler.
   */
  function onRefreshProfilesClick() {
    requestProfiles({
      vendor: selectValue('target-vendor'),
      printer: selectValue('target-printer')
    });
  }

  /**
   * "Import profiles..." click handler (native multi-select dialog in Fusion).
   */
  function onImportProfilesClick() {
    request('import_profiles', {}, {
      statusEl: byId('profiles-status'),
      pendingText: 'Waiting for the profile import dialog...',
      button: byId('import-profiles-btn'),
      timeoutMs: BROWSE_TIMEOUT_MS
    });
  }

  /**
   * Vendor change: reload the printers/processes for that vendor.
   */
  function onTargetVendorChange() {
    requestProfiles({
      vendor: selectValue('target-vendor'),
      printer: ''
    });
  }

  /**
   * Printer change: reload the processes for that printer.
   */
  function onTargetPrinterChange() {
    requestProfiles({
      vendor: selectValue('target-vendor'),
      printer: selectValue('target-printer')
    });
  }

  /**
   * Reflect the prepare toggle in the rest of the export form: while preparing,
   * project_settings.config is written by the preparation, so hide/disable the
   * manual toggle.
   */
  function syncPrepareUi() {
    var prepare = byId('prepare-enabled');
    var writeProject = byId('write-project-settings');
    var preparing = prepare ? prepare.checked : false;
    if (writeProject) {
      writeProject.disabled = preparing;
      var label = (writeProject.closest && writeProject.closest('.checkbox-field')) ||
        writeProject.parentNode;
      if (label && label.classList) {
        label.classList.toggle('is-disabled', preparing);
      }
      writeProject.title = preparing
        ? 'Handled by target-printer preparation while Prepare project for printer is enabled.'
        : '';
    }
  }

  /* =========================================================================
   * Open Filament Database
   * ======================================================================= */

  /**
   * Render the `openfilament` dataset summary line and initial status.
   * @param {object|null|undefined} summary
   */
  function renderOpenFilamentSummary(summary) {
    var dataset = byId('ofd-dataset');
    var status = byId('ofd-status');
    var data = (summary && typeof summary === 'object') ? summary : null;
    var cached = !!(data && data.cached === true);
    state.openfilament.cached = cached;

    var datasetText = 'Dataset: not downloaded';
    if (data && cached) {
      datasetText = 'Dataset: cached';
      if (asString(data.version)) {
        datasetText += ' (' + asString(data.version) + ')';
      }
      var counts = (data.counts && typeof data.counts === 'object') ? data.counts : null;
      if (counts) {
        var parts = [];
        for (var key in counts) {
          if (Object.prototype.hasOwnProperty.call(counts, key)) {
            parts.push(asString(counts[key]) + ' ' + key);
          }
        }
        if (parts.length > 0) {
          datasetText += ' - ' + parts.join(', ');
        }
      }
    }
    if (dataset) {
      dataset.textContent = datasetText;
      dataset.title = data && asString(data.generated_at) ? 'Generated ' + asString(data.generated_at) : '';
    }
    if (status && !state.openfilament.searched) {
      setStatus(status, demoSuffix(cached
        ? 'Dataset cached locally - search by name, brand or material.'
        : 'The first search downloads the Open Filament Database.'), 'muted');
    }
  }

  /**
   * Format the temperature range for an OFD result row.
   * @param {object} result
   * @returns {string}
   */
  function formatOfdTemps(result) {
    var nozzleMin = Number(result.min_print_temp);
    var nozzleMax = Number(result.max_print_temp);
    var bedMin = Number(result.min_bed_temp);
    var bedMax = Number(result.max_bed_temp);
    var parts = [];
    if (isFinite(nozzleMin) && isFinite(nozzleMax)) {
      parts.push('Nozzle ' + nozzleMin + '-' + nozzleMax + ' \u00b0C');
    } else if (isFinite(nozzleMax)) {
      parts.push('Nozzle ' + nozzleMax + ' \u00b0C');
    }
    if (isFinite(bedMin) && isFinite(bedMax)) {
      parts.push('Bed ' + bedMin + '-' + bedMax + ' \u00b0C');
    } else if (isFinite(bedMax)) {
      parts.push('Bed ' + bedMax + ' \u00b0C');
    }
    return parts.join(' \u00b7 ');
  }

  /**
   * Build one Open Filament Database result row (checkbox, swatch, name,
   * material chip, density, temperatures). Safe DOM only.
   * @param {object} result
   * @returns {HTMLElement}
   */
  function createOfdRow(result) {
    var row = document.createElement('div');
    row.className = 'catalog-row';

    var label = document.createElement('label');
    label.className = 'catalog-row__label';

    var checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'catalog-row__checkbox';
    checkbox.value = asString(result.key);
    checkbox.checked = state.openfilament.selectedKeys.has(checkbox.value);
    checkbox.setAttribute('aria-label', 'Select ' + ofdDisplayName(result));
    label.appendChild(checkbox);

    label.appendChild(createSwatch(
      result.color_hex,
      asString(result.variant) || ofdDisplayName(result)
    ));

    var main = document.createElement('span');
    main.className = 'catalog-row__main';
    var name = document.createElement('span');
    name.className = 'catalog-row__name';
    name.textContent = ofdDisplayName(result);
    main.appendChild(name);
    var temps = formatOfdTemps(result);
    if (temps) {
      var meta = document.createElement('span');
      meta.className = 'catalog-row__meta';
      meta.textContent = temps;
      main.appendChild(meta);
    }
    label.appendChild(main);

    var material = asString(result.material).toUpperCase();
    label.appendChild(createChip(material || '\u2014', material ? 'Material: ' + material : 'No material reported'));

    var density = document.createElement('span');
    density.className = 'catalog-row__density';
    density.textContent = formatDensity(result.density_g_cm3);
    density.title = 'Density';
    label.appendChild(density);

    row.appendChild(label);
    return row;
  }

  /**
   * Display name for an OFD result: "Brand - Filament - Variant".
   * @param {object} result
   * @returns {string}
   */
  function ofdDisplayName(result) {
    var parts = [asString(result.brand), asString(result.filament), asString(result.variant)]
      .filter(function (part) { return part.length > 0; });
    return parts.length > 0 ? parts.join(' - ') : 'Unknown filament';
  }

  /**
   * Render the OFD results list, preserving selection by result key.
   */
  function renderOfdResults() {
    var container = byId('ofd-results');
    if (!container) {
      return;
    }
    container.textContent = '';
    var results = state.openfilament.results;

    if (!state.openfilament.searched) {
      var notSearched = document.createElement('p');
      notSearched.className = 'empty-state';
      notSearched.textContent = 'No search yet.';
      container.appendChild(notSearched);
    } else if (results.length === 0) {
      var empty = document.createElement('p');
      empty.className = 'empty-state';
      empty.textContent = 'No filaments matched your search. Try a broader query or material filter.';
      container.appendChild(empty);
    } else {
      for (var i = 0; i < results.length; i += 1) {
        container.appendChild(createOfdRow(results[i]));
      }
    }
    updateOfdSelectionUi();
  }

  /**
   * Refresh the OFD selected count and import-button availability.
   */
  function updateOfdSelectionUi() {
    var selection = byId('ofd-selection');
    if (selection) {
      selection.textContent = state.openfilament.selectedKeys.size + ' selected';
    }
    var importButton = byId('ofd-import-btn');
    if (importButton) {
      importButton.disabled = state.openfilament.selectedKeys.size === 0 ||
        isActionPending('openfilament_import');
    }
  }

  /**
   * Drop OFD selected keys that are not in the current result set.
   */
  function pruneOfdSelection() {
    var valid = new Set();
    for (var i = 0; i < state.openfilament.results.length; i += 1) {
      valid.add(asString(state.openfilament.results[i].key));
    }
    state.openfilament.selectedKeys.forEach(function (key) {
      if (!valid.has(key)) {
        state.openfilament.selectedKeys.delete(key);
      }
    });
  }

  /**
   * "Search Open Filament Database" click handler.
   */
  function onOfdSearchClick() {
    if (isActionPending('openfilament_search')) {
      return;
    }
    var queryInput = byId('ofd-query');
    var query = asString(queryInput && queryInput.value).trim();
    var material = selectValue('ofd-material');
    if (!query && !material) {
      setStatus(byId('ofd-status'), 'Enter a search term or choose a material filter.', 'warning');
      if (queryInput) {
        queryInput.focus();
      }
      return;
    }
    var pendingText = state.openfilament.cached
      ? 'Searching the Open Filament Database...'
      : 'Downloading the filament database (first search)...';
    request('openfilament_search', {
      query: query,
      material: material,
      limit: OFD_RESULT_LIMIT
    }, {
      statusEl: byId('ofd-status'),
      pendingText: pendingText,
      button: byId('ofd-search-btn'),
      timeoutMs: OFD_TIMEOUT_MS
    });
  }

  /**
   * `openFilamentData`: display search results + dataset summary.
   * @param {object} payload
   */
  function handleOpenFilamentData(payload) {
    var status = byId('ofd-status');
    state.openfilament.searched = true;

    if (payload.ok === false) {
      state.openfilament.results = [];
      pruneOfdSelection();
      renderOfdResults();
      var failure = demoSuffix('Open Filament Database search failed: ' + replyError(payload));
      setStatus(status, failure, 'error');
      appendLog(failure, 'error');
      return;
    }

    state.openfilament.cached = true;
    state.openfilament.results = Array.isArray(payload.results) ? payload.results.slice() : [];
    pruneOfdSelection();
    renderOfdResults();

    var summary = (payload.summary && typeof payload.summary === 'object') ? payload.summary : null;
    if (summary) {
      renderOpenFilamentSummary({
        cached: true,
        version: summary.version,
        generated_at: summary.generated_at,
        counts: summary.counts
      });
    }

    var count = state.openfilament.results.length;
    if (count === 0) {
      setStatus(status, demoSuffix('No filaments matched your search.'), 'warning');
    } else {
      setStatus(status, demoSuffix('Found ' + pluralize(count, 'filament') +
        (summary && asString(summary.version) ? ' in database ' + asString(summary.version) : '') + '.'), 'success');
    }
    appendLog(demoSuffix('Open Filament Database search returned ' + pluralize(count, 'result') + '.'), 'info');
  }

  /**
   * `openFilamentImportResult`: report OFD imports, refresh Favorites and
   * optional catalog/material payloads, keep the result selection.
   * @param {object} payload
   */
  function handleOpenFilamentImportResult(payload) {
    var status = byId('ofd-status');
    if (payload.ok === false) {
      var failure = demoSuffix('Open Filament Database import failed: ' + replyError(payload));
      setStatus(status, failure, 'error');
      appendLog(failure, 'error');
      return;
    }

    applyLibraryRefresh(payload);

    var imported = Array.isArray(payload.imported) ? payload.imported : [];
    var errors = Array.isArray(payload.errors) ? payload.errors : [];
    var favoriteCount = 0;
    for (var i = 0; i < imported.length; i += 1) {
      if (asString(imported[i] && imported[i].favorite_name)) {
        favoriteCount += 1;
      }
    }

    var summary;
    var kind;
    if (errors.length === 0) {
      summary = 'Imported ' + pluralize(imported.length, 'filament') + ' from the Open Filament Database - ' +
        favoriteCount + ' in Favorites';
      var catalogCount = Number(payload.catalog_count);
      if (isFinite(catalogCount) && catalogCount >= 0) {
        summary += '; catalog now ' + pluralize(catalogCount, 'filament');
      }
      summary = demoSuffix(summary + '.');
      kind = 'success';
    } else if (imported.length > 0) {
      summary = demoSuffix('Imported ' + imported.length + ' filament(s) (' + favoriteCount +
        ' in Favorites); ' + errors.length + ' failed: ' + summarizeErrors(errors));
      kind = 'warning';
    } else {
      summary = demoSuffix('Open Filament Database import failed: ' + summarizeErrors(errors));
      kind = 'error';
    }
    setStatus(status, summary, kind);
    appendLog(summary, kind === 'error' ? 'error' : (kind === 'warning' ? 'warning' : 'info'));
    updateOfdSelectionUi();
    if (imported.length > 0) {
      setStatus(byId('materials-status'), demoSuffix('Added ' + pluralize(imported.length, 'filament') +
        ' to Fusion. Assign them to bodies with the Material Browser.'), 'info');
      showScreen('home');
    }
  }

  /**
   * "Import selected" click handler for OFD results.
   */
  function onOfdImportClick() {
    if (isActionPending('openfilament_import')) {
      return;
    }
    var keys = Array.from(state.openfilament.selectedKeys);
    if (keys.length === 0) {
      setStatus(byId('ofd-status'), 'Select at least one Open Filament Database result first.', 'warning');
      return;
    }
    request('openfilament_import', { keys: keys }, {
      statusEl: byId('ofd-status'),
      pendingText: 'Importing ' + pluralize(keys.length, 'filament') + ' into Favorites and the design...',
      button: byId('ofd-import-btn'),
      timeoutMs: OFD_TIMEOUT_MS
    });
  }

  /* =========================================================================
   * Custom material library
   * ======================================================================= */

  /**
   * Normalise a custom_library payload from Fusion.
   * @param {*} raw
   * @returns {{ok: boolean, path: string, exists: boolean, loaded: boolean,
   *            material_count: number, message: string}|null}
   */
  function normalizeCustomLibrary(raw) {
    if (!raw || typeof raw !== 'object') {
      return null;
    }
    var count = Number(raw.material_count);
    if (!isFinite(count) || count < 0) {
      count = 0;
    }
    return {
      ok: raw.ok !== false,
      path: asString(raw.path),
      exists: raw.exists === true,
      loaded: raw.loaded === true,
      material_count: Math.floor(count),
      message: asString(raw.message)
    };
  }

  /**
   * Render the Custom Material Library status line + help message.
   * @param {object|null|undefined} raw
   */
  function renderCustomLibrary(raw) {
    var status = byId('custom-library-status');
    var messageElement = byId('custom-library-message');
    var library = normalizeCustomLibrary(raw);

    if (!library) {
      if (status) {
        setStatus(status, 'Library status not reported by Fusion.', 'muted');
      }
      if (messageElement) {
        messageElement.textContent = '';
        messageElement.hidden = true;
      }
      return;
    }

    state.customLibrary = library;

    if (messageElement) {
      if (library.message) {
        messageElement.textContent = library.message;
        messageElement.hidden = false;
      } else {
        messageElement.textContent = '';
        messageElement.hidden = true;
      }
    }
    if (!status) {
      return;
    }

    var text;
    var kind;
    if (library.ok === false) {
      text = 'Library unavailable' +
        (library.message ? ' - ' + library.message : '');
      kind = 'error';
    } else if (!library.path || !library.exists) {
      text = 'Library not created yet.';
      kind = 'muted';
    } else {
      text = 'Library: ' + pluralize(library.material_count, 'filament') +
        (library.loaded ? ', loaded in Fusion.' : ', not loaded in Fusion.');
      kind = library.loaded ? 'success' : (library.exists ? 'warning' : 'muted');
    }
    setStatus(status, text, kind);
    status.title = library.message || library.path;
  }

  /**
   * Apply the catalog/library data that import and library replies carry:
   * catalog + categories (Catalog tab), Favorites, the custom library status
   * and the design materials. Missing fields are left as they are.
   * @param {object} payload
   */
  function applyLibraryRefresh(payload) {
    if (!payload || typeof payload !== 'object') {
      return;
    }
    var favorites = normalizeFavorites(payload.favorites);
    if (favorites) {
      state.favorites = favorites;
      renderFavoritesStatus(favorites);
    }
    if (payload.custom_library) {
      renderCustomLibrary(payload.custom_library);
    }
    if (Array.isArray(payload.catalog)) {
      state.catalog = payload.catalog.slice();
      if (Array.isArray(payload.categories)) {
        state.categories = normalizeCategories(payload.categories);
      }
      catalogIndexCache = null;
      pruneSelection();
      renderCatalog();
    }
    if (Array.isArray(payload.materials)) {
      renderMaterials(payload.materials);
    }
  }

  /**
   * "Create / Update Filamatch Library" click handler.
   */
  function onCreateLibraryClick() {
    request('create_library', {}, {
      statusEl: byId('custom-library-status'),
      pendingText: 'Building the Filamatch material library...',
      button: byId('create-library-btn'),
      timeoutMs: CUSTOM_LIBRARY_TIMEOUT_MS
    });
  }

  /**
   * `libraryCreated`: apply the rebuilt custom library status, refresh optional
   * payload data, and report success/failure in the log.
   * @param {object} payload
   */
  function handleLibraryCreated(payload) {
    if (payload.ok === false) {
      if (payload.custom_library) {
        renderCustomLibrary(payload.custom_library);
      } else {
        setStatus(byId('custom-library-status'),
          'Could not create the Filamatch material library: ' + replyError(payload), 'error');
      }
      var failure = 'Filamatch material library failed: ' + replyError(payload);
      appendLog(failure, 'error');
      return;
    }

    applyLibraryRefresh(payload);

    var library = state.customLibrary;
    var summary = 'Filamatch material library updated' +
      (library.path ? ': ' + library.path : '') +
      (library.exists ? ' (' + pluralize(library.material_count, 'material') + ')' : '') + '.';
    appendLog(demoSuffix(summary), 'info');
    scheduleSettingsSave();
  }

  /* =========================================================================
   * Settings + export
   * ======================================================================= */

  /**
   * Render the version chip.
   */
  function renderVersion() {
    var version = byId('app-version');
    if (version) {
      version.textContent = 'v' + (state.version || '\u2014');
    }
  }

  /**
   * Populate the settings/export/slicer form fields from `initData`.
   * @param {object} settings
   */
  function renderSettings(settings) {
    var outputPath = byId('output-path');
    var writeProject = byId('write-project-settings');
    var orcaEnabled = byId('orca-enabled');
    var orcaPath = byId('orca-path');
    var orcaProfileDir = byId('orca-profile-dir');

    if (outputPath) {
      outputPath.value = settings.output_path;
    }
    if (writeProject) {
      writeProject.checked = settings.write_project_settings !== false;
    }
    if (orcaEnabled) {
      orcaEnabled.checked = settings.orca_enabled === true;
    }
    if (orcaPath) {
      orcaPath.value = settings.slicer_path || settings.orca_path;
    }
    if (orcaProfileDir) {
      orcaProfileDir.value = settings.orca_profile_dir;
    }
    var prepareEnabled = byId('prepare-enabled');
    if (prepareEnabled) {
      prepareEnabled.checked = settings.prepare_enabled === true;
    }
    var autoLibraryUpdate = byId('auto-library-update');
    if (autoLibraryUpdate) {
      autoLibraryUpdate.checked = settings.auto_library_update !== false;
    }
    var verifyTls = byId('bambuddy-verify-tls');
    if (verifyTls) {
      verifyTls.checked = settings.bambuddy_verify_tls !== false;
    }
    syncPrepareUi();
    renderSlicerSummary();
    renderSaveLine();
    renderExportButton();
  }

  /**
   * Read the current settings form values in the `save_settings` shape.
   * @returns {object}
   */
  function collectSettings() {
    var outputPath = byId('output-path');
    var writeProject = byId('write-project-settings');
    var orcaEnabled = byId('orca-enabled');
    var orcaPath = byId('orca-path');
    var orcaProfileDir = byId('orca-profile-dir');
    var prepareEnabled = byId('prepare-enabled');
    var autoLibraryUpdate = byId('auto-library-update');
    var verifyTls = byId('bambuddy-verify-tls');
    var slicerPath = orcaPath ? orcaPath.value.trim() : '';
    var payload = {
      output_path: outputPath ? outputPath.value.trim() : '',
      write_project_settings: writeProject ? writeProject.checked : true,
      orca_enabled: orcaEnabled ? orcaEnabled.checked : false,
      orca_path: slicerPath,
      orca_profile_dir: orcaProfileDir ? orcaProfileDir.value.trim() : '',
      prepare_enabled: prepareEnabled ? prepareEnabled.checked : false,
      target_vendor: selectValue('target-vendor'),
      target_printer: selectValue('target-printer'),
      target_process: selectValue('target-process'),
      slicer_path: slicerPath,
      auto_library_update: autoLibraryUpdate ? autoLibraryUpdate.checked : true,
      custom_library_path: (state.customLibrary && state.customLibrary.path) ||
        state.settings.custom_library_path || '',
      bambuddy_printer_id: state.bambuddy.selectedPrinterId || 0,
      bambuddy_slot_map: state.bambuddy.slotMap || {},
      bambuddy_verify_tls: verifyTls ? verifyTls.checked : true
    };
    /* The Bambuddy address and API key are sent only by an explicit Connect
       (requestBambuddyContext), never by the autosave. */
    return payload;
  }

  /**
   * Persist the settings form via `save_settings` (debounced by the caller).
   */
  function saveSettingsNow() {
    if (!state.settingsLoaded) {
      return;
    }
    request('save_settings', collectSettings(), {
      statusEl: byId('settings-status'),
      pendingText: 'Saving settings...'
    });
  }

  /**
   * Debounce-and-save the settings form after an edit.
   */
  function scheduleSettingsSave() {
    if (!state.settingsLoaded) {
      return;
    }
    setStatus(byId('settings-status'), 'Saving settings...', 'info');
    if (settingsSaveTimer !== null) {
      window.clearTimeout(settingsSaveTimer);
    }
    settingsSaveTimer = window.setTimeout(function () {
      settingsSaveTimer = null;
      saveSettingsNow();
    }, SETTINGS_SAVE_DEBOUNCE_MS);
  }

  /**
   * Handle the "Export 3MF" button: validate the path, then send the automatic
   * export request (no mapping is sent - the add-in derives it from the
   * detected assembly materials).
   */
  function onExportClick() {
    if (isActionPending('export_3mf')) {
      setStatus(byId('export-status'), 'An export is already running.', 'info');
      return;
    }
    var outputPath = byId('output-path');
    var path = outputPath ? outputPath.value.trim() : '';
    if (!path) {
      setStatus(byId('export-status'), 'Choose where to save the 3MF first.', 'warning');
      onBrowseExportPathClick(byId('export-status'), byId('browse-export-path-btn'));
      return;
    }

    var exportDetails = byId('export-details');
    if (exportDetails) {
      exportDetails.hidden = true;
      exportDetails.textContent = '';
    }

    if (state.materials.length === 0) {
      appendLog('Export requested with no detected assembly materials; slots will default.', 'warning');
    }

    var writeProject = byId('write-project-settings');
    var orcaEnabled = byId('orca-enabled');
    var orcaPath = byId('orca-path');
    var orcaProfileDir = byId('orca-profile-dir');
    var prepareEnabled = byId('prepare-enabled');
    var preparing = prepareEnabled ? prepareEnabled.checked : false;
    var targetVendor = selectValue('target-vendor');
    var targetPrinter = selectValue('target-printer');
    var targetProcess = selectValue('target-process');
    var slicerPath = orcaPath ? orcaPath.value.trim() : '';

    if (preparing && (!targetVendor || !targetPrinter || !targetProcess)) {
      appendLog('Prepare is enabled but no complete printer/process selection was found; ' +
        'the export will fall back to a base-only project.', 'warning');
    }

    var payload = {
      output_path: path,
      write_project_settings: writeProject ? writeProject.checked : true,
      orca: {
        enabled: orcaEnabled ? orcaEnabled.checked : false,
        executable: slicerPath,
        profile_dir: orcaProfileDir ? orcaProfileDir.value.trim() : ''
      },
      target: {
        prepare: preparing,
        slicer_path: slicerPath,
        vendor: targetVendor,
        printer: targetPrinter,
        process: targetProcess
      },
      bambuddy_slot_map: state.bambuddy.slotMap || {},
      bambuddy_printer_id: state.bambuddy.selectedPrinterId || 0
    };

    appendLog('Requested 3MF export to ' + path +
      (preparing ? ' (prepared for ' + targetPrinter + ')' : '') + '.', 'info');
    request('export_3mf', payload, {
      statusEl: byId('export-status'),
      pendingText: payload.orca.enabled ? 'Exporting and opening Orca...' : 'Exporting the 3MF...',
      button: byId('export-btn'),
      timeoutMs: EXPORT_TIMEOUT_MS
    });
  }

  /* =========================================================================
   * Demo-mode simulation
   * ======================================================================= */

  /**
   * Simulate a custom-library status payload for demo mode.
   * @param {string} [message]
   * @returns {object}
   */
  function demoCustomLibraryPayload(message) {
    return {
      ok: true,
      path: DEMO_INIT.custom_library.path,
      exists: true,
      loaded: true,
      material_count: state.demoFavoriteNames.length,
      message: message || DEMO_INIT.custom_library.message
    };
  }

  /**
   * Turn on standalone-browser preview mode: sample data + simulated replies.
   */
  function setupDemoMode() {
    state.demoMode = true;
    state.demoLog = DEMO_INIT.log.slice();
    state.demoFavoriteNames = DEMO_INIT.favorites.names.slice();
    showDemoBanner(true);
    setConnectionState('warning', 'Preview mode - open this palette from Fusion to connect. Showing demo data.');
  }

  /**
   * Build the demo-mode Favorites payload ({count, names[]}).
   * @returns {{count: number, names: string[]}}
   */
  function demoFavoritesPayload() {
    return {
      count: state.demoFavoriteNames.length,
      names: state.demoFavoriteNames.slice()
    };
  }

  /**
   * RGB distance between two hex colours (demo matching only).
   * @param {string} a
   * @param {string} b
   * @returns {number}
   */
  function demoColorDistance(a, b) {
    var x = normalizeHex(a) || '#808080';
    var y = normalizeHex(b) || '#808080';
    var total = 0;
    for (var i = 1; i < 7; i += 2) {
      var delta = parseInt(x.substr(i, 2), 16) - parseInt(y.substr(i, 2), 16);
      total += delta * delta;
    }
    return Math.sqrt(total);
  }

  /**
   * Demo slot matching: same type, nearest colour; user choices win.
   * @returns {object[]}
   */
  function demoMapping() {
    return state.materials.map(function (material) {
      var id = asString(material.material_id);
      var row = {
        material_id: id,
        display_name: asString(material.display_name),
        color_hex: asString(material.color_hex),
        filament_type: asString(material.filament_type),
        slot_key: '',
        slot_label: '',
        confidence: 'unmapped',
        auto: false,
        overridden: false
      };
      var override = asString(state.bambuddy.slotMap[id]);
      var pick = null;
      var confidence = 'unmapped';
      if (override && override !== 'none') {
        pick = DEMO_BAMBUDDY.slots.filter(function (slot) { return slot.key === override; })[0] || null;
        confidence = 'manual';
      } else if (override !== 'none') {
        var best = null;
        var bestDistance = Infinity;
        DEMO_BAMBUDDY.slots.forEach(function (slot) {
          if (slot.filament_type !== asString(material.filament_type).toUpperCase()) {
            return;
          }
          var distance = demoColorDistance(slot.color_hex, material.color_hex);
          if (distance < bestDistance) {
            best = slot;
            bestDistance = distance;
          }
        });
        if (best && bestDistance < 160) {
          pick = best;
          confidence = bestDistance < 40 ? 'auto' : 'review';
        }
      }
      if (pick) {
        row.slot_key = pick.key;
        row.slot_label = pick.label;
        row.confidence = confidence;
        row.auto = confidence === 'auto';
        row.overridden = confidence === 'manual';
      }
      return row;
    });
  }

  /**
   * Simulate a `profilesData` reply for the demo mode.
   * @param {object} request Requested get_profiles payload.
   * @returns {object}
   */
  function demoProfilesPayload(request) {
    var req = (request && typeof request === 'object') ? request : {};
    var vendors = DEMO_PROFILE_VENDORS.slice();
    var vendorId = asString(req.vendor);
    var vendorKnown = false;
    var i;
    for (i = 0; i < vendors.length; i += 1) {
      if (asString(vendors[i].id) === vendorId) {
        vendorKnown = true;
        break;
      }
    }
    if (!vendorKnown) {
      vendorId = DEMO_PROFILES.selected.vendor;
    }

    var printers = demoPrinterRows(vendorId);
    var requestedPrinter = asString(req.printer);
    var printer = null;
    for (i = 0; i < printers.length; i += 1) {
      if (printers[i].name === requestedPrinter) {
        printer = printers[i];
        break;
      }
    }
    if (!printer) {
      printer = printers[0];
    }

    var processes = demoProcessRows();
    var processName = '';
    for (i = 0; i < processes.length; i += 1) {
      if (processes[i].default === true) {
        processName = processes[i].name;
        break;
      }
    }

    return {
      ok: true,
      slicers: DEMO_PROFILES.slicers.slice(),
      vendors: vendors,
      printers: printers,
      processes: processes,
      imported: DEMO_PROFILES.imported,
      selected: {
        slicer_path: asString(req.slicer_path) || DEMO_PROFILES.selected.slicer_path,
        vendor: vendorId,
        printer: printer.name,
        process: processName
      }
    };
  }

  /**
   * Schedule a simulated Python reply for a demo-mode request.
   * @param {string} requestAction
   * @param {object} payload
   */
  function scheduleDemoResponse(requestAction, payload) {
    var responseAction = RESPONSE_BY_REQUEST[requestAction] || requestAction;
    var delay = 220 + Math.round(Math.random() * 160);
    window.setTimeout(function () {
      var response;
      try {
        var builder = DEMO_RESPONSE_BUILDERS[requestAction];
        response = builder ? builder(payload) : {};
      } catch (err) {
        appendLog('Demo simulation failed for "' + requestAction + '": ' + errorMessage(err), 'error');
        response = {};
      }
      dispatchIncoming(responseAction, response);
    }, delay);
  }

  /**
   * Demo-mode reply builders. Each returns the payload Python would send.
   * @type {Object<string, function(object): object>}
   */
  var DEMO_RESPONSE_BUILDERS = {
    init: function () {
      return DEMO_INIT;
    },

    import_selected: function (payload) {
      var ids = Array.isArray(payload.ids) ? payload.ids : [];
      var imported = [];
      var errors = [];
      for (var i = 0; i < ids.length; i += 1) {
        var id = asString(ids[i]);
        var entry = findCatalogEntry(id);
        if (entry) {
          var favoriteName = displayName(entry);
          if (state.demoFavoriteNames.indexOf(favoriteName) === -1) {
            state.demoFavoriteNames.push(favoriteName);
          }
          var relinked = id === 'bambu_pla_basic_black' ? 2 : 0;
          imported.push({
            id: id,
            fusion_name: 'bambu_' + id,
            appearance_name: 'FFB ' + id,
            favorite_name: favoriteName,
            favorite_appearance_name: 'FFB ' + id,
            color_hex: asString(entry.color_hex),
            replaced: false,
            relinked_bodies: relinked
          });
          state.demoLog.push(
            'Imported ' + favoriteName + ' into Favorites and the design (demo)' +
            (relinked ? '; relinked ' + pluralize(relinked, 'body', 'bodies') : '') + '.'
          );
        } else {
          errors.push({ id: id, message: 'Unknown catalog id' });
          state.demoLog.push('Import failed (demo): unknown catalog id ' + id);
        }
      }
      if (imported.length > 0) {
        state.demoLog.push('Custom library auto-updated after import (demo): ' +
          pluralize(state.demoFavoriteNames.length, 'material') + '.');
      }
      return {
        ok: errors.length === 0,
        imported: imported,
        errors: errors,
        materials: state.materials,
        library: DEMO_INIT.library,
        favorites: demoFavoritesPayload(),
        custom_library: demoCustomLibraryPayload(
          imported.length > 0 ? 'Updated automatically after import (demo).' : undefined
        ),
        log: state.demoLog.slice()
      };
    },

    refresh_materials: function () {
      state.demoLog.push('Rescanned the active design (demo): ' +
        pluralize(state.materials.length, 'material') + ' detected.');
      return {
        ok: true,
        materials: state.materials,
        favorites: demoFavoritesPayload(),
        log: state.demoLog.slice()
      };
    },

    export_3mf: function (payload) {
      var outputPath = asString(payload.output_path);
      var rawPath = outputPath.replace(/\.3mf$/i, '_raw.3mf');
      if (rawPath === outputPath) {
        rawPath = outputPath + '.raw';
      }
      var used = state.materials.slice().sort(function (a, b) {
        return slotSortValue(a) - slotSortValue(b);
      }).map(function (material) {
        return {
          slot: Number(material.slot) || 0,
          material_id: asString(material.material_id),
          display_name: asString(material.display_name),
          color_hex: asString(material.color_hex),
          filament_type: asString(material.filament_type),
          density_g_cm3: material.density_g_cm3,
          body_count: Number(material.body_count) || 0
        };
      });
      var target = (payload.target && typeof payload.target === 'object') ? payload.target : {};
      var prepared = target.prepare === true;
      state.demoLog.push(prepared
        ? 'Prepared project for ' + asString(target.printer) + ' / ' + asString(target.process) +
          ' and exported (demo): ' + outputPath
        : 'Exported base-only 3MF without a printer profile (demo): ' + outputPath);
      return {
        ok: true,
        output_path: outputPath,
        raw_export_path: rawPath,
        materials_used: used,
        prepared: prepared,
        printer_name: prepared ? asString(target.printer) : '',
        process_name: prepared ? asString(target.process) : '',
        project_settings_written: payload.write_project_settings === true || prepared,
        orca_launched: false,
        error: '',
        log: state.demoLog.slice(),
        log_tail: prepared
          ? ['Prepared project for ' + asString(target.printer) + ' (' + asString(target.process) + ').',
             'Wrote project_settings.config from the printer profile.']
          : ['Exported without a printer profile (base-only project).']
      };
    },

    run_diagnostics: function () {
      state.demoLog.push('Diagnostics executed (demo).');
      return {
        ok: true,
        text: [
          'Filamatch - demo diagnostics',
          '  preview mode       : ON (not connected to Fusion)',
          '  catalog fixtures   : ' + state.catalog.length + ' filament(s)',
          '  categories         : ' + getCategoryIndex().categoryNames.length,
          '  detected materials : ' + state.materials.length,
          '  detected bodies    : ' + countBodies(state.materials),
          '  favorites          : ' + state.demoFavoriteNames.length + ' app-level material(s)',
          '  bridge requests    : ' + Object.keys(RESPONSE_BY_REQUEST).length,
          '',
          'Open this palette from Fusion for real diagnostics.'
        ].join('\n')
      };
    },

    save_settings: function (payload) {
      state.demoLog.push('Settings saved (demo).');
      var saved = normalizeSettings(payload);
      saved.bambuddy_url = state.bambuddy.url;
      saved.bambuddy_api_key_set = state.settings.bambuddy_api_key_set;
      return {
        ok: true,
        settings: saved,
        log: state.demoLog.slice()
      };
    },

    clear_bambuddy_key: function () {
      state.demoLog.push('Removed the saved Bambuddy API key (demo).');
      var saved = normalizeSettings(state.settings);
      saved.bambuddy_api_key_set = false;
      return {
        ok: true,
        settings: saved,
        log: state.demoLog.slice()
      };
    },

    browse_export_path: function () {
      state.demoLog.push('Native save dialog simulated (demo).');
      return {
        ok: true,
        path: '/Users/demo/Documents/Filamatch/browsed_AMS.3mf'
      };
    },

    browse_slicer_path: function () {
      state.demoLog.push('Native open dialog simulated (demo).');
      return {
        ok: true,
        path: '/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer'
      };
    },

    get_profiles: function (payload) {
      var profiles = demoProfilesPayload(payload);
      state.demoLog.push('Loaded printer profiles (demo): ' + profiles.vendors.length + ' vendors, ' +
        profiles.printers.length + ' printers, ' + profiles.processes.length + ' processes.');
      profiles.log = state.demoLog.slice();
      return profiles;
    },

    import_profiles: function () {
      state.demoLog.push('Imported slicer profiles (demo): 1 machine, 2 processes, 3 filaments.');
      return {
        ok: true,
        imported: { machines: 1, processes: 2, filaments: 3, root: '/Users/demo/Documents/ImportedProfiles' },
        profiles: demoProfilesPayload({ vendor: 'imported', printer: 'Imported: My Custom Printer (0.4 nozzle)' }),
        log: state.demoLog.slice()
      };
    },

    openfilament_search: function (payload) {
      var query = asString(payload.query).trim().toLowerCase();
      var material = asString(payload.material).trim().toLowerCase();
      var tokens = query ? query.split(/\s+/).filter(function (token) { return token.length > 0; }) : [];
      var matched = DEMO_OFD_RESULTS.filter(function (result) {
        if (material && asString(result.material).toLowerCase() !== material.toLowerCase()) {
          return false;
        }
        var haystack = [result.brand, result.material, result.filament, result.variant].join(' ').toLowerCase();
        for (var i = 0; i < tokens.length; i += 1) {
          if (haystack.indexOf(tokens[i]) === -1) {
            return false;
          }
        }
        return true;
      });
      state.demoLog.push('Open Filament Database search (demo): ' +
        pluralize(matched.length, 'result') + ' for "' + query + '".');
      return {
        ok: true,
        results: matched,
        summary: DEMO_OFD_SUMMARY,
        log: state.demoLog.slice()
      };
    },

    openfilament_import: function (payload) {
      var keys = Array.isArray(payload.keys) ? payload.keys.map(asString) : [];
      var imported = [];
      var errors = [];
      for (var i = 0; i < keys.length; i += 1) {
        var result = null;
        for (var j = 0; j < DEMO_OFD_RESULTS.length; j += 1) {
          if (DEMO_OFD_RESULTS[j].key === keys[i]) {
            result = DEMO_OFD_RESULTS[j];
            break;
          }
        }
        if (!result) {
          errors.push({ key: keys[i], message: 'Unknown Open Filament Database key' });
          continue;
        }
        var display = ofdDisplayName(result);
        var shortId = result.key.replace(/^ofd_/, '');
        imported.push({
          id: 'ofd_' + shortId,
          fusion_name: 'ofd_' + shortId,
          color_hex: result.color_hex,
          favorite_name: display
        });
        if (state.demoFavoriteNames.indexOf(display) === -1) {
          state.demoFavoriteNames.push(display);
        }
      }
      state.demoLog.push('Imported ' + pluralize(imported.length, 'filament') +
        ' from the Open Filament Database (demo).');
      return {
        ok: errors.length === 0,
        imported: imported,
        errors: errors,
        favorites: demoFavoritesPayload(),
        catalog: state.catalog,
        categories: state.categories,
        custom_library: demoCustomLibraryPayload('Updated automatically after import (demo).'),
        catalog_count: state.catalog.length,
        log: state.demoLog.slice()
      };
    },

    create_library: function () {
      var library = demoCustomLibraryPayload('Library rebuilt from the current Favorites (demo).');
      state.demoLog.push('Filamatch material library updated (demo): ' + library.path +
        ' (' + pluralize(library.material_count, 'material') + ').');
      return {
        ok: true,
        custom_library: library,
        error: '',
        log: state.demoLog.slice()
      };
    },

    bambuddy_context: function (payload) {
      /* The second demo printer has nothing loaded. */
      var printerId = Number(payload.bambuddy_printer_id) === 2 ? 2 : 1;
      return {
        ok: true,
        configured: true,
        source: 'bambuddy',
        selected_printer_id: printerId,
        printers: DEMO_BAMBUDDY.printers,
        slots: printerId === 1 ? DEMO_BAMBUDDY.slots : [],
        mapping: printerId === 1 ? demoMapping() : []
      };
    },

    bambuddy_import: function (payload) {
      var keys = Array.isArray(payload.slot_keys) ? payload.slot_keys.map(asString) : [];
      var imported = DEMO_BAMBUDDY.slots.filter(function (slot) {
        return keys.indexOf(slot.key) !== -1;
      }).map(function (slot) {
        if (state.demoFavoriteNames.indexOf(slot.display_name) === -1) {
          state.demoFavoriteNames.push(slot.display_name);
        }
        return { id: 'bambuddy_spool_' + slot.spool_id, display_name: slot.display_name, slot_key: slot.key };
      });
      state.demoLog.push('Added ' + imported.length + ' printer filament(s) to Favorites (demo).');
      return {
        ok: true,
        imported: imported,
        favorites: demoFavoritesPayload(),
        catalog: state.catalog,
        categories: state.categories,
        custom_library: demoCustomLibraryPayload('Updated automatically after import (demo).'),
        materials: state.materials,
        log: state.demoLog.slice()
      };
    },

    get_log: function () {
      return { log: state.demoLog.slice() };
    }
  };

  /**
   * Look up a catalog entry by id.
   * @param {string} id
   * @returns {object|null}
   */
  function findCatalogEntry(id) {
    var wanted = asString(id);
    for (var i = 0; i < state.catalog.length; i += 1) {
      if (asString(state.catalog[i].id) === wanted) {
        return state.catalog[i];
      }
    }
    return null;
  }

  /* =========================================================================
   * Screens: Home, Add filaments, Settings, Welcome
   * ======================================================================= */

  /** localStorage key remembering that the user skipped the welcome screen. */
  var WELCOME_DISMISSED_STORAGE_KEY = 'ffb-welcome-dismissed';

  /** Minimum gap between two automatic design checks (palette focus). */
  var DESIGN_CHECK_INTERVAL_MS = 1500;

  var fitHeightTimer = 0;
  var lastFitContent = 0;

  /**
   * Ask Fusion to fit the palette height to the visible screen. Debounced,
   * and only sent when the content height really changed.
   */
  function requestFitHeight() {
    if (!bridgeLive()) {
      return;
    }
    window.clearTimeout(fitHeightTimer);
    fitHeightTimer = window.setTimeout(function () {
      var app = byId('app');
      if (!app) {
        return;
      }
      var content = Math.ceil(app.getBoundingClientRect().height);
      if (Math.abs(content - lastFitContent) < 8) {
        return;
      }
      lastFitContent = content;
      try {
        adsk.fusionSendData('fit_height', JSON.stringify({
          content: content,
          viewport: window.innerHeight
        }));
      } catch (err) {
        /* The palette size is cosmetic; never break the page for it. */
      }
    }, 150);
  }

  /** Re-fit the palette whenever the visible content changes size. */
  function watchContentHeight() {
    var app = byId('app');
    if (!app || typeof ResizeObserver !== 'function') {
      return;
    }
    new ResizeObserver(requestFitHeight).observe(app);
  }

  /**
   * Show one screen and hide the others. When the screen changes, keyboard
   * and screen-reader focus moves to the new screen's heading.
   * @param {'home'|'add'|'settings'|'welcome'} name
   */
  function showScreen(name) {
    var screens = document.querySelectorAll('[data-screen]');
    var target = null;
    for (var i = 0; i < screens.length; i += 1) {
      var matches = screens[i].getAttribute('data-screen') === name;
      screens[i].hidden = !matches;
      if (matches) {
        target = screens[i];
      }
    }
    if (!target) {
      return;
    }
    var changed = state.ui.screen !== name;
    state.ui.screen = name;
    window.scrollTo(0, 0);
    if (changed) {
      var heading = target.querySelector('h1');
      if (heading) {
        heading.focus({ preventScroll: true });
      }
    }
    if (name === 'home') {
      checkDesign();
    }
  }

  /** @returns {boolean} true on the first run, before Bambuddy is set up or skipped. */
  function shouldShowWelcome() {
    if (state.demoMode || state.bambuddy.configured) {
      return false;
    }
    try {
      return !(window.localStorage && window.localStorage.getItem(WELCOME_DISMISSED_STORAGE_KEY));
    } catch (err) {
      return false;
    }
  }

  /** Remember that the welcome screen is done (skipped or connected). */
  function dismissWelcome() {
    try {
      if (window.localStorage) {
        window.localStorage.setItem(WELCOME_DISMISSED_STORAGE_KEY, '1');
      }
    } catch (err) {
      /* Storage can be unavailable; the welcome screen then shows again next time. */
    }
  }

  /**
   * Switch the Add-filaments tab.
   * @param {'printer'|'catalog'|'online'} tab
   */
  function setAddTab(tab) {
    var tabs = document.querySelectorAll('[data-tab]');
    for (var i = 0; i < tabs.length; i += 1) {
      var active = tabs[i].getAttribute('data-tab') === tab;
      tabs[i].classList.toggle('is-active', active);
      tabs[i].setAttribute('aria-selected', active ? 'true' : 'false');
      /* Roving tabindex: only the selected tab is in the Tab order. */
      tabs[i].tabIndex = active ? 0 : -1;
    }
    var panels = document.querySelectorAll('[data-tab-panel]');
    for (var j = 0; j < panels.length; j += 1) {
      panels[j].hidden = panels[j].getAttribute('data-tab-panel') !== tab;
    }
  }

  /**
   * ARIA tab pattern: Left/Right (wrapping), Home and End move to and select
   * another tab.
   * @param {KeyboardEvent} event
   */
  function onTabKeydown(event) {
    var tabs = Array.prototype.slice.call(document.querySelectorAll('[data-tab]'));
    var index = tabs.indexOf(event.currentTarget);
    if (index === -1) {
      return;
    }
    var next;
    if (event.key === 'ArrowRight') {
      next = (index + 1) % tabs.length;
    } else if (event.key === 'ArrowLeft') {
      next = (index - 1 + tabs.length) % tabs.length;
    } else if (event.key === 'Home') {
      next = 0;
    } else if (event.key === 'End') {
      next = tabs.length - 1;
    } else {
      return;
    }
    event.preventDefault();
    setAddTab(tabs[next].getAttribute('data-tab'));
    tabs[next].focus();
  }

  /** Open the Add-filaments screen on the most useful tab. */
  function openAddFilaments() {
    setAddTab(state.bambuddy.configured ? 'printer' : 'catalog');
    showScreen('add');
  }

  /**
   * Show or hide a card body behind its toggle button.
   * @param {string} toggleId
   * @param {boolean} [open] force a state; toggles when omitted
   */
  function toggleCardBody(toggleId, open) {
    var toggle = byId(toggleId);
    var body = toggle ? byId(toggle.getAttribute('aria-controls')) : null;
    if (!toggle || !body) {
      return;
    }
    var show = open === undefined ? body.hidden : open;
    body.hidden = !show;
    toggle.setAttribute('aria-expanded', show ? 'true' : 'false');
  }

  /** Open Settings with the Bambuddy fields shown. */
  function openBambuddySetup() {
    showScreen('settings');
    toggleCardBody('bambuddy-edit-toggle', true);
    var url = byId('bambuddy-url');
    if (url && !url.value) {
      url.focus();
    }
  }

  /**
   * Ask Fusion whether the design changed (fire-and-forget). Fusion answers
   * with `materialsData` only when bodies or materials changed, so this is
   * cheap enough to run whenever the palette gets focus.
   */
  function checkDesign() {
    if (!bridgeLive() || !state.connectionReady) {
      return;
    }
    var now = Date.now();
    if (now - state.ui.lastDesignCheck < DESIGN_CHECK_INTERVAL_MS) {
      return;
    }
    state.ui.lastDesignCheck = now;
    try {
      adsk.fusionSendData('design_check', '{}');
    } catch (err) {
      appendLog('Could not ask Fusion for design changes: ' + errorMessage(err), 'warning');
    }
  }

  /**
   * Split a "Brand - Line - Colour" name into a short name ("Line Colour") and
   * the brand. Other names ("Brand Line - Colour") stay whole, without the dash.
   * @param {string} name
   * @returns {{name: string, brand: string}}
   */
  function splitDisplayName(name) {
    var parts = asString(name).split(' - ');
    if (parts.length >= 3) {
      return { name: parts.slice(1).join(' '), brand: parts[0] };
    }
    return { name: parts.join(' '), brand: '' };
  }

  /**
   * Short slot label ("AMS 2 · T4") for a slot label ("AMS 2 - T4").
   * @param {string} label
   * @returns {string}
   */
  function shortSlotLabel(label) {
    return asString(label).replace(/ - /g, ' · ');
  }

  /** @returns {object|null} the selected Bambuddy printer. */
  function selectedPrinter() {
    var id = Number(state.bambuddy.selectedPrinterId);
    return (state.bambuddy.printers || []).filter(function (printer) {
      return Number(printer.id) === id;
    })[0] || null;
  }

  /** @returns {Object<string, object>} mapping rows by Fusion material id. */
  function mappingByMaterial() {
    var rows = Array.isArray(state.bambuddy.mapping) ? state.bambuddy.mapping : [];
    var index = {};
    for (var i = 0; i < rows.length; i += 1) {
      index[asString(rows[i] && rows[i].material_id)] = rows[i];
    }
    return index;
  }

  /** @returns {Object<string, object>} loaded printer slots by key. */
  function slotsByKey() {
    var index = {};
    var slots = state.bambuddy.slots || [];
    for (var i = 0; i < slots.length; i += 1) {
      index[asString(slots[i] && slots[i].key)] = slots[i];
    }
    return index;
  }

  /**
   * Match quality of one mapping row.
   * @param {object|undefined} row
   * @returns {'ok'|'manual'|'close'|'missing'|'unknown'}
   */
  function matchKind(row) {
    if (!state.bambuddy.loaded) {
      return 'unknown';
    }
    if (!row || !row.slot_key) {
      return 'missing';
    }
    var confidence = asString(row.confidence);
    if (row.overridden === true || confidence === 'manual') {
      return 'manual';
    }
    if (confidence === 'review') {
      return 'close';
    }
    return 'ok';
  }

  /**
   * Redraw everything on the Home screen.
   * @param {{forceList?: boolean}} [options] forceList rebuilds the filament
   *   list even while one of its slot menus has focus
   */
  function renderHome(options) {
    renderDesignHeader();
    renderFilamentList(options && options.forceList === true);
    renderPrinterPill();
    renderPrintSummary();
    renderSaveLine();
    renderExportButton();
    renderReadiness();
    renderBambuddySummary();
  }

  /** Design name and "N bodies · M filaments". */
  function renderDesignHeader() {
    var title = byId('design-title');
    if (title) {
      title.textContent = state.ui.designName || 'This design';
    }
    var summary = byId('materials-summary');
    if (summary) {
      summary.textContent = pluralize(countBodies(state.materials), 'body', 'bodies') + ' · ' +
        pluralize(state.materials.length, 'filament');
    }
  }

  /**
   * One row per design filament, problems first, each with its printer slot.
   * A rebuild while a slot <select> has focus (its native menu may be open)
   * waits until focus leaves the list, unless `force` is set; focus is put
   * back on the same slot picker after the rebuild.
   * @param {boolean} [force]
   */
  function renderFilamentList(force) {
    var list = byId('filament-list');
    if (!list) {
      return;
    }
    var active = document.activeElement;
    var slotFocused = !!(active && list.contains(active) && active.classList.contains('slot-chip__select'));
    if (slotFocused && !force) {
      filamentListDeferred = true;
      return;
    }
    filamentListDeferred = false;
    var restoreFocus = keepFocus(list);
    list.textContent = '';
    var mapping = mappingByMaterial();
    var rank = { missing: 0, close: 1, manual: 2, ok: 2, unknown: 2 };
    var rows = state.materials.map(function (material) {
      var row = mapping[asString(material.material_id)];
      return { material: material, row: row, kind: matchKind(row) };
    });
    rows.sort(function (a, b) {
      var delta = rank[a.kind] - rank[b.kind];
      if (delta !== 0) {
        return delta;
      }
      return asString(a.material.display_name).localeCompare(asString(b.material.display_name));
    });
    for (var i = 0; i < rows.length; i += 1) {
      list.appendChild(createFilamentRow(rows[i].material, rows[i].row, rows[i].kind));
    }
    restoreFocus();
  }

  /**
   * Build one Home row: swatch, name, brand and body count, slot chip, badge.
   * @param {object} material design material
   * @param {object|undefined} row mapping row
   * @param {string} kind see matchKind
   * @returns {HTMLElement}
   */
  function createFilamentRow(material, row, kind) {
    var element = document.createElement('div');
    element.className = 'frow' + (kind === 'missing' ? ' frow--problem' : '');
    element.setAttribute('role', 'listitem');
    var full = asString(material.display_name) || asString(material.material_name) || 'Filament';
    var parts = splitDisplayName(full);

    element.appendChild(createSwatch(material.color_hex, full, 'swatch--row'));

    var main = document.createElement('div');
    main.className = 'frow__main';
    var name = document.createElement('div');
    name.className = 'frow__name';
    name.textContent = parts.name;
    name.title = full;
    main.appendChild(name);
    var sub = document.createElement('div');
    sub.className = 'frow__sub';
    sub.textContent = [parts.brand || asString(material.filament_type),
      pluralize(countBodies([material]), 'body', 'bodies')].filter(Boolean).join(' · ');
    var bodies = Array.isArray(material.bodies) ? material.bodies.map(asString) : [];
    if (bodies.length) {
      sub.title = 'Bodies: ' + bodies.join(', ');
    }
    main.appendChild(sub);
    element.appendChild(main);

    var arrow = document.createElement('span');
    arrow.className = 'frow__arrow';
    arrow.setAttribute('aria-hidden', 'true');
    arrow.textContent = '→';
    element.appendChild(arrow);

    element.appendChild(createSlotChip(material, row, kind));
    element.appendChild(createMatchBadge(row, kind));
    return element;
  }

  /**
   * Slot chip: shows the slot colour and name; a transparent native select on
   * top lets the user pick another slot (keyboard and screen readers included).
   * @param {object} material
   * @param {object|undefined} row
   * @param {string} kind
   * @returns {HTMLElement}
   */
  function createSlotChip(material, row, kind) {
    var chip = document.createElement('label');
    chip.className = 'slot-chip';
    var text = document.createElement('span');
    text.className = 'slot-chip__text';
    if (kind === 'unknown') {
      chip.classList.add('slot-chip--none');
      text.textContent = state.bambuddy.configured && !state.bambuddy.error ? 'Reading…' : 'No printer';
      chip.appendChild(text);
      return chip;
    }
    var slot = row && row.slot_key ? slotsByKey()[asString(row.slot_key)] : null;
    if (slot) {
      chip.appendChild(createSwatch(slot.color_hex, asString(slot.display_name), 'swatch--chip'));
      text.textContent = shortSlotLabel(slot.label || row.slot_label);
    } else {
      chip.classList.add('slot-chip--empty');
      text.textContent = 'Not loaded';
    }
    chip.appendChild(text);

    var materialName = asString(material.display_name) || asString(material.material_id);
    var select = document.createElement('select');
    select.className = 'slot-chip__select';
    select.setAttribute('aria-label', 'Printer slot for ' + materialName);
    select.setAttribute('data-material', materialName);
    select.setAttribute('data-focus-key', 'slot:' + asString(material.material_id));
    var none = document.createElement('option');
    none.value = 'none';
    none.textContent = 'Not loaded';
    select.appendChild(none);
    var slots = state.bambuddy.slots || [];
    for (var i = 0; i < slots.length; i += 1) {
      var option = document.createElement('option');
      option.value = asString(slots[i].key);
      option.textContent = shortSlotLabel(slots[i].label) + ' — ' +
        (asString(slots[i].display_name) || 'Filament');
      select.appendChild(option);
    }
    select.value = row && row.slot_key ? asString(row.slot_key) : 'none';
    select.addEventListener('change', function () {
      onSlotOverride(asString(material.material_id), materialName, select.value || 'none');
    });
    chip.appendChild(select);
    return chip;
  }

  /**
   * Badge after the slot chip: exact match, chosen by the user, or closest colour.
   * @param {object|undefined} row
   * @param {string} kind
   * @returns {HTMLElement}
   */
  function createMatchBadge(row, kind) {
    var badge = document.createElement('span');
    badge.className = 'badge';
    if (kind === 'ok') {
      badge.classList.add('badge--ok');
      badge.textContent = '✓';
      badge.title = 'Matched by type and colour';
    } else if (kind === 'manual') {
      badge.classList.add('badge--manual');
      badge.textContent = '✓';
      badge.title = 'Slot chosen by you';
    } else if (kind === 'close') {
      badge.classList.add('badge--close');
      badge.textContent = '≈';
      badge.title = asString(row && row.review_reason) ||
        'Same type, closest colour. Check before printing.';
    } else {
      badge.classList.add('badge--spacer');
      badge.textContent = '✓';
      badge.setAttribute('aria-hidden', 'true');
    }
    return badge;
  }

  /**
   * Remember a slot picked by the user and redraw.
   * @param {string} materialId
   * @param {string} materialName
   * @param {string} value slot key or 'none'
   */
  function onSlotOverride(materialId, materialName, value) {
    state.bambuddy.slotMap[materialId] = value;
    var slot = slotsByKey()[value];
    var rows = Array.isArray(state.bambuddy.mapping) ? state.bambuddy.mapping : [];
    for (var i = 0; i < rows.length; i += 1) {
      if (asString(rows[i].material_id) === materialId) {
        rows[i].slot_key = slot ? value : '';
        rows[i].slot_label = slot ? asString(slot.label) : '';
        rows[i].confidence = slot ? 'manual' : 'unmapped';
        rows[i].overridden = Boolean(slot);
      }
    }
    appendLog('Printer slot: ' + materialName + ' -> ' + (slot ? shortSlotLabel(slot.label) : 'not loaded'), 'info');
    scheduleSettingsSave();
    renderHome({ forceList: true });
  }

  /** Printer pill in the top bar. */
  function renderPrinterPill() {
    var label = byId('printer-pill-label');
    var dot = byId('printer-pill-dot');
    if (!label || !dot) {
      return;
    }
    var printer = selectedPrinter();
    var kind = 'off';
    var text = 'No printer';
    if (state.bambuddy.error) {
      kind = 'error';
      text = 'Printer offline';
    } else if (printer) {
      kind = 'ok';
      text = [asString(printer.name), asString(printer.model)].filter(Boolean).join(' · ');
    } else if (state.bambuddy.configured) {
      kind = 'warn';
      text = 'Reading printer…';
    }
    label.textContent = text;
    dot.className = 'dot' + (kind === 'ok' ? '' : ' dot--' + kind);
  }

  /** One-line summary of the Orca printer and process. */
  function renderPrintSummary() {
    var summary = byId('print-settings-summary');
    if (!summary) {
      return;
    }
    if (selectValue('target-printer') && selectValue('target-process')) {
      summary.textContent = selectedOptionLabel(byId('target-printer')) + ' · ' +
        selectedOptionLabel(byId('target-process')).replace(/\s*\(default\)$/, '').replace(/\s*@.*$/, '');
    } else if (isActionPending('get_profiles')) {
      summary.textContent = 'Loading printer profiles…';
    } else {
      summary.textContent = 'Not chosen yet';
    }
  }

  /** "Saves <file> to <folder>" under the main button. */
  function renderSaveLine() {
    var label = byId('output-file-label');
    if (!label) {
      return;
    }
    var input = byId('output-path');
    var path = input ? input.value.trim() : '';
    if (!path) {
      label.textContent = 'Choose where to save the 3MF';
      label.removeAttribute('title');
      return;
    }
    var parts = path.split(/[\\/]/);
    var file = parts.pop() || path;
    var folder = parts.pop() || '';
    label.textContent = 'Saves ' + file + (folder ? ' to ' + folder : '');
    label.title = path;
  }

  /** The main button opens Orca unless that is turned off in Advanced. */
  function renderExportButton() {
    var button = byId('export-btn');
    var orcaEnabled = byId('orca-enabled');
    if (button) {
      button.textContent = orcaEnabled && !orcaEnabled.checked ? 'Export 3MF' : 'Send to Orca';
    }
  }

  /**
   * Focus the slot picker of one Home row.
   * @param {string} materialName
   * @returns {function()}
   */
  function focusSlotPicker(materialName) {
    return function () {
      var selects = document.querySelectorAll('#filament-list .slot-chip__select');
      for (var i = 0; i < selects.length; i += 1) {
        if (selects[i].getAttribute('data-material') === materialName) {
          selects[i].focus();
          if (typeof selects[i].showPicker === 'function') {
            try {
              selects[i].showPicker();
            } catch (err) {
              /* Older web views open the menu on the next click instead. */
            }
          }
          return;
        }
      }
    };
  }

  /**
   * Ready check above the main button: "Ready to send", or only the items that
   * need attention, each with one fix button. Warnings never block sending.
   */
  function renderReadiness() {
    var container = byId('export-readiness');
    if (!container) {
      return;
    }
    container.textContent = '';
    var printer = selectedPrinter();
    var printerName = printer ? asString(printer.name) : 'the printer';
    var fixes = [];
    if (state.connectionReady && state.materials.length === 0) {
      fixes.push({ text: 'No filament is assigned in this design yet.', action: 'Add filaments', run: openAddFilaments });
    }
    if (state.bambuddy.error) {
      fixes.push({
        text: 'Bambuddy did not answer.',
        action: 'Retry',
        run: function () { requestBambuddyContext({ silent: false }); }
      });
    }
    var mapping = mappingByMaterial();
    var slots = slotsByKey();
    for (var i = 0; i < state.materials.length; i += 1) {
      var material = state.materials[i];
      var full = asString(material.display_name) || asString(material.material_id);
      var row = mapping[asString(material.material_id)];
      var kind = matchKind(row);
      var name = splitDisplayName(full).name;
      if (kind === 'missing') {
        fixes.push({ text: name + ' is not loaded on ' + printerName + '.', action: 'Pick a slot', run: focusSlotPicker(full) });
      } else if (kind === 'close') {
        var slot = slots[asString(row.slot_key)];
        fixes.push({
          text: name + ': closest is ' + shortSlotLabel(row.slot_label) +
            (slot && slot.display_name ? ' (' + splitDisplayName(slot.display_name).name + ')' : '') + '.',
          action: 'Check',
          run: focusSlotPicker(full)
        });
      }
    }
    if (state.connectionReady && !(selectValue('target-printer') && selectValue('target-process')) &&
        !isActionPending('get_profiles')) {
      fixes.push({
        text: 'Choose the printer and process for Orca.',
        action: 'Choose',
        run: function () { toggleCardBody('print-settings-toggle', true); }
      });
    }

    if (!fixes.length) {
      var ready = document.createElement('div');
      ready.className = 'ready-line';
      var dot = document.createElement('span');
      dot.className = 'dot';
      dot.setAttribute('aria-hidden', 'true');
      ready.appendChild(dot);
      ready.appendChild(document.createTextNode(state.ui.exported ? 'Sent. Ready to send again' : 'Ready to send'));
      container.appendChild(ready);
      if (!state.bambuddy.configured && state.materials.length > 0) {
        var hint = document.createElement('p');
        hint.className = 'faint';
        hint.appendChild(document.createTextNode('Printer slots are not checked. '));
        var connect = document.createElement('button');
        connect.type = 'button';
        connect.className = 'link link--small';
        connect.textContent = 'Connect printer';
        connect.addEventListener('click', openBambuddySetup);
        hint.appendChild(connect);
        container.appendChild(hint);
      }
      return;
    }

    var box = document.createElement('div');
    box.className = 'fixes';
    var title = document.createElement('p');
    title.className = 'fixes__title';
    title.textContent = pluralize(fixes.length, 'thing') + ' to check';
    box.appendChild(title);
    for (var j = 0; j < fixes.length; j += 1) {
      var item = document.createElement('div');
      item.className = 'fix';
      var label = document.createElement('span');
      label.className = 'fix__text';
      label.textContent = fixes[j].text;
      item.appendChild(label);
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn--ghost btn--small';
      button.textContent = fixes[j].action;
      button.addEventListener('click', fixes[j].run);
      item.appendChild(button);
      box.appendChild(item);
    }
    container.appendChild(box);
  }

  /**
   * Host part of a URL for status lines.
   * @param {string} url
   * @returns {string}
   */
  function hostOf(url) {
    return asString(url).replace(/^https?:\/\//i, '').replace(/\/+$/, '');
  }

  /** Bambuddy card in Settings: status dot and one summary line. */
  function renderBambuddySummary() {
    var dot = byId('bambuddy-dot');
    var summary = byId('bambuddy-summary');
    if (!dot || !summary) {
      return;
    }
    var kind = 'off';
    var text = 'Not connected';
    if (state.bambuddy.error) {
      kind = 'error';
      text = 'Not reachable: ' + state.bambuddy.error;
    } else if (state.bambuddy.loaded) {
      kind = 'ok';
      text = [hostOf(state.bambuddy.url), pluralize(state.bambuddy.printers.length, 'printer'),
        state.bambuddy.keySet ? 'key saved' : ''].filter(Boolean).join(' · ');
    } else if (state.bambuddy.configured) {
      kind = 'warn';
      text = hostOf(state.bambuddy.url) + ' · connecting…';
    }
    summary.textContent = text;
    dot.className = 'dot' + (kind === 'ok' ? '' : ' dot--' + kind);
  }

  /** OrcaSlicer card in Settings. */
  function renderSlicerSummary() {
    var input = byId('orca-path');
    var dot = byId('slicer-dot');
    var summary = byId('slicer-summary');
    if (!dot || !summary) {
      return;
    }
    var path = input ? input.value.trim() : '';
    if (!path) {
      summary.textContent = 'Not found. Choose the OrcaSlicer app.';
      dot.className = 'dot dot--warn';
      return;
    }
    var app = /^(.*?)[\\/]([^\\/]+\.app)(?:[\\/]|$)/i.exec(path);
    summary.textContent = app ? 'Found ' + app[2] + ' in ' + (app[1] || '/') : 'Found ' + path.split(/[\\/]/).pop();
    summary.title = path;
    dot.className = 'dot';
  }

  /** Connect from the welcome screen: copy the fields into Settings and connect. */
  function onWelcomeConnect() {
    var url = byId('welcome-url');
    var key = byId('welcome-key');
    var urlValue = url ? url.value.trim() : '';
    if (!urlValue) {
      setStatus(byId('welcome-status'), 'Enter the Bambuddy address.', 'warning');
      return;
    }
    var settingsUrl = byId('bambuddy-url');
    if (settingsUrl) {
      settingsUrl.value = urlValue;
    }
    var settingsKey = byId('bambuddy-api-key');
    if (settingsKey && key && key.value.trim()) {
      settingsKey.value = key.value.trim();
    }
    requestBambuddyContext({
      silent: false,
      explicit: true,
      statusEl: byId('welcome-status'),
      button: byId('welcome-connect-btn')
    });
  }

  /** Wire navigation, tabs, card toggles, the welcome screen and auto-refresh. */
  function bindScreens() {
    var backButtons = document.querySelectorAll('[data-screen-back]');
    for (var i = 0; i < backButtons.length; i += 1) {
      backButtons[i].addEventListener('click', function () { showScreen('home'); });
    }
    var tabs = document.querySelectorAll('[data-tab]');
    for (var j = 0; j < tabs.length; j += 1) {
      (function (tab) {
        tab.addEventListener('click', function () { setAddTab(tab.getAttribute('data-tab')); });
        tab.addEventListener('keydown', onTabKeydown);
      })(tabs[j]);
    }
    var filamentList = byId('filament-list');
    if (filamentList) {
      /* Run a Home rebuild that waited for a slot menu once focus moves on. */
      filamentList.addEventListener('focusout', function (event) {
        if (filamentListDeferred && !filamentList.contains(event.relatedTarget)) {
          window.setTimeout(function () {
            if (filamentListDeferred) {
              renderFilamentList();
              renderReadiness();
            }
          }, 0);
        }
      });
    }
    var initRetry = byId('init-retry-btn');
    if (initRetry) {
      initRetry.addEventListener('click', function () {
        if (!isActionPending('init')) {
          showInitError('');
          requestInitialData();
        }
      });
    }
    var clicks = {
      'open-settings-btn': function () { showScreen('settings'); },
      'open-add-btn': openAddFilaments,
      'printer-pill': function () {
        if (state.bambuddy.configured) {
          setAddTab('printer');
          showScreen('add');
        } else {
          openBambuddySetup();
        }
      },
      'bambuddy-setup-btn': openBambuddySetup,
      'print-settings-toggle': function () { toggleCardBody('print-settings-toggle'); },
      'bambuddy-edit-toggle': function () { toggleCardBody('bambuddy-edit-toggle'); },
      'slicer-edit-toggle': function () { toggleCardBody('slicer-edit-toggle'); },
      'welcome-connect-btn': onWelcomeConnect,
      'welcome-skip-btn': function () {
        dismissWelcome();
        showScreen('home');
      }
    };
    Object.keys(clicks).forEach(function (id) {
      var element = byId(id);
      if (element) {
        element.addEventListener('click', clicks[id]);
      }
    });
    var browserButton = byId('open-material-browser-btn');
    if (browserButton) {
      browserButton.addEventListener('click', function () {
        openMaterialBrowser(browserButton);
      });
    }
    /* Materials can change in Fusion while the palette is open. Fusion pushes
       changes after each command; focus is a cheap extra check. */
    window.addEventListener('focus', checkDesign);
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) {
        checkDesign();
      }
    });
  }

  /**
   * Ask Fusion to open the native Physical Material browser.
   *
   * This is fire-and-forget: the native command can block the palette event
   * loop while its modal window is open, so waiting for a request reply would
   * show a false timeout.
   */
  function openMaterialBrowser(button) {
    var status = byId('materials-status');
    if (button) {
      button.disabled = true;
      window.setTimeout(function () { button.disabled = false; }, 1200);
    }
    if (!bridgeLive()) {
      setStatus(status, 'Preview: in Fusion this opens the Physical Material browser.', 'info');
      return;
    }
    try {
      adsk.fusionSendData('open_material_browser', JSON.stringify({}));
      setStatus(status, 'Assign filaments to bodies. This list updates when you close the Material Browser.', 'info');
      appendLog('Requested the Fusion Physical Material browser.', 'info');
    } catch (err) {
      setStatus(status, 'Could not open the Fusion Material Browser: ' + errorMessage(err), 'error');
    }
  }

  /**
   * `materialBrowserData`: Fusion reports a Material Browser failure (the
   * request itself is fire-and-forget).
   * @param {object} payload
   */
  function handleMaterialBrowserData(payload) {
    if (payload.ok === false) {
      setStatus(byId('materials-status'), 'Could not open the Fusion Material Browser: ' + replyError(payload), 'error');
      return;
    }
    appendLog('Opened the Fusion Material Browser.', 'info');
  }

  /* =========================================================================
   * Event binding + startup
   * ======================================================================= */

  /**
   * Attach all DOM event listeners. No inline handler attributes are used.
   */
  function bindUi() {
    var search = byId('catalog-search');
    if (search) {
      search.addEventListener('input', scheduleCatalogRender);
    }

    var categoryFilters = byId('category-filters');
    if (categoryFilters) {
      categoryFilters.addEventListener('click', onCategoryFilterClick);
    }

    var catalogList = byId('catalog-list');
    if (catalogList) {
      catalogList.addEventListener('click', onCatalogListClick);
      catalogList.addEventListener('change', function (event) {
        var target = event.target;
        if (!target || target.type !== 'checkbox') {
          return;
        }
        var id = asString(target.value);
        if (target.checked) {
          state.selectedIds.add(id);
        } else {
          state.selectedIds.delete(id);
        }
        updateSelectionUi();
      });
    }

    var selectAll = byId('select-all-btn');
    if (selectAll) {
      selectAll.addEventListener('click', selectAllVisible);
    }

    var selectNoneButton = byId('select-none-btn');
    if (selectNoneButton) {
      selectNoneButton.addEventListener('click', selectNone);
    }

    var importButton = byId('import-btn');
    if (importButton) {
      importButton.addEventListener('click', onImportClick);
    }

    var createLibraryButton = byId('create-library-btn');
    if (createLibraryButton) {
      createLibraryButton.addEventListener('click', onCreateLibraryClick);
    }

    var autoLibraryUpdate = byId('auto-library-update');
    if (autoLibraryUpdate) {
      autoLibraryUpdate.addEventListener('change', scheduleSettingsSave);
    }

    var refreshMaterials = byId('refresh-materials-btn');
    if (refreshMaterials) {
      refreshMaterials.addEventListener('click', onRefreshMaterialsClick);
    }

    var exportButton = byId('export-btn');
    if (exportButton) {
      exportButton.addEventListener('click', onExportClick);
    }

    var browseExportPath = byId('browse-export-path-btn');
    if (browseExportPath) {
      browseExportPath.addEventListener('click', function () {
        onBrowseExportPathClick(byId('export-status'), browseExportPath);
      });
    }

    var browseSlicerPath = byId('browse-slicer-path-btn');
    if (browseSlicerPath) {
      browseSlicerPath.addEventListener('click', onBrowseSlicerPathClick);
    }

    /* Target printer profile controls. */
    var prepareEnabled = byId('prepare-enabled');
    if (prepareEnabled) {
      prepareEnabled.addEventListener('change', function () {
        syncPrepareUi();
        scheduleSettingsSave();
      });
    }

    var targetVendor = byId('target-vendor');
    if (targetVendor) {
      targetVendor.addEventListener('change', onTargetVendorChange);
    }

    var targetPrinter = byId('target-printer');
    if (targetPrinter) {
      targetPrinter.addEventListener('change', onTargetPrinterChange);
    }

    var targetProcess = byId('target-process');
    if (targetProcess) {
      targetProcess.addEventListener('change', function () {
        renderPrintSummary();
        renderReadiness();
        scheduleSettingsSave();
      });
    }

    var refreshProfiles = byId('refresh-profiles-btn');
    if (refreshProfiles) {
      refreshProfiles.addEventListener('click', onRefreshProfilesClick);
    }

    var importProfiles = byId('import-profiles-btn');
    if (importProfiles) {
      importProfiles.addEventListener('click', onImportProfilesClick);
    }

    /* Open Filament Database controls. */
    var ofdSearch = byId('ofd-search-btn');
    if (ofdSearch) {
      ofdSearch.addEventListener('click', onOfdSearchClick);
    }

    var ofdQuery = byId('ofd-query');
    if (ofdQuery) {
      ofdQuery.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') {
          event.preventDefault();
          onOfdSearchClick();
        }
      });
    }

    var ofdImport = byId('ofd-import-btn');
    if (ofdImport) {
      ofdImport.addEventListener('click', onOfdImportClick);
    }

    var ofdResults = byId('ofd-results');
    if (ofdResults) {
      ofdResults.addEventListener('change', function (event) {
        var target = event.target;
        if (!target || target.type !== 'checkbox') {
          return;
        }
        var key = asString(target.value);
        if (target.checked) {
          state.openfilament.selectedKeys.add(key);
        } else {
          state.openfilament.selectedKeys.delete(key);
        }
        updateOfdSelectionUi();
      });
    }

    var diagnosticsButton = byId('diagnostics-btn');
    if (diagnosticsButton) {
      diagnosticsButton.addEventListener('click', onDiagnosticsClick);
    }

    var outputPath = byId('output-path');
    if (outputPath) {
      outputPath.addEventListener('input', function () {
        renderSaveLine();
        scheduleSettingsSave();
      });
    }

    var settingsBrowse = byId('settings-browse-export-btn');
    if (settingsBrowse) {
      settingsBrowse.addEventListener('click', function () {
        onBrowseExportPathClick(byId('settings-export-status'), settingsBrowse);
      });
    }

    var writeProject = byId('write-project-settings');
    if (writeProject) {
      writeProject.addEventListener('change', scheduleSettingsSave);
    }

    var orcaEnabled = byId('orca-enabled');
    if (orcaEnabled) {
      orcaEnabled.addEventListener('change', function () {
        renderExportButton();
        scheduleSettingsSave();
      });
    }

    var orcaPath = byId('orca-path');
    if (orcaPath) {
      orcaPath.addEventListener('input', function () {
        renderSlicerSummary();
        scheduleSettingsSave();
      });
    }

    var orcaProfileDir = byId('orca-profile-dir');
    if (orcaProfileDir) {
      orcaProfileDir.addEventListener('input', scheduleSettingsSave);
    }

    var bambuddyConnect = byId('bambuddy-connect-btn');
    if (bambuddyConnect) {
      bambuddyConnect.addEventListener('click', function () {
        requestBambuddyContext({
          silent: false,
          explicit: true,
          statusEl: byId('setup-bambuddy-status'),
          button: bambuddyConnect
        });
      });
    }
    var bambuddyRemoveKey = byId('bambuddy-remove-key-btn');
    if (bambuddyRemoveKey) {
      bambuddyRemoveKey.addEventListener('click', onRemoveBambuddyKeyClick);
    }
    var bambuddyVerifyTls = byId('bambuddy-verify-tls');
    if (bambuddyVerifyTls) {
      bambuddyVerifyTls.addEventListener('change', scheduleSettingsSave);
    }
    var bambuddyRefresh = byId('bambuddy-refresh-btn');
    if (bambuddyRefresh) {
      bambuddyRefresh.addEventListener('click', function () {
        requestBambuddyContext({ silent: false });
      });
    }
    var bambuddyImport = byId('bambuddy-import-btn');
    if (bambuddyImport) {
      bambuddyImport.addEventListener('click', onBambuddyImportClick);
    }
    var bambuddySlots = byId('bambuddy-slots-list');
    if (bambuddySlots) {
      bambuddySlots.addEventListener('change', function (event) {
        var target = event.target;
        if (!target || target.type !== 'checkbox') {
          return;
        }
        if (target.checked) {
          state.bambuddy.importKeys.add(asString(target.value));
        } else {
          state.bambuddy.importKeys.delete(asString(target.value));
        }
        updateBambuddyImportButton();
      });
    }
    var bambuddyPrinter = byId('bambuddy-printer');
    if (bambuddyPrinter) {
      bambuddyPrinter.addEventListener('change', function () {
        state.bambuddy.selectedPrinterId = Number(bambuddyPrinter.value) || 0;
        requestBambuddyContext({ silent: false });
        scheduleSettingsSave();
      });
    }

    var refreshLog = byId('refresh-log-btn');
    if (refreshLog) {
      refreshLog.addEventListener('click', onRefreshLogClick);
    }

    var clearLog = byId('clear-log-btn');
    if (clearLog) {
      clearLog.addEventListener('click', function () {
        /* Clear only the visible panel; the Fusion-log watermark is kept so
           later replies only append genuinely new lines. */
        clearLogDisplay();
      });
    }
  }

  /**
   * "Import selected into Fusion" click handler.
   */
  function onImportClick() {
    var ids = Array.from(state.selectedIds);
    if (ids.length === 0) {
      setStatus(byId('catalog-status'), 'Select at least one filament first.', 'warning');
      var search = byId('catalog-search');
      if (search) {
        search.focus();
      }
      return;
    }
    appendLog('Requested import of ' + pluralize(ids.length, 'filament') +
      ' (material + colored appearance each).', 'info');
    request('import_selected', { ids: ids }, {
      statusEl: byId('catalog-status'),
      pendingText: 'Importing ' + pluralize(ids.length, 'filament') + ' into the design...',
      button: byId('import-btn')
    });
  }

  /**
   * "Refresh from design" click handler for automatically detected materials.
   */
  function onRefreshMaterialsClick() {
    request('refresh_materials', {}, {
      statusEl: byId('materials-status'),
      pendingText: 'Scanning the design for assigned materials...',
      button: byId('refresh-materials-btn')
    });
  }

  /**
   * "Browse..." click handler for the 3MF output path. Sends the current input
   * value so Fusion's native save dialog can start in the right folder. The
   * dialog is interactive, so this request uses a much longer timeout.
   * @param {HTMLElement|null} [statusEl] status line of the screen that asked
   * @param {HTMLElement|null} [button] button disabled while the dialog is open
   */
  function onBrowseExportPathClick(statusEl, button) {
    if (isActionPending('browse_export_path')) {
      return;
    }
    var input = byId('output-path');
    var currentPath = input ? input.value.trim() : '';
    request('browse_export_path', { current_path: currentPath }, {
      statusEl: statusEl || byId('export-status'),
      pendingText: 'Waiting for the native file dialog...',
      button: button || null,
      timeoutMs: BROWSE_TIMEOUT_MS
    });
  }

  /**
   * "Browse..." click handler for the slicer executable path.
   */
  function onBrowseSlicerPathClick() {
    var input = byId('orca-path');
    var currentPath = input ? input.value.trim() : '';
    request('browse_slicer_path', { current_path: currentPath }, {
      statusEl: byId('slicer-status'),
      pendingText: 'Waiting for the native file dialog...',
      button: byId('browse-slicer-path-btn'),
      timeoutMs: BROWSE_TIMEOUT_MS
    });
  }

  /**
   * "Run diagnostics" click handler.
   */
  function onDiagnosticsClick() {
    request('run_diagnostics', {}, {
      statusEl: byId('diagnostics-status'),
      pendingText: 'Running diagnostics...',
      button: byId('diagnostics-btn')
    });
  }

  /**
   * "Refresh" click handler for the log panel (`get_log`).
   */
  function onRefreshLogClick() {
    request('get_log', {}, {
      button: byId('refresh-log-btn')
    });
  }

  /**
   * Send the initial `init` request to Fusion (or the simulated demo reply).
   */
  function requestInitialData() {
    request('init', {}, {
      statusEl: byId('connection-status'),
      pendingText: state.demoMode ? 'Loading demo data...' : 'Connecting to Fusion...',
      onFail: showInitError
    });
  }

  /**
   * Startup: wait for the Fusion palette bridge, then load data.
   */
  function initPalette() {
    bindUi();
    bindScreens();
    updateLogCount();
    updateSelectionUi();
    updateOfdSelectionUi();
    renderOfdResults();
    syncPrepareUi();
    watchContentHeight();
    uiReady = true;
    flushEarlyMessages();

    if (hasFusionBridge()) {
      requestInitialData();
      return;
    }

    /* In a plain browser the bridge never appears: fall back to demo mode.
       In Fusion the bridge can be wired a moment after DOMContentLoaded, so
       give it a few short retries first. */
    var attempts = 0;
    var waitForBridge = function () {
      if (hasFusionBridge()) {
        requestInitialData();
        return;
      }
      attempts += 1;
      if (attempts >= BRIDGE_RETRY_ATTEMPTS) {
        setupDemoMode();
        requestInitialData();
        return;
      }
      setConnectionState('info', 'Looking for the Fusion bridge...');
      window.setTimeout(waitForBridge, BRIDGE_RETRY_DELAY_MS);
    };
    waitForBridge();
  }

  /* Deferred scripts run before DOMContentLoaded; wait for the full DOM so all
     element references are guaranteed to exist. */
  if (document.readyState === 'complete') {
    initPalette();
  } else {
    document.addEventListener('DOMContentLoaded', initPalette, { once: true });
  }
})();
