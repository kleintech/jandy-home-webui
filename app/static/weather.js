/* Weather card: current conditions, a one-line outlook and a 6-hour chart.
   Self-contained; renders into <section id="weather">. Hidden whenever the
   server has no weather to show, so guests never see an error. */
(function () {
  "use strict";

  var root = document.getElementById("weather");
  if (!root) return;

  var REFRESH_MS = 10 * 60 * 1000;
  var SVGNS = "http://www.w3.org/2000/svg";
  var data = null;        // last good payload
  var lastFetch = 0;
  var timer = null;
  var lastWidth = 0;

  root.hidden = true;

  // ------------------------------------------------------------------ helpers

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function svg(tag, attrs, text) {
    var n = document.createElementNS(SVGNS, tag);
    for (var k in attrs) if (attrs[k] != null) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = text;
    return n;
  }

  function num(v) { return typeof v === "number" && isFinite(v) ? v : null; }

  // Times arrive as "2026-10-07T13:45-04:00" in the pool's own time zone. Label
  // from the string itself so a visitor's phone time zone can't relabel them.
  function clock(iso, withMinutes) {
    var m = /T(\d{2}):(\d{2})/.exec(iso || "");
    if (!m) return "";
    var h = +m[1], min = m[2];
    var ampm = h < 12 ? "AM" : "PM";
    h = h % 12 || 12;
    return (withMinutes || min !== "00") ? h + ":" + min + " " + ampm : h + " " + ampm;
  }

  function deg(v) { return Math.round(v) + "°"; }

  // ------------------------------------------------------------------ icons
  // Static markup only (no data interpolated), 48x48 viewBox.

  var CLOUD = '<path fill="var(--wx-cloud-icon)" d="M15 37h19a8 8 0 0 0 1.2-15.9A11 11 0 0 0 14.6 23.2 7 7 0 0 0 15 37z"/>';
  function sun(cx, cy, r) {
    var s = '<g stroke="var(--wx-sun)" stroke-width="2.5" stroke-linecap="round">';
    for (var i = 0; i < 8; i++) {
      var a = i * Math.PI / 4, c = Math.cos(a), d = Math.sin(a);
      s += '<line x1="' + (cx + c * (r + 4)).toFixed(1) + '" y1="' + (cy + d * (r + 4)).toFixed(1) +
           '" x2="' + (cx + c * (r + 8)).toFixed(1) + '" y2="' + (cy + d * (r + 8)).toFixed(1) + '"/>';
    }
    return s + '</g><circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="var(--wx-sun)"/>';
  }
  function moon(cx, cy, r) {
    return '<path fill="var(--wx-sun)" d="M' + (cx + r * 0.3) + ' ' + (cy - r) +
      'a' + r + ' ' + r + ' 0 1 0 ' + (r * 0.75) + ' ' + (r * 1.55) +
      'a' + (r * 0.85) + ' ' + (r * 0.85) + ' 0 0 1 -' + (r * 0.75) + ' -' + (r * 1.55) + 'z"/>';
  }
  function drops(color, len, n) {
    var s = '<g stroke="' + color + '" stroke-width="2.5" stroke-linecap="round">';
    var xs = n === 2 ? [19, 29] : [16, 24, 32];
    for (var i = 0; i < xs.length; i++) {
      s += '<line x1="' + xs[i] + '" y1="36" x2="' + (xs[i] - 2) + '" y2="' + (36 + len) + '"/>';
    }
    return s + '</g>';
  }
  var UP = '<g transform="translate(0 -6)">' + CLOUD + '</g>';
  var ICONS = {
    "clear-day": sun(24, 24, 9),
    "clear-night": moon(22, 24, 12),
    "partly-day": sun(18, 18, 7) + CLOUD,
    "partly-night": '<g transform="translate(-4 -6) scale(.75)">' + moon(22, 24, 12) + '</g>' + CLOUD,
    "cloudy": '<g transform="translate(-6 -8) scale(.7)" opacity=".6">' + CLOUD + '</g>' + CLOUD,
    "fog": '<g stroke="var(--wx-cloud-icon)" stroke-width="3" stroke-linecap="round">' +
           '<line x1="10" y1="18" x2="38" y2="18"/><line x1="6" y1="26" x2="34" y2="26"/>' +
           '<line x1="12" y1="34" x2="40" y2="34"/></g>',
    "drizzle": UP + drops("var(--wx-rain)", 4, 3),
    "rain": UP + drops("var(--wx-rain)", 8, 3),
    "snow": UP + '<g fill="var(--wx-cloud-icon)"><circle cx="17" cy="39" r="2.2"/><circle cx="25" cy="43" r="2.2"/><circle cx="33" cy="39" r="2.2"/></g>',
    "storm": UP + '<path fill="var(--wx-sun)" d="M26 30l-7 10h5l-2 7 8-11h-5l3-6z"/>'
  };

  function iconFor(cur) {
    var k = cur.icon;
    if (k === "clear" || k === "partly") k += cur.is_day ? "-day" : "-night";
    return ICONS[k] || ICONS.cloudy;
  }

  // ------------------------------------------------------------------ render

  function render() {
    var d = data, c = d.current;
    root.textContent = "";

    var head = el("div", "wx-head");
    var h2 = el("h2", null, "Weather");
    h2.id = "wx-h";
    head.appendChild(h2);
    head.appendChild(el("span", "wx-where", d.location || ""));
    root.appendChild(head);

    var now = el("div", "wx-now");
    var icon = el("div", "wx-icon");
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = '<svg viewBox="0 0 48 48">' + iconFor(c) + "</svg>";
    now.appendChild(icon);

    var t = el("div", "wx-temp", String(Math.round(c.temp_f)));
    t.appendChild(el("span", "unit", "°F"));
    now.appendChild(t);

    var cond = el("div", "wx-cond");
    cond.appendChild(el("div", "wx-desc", c.description || ""));
    var meta = el("div", "wx-meta");
    if (num(c.feels_like_f) != null) meta.appendChild(el("span", null, "Feels " + deg(c.feels_like_f)));
    if (num(c.wind_mph) != null) meta.appendChild(el("span", null, "Wind " + Math.round(c.wind_mph) + " mph"));
    if (num(c.humidity) != null) meta.appendChild(el("span", null, "Humidity " + Math.round(c.humidity) + "%"));
    cond.appendChild(meta);
    now.appendChild(cond);
    root.appendChild(now);

    if (d.summary) root.appendChild(el("p", "wx-summary", d.summary));
    if (d.stale && d.updated_at) root.appendChild(el("p", "wx-stale", "Forecast as of " + clock(d.updated_at, true)));

    var legend = el("ul", "wx-legend");
    legend.setAttribute("aria-hidden", "true");
    [["temp", "Temp"], ["rain", "Rain %"], ["cloud", "Clouds"]].forEach(function (p) {
      var li = el("li");
      li.appendChild(el("span", "wx-key " + p[0]));
      li.appendChild(document.createTextNode(p[1]));
      legend.appendChild(li);
    });
    root.appendChild(legend);

    var chart = el("div", "wx-chart");
    root.appendChild(chart);
    root.appendChild(table(d.hourly));
    drawChart(chart, d.hourly);
  }

  // Screen-reader table of the same series (the chart itself is role="img").
  function table(rows) {
    // Visually hidden via a wrapper div: a <table> ignores width:1px and would
    // widen the page on narrow phones.
    var wrap = el("div", "wx-sr");
    var tbl = el("table");
    wrap.appendChild(tbl);
    tbl.appendChild(el("caption", null, "Forecast, next 6 hours"));
    var hr = el("tr");
    ["Time", "Temp", "Rain chance", "Cloud cover"].forEach(function (h) { hr.appendChild(el("th", null, h)); });
    tbl.appendChild(hr);
    rows.forEach(function (r, i) {
      if (rows.length > 8 && !/:00/.test(r.time.slice(11, 16)) && i !== 0) return; // hourly is enough
      var tr = el("tr");
      [clock(r.time, true), deg(r.temp_f), r.precip_prob + "%", r.cloud_cover + "%"].forEach(function (v) {
        tr.appendChild(el("td", null, v));
      });
      tbl.appendChild(tr);
    });
    return wrap;
  }

  function ariaSummary(rows) {
    var temps = rows.map(function (r) { return r.temp_f; });
    var rain = rows.map(function (r) { return r.precip_prob; });
    var clouds = rows.map(function (r) { return r.cloud_cover; });
    var avg = clouds.reduce(function (a, b) { return a + b; }, 0) / clouds.length;
    return "Chart of the next 6 hours, " + clock(rows[0].time) + " to " + clock(rows[rows.length - 1].time) +
      ". Temperature from " + deg(temps[0]) + " to " + deg(temps[temps.length - 1]) +
      ", high " + deg(Math.max.apply(null, temps)) + ", low " + deg(Math.min.apply(null, temps)) +
      ". Rain chance up to " + Math.max.apply(null, rain) + "%. Cloud cover averages " + Math.round(avg) + "%.";
  }

  // Temperature scale for the left axis: four equal steps of a round size, so
  // every °F tick lands on a 0/25/50/75/100% gridline of the right axis and one
  // set of gridlines serves both. Never narrower than 8° (2° steps), so a
  // one-degree wobble doesn't look like a swing.
  function tempScale(tMin, tMax) {
    var steps = [2, 5, 10, 20, 50], s = 2, lo = 0;
    for (var k = 0; k < steps.length; k++) {
      s = steps[k];
      lo = Math.floor((tMin - 0.5) / s) * s;
      if (lo + 4 * s >= tMax + 0.5) break;
    }
    // Slide down a step at a time while that centers the line better.
    var mid = (tMin + tMax) / 2;
    while (lo - s + 4 * s >= tMax + 0.5 && mid - (lo + 2 * s) < -s / 2) lo -= s;
    return { lo: lo, hi: lo + 4 * s, step: s };
  }

  function drawChart(host, rows) {
    host.textContent = "";
    var W = Math.max(240, Math.round(host.clientWidth || root.clientWidth || 320));
    lastWidth = W;

    // One plot area, two y-axes: °F on the left, 0-100% on the right.
    var padL = 34, padR = 34;
    var top = 14, plotH = 136;
    var base = top + plotH;
    var axisY = base + 17;
    var H = axisY + 5;
    var plotW = W - padL - padR;
    var x0 = padL, x1 = padL + plotW;

    var t0 = Date.parse(rows[0].time), t1 = Date.parse(rows[rows.length - 1].time);
    var span = Math.max(1, t1 - t0);
    var xs = rows.map(function (r) { return x0 + (Date.parse(r.time) - t0) / span * plotW; });

    var temps = rows.map(function (r) { return r.temp_f; });
    var tMin = Math.min.apply(null, temps), tMax = Math.max.apply(null, temps);
    var sc = tempScale(tMin, tMax);
    function ty(v) { return base - (v - sc.lo) / (sc.hi - sc.lo) * plotH; }
    function py(v) { return base - Math.max(0, Math.min(100, v)) / 100 * plotH; }

    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img",
                         "aria-label": ariaSummary(rows) });


    // Left axis: temperature in degrees (text color); right axis: % (muted).
    for (var q = 0; q <= 4; q++) {
      var v = sc.lo + q * sc.step, y = py(q * 25) + 4;
      s.appendChild(svg("text", { "class": "axis axis-t", x: x0 - 5, y: y, "text-anchor": "end" },
        v + "°"));
    }
    [0, 50, 100].forEach(function (v) {
      s.appendChild(svg("text", { "class": "axis axis-p", x: x1 + 5, y: py(v) + 4 }, v + "%"));
    });

    // Cloud cover: grey area behind everything (% scale).
    var cloudPath = "M" + x0 + " " + base;
    rows.forEach(function (r, i) { cloudPath += "L" + xs[i].toFixed(1) + " " + py(r.cloud_cover).toFixed(1); });
    cloudPath += "L" + x1 + " " + base + "Z";
    s.appendChild(svg("path", { "class": "cloud", d: cloudPath }));

    // Rain chance: blue line over a light wash (% scale).
    var rainLine = "";
    rows.forEach(function (r, i) { rainLine += (i ? "L" : "M") + xs[i].toFixed(1) + " " + py(r.precip_prob).toFixed(1); });
    s.appendChild(svg("path", { "class": "rain-area", d: rainLine + "L" + x1 + " " + base + "L" + x0 + " " + base + "Z" }));
    // Gridlines at the quarter marks, shared by both axes (each °F tick sits on
    // one). Drawn over the areas, translucent, so they read through the clouds.
    [25, 50, 75, 100].forEach(function (v) {
      var y = Math.round(py(v)) + .5;
      s.appendChild(svg("line", { "class": "grid", x1: x0, x2: x1, y1: y, y2: y }));
    });
    s.appendChild(svg("line", { "class": "base", x1: x0, x2: x1, y1: base + .5, y2: base + .5 }));
    s.appendChild(svg("path", { "class": "rain", d: rainLine }));

    // Temperature: red line on top (°F scale).
    var tempLine = "";
    rows.forEach(function (r, i) { tempLine += (i ? "L" : "M") + xs[i].toFixed(1) + " " + ty(r.temp_f).toFixed(1); });
    s.appendChild(svg("path", { "class": "temp", d: tempLine }));

    // Direct labels, sparingly: start, end, and one interior extreme. Each
    // sits inside the plot (clear of both axes' labels) on the side of the
    // point the line isn't heading toward.
    var last = rows.length - 1;
    var placed = [];   // label boxes, for the rain-peak collision check
    function put(x, y, anchor, text) {
      var w = text.length * 7 + 2;
      var bx = anchor === "start" ? x : anchor === "end" ? x - w : x - w / 2;
      placed.push({ x: bx, y: y - 11, w: w, h: 13 });
      s.appendChild(svg("text", { "class": "lbl", x: x, y: y, "text-anchor": anchor }, text));
    }
    function side(i, j) {   // "above" unless the neighbour j is higher, or no room
      var y = ty(temps[i]), yn = ty(temps[j]);
      var up = !(yn < y - 3);
      if (up && y - 9 < top + 2) up = false;
      if (!up && y + 18 > base - 2) up = true;
      return up ? y - 9 : y + 18;
    }
    var labels = [{ i: 0, pos: "start" }, { i: last, pos: "end" }];
    var iMax = temps.indexOf(tMax), iMin = temps.indexOf(tMin);
    var ends = [Math.round(temps[0]), Math.round(temps[last])];
    if (Math.round(tMax) > Math.max.apply(null, ends) && iMax > 0 && iMax < last) labels.push({ i: iMax, pos: "above" });
    else if (Math.round(tMin) < Math.min.apply(null, ends) && iMin > 0 && iMin < last) labels.push({ i: iMin, pos: "below" });
    labels.forEach(function (L) {
      var x = xs[L.i], y = ty(temps[L.i]), txt = deg(temps[L.i]);
      if (L.pos === "above" || L.pos === "below") {
        if (x - xs[0] < 40 || xs[last] - x < 40) return;  // would collide with an end label
      }
      s.appendChild(svg("circle", { "class": "dot-temp", cx: x, cy: y, r: 4 }));
      if (L.pos === "start") put(x + 2, side(0, Math.min(1, last)), "start", txt);
      else if (L.pos === "end") put(x - 2, side(last, Math.max(0, last - 1)), "end", txt);
      else if (L.pos === "above") put(x, y - 9 < top + 2 ? y + 18 : y - 9, "middle", txt);
      else put(x, y + 18 > base - 2 ? y - 9 : y + 18, "middle", txt);
    });

    // Rain peak label when there's a meaningful chance and room for it.
    var rain = rows.map(function (r) { return r.precip_prob; });
    var rMax = Math.max.apply(null, rain);
    if (rMax >= 10) {
      var ri = rain.indexOf(rMax), rx = xs[ri], ry = py(rMax);
      var anchor = rx < x0 + 20 ? "start" : (rx > x1 - 20 ? "end" : "middle");
      var txt = rMax + "%", tw = txt.length * 7 + 2;
      var ly = ry - 8 < top + 2 ? ry + 17 : ry - 8;
      var bx = anchor === "start" ? rx : anchor === "end" ? rx - tw : rx - tw / 2;
      var hit = placed.some(function (b) {
        return bx < b.x + b.w && bx + tw > b.x && ly - 11 < b.y + b.h && ly + 2 > b.y;
      });
      // ...and clear of the temperature line itself.
      for (var lx = bx - 3; lx <= bx + tw + 3 && !hit; lx += 2) {
        var j = 1;
        while (j < last && xs[j] < lx) j++;
        var f = Math.max(0, Math.min(1, (lx - xs[j - 1]) / ((xs[j] - xs[j - 1]) || 1)));
        var ly2 = ty(temps[j - 1] + (temps[j] - temps[j - 1]) * f);
        hit = ly2 > ly - 16 && ly2 < ly + 7;
      }
      if (!hit) {
        s.appendChild(svg("circle", { "class": "dot-rain", cx: rx, cy: ry, r: 4 }));
        s.appendChild(svg("text", { "class": "lbl lbl-rain", x: rx, y: ly, "text-anchor": anchor }, txt));
      }
    }

    // Hour labels along the bottom.
    var everyH = plotW / 6 >= 46 ? 1 : 2;
    rows.forEach(function (r, i) {
      var m = /T(\d{2}):00/.exec(r.time);
      if (!m) return;
      var x = xs[i];
      s.appendChild(svg("line", { "class": "base", x1: Math.round(x) + .5, x2: Math.round(x) + .5, y1: base, y2: base + 4 }));
      if (+m[1] % everyH !== 0 || x < x0 + 14 || x > x1 - 14) return;
      s.appendChild(svg("text", { "class": "axis", x: x, y: axisY, "text-anchor": "middle" }, clock(r.time)));
    });

    // Crosshair layer.
    var cross = svg("g", { visibility: "hidden" });
    var vline = svg("line", { "class": "cross", y1: top, y2: base });
    var cdT = svg("circle", { "class": "dot-temp", r: 4 });
    var cdR = svg("circle", { "class": "dot-rain", r: 4 });
    cross.appendChild(vline); cross.appendChild(cdT); cross.appendChild(cdR);
    s.appendChild(cross);
    host.appendChild(s);

    var tip = el("div", "wx-tip");
    tip.hidden = true;
    tip.setAttribute("aria-hidden", "true");
    host.appendChild(tip);

    function tipRow(kind, value, label) {
      var row = el("div", "row");
      row.appendChild(el("span", "wx-key " + kind));
      row.appendChild(el("b", null, value));
      row.appendChild(el("span", null, label));
      return row;
    }

    var cur = -1;
    function show(i) {
      cur = i;
      var r = rows[i], x = xs[i];
      vline.setAttribute("x1", Math.round(x) + .5); vline.setAttribute("x2", Math.round(x) + .5);
      cdT.setAttribute("cx", x); cdT.setAttribute("cy", ty(r.temp_f));
      cdR.setAttribute("cx", x); cdR.setAttribute("cy", py(r.precip_prob));
      cross.setAttribute("visibility", "visible");
      tip.textContent = "";
      tip.appendChild(el("div", "t", clock(r.time, true)));
      tip.appendChild(tipRow("temp", deg(r.temp_f), "Temp"));
      tip.appendChild(tipRow("rain", r.precip_prob + "%", "Rain"));
      tip.appendChild(tipRow("cloud", r.cloud_cover + "%", "Clouds"));
      tip.hidden = false;
      var tw = tip.offsetWidth;
      var left = x + 12;
      if (left + tw > W) left = x - 12 - tw;
      tip.style.left = Math.max(0, left) + "px";
    }
    function hide() { cur = -1; cross.setAttribute("visibility", "hidden"); tip.hidden = true; }
    function nearest(clientX) {
      var rect = s.getBoundingClientRect();
      var x = (clientX - rect.left) * (W / rect.width);
      var best = 0;
      for (var i = 1; i < xs.length; i++) if (Math.abs(xs[i] - x) < Math.abs(xs[best] - x)) best = i;
      return best;
    }

    host.onpointermove = function (e) { show(nearest(e.clientX)); };
    host.onpointerdown = function (e) { show(nearest(e.clientX)); };
    host.onpointerleave = hide;
    host.tabIndex = 0;
    host.setAttribute("role", "group");
    host.setAttribute("aria-label", "Forecast chart. Use left and right arrow keys to read values.");
    host.onkeydown = function (e) {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        e.preventDefault();
        var step = e.key === "ArrowRight" ? 1 : -1;
        show(Math.max(0, Math.min(rows.length - 1, (cur < 0 ? 0 : cur + step))));
      } else if (e.key === "Escape") hide();
    };
    host.onblur = hide;
  }

  // ------------------------------------------------------------------ data

  function usable(d) {
    return d && d.available && d.current && num(d.current.temp_f) != null &&
      Array.isArray(d.hourly) && d.hourly.length >= 2 &&
      d.hourly.every(function (r) {
        return r && typeof r.time === "string" && num(r.temp_f) != null &&
          num(r.precip_prob) != null && num(r.cloud_cover) != null;
      });
  }

  function load() {
    lastFetch = Date.now();
    fetch("/api/weather", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (usable(d)) {
          data = d;
          root.hidden = false;
          render();
        } else if (d && d.available === false) {
          data = null;
          root.hidden = true;
        }
        // Any other failure: keep whatever is showing (or stay hidden).
      })
      .catch(function () { /* network blip: keep the last render */ });
  }

  function schedule() {
    clearInterval(timer);
    timer = setInterval(load, REFRESH_MS);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible" && Date.now() - lastFetch > 60 * 1000) {
      load();
      schedule();
    }
  });

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      var chart = root.querySelector(".wx-chart");
      if (data && chart && Math.abs(chart.clientWidth - lastWidth) > 2) drawChart(chart, data.hourly);
    }, 150);
  });

  load();
  schedule();
})();
