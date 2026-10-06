/* ===================================================================
 * Lightbucket Astro Planner — Wide-field Sky Map (Phase 1 harness)
 *
 * Standalone d3-celestial explorer, Option B layout. Driven entirely by
 * URL query params so it can be opened in a browser for testing and,
 * later, launched by the app via the same param contract.
 *
 *   ?ra=<deg>&dec=<deg>&name=<str>&common=<str>&catalog=<str>
 *   &fovw=<deg>&fovh=<deg>&pa=<deg>&theme=<day|night>
 *
 * Defaults to Sh2-155 (Cave Nebula) so the page is useful with no params.
 *
 * NOTE on coordinates: d3-celestial's programmatic API uses INTERNAL
 * longitude degrees for equatorial coords — RA in degrees mapped to the
 * range [-180, 180] (hours are only used by its built-in form field,
 * which we don't use). All center / projection / overlay coordinates
 * below follow that convention via raToLon().
 *
 * FRAME COMPOSER (section 6): the rig's FOV frame can be dragged, rotated
 * and snapped to objects, then sent to the app's Tonight's Plan through the
 * local server's /api/tonight (see _SkymapAPI in AstroPlanner.py).
 *
 * LABELS: d3-celestial's own text drawing is switched off entirely. Every
 * label (target, RA/Dec markers, constellation, deep-sky and star names)
 * is drawn by the label engine in section 7b onto a transparent overlay
 * canvas, through ONE shared collision pass — so labels never overlap at
 * any zoom — plus the callout lens for reading crowded regions.
 * =================================================================== */

(function () {
  "use strict";

  /* ---------- 1. Read launch params ------------------------------ */
  var q = new URLSearchParams(window.location.search);
  function num(k, d) { var v = parseFloat(q.get(k)); return isFinite(v) ? v : d; }
  function str(k, d) { var v = q.get(k); return (v === null || v === "") ? d : v; }

  var TARGET = {
    ra:      num("ra", 344.475),     // Sh2-155 ≈ 22h57m54s
    dec:     num("dec", 62.51),      //           +62°31′
    name:    str("name", "Sh2-155"),
    common:  str("common", "Cave Nebula"),
    catalog: str("catalog", "Sharpless"),
    fovw:    num("fovw", 1.4),       // sensor FOV width  (deg)
    fovh:    num("fovh", 0.9),       // sensor FOV height (deg)
    pa:      num("pa", 11),          // position angle    (deg, NINA/IAU: N→E)
    rig:     str("rig", ""),         // active rig label: saved rig name, else "scope · camera"
    rigColor: str("rigcolor", ""),   // rig accent colour (same as its plan cards / timeline bars)
    apiKey:  str("key", "")          // per-launch token for the app's /api (empty = standalone)
  };
  var themeName = str("theme", "day");

  // Remembered map settings (Layers panel, deep-sky groups, label HUD, mag
  // slider, Hide planned).  The app stores them in its own data file and
  // passes them back on every open — the map window runs in private mode,
  // so the browser's own storage is wiped each time it closes.
  var PREFS = {};
  try { PREFS = JSON.parse(q.get("prefs") || "{}") || {}; } catch (e) { PREFS = {}; }
  var prefsReady = false, prefsTimer = 0;
  function savePrefs() {
    if (!prefsReady || !TARGET.apiKey) return;
    clearTimeout(prefsTimer);
    prefsTimer = setTimeout(function () {
      var out = { layers: {}, dso: {}, labels: Labels.getPrefs(),
                  mag: parseFloat(magSlider.value), hidePlanned: Plan.isHidden() };
      document.querySelectorAll(".row[data-layer]").forEach(function (r) {
        out.layers[r.getAttribute("data-layer")] = r.classList.contains("on");
      });
      for (var g in DSO_ON) out.dso[g] = !!DSO_ON[g];
      try {
        fetch("/api/prefs", { method: "POST",
          headers: { "Content-Type": "application/json", "X-LB-Key": TARGET.apiKey },
          body: JSON.stringify(out) }).catch(function () {});
      } catch (e) {}
    }, 400);
  }

  /* ---------- 1a. Diagnostics + resilient catalog loading --------- */
  // One line into the app's skymap.log (the packaged Windows app has no
  // console).  Fire-and-forget; silent when standalone.
  function diag(msg) {
    try { console.warn("[skymap] " + msg); } catch (e) {}
    if (!TARGET.apiKey) return;
    try {
      fetch("/api/log", { method: "POST",
        headers: { "Content-Type": "application/json", "X-LB-Key": TARGET.apiKey },
        body: JSON.stringify({ msg: String(msg).slice(0, 400) }) }).catch(function () {});
    } catch (e) {}
  }
  window.addEventListener("error", function (e) {
    diag("JS error: " + (e.message || e) + (e.filename ? " @ " + e.filename.split("/").pop() + ":" + e.lineno : ""));
  });
  // Every catalog file is fetched with d3.json — by the engine (stars, DSOs,
  // constellations, Milky Way…) and by the label engine.  The engine just
  // drops a layer whose request fails, so one refused connection meant a
  // map with no deep-sky objects (or no stars) until the next open.
  // Retry a failed request a few times (connection-level failures and
  // server errors; a 404 is final), and log what happened.
  if (window.d3 && d3.json) {
    var d3json = d3.json;
    d3.json = function (url, cb) {
      if (typeof cb !== "function") return d3json.apply(this, arguments);
      var tries = 0;
      (function go() {
        d3json(url, function (err, data) {
          var st = err && err.status;
          if (err && st !== 404 && tries < 5) { tries++; setTimeout(go, 200 * tries); return; }
          if (err) { if (st !== 404) diag("catalog file failed: " + url + " (status " + st + ", " + tries + " retries)"); }
          else if (tries) diag("catalog file " + url + " loaded after " + tries + " retr" + (tries > 1 ? "ies" : "y"));
          cb(err, data);
        });
      })();
    };
  }

  // Catalog tier (Lean bundled by default; Extended/Full come from the
  // app's downloaded catalog, served over the same localhost root).
  var CATALOG = {
    stars:    str("stars", "stars.6.json"),
    dsos:     str("dsos", "messier.json"),
    maglimit: num("maglimit", 6)
  };
  // Initial on-screen magnitude — cap the first render so Full (mag 14)
  // doesn't paint every star at once; the slider still reaches maglimit.
  var INIT_MAG = Math.min(CATALOG.maglimit, 9);

  /* ---------- 2. Theme palettes ---------------------------------- */
  // Each palette carries both the page-chrome CSS vars and the colours
  // fed into the d3-celestial config so the sky itself recolours too.
  var THEMES = {
    day: {
      css: {
        "--bg": "#101826", "--panel": "#16243a", "--sky": "#070b13",
        "--border": "#2e4a63", "--text": "#e8eef5", "--dim": "#6b7e92",
        "--faint": "#3f5163", "--accent": "#7eb8d4", "--accent-bright": "#aed0ed",
        "--accent-fill": "#234055", "--glass": "rgba(13,24,38,0.92)",
        "--switch-off": "#27384a", "--switch-on": "#2b5573",
        "--badge-bg": "#8c4528", "--badge-fg": "#efb6a0"
      },
      sky: "#070b13", star: "#ffffff",
      conLine: "#86abd0", conName: "#a6c0da", bounds: "#86c49a",
      mw: "#9fb8d0", grid: "#92aec8", dsoName: "#93b3c8", starName: "#d9d2c0",
      reticle: "#7eb8d4", frame: "#aed0ed",
      // label engine / callout lens
      target: "#f2c14e", targetBg: "rgba(242,193,78,0.14)",
      lens: "#7eb8d4", leader: "rgba(126,184,212,0.45)",
      panel: "rgba(13,24,38,0.94)", panelEdge: "#2e4a63", detail: "#6b7e92",
      planned: "#6fd3a0"
    },
    night: {
      css: {
        "--bg": "#120000", "--panel": "#1a0000", "--sky": "#0a0000",
        "--border": "#3a0000", "--text": "#d98a8a", "--dim": "#aa4444",
        "--faint": "#5a1a1a", "--accent": "#cc4444", "--accent-bright": "#e06666",
        "--accent-fill": "#2a0000", "--glass": "rgba(20,0,0,0.92)",
        "--switch-off": "#3a1010", "--switch-on": "#6a1a1a",
        "--badge-bg": "#5a1a00", "--badge-fg": "#e0a070"
      },
      sky: "#0a0000", star: "#e0a0a0",
      conLine: "#b85d5d", conName: "#c87070", bounds: "#d69a5a",
      mw: "#aa6666", grid: "#b06868", dsoName: "#c07878", starName: "#d29a72",
      reticle: "#cc5555", frame: "#e06666",
      target: "#e0a070", targetBg: "rgba(224,160,112,0.14)",
      lens: "#e06666", leader: "rgba(224,102,102,0.65)",
      panel: "rgba(20,0,0,0.94)", panelEdge: "#3a0000", detail: "#aa4444",
      planned: "#d6a05a"
    }
  };
  var T = THEMES[themeName] || THEMES.day;

  // Rig accent colours come from the app (one per scope + camera, matching
  // Tonight's Plan cards and timeline bars).  Night theme keeps the hue but
  // pulls it well down toward the red-black sky so it can't blow dark
  // adaptation.  Falls back to the theme colour when a rig has none.
  function rigInk(hex, fallback) {
    var m = /^#?([0-9a-f]{6})$/i.exec(String(hex || ""));
    if (!m) return fallback || T.frame;
    if (themeName !== "night") return "#" + m[1];
    var n = parseInt(m[1], 16), r = n >> 16, g = (n >> 8) & 255, b = n & 255;
    var k = 0.55, mix = function (c, t) { return Math.round(c * k + t * (1 - k)); };
    return "rgb(" + mix(r, 90) + "," + mix(g, 10) + "," + mix(b, 10) + ")";
  }
  function rgba(css, a) {               // "#rrggbb" | "rgb(r,g,b)" → rgba()
    var m = /^#([0-9a-f]{6})$/i.exec(css), r, g, b;
    if (m) { var n = parseInt(m[1], 16); r = n >> 16; g = (n >> 8) & 255; b = n & 255; }
    else { m = /(\d+),\s*(\d+),\s*(\d+)/.exec(css); if (!m) return css; r = +m[1]; g = +m[2]; b = +m[3]; }
    return "rgba(" + r + "," + g + "," + b + "," + a + ")";
  }

  /* ---------- 2b. Deep-sky type groups -------------------------- *
   * d3-celestial type codes → the five Layers-panel groups.  Hiding a
   * group hides its symbols (engine, via per-type symbol opacity) and its
   * labels / lens rows (label engine).  The target is never hidden.   */
  var DSO_GROUP = {
    g: "gal", s: "gal", s0: "gal", sd: "gal", e: "gal", i: "gal", gg: "gal",
    en: "neb", bn: "neb", sfr: "neb", rn: "neb",
    dn: "dark",
    oc: "clu", gc: "clu", pos: "clu", ast: "clu", ds: "clu",
    pn: "rem", snr: "rem"
  };
  function dsoGroup(type) { return DSO_GROUP[type] || "neb"; }
  var DSO_ON = { gal: true, neb: true, dark: true, clu: true, rem: true };

  function applyChromeTheme() {
    var root = document.documentElement;
    for (var k in T.css) root.style.setProperty(k, T.css[k]);
  }

  /* ---------- 3. Coordinate helpers ------------------------------ */
  var D2R = Math.PI / 180;
  function raToLon(ra) { ra = ((ra % 360) + 360) % 360; return ra > 180 ? ra - 360 : ra; }
  var LON = raToLon(TARGET.ra);

  function fmtRA(raDeg) {
    var h = (((raDeg % 360) + 360) % 360) / 15;
    var hh = Math.floor(h), mm = Math.floor((h - hh) * 60),
        ss = Math.round((((h - hh) * 60) - mm) * 60);
    if (ss === 60) { ss = 0; mm++; } if (mm === 60) { mm = 0; hh = (hh + 1) % 24; }
    return "RA " + pad(hh) + ":" + pad(mm) + ":" + pad(ss);
  }
  function fmtDec(decDeg) {
    var sign = decDeg < 0 ? "-" : "+", a = Math.abs(decDeg);
    var dd = Math.floor(a), mm = Math.round((a - dd) * 60);
    if (mm === 60) { mm = 0; dd++; }
    return "Dec " + sign + pad(dd) + "°" + pad(mm) + "′";
  }
  function pad(n) { return (n < 10 ? "0" : "") + n; }

  /* ---------- 4. FOV-frame geometry ----------------------------- *
   * Tangent-plane offset (xe east, yn north, degrees) from a centre,
   * rotated by position angle (NINA/IAU convention: measured from North
   * through East), projected back to internal [lon, lat].  Accurate enough
   * for camera-sized frames.  frameRing() subdivides each edge so large
   * (mosaic-sized) frames still follow the projection's curvature.   */
  function frameOffset(ra, dec, pa, xe, yn) {
    var pr = pa * D2R, cd = Math.max(Math.cos(dec * D2R), 1e-3);
    var xr =  xe * Math.cos(pr) + yn * Math.sin(pr);
    var yr = -xe * Math.sin(pr) + yn * Math.cos(pr);
    return [raToLon(ra + xr / cd), dec + yr];
  }
  function frameRing(ra, dec, pa, fovw, fovh) {
    var hw = fovw / 2, hh = fovh / 2, N = 8, out = [];
    var c = [[-hw, hh], [hw, hh], [hw, -hh], [-hw, -hh]];
    for (var i = 0; i < 4; i++) {
      var a = c[i], b = c[(i + 1) % 4];
      for (var k = 0; k < N; k++) {
        var t = k / N;
        out.push(frameOffset(ra, dec, pa, a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t));
      }
    }
    return out;
  }

  /* ---------- 5. d3-celestial configuration ---------------------- */
  // All engine text is OFF (stars/dsos/constellation names) — the label
  // engine (7b) draws them instead, with collision avoidance.
  var config = {
    width: 0,
    projection: "stereographic",
    transform: "equatorial",
    center: [LON, TARGET.dec, 0],
    follow: "center",
    orientationfixed: true,
    background: { fill: T.sky, stroke: T.sky, opacity: 1 },
    adaptable: true,
    interactive: true,
    controls: false,      // we supply our own zoom UI
    form: false,          // we supply our own layer UI
    container: "celestial-map",
    datapath: "data/",
    stars: {
      show: true, limit: INIT_MAG, colors: false, style: { fill: T.star, opacity: 1 },
      names: false, proper: false, desig: false,
      size: 6, exponent: -0.28, data: CATALOG.stars
    },
    dsos: {
      show: true, limit: Math.max(CATALOG.maglimit, 10), names: false, desig: false,
      size: null, exponent: 1.4, data: CATALOG.dsos
    },
    constellations: {
      show: true, names: false, desig: true,
      lines: true, linestyle: { stroke: T.conLine, width: 1, opacity: 0.7 },
      bounds: true, boundstyle: { stroke: T.bounds, width: 0.6, opacity: 0, dash: [2, 4] }
    },
    mw: { show: true, style: { fill: T.mw, opacity: 0.07 } },
    planets: { show: false },
    lines: {
      graticule: { show: false, stroke: T.grid, width: 0.6, opacity: 0.7,
        lon: { pos: [""] }, lat: { pos: [""] } },
      equatorial: { show: false }, ecliptic: { show: false },
      galactic: { show: false }, supergalactic: { show: false }
    }
  };

  /* ---------- 6c. Magnitude-less nebulae ------------------------ *
   * The catalog files store "no magnitude" as the STRING "999", but the
   * engine tests mag === 999 (a number), so it silently skips every
   * magnitude-less object — ~900 of the 950 bright nebulae in dsos.6
   * (all LBN / Sharpless / RCW / Cederblad).  And even a numeric 999 is only
   * drawn when sqrt(size′) exceeds the magnitude limit, i.e. only huge
   * clouds.  This pass draws those objects with the engine's own symbol
   * shapes, colours and size formula, honouring the deep-sky type toggles.
   * (Registered before the label engine reads the sky, after engine DSOs.) */
  function engineSkips(p, limit) {
    if (p.mag === 999) return !(Math.sqrt(parseInt(p.dim, 10)) > limit);   // numeric: engine's own rule
    return p.mag === "999" || p.mag === "" || p.mag == null;              // string / missing
  }
  Celestial.add({
    type: "line",
    callback: function () { Celestial.redraw(); },
    redraw: function () {
      var ctx = Celestial.context;
      ctx.save();
      try {
        var limit = config.dsos.limit;
        var adapt = Math.sqrt(Celestial.zoomBy() || 1);
        var base = config.dsos.size || config.stars.size || 7;
        var syms = dsoSymbols();
        Celestial.container.selectAll(".dso").each(function (d) {
          var p = d.properties;
          if (!engineSkips(p, limit)) return;
          if (!DSO_ON[dsoGroup(p.type)]) return;
          var c = d.geometry.coordinates;
          if (!Celestial.clip(c)) return;
          var pt = Celestial.mapProjection(c);
          if (!pt) return;
          var sym = syms[p.type] || syms.bn;
          var dim = parseInt(p.dim, 10) || 10;
          Celestial.setStyle(sym);
          Celestial.Canvas.symbol().type(sym.shape || "square")
            .size(Math.pow(dim * base * adapt / 7, 0.5)).position(pt)(ctx);
          if (sym.stroke) ctx.stroke(); else ctx.fill();
        });
      } catch (e) { console.warn("nebula pass skipped:", e); }
      ctx.restore();
      ctx.globalAlpha = 1;
    }
  });

  /* ---------- 6. Frame composer ---------------------------------- *
   * The rig's FOV frame is live on the map: drag inside it to move it,
   * drag the knob above its top edge to rotate it (or type a PA).  The
   * drag is free — no snapping — and the frame's exact centre is what's
   * sent.  The frame stays "on" the object it was placed on (the launched
   * target, or via a card's "Frame here") while that object is still
   * inside it, so a nudge is an offset framing of that object; once the
   * object leaves the frame it becomes a custom field ("Field J2058+4420").
   * Right-click "Frame this spot" always starts a field.  "Send framing"
   * posts centre / PA / name to the app, which adds it to Tonight's Plan
   * exactly like the Frame dialog's Confirm (same rig, same duplicate
   * handling — re-sending an already-planned target updates its framing).
   *
   * Drawn on the label overlay (cheap to repaint while dragging); its
   * mouse handlers are registered BEFORE the label engine's, so a grab
   * on the frame never pans the map or pins the lens.                 */
  var Composer = (function () {
    var wrap = document.getElementById("map-wrap");
    var enabled = TARGET.fovw > 0 && TARGET.fovh > 0;

    function norm(a) { a = a % 360; return a < 0 ? a + 360 : a; }
    function lonToRa(l) { return l < 0 ? l + 360 : l; }

    // Frame state.  "snap" is the object the frame is framing (its centre
    // may be offset from it), with the map's position for that object:
    //   { kind: "target", id, ra, dec }                     — the launched target
    //   { kind: "dso", id, type, dim, common, ra, dec }     — a sky-map object
    //   null                                                 — a custom field
    function targetSnap() { return { kind: "target", id: TARGET.name, ra: TARGET.ra, dec: TARGET.dec }; }
    var st = {
      ra: TARGET.ra, dec: TARGET.dec, pa: norm(TARGET.pa),
      snap: targetSnap(),
      name: TARGET.name, nameEdited: false
    };
    var drag = null, sending = false;

    function fieldName(ra, dec) {
      var h = ra / 15, hh = Math.floor(h), mm = Math.floor((h - hh) * 60);
      var sg = dec < 0 ? "-" : "+", a = Math.abs(dec), dd = Math.floor(a), dm = Math.floor((a - dd) * 60);
      return "Field J" + pad(hh) + pad(mm) + sg + pad(dd) + pad(dm);
    }

    function project(c) {
      if (!Celestial.mapProjection || !Celestial.clip(c)) return null;
      return Celestial.mapProjection(c);
    }

    // Screen geometry of the frame: polygon, centre, top-edge midpoint, knob.
    function geom() {
      if (!enabled) return null;
      var ctr = project([raToLon(st.ra), st.dec]);
      if (!ctr) return null;
      var poly = [], ring = frameRing(st.ra, st.dec, st.pa, TARGET.fovw, TARGET.fovh);
      for (var i = 0; i < ring.length; i++) {
        var p = project(ring[i]);
        if (!p) return null;
        poly.push(p);
      }
      var top = project(frameOffset(st.ra, st.dec, st.pa, 0, TARGET.fovh / 2)) || ctr;
      var dx = top[0] - ctr[0], dy = top[1] - ctr[1], L = Math.hypot(dx, dy) || 1;
      var reach = Math.max(L + 16, 24);                      // knob stays grabbable when tiny
      return { poly: poly, ctr: ctr, top: top,
               knob: [ctr[0] + dx / L * reach, ctr[1] + dy / L * reach],
               size: Math.min(polyW(poly), polyH(poly)) };
    }
    function polyW(p) { var a = p.map(function (q) { return q[0]; }); return Math.max.apply(null, a) - Math.min.apply(null, a); }
    function polyH(p) { var a = p.map(function (q) { return q[1]; }); return Math.max.apply(null, a) - Math.min.apply(null, a); }
    function inPoly(pt, poly) {
      var inside = false;
      for (var i = 0, j = poly.length - 1; i < poly.length; j = i++) {
        var a = poly[i], b = poly[j];
        if (((a[1] > pt[1]) !== (b[1] > pt[1])) &&
            (pt[0] < (b[0] - a[0]) * (pt[1] - a[1]) / (b[1] - a[1]) + a[0])) inside = !inside;
      }
      return inside;
    }
    function hit(pt) {
      var g = geom(); if (!g) return null;
      if (Math.hypot(pt[0] - g.knob[0], pt[1] - g.knob[1]) < 10) return "rot";
      if (inPoly(pt, g.poly) || Math.hypot(pt[0] - g.ctr[0], pt[1] - g.ctr[1]) < 12) return "move";
      return null;
    }

    // Position angle (N→E) of a screen point around the frame centre, from
    // the projected North and East directions there — independent of the
    // projection's handedness, so it matches frameOffset()'s convention.
    function paAt(pt) {
      var c = [raToLon(st.ra), st.dec], eps = 0.05;
      var p0 = project(c);
      var pn = project([c[0], Math.min(89.9, st.dec + eps)]);
      var pe = project([raToLon(st.ra + eps / Math.max(Math.cos(st.dec * D2R), 1e-3)), st.dec]);
      if (!p0 || !pn || !pe) return st.pa;
      var n = [pn[0] - p0[0], pn[1] - p0[1]], e = [pe[0] - p0[0], pe[1] - p0[1]];
      var ln = Math.hypot(n[0], n[1]) || 1, le = Math.hypot(e[0], e[1]) || 1;
      var v = [pt[0] - p0[0], pt[1] - p0[1]];
      var vn = (v[0] * n[0] + v[1] * n[1]) / ln, ve = (v[0] * e[0] + v[1] * e[1]) / le;
      return norm(Math.atan2(ve, vn) / D2R);
    }

    // Is an object still inside the frame?  (Keeps the frame "on" it.)
    function inFrame(obj) {
      var g = geom(); if (!g || !obj) return false;
      var p = project([raToLon(obj.ra), obj.dec]);
      return !!p && inPoly(p, g.poly);
    }
    // Frame centre's offset from the object it's on, in arcminutes.
    function offsetArcmin() {
      if (!st.snap) return 0;
      var dRa = (((st.ra - st.snap.ra) + 540) % 360 - 180) * Math.cos(st.dec * D2R);
      return Math.hypot(dRa, st.dec - st.snap.dec) * 60;
    }

    /* --- drawing (called by the label engine's paint, under labels) --- */
    var tagBox = null;                  // screen box of the rig tag (a no-label zone)
    function frameInk() { return rigInk(TARGET.rigColor, T.frame); }
    function draw(ctx) {
      tagBox = null;
      var g = geom(); if (!g) return;
      var ink = frameInk();
      ctx.save();
      ctx.globalAlpha = 1;
      // Frame — in the rig's own colour, so it reads as "this rig"
      var flash = performance.now() < flashUntil;
      ctx.strokeStyle = ink; ctx.lineWidth = drag ? 2 : flash ? 2.8 : 1.7;
      ctx.setLineDash(drag || flash ? [] : [6, 4]);
      ctx.beginPath();
      g.poly.forEach(function (p, i) { if (i) ctx.lineTo(p[0], p[1]); else ctx.moveTo(p[0], p[1]); });
      ctx.closePath(); ctx.stroke();
      if (drag) { ctx.fillStyle = rgba(ink, 0.07); ctx.fill(); }
      ctx.setLineDash([]);
      drawTag(ctx, g, ink);
      // Rotation stalk + knob
      ctx.strokeStyle = ink; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(g.top[0], g.top[1]); ctx.lineTo(g.knob[0], g.knob[1]); ctx.stroke();
      ctx.fillStyle = drag && drag.mode === "rot" ? ink : T.sky;
      ctx.beginPath(); ctx.arc(g.knob[0], g.knob[1], 5, 0, 2 * Math.PI); ctx.fill(); ctx.stroke();
      // Centre tick (only once the frame has left the target's reticle)
      if (!st.snap || st.snap.kind !== "target") {
        ctx.beginPath();
        ctx.moveTo(g.ctr[0] - 5, g.ctr[1]); ctx.lineTo(g.ctr[0] + 5, g.ctr[1]);
        ctx.moveTo(g.ctr[0], g.ctr[1] - 5); ctx.lineTo(g.ctr[0], g.ctr[1] + 5);
        ctx.stroke();
      }
      // Offset framing: a faint tether from the centre to the object it's on
      if (st.snap && offsetArcmin() >= 0.5) {
        var op = project([raToLon(st.snap.ra), st.snap.dec]);
        if (op) {
          ctx.strokeStyle = ink; ctx.globalAlpha = 0.6; ctx.lineWidth = 1; ctx.setLineDash([2, 3]);
          ctx.beginPath(); ctx.moveTo(g.ctr[0], g.ctr[1]); ctx.lineTo(op[0], op[1]); ctx.stroke();
          ctx.setLineDash([]); ctx.beginPath(); ctx.arc(op[0], op[1], 4, 0, 2 * Math.PI); ctx.stroke();
          ctx.globalAlpha = 1;
        }
      }
      ctx.restore();
    }

    // Rig tag: a small pill on the frame's upper-left corner naming the rig,
    // so there's never doubt which rig a frame on the map belongs to.
    function drawTag(ctx, g, ink) {
      if (!TARGET.rig || g.size < 26) return;
      var font = "bold 10px Helvetica, Arial, sans-serif";
      var text = "▭ " + TARGET.rig;
      ctx.font = font;
      if (ctx.measureText(text).width > 220) {
        while (text.length > 4 && ctx.measureText(text + "…").width > 220) text = text.slice(0, -1);
        text += "…";
      }
      var w = ctx.measureText(text).width + 12, h = 15;
      var xs = g.poly.map(function (p) { return p[0]; }), ys = g.poly.map(function (p) { return p[1]; });
      var x = Math.min.apply(null, xs), y = Math.min.apply(null, ys) - h - 4;
      var W = wrap.clientWidth;
      x = Math.max(4, Math.min(x, W - w - 4)); y = Math.max(4, y);
      ctx.fillStyle = T.panel; ctx.strokeStyle = ink; ctx.lineWidth = 1;
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(x, y, w, h, 7); else ctx.rect(x, y, w, h);
      ctx.fill(); ctx.stroke();
      ctx.fillStyle = ink; ctx.textBaseline = "middle"; ctx.textAlign = "left";
      ctx.fillText(text, x + 6, y + h / 2 + 0.5);
      tagBox = [x - 2, y - 2, w + 4, h + 4];
    }

    /* --- mouse --- */
    function local(e) {
      var r = wrap.getBoundingClientRect();
      return [e.clientX - r.left, e.clientY - r.top];
    }
    wrap.addEventListener("mousedown", function (e) {
      if (e.button !== 0 || !(e.target && e.target.tagName === "CANVAS")) return;
      var pt = local(e), h = hit(pt);
      if (!h) return;
      var g = geom();
      drag = { mode: h, off: [g.ctr[0] - pt[0], g.ctr[1] - pt[1]], start: pt, moved: false,
               anchor: st.snap };             // what the frame was on when the drag began
      e.stopImmediatePropagation(); e.preventDefault();       // no pan, no lens pin
      Labels.repaint();
    }, true);
    window.addEventListener("mousemove", function (e) {
      if (!drag) {
        if (e.target && e.target.tagName === "CANVAS") {
          var lp = local(e), h = hit(lp);
          wrap.style.cursor = h === "rot" ? "grab" : h === "move" ? "move"
                            : (Labels.objectAt({ x: lp[0], y: lp[1] }) ? "pointer" : "");
        }
        return;
      }
      var pt = local(e);
      if (!drag.moved) {
        if (Math.hypot(pt[0] - drag.start[0], pt[1] - drag.start[1]) < 3) return;
        drag.moved = true;
      }
      if (drag.mode === "move") {
        var sky = Celestial.mapProjection.invert([pt[0] + drag.off[0], pt[1] + drag.off[1]]);
        if (!sky || !isFinite(sky[0]) || !isFinite(sky[1])) return;
        st.ra = lonToRa(sky[0]); st.dec = Math.max(-89.9, Math.min(89.9, sky[1]));
        // No snapping: the frame goes exactly where it's dragged.  It stays
        // on its object while that object is still inside it.
        st.snap = drag.anchor && inFrame(drag.anchor) ? drag.anchor : null;
      } else {
        var pa = paAt(pt);
        st.pa = e.shiftKey ? norm(Math.round(pa / 15) * 15) : norm(Math.round(pa));
      }
      wrap.style.cursor = drag.mode === "rot" ? "grabbing" : "move";
      syncBar(); Labels.repaint();
    });
    window.addEventListener("mouseup", function () {
      if (!drag) return;
      if (!drag.moved) {                 // a plain click on the frame: treat
        var at = drag.start;             // it as a click on whatever's under it
        drag = null; Labels.repaint();
        Labels.clickAt({ x: at[0], y: at[1] });
        return;
      }
      if (drag.mode === "move" && !st.nameEdited) {
        st.name = st.snap ? st.snap.id : fieldName(st.ra, st.dec);
      }
      drag = null;
      clearMsg(); syncBar(); Labels.repaint();
    });

    /* --- composer bar --- */
    var el = function (id) { return document.getElementById(id); };
    var bar = el("composer"), posEl = el("cmp-pos"), snapEl = el("cmp-snap"),
        nameEl = el("cmp-name"), paEl = el("cmp-pa"), sendEl = el("cmp-send"),
        resetEl = el("cmp-reset"), msgEl = el("cmp-msg");
    var rigEl = el("cmp-rig"), rigLblEl = el("cmp-rig-lbl"), rigMenu = el("rigmenu");
    function syncRigChip() {
      rigLblEl.textContent = TARGET.rig || "Active rig";
      var ink = frameInk();
      rigEl.style.color = ink; rigEl.style.borderColor = ink;
      bar.classList.toggle("disabled", !enabled);
      if (!enabled) snapEl.textContent = "No rig frame — pick a rig from the rig menu";
    }
    syncRigChip();
    if (!TARGET.apiKey) {
      sendEl.title = "Open the sky map from the app to send framings to Tonight's Plan";
    }

    function fmtPos() {
      var h = st.ra / 15, hh = Math.floor(h), mm = Math.floor((h - hh) * 60),
          ss = Math.round(((h - hh) * 60 - mm) * 60);
      if (ss === 60) { ss = 0; mm++; } if (mm === 60) { mm = 0; hh = (hh + 1) % 24; }
      var sg = st.dec < 0 ? "−" : "+", a = Math.abs(st.dec), dd = Math.floor(a), dm = Math.round((a - dd) * 60);
      if (dm === 60) { dm = 0; dd++; }
      return "RA " + pad(hh) + "h" + pad(mm) + "m" + pad(ss) + "s  Dec " + sg + pad(dd) + "°" + pad(dm) + "′";
    }
    function syncBar() {
      if (!enabled) return;
      posEl.textContent = fmtPos();
      if (document.activeElement !== paEl) paEl.value = Math.round(st.pa);
      if (document.activeElement !== nameEl) nameEl.value = st.name;
      var off = offsetArcmin();
      var offTxt = off >= 0.5 ? " · offset " + (off < 10 ? off.toFixed(1) : Math.round(off)) + "′" : "";
      if (st.snap && st.snap.kind === "target" && !offTxt) { snapEl.textContent = "⌁ on target"; snapEl.className = "cchip ok"; }
      else if (st.snap) { snapEl.textContent = "⌁ " + st.snap.id + offTxt; snapEl.className = "cchip ok"; }
      else { snapEl.textContent = "◌ custom field"; snapEl.className = "cchip warn"; }
      var moved = st.snap === null || st.snap.kind !== "target" || off >= 0.5 ||
                  Math.round(st.pa) !== Math.round(norm(TARGET.pa));
      resetEl.style.visibility = moved ? "visible" : "hidden";
    }

    nameEl.addEventListener("input", function () {
      st.name = nameEl.value; st.nameEdited = nameEl.value.trim() !== "";
      if (!st.nameEdited) st.name = st.snap ? st.snap.id : fieldName(st.ra, st.dec);
    });
    paEl.addEventListener("input", function () {
      var v = parseFloat(paEl.value);
      if (isFinite(v)) { st.pa = norm(v); Labels.repaint(); syncBar(); }
    });
    resetEl.addEventListener("click", function () {
      st.ra = TARGET.ra; st.dec = TARGET.dec; st.pa = norm(TARGET.pa);
      st.snap = targetSnap(); st.name = TARGET.name; st.nameEdited = false;
      clearMsg(); syncBar(); Labels.repaint();
    });
    // [ / ] nudge the PA (Shift = 10°) — handy for fine-tuning to a guide star.
    window.addEventListener("keydown", function (e) {
      if (!enabled || (e.target && /INPUT|TEXTAREA/.test(e.target.tagName))) return;
      if (e.key === "[" || e.key === "]" || e.key === "{" || e.key === "}") {
        var step = e.shiftKey ? 10 : 1;
        st.pa = norm(st.pa + ((e.key === "]" || e.key === "}") ? step : -step));
        syncBar(); Labels.repaint(); e.preventDefault();
      }
    });

    /* --- send to the app --- */
    function clearMsg() { msgEl.innerHTML = ""; msgEl.style.display = "none"; }
    function showMsg(html, cls) {
      msgEl.className = cls || ""; msgEl.innerHTML = html; msgEl.style.display = "flex";
    }
    function esc(t) { return String(t).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

    function send(force) {
      if (!enabled || sending) return;
      if (!TARGET.apiKey) {
        showMsg("Open the sky map from the app to send framings to Tonight's Plan.", "warn");
        return;
      }
      var name = (st.name || "").trim() || (st.snap ? st.snap.id : fieldName(st.ra, st.dec));
      var body = {
        name: name,
        ra: Math.round(st.ra * 1e6) / 1e6,
        dec: Math.round(st.dec * 1e6) / 1e6,
        pa: Math.round(norm(st.pa) * 10) / 10,
        // The object the frame is on (with the map's position for it — the
        // frame may be offset), when the name still refers to it; the app
        // resolves it against its own catalog first.
        object: (st.snap && name === st.snap.id) ? st.snap : null,
        force: !!force
      };
      sending = true; sendEl.classList.add("busy"); sendEl.textContent = "Sending…";
      postTonight(body).then(function (res) {
        if (res.ok) {
          Plan.refresh();
          // Show the name the app actually saved (it strips characters NINA
          // can't use in file paths, and never shadows a catalog ID).
          if (res.id && res.id !== st.name) { st.name = res.id; st.nameEdited = true; syncBar(); }
          showMsg("✓ " + esc(res.message || "Sent to Tonight's Plan"), "ok");
          toast(res.message || "Sent to Tonight's Plan");
        } else if (res.confirm) {
          showMsg(esc(res.message) + ' <span class="cbtn" id="cmp-force">Add anyway</span>' +
                  '<span class="cbtn ghost" id="cmp-cancel">Cancel</span>', "warn");
          el("cmp-force").onclick = function () { clearMsg(); send(true); };
          el("cmp-cancel").onclick = clearMsg;
        } else {
          showMsg(esc(res.error || "The app couldn't add this framing."), "warn");
        }
      }).catch(function () {
        showMsg("Couldn't reach the app — is it still open?", "warn");
      }).then(function () {
        sending = false; sendEl.classList.remove("busy"); sendEl.textContent = "Send framing → Tonight";
      });
    }
    sendEl.addEventListener("click", function () { send(false); });

    // Put the frame on an object (card "Frame here" / right-click), keeping
    // the current PA; flash it so the eye finds it.
    var flashUntil = 0;
    function place(ra, dec, snap) {
      if (!enabled) { toast("No rig frame — pick a rig in the app, then reopen the sky map"); return; }
      st.ra = ra; st.dec = dec; st.snap = snap;
      st.name = snap ? snap.id : fieldName(ra, dec); st.nameEdited = false;
      flashUntil = performance.now() + 900;
      clearMsg(); syncBar(); Labels.repaint();
      setTimeout(Labels.repaint, 950);
      toast("Frame on " + st.name + " — drag or rotate it, then Send framing");
    }
    function frameOn(obj) {
      if (!obj) return;
      place(obj.ra, obj.dec, obj.kind === "target" ? targetSnap()
        : { kind: "dso", id: obj.id, type: obj.type || "", dim: obj.dim || "", common: obj.common || "",
            ra: obj.ra, dec: obj.dec });
    }
    // Right-click "Frame this spot": always a free field, even over an
    // object (the menu has a separate "Frame <object>" item for that).
    function frameAtScreen(pt) {
      var sky = Celestial.mapProjection.invert(pt);
      if (!sky || !isFinite(sky[0]) || !isFinite(sky[1])) return;
      place(lonToRa(sky[0]), sky[1], null);
    }

    /* --- rig menu: switch the frame's rig, or save an unsaved setup --- */
    // Switching keeps the frame's centre, angle and name — only its size,
    // colour and tag change — and the app makes the same rig active on
    // Tonight's Plan (so a Send lands on the rig you see).
    function setRig(info) {
      if (!info) return;
      TARGET.fovw = +info.fovw || 0; TARGET.fovh = +info.fovh || 0;
      TARGET.rig = info.label || ""; TARGET.rigColor = info.color || "";
      enabled = TARGET.fovw > 0 && TARGET.fovh > 0;
      syncRigChip(); clearMsg(); syncBar();
      flashUntil = performance.now() + 700;
      Labels.repaint(); setTimeout(Labels.repaint, 750);
      Plan.refresh(); Card.planChanged();
    }
    var menuOpen = false, rigs = [], saving = false, saveErr = "";
    function openMenu() {
      if (!TARGET.apiKey) { toast("Open the sky map from the app to switch rigs"); return; }
      menuOpen = true; saving = false; saveErr = "";
      rigMenu.innerHTML = '<div class="rh">Frame rig</div><div class="empty">Loading rigs…</div>';
      rigMenu.style.display = "block";
      apiGet("/api/rigs").then(function (r) {
        if (!menuOpen) return;
        if (!r || !r.ok) { rigMenu.innerHTML = '<div class="empty">' + esc((r && r.error) || "Couldn't load rigs.") + "</div>"; return; }
        rigs = r.rigs || []; renderMenu();
      }).catch(function () {
        if (menuOpen) rigMenu.innerHTML = '<div class="empty">Couldn\'t reach the app — is it still open?</div>';
      });
    }
    function closeMenu() { menuOpen = false; rigMenu.style.display = "none"; }
    function gearOf(r) {
      return [r.scope, r.camera, r.reduction, r.filter].filter(function (x) { return x; }).join(" · ");
    }
    function renderMenu() {
      var h = '<div class="rh">Frame rig</div>', any = false;
      rigs.forEach(function (r, i) {
        var ink = rigInk(r.color, T.frame);
        if (r.saved && i > 0 && !rigs[i - 1].saved) h += '<div class="sep"></div>';
        h += '<div class="ri' + (r.current ? " cur" : "") + '" data-rig="' + esc(r.id) + '" title="' + esc(gearOf(r)) + '">' +
             '<span class="sw" style="border-color:' + ink + '"></span>' +
             '<span class="tx"><div class="nm">' + esc(r.label) + (r.saved ? "" : '<span class="trial">TRIAL</span>') + "</div>" +
             '<div class="gr">' + esc(gearOf(r)) + " · " + (+r.fovw).toFixed(2) + "°×" + (+r.fovh).toFixed(2) + "°</div></span>" +
             '<span class="ck">' + (r.current ? "✓" : "") + "</span></div>";
        if (!r.saved) {
          if (saving) {
            h += '<div class="svrow"><input id="rig-name" type="text" maxlength="60" spellcheck="false" value="' +
                 esc(r.scope + " · " + r.camera) + '"><span class="cbtn pri" data-act="dosave">Save</span>' +
                 '<span class="cbtn ghost" data-act="nosave">✕</span></div>';
            if (saveErr) h += '<div class="err">' + esc(saveErr) + "</div>";
          } else {
            h += '<span class="sv" data-act="save">☆ Save as rig…</span>';
          }
        }
        any = true;
      });
      if (!any) h += '<div class="empty">No saved rigs yet — save one in the app\'s Equipment tab.</div>';
      rigMenu.innerHTML = h;
      var inp = el("rig-name");
      if (inp) { inp.focus(); inp.select(); }
    }
    function pick(id) {
      closeMenu();
      fetch("/api/rig", { method: "POST",
        headers: { "Content-Type": "application/json", "X-LB-Key": TARGET.apiKey },
        body: JSON.stringify({ id: id }) }).then(function (r) { return r.json(); })
        .then(function (res) {
          if (res && res.ok) { setRig(res.rig); toast("Framing with " + res.rig.label + " · Tonight's Plan now using it"); }
          else showMsg(esc((res && res.error) || "Couldn't switch rigs."), "warn");
        }).catch(function () { showMsg("Couldn't reach the app — is it still open?", "warn"); });
    }
    function saveRig() {
      var inp = el("rig-name"), name = inp ? inp.value.trim() : "";
      if (!name) { saveErr = "Please enter a name."; renderMenu(); return; }
      fetch("/api/saverig", { method: "POST",
        headers: { "Content-Type": "application/json", "X-LB-Key": TARGET.apiKey },
        body: JSON.stringify({ name: name }) }).then(function (r) { return r.json(); })
        .then(function (res) {
          if (res && res.ok) {
            // The frame's rig is now this saved rig (if the map is on it)
            if (res.rig && res.rig.saved) { TARGET.rig = res.rig.label; syncRigChip(); Labels.repaint(); }
            toast("Saved rig: " + name);
            closeMenu();
          } else { saveErr = (res && res.error) || "Couldn't save the rig."; renderMenu(); }
        }).catch(function () { saveErr = "Couldn't reach the app — is it still open?"; renderMenu(); });
    }
    rigEl.addEventListener("click", function (e) {
      e.stopPropagation();
      if (menuOpen) closeMenu(); else openMenu();
    });
    rigMenu.addEventListener("click", function (e) {
      e.stopPropagation();
      var a = e.target.closest("[data-act]");
      if (a) {
        var act = a.getAttribute("data-act");
        if (act === "save") { saving = true; saveErr = ""; renderMenu(); }
        else if (act === "nosave") { saving = false; saveErr = ""; renderMenu(); }
        else if (act === "dosave") saveRig();
        return;
      }
      var row = e.target.closest("[data-rig]");
      if (row && !row.classList.contains("cur")) pick(row.getAttribute("data-rig"));
      else if (row) closeMenu();
    });
    rigMenu.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && e.target.id === "rig-name") { e.preventDefault(); saveRig(); }
      else if (e.key === "Escape") { e.stopPropagation(); closeMenu(); }
    });
    ["mousedown", "wheel", "dblclick", "contextmenu"].forEach(function (ev) {
      rigMenu.addEventListener(ev, function (e) { e.stopPropagation(); });
    });
    window.addEventListener("mousedown", function (e) {
      if (menuOpen && !rigMenu.contains(e.target) && !rigEl.contains(e.target)) closeMenu();
    }, true);

    clearMsg(); syncBar();
    return {
      tagBox: function () { return tagBox; },
      setRig: setRig,
      draw: draw,
      busy: function () { return !!drag; },
      flashing: function () { return performance.now() < flashUntil; },
      refresh: syncBar,
      frameOn: frameOn,
      frameAtScreen: frameAtScreen
    };
  })();


  /* ---------- 6b. Custom coordinate grid ------------------------- *
   * Drawn via the same per-feature path as the frame/boundaries so
   * its toggle is reliable (the engine's built-in graticule uses a
   * single draw call that won't re-fire after an apply()).
   * The RA/Dec marker TEXT is placed by the label engine (7b) so it
   * takes part in collision avoidance; gridMarkers() supplies it.   */
  var GRIDON = true;                            // equatorial grid on/off
  var gridStyle = { stroke: T.grid, width: 0.8, opacity: 0.7 };

  /* --- Equatorial graticule (RA hours / Dec degrees) --- */
  var gridJSON = { type: "FeatureCollection",
    features: d3.geo.graticule().step([15, 15]).lines().map(function (g) {
      return { type: "Feature", properties: {}, geometry: g };
    }) };

  Celestial.add({
    type: "line",
    callback: function (error) {
      if (error) return console.warn(error);
      var data = Celestial.getData(gridJSON, config.transform);
      Celestial.container.selectAll(".lbgrid")
        .data(data.features).enter().append("path").attr("class", "lbgrid");
      Celestial.redraw();
    },
    redraw: function () {
      if (!GRIDON) return;
      var ctx = Celestial.context;
      ctx.save();
      try {
        ctx.strokeStyle = gridStyle.stroke;
        ctx.lineWidth = gridStyle.width;
        ctx.globalAlpha = gridStyle.opacity;
        ctx.setLineDash([]);
        Celestial.container.selectAll(".lbgrid").each(function (d) {
          ctx.beginPath();
          Celestial.map(d);
          ctx.stroke();
        });
      } catch (e) { console.warn("grid draw skipped:", e); }
      ctx.restore();
    }
  });

  // RA (hours) / Dec (degrees) markers, set off toward the lower-left.
  // Returns [{text, x, y}] in screen px (centre-aligned).
  function gridMarkers() {
    var out = [];
    if (!GRIDON) return out;
    var c = Celestial.rotate();
    if (!c) return out;
    var s = Celestial.mapProjection.scale();
    var fw = (mapWrap.clientWidth  / s) * (180 / Math.PI);
    var fh = (mapWrap.clientHeight / s) * (180 / Math.PI);
    var cosL = Math.max(Math.cos(c[1] * D2R), 0.15);
    var labDec = Math.max(-89, Math.min(89, c[1] - fh * 0.33));
    var labLon = c[0] - (fw * 0.33) / cosL;
    labLon = ((labLon % 360) + 360) % 360; if (labLon > 180) labLon -= 360;
    var ra, dec, lon, pt;
    for (ra = 0; ra < 360; ra += 15) {
      lon = ra > 180 ? ra - 360 : ra;
      if (Celestial.clip([lon, labDec])) {
        pt = Celestial.mapProjection([lon, labDec]);
        if (pt) out.push({ text: (ra / 15) + "h", x: pt[0], y: pt[1] });
      }
    }
    for (dec = -75; dec <= 75; dec += 15) {
      if (Celestial.clip([labLon, dec])) {
        pt = Celestial.mapProjection([labLon, dec]);
        if (pt) out.push({ text: (dec >= 0 ? "+" : "") + dec + "°", x: pt[0], y: pt[1] });
      }
    }
    return out;
  }


  /* ---------- 7. Reticle + readout (per redraw) ----------------- */
  var coordEl   = document.getElementById("coord");
  var fovLblEl  = document.getElementById("fov-lbl");
  var mapWrap   = document.getElementById("map-wrap");

  Celestial.addCallback(function () {
    var tp = [LON, TARGET.dec];
    if (Celestial.clip(tp)) {
      var pt = Celestial.mapProjection(tp);
      var ctx = Celestial.context;
      ctx.save();
      ctx.globalAlpha = 1;
      ctx.strokeStyle = T.reticle; ctx.lineWidth = 1.4; ctx.setLineDash([]);
      ctx.beginPath(); ctx.arc(pt[0], pt[1], 13, 0, 2 * Math.PI); ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(pt[0], pt[1] - 17); ctx.lineTo(pt[0], pt[1] - 23);
      ctx.moveTo(pt[0], pt[1] + 17); ctx.lineTo(pt[0], pt[1] + 23);
      ctx.moveTo(pt[0] + 17, pt[1]); ctx.lineTo(pt[0] + 23, pt[1]);
      ctx.moveTo(pt[0] - 17, pt[1]); ctx.lineTo(pt[0] - 23, pt[1]);
      ctx.stroke();
      ctx.restore();
    }
    updateReadout();
    Labels.onSkyRedraw();
  });

  function updateReadout() {
    var c = Celestial.rotate();                 // [lon, lat, orient] internal deg
    if (c) {
      var raDeg = c[0] < 0 ? c[0] + 360 : c[0];
      coordEl.textContent = fmtRA(raDeg) + " · " + fmtDec(c[1]) + " · " + fovText();
    }
  }
  function fieldDeg() {
    try { return (mapWrap.clientWidth / Celestial.mapProjection.scale()) * (180 / Math.PI); }
    catch (e) { return 60; }
  }
  function fovText() {
    try {
      var deg = fieldDeg();
      var lbl = deg >= 10 ? Math.round(deg) + "°"
              : deg >= 1  ? deg.toFixed(1) + "°"
                          : Math.round(deg * 60) + "′";
      fovLblEl.textContent = lbl + " field";
      return "field " + lbl;
    } catch (e) { fovLblEl.textContent = "field"; return "field —"; }
  }

  /* ---------- 6d. App link: API helpers, planned markers, target card,
   *               right-click menu ------------------------------------ *
   * Click an object (or a row in the pinned lens) → its card: tonight's
   * altitude curve with the best window, moon, how it fits the rig frame,
   * and "⌖ Frame here" (puts the composer frame on it) / "＋ Tonight"
   * (adds it centred, PA 0 — the same neutral add as the app's target
   * cards).  Planned targets get a ✓ label and, for this rig, a ghost of
   * their framing.  Right-click: frame this spot / pin the lens.        */
  function lonToRa0(l) { return l < 0 ? l + 360 : l; }
  function escHtml(t) {
    return String(t == null ? "" : t).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function apiGet(path, params) {
    if (!TARGET.apiKey) return Promise.reject(new Error("standalone"));
    var qs = Object.keys(params || {}).map(function (k) {
      return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]);
    }).join("&");
    return fetch(path + (qs ? "?" + qs : ""), { headers: { "X-LB-Key": TARGET.apiKey } })
      .then(function (r) { return r.json(); });
  }
  function postTonight(body) {
    if (!TARGET.apiKey) return Promise.reject(new Error("standalone"));
    return fetch("/api/tonight", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-LB-Key": TARGET.apiKey },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); });
  }
  function toast(text) {
    var t = document.getElementById("toast"); t.textContent = text; t.classList.add("on");
    clearTimeout(t._h); t._h = setTimeout(function () { t.classList.remove("on"); }, 2200);
  }
  // Catalog-ID key that ignores spacing, dashes and zero-padding, so the
  // app's "NGC0224" / "Sh2-155" match the map's "NGC 224" / "Sh2 155".
  function compactId(s) {
    return String(s || "").toUpperCase().replace(/[\s\-_]/g, "").replace(/^([A-Z]+)0+(\d)/, "$1$2");
  }

  /* --- Tonight's Plan markers --- */
  var Plan = (function () {
    var items = [], pts = [];
    function refresh() {
      apiGet("/api/plan").then(function (r) {
        if (!r || !r.ok) return;
        items = r.items || [];
        Labels.repaint(); Card.planChanged();
      }).catch(function () {});
    }
    // Screen positions, computed once per label paint.  idHit: some map
    // object (or the target) carries this plan's ID, so position matching
    // is off for it — a neighbour sitting under its centre isn't it.
    function prep(snapshot) {
      pts = [];
      var ids = {};
      if (snapshot) snapshot.dsos.forEach(function (p) { ids[compactId(p.o.text)] = 1; });
      ids[compactId(TARGET.name)] = 1;
      items.forEach(function (it) {
        var c = [raToLon(it.ra), it.dec];
        if (!Celestial.clip(c)) return;
        var p = Celestial.mapProjection(c), key = compactId(it.id);
        if (p) pts.push({ it: it, x: p[0], y: p[1], key: key, idHit: !!ids[key] });
      });
      return pts;
    }
    // Is a map object planned?  Its ID, or — only for a plan whose ID no
    // map object has (M 31 on the map vs NGC0224 in the plan) — the plan's
    // centre sitting right on it.
    function match(id, x, y) {
      var k = compactId(id), i;
      for (i = 0; i < pts.length; i++) if (pts[i].key === k) return pts[i];
      for (i = 0; i < pts.length; i++) {
        if (!pts[i].idHit && Math.hypot(pts[i].x - x, pts[i].y - y) < 3) return pts[i];
      }
      return null;
    }
    // Planned framings: every rig's, each at its own FOV and in its own
    // accent colour, tagged "✓ planned · <rig>" — so a framing sent earlier
    // on another rig can never be mistaken for the live frame.
    var hidden = PREFS.hidePlanned === true, tags = [];
    var hideEl = document.getElementById("cmp-hide");
    function syncHide() {
      if (!hideEl) return;
      hideEl.classList.toggle("on", hidden);
      hideEl.textContent = hidden ? "◉ Show planned" : "◌ Hide planned";
    }
    if (hideEl) hideEl.addEventListener("click", function () {
      hidden = !hidden;
      syncHide(); Labels.repaint(); savePrefs();
    });
    syncHide();
    function draw(ctx) {
      tags = [];
      if (hidden) return;
      ctx.save();
      ctx.lineWidth = 1.1; ctx.globalAlpha = 0.8;
      pts.forEach(function (p) {
        var it = p.it, ink = rigInk(it.color, T.planned);
        var fw = +it.fovw || (it.same_rig ? TARGET.fovw : 0), fh = +it.fovh || (it.same_rig ? TARGET.fovh : 0);
        ctx.strokeStyle = ink;
        if (fw > 0 && fh > 0) {
          var ring = frameRing(it.ra, it.dec, it.pa, fw, fh), ok = true, poly = [];
          for (var i = 0; i < ring.length; i++) {
            var q = Celestial.clip(ring[i]) ? Celestial.mapProjection(ring[i]) : null;
            if (!q) { ok = false; break; }
            poly.push(q);
          }
          if (!ok) return;
          ctx.setLineDash([3, 3]); ctx.beginPath();
          poly.forEach(function (q, i) { if (i) ctx.lineTo(q[0], q[1]); else ctx.moveTo(q[0], q[1]); });
          ctx.closePath(); ctx.stroke(); ctx.setLineDash([]);
          // Tag on the lower-left corner (the live frame's tag sits top-left)
          var xs = poly.map(function (q) { return q[0]; }), ys = poly.map(function (q) { return q[1]; });
          var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs), maxY = Math.max.apply(null, ys);
          if (maxX - minX >= 40 && it.rig) {
            var text = "✓ planned · " + it.rig;
            ctx.font = "9.5px Helvetica, Arial, sans-serif";
            while (text.length > 14 && ctx.measureText(text).width > Math.max(maxX - minX, 120)) text = text.slice(0, -2) + "…";
            var w = ctx.measureText(text).width, y = maxY + 3;
            ctx.textBaseline = "top"; ctx.textAlign = "left";
            ctx.lineJoin = "round"; ctx.lineWidth = 3; ctx.strokeStyle = T.sky; ctx.globalAlpha = 0.95;
            ctx.strokeText(text, minX, y); ctx.fillStyle = ink; ctx.fillText(text, minX, y);
            ctx.lineWidth = 1.1; ctx.globalAlpha = 0.8;
            tags.push([minX - 2, y - 2, w + 4, 15]);
          }
        } else {
          ctx.beginPath(); ctx.arc(p.x, p.y, 7, 0, 2 * Math.PI); ctx.stroke();
        }
      });
      ctx.restore();
    }
    setInterval(function () { if (!document.hidden) refresh(); }, 5000);
    return { refresh: refresh, prep: prep, match: match, draw: draw,
             points: function () { return pts; },
             tagBoxes: function () { return tags; },
             isHidden: function () { return hidden; } };
  })();

  /* --- Target card --- */
  var Card = (function () {
    var el = document.getElementById("card");
    var cur = null, data = null, err = "", req = 0, busy = false;

    function open(obj) {
      cur = obj; data = null; err = ""; busy = false;
      el.style.display = "block";
      render(); position(); load();
    }
    function close() { cur = null; el.style.display = "none"; Labels.repaint(); }
    function load() {
      var my = ++req;
      if (!TARGET.apiKey) { err = "standalone"; render(); position(); return; }
      apiGet("/api/target", { ra: cur.ra, dec: cur.dec, id: cur.id, kind: cur.kind })
        .then(function (r) {
          if (my !== req || !cur) return;
          if (r && r.ok) data = r; else err = (r && r.error) || "The app couldn't load this target.";
          render(); position();
        }).catch(function () {
          if (my !== req || !cur) return;
          err = "Couldn't reach the app — is it still open?"; render(); position();
        });
    }

    // Anchor beside the object; follow it while the map pans/zooms.
    function position() {
      if (!cur) return;
      var c = [raToLon(cur.ra), cur.dec];
      var p = Celestial.clip(c) ? Celestial.mapProjection(c) : null;
      if (!p) { el.style.visibility = "hidden"; return; }
      el.style.visibility = "visible";
      var W = mapWrap.clientWidth, H = mapWrap.clientHeight;
      var w = el.offsetWidth, h = el.offsetHeight;
      var bar = document.getElementById("composer");
      var bottomLimit = H - (bar ? bar.offsetHeight + 22 : 14);
      var x = p[0] + 22;
      if (x + w > W - 8) x = p[0] - 22 - w;
      x = Math.max(8, Math.min(x, W - w - 8));
      var y = Math.max(8, Math.min(p[1] - h / 2, bottomLimit - h));
      el.style.left = x + "px"; el.style.top = y + "px";
    }

    function sizeArcmin() {
      if (data && data.size_maj > 0) return [data.size_maj, data.size_min || data.size_maj];
      var n = String(cur.dim || "").match(/\d+(?:\.\d+)?/g);
      return n ? [parseFloat(n[0]), parseFloat(n[1] || n[0])] : null;
    }
    function fitChip() {
      var sz = sizeArcmin();
      if (!sz || !(TARGET.fovw > 0)) return "";
      var W = TARGET.fovw * 60, H = TARGET.fovh * 60, a = Math.max(sz[0], sz[1]), b = Math.min(sz[0], sz[1]);
      if (a < 0.12 * W) return '<span class="kchip warn">▣ small in frame (' + Math.max(1, Math.round(a / W * 100)) + '%)</span>';
      if (a <= W * 0.9 && b <= H * 0.9) return '<span class="kchip ok">▣ fits one frame</span>';
      var o1 = [Math.ceil(a / (W * 0.85)), Math.ceil(b / (H * 0.85))];
      var o2 = [Math.ceil(a / (H * 0.85)), Math.ceil(b / (W * 0.85))];
      var m = o1[0] * o1[1] <= o2[0] * o2[1] ? o1 : o2;
      return '<span class="kchip warn">▣ needs ~' + m[0] + "×" + m[1] + " mosaic</span>";
    }
    function spark() {
      var c = data.curve, n = c.alts.length, Wd = 256, Hd = 58;
      var y = function (alt) { return (Hd - 4) - Math.max(0, Math.min(90, alt)) / 90 * (Hd - 8); };
      var d = c.alts.map(function (a, i) { return (i ? "L" : "M") + (i / (n - 1) * Wd).toFixed(1) + " " + y(a).toFixed(1); }).join("");
      var w = data.window || {}, win = "";
      if (w.hrs > 0 && w.start_frac != null && w.end_frac != null) {
        var x0 = w.start_frac * Wd, x1 = w.end_frac * Wd;
        if (x1 < x0) x1 = Wd;
        win = '<rect x="' + x0.toFixed(1) + '" y="0" width="' + (x1 - x0).toFixed(1) + '" height="' + Hd + '" class="kwin"/>';
      }
      var ym = y(data.min_alt).toFixed(1);
      return '<svg class="kspark" viewBox="0 0 ' + Wd + " " + Hd + '" preserveAspectRatio="none">' +
        '<rect x="0" y="0" width="' + Wd + '" height="' + Hd + '" rx="6" class="kbg"/>' + win +
        '<line x1="' + (c.dusk_frac * Wd).toFixed(1) + '" y1="0" x2="' + (c.dusk_frac * Wd).toFixed(1) + '" y2="' + Hd + '" class="ktw"/>' +
        '<line x1="' + (c.dawn_frac * Wd).toFixed(1) + '" y1="0" x2="' + (c.dawn_frac * Wd).toFixed(1) + '" y2="' + Hd + '" class="ktw"/>' +
        '<line x1="0" y1="' + ym + '" x2="' + Wd + '" y2="' + ym + '" class="kmin"/>' +
        '<text x="4" y="' + (ym - 3) + '" class="klbl">' + Math.round(data.min_alt) + "°</text>" +
        '<path d="' + d + '" class="kcurve"/></svg>';
    }

    function render() {
      if (!cur) return;
      var id = (data && data.id) || cur.id;
      var common = (data && data.common) || cur.common || "";
      var bits = [];
      if (cur.detail) bits.push(cur.detail);
      else if (data) {
        if (data.obj_type) bits.push(data.obj_type);
        if (data.v_mag != null) bits.push("mag " + (+data.v_mag).toFixed(1));
        var sz = sizeArcmin(); if (sz) bits.push(Math.round(sz[0]) + "′ × " + Math.round(sz[1]) + "′");
      }
      var h = '<div class="khd"><span class="kid">' + escHtml(id) + "</span>" +
              (common ? '<span class="kcom">' + escHtml(common) + "</span>" : "") +
              '<span class="kx" data-act="close" title="Close (Esc)">✕</span></div>' +
              '<div class="kdesc">' + escHtml(bits.join(" · ") || "Deep-sky object") + "</div>";

      if (err === "standalone") {
        h += '<div class="knote">Open the sky map from the app to see tonight\'s data and add targets.</div>';
      } else if (err) {
        h += '<div class="knote warn">' + escHtml(err) + "</div>";
      } else if (!data) {
        h += '<div class="kskel"></div><div class="knote">Checking tonight…</div>';
      } else if (!data.has_location) {
        h += '<div class="knote warn">Set an observer location in the app to see tonight\'s window.</div>';
      } else if (!data.curve) {
        h += '<div class="knote warn">No astronomical darkness tonight at your location.</div>';
      } else {
        var w = data.window || {}, pk = data.peak || {};
        h += spark() + '<div class="ktimes"><span>dusk ' + data.curve.dusk + "</span><span>" +
             (w.hrs > 0 ? "best " + w.start + "–" + w.end : "never above " + Math.round(data.min_alt) + "°") +
             " · peak " + pk.alt + "° " + pk.time + "</span><span>dawn " + data.curve.dawn + "</span></div>";
      }
      var chips = fitChip();
      if (data && data.moon) {
        var mc = data.moon.tag === "optimal" ? "ok" : data.moon.tag === "highlight" ? "warn" : "bad";
        chips += '<span class="kchip ' + mc + '" title="' + escHtml(data.moon.impact) + '">☾ ' +
                 data.moon.sep + "° · " + data.moon.illum + "%</span>";
      }
      if (data) chips += data.in_catalog ? '<span class="kchip">In app catalog</span>'
                                         : '<span class="kchip" title="Not in the app\'s catalog — added as a custom target with this name and position">Custom target</span>';
      var mine = data ? data.planned.filter(function (p) { return p.same_rig && p.filter_mode === data.rig_filter; }) : [];
      var other = data ? data.planned.filter(function (p) { return !p.same_rig; }) : [];
      if (other.length) chips += '<span class="kchip ok">✓ planned on ' + escHtml(other[0].scope) + "</span>";
      if (chips) h += '<div class="kchips">' + chips + "</div>";
      h += '<div class="kmsg" id="kmsg"></div>';
      var canAct = !!TARGET.apiKey;
      h += '<div class="kact">' +
           '<span class="cbtn' + (TARGET.fovw > 0 ? "" : " off") + '" data-act="frame" title="Put the rig frame on it — then drag / rotate and Send framing">⌖ Frame here</span>' +
           (mine.length ? '<span class="cbtn done" title="Already in Tonight\'s Plan on this rig — use Frame here to change its framing">✓ In Tonight · ' + escHtml(mine[0].filter_mode || "planned") + "</span>"
                        : '<span class="cbtn pri' + (canAct ? "" : " off") + '" data-act="add" title="Add centred, PA 0°, with this rig">＋ Tonight</span>') +
           "</div>";
      el.innerHTML = h;
    }

    function msg(html, cls) { var m = document.getElementById("kmsg"); if (m) { m.className = "kmsg " + (cls || ""); m.innerHTML = html; } }
    function add(force) {
      if (!cur || busy) return;
      busy = true; msg("Adding…");
      var id = cur.kind === "target" ? TARGET.name : cur.id;
      postTonight({ name: id, ra: cur.ra, dec: cur.dec, pa: 0,
                    object: cur.kind === "target" ? { kind: "target", id: TARGET.name, ra: TARGET.ra, dec: TARGET.dec }
                          : { kind: "dso", id: cur.id, type: cur.type || "", dim: cur.dim || "", common: cur.common || "",
                              ra: cur.ra, dec: cur.dec },
                    force: !!force })
        .then(function (res) {
          busy = false;
          if (res.ok) { toast(res.message); Plan.refresh(); load(); }
          else if (res.confirm) {
            msg(escHtml(res.message) + ' <span class="cbtn" data-act="force">Add anyway</span><span class="cbtn ghost" data-act="cancel">Cancel</span>', "warn");
          } else msg(escHtml(res.error || "The app couldn't add it."), "warn");
        }).catch(function () { busy = false; msg("Couldn't reach the app — is it still open?", "warn"); });
    }
    el.addEventListener("click", function (e) {
      var a = e.target.closest("[data-act]"); if (!a || a.classList.contains("off")) return;
      var act = a.getAttribute("data-act");
      if (act === "close") close();
      else if (act === "frame") { Composer.frameOn(cur); close(); }
      else if (act === "add") add(false);
      else if (act === "force") add(true);
      else if (act === "cancel") msg("");
    });
    // Keep map gestures from starting under the card.
    ["mousedown", "wheel", "dblclick", "contextmenu"].forEach(function (ev) {
      el.addEventListener(ev, function (e) { e.stopPropagation(); });
    });

    return {
      open: open, close: close, position: position,
      isOpen: function () { return !!cur; },
      planChanged: function () { if (cur && data) load(); }
    };
  })();

  /* --- Right-click menu --- */
  var Menu = (function () {
    var el = document.getElementById("ctxmenu"), at = null, obj = null;
    function open(pt, o) {
      at = pt; obj = o;
      var h = "";
      if (o) {
        h += '<div data-act="card">ⓘ ' + escHtml(o.id) + "</div>" +
             '<div data-act="frameobj">⌖ Frame ' + escHtml(o.id) + "</div>";
      }
      h += '<div data-act="spot">⌖ Frame this spot</div>';
      h += Labels.isPinned() ? '<div data-act="unpin">◎ Release the lens</div>'
                             : '<div data-act="pin">◎ Pin the lens here</div>';
      el.innerHTML = h; el.style.display = "block";
      var W = mapWrap.clientWidth, H = mapWrap.clientHeight;
      el.style.left = Math.min(pt.x, W - el.offsetWidth - 6) + "px";
      el.style.top = Math.min(pt.y, H - el.offsetHeight - 6) + "px";
    }
    function close() { el.style.display = "none"; }
    el.addEventListener("click", function (e) {
      var a = e.target.closest("[data-act]"); if (!a) return;
      var act = a.getAttribute("data-act"); close();
      if (act === "card") Card.open(obj);
      else if (act === "frameobj") Composer.frameOn(obj);
      else if (act === "spot") Composer.frameAtScreen([at.x, at.y]);
      else if (act === "pin") Labels.pinAt(at);
      else if (act === "unpin") Labels.pinAt(null);
    });
    el.addEventListener("mousedown", function (e) { e.stopPropagation(); });
    mapWrap.addEventListener("contextmenu", function (e) {
      if (!(e.target && e.target.tagName === "CANVAS")) return;
      e.preventDefault();
      var r = mapWrap.getBoundingClientRect(), pt = { x: e.clientX - r.left, y: e.clientY - r.top };
      open(pt, Labels.objectAt(pt));
    });
    window.addEventListener("mousedown", function (e) { if (!el.contains(e.target)) close(); }, true);
    window.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
    return { close: close };
  })();

  /* ---------- 7b. Label engine + callout lens -------------------- *
   * One overlay canvas, one collision pass, in priority order:
   *   target → RA/Dec markers → constellation names → DSOs/stars
   * (DSOs and stars interleave by effective magnitude, DSOs boosted).
   * Each point label tries four slots (NE, SE, NW, SW), starting from
   * the slot it used last frame (sticky, so labels don't jump while
   * panning) and is dropped if none are free. New labels fade in.
   *
   * The callout lens reserves its circle and callout columns BEFORE the
   * map labels are placed, so map labels step aside; objects inside the
   * lens are listed in tidy columns beside it with leader lines — the
   * column stack means lens labels can never overlap.
   *
   * Map labels are drawn per the density preset (magnitude limits that
   * rise as the field narrows); the lens lists everything shown on the
   * map, filtered only by the ★ / DSO toggles.                         */
  var Labels = (function () {
    var cv = document.getElementById("label-layer");
    var ctx = cv.getContext("2d");
    var DPR = window.devicePixelRatio || 1;

    var S = { stars: false, dsos: true, cons: true, lens: true, density: 1 };
    // Base magnitude limits at a 120° field; they rise as the field narrows.
    var DENSITY = [ { star: 1.0, dso: 5.5 }, { star: 2.0, dso: 7.0 }, { star: 3.0, dso: 8.5 } ];

    var FONT = {
      star:   "10px Helvetica, Arial, sans-serif",
      dso:    "10px Helvetica, Arial, sans-serif",
      grid:   "10px Helvetica, Arial, sans-serif",
      target: "bold 11px Helvetica, Arial, sans-serif",
      con:    ["bold 13px Helvetica, Arial, sans-serif",
               "bold 12px Helvetica, Arial, sans-serif",
               "bold 11px Helvetica, Arial, sans-serif"],
      row:    "11px Helvetica, Arial, sans-serif",
      rowDim: "10px Helvetica, Arial, sans-serif"
    };
    var TYPE = {
      g: "Galaxy", s: "Spiral galaxy", s0: "Lenticular galaxy", sd: "Dwarf galaxy",
      e: "Elliptical galaxy", i: "Irregular galaxy", gg: "Galaxy cluster",
      oc: "Open cluster", gc: "Globular cluster", en: "Emission nebula",
      bn: "Bright nebula", sfr: "Star-forming region", rn: "Reflection nebula",
      pn: "Planetary nebula", snr: "Supernova remnant", dn: "Dark nebula",
      ast: "Asterism", ds: "Double star", pos: "Position"
    };

    var starNames = {}, dsoNames = {};
    d3.json(config.datapath + "starnames.json", function (e, j) {
      if (!e && j) { starNames = j; namedStars = null; Labels.onSkyRedraw(); }
    });
    d3.json(config.datapath + "dsonames.json", function (e, j) {
      if (!e && j) { dsoNames = j; dsoList = null; Labels.onSkyRedraw(); }
    });

    // Candidate lists, built once per catalog load (not per frame).
    var namedStars = null, namedStarCount = -1;
    var dsoList = null, dsoCount = -1;

    function num(v) { var n = parseFloat(v); return isFinite(n) ? n : null; }

    function buildStars() {
      var sel = Celestial.container.selectAll(".star");
      if (namedStars && sel.size() === namedStarCount) return;
      namedStarCount = sel.size();
      namedStars = [];
      sel.each(function (d) {
        var sn = starNames[d.id];
        if (!sn) return;
        var desig = sn.desig ? (sn.desig + (sn.c ? " " + sn.c : "")) : "";
        var proper = sn.name || "";
        if (!proper && !desig) return;
        namedStars.push({
          k: "s" + d.id, kind: "star", c: d.geometry.coordinates,
          mag: d.properties.mag, proper: proper,
          // Bayer/Flamsteed (δ Cyg, 61 Cyg) are map-label worthy when zoomed
          // in; variable-star / Gliese IDs (V1934 Cyg, GJ 1249) are lens-only.
          classic: !!(sn.bayer || sn.flam),
          text: proper || desig,
          detail: (proper && desig ? desig + " · " : "") + "mag " + (+d.properties.mag).toFixed(1)
        });
      });
    }

    // "120x100" → "120′ × 100′"; survey catalogs store a raw float ("50.78…").
    function fmtDim(dim) {
      return String(dim).split("x").map(function (v) {
        var n = parseFloat(v);
        if (!isFinite(n)) return v;
        return (n >= 10 ? Math.round(n) : Math.round(n * 10) / 10) + "′";
      }).join(" × ");
    }

    function buildDsos() {
      var sel = Celestial.container.selectAll(".dso");
      if (dsoList && sel.size() === dsoCount) return;
      dsoCount = sel.size();
      dsoList = [];
      sel.each(function (d) {
        var p = d.properties;
        var id = p.name || p.desig || String(d.id);        // M31 / NGC 7000
        var common = p.alt || (dsoNames[d.id] && dsoNames[d.id].name) || "";
        if (common === id) common = "";
        // Survey catalogs (LDN/LBN/Barnard/SNR/RCW…) reuse the "mag" field for
        // opacity/brightness classes (1–6), not magnitudes — ignore it there.
        var survey = /^(LBN|LDN|B|RCW|Ced|Cr|Tr|Mel|St|Do|Be|Ru|PGC|UGC|ESO|PK)\s?\d|^SNR\b/.test(id);
        var mag = survey ? null : num(p.mag); if (mag === 999) mag = null;
        var dim = num(String(p.dim || "").split("x")[0]);
        // Effective magnitude for priority: catalogue mag, or for mag-less
        // extended nebulae a size-based stand-in (bigger = more prominent).
        var eff = mag !== null ? mag
                : dim ? Math.max(4, 12 - 2 * Math.log(dim / 15) / Math.LN2) : 13;
        // Showpiece catalogs outrank the big survey catalogs (LBN/LDN/B…),
        // and dark nebulae — hard to see on a star chart — rank lower still.
        if (/^(M|C)\s?\d/.test(id)) eff -= 1.5;
        else if (survey) eff += 3;
        if (p.type === "dn") eff += 1.5;
        var bits = [];
        if (TYPE[p.type]) bits.push(TYPE[p.type]);
        if (mag !== null) bits.push("mag " + mag.toFixed(1));
        if (p.dim) bits.push(fmtDim(p.dim));
        dsoList.push({
          k: "d" + d.id, kind: "dso", c: d.geometry.coordinates, eff: eff, p: p,
          group: dsoGroup(p.type),
          text: id, common: common, detail: bits.join(" · ")
        });
      });
    }

    // Per-sky-redraw screen snapshot, reused by every lens repaint.
    var snap = null;

    function project(c, W, H) {
      if (!Celestial.clip(c)) return null;
      var pt = Celestial.mapProjection(c);
      if (!pt || pt[0] < -40 || pt[1] < -20 || pt[0] > W + 40 || pt[1] > H + 20) return null;
      return pt;
    }

    function takeSnapshot() {
      var W = mapWrap.clientWidth, H = mapWrap.clientHeight;
      buildStars(); buildDsos();
      var magLim = parseFloat(magSlider.value);
      var dsoLim = config.dsos.limit;
      // Same test used to DRAW a DSO symbol (the engine's dsoDisplay, plus the
      // 6c pass for magnitude-less nebulae), so every symbol has a name —
      // priority (eff) only affects map-label order, never lens visibility.
      function drawn(p) {
        if (engineSkips(p, dsoLim)) return true;              // drawn by 6c
        var m = p.mag;
        return (m === 999 && Math.sqrt(parseInt(p.dim, 10)) > dsoLim) ||
               (m !== 999 && m <= dsoLim);
      }
      var tpt = project([LON, TARGET.dec], W, H);
      var s = { W: W, H: H, field: fieldDeg(), target: tpt, stars: [], dsos: [], cons: [],
                grid: gridMarkers() };
      namedStars.forEach(function (o) {
        if (o.mag > magLim) return;
        var pt = project(o.c, W, H); if (!pt) return;
        s.stars.push({ o: o, x: pt[0], y: pt[1] });
      });
      dsoList.forEach(function (o) {
        if (!drawn(o.p)) return;
        var pt = project(o.c, W, H); if (!pt) return;
        // The target itself is labeled separately — skip its catalog twin.
        if (tpt && Math.abs(pt[0] - tpt[0]) < 4 && Math.abs(pt[1] - tpt[1]) < 4) return;
        s.dsos.push({ o: o, x: pt[0], y: pt[1] });
      });
      Celestial.container.selectAll(".constname").each(function (d) {
        var pt = project(d.geometry.coordinates, W, H); if (!pt) return;
        var rank = Math.min(Math.max(parseInt(d.properties.rank, 10) || 3, 1), 3);
        s.cons.push({ text: d.properties.name, rank: rank, x: pt[0], y: pt[1] });
      });
      return s;
    }

    /* --- geometry helpers --- */
    var wcache = {};
    function tw(font, text) {
      var key = font + "|" + text;
      if (!(key in wcache)) { ctx.font = font; wcache[key] = ctx.measureText(text).width; }
      return wcache[key];
    }
    // Map-label text with a sky-coloured halo, so names stay readable over
    // DSO symbols, constellation lines and the Milky Way.
    function haloText(text, x, y, font, fill, alpha) {
      ctx.font = font;
      ctx.globalAlpha = alpha;
      ctx.lineJoin = "round"; ctx.lineWidth = 3; ctx.strokeStyle = T.sky;
      ctx.strokeText(text, x, y);
      ctx.fillStyle = fill; ctx.fillText(text, x, y);
    }
    function overlaps(b, list, pad) {
      for (var i = 0; i < list.length; i++) {
        var c = list[i];
        if (b[0] < c[0] + c[2] + pad && b[0] + b[2] + pad > c[0] &&
            b[1] < c[1] + c[3] + pad && b[1] + b[3] + pad > c[1]) return true;
      }
      return false;
    }
    function inView(b, W, H) { return b[0] >= 2 && b[1] >= 2 && b[0] + b[2] <= W - 2 && b[1] + b[3] <= H - 2; }

    // Glass control panels are no-label zones.
    function panelRects() {
      var wr = mapWrap.getBoundingClientRect(), out = [];
      mapWrap.querySelectorAll(".glass").forEach(function (el) {
        var r = el.getBoundingClientRect();
        out.push([r.left - wr.left - 4, r.top - wr.top - 4, r.width + 8, r.height + 8]);
      });
      return out;
    }

    /* --- sticky slots + fade-in state --- */
    var lastSlot = {}, born = {}, fading = false;
    function alphaFor(k, now) {
      if (!(k in born)) born[k] = now;
      var a = Math.min(1, (now - born[k]) / 150);
      if (a < 1) fading = true;
      return a;
    }

    /* --- lens state --- */
    var LENS_R = 64, ROW_H = 16, COL_W_MAX = 280, MAX_ROWS = 14;
    var mouse = null;          // screen px while hovering the sky
    var pinned = null;         // sky coords [lon, lat] when pinned
    var dragging = false;

    function lensCentre() {
      if (!S.lens) return null;
      if (pinned) {
        if (!Celestial.clip(pinned)) return null;
        var pt = Celestial.mapProjection(pinned);
        return pt ? { x: pt[0], y: pt[1] } : null;
      }
      if (dragging || !mouse || Composer.busy()) return null;
      return mouse;
    }

    // Lay out callout columns for everything under the lens.
    function layoutLens(s, L) {
      var R = LENS_R, items = [];
      if (S.dsos) s.dsos.forEach(function (p) { if (DSO_ON[p.o.group] && Math.hypot(p.x - L.x, p.y - L.y) < R) items.push(p); });
      if (S.stars) s.stars.forEach(function (p) { if (Math.hypot(p.x - L.x, p.y - L.y) < R) items.push(p); });
      if (s.target && Math.hypot(s.target[0] - L.x, s.target[1] - L.y) < R) {
        items.push({ target: true, x: s.target[0], y: s.target[1],
                     o: { text: TARGET.name, common: TARGET.common, detail: "Target", kind: "target" } });
      }
      // Most prominent first; cap the list.
      items.sort(function (a, b) { return rank(a) - rank(b); });
      var capRows = Math.max(3, Math.min(MAX_ROWS, Math.floor((s.H - 16) / ROW_H) - 1));
      var extra = Math.max(0, items.length - capRows * 2);
      items = items.slice(0, capRows * 2);

      // Row text + width
      items.forEach(function (p) {
        var o = p.o, main = o.common ? o.text + "  " + o.common : o.text;
        if (!p.target && o.kind === "dso" && Plan.match(o.text, p.x, p.y)) main = "✓ " + main;
        p.main = main; p.sub = o.detail || "";
        p.w = Math.min(COL_W_MAX, tw(FONT.row, main) + (p.sub ? 10 + tw(FONT.rowDim, p.sub) : 0));
      });

      var left = [], right = [];
      items.forEach(function (p) { (p.x < L.x ? left : right).push(p); });
      function colW(arr) { return arr.reduce(function (m, p) { return Math.max(m, p.w); }, 60); }
      var gap = R + 16;
      // Flip sides that would fall off-screen; rebalance overfull columns.
      if (L.x - gap - colW(left) < 4) { right = right.concat(left); left = []; }
      if (L.x + gap + colW(right) > s.W - 4) { left = left.concat(right); right = []; }
      function rebalance(a, b) { while (a.length > capRows) b.push(a.pop()); }
      if (L.x - gap - colW(left.concat(right)) >= 4 && L.x + gap + colW(left.concat(right)) <= s.W - 4) {
        rebalance(left, right); rebalance(right, left);
      }
      var cols = [];
      [[left, -1], [right, 1]].forEach(function (pair) {
        var arr = pair[0], sd = pair[1];
        if (!arr.length) return;
        arr.sort(function (a, b) { return a.y - b.y; });        // match on-sky order
        var w = colW(arr), h = arr.length * ROW_H;
        var top = Math.min(Math.max(L.y - h / 2, 8), s.H - h - 8);
        var x0 = sd < 0 ? L.x - gap - w : L.x + gap;
        cols.push({ sd: sd, rows: arr, x0: x0, top: top, w: w, h: h,
                    box: [x0 - 8, top - 6, w + 16, h + 12] });
      });
      return { L: L, cols: cols, extra: extra, count: items.length };
    }

    // Per-group "in view" counts for the Layers panel (whether shown or not,
    // so a hidden group still tells you what you'd get by switching it on).
    var cntEls = {};
    document.querySelectorAll("[data-cnt]").forEach(function (el) { cntEls[el.getAttribute("data-cnt")] = el; });
    var lastCounts = "";
    function updateTypeCounts(s) {
      var c = { gal: 0, neb: 0, dark: 0, clu: 0, rem: 0 };
      s.dsos.forEach(function (p) { c[p.o.group]++; });
      var key = JSON.stringify(c);
      if (key === lastCounts) return;
      lastCounts = key;
      for (var g in c) if (cntEls[g]) cntEls[g].textContent = c[g] > 999 ? "999+" : c[g];
    }

    function rank(p) {
      if (p.target) return -999;
      if (p.planned) return -500;
      return p.o.kind === "dso" ? p.o.eff - 1.5 : p.o.mag;
    }

    /* --- main paint (cheap: runs on every mouse-move) --- */
    function paint() {
      var s = snap; if (!s) return;
      var W = s.W, H = s.H, now = performance.now();
      if (cv.width !== Math.round(W * DPR) || cv.height !== Math.round(H * DPR)) {
        cv.width = Math.round(W * DPR); cv.height = Math.round(H * DPR);
        cv.style.width = W + "px"; cv.style.height = H + "px";
      }
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
      ctx.clearRect(0, 0, W, H);
      Card.position();                          // card follows its object (and is a no-label zone)
      Plan.prep(s); Plan.draw(ctx);             // planned targets' ghost frames
      Composer.draw(ctx);                       // FOV frame sits under every label
      ctx.textBaseline = "top"; ctx.textAlign = "left"; ctx.globalAlpha = 1;
      fading = false;

      var boxes = [], placed = {};
      var noGo = panelRects();
      var ctag = Composer.tagBox(); if (ctag) noGo.push(ctag);   // rig tag on the live frame
      noGo = noGo.concat(Plan.tagBoxes());                        // planned-framing tags

      // 1. Lens claims its space first.
      var Lc = lensCentre(), lens = null;
      lastLens = null;
      if (Lc) {
        lens = layoutLens(s, Lc);
        lastLens = lens;
        noGo.push([Lc.x - LENS_R - 2, Lc.y - LENS_R - 2, 2 * LENS_R + 4, 2 * LENS_R + 4]);
        lens.cols.forEach(function (c) { noGo.push(c.box); });
      }
      function free(b) { return inView(b, W, H) && !overlaps(b, noGo, 0) && !overlaps(b, boxes, 3); }

      // 2. Target label — always first, never displaced by map labels.
      if (s.target) {
        var tPlan = Plan.match(TARGET.name, s.target[0], s.target[1]);
        var tt = (tPlan ? "✓ " : "") + TARGET.name, tw0 = tw(FONT.target, tt);
        var tslots = [[27, -7], [-27 - tw0, -7], [-tw0 / 2, 26], [-tw0 / 2, -40]];
        for (var i = 0; i < tslots.length; i++) {
          var tb = [s.target[0] + tslots[i][0], s.target[1] + tslots[i][1], tw0, 13];
          if (!inView(tb, W, H)) continue;
          if (i < tslots.length - 1 && overlaps(tb, noGo, 0)) continue;
          boxes.push(tb);
          ctx.fillStyle = T.targetBg; ctx.fillRect(tb[0] - 4, tb[1] - 2, tb[2] + 8, tb[3] + 4);
          haloText(tt, tb[0], tb[1], FONT.target, T.target, 1);
          break;
        }
      }
      // Reticle ring is a no-label zone for everything else.
      if (s.target) boxes.push([s.target[0] - 24, s.target[1] - 24, 48, 48]);

      // 3. RA/Dec grid markers (centre-aligned, fixed position).
      ctx.font = FONT.grid;
      s.grid.forEach(function (g) {
        var w = tw(FONT.grid, g.text), b = [g.x - w / 2, g.y - 6, w, 12];
        if (!free(b)) return;
        boxes.push(b);
        haloText(g.text, b[0], b[1], FONT.grid, T.conName, 1);
      });

      // 4. Constellation names (centre-aligned at their anchor).
      if (S.cons) {
        s.cons.slice().sort(function (a, b) { return a.rank - b.rank; }).forEach(function (c) {
          var f = FONT.con[c.rank - 1], w = tw(f, c.text), h = 14 - c.rank;
          var b = [c.x - w / 2, c.y - h / 2, w, h];
          if (!free(b)) return;
          boxes.push(b);
          var k = "c" + c.text; placed[k] = 1;
          haloText(c.text, b[0], b[1], f, T.conName, 0.85 * alphaFor(k, now));
        });
      }

      // 5. DSOs + stars, merged by priority, within the density limits.
      var boost = 2.5 * Math.log(Math.max(120 / Math.max(s.field, 0.5), 1)) / Math.LN10;
      // Below a 40° field the limits open up quickly, so at close zoom every
      // object competes for a label and only collisions decide (at the
      // engine's max zoom, ~12° field, nothing is held back by magnitude).
      var close = s.field < 40 ? (40 - s.field) / 28 : 0;
      var dens = DENSITY[S.density], dScale = [0.6, 1, 1.4][S.density];
      var starLim = dens.star + boost + close * 5 * dScale;
      var dsoLim = dens.dso + boost * 1.3 + close * 14 * dScale;
      var list = [];
      if (S.dsos) s.dsos.forEach(function (p) { if (DSO_ON[p.o.group] && p.o.eff <= dsoLim) list.push(p); });
      // Wide fields: proper names only (Vega, Deneb…). Zoomed in (< 40°):
      // Bayer/Flamsteed designations too (δ Cyg, 61 Cyg), brightest first.
      if (S.stars) s.stars.forEach(function (p) {
        if ((p.o.proper || (close > 0 && p.o.classic)) && p.o.mag <= starLim) list.push(p);
      });
      // Planned targets: their map object gets a ✓ label that always wins;
      // plans with no visible map object (custom fields) get their own.
      var matched = {};
      s.dsos.forEach(function (p) { p.planned = false; });
      list.forEach(function (p) {
        if (p.o.kind !== "dso") return;
        var m = Plan.match(p.o.text, p.x, p.y);
        if (m) { p.planned = true; matched[m.key] = 1; }
      });
      s.dsos.forEach(function (p) {            // planned but below the label limits
        if (p.planned || !DSO_ON[p.o.group]) return;
        var m = Plan.match(p.o.text, p.x, p.y);
        if (m && !matched[m.key]) { p.planned = true; matched[m.key] = 1; list.push(p); }
      });
      Plan.points().forEach(function (m) {
        if (matched[m.key] || compactId(m.it.id) === compactId(TARGET.name)) return;
        list.push({ planned: true, x: m.x, y: m.y,
                    o: { k: "p" + m.key, kind: "plan", text: m.it.id } });
      });
      list.sort(function (a, b) { return rank(a) - rank(b); });

      list.forEach(function (p) {
        var o = p.o, font = o.kind === "star" ? FONT.star : FONT.dso;
        var text = p.planned ? "✓ " + o.text : o.text;
        var w = tw(font, text), h = 11, r = o.kind === "star" ? 3 : 5;
        var slots = [[r + 2, -r - h], [r + 2, r], [-r - 2 - w, -r - h], [-r - 2 - w, r]];
        var start = lastSlot[o.k] || 0;
        for (var n = 0; n < 4; n++) {
          var si = (start + n) % 4;
          var b = [p.x + slots[si][0], p.y + slots[si][1], w, h];
          if (!free(b)) continue;
          boxes.push(b); lastSlot[o.k] = si; placed[o.k] = 1;
          haloText(text, b[0], b[1], font,
                   p.planned ? T.planned : o.kind === "star" ? T.starName : T.dsoName,
                   alphaFor(o.k, now));
          break;
        }
      });

      // Forget fade state for labels that dropped out (so they fade back in).
      for (var k in born) if (!placed[k]) delete born[k];

      // 6. The lens itself, drawn over everything.
      if (lens) drawLens(lens);

      ctx.globalAlpha = 1;
      if (fading) requestPaint();
    }

    function drawLens(lens) {
      var L = lens.L, R = LENS_R;
      ctx.globalAlpha = 1;
      // Ring (solid when pinned, dashed when following the mouse)
      ctx.strokeStyle = T.lens; ctx.lineWidth = pinned ? 1.6 : 1.2;
      ctx.setLineDash(pinned ? [] : [4, 3]);
      ctx.beginPath(); ctx.arc(L.x, L.y, R, 0, 2 * Math.PI); ctx.stroke();
      ctx.setLineDash([]);

      lens.cols.forEach(function (c) {
        // Leader lines first, so the panel sits on top of their ends.
        c.rows.forEach(function (p, i) {
          var ly = c.top + i * ROW_H + ROW_H / 2;
          var ax = c.sd < 0 ? c.x0 + c.w + 8 : c.x0 - 8;
          var a = Math.atan2(ly - L.y, ax - L.x);
          var rx = L.x + Math.cos(a) * R, ry = L.y + Math.sin(a) * R;
          ctx.strokeStyle = T.leader; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(rx, ry); ctx.lineTo(ax, ly); ctx.stroke();
          ctx.fillStyle = p.target ? T.target : (p.o.kind === "dso" ? T.dsoName : T.starName);
          ctx.beginPath(); ctx.arc(p.x, p.y, 2, 0, 2 * Math.PI); ctx.fill();
        });
        // Glass panel
        var b = c.box;
        ctx.fillStyle = T.panel; ctx.strokeStyle = T.panelEdge; ctx.lineWidth = 1;
        roundRect(b[0], b[1], b[2], b[3], 8); ctx.fill(); ctx.stroke();
        // Rows — name (+ common name) bright, details dim; clipped to column.
        ctx.save();
        ctx.beginPath(); ctx.rect(c.x0 - 2, c.top - 4, c.w + 4, c.h + 8); ctx.clip();
        ctx.textBaseline = "middle";
        c.rows.forEach(function (p, i) {
          var ly = c.top + i * ROW_H + ROW_H / 2;
          var x = c.sd < 0 ? c.x0 + c.w - p.w : c.x0;      // left column is right-aligned
          ctx.font = FONT.row;
          ctx.fillStyle = p.target ? T.target : (p.o.kind === "dso" ? T.dsoName : T.starName);
          ctx.fillText(p.main, x, ly);
          if (p.sub) {
            ctx.font = FONT.rowDim; ctx.fillStyle = T.detail;
            ctx.fillText(p.sub, x + tw(FONT.row, p.main) + 10, ly);
          }
        });
        ctx.restore();
        ctx.textBaseline = "top";
      });

      // Footer chip: overflow count / pinned state / empty lens
      var note = [];
      if (!lens.count) note.push("nothing labelled here");
      if (lens.extra) note.push("+" + lens.extra + " more — zoom in");
      if (pinned) note.push("pinned · Esc");
      if (note.length) {
        var txt = note.join("  ·  "), w = tw(FONT.rowDim, txt) + 16;
        var y = Math.min(L.y + R + 8, snap.H - 22);
        var cx = Math.min(Math.max(L.x, w / 2 + 4), snap.W - w / 2 - 4);   // keep on-screen
        ctx.fillStyle = T.panel; ctx.strokeStyle = T.panelEdge;
        roundRect(cx - w / 2, y, w, 16, 8); ctx.fill(); ctx.stroke();
        ctx.font = FONT.rowDim; ctx.fillStyle = T.detail; ctx.textAlign = "center";
        ctx.fillText(txt, cx, y + 3); ctx.textAlign = "left";
      }
    }

    function roundRect(x, y, w, h, r) {
      ctx.beginPath();
      ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r);
      ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r);
      ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
    }

    var rafId = 0;
    function requestPaint() {
      if (rafId) return;
      rafId = requestAnimationFrame(function () { rafId = 0; paint(); });
    }

    /* --- mouse: hover lens, click to pin (drag pans, dbl-click zooms) --- */
    var down = null, clickTimer = 0, lastLens = null;
    function local(e) {
      var r = mapWrap.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    }
    function overSky(e) { return e.target && e.target.tagName === "CANVAS"; }

    mapWrap.addEventListener("mousemove", function (e) {
      mouse = overSky(e) ? local(e) : null;
      if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) > 4) dragging = true;
      if (S.lens) requestPaint();
    }, true);
    mapWrap.addEventListener("mouseleave", function () { mouse = null; requestPaint(); });
    mapWrap.addEventListener("mousedown", function (e) {
      if (!overSky(e) || e.button !== 0) return;    // right-click has its own menu
      down = { x: e.clientX, y: e.clientY }; dragging = false;
    }, true);
    window.addEventListener("mouseup", function (e) {
      var wasClick = down && !dragging && overSky(e);
      down = null;
      if (dragging) { dragging = false; requestPaint(); return; }
      if (!wasClick) return;
      clickAt(local(e));
    }, true);

    // Click routing: pinned-lens row → card; object → card; otherwise close
    // an open card, or pin / release the lens (as before).
    function clickAt(at) {
      clearTimeout(clickTimer);                // wait out a double-click (engine zooms)
      clickTimer = setTimeout(function () {
        var o = lensRowAt(at) || objectAt(at);
        if (o) { Card.open(o); requestPaint(); return; }
        if (Card.isOpen()) { Card.close(); return; }
        if (!S.lens) return;
        if (pinned) { pinned = null; mouse = at; }
        else pinAt(at);
        requestPaint();
      }, 260);
    }
    function pinAt(at) {
      if (!at) { pinned = null; requestPaint(); return; }
      if (!S.lens) setLens(true);
      var sky = Celestial.mapProjection.invert([at.x, at.y]);
      if (sky && isFinite(sky[0])) pinned = sky;
      requestPaint();
    }
    function asObj(p) {
      if (p.target) return { kind: "target", id: TARGET.name, ra: TARGET.ra, dec: TARGET.dec,
                             common: TARGET.common };
      if (p.o.kind !== "dso") return null;
      return { kind: "dso", id: p.o.text, ra: lonToRa0(p.o.c[0]), dec: p.o.c[1],
               type: p.o.p.type || "", dim: p.o.p.dim || "", common: p.o.common || "",
               detail: p.o.detail || "" };
    }
    // The deep-sky object (or target, or planned custom field) under a point.
    function objectAt(at) {
      var s = snap; if (!s) return null;
      if (s.target && Math.hypot(s.target[0] - at.x, s.target[1] - at.y) < 12)
        return asObj({ target: true });
      var best = null, bd = 9;
      s.dsos.forEach(function (p) {
        if (!DSO_ON[p.o.group]) return;
        var d = Math.hypot(p.x - at.x, p.y - at.y);
        if (d < bd) { bd = d; best = p; }
      });
      if (best) return asObj(best);
      var pl = null;
      Plan.points().forEach(function (m) {
        var d = Math.hypot(m.x - at.x, m.y - at.y);
        if (d < bd) { bd = d; pl = m; }
      });
      return pl ? { kind: "dso", id: pl.it.id, ra: pl.it.ra, dec: pl.it.dec } : null;
    }
    function lensRowAt(at) {
      if (!pinned || !lastLens) return null;
      for (var ci = 0; ci < lastLens.cols.length; ci++) {
        var c = lastLens.cols[ci];
        if (at.x < c.x0 - 8 || at.x > c.x0 + c.w + 8) continue;
        var i = Math.floor((at.y - c.top) / ROW_H);
        if (i >= 0 && i < c.rows.length) return asObj(c.rows[i]);
      }
      return null;
    }
    mapWrap.addEventListener("dblclick", function () { clearTimeout(clickTimer); }, true);
    window.addEventListener("keydown", function (e) {
      if (e.target && /INPUT|TEXTAREA/.test(e.target.tagName)) return;   // typing a name
      if (e.key === "Escape" && Card.isOpen()) Card.close();
      else if (e.key === "Escape" && pinned) { pinned = null; requestPaint(); }
      else if ((e.key === "l" || e.key === "L") && !e.ctrlKey && !e.metaKey) setLens(!S.lens);
    });

    /* --- HUD --- */
    var hud = document.getElementById("labelhud");
    function syncHud() {
      hud.querySelectorAll("[data-density]").forEach(function (c) {
        c.classList.toggle("on", +c.getAttribute("data-density") === S.density);
      });
      hud.querySelectorAll("[data-label]").forEach(function (c) {
        c.classList.toggle("on", !!S[c.getAttribute("data-label")]);
      });
    }
    function setLens(on) { S.lens = on; if (!on) pinned = null; syncHud(); requestPaint(); savePrefs(); }
    hud.addEventListener("click", function (e) {
      var c = e.target.closest(".chip"); if (!c) return;
      if (c.hasAttribute("data-density")) S.density = +c.getAttribute("data-density");
      else {
        var key = c.getAttribute("data-label");
        if (key === "lens") return setLens(!S.lens);
        S[key] = !S[key];
      }
      syncHud(); requestPaint(); savePrefs();
    });
    (function () {                                  // remembered label settings
      var lp = PREFS.labels || {};
      ["stars", "dsos", "lens"].forEach(function (k) { if (typeof lp[k] === "boolean") S[k] = lp[k]; });
      if (lp.density === 0 || lp.density === 1 || lp.density === 2) S.density = lp.density;
    })();
    syncHud();

    return {
      onSkyRedraw: function () {
        if (!Celestial.container || !Celestial.mapProjection) return;
        try { snap = takeSnapshot(); } catch (e) { console.warn("label snapshot skipped:", e); return; }
        updateTypeCounts(snap);
        requestPaint();
      },
      setConstellationNames: function (on) { S.cons = on; requestPaint(); },
      getPrefs: function () { return { stars: S.stars, dsos: S.dsos, lens: S.lens, density: S.density }; },
      repaint: requestPaint,
      getSnap: function () { return snap; },
      objectAt: objectAt,
      clickAt: clickAt,
      pinAt: pinAt,
      isPinned: function () { return !!pinned; }
    };
  })();

  /* ---------- 8. Control wiring ---------------------------------- */
  // Layer toggles (star / deep-sky labels are in the Labels HUD, 7b).
  var LAYER_APPLY = {
    conlines: function (on) { return { constellations: { lines: on } }; },
    mw:       function (on) { return { mw: { show: on } }; },
    bounds:   function (on) { return { constellations: { boundStyle: { stroke: T.bounds, width: 0.6, opacity: on ? 0.7 : 0, dash: [2, 4] } } }; }
  };
  document.querySelectorAll(".row[data-layer]").forEach(function (row) {
    row.addEventListener("click", function () {
      var on = !row.classList.contains("on");
      row.classList.toggle("on", on);
      row.classList.toggle("off", !on);
      var layer = row.getAttribute("data-layer");
      if (layer === "grid") { GRIDON = on; Celestial.redraw(); }
      else if (layer === "connames") { Labels.setConstellationNames(on); }
      else if (LAYER_APPLY[layer]) { Celestial.apply(LAYER_APPLY[layer](on)); }
      savePrefs();
    });
  });

  // Deep-sky type rows
  document.querySelectorAll("[data-dso]").forEach(function (row) {
    row.addEventListener("click", function () {
      var g = row.getAttribute("data-dso"), on = !DSO_ON[g];
      DSO_ON[g] = on;
      row.classList.toggle("on", on);
      row.classList.toggle("off", !on);
      Celestial.apply({ dsos: { symbols: dsoSymbols() } });   // redraws → labels follow
      savePrefs();
    });
  });

  // Magnitude slider — its range follows the active catalog tier.
  var magSlider = document.getElementById("mag-slider");
  var magLbl = document.getElementById("mag-lbl");
  magSlider.max = CATALOG.maglimit;
  magSlider.value = INIT_MAG;
  magLbl.textContent = "Mag ≤ " + INIT_MAG.toFixed(1);
  magSlider.addEventListener("input", function () {
    var v = parseFloat(magSlider.value);
    magLbl.textContent = "Mag ≤ " + v.toFixed(1);
    Celestial.apply({ stars: { limit: v } });
  });
  magSlider.addEventListener("change", savePrefs);

  // Remembered Layers / deep-sky / magnitude settings → the panel, before the
  // first display (reapplyUserState() reads the panel when the map builds).
  (function applyPrefs() {
    var L = PREFS.layers || {}, D = PREFS.dso || {};
    document.querySelectorAll(".row[data-layer]").forEach(function (row) {
      var k = row.getAttribute("data-layer");
      if (typeof L[k] !== "boolean") return;
      row.classList.toggle("on", L[k]); row.classList.toggle("off", !L[k]);
      if (k === "grid") GRIDON = L[k];
      else if (k === "connames") Labels.setConstellationNames(L[k]);
    });
    document.querySelectorAll("[data-dso]").forEach(function (row) {
      var g = row.getAttribute("data-dso");
      if (typeof D[g] !== "boolean") return;
      DSO_ON[g] = D[g];
      row.classList.toggle("on", D[g]); row.classList.toggle("off", !D[g]);
    });
    var m = parseFloat(PREFS.mag);
    if (isFinite(m)) {
      m = Math.max(parseFloat(magSlider.min) || 3, Math.min(CATALOG.maglimit, m));
      magSlider.value = m; magLbl.textContent = "Mag ≤ " + m.toFixed(1);
      config.stars.limit = m;
    }
    prefsReady = true;
  })();

  // Field-of-view zoom
  document.getElementById("fov-in").addEventListener("click", function () {
    Celestial.zoomBy(1.3); Celestial.redraw();
  });
  document.getElementById("fov-out").addEventListener("click", function () {
    Celestial.zoomBy(1 / 1.3); Celestial.redraw();
  });

  // Recenter
  document.getElementById("recenter").addEventListener("click", function () {
    Celestial.rotate({ center: [LON, TARGET.dec, 0] });
    Celestial.redraw();
  });

  // Title-bar: target identity + theme toggle
  document.getElementById("tb-target").textContent = TARGET.name;
  if (TARGET.common) document.getElementById("tb-sub").textContent = "· " + TARGET.common;
  var tb = document.getElementById("tb-badge");
  if (TARGET.catalog) { tb.textContent = TARGET.catalog; tb.style.display = "inline"; }

  document.getElementById("tb-theme").addEventListener("click", function () {
    // Test-only theme flip: simplest reliable path is a full reload with the
    // other theme, since several colours live in the (reload-only) config.
    var next = (themeName === "day") ? "night" : "day";
    q.set("theme", next);
    window.location.search = q.toString();
  });

  /* ---------- 9. Go --------------------------------------------- */
  applyChromeTheme();
  // d3-celestial's legacy-option normalizer forces constellation names on
  // (and remaps star name flags) on every display/reload, so switch ALL
  // engine text off explicitly — the label engine (7b) draws every label.
  // Per-type DSO symbol styles: the engine's defaults, with opacity set to
  // 0 for hidden groups.  (d3-celestial replaces dsos.symbols wholesale on
  // apply(), so the full table is rebuilt from the pristine defaults.)
  var BASE_SYMBOLS = null;
  function dsoSymbols() {
    if (!BASE_SYMBOLS) BASE_SYMBOLS = JSON.parse(JSON.stringify(Celestial.settings().dsos.symbols));
    var out = {};
    for (var t in BASE_SYMBOLS) {
      out[t] = Object.assign({}, BASE_SYMBOLS[t], { opacity: DSO_ON[dsoGroup(t)] ? 1 : 0 });
    }
    return out;
  }
  function engineTextOff() {
    Celestial.apply({
      constellations: { names: false },
      dsos: { names: false, symbols: dsoSymbols() },
      stars: { designation: false, propername: false }
    });
  }
  // Re-apply what the user has set in the Layers panel / mag slider — a
  // (re)display starts from `config`, which doesn't track those.
  function reapplyUserState() {
    document.querySelectorAll(".row[data-layer]").forEach(function (row) {
      var layer = row.getAttribute("data-layer");
      if (LAYER_APPLY[layer]) Celestial.apply(LAYER_APPLY[layer](row.classList.contains("on")));
    });
    Celestial.apply({ stars: { limit: parseFloat(magSlider.value) } });
    engineTextOff();              // also applies the deep-sky type filter
  }

  // Build the map at the map area's real size.  d3-celestial fixes its
  // canvas size, aspect ratio — and, if the area measures 0 wide at that
  // moment, PINS #celestial-map's width — when display() runs.  On Windows
  // the WebView2 window is created and sized in steps, so the page often
  // starts in a 0×0 or tiny viewport: the first map came up empty and only a
  // second open (window already sized) showed stars and DSOs.  So: wait for
  // a usable, settled size (bounded), and on any later size change rebuild
  // at the new size, keeping the view's centre and zoom.
  var displayed = false, lastW = 0, lastH = 0, rebuilds = 0, lastBuild = 0;
  function show(keepView) {
    var el = document.getElementById("celestial-map");
    el.style.width = ""; el.style.height = "";        // drop any pinned size
    var w = mapWrap.clientWidth, h = mapWrap.clientHeight;
    if (!w || !h) { w = 800; h = 500; }
    config.projectionRatio = w / h;
    var zoomTo = 1;
    if (keepView && displayed) {
      var rot = Celestial.mapProjection.rotate();
      config.center = [-rot[0], -rot[1], rot[2]];
      zoomTo = Celestial.zoomBy() || 1;                  // current zoom vs. full view
    }
    lastW = w; lastH = h; lastBuild = Date.now();
    if (keepView) rebuilds++;
    Celestial.display(config);
    // The engine's own window-resize handler only rescales the canvas
    // width (keeping the old aspect ratio and resetting the zoom); the
    // rebuild below replaces it.
    if (window.d3) d3.select(window).on("resize", null);
    displayed = true;
    reapplyUserState();
    if (zoomTo > 1.01) Celestial.zoomBy(zoomTo);        // back to the user's zoom
  }

  var t0 = Date.now(), pw = -1, ph = -1;
  (function waitForSize() {
    var w = mapWrap.clientWidth, h = mapWrap.clientHeight;
    var settled = w >= 300 && h >= 200 && w === pw && h === ph;
    pw = w; ph = h;
    if (settled || Date.now() - t0 > 4000) {
      show(false);
      Plan.refresh();             // ✓ markers for what's already in Tonight's Plan
      var waited = Date.now() - t0;
      setTimeout(function () {    // one summary line per open, for skymap.log
        var sel = function (c) { try { return Celestial.container.selectAll("." + c).size(); } catch (e) { return -1; } };
        diag("opened: " + sel("star") + " stars (" + CATALOG.stars + "), " + sel("dso") + " DSOs (" + CATALOG.dsos +
             "), map " + mapWrap.clientWidth + "×" + mapWrap.clientHeight + " @" + (window.devicePixelRatio || 1) +
             "x, waited " + waited + " ms, " + rebuilds + " rebuild(s)");
      }, 8000);
      return;
    }
    setTimeout(waitForSize, 120);
  })();

  // Rebuild on size change (debounced).  Also catches the WebView2 window
  // being resized/maximised by the host right after the page loaded.
  var rt;
  function onResize() {
    if (!displayed) return;
    clearTimeout(rt);
    rt = setTimeout(function () {
      var w = mapWrap.clientWidth, h = mapWrap.clientHeight;
      if (!(w && h && (Math.abs(w - lastW) > 2 || Math.abs(h - lastH) > 2))) return;
      // A rebuild re-reads every catalog file; never more than once every
      // 1.5 s, so a window that keeps resizing can't starve the loads.
      var wait = 1500 - (Date.now() - lastBuild);
      if (wait > 0) { rt = setTimeout(onResizeNow, wait); return; }
      show(true);
    }, 250);
  }
  function onResizeNow() { lastBuild = 0; clearTimeout(rt); rt = null; onResize(); }
  window.addEventListener("resize", onResize);
  if (window.ResizeObserver) new ResizeObserver(onResize).observe(mapWrap);

})();
