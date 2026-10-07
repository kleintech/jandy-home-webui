/* Pool & Spa guest UI — vanilla JS, no build step.
 *
 * Model:
 *   state      last full state object from the server (GET /api/state or any POST)
 *   overrides  local values the user has set that the server hasn't confirmed yet,
 *              keyed by dotted path ("pool.heat_set"). Rendering reads overrides
 *              first, so a poll can never clobber a value the user just set.
 *   groups     one per POST endpoint; each serialises its own requests (one in
 *              flight at a time, latest value wins) and clears its overrides when
 *              the server answers (success: re-render from the response; error:
 *              roll back + toast).
 *   epoch      bumped whenever a POST starts/finishes; a poll that straddled one
 *              is discarded so it can't paint pre-change state over the response.
 */
(() => {
  'use strict';

  const POLL_MS = 5000;
  const GET_TIMEOUT_MS = 10000;
  const POST_TIMEOUT_MS = 60000;   // a mode change can take ~30 s on real hardware
  const SLIDER_DEBOUNCE_MS = 400;
  const FAILS_BEFORE_OFFLINE = 2;

  const $ = (id) => document.getElementById(id);
  const el = {
    body: document.body,
    conn: $('conn'),
    banner: $('banner'),
    modePool: $('mode-pool'),
    modeSpa: $('mode-spa'),
    busy: $('busy'),
    busyText: $('busy-text'),
    lightBlock: $('light-block'),
    light: $('t-light'),
    colorBtn: $('light-color'),
    colorSwatch: $('light-swatch'),
    sheet: $('color-sheet'),
    sheetClose: $('color-close'),
    chips: $('chips'),
    spaPanel: $('spa-panel'),
    poolPanel: $('pool-panel'),
    waterCur: $('water-cur'),
    airCur: $('air-cur'),
    spaCol: $('spa-col'),
    spaSet: $('spa-set'),
    spaSlider: $('spa-slider'),
    spaWrap: $('spa-slider-wrap'),
    spaMin: $('spa-min'),
    spaMax: $('spa-max'),
    bubbles: $('t-bubbles'),
    poolSp: $('pool-sp'),
    poolWrap: $('pool-wrap'),
    poolBar: $('pool-bar'),
    poolMin: $('pool-min'),
    poolMax: $('pool-max'),
    chillCol: $('chill-col'),
    chillNum: $('chill-num'),
    chillSlider: $('chill-slider'),
    heatNum: $('heat-num'),
    heatSlider: $('heat-slider'),
    spreadNote: $('spread-note'),
    spill: $('t-spill'),
    spillHint: $('spill-hint'),
    wf: $('t-wf'),
    wfHint: $('wf-hint'),
    toast: $('toast'),
  };

  let state = null;
  let reachable = true;       // last GET /api/state reached the backend
  let fetchFailures = 0;
  let epoch = 0;
  let lastRequestedMode = null;
  const overrides = Object.create(null);
  const dragging = new Set(); // group names with a slider currently being dragged

  // ---------- helpers ----------
  const getPath = (obj, path) =>
    path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
  const view = (path) => (path in overrides ? overrides[path] : getPath(state, path));
  const num = (v) => (typeof v === 'number' && isFinite(v) ? v : null);
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
  const unit = () => '°' + ((state && state.unit) || 'F');

  function setTemp(node, value) {
    const v = num(value);
    node.textContent = '';
    node.append(v === null ? '--' : String(Math.round(v)));
    if (v !== null) {
      const u = document.createElement('span');
      u.className = 'unit';
      u.textContent = unit();
      node.append(u);
    }
  }

  // ---------- network ----------
  async function api(method, url, body, timeoutMs) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const res = await fetch(url, {
        method,
        cache: 'no-store',
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        signal: ctl.signal,
      });
      let data = null;
      try { data = await res.json(); } catch (_) { /* non-JSON body */ }
      if (!res.ok) {
        let msg = `Request failed (${res.status})`;
        if (data && typeof data.detail === 'string') msg = data.detail;
        else if (data && Array.isArray(data.detail) && data.detail[0] && data.detail[0].msg) msg = data.detail[0].msg;
        const err = new Error(msg);
        err.status = res.status;
        throw err;
      }
      if (!data || typeof data !== 'object') throw new Error('Unexpected response from server');
      return data;
    } catch (e) {
      if (e && e.name === 'AbortError') throw new Error('The pool controller took too long to answer');
      if (e instanceof TypeError) throw new Error("Couldn't reach the server");
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }

  function applyState(s) {
    state = s;
    if (!s.busy && !groups.mode.inflight && !groups.mode.timer) lastRequestedMode = null;
    render();
  }

  // ---------- polling ----------
  let pollTimer = null;
  let polling = false;

  async function poll() {
    clearTimeout(pollTimer);
    pollTimer = null;
    if (polling) return;
    polling = true;
    const startEpoch = epoch;
    try {
      const s = await api('GET', '/api/state', null, GET_TIMEOUT_MS);
      reachable = true;
      fetchFailures = 0;
      if (startEpoch === epoch) applyState(s);  // else a POST raced us; its response wins
      else render();
    } catch (_) {
      fetchFailures += 1;
      if (fetchFailures >= FAILS_BEFORE_OFFLINE || !state) reachable = false;
      render();
    } finally {
      polling = false;
      schedulePoll();
    }
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = document.hidden ? null : setTimeout(poll, POLL_MS);
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { clearTimeout(pollTimer); pollTimer = null; }
    else poll();
  });
  window.addEventListener('pageshow', (e) => { if (e.persisted) poll(); });

  // ---------- mutations ----------
  const groups = {
    mode:    { label: 'switch mode',          keys: ['mode'],             req: () => ['/api/mode', { mode: view('mode') }] },
    light:   { label: 'change the light',     keys: ['light.on'],         req: () => ['/api/light', { on: !!view('light.on') }] },
    color:   { label: 'change the light color', keys: ['light.color', 'light.on'], req: () => ['/api/light/color', { color: view('light.color') }] },
    spaSet:  { label: 'set the spa temperature', keys: ['spa.set_temp'],  req: () => ['/api/spa/setpoint', { set_temp: view('spa.set_temp') }] },
    bubbles: { label: 'change Bubbles',       keys: ['spa.bubbles'],      req: () => ['/api/spa/bubbles', { on: !!view('spa.bubbles') }] },
    poolSet: {
      label: 'set the pool temperature',
      keys: ['pool.heat_set', 'pool.chill_set'],
      req: () => {
        const body = { heat_set: view('pool.heat_set') };
        if (state.pool && state.pool.chill_supported) body.chill_set = view('pool.chill_set');
        return ['/api/pool/setpoints', body];
      },
    },
    spill:   { label: 'change Spillover',     keys: ['pool.spillover'],   req: () => ['/api/pool/spillover', { on: !!view('pool.spillover') }] },
    wf:      { label: 'change Water Features', keys: ['pool.water_features'], req: () => ['/api/pool/water_features', { on: !!view('pool.water_features') }] },
  };
  for (const g of Object.values(groups)) { g.timer = null; g.inflight = false; g.dirty = false; }

  const isPending = (name) => {
    const g = groups[name];
    return g.inflight || g.timer !== null;
  };

  function clearOverrides(name) {
    for (const k of groups[name].keys) delete overrides[k];
  }

  /** Queue a POST for this group (after `delay` ms). Latest local value wins. */
  function request(name, delay) {
    const g = groups[name];
    epoch += 1;
    clearTimeout(g.timer);
    g.timer = setTimeout(() => { g.timer = null; flush(name); }, delay || 0);
    render();
  }

  async function flush(name) {
    const g = groups[name];
    if (g.inflight) { g.dirty = true; return; }
    if (!state) return;
    g.inflight = true;
    g.dirty = false;
    epoch += 1;
    render();
    const [url, body] = g.req();
    // Keep overrides if the user changed it again meanwhile (queued or dragging).
    const settled = () => !g.dirty && g.timer === null && !dragging.has(name);
    try {
      const s = await api('POST', url, body, POST_TIMEOUT_MS);
      reachable = true;
      fetchFailures = 0;
      if (settled()) clearOverrides(name);
      g.inflight = false;
      epoch += 1;
      applyState(s);
    } catch (e) {
      if (settled()) clearOverrides(name);   // roll back to server state
      g.inflight = false;
      epoch += 1;
      toast(`Couldn't ${g.label}: ${e.message}`);
      render();
      poll();                                 // re-sync with what actually happened
    }
    if (g.dirty) flush(name);
  }

  function toggle(name, path) {
    if (!controlsEnabled()) return;
    overrides[path] = !view(path);
    request(name, 0);
  }

  // ---------- toast ----------
  let toastTimer = null;
  function toast(msg) {
    el.toast.textContent = msg;
    el.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.toast.hidden = true; }, 4500);
  }
  el.toast.addEventListener('click', () => { el.toast.hidden = true; });

  // ---------- derived limits ----------
  // Heat is the LOW set point, Chill the HIGH one; chill >= heat + spread.
  // Ranges come from the server; anything missing falls back to the other side's
  // limits (or wide defaults) so the sliders stay usable on an older backend.
  function poolLimits() {
    const p = state.pool;
    const spread = Math.max(0, num(p.min_spread) ?? 0);
    if (!p.chill_supported) {
      const lo = num(p.heat_min) ?? 40;
      return { chill: false, spread, heatLo: lo, heatHi: Math.max(lo, num(p.heat_max) ?? 104) };
    }
    const heatLo = num(p.heat_min) ?? (num(p.chill_min) !== null ? num(p.chill_min) - spread : 50);
    const chillHi = num(p.chill_max) ?? (num(p.heat_max) !== null ? num(p.heat_max) + spread : 104);
    const heatHi = Math.max(heatLo, num(p.heat_max) ?? chillHi - spread);
    const chillLo = Math.min(chillHi, num(p.chill_min) ?? heatLo + spread);
    return { chill: true, spread, heatLo, heatHi, chillLo, chillHi };
  }

  // ---------- events ----------
  function controlsEnabled() {
    return !!state && state.connected !== false && reachable;
  }

  for (const b of [el.modePool, el.modeSpa]) {
    b.addEventListener('click', () => {
      const target = b.dataset.target;
      if (!controlsEnabled() || state.busy || isPending('mode')) return;
      if (view('mode') === target) return;
      overrides.mode = target;
      lastRequestedMode = target;
      request('mode', 0);
    });
  }

  el.light.addEventListener('click', () => toggle('light', 'light.on'));
  el.bubbles.addEventListener('click', () => toggle('bubbles', 'spa.bubbles'));
  el.spill.addEventListener('click', () => {
    if (view('pool.water_features')) return;
    toggle('spill', 'pool.spillover');
  });
  el.wf.addEventListener('click', () => {
    if (view('pool.spillover')) return;
    toggle('wf', 'pool.water_features');
  });

  // ---------- color sheet ----------
  function openSheet() {
    if (!controlsEnabled() || el.sheet.open) return;
    if (typeof el.sheet.showModal === 'function') el.sheet.showModal();
    else el.sheet.setAttribute('open', '');   // very old browsers: shown inline
    const sel = el.chips.querySelector('.chip[aria-pressed="true"]') || el.chips.querySelector('.chip');
    if (sel) sel.focus();
  }
  function closeSheet() {
    if (!el.sheet.open) return;
    if (typeof el.sheet.close === 'function') el.sheet.close();
    else el.sheet.removeAttribute('open');
  }
  el.colorBtn.addEventListener('click', openSheet);
  el.sheetClose.addEventListener('click', closeSheet);
  // A tap on the backdrop lands on the <dialog> itself (the content fills it
  // edge to edge), so anything whose target is the dialog is "outside".
  el.sheet.addEventListener('click', (e) => { if (e.target === el.sheet) closeSheet(); });
  el.sheet.addEventListener('close', () => { if (document.activeElement === document.body) el.colorBtn.focus(); });

  el.chips.addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip || !controlsEnabled()) return;
    const color = chip.dataset.color;
    closeSheet();
    if (color === view('light.color') && view('light.on')) return;
    overrides['light.color'] = color;
    overrides['light.on'] = true;   // picking a color turns the light on server-side
    request('color', 0);
  });

  function bindSlider(input, name, onInput, gestureActive) {
    input.addEventListener('input', () => {
      if (!controlsEnabled()) return;
      dragging.add(name);
      clearTimeout(groups[name].timer);   // don't fire a queued send mid-drag
      groups[name].timer = null;
      onInput(Number(input.value));
      render();
    });
    // Browsers skip `change` when the thumb is released where it started (or the
    // input was disabled mid-drag), so end the drag on every way a gesture can end.
    const end = () => {
      if (gestureActive && gestureActive()) return;   // the pool bar's pointer drag ends it
      if (!dragging.has(name) || !controlsEnabled()) return;
      dragging.delete(name);
      onInput(Number(input.value));
      const g = groups[name];
      if (g.keys.every((k) => !(k in overrides) || overrides[k] === getPath(state, k))) {
        clearOverrides(name);   // back where the server is: nothing to send
        render();
      } else {
        request(name, SLIDER_DEBOUNCE_MS);
      }
    };
    for (const ev of ['change', 'pointerup', 'pointercancel', 'touchend', 'touchcancel', 'blur']) {
      input.addEventListener(ev, end);
    }
  }

  bindSlider(el.spaSlider, 'spaSet', (v) => { overrides['spa.set_temp'] = v; });

  // ---------- pool set points: one bar, two thumbs ----------
  // Heat (low) pushes Chill (high) up; Chill pushes Heat down. A pushed thumb is
  // clamped to its own limits; if it can't make room, the dragged one stops too.
  function setHeat(v) {
    const L = poolLimits();
    let heat = clamp(v, L.heatLo, L.heatHi);
    if (L.chill) {
      let chill = num(view('pool.chill_set'));
      if (chill === null || chill < heat + L.spread) chill = heat + L.spread;
      chill = clamp(chill, L.chillLo, L.chillHi);
      if (heat > chill - L.spread) heat = Math.max(L.heatLo, chill - L.spread);
      overrides['pool.chill_set'] = chill;
    }
    overrides['pool.heat_set'] = heat;
  }
  function setChill(v) {
    const L = poolLimits();
    if (!L.chill) return;
    let chill = clamp(v, L.chillLo, L.chillHi);
    let heat = num(view('pool.heat_set'));
    if (heat === null || heat > chill - L.spread) heat = chill - L.spread;
    heat = clamp(heat, L.heatLo, L.heatHi);
    if (chill < heat + L.spread) chill = Math.min(L.chillHi, heat + L.spread);
    overrides['pool.heat_set'] = heat;
    overrides['pool.chill_set'] = chill;
  }

  // The two range inputs are drawn overlaid on one track but take no pointer
  // input (CSS pointer-events:none): keyboard and screen readers drive them
  // natively, while touch/mouse go through the bar, which always moves the thumb
  // nearest the finger. Overlaid native ranges otherwise hand the touch to
  // whichever input is on top, which grabs the wrong thumb when they're close.
  let gesture = null;   // { id, which: 'heat'|'chill'|null, startX, offset, snap }
  const gestureActive = () => gesture !== null;

  bindSlider(el.heatSlider, 'poolSet', setHeat, gestureActive);
  bindSlider(el.chillSlider, 'poolSet', setChill, gestureActive);

  function barGeom() {
    const L = poolLimits();
    const r = el.poolBar.getBoundingClientRect();
    const t = parseFloat(getComputedStyle(el.poolBar).getPropertyValue('--thumb')) || 32;
    const lo = L.heatLo;
    const hi = L.chill ? Math.max(lo, L.chillHi) : L.heatHi;
    const usable = Math.max(1, r.width - t);
    return {
      L,
      x: (v) => r.left + t / 2 + (hi > lo ? (v - lo) / (hi - lo) : 0) * usable,
      v: (x) => lo + Math.round(clamp((x - r.left - t / 2) / usable, 0, 1) * (hi - lo)),
      // This close to a thumb's centre = grab it (no jump). Kept under one
      // degree's width on a 320px phone (~22px) so a tap 1° away still jumps.
      grab: t / 2 + 4,
    };
  }
  const shownSet = (L, which) => {
    if (which === 'heat') {
      const v = num(view('pool.heat_set'));
      return clamp(v === null ? L.heatLo : v, L.heatLo, L.heatHi);
    }
    const v = num(view('pool.chill_set'));
    return clamp(v === null ? L.chillLo : v, L.chillLo, L.chillHi);
  };

  function moveThumb(which, x) {
    const v = barGeom().v(x - gesture.offset);
    if (which === 'heat') setHeat(v); else setChill(v);
    render();
  }

  function finishGesture(cancelled) {
    const g = gesture;
    gesture = null;
    try { el.poolBar.releasePointerCapture(g.id); } catch (_) { /* already released */ }
    el.poolBar.classList.remove('active');
    if (!dragging.has('poolSet') || !controlsEnabled()) { render(); return; }
    if (cancelled) {
      // The browser took the touch for scrolling (or the system did): undo what
      // this touch changed rather than send a set point nobody chose.
      for (const [k, o] of Object.entries(g.snap)) {
        if (o.has) overrides[k] = o.val; else delete overrides[k];
      }
    }
    dragging.delete('poolSet');
    const grp = groups.poolSet;
    if (grp.keys.every((k) => !(k in overrides) || overrides[k] === getPath(state, k))) {
      clearOverrides('poolSet');   // back where the server is: nothing to send
      render();
    } else {
      request('poolSet', SLIDER_DEBOUNCE_MS);
    }
  }

  el.poolBar.addEventListener('pointerdown', (e) => {
    if (gesture || !controlsEnabled() || !state || !state.pool) return;
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    if (e.pointerType === 'mouse') e.preventDefault();   // no text selection
    const G = barGeom();
    const L = G.L;
    const xh = G.x(shownSet(L, 'heat'));
    const xc = L.chill ? G.x(shownSet(L, 'chill')) : Infinity;
    const x = e.clientX;
    let which;
    if (!L.chill) which = 'heat';
    else if (Math.abs(xc - xh) < 1) {
      // Thumbs on top of each other: the side of the finger decides, or else the
      // direction of the first move.
      which = x < xh - 1 ? 'heat' : x > xc + 1 ? 'chill' : null;
    } else which = Math.abs(x - xh) <= Math.abs(x - xc) ? 'heat' : 'chill';
    const tx = which === 'chill' ? xc : xh;
    const near = Math.abs(x - tx) <= G.grab;
    const snap = {};
    for (const k of groups.poolSet.keys) snap[k] = { has: k in overrides, val: overrides[k] };
    // Grabbing a thumb keeps it under the finger (no jump); a tap on the track
    // jumps the nearest thumb there, like a native slider.
    gesture = { id: e.pointerId, which, startX: x, offset: near ? x - tx : 0, snap };
    try { el.poolBar.setPointerCapture(e.pointerId); } catch (_) { /* window listeners cover it */ }
    el.poolBar.classList.add('active');
    dragging.add('poolSet');
    clearTimeout(groups.poolSet.timer);   // don't fire a queued send mid-drag
    groups.poolSet.timer = null;
    if (which && !near) moveThumb(which, x);
    else render();
  });

  el.poolBar.addEventListener('pointermove', (e) => {
    if (!gesture || e.pointerId !== gesture.id) return;
    if (!dragging.has('poolSet')) { finishGesture(false); return; }   // dropped by render()
    if (!gesture.which) {
      const dx = e.clientX - gesture.startX;
      if (Math.abs(dx) < 3) return;
      gesture.which = dx > 0 ? 'chill' : 'heat';
    }
    moveThumb(gesture.which, e.clientX);
  });

  const onUp = (e) => { if (gesture && e.pointerId === gesture.id) finishGesture(false); };
  const onCancel = (e) => { if (gesture && e.pointerId === gesture.id) finishGesture(true); };
  el.poolBar.addEventListener('pointerup', onUp);
  el.poolBar.addEventListener('pointercancel', onCancel);
  el.poolBar.addEventListener('lostpointercapture', onUp);
  window.addEventListener('pointerup', onUp, true);
  window.addEventListener('pointercancel', onCancel, true);

  // ---------- rendering ----------
  // Exact names first (Jandy WaterColors / Colors, Pentair, Hayward), then substrings.
  const SHOW = 'conic-gradient(#ef4444, #f59e0b, #22c55e, #06b6d4, #3b82f6, #d946ef, #ef4444)';
  const SWATCHES = {
    'sky blue': '#7dd3fc',
    'cobalt blue': '#1d4ed8',
    'caribbean blue': '#06b6d4',
    'spring green': '#4ade80',
    'emerald green': '#059669',
    'emerald rose': '#f43f5e',
    'garnet red': '#9f1239',
    'ruby red': '#be123c',
    white: '#f8fafc',
    blue: '#2563eb',
    green: '#16a34a',
    red: '#dc2626',
    rose: '#f43f5e',
    magenta: '#d946ef',
    violet: '#8b5cf6',
    purple: '#7c3aed',
    lavender: '#a78bfa',
    sunset: 'linear-gradient(#f97316, #db2777)',
    'light show': SHOW,
    splash: SHOW,
    show: SHOW,
    party: SHOW,
    'usa': 'linear-gradient(#dc2626 33%, #f8fafc 33% 66%, #2563eb 66%)',
    america: 'linear-gradient(#dc2626 33%, #f8fafc 33% 66%, #2563eb 66%)',
    'fat tuesday': 'conic-gradient(#7c3aed, #16a34a, #eab308, #7c3aed)',
    disco: SHOW,
  };
  const swatchFor = (name) => {
    const key = String(name).toLowerCase();
    if (SWATCHES[key]) return SWATCHES[key];
    for (const [k, v] of Object.entries(SWATCHES)) if (key.includes(k)) return v;
    return 'conic-gradient(#94a3b8, #e2e8f0, #94a3b8)';
  };

  let renderedColors = '';
  function renderChips(colors, current, enabled, pending) {
    const sig = JSON.stringify(colors);
    if (sig !== renderedColors) {
      renderedColors = sig;
      el.chips.textContent = '';
      for (const c of colors) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'chip';
        b.dataset.color = c;
        b.setAttribute('aria-label', c);
        const sw = document.createElement('span');
        sw.className = 'chip-swatch';
        sw.style.setProperty('--sw', swatchFor(c));
        const nm = document.createElement('span');
        nm.className = 'chip-name';
        nm.textContent = c;
        nm.setAttribute('aria-hidden', 'true');
        b.append(sw, nm);
        el.chips.append(b);
      }
    }
    for (const b of el.chips.children) {
      const sel = b.dataset.color === current;
      b.setAttribute('aria-pressed', String(sel));
      b.disabled = !enabled;
      b.classList.toggle('pending', sel && pending);
    }
  }

  function renderToggle(btn, on, { enabled = true, pending = false } = {}) {
    btn.setAttribute('aria-pressed', String(!!on));
    btn.disabled = !enabled;
    btn.classList.toggle('pending', pending);
    if (pending) btn.setAttribute('aria-busy', 'true'); else btn.removeAttribute('aria-busy');
  }

  function renderSlider(input, wrap, lo, hi, value, { enabled, pending, minEl, maxEl }) {
    input.min = String(lo);
    input.max = String(hi);
    const v = num(value);
    const shown = v === null ? lo : clamp(v, lo, hi);
    // Don't fight the browser while the thumb is under the user's finger.
    if (document.activeElement !== input || Number(input.value) !== shown) input.value = String(shown);
    const pct = hi > lo ? ((shown - lo) / (hi - lo)) * 100 : 0;
    input.style.setProperty('--pct', pct + '%');
    input.setAttribute('aria-valuetext', v === null ? 'not set' : `${Math.round(v)} ${unit()}`);
    input.disabled = !enabled;
    wrap.classList.toggle('pending', pending);
    if (pending) wrap.setAttribute('aria-busy', 'true'); else wrap.removeAttribute('aria-busy');
    if (minEl) minEl.textContent = lo + '°';
    if (maxEl) maxEl.textContent = hi + '°';
  }

  function setRange(input, lo, hi, value) {
    input.min = String(lo);
    input.max = String(hi);
    const v = num(value);
    const shown = v === null ? lo : clamp(v, lo, hi);
    if (Number(input.value) !== shown) input.value = String(shown);
    input.setAttribute('aria-valuetext', v === null ? 'not set' : `${Math.round(v)} ${unit()}`);
    return shown;
  }

  // One bar from heat_min to chill_max (heat_min..heat_max without Chill). Each
  // input keeps its own min/max (so keyboard and screen readers stop at its real
  // limits) and is sized/offset to cover just its part of the bar, so its native
  // thumb lands on the shared scale.
  function renderPoolBar(L, enabled, pending) {
    const dual = L.chill;
    const lo = L.heatLo;
    const hi = dual ? Math.max(lo, L.chillHi) : L.heatHi;
    const f = (v) => (hi > lo ? clamp((v - lo) / (hi - lo), 0, 1) : 0);
    const heatV = view('pool.heat_set');
    setTemp(el.heatNum, heatV);
    const h = setRange(el.heatSlider, L.heatLo, L.heatHi, heatV);
    const bar = el.poolBar.style;
    bar.setProperty('--h-span', String(f(L.heatHi)));
    if (dual) {
      const chillV = view('pool.chill_set');
      setTemp(el.chillNum, chillV);
      const c = setRange(el.chillSlider, L.chillLo, L.chillHi, chillV);
      bar.setProperty('--c-off', String(f(L.chillLo)));
      bar.setProperty('--c-span', String(f(L.chillHi) - f(L.chillLo)));
      bar.setProperty('--a', String(f(h)));
      bar.setProperty('--b', String(f(c)));
    } else {
      bar.setProperty('--a', '0');
      bar.setProperty('--b', String(f(h)));
    }
    el.poolBar.classList.toggle('dual', dual);
    el.poolBar.classList.toggle('disabled', !enabled);
    el.heatSlider.disabled = el.chillSlider.disabled = !enabled;
    el.poolWrap.classList.toggle('pending', pending);
    if (pending) el.poolWrap.setAttribute('aria-busy', 'true'); else el.poolWrap.removeAttribute('aria-busy');
    el.poolMin.textContent = lo + '°';
    el.poolMax.textContent = hi + '°';
  }

  function render() {
    // A drag interrupted by the controls being disabled can't finish; drop it so its
    // local values don't mask the server's forever.
    if (dragging.size && !controlsEnabled()) {
      for (const name of dragging) clearOverrides(name);
      dragging.clear();
    }
    if (!state) {
      el.conn.className = reachable ? 'dot' : 'dot bad';
      el.conn.setAttribute('aria-label', reachable ? 'Connecting' : 'Offline');
      el.conn.title = el.conn.getAttribute('aria-label');
      el.banner.hidden = reachable;
      for (const b of [el.modePool, el.modeSpa, el.light, el.colorBtn, el.bubbles, el.spill, el.wf]) b.disabled = true;
      for (const s of [el.spaSlider, el.chillSlider, el.heatSlider]) s.disabled = true;
      return;
    }
    el.body.classList.remove('loading');

    const connected = controlsEnabled();
    const mode = view('mode') === 'spa' ? 'spa' : 'pool';
    const modePending = isPending('mode');
    const busy = !!state.busy || modePending;

    // header / banner
    let dotClass = 'dot ok', dotLabel = 'Connected';
    if (!connected) { dotClass = 'dot bad'; dotLabel = "Can't reach the pool controller"; }
    else if (busy) { dotClass = 'dot busy'; dotLabel = 'Working'; }
    el.conn.className = dotClass;
    el.conn.setAttribute('aria-label', dotLabel);
    el.conn.title = dotLabel;
    el.banner.hidden = connected;
    el.body.classList.toggle('offline', !connected);
    el.body.dataset.mode = mode;

    // mode
    el.modePool.setAttribute('aria-pressed', String(mode === 'pool'));
    el.modeSpa.setAttribute('aria-pressed', String(mode === 'spa'));
    el.modePool.disabled = el.modeSpa.disabled = !connected || busy;
    const target = modePending ? view('mode') : lastRequestedMode;
    el.busy.hidden = !busy;
    el.busyText.textContent = target ? `Switching to ${cap(target)}…` : 'Changing mode…';

    // light
    const light = state.light || {};
    el.lightBlock.hidden = light.available === false;
    renderToggle(el.light, view('light.on'), { enabled: connected, pending: isPending('light') });
    const colors = Array.isArray(light.colors) ? light.colors : [];
    const color = view('light.color') || 'White';
    const colorPending = isPending('color');
    el.colorBtn.hidden = colors.length === 0;
    el.colorBtn.disabled = !connected;
    el.colorBtn.classList.toggle('pending', colorPending);
    el.colorSwatch.style.setProperty('--sw', swatchFor(color));
    el.colorBtn.setAttribute('aria-label', `Light color: ${color}, change`);
    el.colorBtn.title = 'Change the light color';
    if (colors.length) renderChips(colors, color, connected, colorPending);
    if (el.sheet.open && (colors.length === 0 || light.available === false || !connected)) closeSheet();

    // panels: shared temperature layout, mode-specific set points and toggles
    const spaMode = mode === 'spa';
    el.spaPanel.hidden = !spaMode;
    el.poolPanel.hidden = spaMode;
    const spa = state.spa || {};
    const pool = state.pool || null;
    setTemp(el.waterCur, spaMode ? spa.current_temp : pool && pool.current_temp);
    setTemp(el.airCur, state.air_temp);

    // spa
    el.spaCol.hidden = !spaMode;
    setTemp(el.spaSet, view('spa.set_temp'));
    const spaLo = num(spa.set_min) ?? 80;
    const spaHi = Math.max(spaLo, num(spa.set_max) ?? 104);
    renderSlider(el.spaSlider, el.spaWrap, spaLo, spaHi, view('spa.set_temp'), {
      enabled: connected, pending: isPending('spaSet'), minEl: el.spaMin, maxEl: el.spaMax,
    });
    el.spaSlider.setAttribute('aria-label', 'Spa set temperature');
    renderToggle(el.bubbles, view('spa.bubbles'), { enabled: connected, pending: isPending('bubbles') });

    // pool
    const L = pool ? poolLimits() : null;
    const dual = !!(L && L.chill);
    el.poolSp.hidden = spaMode || !pool;
    el.poolSp.classList.toggle('single', !dual);
    el.chillCol.hidden = !dual;
    el.chillSlider.hidden = !dual;
    el.spreadNote.hidden = spaMode || !dual || !(L.spread > 0);
    if (pool) {
      renderPoolBar(L, connected, isPending('poolSet'));
      if (dual) el.spreadNote.textContent = `Chill stays at least ${L.spread}° above Heat.`;

      const spill = !!view('pool.spillover');
      const wf = !!view('pool.water_features');
      renderToggle(el.spill, spill, { enabled: connected && !wf, pending: isPending('spill') });
      renderToggle(el.wf, wf, { enabled: connected && !spill, pending: isPending('wf') });
      // Hidden until the backend finds a Spillover device on the panel.
      const spillAvail = view('pool.spillover_available') !== false;
      el.spill.hidden = !spillAvail;
      el.spillHint.hidden = !wf || !spillAvail;
      el.wfHint.hidden = !spill;
    }
  }

  render();
  poll();
})();
