/* KisanRoute shell: accounts, routing by role, and the live polling loop.
 *
 * app.js owns the coordinator dashboard and is booted by this file once a
 * coordinator signs in. Everything else -- login, the farmer, driver and buyer
 * views, and the 3-second poll that keeps every browser in step -- lives here.
 *
 * The server decides what each role may see: this file renders whatever
 * /api/me/state returns and never filters for privacy, because by the time the
 * payload reaches the browser the other farmers' numbers were never in it.
 */
(function () {
  'use strict';

  var TOKEN_KEY = 'kisanroute.token';
  var LANG_KEY = 'kisanroute.lang';
  var POLL_MS = 3000;
  var NAGPUR = [21.1458, 79.0882];
  var TRUCK_COLOURS = ['#1971c2', '#0c8599', '#5f3dc4', '#c2255c', '#2b8a3e', '#b35c00'];
  var CROP_COLOURS = {
    tomato: '#c92a2a', leafy_greens: '#2f9e44', onion: '#9c36b5',
    orange: '#e8590c', banana: '#b08900', potato: '#6d4c41'
  };

  /* ------------------------------------------------------------------ */
  /* Translations. Farmer-facing labels and crop names, per the brief.   */
  /* ------------------------------------------------------------------ */
  var I18N = {
    en: {
      role_farmer: 'Farmer', role_driver: 'Driver', role_buyer: 'Buyer',
      role_coordinator: 'Coordinator', logout: 'Logout',
      reoptimising: 'Re-optimising…',
      your_pickup: 'Your pickup', your_lots: 'Your lots',
      post_produce: 'Post new produce', post_it: 'Post produce',
      crop: 'Crop', quantity: 'Quantity (kg)',
      harvest_time: 'Harvested (minutes after 06:00)',
      ready_from: 'Ready from (optional)', ready_until: 'Ready until (optional)',
      status_waiting: 'Waiting', status_proposed: 'Truck proposed',
      status_confirmed: 'Confirmed', status_picked_up: 'Picked up',
      status_delivered: 'Delivered', status_unserved: 'No truck',
      truck: 'Truck', arrives: 'Arrives', buyer: 'Buyer', price_per_kg: 'Price per kg',
      quantity_short: 'Quantity', deadline: 'Spoils at',
      you_receive: 'You receive', middleman: 'A middleman would pay', you_gain: 'You gain',
      gross: 'Sale value', transport: 'Transport share',
      no_lots: 'You have not posted any produce yet.',
      posted_ok: 'Posted. The planner is re-optimising now.',
      your_farm: 'Your farm', pickup_n: 'Pickup {n}', depot: 'Depot',
      map_note: 'Your farm, the truck coming for you, and where it delivers. Other growers on the same truck are not named.',
      h_confirmed: 'Confirmed — Truck {truck} arrives ~{eta}, sold to {buyer} at {price}/kg, you receive {net}.',
      h_proposed: 'Truck {truck} is proposed for ~{eta} — {buyer} at {price}/kg, about {net} to you. Waiting for the driver to accept.',
      h_picked: 'Picked up by truck {truck}. On its way to {buyer}.',
      h_delivered: 'Delivered to {buyer}. You receive {net}.',
      h_waiting: 'Listed. Waiting for the planner to propose a truck.',
      h_unserved: 'No truck could collect this lot today.',
      h_none: 'Post your produce below and a truck will be planned for it.',
      status_matched: 'Match found \u2014 accept?',
      status_accepted: 'Accepted',
      status_withdrawn: 'Withdrawn',
      status_unmatched: 'No truck',
      status_no_price: 'No buyer at your price',
      min_price: 'Minimum price (\u20b9/kg)',
      min_price_short: 'Your minimum',
      new_min_price: 'New minimum price (\u20b9/kg)',
      save_price: 'Save price',
      edit_price: 'Edit price',
      accept: 'Accept',
      decline: 'Decline',
      h_matched: 'Match found \u2014 {buyer} will buy your {crop} at {price}/kg. Truck {truck} picks up ~{eta}. You receive {net} after transport.',
      h_accepted: 'You accepted. Waiting for {who}.',
      who_driver: 'the driver',
      who_farmers: '{n} other farmer(s)',
      who_both: 'the driver and {n} other farmer(s)',
      h_price: 'No buyer at your price. Best offer for {crop} today: {best}/kg. Your {crop} spoils at {spoils}.',
      h_withdrawn: 'You withdrew this lot. Set a price to post it again.',
      day_from_now: '{clock}, 1 day from now',
      days_from_now: '{clock}, {n} days from now',
      price_hint: 'Best offer for {crop} today: {best}/kg',
      withdrawn_ok: 'Lot withdrawn. The planner is re-optimising.',
      price_saved: 'Price saved. The planner is re-optimising.',
      best_offer: 'Best offer today'
    },
    hi: {
      role_farmer: 'किसान', role_driver: 'चालक',
      role_buyer: 'खरीदार', role_coordinator: 'समन्वयक',
      logout: 'लॉग आउट',
      reoptimising: 'फिर से योजना बन रही है…',
      your_pickup: 'आपकी उठान', your_lots: 'आपकी उपज',
      post_produce: 'नई उपज दर्ज करें', post_it: 'दर्ज करें',
      crop: 'फसल', quantity: 'मात्रा (किलो)',
      harvest_time: 'कटाई (06:00 के बाद मिनट)',
      ready_from: 'तैयार (वैकल्पिक)',
      ready_until: 'अंतिम समय (वैकल्पिक)',
      status_waiting: 'प्रतीक्षा में',
      status_proposed: 'ट्रक प्रस्तावित',
      status_confirmed: 'पुष्टि हुई',
      status_picked_up: 'उठा लिया',
      status_delivered: 'पहुँचा दिया',
      status_unserved: 'कोई ट्रक नहीं',
      truck: 'ट्रक', arrives: 'पहुँचेगा',
      buyer: 'खरीदार', price_per_kg: 'भाव प्रति किलो',
      quantity_short: 'मात्रा', deadline: 'खराब होगा',
      you_receive: 'आपको मिलेंगे',
      middleman: 'बिचौलिया देता',
      you_gain: 'आपका लाभ',
      gross: 'बिक्री मूल्य',
      transport: 'भाड़े का हिस्सा',
      no_lots: 'आपने अभी तक कोई उपज दर्ज नहीं की है।',
      posted_ok: 'दर्ज हो गया। योजना फिर से बन रही है।',
      your_farm: 'आपका खेत', pickup_n: 'उठान {n}', depot: 'डिपो',
      map_note: 'आपका खेत, आपके लिए आ रहा ट्रक, और वह कहाँ पहुँचाएगा।',
      h_confirmed: 'पुष्टि — ट्रक {truck} लगभग {eta} पर आएगा, {buyer} को {price}/किलो पर बिका, आपको {net} मिलेंगे।',
      h_proposed: 'ट्रक {truck} लगभग {eta} के लिए प्रस्तावित है — {buyer}, {price}/किलो, लगभग {net}। चालक की स्वीकृति का इंतजार।',
      h_picked: 'ट्रक {truck} ने उठा लिया। {buyer} की ओर रवाना।',
      h_delivered: '{buyer} को पहुँचा दिया। आपको {net} मिलेंगे।',
      h_waiting: 'दर्ज हो गया। ट्रक की योजना का इंतजार।',
      h_unserved: 'आज कोई ट्रक यह उपज नहीं उठा सका।',
      h_none: 'नीचे अपनी उपज दर्ज करें।',
      status_matched: "मैच मिला — स्वीकार करें?",
      status_accepted: "स्वीकार किया",
      status_withdrawn: "वापस लिया",
      status_unmatched: "कोई ट्रक नहीं",
      status_no_price: "आपके भाव पर कोई खरीदार नहीं",
      min_price: "न्यूनतम भाव (₹/किलो)",
      min_price_short: "आपका न्यूनतम भाव",
      new_min_price: "नया न्यूनतम भाव (₹/किलो)",
      save_price: "भाव सहेजें",
      edit_price: "भाव बदलें",
      accept: "स्वीकार करें",
      decline: "मना करें",
      h_matched: "मैच मिला — {buyer} आपका {crop} {price}/किलो पर खरीदेगा। ट्रक {truck} लगभग {eta} पर उठाएगा। भाड़े के बाद आपको {net} मिलेंगे।",
      h_accepted: "आपने स्वीकार किया। {who} का इंतज़ार है।",
      who_driver: "चालक",
      who_farmers: "{n} अन्य किसान",
      who_both: "चालक और {n} अन्य किसान",
      h_price: "आपके भाव पर कोई खरीदार नहीं। आज {crop} का सबसे अच्छा भाव: {best}/किलो। आपका {crop} {spoils} तक खराब हो जाएगा।",
      h_withdrawn: "आपने यह उपज वापस ली। फिर से दर्ज करने के लिए भाव तय करें।",
      day_from_now: "{clock}, 1 दिन बाद",
      days_from_now: "{clock}, {n} दिन बाद",
      price_hint: "आज {crop} का सबसे अच्छा भाव: {best}/किलो",
      withdrawn_ok: "उपज वापस ली गई। योजना फिर से बन रही है।",
      price_saved: "भाव सहेजा गया। योजना फिर से बन रही है।",
      best_offer: "आज का सबसे अच्छा भाव"
    },
    mr: {
      role_farmer: 'शेतकरी', role_driver: 'चालक',
      role_buyer: 'खरेदीदार', role_coordinator: 'समन्वयक',
      logout: 'बाहेर पडा',
      reoptimising: 'पुन्हा नियोजन सुरू आहे…',
      your_pickup: 'तुमची उचल', your_lots: 'तुमचा माल',
      post_produce: 'नवीन माल नोंदवा', post_it: 'नोंदवा',
      crop: 'पीक', quantity: 'वजन (किलो)',
      harvest_time: 'कापणी (06:00 नंतर मिनिटे)',
      ready_from: 'तयार (ऐच्छिक)',
      ready_until: 'शेवटची वेळ (ऐच्छिक)',
      status_waiting: 'प्रतीक्षेत',
      status_proposed: 'ट्रक सुचवला',
      status_confirmed: 'निश्चित',
      status_picked_up: 'उचलले',
      status_delivered: 'पोहोचवले',
      status_unserved: 'ट्रक नाही',
      truck: 'ट्रक', arrives: 'येईल',
      buyer: 'खरेदीदार', price_per_kg: 'दर प्रति किलो',
      quantity_short: 'वजन', deadline: 'खराब होईल',
      you_receive: 'तुम्हाला मिळतील',
      middleman: 'दलाल देणार होता',
      you_gain: 'तुमचा फायदा',
      gross: 'विक्री मूल्य',
      transport: 'वाहतूक वाटा',
      no_lots: 'तुम्ही अजून माल नोंदवलेला नाही.',
      posted_ok: 'नोंदवले. नियोजन पुन्हा सुरू आहे.',
      your_farm: 'तुमचे शेत', pickup_n: 'उचल {n}', depot: 'डेपो',
      map_note: 'तुमचे शेत, तुमच्यासाठी येणारा ट्रक, आणि तो कुठे पोहोचवेल.',
      h_confirmed: 'निश्चित — ट्रक {truck} सुमारे {eta} वाजता येईल, {buyer} ला {price}/किलो दराने विकले, तुम्हाला {net} मिळतील.',
      h_proposed: 'ट्रक {truck} सुमारे {eta} साठी सुचवला आहे — {buyer}, {price}/किलो, अंदाजे {net}. चालकाच्या स्वीकाराची प्रतीक्षा.',
      h_picked: 'ट्रक {truck} ने उचलले. {buyer} कडे निघाला.',
      h_delivered: '{buyer} ला पोहोचवले. तुम्हाला {net} मिळतील.',
      h_waiting: 'नोंदवले. ट्रक नियोजनाची प्रतीक्षा.',
      h_unserved: 'आज कोणताही ट्रक हा माल उचलू शकला नाही.',
      h_none: 'खाली तुमचा माल नोंदवा.',
      status_matched: "खरेदीदार मिळाला — स्वीकारायचे?",
      status_accepted: "स्वीकारले",
      status_withdrawn: "मागे घेतले",
      status_unmatched: "ट्रक नाही",
      status_no_price: "तुमच्या दरात खरेदीदार नाही",
      min_price: "किमान दर (₹/किलो)",
      min_price_short: "तुमचा किमान दर",
      new_min_price: "नवीन किमान दर (₹/किलो)",
      save_price: "दर जतन करा",
      edit_price: "दर बदला",
      accept: "स्वीकारा",
      decline: "नाकारा",
      h_matched: "खरेदीदार मिळाला — {buyer} तुमचा {crop} {price}/किलो दराने घेईल. ट्रक {truck} सुमारे {eta} वाजता उचलेल. वाहतुकीनंतर तुम्हाला {net} मिळतील.",
      h_accepted: "तुम्ही स्वीकारले. {who} यांची वाट पाहत आहोत.",
      who_driver: "चालक",
      who_farmers: "{n} इतर शेतकरी",
      who_both: "चालक आणि {n} इतर शेतकरी",
      h_price: "तुमच्या दरात खरेदीदार नाही. आज {crop} साठी सर्वोत्तम दर: {best}/किलो. तुमचा {crop} {spoils} पर्यंत खराब होईल.",
      h_withdrawn: "तुम्ही हा माल मागे घेतला. पुन्हा नोंदवण्यासाठी दर ठरवा.",
      day_from_now: "{clock}, 1 दिवसानंतर",
      days_from_now: "{clock}, {n} दिवसांनंतर",
      price_hint: "आज {crop} साठी सर्वोत्तम दर: {best}/किलो",
      withdrawn_ok: "माल मागे घेतला. नियोजन पुन्हा सुरू आहे.",
      price_saved: "दर जतन केला. नियोजन पुन्हा सुरू आहे.",
      best_offer: "आजचा सर्वोत्तम दर"
    }
  };

  var CROP_I18N = {
    en: { tomato: 'Tomato', leafy_greens: 'Leafy greens', onion: 'Onion',
          orange: 'Orange', banana: 'Banana', potato: 'Potato' },
    hi: { tomato: 'टमाटर', leafy_greens: 'पत्तेदार साग',
          onion: 'प्याज़', orange: 'संतरा',
          banana: 'केला', potato: 'आलू' },
    mr: { tomato: 'टोमॅटो', leafy_greens: 'पालेभाज्या',
          onion: 'कांदा', orange: 'संत्री',
          banana: 'केळी', potato: 'बटाटा' }
  };

  /* ------------------------------------------------------------------ */
  /* State                                                              */
  /* ------------------------------------------------------------------ */
  var S = {
    token: null, user: null, version: -1, planning: false, data: null,
    lang: 'en', poll: null, maps: {}, layers: {}, pickedLatLng: null, inflight: false,
    forceAgain: false, seen: null, bellOpen: false, editingPrice: null, pinnedLot: null,
    activeLot: null, market: {}, buyerData: null
  };

  /* ------------------------------------------------------------------ */
  /* Small helpers                                                      */
  /* ------------------------------------------------------------------ */
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function clear(n) { while (n && n.firstChild) n.removeChild(n.firstChild); }
  function show(node, on) { if (node) node.hidden = !on; }

  var NF = {};
  function fmtNum(v, d) {
    if (v === null || v === undefined || v === '' || (typeof v === 'number' && !isFinite(v))) {
      return '—';
    }
    var k = Math.max(0, Math.min(6, isFinite(d) ? Math.floor(d) : 0));
    if (!NF[k]) {
      NF[k] = new Intl.NumberFormat('en-IN', { minimumFractionDigits: k, maximumFractionDigits: k });
    }
    return NF[k].format(Number(v));
  }
  function fmtINR(v, d) {
    return (v === null || v === undefined) ? '—' : '₹' + fmtNum(v, d || 0);
  }
  function fmtKg(v) { return fmtNum(v, 0) + ' kg'; }
  function clock(min) {
    if (min === null || min === undefined) return '—';
    var total = 360 + Math.round(min);
    var day = Math.floor(total / 1440), rest = ((total % 1440) + 1440) % 1440;
    return String(Math.floor(rest / 60)).padStart(2, '0') + ':' +
           String(rest % 60).padStart(2, '0') + (day ? ' +' + day + 'd' : '');
  }
  function truckColour(id) {
    if (!id) return '#6b7884';
    var n = 0;
    for (var i = 0; i < id.length; i++) n += id.charCodeAt(i);
    return TRUCK_COLOURS[n % TRUCK_COLOURS.length];
  }
  function cropColour(c) { return CROP_COLOURS[c] || '#0b5394'; }

  function t(key, vars) {
    var dict = I18N[S.lang] || I18N.en;
    var s = dict[key] !== undefined ? dict[key] : (I18N.en[key] !== undefined ? I18N.en[key] : key);
    if (vars) {
      Object.keys(vars).forEach(function (k) {
        s = s.split('{' + k + '}').join(String(vars[k]));
      });
    }
    return s;
  }
  function cropName(crop) {
    var dict = CROP_I18N[S.lang] || CROP_I18N.en;
    if (dict[crop]) return dict[crop];
    return String(crop || '').replace(/_/g, ' ').replace(/\b\w/g, function (c) {
      return c.toUpperCase();
    });
  }

  /* ------------------------------------------------------------------ */
  /* API                                                                */
  /* ------------------------------------------------------------------ */
  function api(method, path, body) {
    var opts = { method: method, headers: { accept: 'application/json' } };
    if (body !== undefined) {
      opts.headers['content-type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    if (S.token) opts.headers.Authorization = 'Bearer ' + S.token;

    return fetch(path, opts).catch(function (e) {
      throw new Error('Cannot reach the KisanRoute API — is the server running? (' +
        (e && e.message ? e.message : e) + ')');
    }).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (_) { /* not JSON */ }
        if (!res.ok) {
          var detail = data && data.detail;
          var msg = typeof detail === 'string' ? detail
            : Array.isArray(detail) ? detail.map(function (d) { return d.msg || JSON.stringify(d); }).join('; ')
            : (text || res.statusText);
          var err = new Error(msg);
          err.status = res.status;
          throw err;
        }
        return data;
      });
    });
  }

  function showError(message) {
    $('error-text').textContent = message;
    $('error-retry').hidden = true;
    $('error-banner').hidden = false;
  }
  function hideError() { $('error-banner').hidden = true; }

  /* ------------------------------------------------------------------ */
  /* Storage                                                            */
  /* ------------------------------------------------------------------ */
  function store(key, value) {
    try {
      if (value === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    } catch (_) { /* private mode: session simply will not survive a reload */ }
  }
  function recall(key) {
    try { return window.localStorage.getItem(key); } catch (_) { return null; }
  }

  /* ------------------------------------------------------------------ */
  /* Maps                                                               */
  /* ------------------------------------------------------------------ */
  function hasLeaflet() { return typeof window.L !== 'undefined'; }

  function ensureMap(domId) {
    if (!hasLeaflet()) return null;
    if (S.maps[domId]) return S.maps[domId];
    var node = $(domId);
    if (!node) return null;
    var m = L.map(domId, { zoomSnap: 0, zoomDelta: 0.5 });
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 18,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    }).addTo(m);
    m.setView(NAGPUR, 9);
    S.maps[domId] = m;
    S.layers[domId] = L.layerGroup().addTo(m);
    return m;
  }

  function fitTo(domId, points) {
    var m = S.maps[domId];
    if (!m || !points.length) return;
    window.requestAnimationFrame(function () {
      m.invalidateSize();
      m.fitBounds(L.latLngBounds(points).pad(0.12), { padding: [24, 24], maxZoom: 12 });
    });
  }

  function pin(colour, label, size) {
    return L.divIcon({
      className: '',
      html: '<div class="marker-pin" style="background:' + colour + ';border-radius:6px;width:' +
            size + 'px;height:' + size + 'px">' + label + '</div>',
      iconSize: [size, size],
      iconAnchor: [size / 2, size / 2]
    });
  }

  /* ------------------------------------------------------------------ */
  /* Views                                                              */
  /* ------------------------------------------------------------------ */
  function showView(role) {
    ['view-login', 'view-farmer', 'view-driver', 'view-buyer', 'view-coordinator']
      .forEach(function (id) { show($(id), false); });
    show($('appbar'), !!role);
    document.body.className = role ? 'role-' + role : '';
    if (!role) { show($('view-login'), true); return; }
    show($('view-' + role), true);
  }

  function renderAppbar() {
    if (!S.user) return;
    $('appbar-role').textContent = t('role_' + S.user.role);
    $('appbar-user').textContent = S.user.display_name +
      (S.user.ref_id ? ' · ' + S.user.ref_id : '');
    show($('appbar-lang'), S.user.role === 'farmer');
    $('btn-logout').textContent = t('logout');
    $('appbar-planning-text').textContent = t('reoptimising');
    show($('appbar-planning'), S.planning);
  }

  /* ---- Farmer ------------------------------------------------------ */
  /** '11:35', or '06:50, 3 days from now' for crops that keep. */
  function spoilsPhrase(minutes) {
    if (minutes === null || minutes === undefined) return '—';
    var text = clock(minutes).split(' +')[0];
    var days = Math.floor((360 + minutes) / 1440);
    if (days <= 0) return text;
    return t(days === 1 ? 'day_from_now' : 'days_from_now', { clock: text, n: days });
  }

  function isPriceBlocked(lot) {
    return !!lot && lot.status === 'unmatched' && lot.reason_code === 'price';
  }

  function statusLabel(lot) {
    if (!lot) return t('status_waiting');
    if (isPriceBlocked(lot)) return t('status_no_price');
    return t('status_' + lot.status);
  }

  function waitingOn(lot) {
    var others = Math.max(0, (lot.awaiting_farmers || 0));
    if (!lot.driver_accepted && others > 0) return t('who_both', { n: others });
    if (!lot.driver_accepted) return t('who_driver');
    return t('who_farmers', { n: others });
  }

  function headlineFor(lot) {
    if (!lot) return t('h_none');
    var vars = {
      truck: lot.truck_id || '—',
      eta: lot.eta_clock || '—',
      buyer: lot.buyer_name || '—',
      crop: cropName(lot.crop).toLowerCase(),
      price: lot.price_per_kg !== null && lot.price_per_kg !== undefined
        ? fmtINR(lot.price_per_kg, 2) : '—',
      net: lot.net_payout !== null && lot.net_payout !== undefined
        ? fmtINR(lot.net_payout, 0) : '—',
      best: lot.best_offer_per_kg !== null && lot.best_offer_per_kg !== undefined
        ? fmtINR(lot.best_offer_per_kg, 2) : '—',
      spoils: spoilsPhrase(lot.spoilage_deadline_min)
    };
    if (isPriceBlocked(lot)) return t('h_price', vars);
    switch (lot.status) {
      case 'matched': return t('h_matched', vars);
      case 'accepted': return t('h_accepted', { who: waitingOn(lot) });
      case 'confirmed': return t('h_confirmed', vars);
      case 'picked_up': return t('h_picked', vars);
      case 'delivered': return t('h_delivered', vars);
      case 'withdrawn': return t('h_withdrawn', vars);
      case 'unmatched': return t('h_unserved', vars);
      default: return t('h_waiting', vars);
    }
  }

  function renderFarmer(data) {
    var lots = data.lots || [];
    S.market = data.crops || {};
    fillCropSelect(S.market);
    var active = null;
    for (var i = 0; i < lots.length; i++) {
      if (lots[i].lot_id === (S.pinnedLot || data.active_lot_id)) { active = lots[i]; break; }
    }
    if (!active && lots.length) active = lots[0];
    S.activeLot = active;

    var statusEl = $('farmer-status');
    statusEl.textContent = statusLabel(active);
    statusEl.className = 'statuspill statuspill--' +
      (active ? (isPriceBlocked(active) ? 'unmatched' : active.status) : 'waiting');
    $('farmer-lot').textContent = active
      ? active.lot_id + ' · ' + cropName(active.crop) + ' · ' + fmtKg(active.quantity_kg) +
        ' · ' + t('min_price_short') + ' ' + fmtINR(active.min_price_per_kg, 2) + '/kg'
      : '';
    $('farmer-headline').textContent = headlineFor(active);

    var grid = $('farmer-grid');
    clear(grid);
    function row(label, value) {
      var pair = el('div', 'bigcard__pair');
      pair.appendChild(el('dt', null, label));
      pair.appendChild(el('dd', null, value));
      grid.appendChild(pair);
    }
    if (active) {
      if (active.truck_id) {
        row(t('buyer'), active.buyer_name || '\u2014');
        row(t('price_per_kg'), active.price_per_kg ? fmtINR(active.price_per_kg, 2) : '\u2014');
        row(t('truck'), active.truck_id);
        row(t('arrives'), active.eta_clock || '\u2014');
      }
      row(t('min_price_short'), fmtINR(active.min_price_per_kg, 2));
      if (!active.truck_id && active.best_offer_per_kg) {
        row(t('best_offer'), fmtINR(active.best_offer_per_kg, 2));
      }
      row(t('deadline'), spoilsPhrase(active.spoilage_deadline_min));
    }

    var money = $('farmer-money');
    clear(money);
    function box(cls, label, value, sub) {
      var b = el('div', 'moneybox ' + cls);
      b.appendChild(el('div', 'moneybox__label', label));
      b.appendChild(el('div', 'moneybox__value', value));
      if (sub) b.appendChild(el('div', 'moneybox__sub', sub));
      money.appendChild(b);
    }
    if (active && active.net_payout !== null && active.net_payout !== undefined) {
      box('moneybox--you', t('you_receive'), fmtINR(active.net_payout, 0),
        t('gross') + ' ' + fmtINR(active.gross_revenue, 0) + ' − ' +
        t('transport') + ' ' + fmtINR(active.transport_share, 0));
      box('', t('middleman'), fmtINR(active.middleman_revenue, 0),
        fmtINR(active.middleman_price_per_kg, 2) + '/kg');
      box('moneybox--gain', t('you_gain'), fmtINR(active.gain_vs_middleman, 0),
        active.middleman_revenue
          ? '+' + fmtNum(100 * active.gain_vs_middleman / active.middleman_revenue, 0) + '%'
          : '');
    }

    // The server's prose reason is shown only for non-price cases; the price
    // case is rendered from its fields above so it can be translated.
    var reason = $('farmer-reason');
    var showReason = !!(active && active.reason && active.status === 'unmatched' && !isPriceBlocked(active));
    reason.textContent = showReason ? active.reason : '';
    show(reason, showReason);

    var actions = $('farmer-actions');
    clear(actions);
    function button(cls, label, handler, id) {
      var b = el('button', 'btn ' + cls, label);
      b.type = 'button';
      if (id) b.id = id;
      b.onclick = handler;
      actions.appendChild(b);
      return b;
    }
    if (active) {
      if (active.status === 'matched' && active.can_accept) {
        button('btn--big', t('accept'), function () { matchAction(active.lot_id, 'accept'); }, 'btn-match-accept');
        button('btn--danger', t('decline'), function () { matchAction(active.lot_id, 'decline'); }, 'btn-match-decline');
      } else if (active.status === 'accepted') {
        actions.appendChild(el('div', 'bigcard__waiting', t('h_accepted', { who: waitingOn(active) })));
        button('btn--plain', t('decline'), function () { matchAction(active.lot_id, 'decline'); }, 'btn-match-decline');
      }
      var priceCase = isPriceBlocked(active) || active.status === 'withdrawn' ||
        active.status === 'waiting' || active.status === 'unmatched';
      if (priceCase && active.can_edit_price) {
        button(isPriceBlocked(active) ? 'btn--big' : 'btn--plain', t('edit_price'), function () {
          S.editingPrice = S.editingPrice === active.lot_id ? null : active.lot_id;
          renderPriceEditor(active);
        }, 'btn-edit-price');
      }
    }
    renderPriceEditor(active);

    var list = $('farmer-lots');
    clear(list);
    if (!lots.length) {
      list.appendChild(el('p', 'empty', t('no_lots')));
    } else {
      lots.forEach(function (lot) {
        var row2 = el('div', 'lot' + (active && lot.lot_id === active.lot_id ? ' is-active' : ''));
        row2.style.cursor = 'pointer';
        row2.onclick = function () { S.pinnedLot = lot.lot_id; S.editingPrice = null; render(); };
        var dot = el('span', 'crop-dot');
        dot.style.background = cropColour(lot.crop);
        row2.appendChild(dot);
        row2.appendChild(el('span', 'lot__crop', cropName(lot.crop)));
        row2.appendChild(el('span', 'lot__kg', fmtKg(lot.quantity_kg)));
        var pill = el('span', 'statuspill statuspill--' +
          (isPriceBlocked(lot) ? 'unmatched' : lot.status), statusLabel(lot));
        pill.style.fontSize = '.85rem';
        row2.appendChild(pill);
        row2.appendChild(el('span', 'lot__meta',
          (lot.truck_id ? lot.truck_id + ' · ' + (lot.eta_clock || '') : '')));
        if (lot.net_payout !== null && lot.net_payout !== undefined) {
          row2.appendChild(el('span', 'lot__pay', fmtINR(lot.net_payout, 0)));
        }
        list.appendChild(row2);
      });
    }

    $('farmer-map-note').textContent = t('map_note');
    renderFarmerMap(data, active);
    updateLotPriceHint();
    translateStatic();
  }

  function renderPriceEditor(lot) {
    var form = $('form-price');
    var open = !!lot && S.editingPrice === lot.lot_id;
    show(form, open);
    if (open && document.activeElement !== $('price-input')) {
      var suggestion = lot.best_offer_per_kg ? Math.floor(lot.best_offer_per_kg) : lot.min_price_per_kg;
      $('price-input').value = isPriceBlocked(lot) ? suggestion : lot.min_price_per_kg;
    }
  }

  function renderFarmerMap(data, active) {
    var m = ensureMap('map-farmer');
    if (!m) return;
    var g = S.layers['map-farmer'];
    g.clearLayers();
    var pts = [];

    if (S.user && S.user.lat !== null && S.user.lat !== undefined) {
      pts.push([S.user.lat, S.user.lng]);
      L.circleMarker([S.user.lat, S.user.lng], {
        radius: 13, color: '#fff', weight: 3,
        fillColor: active ? cropColour(active.crop) : '#2f9e44', fillOpacity: 1
      }).bindPopup('<b>' + t('your_farm') + '</b><br>' + (S.user.village || '')).addTo(g);
    }

    var route = data.route || [];
    if (route.length > 1) {
      var colour = truckColour(data.truck ? data.truck.truck_id : null);
      L.polyline(route.map(function (s) { return [s.lat, s.lng]; }),
        { color: colour, weight: 5, opacity: .9 }).addTo(g);
      route.forEach(function (s) {
        pts.push([s.lat, s.lng]);
        if (s.kind === 'depot') {
          L.marker([s.lat, s.lng], { icon: pin('#0d1a13', 'D', 26) })
            .bindPopup('<b>' + t('depot') + '</b>').addTo(g);
        } else if (s.kind === 'buyer') {
          L.marker([s.lat, s.lng], { icon: pin('#0b5394', 'B', 26) })
            .bindPopup('<b>' + (s.name || t('buyer')) + '</b><br>' + (s.arrival_clock || '')).addTo(g);
        } else if (!s.mine) {
          L.circleMarker([s.lat, s.lng], {
            radius: 7, color: '#8b9690', weight: 2, fillColor: '#cfd6d0', fillOpacity: .9
          }).bindPopup(t('pickup_n', { n: s.pickup_index })).addTo(g);
        }
      });
    }
    (data.buyers || []).forEach(function (b) { pts.push([b.lat, b.lng]); });
    fitTo('map-farmer', pts);
  }

  /* ---- Driver ------------------------------------------------------ */
  function driverStatusLabel(trip, fallback) {
    if (!trip) return (fallback || 'idle') === 'declined' ? 'Declined' : 'No trip';
    if (trip.status === 'proposed') {
      return trip.driver_accepted
        ? 'Accepted — waiting for ' + trip.awaiting_farmers + ' farmer' +
          (trip.awaiting_farmers === 1 ? '' : 's')
        : 'Trip offer';
    }
    return ({ confirmed: 'Confirmed', in_transit: 'In transit', delivered: 'Delivered',
              declined: 'Declined', idle: 'No trip' })[trip.status] || trip.status;
  }

  function renderDriver(data) {
    var trip = data.trip;
    var statusEl = $('driver-status');
    var status = trip ? trip.status : (data.status || 'idle');
    statusEl.textContent = driverStatusLabel(trip, status);
    statusEl.className = 'statuspill statuspill--' +
      (trip && trip.status === 'proposed' && trip.driver_accepted ? 'accepted' : status);
    $('driver-title').textContent = 'Truck ' + (data.truck ? data.truck.truck_id : '');

    var stats = $('driver-stats');
    clear(stats);
    var actions = $('driver-actions');
    clear(actions);
    var stops = $('driver-stops');
    clear(stops);
    var waiting = $('driver-waiting');

    if (!trip) {
      show($('driver-empty'), true);
      show(waiting, false);
      $('driver-empty').textContent = status === 'declined'
        ? 'You declined this round. Your lots have been re-planned onto other trucks.'
        : 'No trip offer for your truck right now. This page updates by itself.';
      var m0 = ensureMap('map-driver');
      if (m0) S.layers['map-driver'].clearLayers();
      return;
    }
    show($('driver-empty'), false);

    function stat(label, value, cls) {
      var s = el('div', 'stat' + (cls ? ' ' + cls : ''));
      s.appendChild(el('div', 'stat__label', label));
      s.appendChild(el('div', 'stat__value', value));
      stats.appendChild(s);
    }
    stat('Deliver to', trip.buyer_name || '—');
    stat('Your fee', fmtINR(trip.fee, 0), 'stat--pay');
    stat('Distance', fmtNum(trip.distance_km, 1) + ' km');
    stat('Load', fmtKg(trip.load_kg) + ' / ' + fmtKg(trip.capacity_kg));
    stat('Farmers', trip.farmers_accepted + ' / ' + trip.pickups + ' confirmed');
    stat('Deliver by', trip.delivery_clock);

    var waitingText = '';
    if (trip.status === 'proposed' && trip.driver_accepted) {
      waitingText = 'You accepted. The trip confirms when ' + trip.awaiting_farmers +
        ' more farmer' + (trip.awaiting_farmers === 1 ? '' : 's') + ' accept' +
        (trip.awaiting_farmers === 1 ? 's' : '') + '.';
    } else if (trip.status === 'proposed' && trip.awaiting_farmers > 0) {
      waitingText = trip.awaiting_farmers + ' farmer' + (trip.awaiting_farmers === 1 ? ' has' : 's have') +
        ' not answered yet. You can accept now; the trip confirms when everyone has.';
    }
    waiting.textContent = waitingText;
    show(waiting, !!waitingText);

    if (trip.can_accept) {
      var accept = el('button', 'btn btn--big', 'Accept trip');
      accept.id = 'btn-trip-accept';
      accept.onclick = function () { tripAction('accept'); };
      actions.appendChild(accept);
    }
    if (trip.can_decline) {
      var decline = el('button', trip.can_accept ? 'btn btn--danger' : 'btn btn--plain', 'Decline');
      decline.id = 'btn-trip-decline';
      decline.onclick = function () { tripAction('decline'); };
      actions.appendChild(decline);
    }
    if (trip.status === 'confirmed' || trip.status === 'in_transit') {
      var done = el('button', 'btn btn--big', 'Mark delivered');
      done.onclick = function () { tripAction('deliver'); };
      actions.appendChild(done);
    }

    var rolling = trip.status === 'confirmed' || trip.status === 'in_transit';
    trip.stops.forEach(function (s) {
      var li = el('li');
      var isFarmer = s.kind === 'farmer';
      var collected = isFarmer && s.status === 'picked_up';
      if (collected) li.className = 'is-done';
      li.appendChild(el('div', 'stop__num',
        isFarmer ? String(s.seq) : (s.kind === 'buyer' ? 'B' : 'D')));

      var name = el('div', 'stop__name', s.name);
      if (isFarmer && s.badge) {
        var kind = s.acceptance === 'accepted' ? 'ok' : s.acceptance === 'declined' ? 'no' : 'wait';
        name.appendChild(el('span', 'badge badge--' + kind, s.badge));
      }
      li.appendChild(name);

      var meta = el('div', 'stop__meta');
      if (isFarmer) {
        meta.appendChild(document.createTextNode(
          cropName(s.crop) + ' · ' + fmtKg(s.quantity_kg) + ' · ' + (s.village || '') + ' · '));
        if (s.phone) {
          var a = el('a', null, s.phone);
          a.href = 'tel:' + String(s.phone).replace(/\s/g, '');
          meta.appendChild(a);
        }
      } else {
        meta.appendChild(document.createTextNode(
          s.kind === 'buyer' ? 'Deliver ' + fmtKg(s.load_kg_after) : 'Depot'));
      }
      li.appendChild(meta);

      var right = el('div', 'stop__action');
      right.appendChild(el('div', 'stop__time', s.arrival_clock));
      if (isFarmer && rolling && !collected) {
        var btn = el('button', 'btn btn--plain', 'Picked up');
        btn.onclick = function () { pickupStop(s.ref_id); };
        right.appendChild(btn);
      }
      li.appendChild(right);
      stops.appendChild(li);
    });

    renderDriverMap(trip);
  }

  function renderDriverMap(trip) {
    var m = ensureMap('map-driver');
    if (!m) return;
    var g = S.layers['map-driver'];
    g.clearLayers();
    var pts = trip.stops.map(function (s) { return [s.lat, s.lng]; });
    var colour = truckColour(trip.truck_id);
    if (pts.length > 1) L.polyline(pts, { color: colour, weight: 6, opacity: .9 }).addTo(g);
    trip.stops.forEach(function (s) {
      if (s.kind === 'depot') {
        L.marker([s.lat, s.lng], { icon: pin('#0d1a13', 'D', 26) })
          .bindPopup('<b>Depot</b>').addTo(g);
      } else if (s.kind === 'buyer') {
        L.marker([s.lat, s.lng], { icon: pin('#0b5394', 'B', 26) })
          .bindPopup('<b>' + s.name + '</b><br>' + s.arrival_clock).addTo(g);
      } else {
        L.marker([s.lat, s.lng], {
          icon: pin(s.status === 'picked_up' ? '#0b7a3b' : colour, String(s.seq), 28)
        }).bindPopup('<b>' + s.name + '</b><br>' + cropName(s.crop) + ' · ' +
          fmtKg(s.quantity_kg) + '<br>' + s.arrival_clock +
          (s.phone ? '<br>' + s.phone : '')).addTo(g);
      }
    });
    fitTo('map-driver', pts);
  }

  /* ---- Buyer ------------------------------------------------------- */
  function orderRow(row) {
    var node = el('div', 'incoming__row');
    var badge = el('div', 'incoming__truck', row.truck_id);
    badge.style.background = truckColour(row.truck_id);
    node.appendChild(badge);

    var mid = el('div');
    mid.appendChild(el('div', 'stop__name', fmtKg(row.load_kg) + ' · ' + row.lots + ' lots'));
    var label = row.confirmed
      ? (row.status === 'in_transit' ? 'In transit' : row.status === 'delivered' ? 'Delivered' : 'Confirmed')
      : 'Pending — ' + (row.driver_accepted ? '' : 'driver' + (row.awaiting_farmers ? ' + ' : '')) +
        (row.awaiting_farmers ? row.awaiting_farmers + ' farmer' + (row.awaiting_farmers === 1 ? '' : 's') : '') +
        (row.driver_accepted && !row.awaiting_farmers ? 'confirming' : ' to accept');
    var pill = el('span', 'statuspill statuspill--' + (row.confirmed ? 'confirmed' : 'proposed'), label);
    pill.style.fontSize = '.8rem';
    mid.appendChild(pill);
    node.appendChild(mid);

    node.appendChild(el('div', 'incoming__crops', Object.keys(row.crops).map(function (c) {
      return cropName(c) + ' ' + fmtKg(row.crops[c]);
    }).join(' · ')));

    var eta = el('div', 'incoming__eta', 'ETA ' + row.eta_clock);
    eta.appendChild(el('small', null, row.picked_up + '/' + row.pickups + ' collected'));
    eta.appendChild(el('small', null, fmtINR(row.value, 0)));
    node.appendChild(eta);
    return node;
  }

  function renderBuyer(data) {
    var buyer = data.buyer || {};
    S.buyerData = data;
    $('buyer-title').textContent = buyer.name || 'Your orders';

    var stats = $('buyer-stats');
    clear(stats);
    function stat(label, value) {
      var s = el('div', 'stat');
      s.appendChild(el('div', 'stat__label', label));
      s.appendChild(el('div', 'stat__value', value));
      stats.appendChild(s);
    }
    stat('Confirmed', fmtKg(data.confirmed_kg || 0));
    stat('Pending', fmtKg((data.committed_kg || 0) - (data.confirmed_kg || 0)));
    stat('Still wanted', fmtKg(data.outstanding_kg || 0));
    stat('Demand', fmtKg(buyer.demand_kg || 0));
    if (buyer.window_start !== undefined) {
      stat('Window', clock(buyer.window_start) + '–' + clock(buyer.window_end));
    }

    var confirmedBox = $('buyer-confirmed');
    clear(confirmedBox);
    var confirmed = data.confirmed || [];
    if (!confirmed.length) {
      confirmedBox.appendChild(el('p', 'empty',
        'Nothing confirmed yet. An order confirms when its driver and every farmer on it accept.'));
    } else {
      confirmed.forEach(function (row) { confirmedBox.appendChild(orderRow(row)); });
    }

    var pendingBox = $('buyer-incoming');
    clear(pendingBox);
    var pending = data.pending || [];
    if (!pending.length) {
      pendingBox.appendChild(el('p', 'empty', 'No pending orders.'));
    } else {
      pending.forEach(function (row) { pendingBox.appendChild(orderRow(row)); });
    }

    var m = ensureMap('map-buyer');
    if (m) {
      var g = S.layers['map-buyer'];
      g.clearLayers();
      var pts = [];
      if (buyer.lat) {
        pts.push([buyer.lat, buyer.lng]);
        L.marker([buyer.lat, buyer.lng], { icon: pin('#0b5394', 'B', 30) })
          .bindPopup('<b>' + buyer.name + '</b>').addTo(g);
      }
      (data.incoming || []).forEach(function (row) {
        var line = (row.route || []).map(function (s) { return [s.lat, s.lng]; });
        line.forEach(function (pt) { pts.push(pt); });
        if (line.length > 1) {
          L.polyline(line, {
            color: truckColour(row.truck_id), weight: 5,
            opacity: row.confirmed ? 0.9 : 0.45, dashArray: row.confirmed ? null : '8 8'
          }).bindTooltip(row.truck_id, { permanent: true, direction: 'center', className: 'route-label' })
            .addTo(g);
        }
      });
      fitTo('map-buyer', pts);
    }

    buildDemandForm(data);
  }

  function buildDemandForm(data) {
    var buyer = data.buyer || {};
    var select = $('demand-crop');
    if (!select.childElementCount) {
      (data.crops || []).forEach(function (crop) {
        var o = document.createElement('option');
        o.value = crop;
        o.textContent = cropName(crop);
        select.appendChild(o);
      });
      if (buyer.crops_wanted && buyer.crops_wanted.length) select.value = buyer.crops_wanted[0];
      $('demand-kg').value = buyer.demand_kg || '';
      $('demand-start').value = buyer.window_start;
      $('demand-end').value = buyer.window_end;
      syncDemandPrice();
    }
    var current = $('demand-current');
    clear(current);
    var prices = data.prices || {};
    Object.keys(prices).sort().forEach(function (crop) {
      current.appendChild(el('span', null, cropName(crop) + ' ' + fmtINR(prices[crop], 2) + '/kg'));
    });
  }

  function syncDemandPrice() {
    var data = S.buyerData || {};
    var crop = $('demand-crop').value;
    var prices = data.prices || {};
    if (document.activeElement !== $('demand-price')) {
      $('demand-price').value = prices[crop] !== undefined ? prices[crop] : '';
    }
    $('demand-price-hint').textContent = prices[crop] !== undefined
      ? 'You currently offer ' + fmtINR(prices[crop], 2) + '/kg for ' + cropName(crop).toLowerCase() + '.'
      : 'You are not buying ' + cropName(crop).toLowerCase() + ' yet. Posting adds it.';
  }

  /* ---- Coordinator trip board -------------------------------------- */
  var REASON_LABEL = {
    price: 'price', capacity: 'capacity', window: 'spoils first',
    no_buyer: 'no buyer', no_truck: 'no truck', other: 'other'
  };

  function renderCoordinator(data) {
    var board = $('trip-board');
    if (!board) return;
    clear(board);
    (data.trips || []).forEach(function (row) {
      var card = el('div', 'tripcard');
      card.style.borderLeftColor = truckColour(row.truck_id);
      var head = el('div', 'tripcard__head');
      head.appendChild(el('span', 'tripcard__id', row.truck_id));
      var label = row.status === 'proposed'
        ? (row.driver_accepted ? 'driver in' : 'proposed') : row.status.replace(/_/g, ' ');
      head.appendChild(el('span', 'statuspill statuspill--' +
        (row.status === 'proposed' && row.driver_accepted ? 'accepted' : row.status), label));
      card.appendChild(head);
      var line1 = el('div', 'tripcard__line');
      line1.innerHTML = row.buyer_name
        ? '→ <b>' + row.buyer_name + '</b>'
        : '<i>no trip this round</i>';
      card.appendChild(line1);
      if (row.pickups) {
        var line2 = el('div', 'tripcard__line');
        line2.innerHTML = 'driver <b>' + (row.driver_accepted ? '✓' : '…') + '</b> · farmers <b>' +
          row.farmers_accepted + '/' + row.pickups + '</b> · <b>' + fmtNum(row.load_kg, 0) +
          '</b> kg · fee <b>' + fmtINR(row.fee, 0) + '</b>';
        card.appendChild(line2);
        var line3 = el('div', 'tripcard__line',
          fmtNum(row.distance_km, 1) + ' km · delivers ' + (row.delivery_clock || '—') +
          (row.picked_up ? ' · ' + row.picked_up + '/' + row.pickups + ' collected' : ''));
        card.appendChild(line3);
      }
      board.appendChild(card);
    });

    var unmatched = data.unmatched || [];
    var body = $('unmatched-body');
    if (body) {
      clear(body);
      $('unmatched-count').textContent = unmatched.length
        ? '— ' + unmatched.length + ' lots, ' +
          fmtKg(unmatched.reduce(function (s, r) { return s + r.quantity_kg; }, 0))
        : '— none';
      if (!unmatched.length) {
        var tr0 = el('tr');
        var td0 = el('td', 'wrap', 'Every active lot is on a truck.');
        td0.colSpan = 5;
        tr0.appendChild(td0);
        body.appendChild(tr0);
      }
      unmatched.forEach(function (r) {
        var tr = el('tr');
        tr.id = 'unmatched-' + r.lot_id;
        tr.appendChild(el('td', null, r.lot_id + ' ' + r.name));
        tr.appendChild(el('td', null, cropName(r.crop)));
        tr.appendChild(el('td', 'num', fmtNum(r.quantity_kg, 0)));
        var why = el('td', 'wrap');
        why.appendChild(el('span', 'reasontag reasontag--' + r.reason_code,
          REASON_LABEL[r.reason_code] || r.reason_code));
        why.appendChild(document.createTextNode(r.reason));
        tr.appendChild(why);
        tr.appendChild(el('td', 'num',
          r.reason_code === 'price'
            ? fmtINR(r.min_price_per_kg, 2) + ' / ' + fmtINR(r.best_offer_per_kg, 2)
            : '—'));
        body.appendChild(tr);
      });
    }

    var note = $('trips-note');
    if (note) {
      var p = data.plan || {};
      note.textContent = 'v' + S.version + ' · ' + p.farmers_served + ' lots on trucks · ' +
        fmtNum(p.kg_moved, 0) + ' kg · ' + p.unserved + ' unmatched · ' +
        (p.solver_status || '');
      if (window.kisanDashboard && window.kisanDashboard.booted()) {
        var btn = el('button', 'btn btn--ghost', 'Refresh dashboard');
        btn.style.marginLeft = '.8rem';
        btn.onclick = function () { window.kisanDashboard.reload(); };
        note.appendChild(btn);
      }
    }
  }

  function render() {
    if (!S.user || !S.data) return;
    renderAppbar();
    if (S.user.role === 'farmer') renderFarmer(S.data);
    else if (S.user.role === 'driver') renderDriver(S.data);
    else if (S.user.role === 'buyer') renderBuyer(S.data);
    else if (S.user.role === 'coordinator') renderCoordinator(S.data);
  }

  function translateStatic() {
    Array.prototype.forEach.call(document.querySelectorAll('[data-i18n]'), function (node) {
      node.textContent = t(node.getAttribute('data-i18n'));
    });
  }

  /* ------------------------------------------------------------------ */
  /* Polling                                                            */
  /* ------------------------------------------------------------------ */
  function tick(force) {
    if (!S.token) return Promise.resolve();
    if (S.inflight) {
      // An action wants fresh state while a routine poll is in flight: run
      // again as soon as it lands rather than waiting a whole poll interval.
      if (force) S.forceAgain = true;
      return Promise.resolve();
    }
    S.inflight = true;
    var path = '/api/me/state' + (force || S.version < 0 ? '' : '?since=' + S.version);
    return api('GET', path).then(function (env) {
      hideError();
      S.planning = !!env.planning;
      S.user = env.user;
      show($('appbar-planning'), S.planning);
      renderBell(env.notifications || [], env.unread || 0);
      if (env.changed) {
        S.version = env.version;
        S.data = env.data;
        render();
      }
    }).catch(function (err) {
      if (err.status === 401) { logout(true); return; }
      showError(err.message);
    }).then(function () {
      S.inflight = false;
      if (S.forceAgain) { S.forceAgain = false; tick(true); }
    });
  }

  /* ---- Notifications ------------------------------------------------ */
  function ago(iso) {
    var then = Date.parse(iso);
    if (!isFinite(then)) return '';
    var secs = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (secs < 60) return secs + 's ago';
    if (secs < 3600) return Math.round(secs / 60) + ' min ago';
    return Math.round(secs / 3600) + ' h ago';
  }

  function renderBell(items, unread) {
    var count = $('bell-count');
    count.textContent = unread > 9 ? '9+' : String(unread);
    show(count, unread > 0);

    // First envelope after sign-in: everything already in the inbox is old
    // news, so it is listed in the bell but not toasted.
    var fresh = [];
    if (S.seen === null) {
      S.seen = {};
      items.forEach(function (n) { S.seen[n.id] = true; });
    } else {
      items.forEach(function (n) {
        if (!S.seen[n.id]) { S.seen[n.id] = true; fresh.push(n); }
      });
    }
    fresh.slice(0, 3).reverse().forEach(toast);

    var list = $('bell-list');
    clear(list);
    if (!items.length) {
      list.appendChild(el('li', 'bell__empty', 'Nothing yet.'));
      return;
    }
    items.forEach(function (n) {
      var li = el('li', n.read ? '' : 'is-unread');
      li.appendChild(el('div', 'bell__title', n.title));
      li.appendChild(el('div', 'bell__body', n.body));
      li.appendChild(el('div', 'bell__time', ago(n.created_at)));
      list.appendChild(li);
    });
  }

  var MAX_TOASTS = 2;
  function toast(n) {
    var holder = $('toasts');
    var box = el('div', 'toast toast--' + (n.kind || 'info'));
    box.setAttribute('role', 'status');
    box.appendChild(el('div', 'toast__title', n.title));
    box.appendChild(el('div', 'toast__body', n.body));
    holder.appendChild(box);
    // Only the newest news matters; an older toast is superseded, not queued.
    while (holder.children.length > MAX_TOASTS) holder.removeChild(holder.firstChild);
    window.setTimeout(function () {
      if (box.parentNode) box.parentNode.removeChild(box);
    }, 5000);
  }

  function markAllRead() {
    api('POST', '/api/me/notifications/read', {})
      .then(function () { return tick(true); })
      .catch(function (e) { showError(e.message); });
  }

  function startPolling() {
    stopPolling();
    tick(true);
    S.poll = window.setInterval(function () { tick(false); }, POLL_MS);
  }
  function stopPolling() {
    if (S.poll) { window.clearInterval(S.poll); S.poll = null; }
  }

  /* ------------------------------------------------------------------ */
  /* Actions                                                            */
  /* ------------------------------------------------------------------ */
  function tripAction(what) {
    var id = S.data && S.data.trip ? S.data.trip.truck_id : (S.user && S.user.ref_id);
    if (!id) return;
    Array.prototype.forEach.call($('driver-actions').querySelectorAll('button'), function (b) {
      b.disabled = true;
    });
    api('POST', '/api/trips/' + id + '/' + what)
      .then(function () { return tick(true); })
      .catch(function (e) { showError(e.message); tick(true); });
  }
  function matchAction(lotId, what) {
    ['btn-match-accept', 'btn-match-decline'].forEach(function (id) {
      if ($(id)) $(id).disabled = true;
    });
    api('POST', '/api/me/lots/' + lotId + '/' + what)
      .then(function () { return tick(true); })
      .catch(function (e) { showError(e.message); tick(true); });
  }

  function savePrice(lotId, price) {
    $('btn-save-price').disabled = true;
    api('POST', '/api/me/lots/' + lotId + '/price', { min_price_per_kg: price })
      .then(function () {
        S.editingPrice = null;
        S.planning = true;
        show($('appbar-planning'), true);
        return tick(true);
      })
      .catch(function (e) { showError(e.message); })
      .then(function () { $('btn-save-price').disabled = false; });
  }

  function updateLotPriceHint() {
    var crop = $('lot-crop').value;
    var info = (S.market || {})[crop];
    var hint = $('lot-price-hint');
    if (!hint) return;
    hint.textContent = info && info.best_offer_per_kg
      ? t('price_hint', { crop: cropName(crop).toLowerCase(), best: fmtINR(info.best_offer_per_kg, 2) })
      : '';
  }

  function pickupStop(farmerId) {
    var id = S.data && S.data.trip ? S.data.trip.truck_id : (S.user && S.user.ref_id);
    api('POST', '/api/trips/' + id + '/stops/' + farmerId + '/pickup')
      .then(function () { return tick(true); })
      .catch(function (e) { showError(e.message); });
  }

  /* ------------------------------------------------------------------ */
  /* Auth                                                               */
  /* ------------------------------------------------------------------ */
  function enter(auth) {
    S.token = auth.token;
    S.user = auth.user;
    store(TOKEN_KEY, auth.token);
    hideError();
    showView(auth.user.role);
    renderAppbar();
    translateStatic();
    if (auth.user.role === 'coordinator' && window.kisanDashboard) {
      window.kisanDashboard.boot();
    }
    if (auth.user.role === 'farmer') prepareLotForm();
    S.version = -1;
    startPolling();
    window.setTimeout(function () {
      Object.keys(S.maps).forEach(function (k) { S.maps[k].invalidateSize(); });
    }, 250);
  }

  function logout(silent) {
    stopPolling();
    // Fire the revoke first: api() builds the Authorization header synchronously
    // from S.token, so clearing it beforehand would send an unauthenticated
    // request and get a 401 back.
    if (!silent && S.token) {
      api('POST', '/api/auth/logout').catch(function () { /* already gone */ });
    }
    S.token = null; S.user = null; S.data = null; S.version = -1;
    S.seen = null; S.editingPrice = null; S.pinnedLot = null; S.bellOpen = false;
    show($('bell-menu'), false);
    clear($('toasts'));
    store(TOKEN_KEY, null);
    showView(null);
  }

  function authError(message) {
    var node = $('auth-error');
    node.textContent = message;
    show(node, true);
  }

  function doLogin(username, password) {
    show($('auth-error'), false);
    return api('POST', '/api/auth/login', { username: username, password: password })
      .then(enter)
      .catch(function (e) { authError(e.message); });
  }

  function loadDemoChips() {
    api('GET', '/api/auth/demo-accounts').then(function (info) {
      var box = $('demo-chips');
      clear(box);
      (info.chips || []).forEach(function (chip) {
        var b = el('button', 'chip');
        b.type = 'button';
        b.appendChild(document.createTextNode(chip.label));
        b.appendChild(el('small', null, chip.username));
        b.onclick = function () {
          $('login-username').value = chip.username;
          $('login-password').value = info.password;
          doLogin(chip.username, info.password);
        };
        box.appendChild(b);
      });
    }).catch(function () { /* chips are a convenience, not a requirement */ });
  }

  /* ------------------------------------------------------------------ */
  /* Forms                                                              */
  /* ------------------------------------------------------------------ */
  /** Fill the crop dropdown from the same payload the card renders from, so
      the form can never be submitted before it has options. */
  function fillCropSelect(crops) {
    var select = $('lot-crop');
    if (!crops || select.childElementCount) return;
    Object.keys(crops).sort().forEach(function (crop) {
      var o = document.createElement('option');
      o.value = crop;
      o.textContent = cropName(crop) + ' \u00b7 ' + crops[crop].spoilage_hours + ' h';
      select.appendChild(o);
    });
    if (crops.tomato) select.value = 'tomato';
  }

  function prepareLotForm() { fillCropSelect(S.market); }

  function wireForms() {
    $('form-login').addEventListener('submit', function (e) {
      e.preventDefault();
      doLogin($('login-username').value.trim(), $('login-password').value);
    });

    $('auth-tabs').addEventListener('click', function (e) {
      var btn = e.target.closest('button[data-auth-tab]');
      if (!btn) return;
      var isLogin = btn.dataset.authTab === 'login';
      Array.prototype.forEach.call(this.children, function (b) {
        b.classList.toggle('is-active', b === btn);
      });
      show($('form-login'), isLogin);
      show($('auth-demo'), isLogin);
      show($('form-register'), !isLogin);
      show($('auth-error'), false);
      if (!isLogin) {
        var m = ensureMap('map-register');
        if (m) {
          window.setTimeout(function () { m.invalidateSize(); m.setView(NAGPUR, 10); }, 60);
          if (!m.__picker) {
            m.__picker = true;
            m.on('click', function (ev) {
              S.pickedLatLng = ev.latlng;
              S.layers['map-register'].clearLayers();
              L.circleMarker(ev.latlng, {
                radius: 11, color: '#fff', weight: 3, fillColor: '#2f9e44', fillOpacity: 1
              }).addTo(S.layers['map-register']);
              var node = $('register-coords');
              node.textContent = 'Farm pinned at ' + ev.latlng.lat.toFixed(5) + ', ' +
                ev.latlng.lng.toFixed(5);
              node.classList.add('is-set');
            });
          }
        }
      }
    });

    $('form-register').addEventListener('submit', function (e) {
      e.preventDefault();
      if (!S.pickedLatLng) {
        authError('Click the map to mark where your farm is before creating the account.');
        return;
      }
      api('POST', '/api/auth/register', {
        username: $('reg-username').value.trim().toLowerCase(),
        password: $('reg-password').value,
        name: $('reg-name').value.trim(),
        village: $('reg-village').value.trim(),
        phone: $('reg-phone').value.trim(),
        lat: S.pickedLatLng.lat,
        lng: S.pickedLatLng.lng
      }).then(enter).catch(function (err) { authError(err.message); });
    });

    $('form-lot').addEventListener('submit', function (e) {
      e.preventDefault();
      var body = {
        crop: $('lot-crop').value,
        quantity_kg: parseInt($('lot-kg').value, 10),
        min_price_per_kg: parseFloat($('lot-price').value || '0'),
        harvest_time: parseInt($('lot-harvest').value, 10)
      };
      var from = $('lot-from').value, until = $('lot-until').value;
      if (from !== '') body.ready_from = parseInt(from, 10);
      if (until !== '') body.ready_until = parseInt(until, 10);
      var msg = $('lot-msg');
      $('btn-post-lot').disabled = true;
      api('POST', '/api/me/lots', body).then(function (res) {
        // Show the lot that was just posted, not whichever older one ranks first.
        if (res && res.lot) S.pinnedLot = res.lot.id;
        msg.textContent = t('posted_ok');
        msg.classList.remove('is-bad');
        show(msg, true);
        S.planning = true;
        show($('appbar-planning'), true);
        return tick(true);
      }).catch(function (err) {
        msg.textContent = err.message;
        msg.classList.add('is-bad');
        show(msg, true);
      }).then(function () { $('btn-post-lot').disabled = false; });
    });

    $('form-demand').addEventListener('submit', function (e) {
      e.preventDefault();
      var data = S.buyerData || {};
      var buyer = data.buyer || {};
      var crop = $('demand-crop').value;
      var price = parseFloat($('demand-price').value);
      var msg = $('demand-msg');
      if (!crop || !isFinite(price) || price <= 0) {
        msg.textContent = 'Pick a crop and a price above zero.';
        msg.classList.add('is-bad');
        show(msg, true);
        return;
      }
      var wanted = (buyer.crops_wanted || []).slice();
      if (wanted.indexOf(crop) < 0) wanted.push(crop);
      var prices = {};
      prices[crop] = price;
      $('btn-post-demand').disabled = true;
      api('POST', '/api/me/demand', {
        crops_wanted: wanted,
        price_per_kg: prices,
        demand_kg: parseInt($('demand-kg').value, 10),
        window_start: parseInt($('demand-start').value, 10),
        window_end: parseInt($('demand-end').value, 10)
      }).then(function () {
        msg.textContent = 'Demand posted: ' + cropName(crop).toLowerCase() + ' at ' +
          fmtINR(price, 2) + '/kg. The planner is re-optimising now.';
        msg.classList.remove('is-bad');
        show(msg, true);
        S.planning = true;
        show($('appbar-planning'), true);
        return tick(true);
      }).catch(function (err) {
        msg.textContent = err.message;
        msg.classList.add('is-bad');
        show(msg, true);
      }).then(function () { $('btn-post-demand').disabled = false; });
    });

    $('demand-crop').addEventListener('change', syncDemandPrice);

    $('form-price').addEventListener('submit', function (e) {
      e.preventDefault();
      var lot = S.activeLot;
      var price = parseFloat($('price-input').value);
      if (!lot || !isFinite(price) || price < 0) return;
      savePrice(lot.lot_id, price);
    });

    $('lot-crop').addEventListener('change', updateLotPriceHint);

    $('bell-btn').addEventListener('click', function (e) {
      e.stopPropagation();
      S.bellOpen = !S.bellOpen;
      show($('bell-menu'), S.bellOpen);
      $('bell-btn').setAttribute('aria-expanded', S.bellOpen ? 'true' : 'false');
      // Opening the bell is reading it.
      if (S.bellOpen && !$('bell-count').hidden) markAllRead();
    });
    $('bell-clear').addEventListener('click', function (e) {
      e.stopPropagation();
      markAllRead();
    });
    document.addEventListener('click', function (e) {
      if (S.bellOpen && !$('bell').contains(e.target)) {
        S.bellOpen = false;
        show($('bell-menu'), false);
        $('bell-btn').setAttribute('aria-expanded', 'false');
      }
    });

    $('link-register').addEventListener('click', function (e) {
      e.preventDefault();
      var tab = document.querySelector('#auth-tabs button[data-auth-tab="register"]');
      if (tab) tab.click();
    });

    $('lot-harvest').addEventListener('input', function () {
      var v = parseInt(this.value, 10);
      $('lot-harvest-hint').textContent = isFinite(v) ? '= ' + clock(v) : '';
    });

    $('btn-logout').addEventListener('click', function () { logout(false); });

    $('lang-select').addEventListener('change', function () {
      S.lang = this.value;
      store(LANG_KEY, S.lang);
      translateStatic();
      render();
    });

    $('error-close').addEventListener('click', hideError);
  }

  /* ------------------------------------------------------------------ */
  /* Boot                                                               */
  /* ------------------------------------------------------------------ */
  function boot() {
    S.lang = recall(LANG_KEY) || 'en';
    $('lang-select').value = S.lang;
    wireForms();
    translateStatic();
    loadDemoChips();

    var token = recall(TOKEN_KEY);
    if (!token) { showView(null); return; }
    S.token = token;
    api('GET', '/api/auth/me')
      .then(function (user) { enter({ token: token, user: user }); })
      .catch(function () { logout(true); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
