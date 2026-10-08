/* Pool & Spa: the Settings panel (gear, top right).
 *
 * - Opens/closes the Settings sheet (a modal <dialog>): ✕, Escape, or a tap on
 *   the backdrop close it, and focus goes back to the gear.
 * - A ☰ menu in the sheet's header switches between its three sections:
 *   Settings (default; app configuration + QR codes), Advanced (owner
 *   controls) and Equipment status. The header names the current section and
 *   takes focus when one is picked. Escape closes an open menu first.
 * - Equipment status, Settings and Advanced content are drawn by app.js on
 *   every poll; this file only shows and hides them.
 * - QR codes: builds "open the pool page" and "join the Wi-Fi" codes in the
 *   browser with the vendored qrcode-generator (no network calls), and prints a
 *   sign with them. Nothing typed here is sent to the server or stored: there is
 *   no form submission, no fetch and no localStorage in this file.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var btn = $("settings-btn");
  var dlg = $("settings");
  var closeBtn = $("settings-close");
  if (!btn || !dlg) return;

  // ---------- open / close ----------
  var menuBtn = $("settings-menu-btn");
  var menu = $("settings-menu");
  var title = $("settings-h");
  var items = Array.prototype.slice.call(menu.querySelectorAll(".menu-item"));
  var views = items.map(function (b) { return $(b.getAttribute("data-view")); });

  // ---------- section menu ----------
  function menuOpen() { return !menu.hidden; }
  function openMenu() {
    menu.hidden = false;
    menuBtn.setAttribute("aria-expanded", "true");
    var cur = menu.querySelector('[aria-current="page"]') || items[0];
    cur.focus();
  }
  function closeMenu(refocus) {
    if (!menuOpen()) return;
    menu.hidden = true;
    menuBtn.setAttribute("aria-expanded", "false");
    if (refocus) menuBtn.focus();
  }
  /** Show one section; the header names it. */
  function show(id) {
    var name = null;
    items.forEach(function (b, i) {
      var on = b.getAttribute("data-view") === id;
      if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
      if (views[i]) views[i].hidden = !on;
      if (on && views[i]) name = views[i].getAttribute("data-title");
    });
    title.textContent = name || "Settings";
  }
  menuBtn.addEventListener("click", function () {
    if (menuOpen()) closeMenu(true); else openMenu();
  });
  menu.addEventListener("click", function (e) {
    var b = e.target.closest ? e.target.closest(".menu-item") : null;
    if (!b) return;
    show(b.getAttribute("data-view"));
    closeMenu(false);
    dlg.scrollTop = 0;
    title.focus();
  });
  // Arrow keys / Home / End move between the items (Tab works too).
  menu.addEventListener("keydown", function (e) {
    var i = items.indexOf(document.activeElement);
    if (i < 0) return;
    var j = e.key === "ArrowDown" ? (i + 1) % items.length
      : e.key === "ArrowUp" ? (i - 1 + items.length) % items.length
      : e.key === "Home" ? 0 : e.key === "End" ? items.length - 1 : -1;
    if (j < 0) return;
    e.preventDefault();
    items[j].focus();
  });
  // Escape closes the menu, not the sheet. Preventing the keydown's default
  // stops the <dialog> from turning it into a close request.
  dlg.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && menuOpen()) {
      e.preventDefault();
      e.stopPropagation();
      closeMenu(true);
    }
  }, true);
  // Backup for browsers that still send `cancel`: keep the sheet open then.
  dlg.addEventListener("cancel", function (e) {
    if (menuOpen()) { e.preventDefault(); closeMenu(true); }
  });
  // Clicking anywhere else in the sheet (or tabbing out of the menu) closes it.
  dlg.addEventListener("click", function (e) {
    if (menuOpen() && !menu.contains(e.target) && !menuBtn.contains(e.target)) closeMenu(false);
  }, true);
  menu.addEventListener("focusout", function (e) {
    if (menuOpen() && e.relatedTarget && !menu.contains(e.relatedTarget) && e.relatedTarget !== menuBtn) closeMenu(false);
  });

  function open() {
    if (dlg.open) return;
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "");          // very old browsers: shown inline
    btn.setAttribute("aria-expanded", "true");
    renderPreviews();
    closeBtn.focus();
  }
  function close() {
    if (!dlg.open) return;
    if (typeof dlg.close === "function") dlg.close();
    else { dlg.removeAttribute("open"); onClosed(); }
  }
  function onClosed() {
    closeMenu(false);
    btn.setAttribute("aria-expanded", "false");
    document.body.classList.remove("printing-sign");
    btn.focus();
  }
  btn.setAttribute("aria-expanded", "false");
  btn.addEventListener("click", open);
  closeBtn.addEventListener("click", close);
  // The sheet's content fills the <dialog> edge to edge, so a click whose
  // target is the dialog itself landed on the backdrop.
  dlg.addEventListener("click", function (e) { if (e.target === dlg) close(); });
  dlg.addEventListener("close", onClosed);    // also fires for Escape

  // ---------- QR codes ----------
  var urlIn = $("qr-url");
  var ssidIn = $("qr-ssid");
  var passIn = $("qr-pass");
  var secIn = $("qr-sec");
  var hiddenIn = $("qr-hidden");
  var titleIn = $("qr-title");
  var printBtn = $("qr-print");
  var urlBox = $("qr-url-code");
  var wifiBox = $("qr-wifi-code");

  var haveLib = typeof window.qrcode === "function";
  // Encode text as UTF-8 bytes (the library's default keeps only the low byte of
  // each character, which would garble a non-ASCII network name).
  if (haveLib && window.qrcode.stringToBytesFuncs && window.qrcode.stringToBytesFuncs["UTF-8"]) {
    window.qrcode.stringToBytes = window.qrcode.stringToBytesFuncs["UTF-8"];
  }

  try { urlIn.value = window.location.origin; } catch (e) { /* leave empty */ }

  // Wi-Fi QR format (ZXing "WIFI:" URI, read by iOS and Android cameras):
  // backslash-escape \ ; , : " in the network name and password.
  function wifiEscape(s) { return String(s).replace(/([\\;,:"])/g, "\\$1"); }

  function wifiPayload() {
    var ssid = ssidIn.value;
    if (ssid === "") return { empty: true };
    var sec = secIn.value === "nopass" ? "nopass" : "WPA";
    var pass = passIn.value;
    if (sec === "WPA" && pass === "") return { error: "Enter the Wi-Fi password, or choose Security: None." };
    var p = "WIFI:T:" + sec + ";S:" + wifiEscape(ssid) + ";";
    if (sec !== "nopass") p += "P:" + wifiEscape(pass) + ";";
    p += "H:" + (hiddenIn.checked ? "true" : "false") + ";;";
    return { text: p, ssid: ssid };
  }

  function urlPayload() {
    var u = urlIn.value.trim();
    if (u === "") return { empty: true, error: "Enter the address of this page." };
    return { text: u };
  }

  /** An SVG QR code (error correction M, 4-module quiet zone), or throws. */
  function qrSvg(text, label) {
    var qr = window.qrcode(0, "M");             // 0 = smallest version that fits
    qr.addData(text, "Byte");
    qr.make();
    var n = qr.getModuleCount();
    var q = 4;
    var size = n + 2 * q;
    var d = "";
    for (var r = 0; r < n; r += 1) {
      var c = 0;
      while (c < n) {
        if (!qr.isDark(r, c)) { c += 1; continue; }
        var start = c;
        while (c < n && qr.isDark(r, c)) c += 1;
        d += "M" + (start + q) + " " + (r + q) + "h" + (c - start) + "v1h-" + (c - start) + "z";
      }
    }
    var NS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 " + size + " " + size);
    svg.setAttribute("shape-rendering", "crispEdges");
    svg.setAttribute("class", "qr-svg");
    if (label) { svg.setAttribute("role", "img"); svg.setAttribute("aria-label", label); }
    else svg.setAttribute("aria-hidden", "true");
    var bg = document.createElementNS(NS, "rect");
    bg.setAttribute("width", String(size));
    bg.setAttribute("height", String(size));
    bg.setAttribute("fill", "#fff");
    var path = document.createElementNS(NS, "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "#000");
    svg.append(bg, path);
    return svg;
  }

  function note(text) {
    var p = document.createElement("p");
    p.className = "note qr-msg";
    p.textContent = text;
    return p;
  }

  /** Draw `payload` into `box`; returns true when a code was drawn. */
  function draw(box, payload, label) {
    box.textContent = "";
    if (!haveLib) { box.append(note("The QR code generator didn't load. Reload the page.")); return false; }
    if (payload.error) { box.append(note(payload.error)); return false; }
    if (payload.empty || !payload.text) return false;
    try { box.append(qrSvg(payload.text, label)); return true; }
    catch (e) { box.append(note("Too long for a QR code.")); return false; }
  }

  var canPrint = false;
  function renderPreviews() {
    var u = urlPayload();
    var urlOk = draw(urlBox, u, u.text ? "QR code that opens " + u.text : null);
    var w = wifiPayload();
    var wifiOk = draw(wifiBox, w, w.ssid ? "QR code that joins the Wi-Fi network " + w.ssid : null);
    // A Wi-Fi section that's filled in but can't be encoded blocks printing,
    // so the sign never silently leaves out a code the owner asked for.
    canPrint = urlOk && (w.empty || wifiOk);
    printBtn.disabled = !canPrint;
  }

  for (var inp of [urlIn, ssidIn, passIn, secIn, hiddenIn]) {
    inp.addEventListener("input", renderPreviews);
    inp.addEventListener("change", renderPreviews);
  }
  // A password makes no sense without security, and vice versa.
  secIn.addEventListener("change", function () { passIn.disabled = secIn.value === "nopass"; renderPreviews(); });

  // ---------- printed sign ----------
  function buildSign() {
    var u = urlPayload();
    var w = wifiPayload();
    $("ps-title").textContent = titleIn.value.trim() || "Pool & Spa";
    var wifiFig = $("ps-wifi");
    var wifiCode = $("ps-wifi-code");
    wifiCode.textContent = "";
    var withWifi = !!w.text;
    wifiFig.hidden = !withWifi;
    if (withWifi) {
      wifiCode.append(qrSvg(w.text, null));
      $("ps-ssid").textContent = "Network: " + w.ssid;
    }
    var urlCode = $("ps-url-code");
    urlCode.textContent = "";
    urlCode.append(qrSvg(u.text, null));
    $("ps-url-cap").textContent = withWifi ? "2. Open the pool controls" : "Open the pool controls";
    $("ps-url-text").textContent = u.text;
  }

  printBtn.addEventListener("click", function () {
    renderPreviews();
    if (!canPrint) return;
    try { buildSign(); } catch (e) { return; }
    document.body.classList.add("printing-sign");
    window.print();
  });
  window.addEventListener("afterprint", function () { document.body.classList.remove("printing-sign"); });

  renderPreviews();
})();
