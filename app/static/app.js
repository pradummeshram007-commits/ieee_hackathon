/* KisanRoute demo frontend.
 *
 * No framework, no build step. Leaflet draws the routes, Chart.js draws the
 * headline bars, everything else is plain DOM.
 *
 * Field names below come from app/models.py and were confirmed against live
 * responses, so three of them are worth calling out because they are easy to
 * get wrong:
 *
 *   - POST /api/plan     returns  {optimized, baseline, comparison, dataset}
 *   - POST /api/disrupt  returns  {plan, baseline, comparison, diff, ...}
 *     i.e. the new optimised plan arrives as `plan`, NOT as `optimized`.
 *   - Stop carries `arrival_clock` pre-formatted, but `spoilage_deadline_min`
 *     is raw minutes-from-plan-start, so the clock formatter below is needed
 *     for deadlines. Plan start comes from summary.plan_start_label.
 */
(function () {
  'use strict';

  /* ------------------------------------------------------------------ */
  /* Constants                                                          */
  /* ------------------------------------------------------------------ */
  var PLAN_TIME_LIMIT = 8;          // matches the server default
  var NAGPUR = [21.1458, 79.0882];

  var CROP_COLOURS = {
    tomato:       '#c92a2a',
    leafy_greens: '#2f9e44',
    onion:        '#9c36b5',
    orange:       '#e8590c',
    banana:       '#b08900',
    potato:       '#6d4c41'
  };
  var CROP_FALLBACK = ['#0b5394', '#00838f', '#5f3dc4', '#ad1457', '#4e6813'];
  var TRUCK_COLOURS = ['#1971c2', '#0c8599', '#5f3dc4', '#c2255c', '#2b8a3e', '#b35c00'];
  var VIOLATION = '#ff1f1f';
  var UNSERVED_GREY = '#78847d';

  var METRIC_META = {
    total_km:                 ['Total distance',                 'every truck, including the drive home'],
    total_cost:               ['Transport cost',                 'distance x each truck’s INR/km'],
    farmers_served:           ['Farmers served',                 'lots collected'],
    kg_moved:                 ['Produce moved',                  'delivered to a buyer'],
    kg_spoiled:               ['Produce spoiled',                'picked up after its deadline'],
    spoilage_risk_score:      ['Spoilage risk',                  'kg x hours past deadline'],
    deadlines_met_pct:        ['Pickups inside deadline',        ''],
    buyer_windows_met_pct:    ['Deliveries inside buyer window', ''],
    kg_crop_mismatch:         ['Produce to the wrong buyer',     'buyer does not want that crop'],
    capacity_utilisation_pct: ['Fleet utilisation',              'of 5,200 kg of trucks'],
    km_per_tonne:             ['Distance per tonne',             ''],
    cost_per_kg:              ['Transport cost per kg',          ''],
    trucks_used:              ['Trucks used',                    ''],
    kg_sellable:              ['Sellable produce delivered',     'on time, to a buyer at the farmer\u2019s price'],
    km_per_sellable_tonne:    ['Distance per sellable tonne',    'the like-for-like efficiency number'],
    cost_per_sellable_kg:     ['Cost per sellable kg',           ''],
    farmer_payout_total:      ['Farmer net payout',              'after transport share'],
    farmer_gain_vs_middleman: ['Gain vs middleman',              'above the 0.65x base price']
  };

  var CHART_METRICS = [
    { key: 'total_km',            label: ['Distance', '↓ better'] },
    { key: 'total_cost',          label: ['Transport cost', '↓ better'] },
    { key: 'kg_spoiled',          label: ['Produce spoiled', '↓ better'] },
    { key: 'farmer_payout_total', label: ['Farmer payout', '↑ better'] }
  ];

  /* ------------------------------------------------------------------ */
  /* State                                                              */
  /* ------------------------------------------------------------------ */
  var state = {
    summary: null,
    crops: {},
    farmers: [],
    buyers: [],
    trucks: [],
    farmerById: {},
    buyerById: {},
    optimized: null,
    baseline: null,
    comparison: null,
    diff: null,
    mode: 'optimized',
    planStartMin: 360,
    kgRange: [0, 1],
    cropColour: {},
    truckColour: {},
    sort: { key: 'gain_vs_middleman', dir: -1 },
    changedFarmers: {},
    changedTrucks: {},
    chart: null,
    fitted: false,
    busy: false
  };

  /* ------------------------------------------------------------------ */
  /* Formatting                                                         */
  /* ------------------------------------------------------------------ */
  /* en-IN gives the Indian digit grouping the brief asks for: 1,21,989.
     Formatters are memoised per decimal count rather than held in a fixed
     0/1/2 map, so a caller asking for 3 decimals (the greedy planner's solve
     time) gets one instead of a TypeError. */
  var NF_CACHE = {};

  function numberFormat(d) {
    var digits = Math.max(0, Math.min(20, isFinite(d) ? Math.floor(d) : 0));
    if (!NF_CACHE[digits]) {
      NF_CACHE[digits] = new Intl.NumberFormat('en-IN', {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits
      });
    }
    return NF_CACHE[digits];
  }

  function fmtNum(v, d) {
    if (v === null || v === undefined || v === '' || (typeof v === 'number' && !isFinite(v))) {
      return '—';
    }
    return numberFormat(d || 0).format(Number(v));
  }
  function fmtINR(v, d) { return v === null || v === undefined ? '—' : '₹' + fmtNum(v, d); }
  function fmtKg(v) { return fmtNum(v, 0) + ' kg'; }
  function fmtPct(v, d) { return v === null || v === undefined ? '—' : fmtNum(v, d === undefined ? 1 : d) + '%'; }

  function fmtSigned(v, d, unit) {
    if (v === null || v === undefined) return '—';
    var sign = v > 0 ? '+' : v < 0 ? '−' : '';
    return sign + fmtNum(Math.abs(v), d) + (unit || '');
  }

  /** Minutes-from-plan-start as a wall clock, e.g. 90 -> "07:30". */
  function clock(min) {
    if (min === null || min === undefined) return '—';
    var total = state.planStartMin + Math.round(min);
    var day = Math.floor(total / 1440);
    var rest = ((total % 1440) + 1440) % 1440;
    var hh = String(Math.floor(rest / 60)).padStart(2, '0');
    var mm = String(rest % 60).padStart(2, '0');
    return hh + ':' + mm + (day > 0 ? ' +' + day + 'd' : '');
  }

  /** Format a metric value for its unit, as the API reports it. */
  function fmtMetric(value, unit) {
    switch (unit) {
      case 'km':            return fmtNum(value, 1) + ' km';
      case 'INR':           return fmtINR(value, 0);
      case 'INR/kg':        return fmtINR(value, 2);
      case 'kg':            return fmtKg(value);
      case '%':             return fmtPct(value, 1);
      case 'km/t':          return fmtNum(value, 1) + ' km/t';
      case 'kg-hours late': return fmtNum(value, 1);
      case 'farmers':       return fmtNum(value, 0);
      case 'trucks':        return fmtNum(value, 0);
      default:              return fmtNum(value, 2);
    }
  }

  function titleCase(s) {
    return String(s).replace(/_/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
  }
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* ------------------------------------------------------------------ */
  /* DOM helpers                                                        */
  /* ------------------------------------------------------------------ */
  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function td(text, cls) { return el('td', cls, text); }
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

  /* ------------------------------------------------------------------ */
  /* Colours                                                            */
  /* ------------------------------------------------------------------ */
  function cropColour(crop) {
    if (!crop) return UNSERVED_GREY;
    if (state.cropColour[crop]) return state.cropColour[crop];
    var i = Object.keys(state.cropColour).length;
    state.cropColour[crop] = CROP_COLOURS[crop] || CROP_FALLBACK[i % CROP_FALLBACK.length];
    return state.cropColour[crop];
  }
  function truckColour(id) {
    if (!state.truckColour[id]) {
      var i = Object.keys(state.truckColour).length;
      state.truckColour[id] = TRUCK_COLOURS[i % TRUCK_COLOURS.length];
    }
    return state.truckColour[id];
  }

  /* ------------------------------------------------------------------ */
  /* API                                                                */
  /* ------------------------------------------------------------------ */
  function api(path, body) {
    var opts = body === undefined
      ? { method: 'GET', headers: { accept: 'application/json' } }
      : {
          method: 'POST',
          headers: { 'content-type': 'application/json', accept: 'application/json' },
          body: JSON.stringify(body)
        };

    // Every /api route needs a bearer token. The shell owns the session; the
    // dashboard just reads the token it stored.
    try {
      var token = window.localStorage.getItem('kisanroute.token');
      if (token) opts.headers.Authorization = 'Bearer ' + token;
    } catch (_) { /* private browsing: let the request 401 and surface that */ }

    return fetch(path, opts).catch(function (e) {
      throw new Error('Cannot reach the KisanRoute API — is uvicorn still running? (' +
        (e && e.message ? e.message : e) + ')');
    }).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (_) { /* not JSON */ }
        if (!res.ok) {
          var detail = data && data.detail;
          var msg;
          if (typeof detail === 'string') msg = detail;
          else if (Array.isArray(detail)) {
            msg = detail.map(function (d) { return d.msg || JSON.stringify(d); }).join('; ');
          } else msg = text || res.statusText;
          throw new Error('HTTP ' + res.status + ' from ' + path + ' — ' + msg);
        }
        return data;
      });
    });
  }

  var retryHandler = null;
  function showError(message, retry) {
    retryHandler = retry || null;
    $('error-text').textContent = message;
    $('error-retry').hidden = !retryHandler;
    $('error-banner').hidden = false;
  }
  function hideError() { $('error-banner').hidden = true; retryHandler = null; }

  /* ------------------------------------------------------------------ */
  /* Progress                                                           */
  /* ------------------------------------------------------------------ */
  var progTimer = null, progStart = 0;

  function startProgress(label, expectedSeconds, hint) {
    progStart = (window.performance && performance.now) ? performance.now() : Date.now();
    $('solving-label').textContent = label;
    $('solving-hint').textContent = hint || 'OR-Tools guided local search — ' +
      expectedSeconds + ' second budget';
    $('solving-clock').textContent = '0.0s';
    $('solving-fill').style.width = '3%';
    $('solving').hidden = false;
    if (progTimer) clearInterval(progTimer);
    progTimer = setInterval(function () {
      var now = (window.performance && performance.now) ? performance.now() : Date.now();
      var t = (now - progStart) / 1000;
      $('solving-clock').textContent = t.toFixed(1) + 's';
      // No real progress signal exists, so ease asymptotically toward 96%:
      // honest about "still working" without ever claiming to be finished.
      var frac = 1 - Math.exp(-t / (Math.max(0.5, expectedSeconds) * 0.45));
      $('solving-fill').style.width = (3 + frac * 93).toFixed(1) + '%';
      if (state.busy) $('actionbar-busy-text').textContent = 'Re-planning… ' + t.toFixed(1) + 's';
    }, 100);
  }

  function stopProgress() {
    if (progTimer) { clearInterval(progTimer); progTimer = null; }
    $('solving-fill').style.width = '100%';
    setTimeout(function () {
      $('solving').hidden = true;
      $('solving-fill').style.width = '0%';
    }, 400);
  }

  function setBusy(on, label) {
    state.busy = on;
    $('actionbar-busy').hidden = !on;
    $('actionbar-busy-text').textContent = label || 'Re-planning…';
    ['btn-truck', 'btn-buyer', 'btn-delay', 'btn-reset'].forEach(function (id) {
      $(id).disabled = on;
    });
  }

  /* ------------------------------------------------------------------ */
  /* Data intake                                                        */
  /* ------------------------------------------------------------------ */
  function applyDataset(ds) {
    state.summary = ds.summary || {};
    state.crops = ds.crops || {};
    state.farmers = ds.farmers || [];
    state.buyers = ds.buyers || [];
    state.trucks = ds.trucks || [];

    state.farmerById = {};
    state.farmers.forEach(function (f) { state.farmerById[f.id] = f; });
    state.buyerById = {};
    state.buyers.forEach(function (b) { state.buyerById[b.id] = b; });

    var label = String(state.summary.plan_start_label || '06:00').split(':');
    var h = parseInt(label[0], 10), m = parseInt(label[1], 10);
    state.planStartMin = (isFinite(h) ? h : 6) * 60 + (isFinite(m) ? m : 0);

    var kgs = state.farmers.map(function (f) { return f.quantity_kg; });
    state.kgRange = kgs.length ? [Math.min.apply(null, kgs), Math.max.apply(null, kgs)] : [0, 1];

    Object.keys(state.crops).sort().forEach(cropColour);
    state.trucks.forEach(function (t) { truckColour(t.id); });

    $('foot-status').textContent =
      'KisanRoute — ' + state.farmers.length + ' farmers, ' + state.buyers.length +
      ' buyers, ' + state.trucks.length + ' trucks, ' +
      fmtKg(state.summary.total_supply_kg) + ' listed against ' +
      fmtKg(state.summary.total_capacity_kg) + ' of fleet (' +
      fmtPct(state.summary.supply_vs_capacity_pct) + ')' +
      (state.summary.seed !== undefined ? ' · seed ' + state.summary.seed : '');
  }

  /** Both /api/plan and /api/disrupt feed this; they name the plan differently. */
  function applyPlan(payload) {
    if (payload.optimized !== undefined && payload.optimized !== null) {
      state.optimized = payload.optimized;
    } else if (payload.plan) {
      state.optimized = payload.plan;             // /api/disrupt shape
    }
    if (payload.baseline) state.baseline = payload.baseline;
    if (payload.comparison) state.comparison = payload.comparison;
  }

  function spoilageDeadlineOf(farmer) {
    var crop = state.crops[farmer.crop];
    if (!crop) return null;
    return farmer.harvest_time + Math.round(crop.spoilage_hours * 60);
  }

  function primaryPlan() {
    return state.mode === 'baseline' ? (state.baseline || state.optimized) : state.optimized;
  }

  /* ------------------------------------------------------------------ */
  /* 1. Header tiles                                                    */
  /* ------------------------------------------------------------------ */
  function renderTiles() {
    var c = state.comparison;
    if (!c) return;
    var o = c.optimized, b = c.baseline;

    // Produce spoiled -- the headline number, green when it is zero.
    var spoiled = o.kg_spoiled;
    var tv = $('tile-spoiled-value');
    tv.innerHTML = fmtNum(spoiled, 0) + ' <small>kg</small>';
    tv.classList.toggle('is-bad', spoiled > 0);
    $('tile-spoiled-sub').textContent = b.kg_spoiled > spoiled
      ? 'the naive plan loses ' + fmtKg(b.kg_spoiled) + ' · ' +
        fmtPct(o.deadlines_met_pct, 0) + ' of pickups in time'
      : fmtPct(o.deadlines_met_pct, 0) + ' of pickups inside their deadline';

    // Extra money in farmers' hands versus the middleman.
    $('tile-payout-value').innerHTML = '₹' + fmtNum(o.farmer_gain_vs_middleman, 0);
    var payoutRows = (c.optimized_payouts && c.optimized_payouts.farmers) || [];
    var avg = c.optimized_payouts && c.optimized_payouts.avg_gain_pct;
    $('tile-payout-sub').textContent =
      'across ' + payoutRows.length + ' farmers' +
      (avg !== null && avg !== undefined ? ' · avg ' + fmtSigned(avg, 0, '%') + ' each' : '') +
      ' · net ' + fmtINR(o.farmer_payout_total, 0);

    // Distance per *sellable* tonne. Raw kilometres would compare a feasible
    // plan with one that spoils produce and misses buyer windows; this compares
    // what each plan spends to get a tonne to a paying buyer in good condition.
    var perT = b.km_per_sellable_tonne - o.km_per_sellable_tonne;
    var pct = b.km_per_sellable_tonne ? 100 * perT / b.km_per_sellable_tonne : 0;
    var kmTile = $('tile-km-value');
    kmTile.innerHTML = fmtNum(Math.abs(pct), 1) + '<small>%</small>';
    kmTile.classList.toggle('is-bad', perT < 0);
    $('tile-km-sub').textContent =
      (perT >= 0 ? 'less driving' : 'more driving') + ' per sellable tonne \u00b7 ' +
      fmtNum(o.km_per_sellable_tonne, 1) + ' vs ' + fmtNum(b.km_per_sellable_tonne, 1) +
      ' km/t \u00b7 ' + fmtKg(o.kg_sellable) + ' sellable vs ' + fmtKg(b.kg_sellable);
  }

  /* ------------------------------------------------------------------ */
  /* 2. Map                                                             */
  /* ------------------------------------------------------------------ */
  var maps = { a: null, b: null };
  var groups = { a: null, b: null };
  var mapsLinked = false;
  var syncing = false;

  function hasLeaflet() { return typeof window.L !== 'undefined'; }
  function hasChart() { return typeof window.Chart !== 'undefined'; }

  function ensureMap(key) {
    if (!hasLeaflet()) return null;
    if (maps[key]) return maps[key];
    /* zoomSnap: 0 lets fitBounds use a fractional zoom. With the default
       integer snapping, this dataset fits at 9.94 and Leaflet floors it to 9
       -- throwing away 45% of the scale and leaving the villages as a tiny
       clump in the middle of a half-empty map. zoomDelta keeps the +/- buttons
       stepping by a sensible amount. */
    var m = L.map('map-' + key, {
      zoomControl: true,
      attributionControl: true,
      zoomSnap: 0,
      zoomDelta: 0.5
    });
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 18,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    }).addTo(m);
    m.setView(NAGPUR, 9);
    groups[key] = L.layerGroup().addTo(m);
    maps[key] = m;
    linkMaps();
    return m;
  }

  /** Keep the two side-by-side maps pointing at the same place. */
  function linkMaps() {
    if (mapsLinked || !maps.a || !maps.b) return;
    mapsLinked = true;
    [['a', 'b'], ['b', 'a']].forEach(function (pair) {
      maps[pair[0]].on('move zoom', function () {
        if (syncing) return;
        syncing = true;
        maps[pair[1]].setView(maps[pair[0]].getCenter(), maps[pair[0]].getZoom(), { animate: false });
        syncing = false;
      });
    });
  }

  function radiusFor(kg) {
    var lo = state.kgRange[0], hi = state.kgRange[1];
    var t = hi > lo ? (kg - lo) / (hi - lo) : 0.5;
    return 7 + Math.sqrt(Math.max(0, t)) * 13;   // area roughly tracks kg
  }

  function popupRows(pairs) {
    var html = '<dl class="pop__rows">';
    pairs.forEach(function (p) {
      if (p[1] === null || p[1] === undefined || p[1] === '') return;
      html += '<dt>' + esc(p[0]) + '</dt><dd>' + p[1] + '</dd>';
    });
    return html + '</dl>';
  }

  function farmerPopup(farmer, stop, route) {
    var crop = state.crops[farmer.crop];
    var deadline = stop && stop.spoilage_deadline_min !== null && stop.spoilage_deadline_min !== undefined
      ? stop.spoilage_deadline_min
      : spoilageDeadlineOf(farmer);
    var html = '<div class="pop__name">' +
      '<span class="crop-dot" style="background:' + cropColour(farmer.crop) + '"></span>' +
      esc(farmer.name) + '</div>' +
      '<div class="pop__meta">' + esc(farmer.id) + ' · ' + esc(farmer.village) + '</div>';
    html += popupRows([
      ['Crop', esc(titleCase(farmer.crop)) + (crop ? ' <span class="muted">(' +
        fmtNum(crop.spoilage_hours, 0) + ' h shelf life)</span>' : '')],
      ['Quantity', fmtKg(farmer.quantity_kg)],
      ['Pickup', stop ? esc(stop.arrival_clock) +
        (stop.wait_min ? ' <span class="muted">(waited ' + stop.wait_min + ' min)</span>' : '')
        : 'not collected'],
      ['Ready from', clock(farmer.ready_from)],
      ['Spoils at', clock(deadline)],
      ['Truck', route
        ? '<span style="color:' + truckColour(route.truck_id) + '">●</span> ' +
          esc(route.truck_id) + ' → ' + esc(route.buyer_name || route.buyer_id || '')
        : '—']
    ]);
    if (stop && stop.minutes_past_deadline > 0) {
      html += '<span class="pop__flag">SPOILED — picked up ' +
        fmtNum(stop.minutes_past_deadline / 60, 1) + ' h past deadline</span>';
    }
    if (stop && stop.crop_accepted === false) {
      html += '<span class="pop__flag">WRONG BUYER — this buyer does not want ' +
        esc(farmer.crop) + '</span>';
    }
    return html;
  }

  function unservedPopup(entry) {
    var farmer = state.farmerById[entry.farmer_id];
    var html = '<div class="pop__name">' +
      '<span class="crop-dot" style="background:' + UNSERVED_GREY + '"></span>' +
      esc(entry.name) + '</div>' +
      '<div class="pop__meta">' + esc(entry.farmer_id) + ' · ' + esc(entry.village) +
      ' · no truck came</div>';
    html += popupRows([
      ['Crop', esc(titleCase(entry.crop))],
      ['Quantity', fmtKg(entry.quantity_kg)],
      ['Ready from', farmer ? clock(farmer.ready_from) : null],
      ['Spoils at', farmer ? clock(spoilageDeadlineOf(farmer)) : null],
      ['Truck', '— unserved']
    ]);
    html += '<p class="pop__why">' + esc(entry.reason) + '</p>';
    return html;
  }

  function routePopup(route) {
    var buyer = state.buyerById[route.buyer_id];
    var window_ = buyer ? clock(buyer.window_start) + '–' + clock(buyer.window_end) : '—';
    var html = '<div class="pop__name"><span style="color:' + truckColour(route.truck_id) +
      '">▬</span> ' + esc(route.truck_id) + '</div>' +
      '<div class="pop__meta">delivers to ' + esc(route.buyer_name || route.buyer_id || '') + '</div>';
    html += popupRows([
      ['Load', fmtKg(route.load_kg) + ' / ' + fmtKg(route.truck_capacity_kg) +
        ' (' + fmtPct(route.utilisation_pct, 0) + ')'],
      ['Pickups', String(route.farmer_ids.length)],
      ['Distance', fmtNum(route.distance_km, 1) + ' km'],
      ['Cost', fmtINR(route.cost, 0)],
      ['Delivers', clock(route.delivery_min) + ' <span class="muted">(window ' + window_ + ')</span>'],
      ['Back at depot', clock(route.end_min)]
    ]);
    if (!route.delivery_on_time) {
      html += '<span class="pop__flag">OUTSIDE THE BUYER’S WINDOW</span>';
    }
    if (route.deadline_misses > 0) {
      html += '<span class="pop__flag">' + route.deadline_misses +
        ' pickup(s) past the spoilage deadline</span>';
    }
    if (route.kg_rejected > 0) {
      html += '<span class="pop__flag">' + fmtKg(route.kg_rejected) +
        ' this buyer will not take</span>';
    }
    return html;
  }

  /** True when arriving at `stop` breaks a spoilage deadline or a buyer window. */
  function legBreaks(route, stop) {
    if (stop.kind === 'farmer') return stop.minutes_past_deadline > 0;
    if (stop.kind === 'buyer') return route.delivery_on_time === false;
    return false;
  }

  function drawPlan(key, plan, highlightViolations) {
    var map = ensureMap(key);
    if (!map || !plan) return;
    var group = groups[key];
    group.clearLayers();

    var bounds = [];

    /* Depots (deduped by coordinate). */
    var seenDepot = {};
    state.trucks.forEach(function (t) {
      var tag = t.depot_lat.toFixed(5) + ',' + t.depot_lng.toFixed(5);
      if (seenDepot[tag]) return;
      seenDepot[tag] = true;
      bounds.push([t.depot_lat, t.depot_lng]);
      var here = state.trucks.filter(function (x) {
        return x.depot_lat.toFixed(5) + ',' + x.depot_lng.toFixed(5) === tag;
      });
      L.marker([t.depot_lat, t.depot_lng], {
        icon: L.divIcon({
          className: '',
          html: '<div class="marker-pin marker-pin--depot" style="width:30px;height:30px">D</div>',
          iconSize: [30, 30],
          iconAnchor: [15, 15]
        }),
        zIndexOffset: 500,
        title: 'Fleet depot'
      }).bindPopup(
        '<div class="pop__name">Fleet depot</div>' +
        '<div class="pop__meta">every route starts and ends here</div>' +
        popupRows([['Trucks', here.map(function (x) {
          return esc(x.id) + ' (' + fmtKg(x.capacity_kg) + ', ' + fmtINR(x.cost_per_km, 0) + '/km)';
        }).join('<br>')]])
      ).addTo(group);
    });

    /* Routes, leg by leg so a single bad leg can be called out in red. */
    plan.routes.forEach(function (route) {
      var colour = truckColour(route.truck_id);
      var changed = !!state.changedTrucks[route.truck_id];
      var stops = route.stops || [];
      var path = [];

      for (var i = 0; i < stops.length; i++) path.push([stops[i].lat, stops[i].lng]);

      for (var j = 1; j < stops.length; j++) {
        var seg = [[stops[j - 1].lat, stops[j - 1].lng], [stops[j].lat, stops[j].lng]];
        if (highlightViolations && legBreaks(route, stops[j])) {
          // Wider red casing underneath, so the truck colour still reads.
          L.polyline(seg, {
            color: VIOLATION, weight: 11, opacity: 0.85,
            dashArray: '12 8', className: 'leg-violation', interactive: false
          }).addTo(group);
        }
        L.polyline(seg, {
          color: colour, weight: 5, opacity: 0.92,
          className: changed ? 'route-changed' : '',
          interactive: false
        }).addTo(group);
      }

      /* Invisible full-route line: hosts the permanent truck label and the
         route popup without stealing clicks from the farmer markers. */
      if (path.length > 1) {
        L.polyline(path, { color: colour, weight: 16, opacity: 0 })
          .bindTooltip(route.truck_id, {
            permanent: true, direction: 'center', className: 'route-label',
            opacity: 1, offset: [0, 0]
          })
          .bindPopup(routePopup(route))
          .addTo(group);
        var label = group.getLayers()[group.getLayers().length - 1];
        if (label.getTooltip && label.getTooltip()) {
          var t = label.getTooltip();
          if (t._container) t._container.style.color = colour;
        }
      }
    });

    /* Served farmers: filled circles, colour by crop, radius by quantity. */
    var servedIds = {};
    plan.routes.forEach(function (route) {
      (route.stops || []).forEach(function (stop) {
        if (stop.kind !== 'farmer') return;
        var farmer = state.farmerById[stop.ref_id] || {
          id: stop.ref_id, name: stop.name, village: stop.village,
          crop: stop.crop, quantity_kg: stop.quantity_kg,
          ready_from: stop.ready_from, harvest_time: 0, lat: stop.lat, lng: stop.lng
        };
        servedIds[stop.ref_id] = true;
        bounds.push([stop.lat, stop.lng]);
        var bad = stop.minutes_past_deadline > 0 || stop.crop_accepted === false;
        L.circleMarker([stop.lat, stop.lng], {
          radius: radiusFor(stop.quantity_kg),
          color: bad ? VIOLATION : '#ffffff',
          weight: bad ? 3 : 2,
          fillColor: cropColour(stop.crop),
          fillOpacity: 0.95
        }).bindPopup(farmerPopup(farmer, stop, route)).addTo(group);
      });
    });

    /* Unserved farmers: hollow grey, dashed outline. */
    (plan.unserved || []).forEach(function (entry) {
      var farmer = state.farmerById[entry.farmer_id];
      if (!farmer) return;
      bounds.push([farmer.lat, farmer.lng]);
      L.circleMarker([farmer.lat, farmer.lng], {
        radius: radiusFor(entry.quantity_kg),
        color: UNSERVED_GREY,
        weight: 3,
        dashArray: '4 3',
        fill: false,
        fillOpacity: 0
      }).bindPopup(unservedPopup(entry)).addTo(group);
    });

    /* Buyers: rotated squares, distinct from every farmer circle. */
    state.buyers.forEach(function (buyer) {
      bounds.push([buyer.lat, buyer.lng]);
      var prices = Object.keys(buyer.price_per_kg || {}).map(function (crop) {
        return esc(titleCase(crop)) + ' ' + fmtINR(buyer.price_per_kg[crop], 2) + '/kg';
      }).join('<br>');
      var delivered = plan.routes.filter(function (r) { return r.buyer_id === buyer.id; });
      L.marker([buyer.lat, buyer.lng], {
        icon: L.divIcon({
          className: '',
          html: '<div class="marker-pin marker-pin--buyer" style="width:26px;height:26px">' +
                '<span>' + esc(buyer.id) + '</span></div>',
          iconSize: [26, 26],
          iconAnchor: [13, 13]
        }),
        zIndexOffset: 400,
        title: buyer.name
      }).bindPopup(
        '<div class="pop__name">' + esc(buyer.name) + '</div>' +
        '<div class="pop__meta">' + esc(buyer.id) + ' · ' + esc(buyer.type) + '</div>' +
        popupRows([
          ['Wants', esc(buyer.crops_wanted.map(titleCase).join(', '))],
          ['Pays', prices],
          ['Demand', fmtKg(buyer.demand_kg)],
          ['Window', clock(buyer.window_start) + '–' + clock(buyer.window_end)],
          ['Incoming', delivered.length
            ? delivered.map(function (r) {
                return esc(r.truck_id) + ' · ' + fmtKg(r.load_kg) + ' at ' + clock(r.delivery_min);
              }).join('<br>')
            : 'nothing scheduled']
        ])
      ).addTo(group);
    });

    return bounds;   // caller fits once the container has a real size
  }

  function renderMapStats(nodeId, plan, label) {
    var node = $(nodeId);
    clear(node);
    if (!plan) return;
    var spoiled = 0, misses = 0, lateRoutes = 0, rejected = 0;
    plan.routes.forEach(function (r) {
      misses += r.deadline_misses;
      rejected += r.kg_rejected;
      if (!r.delivery_on_time) lateRoutes++;
      (r.stops || []).forEach(function (s) {
        if (s.kind === 'farmer' && s.minutes_past_deadline > 0) spoiled += s.quantity_kg;
      });
    });

    function chip(text, kind) {
      var c = el('span', 'mapstat' + (kind ? ' is-' + kind : ''));
      c.innerHTML = text;
      node.appendChild(c);
    }
    chip('<b>' + esc(label) + '</b>');
    chip(fmtNum(plan.total_distance_km, 1) + ' km');
    chip(fmtINR(plan.total_cost, 0));
    chip(plan.farmers_served + ' farmers · ' + fmtKg(plan.kg_moved));
    chip(fmtKg(spoiled) + ' spoiled', spoiled > 0 ? 'bad' : 'good');
    if (lateRoutes) chip(lateRoutes + (lateRoutes > 1 ? ' late deliveries' : ' late delivery'), 'bad');
    if (rejected) chip(fmtKg(rejected) + ' wrong buyer', 'bad');
    if ((plan.unserved || []).length) chip((plan.unserved || []).length + ' lots unserved');
  }

  function renderMaps() {
    var split = state.mode === 'both';
    $('maps').classList.toggle('is-split', split);
    $('mapwrap-b').hidden = !split;

    if (!hasLeaflet()) {
      var holder = $('maps');
      if (!holder.querySelector('.chart__fallback')) {
        var warn = el('div', 'chart__fallback');
        warn.textContent = 'The map library (Leaflet) could not be loaded from the CDN, ' +
          'so the map is unavailable. Every number below is still live.';
        holder.insertBefore(warn, holder.firstChild);
      }
      return;
    }

    var bounds;
    if (state.mode === 'baseline') {
      $('map-a-cap').textContent = 'Naive baseline';
      $('map-a-cap').classList.add('mapwrap__cap--bad');
      bounds = drawPlan('a', state.baseline, true);
      renderMapStats('map-a-stats', state.baseline, 'Naive baseline');
    } else {
      $('map-a-cap').textContent = 'Optimised';
      $('map-a-cap').classList.remove('mapwrap__cap--bad');
      bounds = drawPlan('a', state.optimized, false);
      renderMapStats('map-a-stats', state.optimized, 'Optimised');
    }

    if (split) {
      drawPlan('b', state.baseline, true);
      renderMapStats('map-b-stats', state.baseline, 'Naive baseline');
    }

    // The container has only just been sized (or revealed), and Leaflet picks
    // a zoom from the size it last measured -- so tell it, then fit. Fitting
    // happens once; after that the judge's own zoom is left alone across
    // re-plans, and Reset clears the flag.
    requestAnimationFrame(function () {
      if (maps.a) maps.a.invalidateSize();
      if (maps.b && split) maps.b.invalidateSize();

      if (!state.fitted && bounds && bounds.length) {
        var box = L.latLngBounds(bounds).pad(0.04);
        if (maps.a) maps.a.fitBounds(box, { padding: [26, 26] });
        if (maps.b && split) maps.b.fitBounds(box, { padding: [26, 26] });
        state.fitted = true;
      }
      if (split && maps.a && maps.b) {
        maps.b.setView(maps.a.getCenter(), maps.a.getZoom(), { animate: false });
      }
    });
  }

  function renderLegend() {
    var node = $('legend');
    clear(node);

    function group(title, items) {
      var g = el('div', 'legend__group');
      g.appendChild(el('span', 'legend__title', title));
      items.forEach(function (it) {
        var item = el('span', 'legend__item');
        var sw = el('span', 'legend__swatch' + (it.extra || ''));
        if (it.colour) sw.style.background = it.colour;
        if (it.border) sw.style.borderTopColor = it.colour;
        item.appendChild(sw);
        item.appendChild(document.createTextNode(it.text));
        g.appendChild(item);
      });
      node.appendChild(g);
    }

    group('Crops', Object.keys(state.crops).sort().map(function (crop) {
      return {
        colour: cropColour(crop),
        text: titleCase(crop) + ' · ' + fmtNum(state.crops[crop].spoilage_hours, 0) + ' h'
      };
    }));

    group('Trucks', state.trucks.map(function (t) {
      return {
        colour: truckColour(t.id),
        extra: ' legend__swatch--line',
        text: t.id + ' · ' + fmtKg(t.capacity_kg)
      };
    }));

    group('Markers', [
      { colour: '#0d1a13', extra: ' legend__swatch--square', text: 'Depot' },
      { colour: '#0b5394', extra: ' legend__swatch--diamond', text: 'Buyer' },
      { extra: ' legend__swatch--hollow', text: 'Unserved lot (size = kg)' },
      { colour: VIOLATION, extra: ' legend__swatch--dash', border: true,
        text: 'Leg that breaks a deadline or buyer window' }
    ]);
  }

  /* ------------------------------------------------------------------ */
  /* 3. Comparison table + chart                                        */
  /* ------------------------------------------------------------------ */
  function renderMetrics() {
    var c = state.comparison;
    var body = $('metrics-body');
    clear(body);
    if (!c) return;

    $('cmp-status').textContent =
      'solver ' + (state.optimized ? state.optimized.solver_status : '?') +
      ' in ' + fmtNum(state.optimized ? state.optimized.solve_seconds : 0, 2) + 's' +
      ' · greedy in ' + fmtNum(state.baseline ? state.baseline.solve_seconds : 0, 3) + 's';

    c.deltas.forEach(function (row) {
      var meta = METRIC_META[row.metric] || [titleCase(row.metric), ''];
      var tr = el('tr');
      var tie = row.delta === 0;
      tr.className = tie ? '' : (row.better ? 'row-win' : 'row-lose');

      var name = el('td', 'metric-name');
      name.appendChild(document.createTextNode(meta[0]));
      var sub = el('small', null,
        meta[1] ? meta[1] + ' · ' + row.direction.replace(/_/g, ' ')
                : row.direction.replace(/_/g, ' '));
      name.appendChild(sub);
      tr.appendChild(name);

      tr.appendChild(td(fmtMetric(row.baseline, row.unit), 'num'));
      tr.appendChild(td(fmtMetric(row.optimized, row.unit), 'num col-opt'));

      var lowerBetter = row.direction === 'lower_is_better';
      var deltaGood = tie ? null : (lowerBetter ? row.delta < 0 : row.delta > 0);
      var dcell = td(fmtSigned(row.delta, row.unit === 'INR' || row.unit === 'kg' ||
        row.unit === 'farmers' || row.unit === 'trucks' ? 0 : row.unit === 'INR/kg' ? 2 : 1), 'num');
      if (deltaGood === true) dcell.className += ' cell-good';
      if (deltaGood === false) dcell.className += ' cell-bad';
      tr.appendChild(dcell);

      var pcell = el('td', 'num');
      var pill = el('span', 'pill');
      if (row.improvement_pct === null || row.improvement_pct === undefined) {
        pill.className += ' pill--flat';
        pill.textContent = 'n/a';
        pill.title = 'The baseline value is zero, so a percentage is meaningless.';
      } else if (tie || row.improvement_pct === 0) {
        pill.className += ' pill--flat';
        pill.textContent = 'same';
      } else if (row.improvement_pct > 0) {
        pill.className += ' pill--good';
        pill.textContent = fmtSigned(row.improvement_pct, 1, '%');
      } else {
        pill.className += ' pill--bad';
        pill.textContent = fmtSigned(row.improvement_pct, 1, '%') + ' worse';
      }
      pcell.appendChild(pill);
      tr.appendChild(pcell);
      body.appendChild(tr);
    });

    renderFootnote();
  }

  /** The farmers_served row is kept visible; this explains what it costs. */
  function renderFootnote() {
    var c = state.comparison;
    var node = $('served-footnote');
    if (!c) { node.hidden = true; return; }

    function find(metric) {
      for (var i = 0; i < c.deltas.length; i++) if (c.deltas[i].metric === metric) return c.deltas[i];
      return null;
    }
    var served = find('farmers_served');
    if (!served) { node.hidden = true; return; }

    node.hidden = false;
    if (served.baseline > served.optimized) {
      var extra = served.baseline - served.optimized;
      node.innerHTML =
        '<b>About the one row the optimiser loses.</b> The naive planner serves ' +
        fmtNum(served.baseline, 0) + ' farmers against the optimiser’s ' +
        fmtNum(served.optimized, 0) + '. It buys ' + (extra === 1 ? 'that extra pickup' : 'those ' +
        extra + ' extra pickups') + ' by spoiling <b>' + fmtKg(c.baseline.kg_spoiled) +
        '</b> of produce in transit and dumping <b>' + fmtKg(c.baseline.kg_crop_mismatch) +
        '</b> on a buyer that does not want that crop' +
        (c.baseline.buyer_windows_met_pct < 100
          ? ', with only ' + fmtPct(c.baseline.buyer_windows_met_pct, 0) +
            ' of deliveries inside the buyer’s window'
          : '') +
        '. The optimiser gives up small lots instead and still moves <b>' +
        fmtKg(c.optimized.kg_moved) + '</b> — ' +
        fmtSigned(c.optimized.kg_moved - c.baseline.kg_moved, 0, ' kg') +
        ' against the baseline — with nothing spoiled. Supply is ' +
        fmtPct(state.summary ? state.summary.supply_vs_capacity_pct : 0, 0) +
        ' of fleet capacity, so some lots must be left behind by either planner.';
    } else if (served.baseline === served.optimized) {
      node.innerHTML = '<b>Both planners serve ' + fmtNum(served.optimized, 0) +
        ' farmers.</b> The difference is what happens to the produce on board: the optimiser ' +
        'spoils ' + fmtKg(c.optimized.kg_spoiled) + ' against the baseline’s ' +
        fmtKg(c.baseline.kg_spoiled) + '.';
    } else {
      node.innerHTML = '<b>The optimiser serves ' +
        fmtNum(served.optimized - served.baseline, 0) + ' more farmer(s) than the naive planner</b>' +
        ' and spoils ' + fmtKg(c.optimized.kg_spoiled) + ' against its ' +
        fmtKg(c.baseline.kg_spoiled) + '.';
    }
  }

  function renderChart() {
    var c = state.comparison;
    if (!c) return;

    if (!hasChart()) {
      var fb = $('chart-fallback');
      fb.hidden = false;
      fb.textContent = 'Chart.js could not be loaded from the CDN, so the bar chart is ' +
        'unavailable. The table to the left carries the same numbers.';
      return;
    }

    var units = {};
    c.deltas.forEach(function (d) { units[d.metric] = d.unit; });

    var labels = [], baseScaled = [], optScaled = [], rawBase = [], rawOpt = [], rawUnits = [];
    CHART_METRICS.forEach(function (m) {
      var b = c.baseline[m.key], o = c.optimized[m.key];
      var peak = Math.max(Math.abs(b), Math.abs(o));
      labels.push(m.label);
      baseScaled.push(peak ? (b / peak) * 100 : 0);
      optScaled.push(peak ? (o / peak) * 100 : 0);
      rawBase.push(b);
      rawOpt.push(o);
      rawUnits.push(units[m.key] || '');
    });

    var data = {
      labels: labels,
      datasets: [
        {
          label: 'Naive baseline',
          data: baseScaled,
          backgroundColor: 'rgba(197, 34, 31, .85)',
          borderColor: '#8c1b19',
          borderWidth: 1,
          borderRadius: 4,
          raw: rawBase
        },
        {
          label: 'Optimised',
          data: optScaled,
          backgroundColor: 'rgba(11, 122, 59, .9)',
          borderColor: '#07502a',
          borderWidth: 1,
          borderRadius: 4,
          raw: rawOpt
        }
      ]
    };

    var options = {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 500 },
      scales: {
        y: {
          beginAtZero: true,
          suggestedMax: 105,
          ticks: { callback: function (v) { return v + '%'; }, font: { size: 13 } },
          title: { display: true, text: '% of the larger value', font: { size: 13 } },
          grid: { color: 'rgba(17,22,28,.08)' }
        },
        x: { ticks: { font: { size: 13, weight: '600' } }, grid: { display: false } }
      },
      plugins: {
        legend: { labels: { font: { size: 14, weight: '700' }, boxWidth: 16 } },
        tooltip: {
          titleFont: { size: 14 },
          bodyFont: { size: 14 },
          callbacks: {
            label: function (ctx) {
              var real = ctx.dataset.raw[ctx.dataIndex];
              return ctx.dataset.label + ': ' + fmtMetric(real, rawUnits[ctx.dataIndex]) +
                '  (' + fmtNum(ctx.parsed.y, 0) + '% of peak)';
            }
          }
        }
      }
    };

    if (state.chart) {
      state.chart.data = data;
      state.chart.options = options;
      state.chart.update();
    } else {
      state.chart = new Chart($('chart').getContext('2d'), { type: 'bar', data: data, options: options });
    }
  }

  /* ------------------------------------------------------------------ */
  /* 4. Payout + unserved tables                                        */
  /* ------------------------------------------------------------------ */
  function renderPayouts() {
    var c = state.comparison;
    var body = $('payout-body'), foot = $('payout-foot');
    clear(body);
    clear(foot);
    if (!c || !c.optimized_payouts) return;

    var summary = c.optimized_payouts;
    var rows = (summary.farmers || []).slice();
    var key = state.sort.key, dir = state.sort.dir;

    rows.sort(function (x, y) {
      var a = x[key], b = y[key];
      if (a === null || a === undefined) a = -Infinity;
      if (b === null || b === undefined) b = -Infinity;
      if (typeof a === 'string' || typeof b === 'string') {
        return String(a).localeCompare(String(b)) * dir;
      }
      return (a - b) * dir;
    });

    $('payout-note').textContent = 'optimised plan · ' + rows.length + ' farmers paid · ' +
      'avg ' + (summary.avg_gain_pct === null || summary.avg_gain_pct === undefined
        ? '—' : fmtSigned(summary.avg_gain_pct, 0, '%')) + ' above middleman rates';

    rows.forEach(function (r) {
      var tr = el('tr');
      if (state.changedFarmers[r.farmer_id]) tr.className = 'row-flash';

      var who = el('td');
      var dot = el('span', 'crop-dot');
      dot.style.background = cropColour(r.crop);
      who.appendChild(dot);
      who.appendChild(document.createTextNode(r.farmer_id + ' ' + r.name));
      if (r.spoiled) {
        var sp = el('span', 'tag', 'spoiled');
        sp.style.background = VIOLATION;
        sp.style.marginLeft = '.4rem';
        who.appendChild(sp);
      }
      if (r.crop_accepted === false) {
        var wb = el('span', 'tag', 'wrong buyer');
        wb.style.background = '#9a5b00';
        wb.style.marginLeft = '.4rem';
        who.appendChild(wb);
      }
      tr.appendChild(who);

      tr.appendChild(td(titleCase(r.crop)));
      tr.appendChild(td(fmtNum(r.kg, 0), 'num'));

      var truck = el('td');
      var tdot = el('span', 'crop-dot');
      tdot.style.background = truckColour(r.truck_id);
      truck.appendChild(tdot);
      truck.appendChild(document.createTextNode(r.truck_id));
      truck.title = r.buyer_name ? 'delivers to ' + r.buyer_name : '';
      tr.appendChild(truck);

      var gross = td(fmtINR(r.gross_revenue, 0), 'num');
      gross.title = fmtINR(r.price_per_kg, 2) + '/kg × ' + fmtKg(r.kg);
      tr.appendChild(gross);

      tr.appendChild(td('−' + fmtINR(r.transport_share, 0), 'num'));
      tr.appendChild(td(fmtINR(r.net_payout, 0), 'num'));

      var mid = td(fmtINR(r.middleman_revenue, 0), 'num');
      mid.title = 'middleman pays ' + fmtINR(r.middleman_price_per_kg, 2) + '/kg × ' + fmtKg(r.kg);
      tr.appendChild(mid);

      tr.appendChild(td(fmtSigned(r.gain_vs_middleman, 0),
        'num ' + (r.gain_vs_middleman >= 0 ? 'cell-good' : 'cell-bad')));
      tr.appendChild(td(r.gain_pct === null || r.gain_pct === undefined
        ? '—' : fmtSigned(r.gain_pct, 0, '%'),
        'num ' + (r.gain_pct >= 0 ? 'cell-good' : 'cell-bad')));

      body.appendChild(tr);
    });

    var totalKg = rows.reduce(function (s, r) { return s + r.kg; }, 0);
    var ftr = el('tr');
    [
      ['TOTAL · ' + rows.length + ' farmers', ''],
      ['', ''],
      [fmtNum(totalKg, 0), 'num'],
      ['', ''],
      [fmtINR(summary.total_gross, 0), 'num'],
      ['−' + fmtINR(summary.total_transport, 0), 'num'],
      [fmtINR(summary.total_net, 0), 'num'],
      [fmtINR(summary.total_middleman, 0), 'num'],
      [fmtSigned(summary.total_gain, 0), 'num'],
      [summary.total_middleman
        ? fmtSigned(100 * summary.total_gain / summary.total_middleman, 0, '%')
        : '—', 'num']
    ].forEach(function (cell) { ftr.appendChild(td(cell[0], cell[1])); });
    foot.appendChild(ftr);

    Array.prototype.forEach.call($('payout-head').children, function (th) {
      th.classList.remove('sorted-asc', 'sorted-desc');
      if (th.dataset.key === key) th.classList.add(dir === 1 ? 'sorted-asc' : 'sorted-desc');
    });
  }

  function renderUnserved() {
    var plan = primaryPlan();
    var body = $('unserved-body');
    clear(body);
    var list = (plan && plan.unserved) || [];
    $('unserved-count').textContent = list.length
      ? '— ' + list.length + ' lots, ' +
        fmtKg(list.reduce(function (s, u) { return s + u.quantity_kg; }, 0)) + ' left on the farm' +
        ' (' + (state.mode === 'baseline' ? 'naive baseline' : 'optimised plan') + ')'
      : '— none';

    if (!list.length) {
      var tr = el('tr');
      var cell = td('Every listed lot was collected.', 'wrap');
      cell.colSpan = 5;
      tr.appendChild(cell);
      body.appendChild(tr);
      return;
    }

    list.forEach(function (u) {
      var tr = el('tr');
      if (state.changedFarmers[u.farmer_id]) tr.className = 'row-flash';
      var who = el('td');
      var dot = el('span', 'crop-dot');
      dot.style.background = cropColour(u.crop);
      who.appendChild(dot);
      who.appendChild(document.createTextNode(u.farmer_id + ' ' + u.name));
      tr.appendChild(who);
      tr.appendChild(td(u.village));
      tr.appendChild(td(titleCase(u.crop)));
      tr.appendChild(td(fmtNum(u.quantity_kg, 0), 'num'));
      tr.appendChild(td(u.reason, 'wrap'));
      body.appendChild(tr);
    });
  }

  /* ------------------------------------------------------------------ */
  /* Disruption diff                                                    */
  /* ------------------------------------------------------------------ */
  function renderDiff(response) {
    var diff = response.diff;
    $('diff-panel').hidden = false;
    $('diff-title').textContent = titleCase(response.disruption) + ' · ' + response.target_id;
    $('diff-desc').textContent = response.description;

    var cards = $('diff-deltas');
    clear(cards);
    [
      { label: 'Farmers served', before: diff.farmers_served_before, after: diff.farmers_served_after,
        delta: diff.farmers_served_delta, fmt: function (v) { return fmtNum(v, 0); }, up: true },
      { label: 'Produce moved', before: diff.kg_moved_before, after: diff.kg_moved_after,
        delta: diff.kg_moved_delta, fmt: function (v) { return fmtKg(v); }, up: true },
      { label: 'Distance', before: diff.km_before, after: diff.km_after, delta: diff.km_delta,
        fmt: function (v) { return fmtNum(v, 1) + ' km'; }, up: false },
      { label: 'Transport cost', before: diff.cost_before, after: diff.cost_after,
        delta: diff.cost_delta, fmt: function (v) { return fmtINR(v, 0); }, up: false },
      { label: 'Trucks used', before: diff.trucks_used_before, after: diff.trucks_used_after,
        delta: diff.trucks_used_after - diff.trucks_used_before,
        fmt: function (v) { return fmtNum(v, 0); }, up: false }
    ].forEach(function (c) {
      var card = el('div', 'diff-card');
      card.appendChild(el('div', 'diff-card__label', c.label));
      var good = c.delta === 0 ? null : (c.up ? c.delta > 0 : c.delta < 0);
      var value = el('div', 'diff-card__value' +
        (good === true ? ' is-good' : good === false ? ' is-bad' : ''));
      value.textContent = c.fmt(c.after);
      card.appendChild(value);
      card.appendChild(el('div', 'diff-card__sub',
        'was ' + c.fmt(c.before) + '  ·  ' + fmtSigned(c.delta, c.label === 'Distance' ? 1 : 0)));
      cards.appendChild(card);
    });

    var lists = $('diff-lists');
    clear(lists);

    function makeList(title, kind, items, describe, notes) {
      var box = el('div', 'difflist' + (kind ? ' difflist--' + kind : ''));
      box.appendChild(el('div', 'difflist__title', title + ' (' + items.length + ')'));
      (notes || []).forEach(function (n) { box.appendChild(el('div', 'difflist__note', n)); });
      if (!items.length) {
        box.appendChild(el('div', 'difflist__empty', 'None.'));
      } else {
        var ul = el('ul');
        items.forEach(function (item) {
          var li = el('li');
          li.innerHTML = describe(item);
          ul.appendChild(li);
        });
        box.appendChild(ul);
      }
      lists.appendChild(box);
    }

    makeList('Lost their pickup', 'bad', diff.newly_unserved, function (c) {
      return '<b>' + esc(c.farmer_id) + ' ' + esc(c.name) + '</b> <span class="muted">' +
        esc(titleCase(c.crop)) + ' · ' + fmtKg(c.quantity_kg) + '</span> ' +
        '<span class="muted">was on ' + esc(c.from_truck || '—') + '</span>';
    });

    makeList('Moved to another truck', 'move', diff.reassigned, function (c) {
      return '<b>' + esc(c.farmer_id) + ' ' + esc(c.name) + '</b> <span class="muted">' +
        fmtKg(c.quantity_kg) + '</span> <span style="color:' + truckColour(c.from_truck) +
        ';font-weight:750">' + esc(c.from_truck) + '</span> → <span style="color:' +
        truckColour(c.to_truck) + ';font-weight:750">' + esc(c.to_truck) + '</span>';
    });

    makeList('Picked up after all', 'good', diff.newly_served, function (c) {
      return '<b>' + esc(c.farmer_id) + ' ' + esc(c.name) + '</b> <span class="muted">' +
        esc(titleCase(c.crop)) + ' · ' + fmtKg(c.quantity_kg) + '</span> ' +
        '<span class="muted">now on ' + esc(c.to_truck || '—') + '</span>';
    });

    if ((diff.notes || []).length) {
      var noteBox = el('div', 'difflist');
      noteBox.appendChild(el('div', 'difflist__title', 'What this means'));
      diff.notes.forEach(function (n) { noteBox.appendChild(el('div', 'difflist__note', n)); });
      lists.appendChild(noteBox);
    }

    $('diff-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function trackChanges(diff) {
    state.changedFarmers = {};
    state.changedTrucks = {};
    if (!diff) return;
    [].concat(diff.reassigned || [], diff.newly_unserved || [], diff.newly_served || [])
      .forEach(function (c) {
        state.changedFarmers[c.farmer_id] = true;
        if (c.from_truck) state.changedTrucks[c.from_truck] = true;
        if (c.to_truck) state.changedTrucks[c.to_truck] = true;
      });
  }

  /* ------------------------------------------------------------------ */
  /* 5. Controls                                                        */
  /* ------------------------------------------------------------------ */
  function fillSelect(node, options, preferred) {
    var previous = node.value;
    clear(node);
    options.forEach(function (o) {
      var opt = el('option', null, o.text);
      opt.value = o.value;
      node.appendChild(opt);
    });
    var values = options.map(function (o) { return o.value; });
    if (values.indexOf(previous) >= 0) node.value = previous;
    else if (preferred && values.indexOf(preferred) >= 0) node.value = preferred;
  }

  function renderControls() {
    fillSelect($('sel-truck'), state.trucks.map(function (t) {
      return { value: t.id, text: t.id + ' · ' + fmtKg(t.capacity_kg) };
    }), 'T1');

    fillSelect($('sel-buyer'), state.buyers.map(function (b) {
      return { value: b.id, text: b.id + ' · ' + b.name };
    }), 'B1');

    var served = state.optimized ? state.optimized.routes.reduce(function (acc, r) {
      return acc.concat(r.farmer_ids);
    }, []) : [];
    var ordered = state.farmers.slice().sort(function (a, b) {
      var sa = served.indexOf(a.id) >= 0 ? 0 : 1, sb = served.indexOf(b.id) >= 0 ? 0 : 1;
      return sa - sb || a.id.localeCompare(b.id);
    });
    fillSelect($('sel-farmer'), ordered.map(function (f) {
      return {
        value: f.id,
        text: f.id + ' · ' + f.name.split(' ')[0] + ' · ' + titleCase(f.crop) +
          (served.indexOf(f.id) >= 0 ? '' : ' (unserved)')
      };
    }), served[0]);

    var truckCount = state.trucks.length, buyerCount = state.buyers.length;
    $('btn-truck').disabled = truckCount <= 1;
    $('btn-buyer').disabled = buyerCount <= 1;
    $('btn-truck').title = truckCount <= 1 ? 'Only one truck left in the fleet' : '';
    $('btn-buyer').title = buyerCount <= 1 ? 'Only one buyer left' : '';
  }

  /* ------------------------------------------------------------------ */
  /* Render everything                                                  */
  /* ------------------------------------------------------------------ */
  function renderAll() {
    renderTiles();
    renderMaps();
    renderMetrics();
    renderChart();
    renderPayouts();
    renderUnserved();
    renderControls();
  }

  /* ------------------------------------------------------------------ */
  /* Actions                                                            */
  /* ------------------------------------------------------------------ */
  function loadAll() {
    hideError();
    startProgress('Loading dataset…', 1, 'Fetching farmers, buyers and trucks');
    return api('/api/dataset').then(function (ds) {
      applyDataset(ds);
      renderLegend();
      renderControls();
      startProgress('Optimising routes…', PLAN_TIME_LIMIT);
      return api('/api/plan', { mode: 'both', time_limit_seconds: PLAN_TIME_LIMIT });
    }).then(function (payload) {
      applyPlan(payload);
      trackChanges(null);
      renderAll();
      stopProgress();
    }).catch(function (err) {
      stopProgress();
      showError(err.message, loadAll);
    });
  }

  function disrupt(body, label) {
    hideError();
    setBusy(true, 'Re-planning…');
    startProgress(label, PLAN_TIME_LIMIT, 'Applying the disruption, then re-solving from scratch');
    var saved = null;
    api('/api/disrupt', body).then(function (response) {
      saved = response;
      applyPlan(response);
      trackChanges(response.diff);
      return api('/api/dataset');
    }).then(function (ds) {
      applyDataset(ds);
      renderLegend();
      renderAll();
      renderDiff(saved);
      stopProgress();
      setBusy(false);
    }).catch(function (err) {
      stopProgress();
      setBusy(false);
      showError(err.message, null);
    });
  }

  function resetAll() {
    hideError();
    setBusy(true, 'Re-seeding…');
    startProgress('Re-seeding the dataset…', PLAN_TIME_LIMIT,
      'Rebuilding the seeded dataset, then re-solving');
    api('/api/reset', {}).then(function () {
      $('diff-panel').hidden = true;
      state.fitted = false;
      state.diff = null;
      return loadAll();
    }).then(function () {
      setBusy(false);
    }).catch(function (err) {
      stopProgress();
      setBusy(false);
      showError(err.message, resetAll);
    });
  }

  /* ------------------------------------------------------------------ */
  /* Events                                                             */
  /* ------------------------------------------------------------------ */
  function wire() {
    $('mode-toggle').addEventListener('click', function (e) {
      var btn = e.target.closest('button[data-mode]');
      if (!btn) return;
      state.mode = btn.dataset.mode;
      Array.prototype.forEach.call(this.children, function (b) {
        b.classList.toggle('is-active', b === btn);
      });
      renderMaps();
      renderUnserved();
    });

    $('payout-head').addEventListener('click', function (e) {
      var th = e.target.closest('th[data-key]');
      if (!th) return;
      var key = th.dataset.key;
      if (state.sort.key === key) state.sort.dir *= -1;
      else state.sort = { key: key, dir: th.dataset.type === 'text' ? 1 : -1 };
      renderPayouts();
    });

    $('btn-truck').addEventListener('click', function () {
      var id = $('sel-truck').value;
      disrupt({ type: 'truck_breakdown', target_id: id, time_limit_seconds: PLAN_TIME_LIMIT },
        'Truck ' + id + ' broke down — re-planning…');
    });

    $('btn-buyer').addEventListener('click', function () {
      var id = $('sel-buyer').value;
      disrupt({ type: 'buyer_cancel', target_id: id, time_limit_seconds: PLAN_TIME_LIMIT },
        'Buyer ' + id + ' cancelled — re-planning…');
    });

    $('btn-delay').addEventListener('click', function () {
      var id = $('sel-farmer').value;
      var minutes = parseInt($('inp-delay').value, 10);
      if (!isFinite(minutes) || minutes <= 0) {
        showError('Enter a positive delay in minutes before applying a pickup delay.', null);
        return;
      }
      disrupt({
        type: 'pickup_delay', target_id: id, delay_minutes: minutes,
        time_limit_seconds: PLAN_TIME_LIMIT
      }, 'Pickup for ' + id + ' delayed ' + minutes + ' min — re-planning…');
    });

    $('btn-reset').addEventListener('click', resetAll);
    $('diff-close').addEventListener('click', function () { $('diff-panel').hidden = true; });
    $('error-close').addEventListener('click', hideError);
    $('error-retry').addEventListener('click', function () {
      var fn = retryHandler;
      hideError();
      if (fn) fn();
    });

    var resizeTimer = null;
    window.addEventListener('resize', function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        if (maps.a) maps.a.invalidateSize();
        if (maps.b && state.mode === 'both') maps.b.invalidateSize();
      }, 180);
    });

    window.addEventListener('error', function (e) {
      showError('Unexpected front-end error: ' + (e.message || 'unknown') +
        (e.filename ? ' (' + e.filename + ':' + e.lineno + ')' : ''), null);
    });
    window.addEventListener('unhandledrejection', function (e) {
      var reason = e.reason && e.reason.message ? e.reason.message : String(e.reason);
      showError('Unexpected front-end error: ' + reason, null);
    });
  }

  /* ------------------------------------------------------------------ */
  /* Boot                                                               */
  /* ------------------------------------------------------------------ */
  var booted = false;

  function boot() {
    wire();
    // Debug handle: lets anyone poke at the live plan from the console during
    // a demo (kisanRoute.state.optimized, kisanRoute.maps.a.getZoom(), ...).
    window.kisanRoute = { state: state, maps: maps, render: renderAll };
    var missing = window.__kisanCdn || [];
    if (!hasLeaflet() && missing.indexOf('Leaflet') < 0) missing = missing.concat('Leaflet');
    if (!hasChart() && missing.indexOf('Chart.js') < 0) missing = missing.concat('Chart.js');
    if (missing.length) {
      showError(missing.join(' and ') + ' could not be loaded from the CDN, so ' +
        (missing.length > 1 ? 'the map and the chart are' : 'that view is') +
        ' unavailable. Check the network; every number on the page is still live.', null);
    }
    loadAll();
  }

  /* The dashboard no longer boots itself: shell.js decides, after login, that
     this browser belongs to a coordinator and calls boot() once. */
  window.kisanDashboard = {
    boot: function () {
      if (booted) return;
      booted = true;
      boot();
    },
    booted: function () { return booted; },
    reload: loadAll,
    state: state
  };
})();
