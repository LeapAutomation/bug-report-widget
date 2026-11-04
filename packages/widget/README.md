# `bug-report-widget` — the frontend

Self-injecting vanilla-JS module. One `<script>` tag, you're done.

## Install

### Copy-paste (any stack)

```bash
curl -O https://raw.githubusercontent.com/LeapAutomation/bug-report-widget/main/packages/widget/src/bug-report.js
curl -O https://raw.githubusercontent.com/LeapAutomation/bug-report-widget/main/packages/widget/src/html2canvas.min.js
```

Drop both into your `static/` (or `public/`) directory and reference them:

```html
<script src="/static/html2canvas.min.js" defer></script>
<script src="/static/bug-report.js" defer></script>
```

### npm (Node consumers)

```bash
npm install github:LeapAutomation/bug-report-widget#main
```

Then either bundle `bug-report-widget/src/bug-report.js` with your usual tool, or copy the two files to your public dir at build time.

## Configure

Set `window.BugReportConfig` **before** the script loads if you want to override defaults:

```html
<script>
  window.BugReportConfig = {
    endpoint: "/api/bugs",
    idPrefix: "bug-report",
    buildSha: window.MY_APP_BUILD_SHA || "",  // or () => string
    position: { bottom: 20, right: 20 },  // before the user drags it; { top: 80, left: 12 } works too
    buttonSize: 52,                         // px diameter
    title: "Report a bug",                  // modal heading
    theme: {
      accent: "#007AFF",
      buttonBg: "#ffffff",
      // … see DEFAULTS in src/bug-report.js for the full list
    },
  };
</script>
<script src="/static/html2canvas.min.js" defer></script>
<script src="/static/bug-report.js" defer></script>
```

The button is draggable; where the user leaves it is remembered in `localStorage`
(`storageKey`, per origin and per browser) and clamped back inside the viewport on
load and on every resize, so it can never be restored off-screen.

### Options added in 1.6.0 (all optional)

| Option | Default | What it does |
|---|---|---|
| `submitTimeoutMs` | `30000` | The POST is aborted and the dialog gives the Submit button back after this long. A total cap, not an inactivity timer: raise it for big uploads on slow links. 0/NaN/unset = the default. |
| `slowNoticeMs` | `8000` | After this long the status line says "Still sending…". |
| `idempotentSubmit` | `false` | Send `clientKey` (a UUID per report: the same on every retry of unchanged text, new if the text or marks were edited). **Only turn this on if your server dedupes on it** (the reference backends ignore it) — it decides whether the timeout message may say "it won't be filed twice". |
| `onClientEvent` | `null` | `(kind, detail) => void`, for apps that log client-side trouble. `"screenshot-failure"` (`detail.reason`, `.message`, `.ms`, `.lateMs`, `.width/.height`, `.path`, `.version`) and `"submit-timeout"` (`.elapsedMs`, `.payloadBytes`, `.idempotent`, `.message`, `.path`, `.version`). Exceptions and rejected promises from it are swallowed. leap-timesheet POSTs these to `/api/client-events`. |
| `html2canvasUrl` | `""` | If `window.html2canvas` is absent when a screenshot is needed, inject this script on the first click and wait for it (inside `captureTimeoutMs`). Keeps ~200 KB off pages where nobody reports. Empty = never inject. `capture-engine.js` only wraps an html2canvas already loaded when it runs, so it is not applied to a lazily loaded one. |
| `captureLateWatchMs` | `30000` | After a capture times out, how long to keep watching for it to finish, so the event can say how long it really took. |
| `reachOverDialogs` | `true` | See below. `false` opts out. |

**Submit never freezes.** While the POST is in flight a `role="status"` line says what is
happening ("Sending… uploading a 1.2 MB screenshot", then "Still sending…"). If nothing
answers within `submitTimeoutMs` the request is aborted, the button comes back, the text is
kept, and the dialog says the report may already be filed. With `idempotentSubmit` it adds
that submitting again will not file it twice; without, it says submitting again could.
(Why: leap-timesheet `bug-20260911-222709` — the server stored the report and answered 201 in
296 ms, the browser never acted on it, and the reporter stared at "Submitting…" for a report
that had been filed.) Escape closes the dialog (not while the screenshot viewer is open — that
Escape closes the viewer), and the dialog has `role="dialog"`, `aria-modal`.

**Why there is no screenshot.** `captureScreenshotDetailed(deps, cfg)` resolves
`{dataUrl, reason, detail, ms, width, height, late}`; `reason` is `null` on success, else one
of `no-library`, `timeout`, `render-error`, `blank`, `encode-error`, `too-large`, `tainted`
(the page holds cross-origin content the browser will not let us read). The dialog says
"Screenshot unavailable (why) — you can still submit text.", the POST carries `screenshotError`,
and `onClientEvent` is told. `captureScreenshot` still returns `dataUrl | null`.

**Reachable over a modal `<dialog>`.** `dialog.showModal()` puts the dialog in the top layer
and makes the rest of the page inert, so a 🐛 button on `<body>` cannot be clicked however
high its z-index. The widget watches for an open modal dialog and moves its button, report
form, viewer and toast into the topmost one (and back to `<body>` when it closes; a dialog
removed from the page with them inside gives them back first). They are `position: fixed`, so
they stay where they are. Non-modal dialogs are ignored. If your app already does this itself it
is harmless, and `reachOverDialogs: false` turns the built-in off.

**What the screenshot covers.** What is on screen when the 🐛 is tapped: the visual viewport
(`window.visualViewport`, falling back to the layout viewport), measured before the form opens, so a
phone's keyboard coming up cannot shrink or move it. Not `documentElement.clientHeight`: iOS Safari
keeps that at its smallest viewport, which left the bottom ~40px of the screen out once the toolbar
collapsed (LeapHQ `bug-20261020-213458`, 1.6.7). A pinch-zoomed page gives the zoomed part.
`visibleArea(window, document)` returns the area. The page shows faintly under iOS 26's floating
toolbar; no API reports that strip and it is behind the browser's controls, so it is not captured.

**The page behind the form cannot be used.** While the form is open its siblings are `inert` (no
taps, no focus, hidden from a screen reader, so Tab stays in the form), and a touch or wheel scroll is
stopped unless it scrolls something inside the form; the form's own scroll does not chain into the
page (`overscroll-behavior: contain`). Inside a modal `<dialog>` it is the dialog's other children.
The screenshot viewer and the toast stay usable, and a node the app made `inert` itself is left as
it was. A phone's keyboard may still scroll the page to show the field (asking the browser not to
leaves the field under the keyboard on iOS), so on close the page is put back where it was. Before
1.6.7, swiping the backdrop on an iPhone scrolled the page underneath.

**DOM ids.** `idPrefix` names the button (`<prefix>-button`), the report form
(`<prefix>-modal` and its parts) and the toast. The screenshot viewer's id is **always**
`bug-report-viewer` (its buttons `bug-report-viewer-done` etc.), whatever the prefix: apps'
own tests select it, so it is not derived from `idPrefix`.

## What ships

| Symbol | Type | Purpose |
|---|---|---|
| `window.BugReportWidget.init()` | function | Mounts the widget. Called automatically unless `window.__bugReportSkipAutoInit` is set. |
| `window.BugReportWidget.createController(opts)` | factory | Headless controller; pass `{document, window, fetch, html2canvas, config}`. Useful for tests. |
| `window.BugReportWidget.buildPostBody(opts)` | pure fn | Returns the request body sent to `/api/bugs`. Wire-format-stable. |
| `window.BugReportWidget.isBlankCanvas(canvas)` | pure fn | Safari fallback signal — true if the canvas has no real pixels. |
| `window.BugReportWidget.captureScreenshot(deps, cfg)` | async | Returns a data URL or `null` (fail/timeout/blank). |
| `window.BugReportWidget.captureScreenshotDetailed(deps, cfg, area?)` | async | `{dataUrl, reason, …}` — says why there is no image. `area` (a `visibleArea()`) crops to an area measured earlier. |
| `window.BugReportWidget.visibleArea(window, document)` | pure fn | What is on screen, in page coordinates, or `null`. |
| `window.BugReportWidget.describeCaptureFailure(result)` | pure fn | The one-line, human-readable reason. |
| `window.BugReportWidget.openViewer(src, {annotate, onDone})` | fn | The zoom/markup viewer, standalone. `annotate: false` is view-only. |

## Zoom and mark up

Clicking the screenshot preview in the modal opens a viewer: zoom (wheel, `+`/`−`, Fit,
100%), pan (✋ Move), and ✏️ pen / ▭ box marks in red, with undo and clear. **Done** replaces
the capture with the marked-up image, which is what gets POSTed. Set
`BugReportConfig.markup = false` to turn it off.

`BugReportWidget.openViewer(src, {annotate: false})` opens the same viewer view-only, for a
triage screen that wants to zoom a filed screenshot.

On a touch screen, with ✋ Move one finger pans and two fingers pinch-zoom; with ✏️ Pen or
▭ Box a second finger is ignored, so a resting palm cannot spoil a mark. Marks are drawn on
a transparent canvas above the image (the image is never repainted as you draw) and are
composited into the file once, on Done. The toolbar keeps clear of a notch.

## A more faithful capture (optional)

html2canvas re-implements CSS layout, and on some pages that comes out wrong. Two extra files
put the browser's own renderer ([html-to-image](https://github.com/bubkoo/html-to-image), MIT)
in front of it, with html2canvas as the fallback:

```html
<script src="/static/html2canvas.min.js" defer></script>
<script src="/static/html-to-image.js" defer></script>
<script src="/static/capture-engine.js" defer></script>
<script src="/static/bug-report.js" defer></script>
```

The screenshot is drawn on the page's own background: the first opaque computed
`background-color` of `<body>`, then `<html>` (white if neither is opaque; the browser's dark
canvas colour if the page declares `color-scheme: dark`). Gradients and images are not rendered,
so a page whose background is only a gradient gets its nearest opaque colour.

Its cost is per DOM node, so a very large page wants `captureTimeoutMs: 20000` in
`BugReportConfig`. It does not draw `<video>` frames (nor does html2canvas) or cross-origin frames.

## Modern colour functions

html2canvas 1.4.1 throws on any colour function newer than `rgb()`/`hsl()`, and browsers report a
`color-mix()` result as `color(srgb …)`, so one tinted badge used to fail the whole capture
("Attempting to parse an unsupported color function \"color\""). Since 1.6.3 the widget hands
html2canvas an `onclone` hook that rewrites every `color()`, `color-mix()`, `oklch()`, `oklab()`,
`lab()`, `lch()` and `hwb()` value in the cloned page as the `rgba()` the browser itself resolves it
to, `::before`/`::after` included.

## Known gaps (as of 1.6.8)

Found while rolling the widget out to seven apps (2026-10-09/12). Not bugs worth blocking on; each is
a real edge a later version should close.

- **A new `clientKey` is minted if the screenshot arrives after the first submit attempt** (it counts as
  a changed report). With `idempotentSubmit` on, a stalled first submit sent before the capture finished,
  followed by a retry, can file twice. Consumers' e2e wait for the capture first.
- **`reachOverDialogs` moves the button, modal and viewer into an open modal `<dialog>`.** If that dialog
  has a CSS `transform`, `position: fixed` children are positioned against the dialog, not the viewport.
  Proven in a real browser only for apps without a transformed dialog (seal viewer, timesheet).
- **The 30 s submit timeout is a total cap, not an inactivity timer** (a slow but progressing upload
  of a big screenshot can still hit it; raise `submitTimeoutMs`).
- **A `<video>` is one still frame, or a dark tile.** Since 1.6.6 the engine puts an `<img>` of the
  video's current frame in its place for the capture (hiding, not removing, the video), fitted as its
  `object-fit` says (`contain`, `cover`, otherwise `fill`). A video the browser will not let a script read
  (cross-origin without CORS, e.g. a signed blob URL) gets a dark tile reading "Video not in screenshot
  (cross-origin)"; before 1.6.6 such a video rejected the whole html-to-image capture. A cross-origin frame
  still comes out blank under a plain "Screenshot captured." (it cannot be read, so it is not counted).
  Same-origin frames are drawn; one that could not be is named in the dialog (1.6.4).
- **With `capture-engine.js`, a page the keyboard has scrolled is drawn where it is now.** html2canvas
  is told the scroll position measured before the form opened (1.6.7); html-to-image renders the live
  page and has no such option, so fixed elements can come out shifted. The capture usually finishes
  before the keyboard moves anything.
- **With a phone's keyboard up, the form may be taller than what is visible**, and scrolling outside
  something that scrolls is blocked (1.6.7), so Submit can need the keyboard dismissed first.
  Checked in the iOS Simulator for the default form, which fits; a tall form was not.
- **The capture engine's cost is per DOM node.** It skips everything below the fold, but a page scrolled
  far down still clones everything above; raise `captureTimeoutMs` (the apps use 20000).
- **The fallback engine draws a worse picture, and says nothing when it is used.** When html-to-image
  rejects a page (an `<img>` with no `src` did, until 1.6.5) the widget falls back to html2canvas, which
  shows a decimal marker beside `<summary>`, flat blocks behind text and unstyled `<select>`s. The report
  looks messy rather than failing. The cause is visible only by logging which engine drew it.
- **A capture in a hidden browser tab stalls** (Chrome throttles rendering) and ends in "capture timed out".
  Nothing to fix; know it when testing with automation.
- **Only a fixed list of colour properties is rewritten** (`color`, backgrounds, borders, outline, text
  decoration and stroke, shadows, `fill`/`stroke`, `list-style-image`): the ones html2canvas reads, and a few
  more. A colour function nested more than one level deep inside another is left as it was.
- **With `capture-engine.js`, an absolutely positioned element sits off by `<body>`'s margin** (8px by
  default) when it is placed against the page rather than a positioned parent: the cropped clone's body is
  its containing block. Seen in the 1.6.8 browser check; it was the same with the transform before it.
- **Only the capture engine has a browser test** (`tests/browser`, since 1.6.8: where fixed and scrolled
  content lands in the picture). Dialog reparenting, pinch-zoom and touch are proven only by the consuming
  apps' Playwright suites (stoppageviewer's is the reference).

## Opting elements out of capture

Mark any DOM element with `data-bug-report-exclude` and html2canvas will skip it:

```html
<input type="password" data-bug-report-exclude="">
<div class="sensitive-widget" data-bug-report-exclude=""> … </div>
```

The widget's own button and modal are already excluded internally — you don't need to mark those.

## What gets POSTed

`POST /api/bugs` with `Content-Type: application/json`:

```json
{
  "title": "first line of details, ≤100 chars",
  "details": "...",
  "screenshot": "data:image/png;base64,...",
  "metaUrl": "...",
  "metaUserAgent": "...",
  "metaBuildSha": "abc1234",
  "tags": ["bug"],
  "addedBy": "web"
}
```

Two further fields are optional and only present when they apply: `screenshotError` (a string:
why there is no screenshot, or since 1.6.4 why part of it may be missing — an embedded frame that
could not be drawn) and `clientKey` (only with `idempotentSubmit`). A server
that does not know them ignores them.

The full request/response contract lives in [`../spec/openapi.yaml`](../spec/openapi.yaml). The Python and Node backend libs implement it; the cross-stack e2e suite in `../../e2e` exercises every reference impl with the same scenarios.

## Tests

```bash
cd packages/widget
npm test
```

Pure helpers (`buildPostBody`, `isBlankCanvas`, `pinchView`, …) and the controller and viewer, driven through a small fake DOM (`tests/fake-dom.js`, no jsdom), are tested in Node with the built-in test runner.

Where things land in the screenshot is tested in real Chromium and WebKit (Playwright, Python), separately:

```bash
cd packages/widget
pip install -r tests/browser/requirements.txt
python -m playwright install chromium webkit
python -m pytest tests/browser
```

Real touch and top-layer inertness are proved by each consuming app's own Playwright suite.
