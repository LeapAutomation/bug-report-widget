/* bug-report.js — portable in-app bug reporting widget.
 *
 * Drop:   <script src="html2canvas.min.js" defer></script>
 *         <script src="bug-report.js" defer></script>
 * Result: floating 🐛 button bottom-right, captures viewport on tap,
 *         shows a modal with preview + textarea, POSTs to /api/bugs.
 *
 * Configure by setting window.BugReportConfig BEFORE this script loads:
 *   window.BugReportConfig = {
 *     endpoint: "/api/bugs",            // default
 *     idPrefix: "bug-report",           // collision-proof your DOM
 *     buildSha: "abc1234",              // or () => string
 *     theme: { accent: "#007AFF", ... } // override CSS tokens
 *     position: { bottom: 20, right: 20 } // px, before user drag; {top, left} works too
 *     // 1.6.0, all optional (see the README): submitTimeoutMs, slowNoticeMs,
 *     // idempotentSubmit, onClientEvent, html2canvasUrl, reachOverDialogs
 *   };
 *
 * UMD: also exports `BugReportWidget` on `window` for tests + programmatic
 * use. In Node (no DOM), exports `createController` etc. for unit testing
 * the pure pieces.
 *
 * Distilled from leap-timesheet's lib/bug-report.js + claude-tmux-dashboard's
 * static/bug-report.js. See bug-report-pattern.md for the design rationale.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    var api = factory();
    root.BugReportWidget = api;
    if (typeof window !== "undefined" && typeof document !== "undefined") {
      if (!window.__bugReportSkipAutoInit) api.init();
    }
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // The screenshot viewer's DOM id. NOT derived from idPrefix: apps' own tests
  // select it (`#bug-report-viewer-box` and friends), so it stays put.
  var VIEWER_ID = "bug-report-viewer";

  var DEFAULTS = {
    endpoint: "/api/bugs",
    idPrefix: "bug-report",
    buildSha: "",
    // Either axis can be given from either edge: {bottom, right} (default),
    // {top, left}, or a mix. Naming `top` drops the default `bottom`, and
    // naming `left` drops the default `right`.
    position: { bottom: 20, right: 20 },
    // Diameter of the floating button, px. Host apps embedded inside another
    // shell often want a smaller footprint so it reads as chrome rather than
    // page content. The glyph scales with it.
    buttonSize: 52,
    // Heading of the report modal. An app whose queue takes more than bugs
    // (feature requests, "the numbers look wrong") can say so.
    title: "Report a bug",
    captureTimeoutMs: 6000,
    // Click the screenshot preview to zoom in and mark it up (pen, box) before
    // filing. The marked-up image is what is sent.
    markup: true,
    // Decoded-byte ceiling for the screenshot payload. Mirrors the reference
    // backend's server-side cap (which stays authoritative — never trust the
    // client). The client cap exists so a capture that would be rejected is
    // re-encoded smaller (or dropped) instead of dead-ending the report in a
    // 413 the user can't do anything about.
    maxScreenshotBytes: 5 * 1024 * 1024,
    storageKey: "bug-report-button-position-v2",
    // The POST is bounded: a submit that never answers ends in a message and a
    // working Submit button, not a dialog frozen on "Submitting…".
    submitTimeoutMs: 30000,
    // After this long without an answer the status line says it is still going.
    slowNoticeMs: 8000,
    // Send a per-open-modal `clientKey` (UUID) so a server that dedupes on it
    // returns the existing row when the same report is retried. OFF by default:
    // a server that ignores the key (the reference backends do) would file the
    // report twice, so the timeout message may only promise "it won't be filed
    // twice" when the app says its server honours the key.
    idempotentSubmit: false,
    // (kind, detail) => void, for apps that log client-side trouble. Kinds:
    // "screenshot-failure" and "submit-timeout". It can never break the widget.
    onClientEvent: null,
    // If set, and window.html2canvas is absent when a screenshot is needed,
    // the script is injected on the first click and awaited (inside
    // captureTimeoutMs). Empty = never inject (the 1.5.0 behaviour).
    html2canvasUrl: "",
    // After a capture times out, keep watching this long for the render to
    // finish, so "screenshot-failure" can say how long it really took.
    captureLateWatchMs: 30000,
    // A modal <dialog>.showModal() makes the rest of the page inert, which
    // would leave the button, report form and viewer unclickable. When true the
    // widget moves them into the topmost open modal dialog, and back to <body>
    // when it closes. Set false to opt out.
    reachOverDialogs: true,
    theme: {
      accent: "#007AFF",
      buttonBg: "#ffffff",
      buttonInk: "#1a1a1e",
      modalBg: "#ffffff",
      modalInk: "#1a1a1e",
      mutedInk: "#666666",
      errorInk: "#c0392b",
      toastBg: "rgba(40,40,42,0.92)",
      toastInk: "#ffffff",
      font: '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", system-ui, sans-serif',
      radius: "12px",
    },
  };

  // A position from the app replaces the default on each axis it names: {top}
  // must not also keep the default bottom (both set would stretch the button).
  function mergePosition(user) {
    user = user || {};
    var d = DEFAULTS.position;
    var out = {};
    var hasV = user.top != null || user.bottom != null;
    var hasH = user.left != null || user.right != null;
    if (!hasV) out.bottom = d.bottom;
    if (!hasH) out.right = d.right;
    for (var k in user) if (user[k] != null) out[k] = user[k];
    return out;
  }

  // The CSS for the button's starting corner. Top wins over bottom, and left
  // over right, if both are (oddly) given.
  function positionCss(pos) {
    var out = {};
    function inset(edge, n) {
      return "calc(env(safe-area-inset-" + edge + ", 0px) + " + (Number(n) || 0) + "px)";
    }
    if (pos.top != null) out.top = inset("top", pos.top);
    else out.bottom = inset("bottom", pos.bottom != null ? pos.bottom : DEFAULTS.position.bottom);
    if (pos.left != null) out.left = inset("left", pos.left);
    else out.right = inset("right", pos.right != null ? pos.right : DEFAULTS.position.right);
    return out;
  }

  function mergeConfig(user) {
    user = user || {};
    var out = {};
    for (var k in DEFAULTS) out[k] = DEFAULTS[k];
    for (var k2 in user) {
      if (k2 === "theme") {
        var merged = {};
        for (var t in DEFAULTS[k2]) merged[t] = DEFAULTS[k2][t];
        for (var t2 in user[k2]) merged[t2] = user[k2][t2];
        out[k2] = merged;
      } else if (k2 === "position") {
        out[k2] = mergePosition(user[k2]);
      } else {
        out[k2] = user[k2];
      }
    }
    // A timeout of 0, NaN or undefined (an unset variable) would abort every
    // submit at once, so such values mean "the default", not "no timeout".
    ["submitTimeoutMs", "slowNoticeMs"].forEach(function (key) {
      var n = Number(out[key]);
      if (!(n > 0) || !isFinite(n)) out[key] = DEFAULTS[key];
    });
    return out;
  }

  // ─── Pure helpers (testable in Node) ───────────────────────────────

  // "ok" | "blank" | "tainted". Blank = all transparent / all white. Safari
  // edge case: html2canvas occasionally returns a canvas with no pixel data.
  // "tainted" = reading the pixels threw a SecurityError (a cross-origin image
  // was drawn without CORS), which is a different fix from "blank". Density of
  // 8×8 (64 samples) reliably finds at least one non-white pixel on any page
  // with real content; 4×4 sometimes false-negatives on wide layouts with
  // white cards (bug-20260507-191820).
  function canvasState(canvas) {
    if (!canvas || !canvas.getContext) return "blank";
    if (!canvas.width || !canvas.height) return "blank";
    var ctx;
    try { ctx = canvas.getContext("2d"); } catch (e) { return e && e.name === "SecurityError" ? "tainted" : "blank"; }
    if (!ctx) return "blank";
    try {
      var w = canvas.width, h = canvas.height;
      var n = 8;
      for (var x = 0; x < n; x++) {
        for (var y = 0; y < n; y++) {
          var px = Math.max(0, Math.min(w - 1, Math.floor((x + 0.5) * w / n)));
          var py = Math.max(0, Math.min(h - 1, Math.floor((y + 0.5) * h / n)));
          var data = ctx.getImageData(px, py, 1, 1).data;
          if (data[3] > 0 && !(data[0] === 255 && data[1] === 255 && data[2] === 255)) {
            return "ok";
          }
        }
      }
      return "blank";
    } catch (e) {
      return e && e.name === "SecurityError" ? "tainted" : "blank";
    }
  }

  // True if a canvas appears blank (or unreadable). Kept as the 1.5.0 boolean.
  function isBlankCanvas(canvas) {
    return canvasState(canvas) !== "ok";
  }

  // Decoded byte size of a data URL's base64 payload — i.e. what a server
  // that base64-decodes before checking its cap will measure.
  function dataUrlBytes(dataUrl) {
    if (typeof dataUrl !== "string") return 0;
    var i = dataUrl.indexOf(",");
    var b64 = i >= 0 ? dataUrl.slice(i + 1) : dataUrl;
    var pad = 0;
    if (b64.slice(-2) === "==") pad = 2;
    else if (b64.slice(-1) === "=") pad = 1;
    return Math.max(0, Math.floor((b64.length * 3) / 4) - pad);
  }

  // Human-sized bytes for the submit status line. A multi-megabyte screenshot
  // is the usual reason a submit takes seconds, and a user who can see that is
  // not watching a frozen dialog.
  function formatBytes(n) {
    if (!n || n < 1024) return (n || 0) + " B";
    if (n < 1024 * 1024) return Math.round(n / 1024) + " KB";
    return (Math.round((n / (1024 * 1024)) * 10) / 10) + " MB";
  }

  // Idempotency key for one report. Hex/dashes only, so it is safe as a
  // storage key on the server.
  function newClientKey(win) {
    var c = win && win.crypto;
    try {
      if (c && typeof c.randomUUID === "function") return c.randomUUID();
      if (c && typeof c.getRandomValues === "function") {
        var a = new Uint8Array(16);
        c.getRandomValues(a);
        var out = "";
        for (var i = 0; i < a.length; i++) out += ("0" + a[i].toString(16)).slice(-2);
        return out;
      }
    } catch (e) { /* fall through to the non-crypto path */ }
    return "k" + Date.now().toString(16) + Math.random().toString(16).slice(2, 10);
  }

  // Serialise a canvas to a data URL whose DECODED size is ≤ maxBytes.
  // Ladder: PNG (lossless) → JPEG 0.85 → JPEG 0.6 → half-resolution JPEG
  // 0.6 → null. A null means the report goes out without a screenshot,
  // which beats a 413 the user can't recover from.
  //
  // JPEG has no alpha channel and browsers composite transparent pixels
  // onto BLACK, so the canvas is flattened onto white before any JPEG
  // encode. If flattening fails (no createElement / getContext in exotic
  // hosts) the unflattened canvas is used — a dark screenshot still beats
  // no screenshot.
  //
  // `diag` (optional) is filled when an encode THREW, so a caller can tell
  // "could not encode" (diag.error; diag.tainted for a SecurityError) from
  // "nothing fitted under the cap". Both return null.
  function encodeCanvasUnderCap(canvas, maxBytes, doc, diag) {
    function fits(url) { return url && dataUrlBytes(url) <= maxBytes ? url : null; }
    function threw(e) {
      if (diag) {
        diag.error = (e && ((e.name ? e.name + ": " : "") + (e.message || ""))) || String(e);
        diag.tainted = !!(e && e.name === "SecurityError");
      }
      return null;
    }
    try {
      var png = fits(canvas.toDataURL("image/png"));
      if (png) return png;
    } catch (e) { return threw(e); }
    var flat = canvas;
    try {
      var f = doc.createElement("canvas");
      f.width = canvas.width; f.height = canvas.height;
      var fctx = f.getContext("2d");
      fctx.fillStyle = "#ffffff";
      fctx.fillRect(0, 0, f.width, f.height);
      fctx.drawImage(canvas, 0, 0);
      flat = f;
    } catch (e) { /* fall through with the unflattened canvas */ }
    var qualities = [0.85, 0.6];
    for (var i = 0; i < qualities.length; i++) {
      try {
        var jpg = fits(flat.toDataURL("image/jpeg", qualities[i]));
        if (jpg) return jpg;
      } catch (e) { return threw(e); }
    }
    try {
      var h = doc.createElement("canvas");
      h.width = Math.max(1, Math.round(flat.width / 2));
      h.height = Math.max(1, Math.round(flat.height / 2));
      var hctx = h.getContext("2d");
      hctx.fillStyle = "#ffffff";
      hctx.fillRect(0, 0, h.width, h.height);
      hctx.drawImage(flat, 0, 0, h.width, h.height);
      return fits(h.toDataURL("image/jpeg", 0.6));
    } catch (e) { return threw(e); }
  }

  // Keep a {left, top} button position inside a vw × vh viewport, with a 4px
  // margin. The drag handler always clamped, but a RESTORED position was
  // applied as stored — so a button dragged near the right or bottom edge of a
  // large window came back off-screen on a smaller one (another browser
  // window, a laptop after an external monitor, a phone), and the widget
  // looked permanently missing until someone cleared localStorage by hand.
  // Unknown geometry (a headless host reporting 0) leaves the position alone.
  function clampPos(pos, vw, vh, w, h) {
    var MARGIN = 4;
    var left = pos.left, top = pos.top;
    if (vw > 0) left = Math.max(MARGIN, Math.min(vw - w - MARGIN, left));
    if (vh > 0) top = Math.max(MARGIN, Math.min(vh - h - MARGIN, top));
    return { left: left, top: top };
  }

  function buildPostBody(opts) {
    var body = {
      title: opts.title || (opts.details || "").split("\n")[0].slice(0, 100),
      details: String(opts.details || "").slice(0, 10 * 1024),
      screenshot: opts.screenshot || null,
      metaUrl: opts.metaUrl || "",
      metaUserAgent: opts.metaUserAgent || "",
      metaBuildSha: opts.metaBuildSha || "",
      tags: Array.isArray(opts.tags) ? opts.tags : ["bug"],
      addedBy: opts.addedBy || "web",
    };
    // Idempotency key: one per open modal, unchanged across retries of that
    // same report. A server that honours it returns the row it already has
    // rather than filing a second one, which is what makes "Submit again"
    // safe after a submit that was never answered. Sent only when asked for.
    if (opts.clientKey) body.clientKey = String(opts.clientKey).slice(0, 64);
    // Why there is no screenshot, or why part of it is blank (1.6.4).
    if (opts.screenshotError) body.screenshotError = String(opts.screenshotError).slice(0, 200);
    return body;
  }

  // ─── Modern colour functions ───────────────────────────────────────

  // html2canvas 1.4.1 parses only rgb(a)/hsl(a) colours and throws on anything
  // newer, failing the whole capture: "Attempting to parse an unsupported color
  // function "color"". Browsers report a `color-mix()` result (and wide-gamut
  // colours) as `color(srgb …)` in computed styles, so one badge tinted with
  // color-mix() was enough (PlacementImageViewer bug-20261012-185703; the seal
  // viewer went weeks with no screenshots for the same reason). Before
  // html2canvas reads the cloned page, every such value is rewritten as the
  // rgba() the browser itself resolves it to (html2canvas has already copied
  // ::before/::after into real elements by then, so they are reached too).
  var MODERN_COLOR = /\b(?:color|color-mix|oklch|oklab|lab|lch|hwb)\((?:[^()]|\([^()]*\))*\)/gi;
  var COLOR_PROPS = ["color", "background-color", "background-image",
    "border-top-color", "border-right-color", "border-bottom-color", "border-left-color",
    "outline-color", "text-decoration-color", "box-shadow", "text-shadow", "fill", "stroke",
    "-webkit-text-stroke-color", "list-style-image"];

  // `value` with each modern colour function replaced by toRgba(it); one that
  // toRgba cannot resolve (null) is left as it was. Pure.
  function plainColors(value, toRgba) {
    if (typeof value !== "string" || value.search(MODERN_COLOR) === -1) return value;
    return value.replace(MODERN_COLOR, function (fn) { return toRgba(fn) || fn; });
  }

  // Any colour the browser understands, as rgba(), by painting it into one
  // canvas pixel and reading it back. null when it cannot.
  function canvasRgba(doc) {
    var ctx = null;
    try {
      var c = doc.createElement("canvas");
      c.width = c.height = 1;
      ctx = c.getContext("2d", { willReadFrequently: true });
    } catch (e) { /* no canvas */ }
    var memo = {};
    return function (fn) {
      if (!ctx) return null;
      if (Object.prototype.hasOwnProperty.call(memo, fn)) return memo[fn];
      var out = null;
      try {
        ctx.fillStyle = "rgba(1, 2, 3, 0.5)";
        var before = ctx.fillStyle;
        ctx.fillStyle = fn;
        if (ctx.fillStyle !== before) {     // an unparseable colour leaves it unchanged
          ctx.clearRect(0, 0, 1, 1);
          ctx.fillRect(0, 0, 1, 1);
          var d = ctx.getImageData(0, 0, 1, 1).data;
          out = "rgba(" + d[0] + ", " + d[1] + ", " + d[2] + ", " + Math.round(d[3] / 255 * 1000) / 1000 + ")";
        }
      } catch (e) { out = null; }
      return (memo[fn] = out);
    };
  }

  // html2canvas's `onclone`: pin every element's modern colours as rgba().
  function plainColorsInClone(clonedDoc) {
    var win = clonedDoc && clonedDoc.defaultView;
    if (!win || !win.getComputedStyle) return;
    var toRgba = canvasRgba(clonedDoc);
    var all = clonedDoc.getElementsByTagName("*");
    for (var i = 0; i < all.length; i++) {
      var el = all[i], cs;
      try { cs = win.getComputedStyle(el); } catch (e) { continue; }
      for (var j = 0; j < COLOR_PROPS.length; j++) {
        var v = cs.getPropertyValue(COLOR_PROPS[j]);
        var plain = plainColors(v, toRgba);
        if (plain !== v && el.style) el.style.setProperty(COLOR_PROPS[j], plain, "important");
      }
    }
  }

  // ─── Capture (browser only) ────────────────────────────────────────

  // html2canvas does not render iframe CONTENT — an embedded frame comes out as
  // a blank rectangle, so a screenshot of a page whose main content is framed
  // shows nothing (reported in the wild as "the screenshot misses the content").
  // For SAME-ORIGIN frames we can reach the inner document, render it
  // separately, and paste it into the parent capture at the frame's position.
  // Cross-origin frames are untouchable by design and stay blank.
  // `origin` is the top-left of the captured region in VIEWPORT coordinates
  // — {left: 0, top: 0} for a viewport-cropped capture, doc.body's rect for
  // a full-body capture — so frame rects (which getBoundingClientRect always
  // reports viewport-relative) land at the right canvas position either way.
  // `missed` (optional array) collects why each same-origin frame could not be
  // drawn: the capture still succeeds, but must not claim the frame is in it.
  function compositeIframes(html2canvas, doc, canvas, scale, origin, missed) {
    missed = missed || [];
    function miss(e) { missed.push(String((e && e.message) || e || "unknown error").slice(0, 120)); }
    var frames;
    try { frames = Array.prototype.slice.call(doc.querySelectorAll("iframe")); }
    catch (e) { return Promise.resolve(canvas); }
    if (!frames.length) return Promise.resolve(canvas);

    var jobs = frames.map(function (f) {
      var idoc = null, rect = null;
      try {
        // throws (or returns null) for cross-origin — treated as "skip"
        idoc = f.contentDocument;
        rect = f.getBoundingClientRect();
      } catch (e) { return null; }
      if (!idoc || !idoc.body || !rect || rect.width < 1 || rect.height < 1) return null;
      if (f.getAttribute && f.getAttribute("data-bug-report-exclude") != null) return null;
      return { el: f, doc: idoc, rect: rect };
    }).filter(Boolean);
    if (!jobs.length) return Promise.resolve(canvas);

    var ctx = canvas.getContext && canvas.getContext("2d");
    if (!ctx) return Promise.resolve(canvas);

    return Promise.all(jobs.map(function (j) {
      return html2canvas(j.doc.body, {
        useCORS: true, logging: false, scale: scale,
        backgroundColor: null, onclone: plainColorsInClone,
        width: Math.ceil(j.rect.width), height: Math.ceil(j.rect.height),
        windowWidth: Math.ceil(j.rect.width), windowHeight: Math.ceil(j.rect.height),
      }).then(function (sub) {
        try {
          // position of the frame within the captured region, in canvas pixels
          var x = (j.rect.left - origin.left) * scale;
          var y = (j.rect.top - origin.top) * scale;
          ctx.drawImage(sub, x, y, j.rect.width * scale, j.rect.height * scale);
        } catch (e) { miss(e); }   // one bad frame must not lose the whole screenshot
      }).catch(miss);
    })).then(function () { return canvas; });
  }

  // The part of the page that is on screen, as html2canvas wants it: x/y/width/
  // height in page coordinates, left/top where it starts inside the layout
  // viewport (iframe rects are relative to that), the scroll position, and the
  // window the clone is laid out in. null when the host reports no geometry.
  //
  // The visual viewport is what the user sees. documentElement.clientHeight is
  // NOT: iOS Safari keeps it at the smallest viewport (toolbar shown), so once
  // the toolbar collapses on scroll the bottom ~40px of what is on screen was
  // left out (LeapHQ bug-20261020-213458, iPhone, iOS 26: client 714, visible
  // 754). A pinch-zoomed page shows only part of the layout viewport, which
  // visualViewport's offset and size also describe. The page also shows faintly
  // under iOS 26's floating toolbar; no API reports that strip, and it is
  // behind the browser's own controls, so it stays out.
  //
  // The clone is laid out at the layout width (media queries) and at the
  // taller of the two heights, which is where position:fixed;bottom:0 sits
  // while the toolbar is collapsed.
  function scrollPos(win) {
    return {
      x: (win && (win.scrollX != null ? win.scrollX : win.pageXOffset)) || 0,
      y: (win && (win.scrollY != null ? win.scrollY : win.pageYOffset)) || 0,
    };
  }
  function visibleArea(win, doc) {
    var de = doc && doc.documentElement;
    var cw = (de && de.clientWidth) || 0, ch = (de && de.clientHeight) || 0;
    var iw = (win && win.innerWidth) || 0, ih = (win && win.innerHeight) || 0;
    var sp = scrollPos(win), sx = sp.x, sy = sp.y;
    var vv = win && win.visualViewport;
    var hasVv = !!(vv && vv.width > 0 && vv.height > 0);
    // innerHeight includes a desktop's horizontal scrollbar, so it only wins
    // when the visible area really is taller than clientHeight (iOS).
    var layoutW = cw || iw;
    var layoutH = hasVv && ch && vv.height <= ch ? ch : Math.max(ch, ih);
    var left = 0, top = 0, w = layoutW, h = layoutH;
    if (hasVv) {
      left = vv.offsetLeft || 0; top = vv.offsetTop || 0;
      w = vv.width; h = vv.height;
    }
    if (!(w > 0 && h > 0)) return null;
    return {
      x: sx + left, y: sy + top, width: Math.round(w), height: Math.round(h),
      left: left, top: top, scrollX: sx, scrollY: sy,
      windowWidth: Math.round(layoutW || w), windowHeight: Math.round(layoutH || h),
    };
  }

  // Inject the html2canvas script once and resolve the global (or null if it
  // would not load). In-flight loads are shared so two quick clicks inject one
  // script; a failed load is forgotten so the next click may try again.
  var h2cFallbackStore = {};
  function loadHtml2canvas(win, doc, url) {
    if (win && win.html2canvas) return Promise.resolve(win.html2canvas);
    var store = win ? (win.__bugReportH2cLoads = win.__bugReportH2cLoads || {}) : h2cFallbackStore;
    if (store[url]) return store[url];
    var p = new Promise(function (resolve) {
      try {
        var s = doc.createElement("script");
        s.src = url;
        s.async = true;
        s.onload = function () { resolve((win && win.html2canvas) || null); };
        s.onerror = function () { resolve(null); };
        (doc.head || doc.body).appendChild(s);
      } catch (e) { resolve(null); }
    }).then(function (h) {
      if (!h) delete store[url];
      return h;
    });
    store[url] = p;
    return p;
  }

  // One line a person can read, from a capture failure. Shown in the dialog
  // and sent as `screenshotError`.
  function describeCaptureFailure(f) {
    if (!f || !f.reason) return "";
    function secs(ms) { return (Math.round((ms || 0) / 100) / 10) + "s"; }
    switch (f.reason) {
      case "no-library": return "the screenshot library did not load";
      case "timeout": return "capture timed out after " + secs(f.ms);
      case "render-error": return "capture failed: " + (f.detail || "unknown error");
      case "blank": return "capture came back blank";
      case "encode-error": return "could not encode the capture: " + (f.detail || "unknown error");
      case "tainted": return "the page holds cross-origin content the browser will not let us read";
      case "too-large": return "capture too large to send (" + (f.width || 0) + "×" + (f.height || 0) + ")";
      default: return "capture failed (" + f.reason + ")";
    }
  }

  // One line for a capture that worked but could not draw some same-origin frames, or
  // "" when nothing is missing. Shown in the dialog and sent as screenshotError.
  function describePartialCapture(r) {
    var n = (r && r.framesMissed) || 0;
    if (!n) return "";
    // "may be": html-to-image (capture-engine.js) draws same-origin frames itself,
    // so a frame whose separate render failed can still be in the picture.
    return (n === 1 ? "1 embedded frame" : n + " embedded frames") + " may be missing from the screenshot"
      + (r.frameError ? " (" + r.frameError + ")" : "");
  }

  // Same capture as captureScreenshot, but says WHY when there is no image.
  // Always resolves { dataUrl, reason, detail, ms, width, height, late }:
  //   success  → dataUrl set, reason null; framesMissed + frameError when a
  //              same-origin frame could not be drawn into it (1.6.4)
  //   failure  → dataUrl null, reason one of no-library | timeout |
  //              render-error | blank | encode-error | too-large | tainted
  // On a timeout, `late` is a promise of the render's real duration in ms (null
  // if it had not finished cfg.captureLateWatchMs later).
  //
  // Why this exists (leap-timesheet bug-20260915-000612): reports from one tab
  // in Safari arrived with no screenshot while other tabs' had one, and every
  // failure path resolved a bare null, so nothing recorded which of the causes
  // it was.
  //
  // The timeout is load-bearing — html2canvas has been known to hang on iOS
  // Safari with certain CSS features (filters, blend modes, large viewports).
  // Without it the widget can lock up. It also covers a lazy script load.
  //
  // `view` (optional): the visibleArea() to crop to, measured earlier. Omitted,
  // it is measured when the render starts.
  function captureScreenshotDetailed(deps, cfg, view) {
    var doc = deps.document;
    var win = deps.window || (typeof window !== "undefined" ? window : null);
    var now = deps.now || function () { return Date.now(); };
    var startedAt = now();
    var lateWatchMs = cfg.captureLateWatchMs != null ? cfg.captureLateWatchMs : DEFAULTS.captureLateWatchMs;
    return new Promise(function (resolve) {
      var done = false, timer = null;
      var finishedAt = null, onFinished = null;
      function markFinished() {
        if (finishedAt == null) {
          finishedAt = now();
          if (onFinished) onFinished(finishedAt - startedAt);
        }
      }
      function finish(result) {
        if (done) return;
        done = true;
        clearTimeout(timer);
        if (result.ms == null) result.ms = now() - startedAt;
        resolve(result);
      }
      function ok(dataUrl, missed) {
        var r = { dataUrl: dataUrl, reason: null };
        if (missed && missed.length) { r.framesMissed = missed.length; r.frameError = missed[0]; }
        finish(r);
      }
      function fail(reason, extra) {
        var r = { dataUrl: null, reason: reason };
        if (extra) for (var k in extra) if (Object.prototype.hasOwnProperty.call(extra, k)) r[k] = extra[k];
        finish(r);
      }
      function errText(e) {
        return String((e && ((e.name && e.name !== "Error" ? e.name + ": " : "") + (e.message || ""))) || e || "unknown error").slice(0, 160);
      }

      timer = setTimeout(function () {
        if (done) return;
        var late = new Promise(function (res) {
          if (finishedAt != null) { res(finishedAt - startedAt); return; }
          var watch = setTimeout(function () { onFinished = null; res(null); }, lateWatchMs);
          if (watch && watch.unref) watch.unref();   // Node: don't pin a test process
          onFinished = function (ms) { clearTimeout(watch); res(ms); };
        });
        fail("timeout", { ms: now() - startedAt, late: late });
      }, cfg.captureTimeoutMs);

      if (!doc) { fail("no-library", { ms: 0 }); return; }
      var libP = deps.html2canvas ? Promise.resolve(deps.html2canvas)
        : cfg.html2canvasUrl ? loadHtml2canvas(win, doc, cfg.html2canvasUrl)
        : Promise.resolve(null);
      libP.then(function (html2canvas) {
        if (done) return;
        if (!html2canvas) { fail("no-library", { ms: now() - startedAt }); return; }
        run(html2canvas);
      });

      function run(html2canvas) {
        var btnId = cfg.idPrefix + "-button";
        var modalId = cfg.idPrefix + "-modal";
        var scale = Math.min((win && win.devicePixelRatio) || 1, 2);

        // Capture what is ON SCREEN, not the whole document. Rendering doc.body
        // uncropped scales with scroll height: a long list page produced a
        // viewport-wide × full-scroll-height PNG that blew past the server's
        // size cap (413, report dead-ended) and previewed as a sliver. What
        // the user sees when they hit the button is the bug context anyway.
        // Falls back to the uncropped capture when viewport geometry is
        // unavailable (headless hosts, exotic embeds). The area is normally
        // measured by openModal before the form opens (see visibleArea).
        var area = view !== undefined ? view : visibleArea(win, doc);
        var cropped = !!area;

        var opts = {
          useCORS: true,
          logging: false,
          scale: scale,
          onclone: plainColorsInClone,
          // Exclude the widget itself — both the floating button and the
          // modal — so neither contributes pixels to its own screenshot.
          // Also honor an opt-out attribute consumers can mark on their
          // own elements (e.g. a password field, a sensitive widget).
          ignoreElements: function (el) {
            if (!el) return false;
            if (el.id === btnId || el.id === modalId) return true;
            if (el.getAttribute && el.getAttribute("data-bug-report-exclude") != null) return true;
            return false;
          },
        };
        if (cropped) {
          opts.x = area.x;
          opts.y = area.y;
          opts.width = area.width;
          opts.height = area.height;
          opts.windowWidth = area.windowWidth;
          opts.windowHeight = area.windowHeight;
          // The scroll position as it was measured: by the time this runs the
          // page may have moved (iOS scrolls a focused field into view).
          opts.scrollX = area.scrollX;
          opts.scrollY = area.scrollY;
        }

        try {
          Promise.resolve(html2canvas(doc.body, opts)).then(function (canvas) {
            // Frame rects are relative to the layout viewport; the cropped
            // canvas starts at the visible area's corner within it.
            // Uncropped, positions are body-relative.
            var origin = cropped
              ? { left: area.left, top: area.top }
              : doc.body.getBoundingClientRect();
            // paste any same-origin iframe content in before serialising
            var missed = [];
            return compositeIframes(html2canvas, doc, canvas, scale, origin, missed).then(function (merged) {
              markFinished();
              try {
                var size = { width: merged && merged.width, height: merged && merged.height };
                var state = canvasState(merged);
                if (state === "tainted") { fail("tainted", size); return; }
                if (state !== "ok") { fail("blank", size); return; }
                var diag = {};
                var url = encodeCanvasUnderCap(merged, cfg.maxScreenshotBytes || DEFAULTS.maxScreenshotBytes, doc, diag);
                if (url) { ok(url, missed); return; }
                if (diag.tainted) { fail("tainted", size); return; }
                if (diag.error) { size.detail = String(diag.error).slice(0, 160); fail("encode-error", size); return; }
                fail("too-large", size);
              } catch (e) { fail("encode-error", { detail: errText(e) }); }
            });
          }).catch(function (e) { markFinished(); fail("render-error", { detail: errText(e) }); });
        } catch (e) { markFinished(); fail("render-error", { detail: errText(e) }); }
      }
    });
  }

  // Always resolves: data URL on success, null on failure, blank canvas,
  // or timeout. (Use captureScreenshotDetailed to learn why.)
  function captureScreenshot(deps, cfg) {
    return captureScreenshotDetailed(deps, cfg).then(function (r) { return r.dataUrl; });
  }

  // ─── Screenshot viewer: zoom, pan and simple markup ────────────────

  // Pure view maths, so the zoom behaviour is testable without a DOM.
  // A view is {z, tx, ty}: the canvas is drawn scaled by z and offset (tx, ty).
  var ZOOM_MIN = 0.1, ZOOM_MAX = 16;

  function fitView(stageW, stageH, imgW, imgH) {
    var z = Math.min(stageW / imgW, stageH / imgH, 1);
    return { z: z, tx: (stageW - imgW * z) / 2, ty: (stageH - imgH * z) / 2 };
  }

  // Zoom by factor f keeping the point (px, py) of the stage fixed on screen.
  function zoomAbout(view, px, py, f) {
    var nz = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, view.z * f));
    var k = nz / view.z;
    return { z: nz, tx: px - (px - view.tx) * k, ty: py - (py - view.ty) * k };
  }

  // Two-finger pinch: the view that results from two touch points moving from
  // (a0, b0) to (a1, b1), starting at view v0. Scale follows the change in the
  // distance between the fingers; the image point that was under the starting
  // midpoint ends up under the current midpoint, so it pans as well as zooms.
  function pinchView(v0, a0, b0, a1, b1) {
    var d0 = Math.hypot(b0.x - a0.x, b0.y - a0.y);
    var d1 = Math.hypot(b1.x - a1.x, b1.y - a1.y);
    if (!d0 || !isFinite(d0) || !isFinite(d1)) return v0;
    var m0 = { x: (a0.x + b0.x) / 2, y: (a0.y + b0.y) / 2 };
    var m1 = { x: (a1.x + b1.x) / 2, y: (a1.y + b1.y) / 2 };
    var z = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, v0.z * d1 / d0));
    var k = z / v0.z;
    return { z: z, tx: m1.x - (m0.x - v0.tx) * k, ty: m1.y - (m0.y - v0.ty) * k };
  }

  // Marks are strokes in image pixels: {kind: "pen"|"box", pts: [[x, y], …]}.
  var MARK_COLOR = "#e5322d";
  function setMarkPen(ctx, lineW) {
    ctx.strokeStyle = MARK_COLOR; ctx.lineWidth = lineW; ctx.lineCap = "round"; ctx.lineJoin = "round";
  }
  function paintStroke(ctx, s) {
    ctx.beginPath();
    if (s.kind === "box") {
      var a = s.pts[0], b = s.pts[s.pts.length - 1];
      ctx.strokeRect(a[0], a[1], b[0] - a[0], b[1] - a[1]);
      return;
    }
    s.pts.forEach(function (p, i) { i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]); });
    if (s.pts.length === 1) ctx.lineTo(s.pts[0][0] + 0.01, s.pts[0][1]);
    ctx.stroke();
  }
  // Repaint ALL marks onto the overlay (undo, clear, a box being dragged). The
  // overlay holds only marks, never the screenshot, so this is cheap.
  function paintMarks(ctx, strokes, lineW, W, H) {
    ctx.clearRect(0, 0, W, H);
    setMarkPen(ctx, lineW);
    strokes.forEach(function (s) { paintStroke(ctx, s); });
  }

  // Opens a full-window viewer on `src` (a data URL or URL).
  //   opts.annotate  show the pen and box tools and Done/Cancel
  //   opts.strokes   marks from an earlier round, so they can still be undone
  //   opts.onDone    (dataUrl|null, strokes) — the marked-up image; null if nothing was drawn,
  //                  or if it could not be encoded under the size cap
  // deps = {document, window}. Marks are kept as strokes in image pixels and
  // drawn on a transparent overlay canvas above the image, so neither zooming
  // nor drawing ever repaints the image, and the file is composited once, on Done.
  //
  // Touch: with the Move tool, one finger pans and two fingers pinch-zoom. With
  // Pen or Box a second finger is ignored, so a resting palm cannot ruin a mark.
  function openViewer(deps, src, opts) {
    opts = opts || {};
    var doc = deps.document;
    var ID = VIEWER_ID;
    if (doc.getElementById(ID)) return;
    var img = doc.createElement("img");
    img.onload = function () { build(img); };
    img.src = src;

    function el(tag, css, text) {
      var e = doc.createElement(tag);
      if (css) e.style.cssText = css;
      if (text != null) e.textContent = text;
      return e;
    }

    function build(img) {
      var W = img.naturalWidth, H = img.naturalHeight;
      var strokes = (opts.strokes || []).slice();
      var tool = opts.annotate ? "pen" : "move";
      var view = { z: 1, tx: 0, ty: 0 };
      var cur = null;                 // the stroke being drawn
      var primary = null;             // pointerId that is drawing
      var pointers = {};              // pointerId → {x, y} in stage px (Move tool)
      var panFrom = null, pinch = null;

      var root = el("div", "position:fixed;inset:0;z-index:100010;background:rgba(10,12,16,.92);" +
        "display:flex;flex-direction:column;font:14px system-ui,sans-serif;color:#fff;" +
        "padding-bottom:env(safe-area-inset-bottom,0px)");
      root.id = ID;
      root.setAttribute("data-bug-report-exclude", "true");
      // A notch or the home indicator must not sit on the buttons.
      var bar = el("div", "display:flex;gap:6px;align-items:center;flex-wrap:wrap;background:#171b22;" +
        "padding:max(8px,env(safe-area-inset-top,0px)) max(10px,env(safe-area-inset-right,0px)) 8px " +
        "max(10px,env(safe-area-inset-left,0px))");
      var stage = el("div", "flex:1;position:relative;overflow:hidden;touch-action:none");
      var layer = el("div", "position:absolute;left:0;top:0;transform-origin:0 0;background:#fff;" +
        "width:" + W + "px;height:" + H + "px");
      // An <img> a mouse drags over starts the browser's own image drag, which
      // cancels the pointer stream: no stroke ever registers (found in 1.6.0 by
      // a real-browser test). The marks canvas is above it anyway; the stage
      // takes the events.
      img.draggable = false;
      img.style.cssText = "position:absolute;left:0;top:0;width:" + W + "px;height:" + H + "px;display:block;" +
        "pointer-events:none;user-select:none;-webkit-user-drag:none";
      var ov = el("canvas", "position:absolute;left:0;top:0;pointer-events:none");
      ov.width = W; ov.height = H;
      layer.appendChild(img); layer.appendChild(ov);
      stage.appendChild(layer);
      root.appendChild(bar); root.appendChild(stage);
      var octx = ov.getContext("2d");

      function btn(label, title, fn, id) {
        var b = el("button", "padding:6px 11px;border:1px solid #3a4250;border-radius:8px;" +
          "background:#232a35;color:#fff;font:inherit;cursor:pointer", label);
        b.type = "button"; b.title = title; if (id) b.id = ID + "-" + id;
        b.addEventListener("click", fn);
        bar.appendChild(b);
        return b;
      }
      var toolBtns = {};
      function setTool(t) {
        tool = t;
        cur = null; primary = null; pointers = {}; panFrom = null; pinch = null;
        Object.keys(toolBtns).forEach(function (k) { toolBtns[k].style.background = k === t ? "#2f6df6" : "#232a35"; });
        stage.style.cursor = t === "move" ? "grab" : "crosshair";
      }
      function apply() {
        layer.style.transform = "translate(" + view.tx + "px," + view.ty + "px) scale(" + view.z + ")";
      }
      function fit() { view = fitView(stage.clientWidth, stage.clientHeight, W, H); apply(); }
      function zoom(px, py, f) { view = zoomAbout(view, px, py, f); apply(); }
      function mid(f) { zoom(stage.clientWidth / 2, stage.clientHeight / 2, f); }
      // Mark weight follows the image, not the zoom, so a mark drawn zoomed in
      // is as visible at fit as one drawn at fit.
      var lineW = Math.max(3, Math.round(Math.max(W, H) / 350));
      function repaint() { paintMarks(octx, cur ? strokes.concat([cur]) : strokes, lineW, W, H); }

      toolBtns.move = btn("✋ Move", "Drag to pan, wheel or pinch to zoom", function () { setTool("move"); }, "move");
      if (opts.annotate) {
        toolBtns.pen = btn("✏️ Pen", "Draw freehand", function () { setTool("pen"); }, "pen");
        toolBtns.box = btn("▭ Box", "Draw a box", function () { setTool("box"); }, "box");
        btn("↶ Undo", "Remove the last mark", function () { strokes.pop(); repaint(); }, "undo");
        btn("Clear", "Remove all marks", function () { strokes = []; repaint(); }, "clear");
      }
      btn("−", "Zoom out", function () { mid(1 / 1.4); }, "zoomout");
      btn("+", "Zoom in", function () { mid(1.4); }, "zoomin");
      btn("Fit", "Fit to window", fit, "fit");
      btn("100%", "Actual size", function () { mid(1 / view.z); }, "actual");
      bar.appendChild(el("span", "flex:1"));
      if (opts.annotate) {
        btn("Done", "Keep the marks and close", function () { close(true); }, "done").style.background = "#2f6df6";
        btn("Cancel", "Discard the marks", function () { close(false); }, "cancel");
      } else {
        btn("Close", "Close", function () { close(false); }, "cancel");
      }

      function stagePt(e) {
        var r = stage.getBoundingClientRect();
        return { x: e.clientX - r.left, y: e.clientY - r.top };
      }
      function toImg(e) {
        var p = stagePt(e);
        return [(p.x - view.tx) / view.z, (p.y - view.ty) / view.z];
      }
      function startPinch() {
        var ids = Object.keys(pointers);
        pinch = { ids: [ids[0], ids[1]], view: view, a0: pointers[ids[0]], b0: pointers[ids[1]] };
        panFrom = null;
      }

      stage.addEventListener("dragstart", function (e) { e.preventDefault(); });
      stage.addEventListener("pointerdown", function (e) {
        var id = e.pointerId;
        if (tool !== "move") {
          if (primary !== null) return;             // a second finger while drawing: ignored
          primary = id;
          try { stage.setPointerCapture(id); } catch (_) {}
          cur = { kind: tool, pts: [toImg(e)] };
          if (tool === "pen") {                     // show the dot straight away
            setMarkPen(octx, lineW); paintStroke(octx, cur);
          }
          return;
        }
        if (Object.keys(pointers).length >= 2) return;   // a third finger: ignored
        try { stage.setPointerCapture(id); } catch (_) {}
        pointers[id] = stagePt(e);
        if (Object.keys(pointers).length === 1) {
          panFrom = [pointers[id].x - view.tx, pointers[id].y - view.ty];
          stage.style.cursor = "grabbing";
        } else {
          startPinch();
        }
      });
      stage.addEventListener("pointermove", function (e) {
        var id = e.pointerId;
        if (tool !== "move") {
          if (!cur || id !== primary) return;
          var p = toImg(e);
          if (cur.kind === "pen") {
            // Only the new segment is painted — never the whole image, never
            // the whole stroke — so a long drag costs the same per move.
            var prev = cur.pts[cur.pts.length - 1];
            cur.pts.push(p);
            setMarkPen(octx, lineW);
            octx.beginPath(); octx.moveTo(prev[0], prev[1]); octx.lineTo(p[0], p[1]); octx.stroke();
          } else {
            cur.pts[1] = p;
            repaint();                               // the overlay only: marks, not the image
          }
          return;
        }
        if (!pointers[id]) return;
        pointers[id] = stagePt(e);
        if (pinch) {
          view = pinchView(pinch.view, pinch.a0, pinch.b0, pointers[pinch.ids[0]], pointers[pinch.ids[1]]);
          apply();
        } else if (panFrom) {
          view = { z: view.z, tx: pointers[id].x - panFrom[0], ty: pointers[id].y - panFrom[1] };
          apply();
        }
      });
      function up(e, cancelled) {
        var id = e.pointerId;
        if (tool !== "move") {
          if (id !== primary) return;
          primary = null;
          if (cur && !cancelled) strokes.push(cur);
          cur = null;
          if (cancelled) repaint();                  // drop the half-drawn mark
          return;
        }
        if (!pointers[id]) return;
        delete pointers[id];
        var left = Object.keys(pointers);
        if (pinch) {
          pinch = null;
          if (left.length === 1) {                   // one finger stays down: carry on panning
            panFrom = [pointers[left[0]].x - view.tx, pointers[left[0]].y - view.ty];
          }
        }
        if (!left.length) { panFrom = null; stage.style.cursor = "grab"; }
      }
      stage.addEventListener("pointerup", function (e) { up(e, false); });
      stage.addEventListener("pointercancel", function (e) { up(e, true); });
      stage.addEventListener("wheel", function (e) {
        e.preventDefault();
        var p = stagePt(e);
        zoom(p.x, p.y, e.deltaY < 0 ? 1.15 : 1 / 1.15);
      }, { passive: false });

      function onKey(e) {
        if (e.key !== "Escape") return;
        // The viewer may sit inside a host <dialog> (reachOverDialogs): without
        // this the same Escape would also close the app's dialog underneath.
        try { e.preventDefault && e.preventDefault(); } catch (_) {}
        close(false);
      }
      doc.addEventListener("keydown", onKey);
      function close(keep) {
        doc.removeEventListener("keydown", onKey);
        if (root.parentNode) root.parentNode.removeChild(root);
        if (!keep || !opts.onDone) return;
        if (!strokes.length) { opts.onDone(null, []); return; }
        // Composite image + marks once, here. Through the same size ladder as
        // the capture, so a busy annotated PNG still lands under the cap.
        var flat = doc.createElement("canvas");
        flat.width = W; flat.height = H;
        var fctx = flat.getContext("2d");
        fctx.drawImage(img, 0, 0, W, H);
        fctx.drawImage(ov, 0, 0);
        opts.onDone(encodeCanvasUnderCap(flat, DEFAULTS.maxScreenshotBytes, doc), strokes);
      }

      doc.body.appendChild(root);
      repaint(); setTool(tool); fit();
    }
  }

  // ─── DOM construction (browser only) ───────────────────────────────

  function styleStr(obj) {
    var parts = [];
    for (var k in obj) parts.push(k + ":" + obj[k]);
    return parts.join(";");
  }

  function viewportSize(win) {
    return {
      w: (win && win.innerWidth) || 0,
      h: (win && win.innerHeight) || 0,
    };
  }

  // Re-apply the stored position, clamped to the viewport as it is NOW. Called
  // at mount and on every resize; never writes storage, so a position chosen on
  // a big window comes back when the window does.
  function placeFromStorage(btn, cfg, win, size) {
    var pos = loadStoredPos(cfg);
    if (!pos) return;
    var vp = viewportSize(win);
    var p = clampPos(pos, vp.w, vp.h, size, size);
    applyAbsolutePos(btn, p.left, p.top);
  }

  function buildButton(doc, cfg, onClick, win) {
    var btn = doc.createElement("button");
    btn.id = cfg.idPrefix + "-button";
    btn.type = "button";
    btn.setAttribute("aria-label", "Report a bug");
    btn.setAttribute("data-bug-report-exclude", "true");
    btn.textContent = "🐛";
    var size = Math.max(24, Math.min(96, Number(cfg.buttonSize) || 52));
    var css = {
      position: "fixed",
      "z-index": "99998",
      width: size + "px",
      height: size + "px",
      "border-radius": "50%",
      border: "0",
      background: cfg.theme.buttonBg,
      color: cfg.theme.buttonInk,
      "box-shadow": "0 6px 18px rgba(0,0,0,0.22), 0 0 0 0.5px rgba(0,0,0,0.08)",
      "font-size": Math.round(size * 0.46) + "px",
      "line-height": "1",
      "font-family": cfg.theme.font,
      cursor: "grab",
      padding: "0",
      "user-select": "none",
      "touch-action": "none",
      display: "flex",
      "align-items": "center",
      "justify-content": "center",
    };
    var corner = positionCss(cfg.position);
    for (var k in corner) css[k] = corner[k];
    btn.style.cssText = styleStr(css);

    btn._size = size;
    placeFromStorage(btn, cfg, win, size);

    btn.addEventListener("click", function (e) {
      if (btn._suppressClick) { btn._suppressClick = false; e.preventDefault(); e.stopPropagation(); return; }
      onClick();
    });
    makeDraggable(btn, cfg, win);
    return btn;
  }

  function applyAbsolutePos(btn, left, top) {
    btn.style.left = left + "px";
    btn.style.top = top + "px";
    btn.style.right = "auto";
    btn.style.bottom = "auto";
  }

  function loadStoredPos(cfg) {
    try {
      var raw = (typeof localStorage !== "undefined") && localStorage.getItem(cfg.storageKey);
      if (!raw) return null;
      var p = JSON.parse(raw);
      if (typeof p.left === "number" && typeof p.top === "number") return p;
    } catch (e) {}
    return null;
  }

  function savePos(cfg, left, top) {
    try { localStorage.setItem(cfg.storageKey, JSON.stringify({ left: left, top: top })); } catch (e) {}
  }

  function makeDraggable(btn, cfg, win) {
    var dragging = false, startX = 0, startY = 0, origLeft = 0, origTop = 0, moved = false;
    btn.addEventListener("pointerdown", function (e) {
      dragging = true;
      moved = false;
      startX = e.clientX; startY = e.clientY;
      var rect = btn.getBoundingClientRect();
      origLeft = rect.left; origTop = rect.top;
      btn.style.cursor = "grabbing";
      try { btn.setPointerCapture(e.pointerId); } catch (_) {}
    });
    btn.addEventListener("pointermove", function (e) {
      if (!dragging) return;
      var dx = e.clientX - startX, dy = e.clientY - startY;
      if (Math.abs(dx) + Math.abs(dy) > 5) moved = true;
      var vp = viewportSize(win);
      var p = clampPos({ left: origLeft + dx, top: origTop + dy }, vp.w, vp.h, btn.offsetWidth, btn.offsetHeight);
      applyAbsolutePos(btn, p.left, p.top);
    });
    function endDrag(e) {
      if (!dragging) return;
      dragging = false;
      btn.style.cursor = "grab";
      try { btn.releasePointerCapture(e.pointerId); } catch (_) {}
      if (moved) {
        btn._suppressClick = true;
        var rect = btn.getBoundingClientRect();
        savePos(cfg, rect.left, rect.top);
      }
    }
    btn.addEventListener("pointerup", endDrag);
    btn.addEventListener("pointercancel", endDrag);
  }

  function buildModal(doc, cfg, callbacks) {
    var t = cfg.theme;
    var overlay = doc.createElement("div");
    overlay.id = cfg.idPrefix + "-modal";
    overlay.setAttribute("data-bug-report-exclude", "true");
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", cfg.title || DEFAULTS.title);
    overlay.style.cssText = styleStr({
      position: "fixed", inset: "0", "z-index": "100000",
      background: "rgba(0,0,0,0.5)",
      display: "flex", "align-items": "flex-end", "justify-content": "center",
      padding: "0", "font-family": t.font,
    });

    var box = doc.createElement("div");
    box.style.cssText = styleStr({
      background: t.modalBg, color: t.modalInk,
      "border-radius": "16px 16px 0 0",
      padding: "16px",
      "max-width": "560px", width: "100%", "max-height": "92vh",
      overflow: "auto",
      // A scroll that reaches the form's end must not carry on into the page.
      "overscroll-behavior": "contain",
      "box-shadow": "0 -8px 24px rgba(0,0,0,0.2)",
      "padding-bottom": "calc(env(safe-area-inset-bottom, 0px) + 16px)",
    });
    overlay.appendChild(box);
    // On wider screens, center the modal instead of bottom-sheet.
    if (typeof window !== "undefined" && window.innerWidth > 720) {
      overlay.style.alignItems = "center";
      overlay.style.padding = "24px";
      box.style.borderRadius = t.radius;
      box.style.paddingBottom = "16px";
    }

    var title = doc.createElement("h2");
    title.textContent = cfg.title || DEFAULTS.title;
    title.style.cssText = "font-size:18px;font-weight:600;margin:0 0 6px 0";
    box.appendChild(title);

    var hint = doc.createElement("p");
    hint.id = overlay.id + "-hint";
    hint.style.cssText = "font-size:13px;color:" + t.mutedInk + ";margin:0 0 12px 0";
    hint.textContent = "Capturing screenshot…";
    box.appendChild(hint);

    var preview = doc.createElement("img");
    preview.id = overlay.id + "-preview";
    preview.alt = "";
    preview.style.cssText = "display:none;max-width:100%;max-height:220px;border-radius:10px;margin:0 0 12px 0;background:rgba(0,0,0,0.04)";
    box.appendChild(preview);

    var textarea = doc.createElement("textarea");
    textarea.id = overlay.id + "-textarea";
    textarea.placeholder = "What happened? What did you expect?";
    textarea.style.cssText = styleStr({
      width: "100%", "min-height": "110px",
      padding: "10px 12px",
      border: "0.5px solid rgba(0,0,0,0.18)",
      "border-radius": "10px",
      "font-family": "inherit", "font-size": "16px",
      "box-sizing": "border-box", resize: "vertical",
      background: t.modalBg, color: t.modalInk,
    });
    box.appendChild(textarea);

    // What the submit is doing right now ("Sending… uploading a 1.2 MB
    // screenshot"). Separate from the error line so a retry's progress does not
    // read as another failure.
    var status = doc.createElement("div");
    status.id = overlay.id + "-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    status.style.cssText = "color:" + t.mutedInk + ";font-size:12px;margin-top:8px;display:none";
    box.appendChild(status);

    var error = doc.createElement("div");
    error.id = overlay.id + "-error";
    error.style.cssText = "color:" + t.errorInk + ";font-size:13px;margin-top:8px;display:none";
    box.appendChild(error);

    var actions = doc.createElement("div");
    actions.style.cssText = "display:flex;gap:8px;justify-content:flex-end;margin-top:14px";
    box.appendChild(actions);

    var cancel = doc.createElement("button");
    cancel.id = overlay.id + "-cancel";
    cancel.type = "button";
    cancel.textContent = "Cancel";
    cancel.style.cssText = "padding:8px 14px;border:0;background:transparent;color:" + t.accent + ";border-radius:10px;cursor:pointer;font:inherit";
    actions.appendChild(cancel);

    var submit = doc.createElement("button");
    submit.id = overlay.id + "-submit";
    submit.type = "button";
    submit.textContent = "Submit";
    submit.style.cssText = "padding:10px 18px;border:0;background:" + t.accent + ";color:#fff;border-radius:10px;cursor:pointer;font:inherit;font-weight:600";
    actions.appendChild(submit);

    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) callbacks.onCancel && callbacks.onCancel();
    });
    cancel.addEventListener("click", function () { callbacks.onCancel && callbacks.onCancel(); });
    // Double-submit guard. The POST is async and nothing here changed on the
    // first tap, so on a phone — where the tap target is small and the network
    // is slow — people tap again. That filed the same report 2-3 times, seconds
    // apart, roughly doubling the queue for mobile reporters.
    //
    // NOTE the asymmetry: onSubmit calls showError(null) on the way IN to clear
    // a previous message, immediately before the fetch. So only a TRUTHY msg
    // may restore the button — re-enabling on every showError call would undo
    // the guard at the exact moment it's needed. Every exit from onSubmit must
    // therefore end in: the modal closed (success), or showError with a
    // message (which restores the button). The submit timeout in the controller
    // is what guarantees one of those two endings arrives.
    var submitting = false;
    function resetSubmit() {
      submitting = false;
      submit.disabled = false;
      submit.style.opacity = "";
      submit.style.cursor = "pointer";
      submit.textContent = "Submit";
    }
    function setStatus(msg) {
      status.textContent = msg || "";
      status.style.display = msg ? "block" : "none";
    }
    function showError(msg) {
      error.textContent = msg || "";
      error.style.display = msg ? "block" : "none";
      if (msg) {
        setStatus(null);
        resetSubmit();   // a real failure (or no answer) — let them try again
      }
    }
    submit.addEventListener("click", function () {
      if (submitting) return;
      submitting = true;
      submit.disabled = true;
      submit.style.opacity = "0.6";
      submit.style.cursor = "default";
      submit.textContent = "Submitting…";
      callbacks.onSubmit && callbacks.onSubmit(textarea.value, { showError: showError, setStatus: setStatus });
    });
    return overlay;
  }

  function buildToast(doc, cfg, message) {
    var toast = doc.createElement("div");
    toast.id = cfg.idPrefix + "-toast";
    toast.setAttribute("data-bug-report-exclude", "true");
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
    toast.textContent = message;
    toast.style.cssText = styleStr({
      position: "fixed",
      top: "max(env(safe-area-inset-top, 0px), 14px)",
      left: "50%", transform: "translateX(-50%)",
      "z-index": "100001",
      background: cfg.theme.toastBg, color: cfg.theme.toastInk,
      padding: "10px 16px",
      "border-radius": "14px",
      "font-family": cfg.theme.font, "font-size": "14px",
      "backdrop-filter": "blur(20px)",
      "-webkit-backdrop-filter": "blur(20px)",
    });
    return toast;
  }

  function removeById(doc, id) {
    var el = doc.getElementById(id);
    if (el && el.parentNode) el.parentNode.removeChild(el);
  }

  // The topmost open MODAL dialog, or null. `showModal()` puts a dialog in the
  // top layer and makes everything outside it inert — however high its z-index
  // — so the widget's UI has to live inside it to be clickable. A non-modal
  // dialog does not block the page and is ignored. Document order stands in for
  // "opened last": the browser does not expose the top-layer stack.
  function topModalDialog(doc) {
    var open;
    try { open = doc.querySelectorAll("dialog[open]"); } catch (e) { return null; }
    if (!open) return null;
    for (var i = open.length - 1; i >= 0; i--) {
      var modal = true;
      try { modal = open[i].matches(":modal"); } catch (e) { /* no :modal: assume modal, the safe way */ }
      if (modal) return open[i];
    }
    return null;
  }

  // ─── Controller (one per page) ─────────────────────────────────────

  function createController(opts) {
    opts = opts || {};
    var cfg = mergeConfig(opts.config);
    var deps = {
      document: opts.document,
      window: opts.window,
      fetch: opts.fetch,
      html2canvas: opts.html2canvas,
      now: opts.now,
    };
    var win = deps.window;
    var nowFn = opts.now || function () { return Date.now(); };
    var resolvedBuildSha = function () {
      var b = cfg.buildSha;
      if (typeof b === "function") return b() || "";
      return b || "";
    };

    var capturedDataUrl = null;
    var captureError = "";          // why there is no screenshot, or why part of it may be missing; "" otherwise
    var submissionKey = null;       // idempotency key: one per report (see onSubmit)
    var attempt = null;             // what the last submit sent: {details, shot}
    var openSeq = 0;                // bumped on every open and close: stale async work checks it
    var modalId = cfg.idPrefix + "-modal";
    var btnId = cfg.idPrefix + "-button";
    var toastId = cfg.idPrefix + "-toast";

    function setTimer(fn, ms) {
      var t = (win && win.setTimeout) ? win.setTimeout(fn, ms) : setTimeout(fn, ms);
      // Node only: a trailing timer must not pin a test process open.
      if (t && t.unref) t.unref();
      return t;
    }
    function clearTimer(t) {
      if (t == null) return;
      try { (win && win.clearTimeout) ? win.clearTimeout(t) : clearTimeout(t); } catch (e) {}
    }

    // Tell the app something went wrong on the client. Never the user's problem.
    function emit(kind, detail) {
      if (typeof cfg.onClientEvent !== "function") return;
      try {
        var r = cfg.onClientEvent(kind, detail);
        if (r && typeof r.catch === "function") r.catch(function () {});
      } catch (e) { /* same */ }
    }

    function showToast(message) {
      removeById(deps.document, toastId);
      var t = buildToast(deps.document, cfg, message);
      deps.document.body.appendChild(t);
      deps.window && deps.window.setTimeout && deps.window.setTimeout(function () {
        removeById(deps.document, toastId);
      }, 3500);
    }

    // The reporter's marked-up image replaces the capture: what the preview
    // shows is what is sent, so there is no second copy to go stale.
    // `original` and `strokes` are kept so a second round starts from the
    // untouched capture with the first round's marks still undoable.
    var originalDataUrl = null, markStrokes = [];
    function applyMarkup(dataUrl, strokes) {
      var hint = deps.document.getElementById(modalId + "-hint");
      var prev = deps.document.getElementById(modalId + "-preview");
      if (!dataUrl) {
        if (strokes && strokes.length && hint) {
          hint.textContent = "Could not keep the marks — the image is too large to send. Sending it unmarked.";
        }
        return;
      }
      capturedDataUrl = dataUrl;
      markStrokes = strokes || [];
      if (prev) prev.src = dataUrl;
      if (hint) hint.textContent = "Screenshot marked up. Click it to edit more.";
    }

    // While the form is open, the page behind it cannot be used (LeapHQ
    // bug-20261020-213458: on an iPhone the page under the sheet scrolled).
    // Its siblings are made `inert` — no taps, no focus, hidden from a screen
    // reader, which also keeps Tab inside the form — and touch or wheel
    // scrolling is stopped unless it scrolls something inside the form. The
    // scroll lock is listeners, not overflow:hidden on <html>: that would drop a
    // desktop scrollbar and reflow the page while it is being captured.
    // Siblings of the form's CURRENT parent, so it follows a move into a <dialog>
    // (whose showModal() has made the rest inert already). The viewer and the
    // toast stay usable; a node the app had made inert itself is left alone.
    var inerted = [], inertHost = null;
    function releaseInert() {
      inerted.forEach(function (n) { try { n.removeAttribute("inert"); } catch (e) {} });
      inerted = []; inertHost = null;
    }
    function inertBehind() {
      releaseInert();
      var form = deps.document.getElementById(modalId);
      var host = form && form.parentNode;
      if (!host || !host.children) return;
      inertHost = host;
      Array.prototype.slice.call(host.children).forEach(function (n) {
        if (n === form || n.id === VIEWER_ID || n.id === toastId) return;
        if (!n.setAttribute || (n.getAttribute && n.getAttribute("inert") != null)) return;
        n.setAttribute("inert", "");
        inerted.push(n);
      });
    }
    function scrollsInsideForm(target) {
      var form = deps.document.getElementById(modalId);
      for (var n = target; n && n !== deps.document; n = n.parentNode) {
        if (n.id === VIEWER_ID) return true;      // the viewer handles its own gestures
        if (n === form) return false;             // reached the backdrop: nothing of ours scrolls
        if (n.scrollHeight > n.clientHeight + 1) return true;
      }
      return false;
    }
    function blockPageScroll(e) {
      if (!scrollsInsideForm(e && e.target)) { try { e.preventDefault(); } catch (_) {} }
    }
    // A plain focus: with preventScroll, iOS leaves the field under the
    // keyboard (the sheet is fixed to the bottom of the layout viewport).
    function focusTextarea() {
      var ta = deps.document.getElementById(modalId + "-textarea");
      if (ta && ta.focus) try { ta.focus(); } catch (_) {}
    }
    // Where the page was when the form opened. A phone's keyboard can still
    // scroll it (iOS moved a page from 577 to 1014 to make room), and the
    // reporter could not have scrolled it themselves, so it goes back on close.
    var openScroll = null;
    function restoreScroll() {
      var at = openScroll;
      openScroll = null;
      if (!at || !win || typeof win.scrollTo !== "function") return;
      var now = scrollPos(win);
      if (now.x !== at.x || now.y !== at.y) try { win.scrollTo(at.x, at.y); } catch (_) {}
    }
    var scrollLocked = false;
    function lockScroll(on) {
      var doc = deps.document;
      if (!doc.addEventListener || on === scrollLocked) return;
      scrollLocked = on;
      ["touchmove", "wheel"].forEach(function (t) {
        if (on) doc.addEventListener(t, blockPageScroll, { passive: false });
        else doc.removeEventListener(t, blockPageScroll, { passive: false });
      });
    }

    // Escape closes the modal — unless the screenshot viewer is up, whose own
    // Escape closes just the viewer (its listener runs after this one, so the
    // viewer is still in the DOM here).
    var escHandler = null;
    function closeModal() {
      openSeq++;
      capturedDataUrl = null; originalDataUrl = null; markStrokes = [];
      captureError = ""; submissionKey = null; attempt = null;
      releaseInert();
      lockScroll(false);
      removeById(deps.document, modalId);
      restoreScroll();
      if (escHandler) {
        deps.document.removeEventListener && deps.document.removeEventListener("keydown", escHandler);
        escHandler = null;
      }
    }

    // A capture that produced no image leaves evidence for the app (if it asked
    // to hear): the reason, the page, the viewport and canvas size, how many
    // elements the renderer had to walk, and — for a timeout — how long the
    // render really took, which is only known later.
    function reportCaptureFailure(f) {
      function send(lateMs) {
        var doc = deps.document;
        var parts = [describeCaptureFailure(f)];
        if (f.reason === "timeout") {
          parts.push(lateMs == null ? "render never finished" : "render finished after " + Math.round(lateMs) + "ms");
        }
        // The path only: a query string can hold tokens or personal data, and
        // this goes wherever the app's hook sends it. The app can add more.
        var loc = (win && win.location) || {};
        var de = doc && doc.documentElement;
        if (de && de.clientWidth) parts.push("viewport " + de.clientWidth + "x" + de.clientHeight + "@" + ((win && win.devicePixelRatio) || 1));
        if (f.width) parts.push("canvas " + f.width + "x" + f.height);
        try { if (doc && doc.getElementsByTagName) parts.push("elements " + doc.getElementsByTagName("*").length); } catch (e) { /* optional */ }
        emit("screenshot-failure", {
          reason: f.reason,
          detail: f.detail || "",
          ms: f.ms,
          lateMs: lateMs == null ? null : lateMs,
          width: f.width || 0,
          height: f.height || 0,
          path: loc.pathname || "/",
          version: resolvedBuildSha(),
          message: parts.join("; ").slice(0, 300),
        });
      }
      if (f.late && typeof f.late.then === "function") f.late.then(send, function () { send(null); });
      else send(null);
    }

    // POST the report, but never wait forever. Resolves exactly one of
    // { response }, { error }, { timedOut: true } — the modal has an ending
    // for each, so the submit button can always come back.
    function raceSubmit(payload) {
      var controller = null;
      try { if (win && win.AbortController) controller = new win.AbortController(); } catch (e) {}
      var init = {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload,
      };
      if (controller) init.signal = controller.signal;
      return new Promise(function (resolve) {
        var settled = false;
        function settle(v) { if (!settled) { settled = true; resolve(v); } }
        var timer = setTimer(function () {
          // Abort so a stalled request releases its connection instead of
          // holding one for the life of the page. An AbortError arriving
          // afterwards is ignored — we have already settled.
          if (controller) { try { controller.abort(); } catch (e) {} }
          settle({ timedOut: true });
        }, cfg.submitTimeoutMs);
        var p;
        try { p = Promise.resolve(deps.fetch(cfg.endpoint, init)); }
        catch (e) { clearTimer(timer); settle({ error: e }); return; }
        p.then(
          function (resp) { clearTimer(timer); settle({ response: resp }); },
          function (err) { clearTimer(timer); settle({ error: err || new Error("fetch failed") }); }
        );
      });
    }

    function onSubmit(description, ui, mySeq) {
      if (!description || !description.trim()) {
        ui.showError("Please describe what happened.");
        return;
      }
      ui.showError(null);
      // One key per REPORT. If the reporter changed the text (or the marks)
      // since the last attempt, this is no longer the same report, and reusing
      // the key would let a deduping server answer with the old row and
      // silently drop the edit.
      var trimmed = description.trim();
      if (attempt && (attempt.details !== trimmed || attempt.shot !== capturedDataUrl)) submissionKey = newClientKey(win);
      attempt = { details: trimmed, shot: capturedDataUrl };
      var body = buildPostBody({
        details: description.trim(),
        screenshot: capturedDataUrl,
        metaUrl: (deps.window && deps.window.location && deps.window.location.href) || "",
        metaUserAgent: (deps.window && deps.window.navigator && deps.window.navigator.userAgent) || "",
        metaBuildSha: resolvedBuildSha(),
        tags: ["bug"],
        addedBy: "web",
        clientKey: cfg.idempotentSubmit ? submissionKey : "",
        screenshotError: captureError,
      });
      var payload = JSON.stringify(body);
      var shotBytes = dataUrlBytes(capturedDataUrl);
      ui.setStatus(shotBytes
        ? "Sending… uploading a " + formatBytes(shotBytes) + " screenshot."
        : "Sending…");
      var slowTimer = setTimer(function () {
        ui.setStatus("Still sending… the screenshot is large, or the connection is slow.");
      }, cfg.slowNoticeMs);

      var startedAt = nowFn();
      raceSubmit(payload).then(function (outcome) {
        clearTimer(slowTimer);
        ui.setStatus(null);

        if (outcome.timedOut) {
          // The report may well be stored: a server can answer a POST the
          // browser then never acts on (leap-timesheet bug-20260911-222709: row
          // stored, 201 in 296 ms, dialog frozen on "Submitting…" for a report
          // that HAD been filed). So this must not read as "it failed". It can
          // only promise a retry is safe if the server dedupes on clientKey.
          var secs = Math.round(cfg.submitTimeoutMs / 1000);
          emit("submit-timeout", {
            elapsedMs: nowFn() - startedAt,
            payloadBytes: payload.length,
            idempotent: !!cfg.idempotentSubmit,
            path: (win && win.location && win.location.pathname) || "/",
            version: resolvedBuildSha(),
            message: "no response after " + Math.round(nowFn() - startedAt) + "ms; payload " + payload.length + " bytes",
          });
          ui.showError(cfg.idempotentSubmit
            ? "No answer from the server after " + secs + " seconds. Your report may already be filed — " +
              "Submit again to be sure; it won't be filed twice."
            : "No answer from the server after " + secs + " seconds. Your report may or may not have been " +
              "filed — submitting again could file it twice.");
          return;
        }
        if (outcome.error) {
          ui.showError("Network error: " + (outcome.error.message || outcome.error));
          return;
        }
        var resp = outcome.response;
        if (!resp || !resp.ok) {
          var jp;
          try { jp = resp && resp.json ? Promise.resolve(resp.json()) : Promise.resolve({}); }
          catch (e) { jp = Promise.resolve({}); }
          return jp.catch(function () { return {}; }).then(function (data) {
            var msg = (data && (data.error && (data.error.message || data.error))) ||
                      ("Could not submit (HTTP " + (resp && resp.status) + ")");
            ui.showError(String(msg));
          });
        }
        // Only close the modal this submit belongs to: if the reporter
        // cancelled and opened a fresh one meanwhile, leave theirs alone.
        if (mySeq === openSeq) closeModal();
        showToast("Bug report submitted — thank you!");
      });
    }

    function openModal() {
      if (deps.document.getElementById(modalId)) return;  // re-entry guard
      var mySeq = ++openSeq;
      submissionKey = newClientKey(win);
      attempt = null;
      captureError = "";
      // What is on screen NOW, before the form opens: focusing its textarea
      // brings up a phone's keyboard, which shrinks the visible area and may
      // scroll the page, and the capture runs after that.
      var area = visibleArea(win, deps.document);
      openScroll = win ? scrollPos(win) : null;

      var overlay = buildModal(deps.document, cfg, {
        onCancel: closeModal,
        onSubmit: function (description, ui) { onSubmit(description, ui, mySeq); },
      });
      deps.document.body.appendChild(overlay);
      inertBehind();
      lockScroll(true);

      if (escHandler && deps.document.removeEventListener) deps.document.removeEventListener("keydown", escHandler);
      escHandler = function (e) {
        if (!e || e.key !== "Escape") return;
        if (e.isComposing) return;                              // Escape cancelling an IME composition, not the dialog
        if (deps.document.getElementById(VIEWER_ID)) return;   // the viewer's Escape, not ours
        try { e.preventDefault && e.preventDefault(); } catch (_) {}   // and not the host dialog's
        closeModal();
      };
      deps.document.addEventListener && deps.document.addEventListener("keydown", escHandler);

      focusTextarea();

      // Capture in the background. Modal is excluded via ignoreElements,
      // so this is safe even though the modal is now visible.
      var hintEl = deps.document.getElementById(modalId + "-hint");
      var previewEl = deps.document.getElementById(modalId + "-preview");
      captureScreenshotDetailed(deps, cfg, area).then(function (r) {
        if (!r.dataUrl) reportCaptureFailure(r);
        // The modal may have been closed (or closed and reopened) mid-capture;
        // this result belongs to the one that asked for it, and only that one.
        if (mySeq !== openSeq || !deps.document.getElementById(modalId)) return;
        var dataUrl = r.dataUrl;
        capturedDataUrl = dataUrl; originalDataUrl = dataUrl; markStrokes = [];
        captureError = dataUrl ? describePartialCapture(r) : describeCaptureFailure(r);
        if (dataUrl) {
          if (previewEl) {
            previewEl.src = dataUrl; previewEl.style.display = "block";
            if (cfg.markup !== false) {
              previewEl.style.cursor = "zoom-in";
              previewEl.title = "Click to zoom and mark up";
              previewEl.addEventListener("click", function () {
                openViewer(deps, originalDataUrl, { annotate: true, strokes: markStrokes, onDone: applyMarkup });
              });
            }
          }
          if (hintEl) { hintEl.textContent = captureError ? "Screenshot captured, but " + captureError + "." : "Screenshot captured."; }
        } else {
          if (hintEl) { hintEl.textContent = "Screenshot unavailable (" + captureError + ") — you can still submit text."; }
        }
      });
    }

    // Keep the button, report form, viewer and toast clickable above a modal
    // <dialog>: move them into the topmost open modal dialog, and back to <body>
    // when none is open. They are position:fixed, so they stay where they are,
    // and a moved node keeps its listeners.
    var reachableStarted = false;
    function followDialogs() {
      var doc = deps.document;
      if (!doc || !doc.body) return;
      var host = topModalDialog(doc) || doc.body;
      var movedModal = false;
      [btnId, modalId, VIEWER_ID, toastId].forEach(function (id) {
        var n = doc.getElementById(id);
        if (n && n.parentNode !== host) {
          host.appendChild(n);
          if (id === modalId) movedModal = true;
        }
      });
      var form = doc.getElementById(modalId);
      // The page behind follows the form wherever it went — moved here, or
      // pulled back out of a removed dialog before this ran.
      if (form && form.parentNode !== inertHost) inertBehind();
      // The form went without closeModal (the host cleared the page): end it
      // properly, or the page would stay inert and unscrollable.
      if (!form && (scrollLocked || inerted.length)) closeModal();
      // openModal focused the textarea while the form was still outside the
      // dialog (inert), and moving a node drops focus: give it back.
      if (movedModal) {
        focusTextarea();
      }
    }
    function keepReachable() {
      if (reachableStarted || cfg.reachOverDialogs === false) return;
      var MO = opts.MutationObserver || (win && win.MutationObserver);
      var doc = deps.document;
      if (!MO || !doc || !doc.documentElement || !doc.body) return;
      reachableStarted = true;
      // `open` toggling and child changes across the whole tree. Children on
      // <body> alone missed a dialog that a framework (React) unmounts deep
      // inside the page while it is open: the button went with it until a
      // reload (1.6.9, the seal viewer). A pass is cheap - up to four
      // getElementById calls per removed element, then one dialog[open] query -
      // and moving our own nodes re-triggers at most one no-op pass.
      var mo = new MO(function (records) {
        // A dialog taken out of the page with our nodes still inside (itself or
        // with an ancestor) would take them with it: pull them back out before they are gone for good.
        (records || []).forEach(function (rec) {
          (rec.removedNodes ? Array.prototype.slice.call(rec.removedNodes) : []).forEach(function (n) {
            if (!n || n.nodeType !== 1 || !n.querySelector) return;
            [btnId, modalId, VIEWER_ID, toastId].forEach(function (id) {
              if (doc.getElementById(id)) return;   // still attached: nothing to rescue, skip the subtree walk
              var inside = null;
              try { inside = n.querySelector('[id="' + id + '"]'); } catch (e) {}
              if (inside) doc.body.appendChild(inside);
            });
          });
        });
        followDialogs();
      });
      mo.observe(doc.documentElement, { subtree: true, childList: true, attributes: true, attributeFilter: ["open"] });
      followDialogs();
    }

    function ensureButton() {
      if (!deps.document || !deps.document.body) return null;
      var existing = deps.document.getElementById(btnId);
      if (existing) return existing;
      var btn = buildButton(deps.document, cfg, openModal, deps.window);
      deps.document.body.appendChild(btn);
      // A window that shrinks after mount (a resize, a rotation, a docked
      // devtools pane) would strand a dragged button just as a reload did.
      if (deps.window && deps.window.addEventListener) {
        deps.window.addEventListener("resize", function () {
          var b = deps.document.getElementById(btnId);
          if (b) placeFromStorage(b, cfg, deps.window, b._size);
        });
      }
      return btn;
    }

    function inject() {
      ensureButton();
      keepReachable();
    }

    return {
      inject: inject,
      openModal: openModal,
      closeModal: closeModal,
      applyMarkup: applyMarkup,
      _config: cfg,
      _state: function () {
        return {
          buttonMounted: !!deps.document.getElementById(btnId),
          modalOpen: !!deps.document.getElementById(modalId),
          capturedDataUrl: capturedDataUrl,
          submissionKey: submissionKey,
          captureError: captureError,
        };
      },
    };
  }

  function init() {
    if (typeof window === "undefined" || typeof document === "undefined") return null;
    var flag = "__bugReportControllerInited";
    if (window[flag]) return window[flag];
    var controller = createController({
      document: document,
      window: window,
      fetch: window.fetch.bind(window),
      html2canvas: window.html2canvas,
      config: window.BugReportConfig || {},
    });
    window[flag] = controller;
    controller.inject();
    return controller;
  }

  return {
    init: init,
    createController: createController,
    buildPostBody: buildPostBody,
    clampPos: clampPos,
    isBlankCanvas: isBlankCanvas,
    canvasState: canvasState,
    captureScreenshot: captureScreenshot,
    captureScreenshotDetailed: captureScreenshotDetailed,
    visibleArea: visibleArea,
    describeCaptureFailure: describeCaptureFailure,
    describePartialCapture: describePartialCapture,
    openViewer: function (src, opts) {
      return openViewer({ document: document, window: window }, src, opts);
    },
    // for tests: the viewer with injected deps
    _openViewer: openViewer,
    fitView: fitView,
    zoomAbout: zoomAbout,
    pinchView: pinchView,
    dataUrlBytes: dataUrlBytes,
    formatBytes: formatBytes,
    newClientKey: newClientKey,
    encodeCanvasUnderCap: encodeCanvasUnderCap,
    plainColors: plainColors,
    plainColorsInClone: plainColorsInClone,
    mergeConfig: mergeConfig,
    positionCss: positionCss,
    DEFAULTS: DEFAULTS,
    VIEWER_ID: VIEWER_ID,
  };
});
