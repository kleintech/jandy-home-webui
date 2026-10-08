/* Pool & Spa: the owner's parts of the Settings sheet.
 *
 * - Owner unlock: one PIN form, shown in Advanced and at the top of Settings.
 *   POST /api/advanced/unlock sets an HttpOnly cookie; the PIN itself is only
 *   held for the length of that request (never stored, the field is cleared on
 *   submit). 429 answers show a countdown from Retry-After. /api/state's
 *   `advanced` {enabled, unlocked} says which form to show.
 * - Advanced: GET /api/advanced when the section is shown, then every 10 s while
 *   it stays visible (never while hidden). Every write asks for confirmation
 *   first, sends exactly one request, shows it pending, and redraws from the
 *   view the server answers with (or shows the server's reason).
 * - Settings: forms for GET /api/config, read-only until unlocked; one Save
 *   (PUT, whole document plus the version it was based on) and Reset to
 *   defaults. Device pickers list the panel's devices from GET /api/advanced.
 *
 * Talks to app.js through window.PoolApp (state, config, subscribe, refresh).
 * Panel-provided labels are only ever set as text, never as HTML.
 */
(() => {
  'use strict';

  const App = window.PoolApp;
  const $ = (id) => document.getElementById(id);
  const dlg = $('settings');
  const advRoot = $('adv-root');
  const setView = $('view-settings');
  if (!App || !dlg || !advRoot || !setView) return;

  const ADV_POLL_MS = 10000;
  const GET_TIMEOUT_MS = 15000;
  const POST_TIMEOUT_MS = 60000;
  const MAX_STEPS = 12;
  const MAX_TOGGLES = 20;
  const PANEL_MIN = 34;
  const PANEL_MAX = 104;
  // Fallback names for the panel's fixed devices while their labels are unknown (locked).
  const HOME_LABELS = {
    pool_pump: 'Filter pump', spa_pump: 'Spa mode', spa_heater: 'Spa heater',
    pool_heater: 'Pool heater', solar_heater: 'Solar heater', heatpump: 'Heat pump',
  };

  // ---------- helpers ----------
  /** Element builder: props set as properties when non-string (disabled, checked),
   *  else attributes; on* props are listeners; text/class are shortcuts. */
  function h(tag, props, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
      else if (typeof v !== 'string' && k in n) n[k] = v;
      else n.setAttribute(k, v === true ? '' : String(v));
    }
    for (const c of kids.flat(Infinity)) {
      if (c == null || c === false) continue;
      n.append(c instanceof Node ? c : String(c));
    }
    return n;
  }
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const isInt = (v) => typeof v === 'number' && Number.isInteger(v);
  const unit = () => '°' + ((App.state() && App.state().unit) || 'F');
  const cap = (s) => { s = String(s || ''); return s.charAt(0).toUpperCase() + s.slice(1); };
  const pretty = (id) => cap(String(id || '').replace(/_/g, ' '));
  const fidSel = (fid) => `[data-fid="${CSS.escape(fid)}"]`;

  function select(props, options, value) {
    const s = h('select', props, options.map(([v, label, extra]) => h('option', { value: v, ...(extra || {}) }, label)));
    s.value = value == null ? '' : String(value);
    return s;
  }

  // ---------- network ----------
  function detailText(data, status) {
    const d = data && data.detail;
    if (typeof d === 'string' && d) return d;
    if (Array.isArray(d) && d[0]) {
      const loc = Array.isArray(d[0].loc) ? d[0].loc.filter((x) => x !== 'body').join('.') : '';
      return (loc ? loc + ': ' : '') + (d[0].msg || 'invalid value');
    }
    if (status === 502) return "Couldn't reach the pool controller";
    return `Request failed (${status})`;
  }

  async function req(method, url, body, timeoutMs) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs || GET_TIMEOUT_MS);
    const fail = (msg, status) => Object.assign(new Error(msg), { status });
    try {
      const res = await fetch(url, {
        method,
        cache: 'no-store',
        credentials: 'same-origin',
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        signal: ctl.signal,
      });
      let data = null;
      try { data = await res.json(); } catch (_) { /* non-JSON body */ }
      if (!res.ok) {
        const err = fail(detailText(data, res.status), res.status);
        err.retryAfter = Number(res.headers.get('Retry-After')) || null;
        throw err;
      }
      if (!data || typeof data !== 'object') throw fail('Unexpected response from the server', 0);
      return data;
    } catch (e) {
      if (e && e.name === 'AbortError') throw fail('The server took too long to answer. Check the result before trying again.', 0);
      if (e instanceof TypeError) throw fail("Couldn't reach the server", 0);
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }

  // ---------- confirm dialog ----------
  const cdlg = $('confirm-dlg');
  const cTitle = $('confirm-h');
  const cBody = $('confirm-body');
  const cOk = $('confirm-ok');
  const cCancel = $('confirm-cancel');
  let cResolve = null;

  /** Ask before a write; resolves true only for the OK button. */
  function confirmAction({ title, body, ok, danger }) {
    return new Promise((resolve) => {
      if (cResolve || !cdlg) { resolve(false); return; }
      cResolve = resolve;
      cTitle.textContent = title;
      cBody.textContent = body || '';
      cBody.hidden = !body;
      cOk.textContent = ok || 'OK';
      cOk.classList.toggle('btn-danger', !!danger);
      cdlg.classList.toggle('danger', !!danger);
      if (typeof cdlg.showModal === 'function') cdlg.showModal(); else cdlg.setAttribute('open', '');
      cCancel.focus();
    });
  }
  function settle(v) {
    const r = cResolve;
    cResolve = null;
    if (cdlg.open) { if (typeof cdlg.close === 'function') cdlg.close(); else cdlg.removeAttribute('open'); }
    if (r) r(v);
  }
  if (cdlg) {
    cOk.addEventListener('click', () => settle(true));
    cCancel.addEventListener('click', () => settle(false));
    cdlg.addEventListener('close', () => settle(false));          // Escape
    cdlg.addEventListener('click', (e) => { if (e.target === cdlg) settle(false); });
  }

  // ---------- owner status, unlock / lock ----------
  let owner = { enabled: null, unlocked: false };
  let ownerChangedAt = -Infinity;  // a state fetched before this has an outdated owner status
  let lockUntil = 0;               // Date.now() until which unlock is rate limited
  let lockTimer = null;
  let unlocking = false;
  let pinMsg = '';

  function takeStatus(s) {
    const a = s && s.advanced;
    if (!a || typeof a !== 'object' || typeof s.fetchedAt !== 'number' || s.fetchedAt < ownerChangedAt) return false;
    const next = { enabled: a.enabled === true, unlocked: a.enabled === true && a.unlocked === true };
    if (next.enabled === owner.enabled && next.unlocked === owner.unlocked) return false;
    owner = next;
    return true;
  }

  function setOwner(next) {
    ownerChangedAt = performance.now();
    owner = { ...owner, ...next };
    if (!owner.enabled) owner.unlocked = false;
    onOwnerChange();
  }

  /** An owner endpoint said 401 (session gone) or 403 (owner controls off). */
  function ownerLost(status) {
    if (status === 403) setOwner({ enabled: false, unlocked: false });
    else { pinMsg = 'Your owner session ended. Enter the PIN again.'; setOwner({ unlocked: false }); }
  }

  function lockText() {
    const left = Math.ceil((lockUntil - Date.now()) / 1000);
    if (left <= 0) return '';
    const m = Math.floor(left / 60);
    const s = String(left % 60).padStart(2, '0');
    return `Too many attempts. Try again in ${m}:${s}.`;
  }

  function updatePinUi() {
    const limited = Date.now() < lockUntil;
    const msg = limited ? lockText() : pinMsg;
    for (const box of document.querySelectorAll('.pin-form')) {
      const m = box.querySelector('.pin-msg');
      m.textContent = msg;
      m.hidden = !msg;
      box.querySelector('.pin-input').disabled = limited || unlocking;
      const b = box.querySelector('button[type="submit"]');
      b.disabled = limited || unlocking;
      b.textContent = unlocking ? 'Unlocking…' : 'Unlock';
      box.classList.toggle('limited', limited);
    }
  }

  function startCountdown(seconds) {
    lockUntil = Date.now() + Math.max(1, seconds) * 1000;
    clearInterval(lockTimer);
    lockTimer = setInterval(() => {
      if (Date.now() >= lockUntil) { clearInterval(lockTimer); lockTimer = null; pinMsg = ''; }
      updatePinUi();
    }, 1000);
    updatePinUi();
  }

  async function unlock(input) {
    const pin = input.value.trim();
    input.value = '';                 // the PIN never outlives this request
    if (unlocking || Date.now() < lockUntil) return;
    if (!/^\d{4,12}$/.test(pin)) {
      pinMsg = 'The owner PIN is 4 to 12 digits.';
      updatePinUi();
      input.focus();
      return;
    }
    unlocking = true;
    pinMsg = '';
    updatePinUi();
    try {
      await req('POST', '/api/advanced/unlock', { pin }, GET_TIMEOUT_MS);
      unlocking = false;
      setOwner({ enabled: true, unlocked: true });
      App.refresh();
      return;
    } catch (e) {
      if (e.status === 429) { pinMsg = ''; startCountdown(e.retryAfter || 60); }
      else if (e.status === 403) { unlocking = false; setOwner({ enabled: false }); return; }
      else if (e.status === 401) pinMsg = 'Wrong PIN. Try again.';
      else pinMsg = e.message;
    }
    unlocking = false;
    updatePinUi();
    const again = document.querySelector('.pin-form:not([hidden]) .pin-input');
    if (again && !again.disabled && again.offsetParent) again.focus();
  }

  let locking = false;
  async function lock(btn) {
    if (locking) return;
    locking = true;
    btn.disabled = true;
    try {
      await req('POST', '/api/advanced/lock', null, GET_TIMEOUT_MS);
      pinMsg = '';
      setOwner({ unlocked: false });
      App.refresh();
    } catch (e) {
      btn.disabled = false;
      App.toast(`Couldn't lock: ${e.message}`);
    } finally {
      locking = false;
    }
  }

  // One PIN form type="text" masked with CSS where supported, so browsers don't
  // offer to save it as a password; plain type="password" elsewhere.
  const MASK = typeof CSS !== 'undefined' && CSS.supports && CSS.supports('-webkit-text-security', 'disc');

  function renderOwnerBox(box, ctx) {
    const sig = JSON.stringify([owner, ctx]);
    if (box.dataset.sig === sig) { updatePinUi(); return; }
    box.dataset.sig = sig;
    box.textContent = '';
    if (owner.enabled === null) {
      box.append(h('p', { class: 'note', text: 'Checking owner controls…' }));
      return;
    }
    if (!owner.enabled) {
      box.append(h('p', { class: 'owner-off', text: 'Owner controls are off — set OWNER_PIN on the server.' }));
      return;
    }
    if (owner.unlocked) {
      box.append(h('div', { class: 'owner-bar' },
        h('span', { class: 'owner-state' }, h('span', { class: 'owner-dot', 'aria-hidden': 'true' }), 'Owner unlocked'),
        h('button', { type: 'button', class: 'btn-secondary btn-small', onclick: (e) => lock(e.currentTarget) }, 'Lock')));
      return;
    }
    const id = 'pin-' + ctx;
    const input = h('input', {
      id, class: 'pin-input' + (MASK ? ' pin-mask' : ''), type: MASK ? 'text' : 'password',
      inputmode: 'numeric', pattern: '[0-9]*', maxlength: '12', autocomplete: 'off',
      autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false', enterkeyhint: 'go',
      'data-1p-ignore': true, 'data-lpignore': 'true', 'aria-describedby': id + '-msg',
    });
    const form = h('form', { class: 'pin-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); unlock(input); } },
      h('label', { class: 'field-label', for: id, text: 'Owner PIN' }),
      h('div', { class: 'pin-row' }, input, h('button', { type: 'submit', class: 'btn-primary pin-go' }, 'Unlock')),
      h('p', { id: id + '-msg', class: 'pin-msg form-msg', role: 'alert', hidden: true }));
    box.append(
      h('p', { class: 'note owner-why', text: ctx === 'adv'
        ? 'These controls switch the panel directly, without the guest limits. Enter the owner PIN to use them.'
        : 'Anyone can see these settings. Enter the owner PIN to change them.' }),
      form);
    updatePinUi();
  }

  // ---------- visibility (Advanced polls only while shown) ----------
  const advShown = () => dlg.open && !advRoot.hidden && !document.hidden;
  const setShown = () => dlg.open && !setView.hidden && !document.hidden;
  let wasAdvShown = false;

  function onVisibility() {
    const now = advShown();
    if (now && !wasAdvShown) loadAdv();                    // fresh data whenever it's opened
    else if (!now) { clearTimeout(advTimer); advTimer = null; }
    wasAdvShown = now;
    // The Settings pickers need the device list once.
    if (setShown() && owner.unlocked && !adv && !advLoading) loadAdv();
  }
  const mo = new MutationObserver(onVisibility);
  mo.observe(advRoot, { attributes: true, attributeFilter: ['hidden'] });
  mo.observe(setView, { attributes: true, attributeFilter: ['hidden'] });
  mo.observe(dlg, { attributes: true, attributeFilter: ['open'] });
  document.addEventListener('visibilitychange', onVisibility);

  // ---------- Advanced: data ----------
  let adv = null;          // last GET /api/advanced (or write) view
  let advError = null;
  let advLoading = false;
  let advTimer = null;
  let advSeq = 0;          // bumped by writes: a GET that straddled one is dropped
  let writing = null;      // { sec, fid } of the write in flight
  const secErr = Object.create(null);   // section -> message from the last failed write
  const drafts = Object.create(null);   // fid -> value typed but not applied yet
  const openDetails = new Set();
  let restoreFid = null;   // control to refocus once a write has been redrawn

  async function loadAdv() {
    clearTimeout(advTimer);
    advTimer = null;
    if (!owner.enabled || !owner.unlocked) return;
    if (advLoading) return;
    advLoading = true;
    const seq = advSeq;
    try {
      const v = await req('GET', '/api/advanced', null, GET_TIMEOUT_MS);
      if (seq === advSeq && !writing) { adv = v; advError = null; }
    } catch (e) {
      if (e.status === 401 || e.status === 403) ownerLost(e.status);
      else if (seq === advSeq) advError = e.message;
    } finally {
      advLoading = false;
      renderAdv();
      renderSettings();
      scheduleAdv();
    }
  }

  function scheduleAdv() {
    clearTimeout(advTimer);
    advTimer = advShown() && owner.unlocked ? setTimeout(loadAdv, ADV_POLL_MS) : null;
  }

  async function write(sec, fid, url, body, after) {
    if (writing || !adv) return;
    writing = { sec, fid };
    delete secErr[sec];
    advSeq += 1;
    renderAdv();
    try {
      const v = await req('POST', url, body, POST_TIMEOUT_MS);
      adv = v;
      advError = null;
      if (after) after();
      App.refresh();   // the main page shows the change too
    } catch (e) {
      if (e.status === 401 || e.status === 403) ownerLost(e.status);
      else secErr[sec] = e.status === 422 ? `The server didn't accept that: ${e.message}` : cap(e.message);
    } finally {
      writing = null;
      advSeq += 1;
      restoreFid = fid;
      renderAdv();
      scheduleAdv();
    }
  }

  // ---------- Advanced: rendering ----------
  let advSig = '';

  function renderAdv() {
    const body = $('adv-body');
    renderOwnerBox($('adv-owner'), 'adv');
    const sig = JSON.stringify([owner, adv, advError, writing, secErr, !!adv || advLoading]);
    if (sig === advSig) return;
    advSig = sig;
    const active = document.activeElement;
    const focusFid = (active && body.contains(active) && active.dataset.fid) || restoreFid;
    restoreFid = null;
    body.textContent = '';
    if (!owner.enabled || !owner.unlocked) return;
    if (!adv) {
      body.append(h('p', { class: advError ? 'form-msg' : 'note', role: advError ? 'alert' : null,
        text: advError ? `Couldn't load the owner controls: ${advError}` : 'Loading the panel…' }));
      return;
    }
    const enabled = adv.connected !== false && !writing;
    body.append(statusBlock());
    for (const g of switchGroups()) body.append(switchSection(g, enabled));
    if (adv.heatpump) body.append(heatpumpSection(adv.heatpump, enabled));
    const sp = adv.setpoints || {};
    if (sp.spa || sp.pool_heat || sp.pool_chill) body.append(setpointSection(sp, enabled));
    if (Array.isArray(adv.lights) && adv.lights.length) body.append(lightSection(adv.lights, enabled));
    if (Array.isArray(adv.vsp) && adv.vsp.length) body.append(vspSection(adv.vsp, enabled));
    if (adv.salt) body.append(saltSection(adv.salt, enabled));
    if (focusFid) {
      const n = body.querySelector(fidSel(focusFid));
      if (n && !n.disabled) n.focus();
    }
    validateSetpoints();
    validateSalt();
  }

  function statusBlock() {
    const out = h('div', { class: 'adv-status' });
    if (adv.connected === false) {
      out.append(h('p', { class: 'form-msg', role: 'alert', text: "The pool controller isn't reachable, so these controls are off until it is." }));
    } else if (writing) {
      out.append(h('p', { class: 'status adv-sending' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Sending to the panel…'));
    } else if (adv.busy) {
      out.append(h('p', { class: 'note', text: 'The panel is busy with another command; a change waits its turn.' }));
    }
    const d = adv.updated_at ? new Date(adv.updated_at) : null;
    if (d && !isNaN(d)) {
      out.append(h('p', { class: 'note adv-updated',
        text: 'Panel reading from ' + d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' }) }));
    }
    if (advError) out.append(h('p', { class: 'form-msg', role: 'alert', text: `Couldn't refresh: ${advError}` }));
    return out;
  }

  function section(id, title, ...kids) {
    return h('section', { class: 'adv-sec', id: 'adv-' + id, 'aria-labelledby': `adv-${id}-h` },
      h('h3', { class: 'adv-sec-title', id: `adv-${id}-h`, text: title }),
      secErr[id] ? h('p', { class: 'form-msg', role: 'alert', text: secErr[id] }) : null,
      kids);
  }

  const pendingFid = (fid) => !!writing && writing.fid === fid;

  /** Switch groups without devices that have their own section (heat pump, lights). */
  function switchGroups() {
    const lightKeys = new Set((adv.lights || []).map((l) => l.key));
    return (Array.isArray(adv.switches) ? adv.switches : []).map((g) => ({
      ...g,
      items: (g.items || []).filter((it) => !(it.kind === 'heatpump' && adv.heatpump)
        && !(it.kind === 'light' && lightKeys.has(it.key))),
    })).filter((g) => g.items.length);
  }

  /** What makes this switch's next action risky, or null. */
  function dangerOf(it) {
    const turningOn = !it.on;
    if (!turningOn && (it.key === 'pool_pump' || it.role === 'filter_pump')) {
      return 'Turning off the filter pump stops circulation, and the spa and heaters that rely on it.';
    }
    if (turningOn && it.kind === 'scene' && /\ball\s*off\b/i.test(it.label || '')) {
      return 'This runs the panel\'s "All OFF" scene, which switches off the equipment it controls.';
    }
    if (!turningOn && it.kind === 'heatpump') return "The pool won't be heated or chilled until it's back on.";
    return null;
  }

  function sub(it) {
    const parts = [it.key];
    if (it.role) parts.push('guest: ' + pretty(it.role).toLowerCase());
    return parts.join(' · ');
  }

  function toggleRow({ fid, label, subText, on, enabled, danger, onclick }) {
    const pending = pendingFid(fid);
    return h('button', {
      type: 'button', class: 'toggle adv-tog' + (danger ? ' danger' : '') + (pending ? ' pending' : ''),
      'aria-pressed': String(!!on), 'aria-busy': pending ? 'true' : null, 'data-fid': fid,
      disabled: !enabled, onclick,
    },
    h('span', { class: 'adv-tog-text' },
      h('span', { class: 'toggle-label', text: label }),
      subText ? h('span', { class: 'adv-sub', text: subText }) : null,
      danger ? h('span', { class: 'adv-caution', text: 'Caution' }) : null),
    h('span', { class: 'adv-state', 'aria-hidden': 'true', text: on ? 'On' : 'Off' }),
    h('span', { class: 'switch', 'aria-hidden': 'true' }));
  }

  function switchItem(it, sec, enabled) {
    const danger = dangerOf(it);
    return toggleRow({
      fid: 'sw:' + it.key, label: it.label, subText: sub(it), on: it.on, enabled, danger,
      onclick: async () => {
        const on = !it.on;
        const ok = await confirmAction({
          title: `Turn ${on ? 'ON' : 'OFF'} ${it.label}?`,
          body: danger || (it.kind === 'scene'
            ? `Turns the OneTouch scene ${on ? 'on' : 'off'} on the panel.`
            : `Switches ${it.label} (${it.key}) ${on ? 'on' : 'off'} on the panel.`),
          ok: on ? 'Turn on' : 'Turn off', danger: !!danger,
        });
        if (ok) write(sec, 'sw:' + it.key, '/api/advanced/switch', { key: it.key, on });
      },
    });
  }

  function switchSection(g, enabled) {
    const main = g.items.filter((it) => !it.placeholder);
    const spare = g.items.filter((it) => it.placeholder);
    const sec = 'sw-' + g.id;
    const kids = main.map((it) => switchItem(it, sec, enabled));
    if (spare.length) {
      const fid = 'spare:' + g.id;
      const d = h('details', { class: 'adv-spare', 'data-fid': fid, open: openDetails.has(fid) },
        h('summary', { text: `Unused aux slots (${spare.length})` }),
        spare.map((it) => switchItem(it, sec, enabled)));
      d.addEventListener('toggle', () => { if (d.open) openDetails.add(fid); else openDetails.delete(fid); });
      kids.push(d);
    }
    return section(sec, String(g.title || g.id), kids);
  }

  function heatpumpSection(hp, enabled) {
    const off = !hp.on;
    const danger = hp.on ? "The pool won't be heated or chilled until it's back on." : null;
    const info = [hp.status && `Status: ${hp.status}`, hp.type && `type ${hp.type}`].filter(Boolean).join(' · ');
    const modes = Array.isArray(hp.modes) ? hp.modes : [];
    const seg = modes.length ? h('div', { class: 'segmented adv-seg', role: 'group', 'aria-label': 'Heat pump mode' },
      modes.map((m) => h('button', {
        type: 'button', class: 'seg' + (pendingFid('hp:mode:' + m) ? ' pending' : ''), 'data-fid': 'hp:mode:' + m,
        'aria-pressed': String(hp.mode === m), disabled: !enabled,
        onclick: async () => {
          if (hp.mode === m) return;
          const ok = await confirmAction({
            title: `Switch Heat pump to ${cap(m)}?`,
            body: m === 'chill' ? 'The heat pump will cool the pool toward the chill set point.'
              : 'The heat pump will heat the pool toward the heat set point.',
            ok: `Switch to ${cap(m)}`,
          });
          if (ok) write('hp', 'hp:mode:' + m, '/api/advanced/heatpump', { mode: m });
        },
      }, cap(m)))) : null;
    return section('hp', 'Heat pump',
      toggleRow({
        fid: 'hp:on', label: 'Heat pump', subText: info, on: hp.on, enabled, danger,
        onclick: async () => {
          const on = off;
          const ok = await confirmAction({
            title: `Turn ${on ? 'ON' : 'OFF'} Heat pump?`,
            body: danger || 'The heat pump will run to its set points.',
            ok: on ? 'Turn on' : 'Turn off', danger: !!danger,
          });
          if (ok) write('hp', 'hp:on', '/api/advanced/heatpump', { on });
        },
      }),
      seg ? h('div', { class: 'adv-field' }, h('span', { class: 'field-label', text: 'Mode' }), seg) : null);
  }

  // ----- set points -----
  const SP_FIELDS = [['spa', 'Spa'], ['pool_heat', 'Pool heat'], ['pool_chill', 'Pool chill']];

  function spValue(name) {
    const d = drafts['sp:' + name];
    return d !== undefined ? d : adv.setpoints[name] && adv.setpoints[name].value;
  }

  function stepper({ fid, value, min, max, step, enabled, label, onChange }) {
    const input = h('input', {
      type: 'number', inputmode: 'numeric', class: 'num-input', 'data-fid': fid,
      min: String(min), max: String(max), step: String(step || 1),
      value: value == null ? '' : String(value), disabled: !enabled, 'aria-label': label,
      oninput: () => onChange(input.value === '' ? null : Number(input.value)),
    });
    const bump = (dir) => {
      const cur = input.value === '' ? null : Number(input.value);
      const base = cur == null || !isFinite(cur) ? (dir > 0 ? min : max) : cur + dir * (step || 1);
      const v = Math.min(max, Math.max(min, Math.round(base)));
      input.value = String(v);
      onChange(v);
    };
    return h('div', { class: 'stepper' },
      h('button', { type: 'button', class: 'step-btn', disabled: !enabled, 'aria-label': `Lower ${label}`, onclick: () => bump(-1) }, '−'),
      input,
      h('button', { type: 'button', class: 'step-btn', disabled: !enabled, 'aria-label': `Raise ${label}`, onclick: () => bump(1) }, '+'));
  }

  function spErrors() {
    const sp = adv.setpoints;
    const errs = [];
    for (const [name, label] of SP_FIELDS) {
      const r = sp[name];
      if (!r) continue;
      const v = spValue(name);
      if (!isInt(v)) errs.push(`${label}: enter a whole number.`);
      else if (v < r.min || v > r.max) errs.push(`${label}: between ${r.min} and ${r.max}${unit()}.`);
    }
    if (sp.pool_heat && sp.pool_chill && !errs.length) {
      const heat = spValue('pool_heat');
      const chill = spValue('pool_chill');
      const spread = isInt(sp.min_spread) ? sp.min_spread : 1;
      if (isInt(heat) && isInt(chill) && chill < heat + spread) {
        errs.push(`Pool chill must be at least ${spread}° above pool heat (${heat + spread}${unit()} or more).`);
      }
    }
    return errs;
  }

  function spChanges() {
    return SP_FIELDS.filter(([name]) => adv.setpoints[name] && drafts['sp:' + name] !== undefined
      && drafts['sp:' + name] !== adv.setpoints[name].value);
  }

  function validateSetpoints() {
    const err = document.querySelector('#adv-sp .sp-err');
    const btn = document.querySelector('#adv-sp .sp-apply');
    if (!err || !btn || !adv) return;
    const errs = spErrors();
    err.textContent = errs.join(' ');
    err.hidden = !errs.length;
    btn.disabled = !!writing || adv.connected === false || !!errs.length || !spChanges().length;
    for (const [name] of SP_FIELDS) {
      const n = document.querySelector(fidSel('sp:' + name));
      if (n) n.classList.toggle('changed', drafts['sp:' + name] !== undefined && drafts['sp:' + name] !== adv.setpoints[name].value);
    }
  }

  function setpointSection(sp, enabled) {
    const rows = SP_FIELDS.filter(([name]) => sp[name]).map(([name, label]) => {
      const r = sp[name];
      return h('div', { class: 'sp-row' },
        h('div', { class: 'sp-row-label' },
          h('span', { class: 'sp-name', text: label }),
          h('span', { class: 'adv-sub', text: `Panel: ${r.value == null ? '--' : r.value + unit()} · ${r.min}–${r.max}${unit()}` })),
        stepper({
          fid: 'sp:' + name, value: spValue(name), min: r.min, max: r.max, enabled, label: `${label} set point`,
          onChange: (v) => {
            if (v === r.value) delete drafts['sp:' + name]; else drafts['sp:' + name] = v;
            validateSetpoints();
          },
        }));
    });
    const spread = isInt(sp.min_spread) ? sp.min_spread : 1;
    return section('sp', 'Set points',
      rows,
      sp.pool_heat && sp.pool_chill ? h('p', { class: 'note', text: `Pool chill stays at least ${spread}° above pool heat.` }) : null,
      h('p', { class: 'form-msg sp-err', role: 'alert', hidden: true }),
      h('button', {
        type: 'button', class: 'btn-primary sp-apply' + (pendingFid('sp:apply') ? ' pending' : ''), 'data-fid': 'sp:apply',
        disabled: true,
        onclick: async () => {
          const changes = spChanges();
          if (!changes.length || spErrors().length) return;
          const lines = changes.map(([name, label]) => `${label}: ${sp[name].value ?? '--'}° → ${drafts['sp:' + name]}${unit()}`);
          const ok = await confirmAction({
            title: changes.length === 1 ? `Set ${changes[0][1]} to ${drafts['sp:' + changes[0][0]]}${unit()}?` : 'Change these set points?',
            body: lines.join('\n'), ok: 'Apply',
          });
          if (!ok) return;
          const payload = {};
          for (const [name] of changes) payload[name] = drafts['sp:' + name];
          write('sp', 'sp:apply', '/api/advanced/setpoints', payload, () => {
            for (const [name] of SP_FIELDS) delete drafts['sp:' + name];
          });
        },
      }, 'Apply set points'));
  }

  // ----- lights -----
  function lightSection(lights, enabled) {
    return section('lt', 'Lights', lights.map((l) => {
      const label = l.label || l.key;
      const effects = Array.isArray(l.effects) ? l.effects : [];
      const step = isInt(l.brightness_step) && l.brightness_step > 0 ? l.brightness_step : null;
      const fxFid = `lt:${l.key}:fx`;
      const brFid = `lt:${l.key}:br`;
      const fx = effects.length ? select({
        'data-fid': fxFid, disabled: !enabled, 'aria-label': `${label} effect`,
        class: pendingFid(fxFid) ? 'pending' : null,
        onchange: async (e) => {
          const s = e.currentTarget;
          const v = s.value;
          if (!v) return;
          const ok = await confirmAction({
            title: `Set ${label} to ${v}?`,
            body: l.on ? `Changes the light's effect to ${v}.` : `Turns ${label} on with the effect ${v}.`,
            ok: 'Set effect',
          });
          if (ok) write('lt', fxFid, '/api/advanced/light', { key: l.key, effect: v });
          else s.value = l.effect || '';
        },
      }, [...(l.effect ? [] : [['', l.on ? 'Effect unknown' : 'Choose an effect…', { disabled: true }]]),
        ...effects.map((x) => [x, x])], l.effect || '') : null;
      let br = null;
      if (step) {
        const opts = [];
        for (let v = step; v <= 100; v += step) opts.push([String(v), `${v}%`]);
        const cur = isInt(l.brightness) ? String(l.brightness) : '';
        if (!cur) opts.unshift(['', 'Unknown', { disabled: true }]);
        else if (!opts.some(([v]) => v === cur)) opts.unshift([cur, `${cur}%`]);
        br = select({
          'data-fid': brFid, disabled: !enabled, 'aria-label': `${label} brightness`,
          onchange: async (e) => {
            const s = e.currentTarget;
            const v = Number(s.value);
            const ok = await confirmAction({ title: `Set ${label} brightness to ${v}%?`, body: l.on ? null : `Turns ${label} on.`, ok: 'Set brightness' });
            if (ok) write('lt', brFid, '/api/advanced/light', { key: l.key, brightness: v });
            else s.value = cur;
          },
        }, opts, cur);
      }
      return h('div', { class: 'adv-item' },
        toggleRow({
          fid: `lt:${l.key}:on`, label, subText: [l.key, l.type].filter(Boolean).join(' · '), on: l.on, enabled,
          onclick: async () => {
            const on = !l.on;
            const ok = await confirmAction({ title: `Turn ${on ? 'ON' : 'OFF'} ${label}?`, body: null, ok: on ? 'Turn on' : 'Turn off' });
            if (ok) write('lt', `lt:${l.key}:on`, '/api/advanced/light', { key: l.key, on });
          },
        }),
        fx ? h('label', { class: 'adv-field' }, h('span', { class: 'field-label', text: 'Effect' }), fx) : null,
        br ? h('label', { class: 'adv-field' }, h('span', { class: 'field-label', text: 'Brightness' }), br) : null);
    }));
  }

  // ----- variable-speed pumps -----
  function vspSection(list, enabled) {
    return section('vsp', 'Variable-speed pumps', list.map((p) => {
      const label = p.label || p.key;
      const presets = Array.isArray(p.presets) ? p.presets : [];
      const pf = `vsp:${p.key}:preset`;
      const pre = presets.length ? select({
        'data-fid': pf, disabled: !enabled, 'aria-label': `${label} speed`,
        onchange: async (e) => {
          const s = e.currentTarget;
          const v = s.value;
          const ok = await confirmAction({ title: `Set ${label} to ${v}?`, body: p.on ? null : `Starts ${label}.`, ok: 'Set speed' });
          if (ok) write('vsp', pf, '/api/advanced/vsp', { key: p.key, preset: v });
          else s.value = p.preset || '';
        },
      }, [...(p.preset ? [] : [['', 'Choose a speed…', { disabled: true }]]), ...presets.map((x) => [x, x])], p.preset || '') : null;
      return h('div', { class: 'adv-item' },
        toggleRow({
          fid: `vsp:${p.key}:on`, label, subText: p.key, on: p.on, enabled,
          onclick: async () => {
            const on = !p.on;
            const ok = await confirmAction({ title: `Turn ${on ? 'ON' : 'OFF'} ${label}?`, body: null, ok: on ? 'Turn on' : 'Turn off' });
            if (ok) write('vsp', `vsp:${p.key}:on`, '/api/advanced/vsp', { key: p.key, on });
          },
        }),
        pre ? h('label', { class: 'adv-field' }, h('span', { class: 'field-label', text: 'Speed' }), pre) : null);
    }));
  }

  // ----- salt cell -----
  const BOOST_TEXT = { off: 'Off', on: 'Boosting', paused: 'Paused', unknown: 'Unknown' };

  function saltValue(name) {
    const d = drafts['salt:' + name];
    return d !== undefined ? d : adv.salt && adv.salt.config && adv.salt.config[name];
  }

  function saltChanges() {
    const c = adv.salt && adv.salt.config;
    if (!c) return [];
    return [['pool_pct', 'Pool'], ['spa_pct', 'Spa']].filter(([n]) => drafts['salt:' + n] !== undefined && drafts['salt:' + n] !== c[n]);
  }

  function saltErrors() {
    const errs = [];
    for (const [n, label] of [['pool_pct', 'Pool'], ['spa_pct', 'Spa']]) {
      const v = saltValue(n);
      if (drafts['salt:' + n] === undefined) continue;
      if (!isInt(v) || v < 0 || v > 100) errs.push(`${label}: a whole number from 0 to 100.`);
    }
    return errs;
  }

  function validateSalt() {
    const err = document.querySelector('#adv-salt .salt-err');
    const btn = document.querySelector('#adv-salt .salt-apply');
    if (!err || !btn || !adv) return;
    const errs = saltErrors();
    err.textContent = errs.join(' ');
    err.hidden = !errs.length;
    btn.disabled = !!writing || adv.connected === false || !!errs.length || !saltChanges().length;
  }

  function remaining(b) {
    const hh = isInt(b.remaining_hours) ? b.remaining_hours : null;
    const mm = isInt(b.remaining_mins) ? b.remaining_mins : null;
    if (hh == null && mm == null) return '';
    return ` — ${hh || 0} h ${mm || 0} min left`;
  }

  function boostButton(label, action, body, enabled) {
    const fid = 'boost:' + action;
    return h('button', {
      type: 'button', class: 'btn-secondary btn-danger' + (pendingFid(fid) ? ' pending' : ''), 'data-fid': fid, disabled: !enabled,
      onclick: async () => {
        const ok = await confirmAction({ title: `${label} the salt cell boost?`, body, ok: label, danger: true });
        if (ok) write('salt', fid, '/api/advanced/salt/boost', { action });
      },
    }, label);
  }

  function saltSection(salt, enabled) {
    const rows = h('dl', { class: 'eq-rows adv-rows' },
      h('div', { class: 'eq-row' }, h('dt', { text: 'Status' }), h('dd', { text: salt.status == null ? '--' : pretty(salt.status) })),
      h('div', { class: 'eq-row' }, h('dt', { text: 'Output' }), h('dd', { text: salt.output == null ? '--' : `${salt.output}%` })));
    const kids = [
      h('p', { class: 'unverified' },
        h('strong', { text: 'Unverified on this panel. ' }),
        "These commands follow iAqualink's documentation but haven't been confirmed on this panel yet. Check the panel after each change."),
      rows,
    ];
    const c = salt.config;
    if (!c) {
      kids.push(h('p', { class: salt.error ? 'form-msg' : 'note', text: salt.error
        ? "Couldn't read the salt cell's settings, so they can't be changed right now."
        : 'Reading the salt cell settings…' }));
      return section('salt', 'Salt cell', kids);
    }
    const pct = (n, label) => h('div', { class: 'sp-row' },
      h('div', { class: 'sp-row-label' },
        h('span', { class: 'sp-name', text: `${label} output` }),
        h('span', { class: 'adv-sub', text: `Cell: ${c[n] == null ? '--' : c[n] + '%'}` })),
      stepper({
        fid: 'salt:' + n, value: saltValue(n), min: 0, max: 100, enabled, label: `${label} salt output percent`,
        onChange: (v) => { if (v === c[n]) delete drafts['salt:' + n]; else drafts['salt:' + n] = v; validateSalt(); },
      }));
    kids.push(pct('pool_pct', 'Pool'), pct('spa_pct', 'Spa'),
      h('p', { class: 'form-msg salt-err', role: 'alert', hidden: true }),
      h('button', {
        type: 'button', class: 'btn-primary btn-danger-fill salt-apply' + (pendingFid('salt:apply') ? ' pending' : ''),
        'data-fid': 'salt:apply', disabled: true,
        onclick: async () => {
          const changes = saltChanges();
          if (!changes.length || saltErrors().length) return;
          const ok = await confirmAction({
            title: 'Change the salt cell output?',
            body: changes.map(([n, label]) => `${label}: ${c[n] ?? '--'}% → ${drafts['salt:' + n]}%`).join('\n')
              + '\nUnverified on this panel: check the cell afterwards.',
            ok: 'Apply', danger: true,
          });
          if (!ok) return;
          const payload = {};
          for (const [n] of changes) payload[n] = drafts['salt:' + n];
          write('salt', 'salt:apply', '/api/advanced/salt', payload, () => { delete drafts['salt:pool_pct']; delete drafts['salt:spa_pct']; });
        },
      }, 'Apply salt output'));

    const b = c.boost || {};
    const status = b.status in BOOST_TEXT ? b.status : 'unknown';
    const boost = h('div', { class: 'boost' },
      h('h4', { class: 'adv-sub-title', text: 'Boost' }),
      h('p', { class: 'boost-status', text: `${BOOST_TEXT[status]}${status === 'on' || status === 'paused' ? remaining(b) : ''}${b.mode ? ` (${b.mode})` : ''}` }));
    if (status === 'off') {
      if (!b.dip_enabled) {
        boost.append(h('p', { class: 'note', text: "Boost is turned off by the salt cell's DIP switch." }));
      } else {
        const hrs = drafts['boost:hours'] ?? (isInt(b.hours) && b.hours >= 1 && b.hours <= 24 ? b.hours : 24);
        const mode = drafts['boost:mode'] ?? (b.mode || 'pool');
        const hOpts = [];
        for (let i = 1; i <= 24; i += 1) hOpts.push([String(i), i === 1 ? '1 hour' : `${i} hours`]);
        boost.append(h('div', { class: 'field-row boost-row' },
          h('label', { class: 'field field-inline' }, h('span', { class: 'field-label', text: 'Hours' }),
            select({ 'data-fid': 'boost:hours', disabled: !enabled, onchange: (e) => { drafts['boost:hours'] = Number(e.currentTarget.value); } }, hOpts, hrs)),
          h('label', { class: 'field field-inline' }, h('span', { class: 'field-label', text: 'Mode' }),
            select({ 'data-fid': 'boost:mode', disabled: !enabled, onchange: (e) => { drafts['boost:mode'] = e.currentTarget.value; } },
              [['pool', 'Pool'], ['spillover', 'Spillover']], mode))),
        h('button', {
          type: 'button', class: 'btn-primary btn-danger-fill' + (pendingFid('boost:start') ? ' pending' : ''), 'data-fid': 'boost:start', disabled: !enabled,
          onclick: async () => {
            const hours = drafts['boost:hours'] ?? hrs;
            const m = drafts['boost:mode'] ?? mode;
            const ok = await confirmAction({
              title: `Start a ${hours}-hour salt cell boost (${m})?`,
              body: 'The cell runs at full output for that long. Unverified on this panel: check the cell afterwards.',
              ok: 'Start boost', danger: true,
            });
            if (ok) write('salt', 'boost:start', '/api/advanced/salt/boost', { action: 'start', hours, mode: m },
              () => { delete drafts['boost:hours']; delete drafts['boost:mode']; });
          },
        }, 'Start boost'));
      }
    } else if (status === 'on') {
      boost.append(h('div', { class: 'btn-row' },
        boostButton('Pause', 'pause', 'The boost pauses and can be resumed.', enabled),
        boostButton('Stop', 'stop', 'Ends the boost.', enabled)));
    } else if (status === 'paused') {
      boost.append(h('div', { class: 'btn-row' },
        boostButton('Resume', 'resume', 'The boost carries on where it paused.', enabled),
        boostButton('Stop', 'stop', 'Ends the boost.', enabled)));
    } else {
      boost.append(h('p', { class: 'note', text: 'The cell reported a boost state this page doesn\'t know, so boost controls are hidden.' }));
    }
    kids.push(boost);
    return section('salt', 'Salt cell', kids);
  }

  // ---------- Settings (app configuration) ----------
  let cfg = null;            // the server's document
  let draft = null;          // the owner's working copy (no version)
  let baseVersion = null;    // version `draft` was taken from (sent with the PUT)
  let saving = false;
  let serverErr = null;      // { field, msg } from a 422
  let topMsg = null;         // { text, reload } shown above the forms
  let statusMsg = '';
  const newIds = new Set();  // guest toggle ids added in this draft (renamed with their label)

  const strip = (doc) => { const d = clone(doc); delete d.version; return d; };
  const isDirty = () => !!(draft && cfg) && JSON.stringify(draft) !== JSON.stringify(strip(cfg));
  const editable = () => owner.enabled === true && owner.unlocked && !!cfg && !!draft && !saving;

  function resetDraft() {
    draft = cfg ? strip(cfg) : null;
    for (const k of ['main_page', 'limits', 'weather']) if (draft && (!draft[k] || typeof draft[k] !== 'object')) draft[k] = {};
    for (const k of ['hot_tub_on', 'hot_tub_off', 'guest_toggles']) if (draft && !Array.isArray(draft[k])) draft[k] = [];
    baseVersion = cfg ? cfg.version : null;
    serverErr = null;
    topMsg = null;
    newIds.clear();
  }

  function onConfig(c) {
    if (!c) return;
    if (cfg && c.version === cfg.version) return;
    const dirty = isDirty();
    cfg = c;
    if (!draft || !dirty) { resetDraft(); statusMsg = ''; }
    else if (c.version !== baseVersion) {
      topMsg = { text: 'These settings were changed somewhere else. Reload to see them (your unsaved changes here will be lost).', reload: true };
    }
    renderSettings(true);
  }

  // ----- devices for the pickers (from GET /api/advanced) -----
  function devices() {
    const items = adv && Array.isArray(adv.switches) ? adv.switches.flatMap((g) => g.items || []) : [];
    const lights = adv && Array.isArray(adv.lights) ? adv.lights : [];
    const vsp = adv && Array.isArray(adv.vsp) ? adv.vsp.map((p) => ({ ...p, kind: 'vsp' })) : [];
    return { items: [...items, ...vsp], lights };
  }

  function devLabel(key) {
    const { items, lights } = devices();
    const d = items.find((x) => x.key === key) || lights.find((x) => x.key === key);
    return (d && d.label) || HOME_LABELS[key] || null;
  }

  function devOptions(kind, current) {
    const { items, lights } = devices();
    let list;
    // Which device kinds the server accepts for each use (FITS in app/service.py).
    // Toggles may also use the devices already mapped to the old guest toggles.
    const roleOk = (d) => kind === 'toggle' && ['bubbles', 'spillover', 'water_features'].includes(d.role);
    const fits = { switch: ['pump', 'heater', 'heatpump', 'aux', 'vsp'], scene: ['scene'], toggle: ['aux', 'light', 'scene'] };
    if (kind === 'light') list = lights.map((l) => ({ key: l.key, label: l.label }));
    else list = items.filter((d) => (fits[kind].includes(d.kind) || roleOk(d)) && (!d.placeholder || d.key === current));
    const opts = list.map((d) => [d.key, d.label && d.label !== d.key ? `${d.label} (${d.key})` : d.key]);
    if (current && !list.some((d) => d.key === current)) {
      const lab = devLabel(current);
      opts.unshift([current, adv ? `${current} (not on the panel)` : (lab ? `${lab} (${current})` : current)]);
    }
    if (!current) opts.unshift(['', 'Choose a device…', { disabled: true }]);
    return opts;
  }

  function firstKey(kind, prev) {
    const opts = devOptions(kind, '').filter(([v]) => v);
    if (prev && opts.some(([v]) => v === prev)) return prev;
    return opts.length ? opts[0][0] : '';
  }

  // ----- validation (mirrors the server's rules) -----
  const squash = (v) => String(v || '').split(/\s+/).filter(Boolean).join(' ');

  const LIMITS = [
    ['spa_min', 'Spa set point, lowest'], ['spa_max', 'Spa set point, highest'],
    ['pool_heat_min', 'Pool heat, lowest'], ['pool_heat_max', 'Pool heat, highest'],
    ['pool_chill_max', 'Pool chill, highest'], ['min_spread', 'Chill above heat by at least'],
  ];

  function validate() {
    const e = {};
    if (!draft) return e;
    const L = draft.limits || {};
    for (const [k] of LIMITS) {
      const v = L[k];
      if (!isInt(v)) e['limits.' + k] = 'Enter a whole number.';
      else if (k === 'min_spread' ? v < 0 || v > PANEL_MAX - PANEL_MIN : v < PANEL_MIN || v > PANEL_MAX) {
        e['limits.' + k] = k === 'min_spread' ? `Between 0 and ${PANEL_MAX - PANEL_MIN}.` : `Between ${PANEL_MIN} and ${PANEL_MAX}.`;
      }
    }
    const ok = (k) => !e['limits.' + k];
    if (ok('spa_min') && ok('spa_max') && L.spa_min > L.spa_max) e['limits.spa_max'] = `At least the lowest spa set point (${L.spa_min}).`;
    if (ok('pool_heat_min') && ok('pool_heat_max') && L.pool_heat_min > L.pool_heat_max) {
      e['limits.pool_heat_max'] = `At least the lowest pool heat (${L.pool_heat_min}).`;
    }
    if (ok('pool_heat_min') && ok('min_spread') && ok('pool_chill_max') && L.pool_heat_min + L.min_spread > L.pool_chill_max) {
      e['limits.pool_chill_max'] = `At least lowest pool heat + spread (${L.pool_heat_min + L.min_spread}).`;
    }
    for (const list of ['hot_tub_on', 'hot_tub_off']) {
      const steps = draft[list];
      if (steps.length > MAX_STEPS) e[list] = `At most ${MAX_STEPS} steps.`;
      steps.forEach((s, i) => {
        if (s.action === 'spa_setpoint_max') {
          if (!isInt(s.value) || s.value < PANEL_MIN || s.value > PANEL_MAX) e[`${list}.${i}`] = `A whole number from ${PANEL_MIN} to ${PANEL_MAX}.`;
        } else if (!s.key) e[`${list}.${i}`] = 'Choose a device.';
      });
    }
    const ids = new Set();
    if (draft.guest_toggles.length > MAX_TOGGLES) e.guest_toggles = `At most ${MAX_TOGGLES} toggles.`;
    draft.guest_toggles.forEach((t, i) => {
      const p = `guest_toggles.${i}`;
      const label = squash(t.label);
      if (!label) e[p + '.label'] = 'Give it a label.';
      else if (label.length > 30) e[p + '.label'] = 'At most 30 characters.';
      else if (!/^[a-z0-9_]{1,30}$/.test(t.id || '') || ids.has(t.id)) e[p + '.label'] = 'Use a different label (its id clashes with another toggle).';
      ids.add(t.id);
      if (!t.key) e[p + '.key'] = 'Choose a device.';
      if (!Array.isArray(t.modes) || !t.modes.length) e[p + '.modes'] = 'Pick Pool, Spa or both.';
    });
    const w = draft.weather;
    const zip = String(w.zip || '').trim();
    if (zip && !/^[A-Za-z0-9 -]{2,10}$/.test(zip)) e['weather.zip'] = '2 to 10 letters, digits, spaces or dashes.';
    if (squash(w.label).length > 40) e['weather.label'] = 'At most 40 characters.';
    return e;
  }

  // A 422's detail -> the field it names (when the form has a slot for it).
  function fieldOf(msg) {
    const m = String(msg || '');
    const rules = [
      [/\b(spa_min|spa_max|pool_heat_min|pool_heat_max|pool_chill_max|min_spread)\b/i, (x) => 'limits.' + x[1].toLowerCase()],
      [/guest_toggles\D{0,3}(\d+)\D{0,3}(label|modes|key)\b/, (x) => `guest_toggles.${x[1]}.${x[2]}`],
      [/\bguest_toggles?\b/, () => 'guest_toggles'],
      [/\b(hot_tub_on|hot_tub_off)\D{0,3}(\d+)/, (x) => `${x[1]}.${x[2]}`],
      [/\b(hot_tub_on|hot_tub_off)\b/, (x) => x[1]],
      [/\bweather\.(zip|label)\b/, (x) => 'weather.' + x[1]],
      [/\bzip\b/i, () => 'weather.zip'],
      [/\bweather\b/i, () => 'weather'],
      [/\bmain_page\b/, () => 'main_page'],
      [/\blimits?\b/i, () => 'limits'],
    ];
    for (const [re, f] of rules) {
      const x = re.exec(m);
      if (x) {
        const field = f(x);
        if (setView.querySelector(`[data-err="${CSS.escape(field)}"]`)) return field;
      }
    }
    return null;
  }

  function errSlot(path) {
    return h('p', { class: 'field-err', 'data-err': path, role: 'alert', hidden: true });
  }

  function updateErrors() {
    const errs = validate();
    for (const n of setView.querySelectorAll('[data-err]')) {
      const path = n.dataset.err;
      const msg = errs[path] || (serverErr && serverErr.field === path ? serverErr.msg : '');
      n.textContent = msg;
      n.hidden = !msg;
      const field = n.previousElementSibling;
      if (field && field.matches && field.matches('input, select, .field, .chk-group')) {
        const inp = field.matches('input, select') ? field : field.querySelector('input, select');
        if (inp) { if (msg) inp.setAttribute('aria-invalid', 'true'); else inp.removeAttribute('aria-invalid'); }
      }
    }
    return errs;
  }

  function updateActions() {
    const errs = updateErrors();
    const can = editable();
    const dirty = isDirty();
    $('set-actions').hidden = !(owner.enabled && owner.unlocked && cfg);
    $('set-reset-wrap').hidden = !(owner.enabled && owner.unlocked && cfg);
    $('set-save').disabled = !can || !dirty || Object.keys(errs).length > 0;
    $('set-save').textContent = saving ? 'Saving…' : 'Save';
    $('set-discard').disabled = !can || !dirty;
    $('set-reset').disabled = !can;
    $('set-actions').classList.toggle('dirty', dirty);
    const n = Object.keys(errs).length;
    $('set-status').textContent = saving ? 'Saving…'
      : n && dirty ? `Fix ${n === 1 ? 'the highlighted field' : `${n} highlighted fields`} to save.`
        : dirty ? 'Unsaved changes.' : statusMsg;
    const top = $('set-top');
    top.textContent = '';
    const text = topMsg ? topMsg.text : serverErr && !serverErr.field ? serverErr.msg : '';
    top.hidden = !text;
    if (text) {
      top.append(h('span', { text }));
      if (topMsg && topMsg.reload) {
        top.append(h('button', { type: 'button', class: 'btn-secondary btn-small', onclick: () => { resetDraft(); statusMsg = ''; renderSettings(true); } }, 'Reload'));
      }
    }
  }

  function changed() {
    statusMsg = '';
    if (serverErr) serverErr = null;
    updateActions();
  }

  // ----- forms -----
  function sectionBody(id) { return $(id).querySelector('.set-body'); }

  function mainForm(ro) {
    const LABELS = {
      weather: 'Weather card', weather_chart: '6-hour forecast chart', swim: 'Swim rating',
      temps: 'Water and air temperature', mode_switch: 'Hot Tub On / Off buttons', light: 'Light switch',
      light_color: 'Light color', setpoints: 'Set temperatures', toggles: 'Guest toggles',
    };
    const mp = draft.main_page;
    return [
      h('p', { class: 'note', text: 'What guests see on the main page.' }),
      h('div', { class: 'chk-group' }, Object.keys(mp).map((k) => h('label', { class: 'check' },
        h('input', { type: 'checkbox', 'data-fid': 'mp:' + k, checked: mp[k] !== false, disabled: ro,
          onchange: (e) => { mp[k] = e.currentTarget.checked; changed(); } }),
        h('span', { text: LABELS[k] || pretty(k) })))),
      errSlot('main_page'),
    ];
  }

  function numInput(fid, value, ro, onValue, extra) {
    const input = h('input', {
      type: 'number', inputmode: 'numeric', step: '1', class: 'num-input', 'data-fid': fid,
      value: value == null ? '' : String(value), disabled: ro, ...(extra || {}),
      oninput: () => { onValue(input.value === '' ? null : Number(input.value)); changed(); },
    });
    return input;
  }

  function limitsForm(ro) {
    const L = draft.limits;
    return [
      h('p', { class: 'note', text: `The ranges guests can pick on the main page (${unit()}). The server enforces them too.` }),
      h('div', { class: 'limits-grid' }, LIMITS.map(([k, label]) => h('div', { class: 'lim' },
        h('label', { class: 'field-label', for: 'lim-' + k, text: label }),
        h('div', { class: 'num-unit' },
          numInput('lim:' + k, L[k], ro, (v) => { L[k] = v; },
            { id: 'lim-' + k, min: k === 'min_spread' ? '0' : String(PANEL_MIN), max: String(PANEL_MAX) }),
          h('span', { class: 'unit-suffix', text: '°' })),
        errSlot('limits.' + k)))),
      errSlot('limits'),
    ];
  }

  const STEP_TYPES = [['switch', 'Switch'], ['scene', 'OneTouch scene'], ['light', 'Light'], ['spa_setpoint_max', 'Spa set point cap']];

  function retype(step, action) {
    if (action === 'spa_setpoint_max') {
      const max = draft.limits && isInt(draft.limits.spa_max) ? draft.limits.spa_max : 103;
      return { action, value: max };
    }
    if (action === 'light') return { action, key: firstKey('light', step.key), on: true, effect: null };
    return { action, key: firstKey(action, step.key), on: step.on !== false };
  }

  function moveBtns(list, i, label, ro, onRemove) {
    if (ro) return null;
    const move = (d) => {
      const j = i + d;
      if (j < 0 || j >= list.length) return;
      [list[i], list[j]] = [list[j], list[i]];
      changed();
      renderForms(`${label}:${j}:${d < 0 ? 'up' : 'down'}`);
    };
    return h('div', { class: 'row-btns' },
      h('button', { type: 'button', class: 'mini-btn', 'data-fid': `${label}:${i}:up`, disabled: i === 0, 'aria-label': 'Move up', title: 'Move up', onclick: () => move(-1) }, '↑'),
      h('button', { type: 'button', class: 'mini-btn', 'data-fid': `${label}:${i}:down`, disabled: i === list.length - 1, 'aria-label': 'Move down', title: 'Move down', onclick: () => move(1) }, '↓'),
      h('button', { type: 'button', class: 'mini-btn mini-del', 'aria-label': 'Remove', title: 'Remove', onclick: onRemove }, '✕'));
  }

  function stepRow(listName, steps, i, ro) {
    const s = steps[i];
    const fid = (x) => `${listName}.${i}.${x}`;
    const n = i + 1;
    const typeSel = select({ 'data-fid': fid('action'), disabled: ro, 'aria-label': `Step ${n} type`,
      onchange: (e) => { steps[i] = retype(s, e.currentTarget.value); changed(); renderForms(fid('action')); } },
    STEP_TYPES.some(([v]) => v === s.action) ? STEP_TYPES : [[s.action, String(s.action)], ...STEP_TYPES], s.action);
    let detail;
    if (s.action === 'spa_setpoint_max') {
      detail = h('div', { class: 'step-detail' },
        h('span', { class: 'step-text', text: 'Lower the spa set point to at most (only ever lowers it; the spa limit\'s highest still applies)' }),
        h('div', { class: 'num-unit' },
          numInput(fid('value'), s.value, ro, (v) => { s.value = v; }, { min: String(PANEL_MIN), max: String(PANEL_MAX), 'aria-label': `Step ${n} spa set point cap` }),
          h('span', { class: 'unit-suffix', text: '°' })));
    } else {
      const kind = s.action === 'light' ? 'light' : s.action === 'scene' ? 'scene' : 'switch';
      const dev = select({ 'data-fid': fid('key'), disabled: ro, 'aria-label': `Step ${n} device`,
        onchange: (e) => { s.key = e.currentTarget.value; if (s.action === 'light') s.effect = null; changed(); if (s.action === 'light') renderForms(fid('key')); } },
      devOptions(kind, s.key), s.key);
      let second;
      if (s.action === 'light') {
        const l = devices().lights.find((x) => x.key === s.key);
        const effects = l && Array.isArray(l.effects) ? l.effects : [];
        const opts = [['', 'On (keep its effect)'], ...effects.map((x) => [x, `On, ${x}`])];
        if (s.effect && !effects.includes(s.effect)) opts.push([s.effect, `On, ${s.effect}`]);
        second = select({ 'data-fid': fid('effect'), disabled: ro, 'aria-label': `Step ${n} light effect`,
          onchange: (e) => { s.effect = e.currentTarget.value || null; changed(); } }, opts, s.effect || '');
      } else {
        second = select({ 'data-fid': fid('on'), disabled: ro, class: 'onoff', 'aria-label': `Step ${n} on or off`,
          onchange: (e) => { s.on = e.currentTarget.value === 'on'; changed(); } }, [['on', 'On'], ['off', 'Off']], s.on === false ? 'off' : 'on');
      }
      detail = h('div', { class: 'step-detail' }, dev, second);
    }
    return h('li', { class: 'step' },
      h('div', { class: 'step-head' },
        h('span', { class: 'step-n', text: n + '.' }), typeSel,
        moveBtns(steps, i, listName, ro, () => { steps.splice(i, 1); changed(); renderForms(`${listName}:add`); })),
      detail,
      errSlot(`${listName}.${i}`));
  }

  function hottubForm(ro) {
    const block = (listName, title, note) => {
      const steps = draft[listName];
      return h('div', { class: 'steps-block' },
        h('h4', { class: 'adv-sub-title', text: title }),
        h('p', { class: 'note', text: note }),
        steps.length ? h('ol', { class: 'steps' }, steps.map((_, i) => stepRow(listName, steps, i, ro)))
          : h('p', { class: 'note empty', text: 'No steps: the button does nothing on the panel.' }),
        ro ? null : h('button', {
          type: 'button', class: 'btn-secondary add-btn', 'data-fid': `${listName}:add`, disabled: steps.length >= MAX_STEPS,
          onclick: () => {
            steps.push({ action: 'switch', key: firstKey('switch', ''), on: listName === 'hot_tub_on' });
            changed();
            renderForms(`${listName}.${steps.length - 1}.action`);
          },
        }, steps.length >= MAX_STEPS ? `${MAX_STEPS} steps at most` : '+ Add step'),
        errSlot(listName));
    };
    return [
      block('hot_tub_on', 'Hot Tub On', 'Run in this order when a guest taps Hot Tub On. A switch already in the right state is left alone.'),
      block('hot_tub_off', 'Hot Tub Off', 'Run in this order when a guest taps Hot Tub Off.'),
    ];
  }

  const slug = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 30);

  function uniqueId(base, except) {
    const taken = new Set(draft.guest_toggles.map((t) => t.id).filter((x) => x !== except));
    let id = base || 'toggle';
    for (let n = 2; taken.has(id); n += 1) id = `${(base || 'toggle').slice(0, 27)}_${n}`;
    return id;
  }

  function renameToggle(t, label) {
    const id = uniqueId(slug(label) || 'toggle', t.id);
    if (id === t.id) return;
    for (const o of draft.guest_toggles) {
      if (Array.isArray(o.conflicts)) o.conflicts = o.conflicts.map((c) => (c === t.id ? id : c));
    }
    newIds.delete(t.id);
    newIds.add(id);
    t.id = id;
  }

  function setConflict(a, b, on) {
    for (const [x, y] of [[a, b], [b, a]]) {
      x.conflicts = (Array.isArray(x.conflicts) ? x.conflicts : []).filter((c) => c !== y.id);
      if (on) x.conflicts.push(y.id);
    }
  }

  function togglesForm(ro) {
    const list = draft.guest_toggles;
    const cards = list.map((t, i) => {
      const p = `guest_toggles.${i}`;
      const others = list.filter((o) => o !== t);
      const conflicts = new Set(Array.isArray(t.conflicts) ? t.conflicts : []);
      const labelIn = h('input', {
        type: 'text', id: `gt-${i}-label`, 'data-fid': `${p}.label`, maxlength: '30', autocomplete: 'off',
        value: t.label || '', disabled: ro,
        oninput: (e) => {
          t.label = e.currentTarget.value;
          if (newIds.has(t.id)) { renameToggle(t, t.label); const idn = $(`gt-${i}-id`); if (idn) idn.textContent = `id: ${t.id}`; }
          // The other cards name this toggle in their "Can't be on with" lists.
          for (const sp of setView.querySelectorAll('.cf-name')) if (sp.tg === t) sp.textContent = t.label || t.id;
          changed();
        },
      });
      return h('li', { class: 'tg-card' },
        h('div', { class: 'tg-head' },
          h('label', { class: 'field tg-label' }, h('span', { class: 'field-label', text: 'Label' }), labelIn, errSlot(p + '.label')),
          moveBtns(list, i, 'guest_toggles', ro, async () => {
            const ok = await confirmAction({ title: `Remove the "${t.label || t.id}" toggle?`, body: 'Guests stop seeing it once you save.', ok: 'Remove' });
            if (!ok) return;
            list.splice(i, 1);
            for (const o of list) if (Array.isArray(o.conflicts)) o.conflicts = o.conflicts.filter((c) => c !== t.id);
            newIds.delete(t.id);
            changed();
            renderForms('guest_toggles:add');
          })),
        h('label', { class: 'field' }, h('span', { class: 'field-label', text: 'Device' }),
          select({ 'data-fid': `${p}.key`, disabled: ro, onchange: (e) => { t.key = e.currentTarget.value; changed(); } }, devOptions('toggle', t.key), t.key),
          errSlot(p + '.key')),
        h('fieldset', { class: 'chk-set' },
          h('legend', { class: 'field-label', text: 'Shown in' }),
          h('div', { class: 'chk-group chk-inline' }, [['pool', 'Pool mode'], ['spa', 'Spa mode']].map(([m, ml]) => h('label', { class: 'check' },
            h('input', { type: 'checkbox', 'data-fid': `${p}.mode.${m}`, checked: Array.isArray(t.modes) && t.modes.includes(m), disabled: ro,
              onchange: (e) => {
                const set = new Set(Array.isArray(t.modes) ? t.modes : []);
                if (e.currentTarget.checked) set.add(m); else set.delete(m);
                t.modes = ['pool', 'spa'].filter((x) => set.has(x));
                changed();
              } }),
            h('span', { text: ml })))),
          errSlot(p + '.modes')),
        others.length ? h('fieldset', { class: 'chk-set' },
          h('legend', { class: 'field-label', text: "Can't be on with" }),
          h('div', { class: 'chk-group chk-inline' }, others.map((o) => h('label', { class: 'check' },
            h('input', { type: 'checkbox', 'data-fid': `${p}.cf.${o.id}`, checked: conflicts.has(o.id), disabled: ro,
              onchange: (e) => { setConflict(t, o, e.currentTarget.checked); changed(); renderForms(`${p}.cf.${o.id}`); } }),
            Object.assign(h('span', { class: 'cf-name', text: o.label || o.id }), { tg: o }))))) : null,
        h('p', { class: 'note tg-id', id: `gt-${i}-id`, text: `id: ${t.id}` }));
    });
    return [
      h('p', { class: 'note', text: 'Switches guests can use on the main page, in this order. Two toggles that can\'t be on together: the second is refused (and greyed out) while the first is on.' }),
      list.length ? h('ol', { class: 'tg-list' }, cards) : h('p', { class: 'note empty', text: 'No guest toggles.' }),
      ro ? null : h('button', {
        type: 'button', class: 'btn-secondary add-btn', 'data-fid': 'guest_toggles:add', disabled: list.length >= MAX_TOGGLES,
        onclick: () => {
          const id = uniqueId('toggle');
          newIds.add(id);
          const mode = (App.state() && App.state().mode) === 'spa' ? 'spa' : 'pool';
          list.push({ id, key: '', label: '', modes: [mode], conflicts: [] });
          changed();
          renderForms(`guest_toggles.${list.length - 1}.label`);
        },
      }, '+ Add toggle'),
      errSlot('guest_toggles'),
    ];
  }

  function weatherForm(ro) {
    const w = draft.weather;
    const field = (k, label, extra) => h('label', { class: 'field' },
      h('span', { class: 'field-label', text: label }),
      h('input', { type: 'text', 'data-fid': 'w:' + k, value: w[k] == null ? '' : String(w[k]), disabled: ro,
        autocomplete: 'off', spellcheck: 'false', ...(extra || {}),
        oninput: (e) => {
          const v = e.currentTarget.value.trim();
          w[k] = k === 'zip' ? (v || null) : v;   // the server stores "no zip" as null
          changed();
        } }),
      errSlot('weather.' + k));
    return [
      h('p', { class: 'note', text: 'Where the weather card\'s forecast is for.' }),
      field('zip', 'Zip code', { inputmode: 'text', maxlength: '10', autocapitalize: 'characters' }),
      field('label', 'Name on the card (optional)', { maxlength: '40', placeholder: 'The place name' }),
      w.country ? h('p', { class: 'note', text: `Zip codes are looked up for country "${w.country}".` }) : null,
      errSlot('weather'),
    ];
  }

  let formSig = '';

  /** Rebuild the forms; `focusFid` (else the focused field) gets focus back. */
  function renderForms(focusFid) {
    const active = document.activeElement;
    const keep = focusFid || (active && setView.contains(active) ? active.dataset.fid : null);
    const ro = !editable();
    if (!cfg || !draft) {
      const st = App.state();
      const msg = st && typeof st.config_version === 'number' ? 'Loading settings…'
        : "This server doesn't support changing settings yet.";
      for (const id of ['set-main', 'set-limits', 'set-hottub', 'set-toggles', 'set-weather']) {
        sectionBody(id).textContent = '';
      }
      sectionBody('set-main').append(h('p', { class: 'note set-placeholder', text: msg }));
      updateActions();
      return;
    }
    const parts = [['set-main', mainForm], ['set-limits', limitsForm], ['set-hottub', hottubForm],
      ['set-toggles', togglesForm], ['set-weather', weatherForm]];
    for (const [id, f] of parts) {
      const b = sectionBody(id);
      b.textContent = '';
      b.append(...f(ro).flat().filter(Boolean));
    }
    setView.classList.toggle('read-only', ro);
    updateActions();
    if (keep) {
      const n = setView.querySelector(fidSel(keep));
      if (n && !n.disabled) n.focus({ preventScroll: true });
    }
  }

  function devSig() {
    const { items, lights } = devices();
    return JSON.stringify([items.map((d) => [d.key, d.label, d.kind, d.placeholder]), lights.map((l) => [l.key, l.label, l.effects])]);
  }

  function renderSettings(force) {
    renderOwnerBox($('set-owner'), 'set');
    const sig = JSON.stringify([cfg && cfg.version, baseVersion, editable(), devSig(), !!cfg]);
    if (force || sig !== formSig) {
      formSig = sig;
      renderForms();
    } else {
      updateActions();
    }
  }

  // ----- save / discard / reset -----
  function saveFailed(e) {
    if (e.status === 401 || e.status === 403) { ownerLost(e.status); return; }
    if (e.status === 409) {
      topMsg = { text: 'These settings were changed somewhere else, so yours weren\'t saved. Reload to see them (your unsaved changes here will be lost).', reload: true };
    } else if (e.status === 422) {
      serverErr = { field: fieldOf(e.message), msg: e.message };
    } else if (e.status === 503) {
      serverErr = { field: null, msg: "Settings storage isn't available, so nothing was saved. The app keeps running on its current settings." };
    } else {
      serverErr = { field: null, msg: `Couldn't save: ${e.message}` };
    }
  }

  function adopt(doc, msg) {
    cfg = doc;
    resetDraft();
    statusMsg = msg;
    App.adoptConfig(doc);   // the main page follows right away
  }

  $('set-save').addEventListener('click', async () => {
    if (!editable() || !isDirty() || Object.keys(validate()).length) return;
    saving = true;
    serverErr = null;
    topMsg = null;
    const body = { ...clone(draft), version: baseVersion };
    for (const t of body.guest_toggles) t.label = squash(t.label);
    body.weather.label = squash(body.weather.label);
    renderSettings();
    try {
      const doc = await req('PUT', '/api/config', body, POST_TIMEOUT_MS);
      saving = false;
      adopt(doc, 'Saved.');
    } catch (e) {
      saving = false;
      saveFailed(e);
    }
    renderSettings(true);
    if (serverErr && serverErr.field) {
      const n = setView.querySelector(`[data-err="${CSS.escape(serverErr.field)}"]`);
      if (n) n.scrollIntoView({ block: 'center' });
    } else if (serverErr || topMsg) {
      $('set-top').scrollIntoView({ block: 'center' });
    }
  });

  $('set-discard').addEventListener('click', () => {
    resetDraft();
    statusMsg = '';
    renderSettings(true);
  });

  $('set-reset').addEventListener('click', async () => {
    if (!editable()) return;
    const ok = await confirmAction({
      title: 'Reset all settings to defaults?',
      body: 'Main page, Temperature limits, Hot Tub On / Off, Guest toggles and Weather go back to the server\'s defaults. This can\'t be undone.',
      ok: 'Reset', danger: true,
    });
    if (!ok) return;
    saving = true;
    serverErr = null;
    topMsg = null;
    renderSettings();
    try {
      const doc = await req('POST', '/api/config/reset', null, POST_TIMEOUT_MS);
      saving = false;
      adopt(doc, 'Back to the defaults.');
    } catch (e) {
      saving = false;
      saveFailed(e);
    }
    renderSettings(true);
  });

  // ---------- wiring ----------
  function onOwnerChange() {
    if (!owner.unlocked) {
      adv = null;
      advError = null;
      for (const k of Object.keys(drafts)) delete drafts[k];
      clearTimeout(advTimer);
      advTimer = null;
    }
    renderAdv();
    renderSettings();
    onVisibility();
    if (owner.unlocked && advShown()) loadAdv();
  }

  App.subscribe((s, c) => {
    const changedOwner = takeStatus(s);
    onConfig(c);
    if (changedOwner) onOwnerChange();
    else { renderAdv(); renderSettings(); }
  });
  renderAdv();
  renderSettings(true);
})();
