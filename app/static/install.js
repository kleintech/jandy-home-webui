/* Pool & Spa: "add to Home Screen" nudge + service worker registration.
 *
 * - Chrome/Edge (Android, desktop): waits for `beforeinstallprompt`, then
 *   offers a button that opens the browser's own install prompt.
 * - iOS/iPadOS Safari: no programmatic prompt exists, so it shows how to do
 *   it by hand (Share, then "Add to Home Screen"). Other iOS browsers and
 *   in-app web views get nothing -- the steps differ per app.
 * - Never shown when already running as an installed app.
 * - "Remind me later" snoozes for 7 days; "Don't show again" is permanent.
 *   Both live in localStorage: per device, never sent to the server. If
 *   storage is unavailable the card shows at most once per page load.
 */
(function () {
  "use strict";

  var KEY = "pool.installNudge";            // "never" | snooze-until epoch ms
  var SNOOZE_MS = 7 * 24 * 60 * 60 * 1000;
  var DELAY_MS = 3000;

  // ---- service worker (installability only; it caches nothing) ----
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      try {
        navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {});
      } catch (e) { /* insecure context etc. */ }
    });
  }

  // ---- storage, fail-soft ----
  function readPref() {
    try { return window.localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function writePref(v) {
    try { window.localStorage.setItem(KEY, String(v)); } catch (e) { /* shown-once-per-load still applies */ }
  }
  function suppressed() {
    var v = readPref();
    if (!v) return false;
    if (v === "never") return true;
    var until = Number(v);
    return isFinite(until) && until > Date.now();
  }

  function standalone() {
    try {
      if (window.matchMedia("(display-mode: standalone)").matches) return true;
      if (window.matchMedia("(display-mode: fullscreen)").matches) return true;
      if (window.matchMedia("(display-mode: minimal-ui)").matches) return true;
    } catch (e) { /* old browser */ }
    return window.navigator.standalone === true;
  }

  if (standalone()) return;

  var ua = navigator.userAgent || "";
  var isIOS = /iPad|iPhone|iPod/.test(ua) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);   // iPadOS "desktop" UA
  // Real Safari only: other browsers and in-app views on iOS put their own token in the UA.
  var isIOSSafari = isIOS && /Safari\//.test(ua) &&
    !/CriOS|FxiOS|EdgiOS|OPiOS|OPT\/|GSA\/|YaBrowser|DuckDuckGo|FBAN|FBAV|FB_IAB|Instagram|Line\/|Snapchat|Twitter|LinkedInApp|Pinterest|WhatsApp/.test(ua);
  var isMobile = isIOS || /Android|Mobi/.test(ua);

  var deferred = null;     // beforeinstallprompt event (Chrome/Edge)
  var shown = false;       // at most once per page load, whatever storage says
  var ready = false;       // the initial delay has passed
  var card = null;
  var spacer = null;
  var ro = null;

  var SHARE_ICON =
    '<svg class="pwa-nudge-share" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">' +
    '<path d="M12 3v12M7.5 7.5L12 3l4.5 4.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>' +
    '<path d="M8 10.5H6.5A1.5 1.5 0 0 0 5 12v7.5A1.5 1.5 0 0 0 6.5 21h11a1.5 1.5 0 0 0 1.5-1.5V12a1.5 1.5 0 0 0-1.5-1.5H16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>' +
    "</svg>";

  function build(mode) {
    var el = document.createElement("section");
    el.id = "pwa-nudge";
    el.className = "pwa-nudge";
    el.setAttribute("role", "region");
    el.setAttribute("aria-labelledby", "pwa-nudge-title");

    var where = isMobile ? "Home Screen" : "computer";
    var html =
      '<img class="pwa-nudge-icon" src="/static/icons/icon-192.png" width="44" height="44" alt="">' +
      '<div class="pwa-nudge-body">' +
      '<h2 class="pwa-nudge-title" id="pwa-nudge-title">Add Pool &amp; Spa to your ' + where + "</h2>";
    if (mode === "ios") {
      html += '<p class="pwa-nudge-text">Tap ' + SHARE_ICON +
        ' <strong>Share</strong>, then <strong>Add to Home Screen</strong>.</p>';
    } else {
      html += '<p class="pwa-nudge-text">Open it like an app, one tap away.</p>';
    }
    html += '</div><div class="pwa-nudge-actions">';
    if (mode === "prompt") {
      html += '<button type="button" class="pwa-nudge-btn pwa-nudge-primary" data-act="install">' +
        (isMobile ? "Add to Home Screen" : "Install app") + "</button>";
    }
    html +=
      '<button type="button" class="pwa-nudge-btn" data-act="later">Remind me later</button>' +
      '<button type="button" class="pwa-nudge-btn" data-act="never">Don’t show again</button>' +
      "</div>";
    el.innerHTML = html;

    el.addEventListener("click", function (ev) {
      var btn = ev.target.closest ? ev.target.closest("button[data-act]") : null;
      if (!btn) return;
      var act = btn.getAttribute("data-act");
      if (act === "later") { writePref(Date.now() + SNOOZE_MS); hide(); }
      else if (act === "never") { writePref("never"); hide(); }
      else if (act === "install") { install(); }
    });
    el.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { writePref(Date.now() + SNOOZE_MS); hide(); }
    });
    return el;
  }

  function show(mode) {
    if (shown || card || suppressed() || standalone()) return;
    shown = true;
    card = build(mode);
    // Spacer at the end of the page so the card never permanently hides the last controls.
    spacer = document.createElement("div");
    spacer.className = "pwa-nudge-spacer";
    spacer.setAttribute("aria-hidden", "true");
    document.body.appendChild(spacer);
    document.body.appendChild(card);
    var fit = function () { if (card && spacer) spacer.style.height = card.offsetHeight + 16 + "px"; };
    fit();
    if (window.ResizeObserver) { ro = new ResizeObserver(fit); ro.observe(card); }
    // next frame so the entrance transition runs
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(function () { if (card) card.classList.add("pwa-nudge-in"); });
    });
  }

  function hide() {
    if (ro) { ro.disconnect(); ro = null; }
    var hadFocus = card && card.contains(document.activeElement);
    if (card && card.parentNode) card.parentNode.removeChild(card);
    if (spacer && spacer.parentNode) spacer.parentNode.removeChild(spacer);
    card = spacer = null;
    if (hadFocus && document.body) {      // don't strand keyboard focus on a removed node
      var h = document.querySelector("h1");
      if (h) { h.setAttribute("tabindex", "-1"); h.focus({ preventScroll: true }); }
    }
  }

  function install() {
    var ev = deferred;
    deferred = null;                       // a prompt event can only be used once
    hide();
    if (!ev || typeof ev.prompt !== "function") return;
    try {
      var p = ev.prompt();
      var choice = ev.userChoice;
      Promise.resolve(choice || p).then(function (res) {
        // Said no to the browser's own dialog: treat like "remind me later".
        if (res && res.outcome === "dismissed") writePref(Date.now() + SNOOZE_MS);
      }, function () {});
    } catch (e) { /* prompt() already used or not allowed */ }
  }

  function maybeShow() {
    if (!ready) return;
    if (deferred) show("prompt");
    else if (isIOSSafari) show("ios");
  }

  window.addEventListener("beforeinstallprompt", function (ev) {
    ev.preventDefault();                   // keep Chrome's mini-infobar away; we show our own card
    deferred = ev;
    maybeShow();
  });

  window.addEventListener("appinstalled", function () {
    deferred = null;
    writePref("never");                    // installed: no need to ask in this browser again
    hide();
  });

  function start() {
    window.setTimeout(function () { ready = true; maybeShow(); }, DELAY_MS);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
