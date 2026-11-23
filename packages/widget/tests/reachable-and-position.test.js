// 1.6.0: (g) the widget's UI stays reachable over a modal <dialog>, and (e)
// position accepts {top, left} as well as {bottom, right}.
//
// (g) comes from the seal viewer's keepBugUiReachable(). showModal() puts the
// dialog in the top layer and makes the rest of the page inert, so the bug
// button cannot be clicked however high its z-index — and a dialog is when
// somebody most wants to report what they see. The fix is to be INSIDE the open
// dialog. The browser half (inertness, :modal, position:fixed staying put) is
// proved by a consuming app's Playwright suite; what is tested here is the
// decision of where each node lives, with a fake DOM and a fake MutationObserver.

const test = require("node:test");
const assert = require("node:assert/strict");
const widget = require("../src/bug-report.js");
const { makeDom } = require("./fake-dom.js");

function setup(config) {
  const doc = makeDom();
  const observers = [];
  function MO(cb) { this.cb = cb; this.targets = []; this.observe = (t, o) => this.targets.push([t, o]); observers.push(this); }
  const win = { location: { href: "x" }, navigator: {}, MutationObserver: MO, addEventListener() {} };
  const ctl = widget.createController({ document: doc, window: win, fetch: () => new Promise(() => {}), html2canvas: null, config });
  ctl.inject();
  const fire = (records) => observers.forEach((o) => o.cb(records || []));
  const dialog = (modal) => {
    const d = doc.createElement("dialog");
    d._modal = modal;
    doc.body.appendChild(d);
    return d;
  };
  const open = (d) => { d.setAttribute("open", ""); fire(); };
  const close = (d) => { d.removeAttribute("open"); fire(); };
  return { doc, ctl, observers, fire, dialog, open, close, btn: () => doc.getElementById("bug-report-button") };
}

test("the button moves into an open modal dialog, and back to <body> when it closes", () => {
  const s = setup();
  const d = s.dialog(true);
  assert.equal(s.btn().parentNode, s.doc.body);
  s.open(d);
  assert.equal(s.btn().parentNode, d, "inside the dialog, so the top layer cannot make it inert");
  s.close(d);
  assert.equal(s.btn().parentNode, s.doc.body);
});

test("the report form, the viewer and the toast follow too, including when created while a dialog is open", () => {
  const s = setup();
  const d = s.dialog(true);
  s.open(d);
  s.ctl.openModal();                                // appended to <body> by the widget
  const viewer = s.doc.createElement("div"); viewer.id = widget.VIEWER_ID; s.doc.body.appendChild(viewer);
  const toast = s.doc.createElement("div"); toast.id = "bug-report-toast"; s.doc.body.appendChild(toast);
  s.fire();                                         // the observer's childList callback
  assert.equal(s.doc.getElementById("bug-report-modal").parentNode, d);
  assert.equal(viewer.parentNode, d);
  assert.equal(toast.parentNode, d);
  s.close(d);
  assert.equal(s.doc.getElementById("bug-report-modal").parentNode, s.doc.body);
  assert.equal(viewer.parentNode, s.doc.body);
});

test("a dialog already open at mount time is handled", () => {
  const doc = makeDom();
  const d = doc.createElement("dialog"); d._modal = true; d.setAttribute("open", ""); doc.body.appendChild(d);
  function MO() { this.observe = () => {}; }
  const ctl = widget.createController({ document: doc, window: { MutationObserver: MO, location: {}, navigator: {} }, fetch() {}, html2canvas: null });
  ctl.inject();
  assert.equal(doc.getElementById("bug-report-button").parentNode, d);
});

test("the textarea is focused again once the form has been moved into the dialog (moving drops focus)", () => {
  const s = setup();
  const d = s.dialog(true);
  s.open(d);
  s.ctl.openModal();
  const ta = s.doc.getElementById("bug-report-modal-textarea");
  let focused = 0;
  ta.focus = () => { focused++; };
  s.fire();
  assert.equal(s.doc.getElementById("bug-report-modal").parentNode, d);
  assert.equal(focused, 1);
  s.fire();
  assert.equal(focused, 1, "not again on a pass that moved nothing");
});

test("a non-modal dialog does not block the page, so nothing moves", () => {
  const s = setup();
  const d = s.dialog(false);
  s.open(d);
  assert.equal(s.btn().parentNode, s.doc.body);
});

test("with two modal dialogs open, the last one wins", () => {
  const s = setup();
  const a = s.dialog(true), b = s.dialog(true);
  s.open(a); s.open(b);
  assert.equal(s.btn().parentNode, b);
  s.close(b);
  assert.equal(s.btn().parentNode, a);
});

test("a dialog removed from the page with our nodes inside does not take them with it", () => {
  const s = setup();
  const d = s.dialog(true);
  s.open(d);
  assert.equal(s.btn().parentNode, d);
  d.remove();                                       // the host tears the dialog down, still open
  assert.equal(s.btn(), null, "gone with the dialog, as without the rescue");
  s.fire([{ removedNodes: [d] }]);
  assert.ok(s.btn(), "pulled back out");
  assert.equal(s.btn().parentNode, s.doc.body);
});

test("a modal dialog deep inside the page, removed with an ancestor (a React unmount), does not take the button", () => {
  const s = setup();
  const root = s.doc.createElement("div"), wrap = s.doc.createElement("div");
  s.doc.body.appendChild(root); root.appendChild(wrap);
  const d = s.doc.createElement("dialog"); d._modal = true; wrap.appendChild(d);
  s.open(d);
  assert.equal(s.btn().parentNode, d);
  wrap.remove();
  // Deliver the record only to an observer that watches childList on its target, as a browser
  // would: the removal is inside #root, not a child of <body>.
  const rec = { type: "childList", target: root, removedNodes: [wrap] };
  const contains = (a, b) => { for (let n = b; n; n = n.parentNode) if (n === a) return true; return false; };
  s.observers.forEach((o) => {
    if (o.targets.some(([t, opt]) => opt.childList && (t === root || (opt.subtree && contains(t, root))))) o.cb([rec]);
  });
  assert.ok(s.btn(), "pulled back out of the removed subtree");
  assert.equal(s.btn().parentNode, s.doc.body);
});

test("the idPrefix is honoured", () => {
  const doc = makeDom();
  const d = doc.createElement("dialog"); d._modal = true; d.setAttribute("open", ""); doc.body.appendChild(d);
  function MO() { this.observe = () => {}; }
  widget.createController({ document: doc, window: { MutationObserver: MO, location: {}, navigator: {} }, fetch() {}, html2canvas: null,
    config: { idPrefix: "sv-bug" } }).inject();
  assert.equal(doc.getElementById("sv-bug-button").parentNode, d);
});

test("reachOverDialogs: false switches it off (no observer, no move)", () => {
  const s = setup({ reachOverDialogs: false });
  assert.equal(s.observers.length, 0);
  const d = s.dialog(true);
  s.open(d);
  assert.equal(s.btn().parentNode, s.doc.body);
});

test("a host with no MutationObserver (or no dialogs support) just mounts as before", () => {
  const doc = makeDom();
  const ctl = widget.createController({ document: doc, window: { location: {}, navigator: {} }, fetch() {}, html2canvas: null });
  ctl.inject();
  assert.equal(doc.getElementById("bug-report-button").parentNode, doc.body);
});

// ── (e) position ────────────────────────────────────────────────────────────

test("position: the default is bottom-right", () => {
  const cfg = widget.mergeConfig({});
  assert.deepEqual(cfg.position, { bottom: 20, right: 20 });
  assert.deepEqual(Object.keys(widget.positionCss(cfg.position)).sort(), ["bottom", "right"]);
});

test("position: {top, left} does not also keep the default bottom/right", () => {
  const cfg = widget.mergeConfig({ position: { top: 80, left: 12 } });
  assert.deepEqual(cfg.position, { top: 80, left: 12 });
  const css = widget.positionCss(cfg.position);
  assert.deepEqual(Object.keys(css).sort(), ["left", "top"]);
  assert.match(css.top, /safe-area-inset-top.*\+ 80px/);
  assert.match(css.left, /safe-area-inset-left.*\+ 12px/);
});

test("position: axes mix, and a single named axis keeps the other default", () => {
  assert.deepEqual(widget.mergeConfig({ position: { top: 10 } }).position, { right: 20, top: 10 });
  assert.deepEqual(widget.mergeConfig({ position: { bottom: 5, left: 7 } }).position, { bottom: 5, left: 7 });
  assert.deepEqual(widget.mergeConfig({ position: { bottom: 30 } }).position, { right: 20, bottom: 30 });
});

test("position: the mounted button gets top/left styles", () => {
  const doc = makeDom();
  widget.createController({ document: doc, window: { location: {}, navigator: {} }, fetch() {}, html2canvas: null,
    config: { position: { top: 80, left: 12 } } }).inject();
  const css = doc.getElementById("bug-report-button").style.cssText;
  assert.match(css, /top:calc\(env\(safe-area-inset-top, 0px\) \+ 80px\)/);
  assert.match(css, /left:calc\(env\(safe-area-inset-left, 0px\) \+ 12px\)/);
  assert.doesNotMatch(css, /bottom:|right:/);
});

test("DEFAULTS exposes the viewer id unchanged and the new options at their opt-in values", () => {
  assert.equal(widget.VIEWER_ID, "bug-report-viewer");
  assert.equal(widget.DEFAULTS.submitTimeoutMs, 30000);
  assert.equal(widget.DEFAULTS.idempotentSubmit, false);
  assert.equal(widget.DEFAULTS.html2canvasUrl, "");
  assert.equal(widget.DEFAULTS.reachOverDialogs, true);
});
