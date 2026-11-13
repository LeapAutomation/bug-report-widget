# Changelog

All notable changes to this monorepo.

## Ownership — 2026-11-04

- The repository moved from `kmoy007/bug-report-widget` to `LeapAutomation/bug-report-widget`; GitHub
  redirects the old URLs. Copyright is now Leap Automation Ltd (LICENSE, new NOTICE); package `author`
  and `repository` fields and README install snippets point at the new home. No code, version or tag
  changed: `backends-v2.1.1` and the widget commits the apps pin are the same objects.

## widget v1.6.8 — 2026-10-26

From the daily report (hq.helloleap.ai/daily-report): its floating "Run report again" button was
missing from bug screenshots.

- **Fix: `position: fixed` elements were left out of, or misplaced in, the screenshot when the page was
  scrolled.** `capture-engine.js` cropped by putting `transform: translate(-x, -y)` on the cloned
  `<body>`; a transformed element is the containing block of its fixed descendants, so they were laid
  out against the body, not the screen. Scrolled mid-page, a bottom-right button was not in the picture
  at all; at the top it was 8px off (the body's margin). The crop is now `position: relative` with
  `top`/`left`, which leaves fixed elements placed against the canvas, i.e. the screen. The clone's
  computed `inset-block`/`inset-inline` are cleared, since Chromium serialises them after `top` and they
  would win. In-flow content is unchanged. The html2canvas fallback already drew fixed elements right.
- **New: a browser test** (`packages/widget/tests/browser`, Python + Playwright, Chromium and WebKit) that
  captures a page scrolled to the top, mid-page and the bottom and checks a fixed button, a fixed bar
  and a scrolled marker land where the page shows them. See the widget README for how to run it.
- No option and no wire change.

## widget v1.6.7 — 2026-10-20

From LeapHQ bug-20261020-213458, filed on an iPhone (iOS 26 Safari); both halves reproduced in the iOS
Simulator first.

- **Fix: the screenshot left out the bottom of the screen on iOS.** The capture was cropped to
  `documentElement.clientHeight`, which iOS Safari keeps at its smallest viewport (toolbar shown):
  714px against 754px on screen once the toolbar collapsed on scroll. It now crops to the visual
  viewport (`visibleArea()`: `visualViewport` offset and size, else the layout viewport), lays the
  clone out at the layout width and the taller height (where `position: fixed; bottom: 0` sits), and
  places same-origin frames relative to the visible area, which also makes a pinch-zoomed page come out
  as the zoomed part. The area and the scroll position are measured in `openModal` BEFORE the form
  opens and passed to `captureScreenshotDetailed(deps, cfg, area)`, because focusing the textarea
  brings up the keyboard, which shrinks the visible area and scrolls the page before the capture runs.
- **Fix: the page behind the form could be used.** On an iPhone, swiping the dimmed backdrop scrolled
  the page underneath. While the form is open its siblings (the dialog's, inside a modal `<dialog>`)
  are `inert`, and `touchmove`/`wheel` outside anything that scrolls inside the form are cancelled
  (listeners, not `overflow: hidden` on `<html>`, which drops a desktop scrollbar and reflows the page
  mid-capture); the form's box has `overscroll-behavior: contain`. The viewer and the toast are left
  usable, an app's own `inert` is left alone, and everything is released on close. iOS's keyboard still
  scrolls the page to show the field (`focus({preventScroll: true})` was tried and leaves the field
  under the keyboard), so close puts the scroll back where it was when the form opened. If the form
  leaves the page some other way (the host clears `<body>`), the dialog watcher ends it the same way,
  so the page cannot be left inert; a form pulled back out of a removed `<dialog>` makes `<body>`'s
  siblings inert again.
- No option and no wire change. The button is inert while the form is open (it was already under the
  backdrop).

## backends v2.2.0 — 2026-11-13

From Chris Hamblin's report bug-20261112-124952: it was fixed, and Ken had to tell Chris by hand. Closing
a report can now carry a note and email the person who filed it, and people are named throughout
(bug-report-py 2.2.0; bug-report-node passes the new fields through). All additive.

- **New: `create_blueprint(notifier=…, app_url=…)`.** `PATCH` takes `notify: true` (default false). On a
  change to `resolved` or `declined` the server emails the reporter once: subject `Fixed: <title>` or
  `Your bug report: <title> — not taken forward`; "Hi <first name>,", the note (URLs made links), signed with
  the closer's name, "You reported this on <date> from <page>", the report id. Reply-To is the closer.
  Reopening never emails. `notify` needs a non-empty `note` (400 otherwise, nothing changed). The status
  change and its audit row are written first and a failed send never undoes them; a second audit row
  (from = to = the new status) says `emailed Chris Hamblin <address>` or `email to … failed: <reason>`. The
  response gains `notified: {ok, to, error?}`; no notifier configured, or a report with no address, is
  `ok: false` with the reason (and the audit row). `GET /bugs/<id>` gains `canNotify` (a notifier is
  configured and the report has an address). An app that passes no notifier never emails.
- **New: `bug_report.email`** — `Notice`, `render(notice) -> (subject, html)` (everything escaped) and
  `AcsNotifier`, an Azure Communication Services Email sender modelled on leap-daily-report's, behind
  the `[email]` extra (`azure-communication-email`, `azure-identity`). `AcsNotifier.from_env()` reads
  `ACS_ENDPOINT` (managed identity; `AZURE_CLIENT_ID` picks a user-assigned one) or `ACS_CONNECTION_STRING`,
  `BUG_NOTIFY_SENDER` and `BUG_NOTIFY_DOMAINS`, and returns `None` when any is missing, so
  `notifier=AcsNotifier.from_env()` just does not email when unset. Fails closed: an address outside
  `BUG_NOTIFY_DOMAINS` is refused before anything is sent. SDK retries are off (`retry_total=0`) and the
  wait on the send is bounded (60 s).
- **New: people are named.** Reports gain `actorName` and audit rows `changedByName` (`""` when unknown;
  the UI derives "Chris Hamblin" from `chris.hamblin@…` the same way `bug_report.person_name` does).
  `actor_identity` may return `{"email": …, "name": …}` as well as a bare address, and now also stamps
  `changedBy`/`changedByName` on a status change, replacing the request body's claim, which read "admin"
  on every close. Without the hook the body's `actorName`/`changedBy`/`changedByName` are used, as before.
  `BugRecord.actor_name` and `AuditEntry.changed_by_name` default to `""`, so existing constructors and
  files on disk work unchanged.
- **Node:** accepts and stores `actorName`/`changedByName` (Azure Tables: `ActorName`, `ChangedByName`),
  serves `canNotify: false`, and ignores `notify` (no `notified`).
- Spec: `openapi.yaml`, `types.ts`, `types.py`, and `report-contract.json` (`optionalSummaryFields`,
  `optionalAuditFields`) updated; e2e contract tests cover the names on both backends.

## backends — bug-report-py 2.1.1, bug-report-node 1.1.1 — 2026-10-20

- **Fix: `FilesystemStore` wrote a report in place, so a concurrent read saw half of it.** `put_bug`
  (and every PATCH, which rewrites the report) truncated the file and then wrote it: a `get_bug`
  between the two raised `StoreError("could not read ...")`, `list_bugs` silently dropped the report,
  and a crash mid-write left a corrupt one (StoppageViewer CI flake, "could not read
  bug-20261020-043638-a558fe"; production stores are Azure Files mounts, where the window is wider).
  Both backends now write the report and the screenshot to a dot-prefixed `.tmp` beside the target,
  fsync it and rename it over the target, so a reader sees the old file or the new one. Temp files
  never match the `bug-*.json` listing and a crash's leftovers older than 10 minutes are swept when the
  store opens. A report that still will not parse is re-read up to 5 times (20 ms apart) before
  `get_bug` raises or `list_bugs` skips it. If the share refuses the rename (SMB can, while another
  handle holds the target open) the write falls back to the old in-place write and logs a warning.
  The audit file is appended to, never rewritten, and is unchanged. A new report or screenshot gets a plain write's mode (0666 & ~umask); a rewrite
  installs a new file, so a mode, owner or ACL set by hand on a report does not survive its next PATCH.
  Node: `new FilesystemStore(root, { readAttempts })` sets the re-read count. Known and accepted: the in-place fallback can still tear a read (and a screenshot read
  has no retry); an unparseable report costs ~80 ms each time it is read. Not verified on a live Azure Files
  mount by this change; the fallback is the guard.

## widget v1.6.6 — 2026-10-15

- **Fix: a cross-origin `<video>` sent the whole screenshot to the old engine, and the old engine
  draws a worse picture.** html-to-image clones a video by drawing it on a canvas and calling
  `toDataURL()`; a video from another origin without CORS (StoppageViewer's CCTV clip, a signed
  blob URL) taints that canvas, `toDataURL()` throws a SecurityError and the capture was rejected. The
  fallback, html2canvas, draws no video and piled the help panel's text one letter per line down the right
  edge (StoppageViewer bug-20261014-163549 and bug-20261014-231951). For the length of a capture the
  engine now stands in an `<img>` for every visible `<video>` (same class, inline style and size, and the
  video's computed position, inset, margins, display, transform and z-index, since a rule keyed on `video` does
  not match an `<img>`; put right before it; the video is hidden, not removed, so playback is not disturbed) and restores it on
  success and failure. The picture is the video's current frame, fitted by `object-fit`; if the browser
  will not let a script read it, a dark tile reading "Video not in screenshot (cross-origin)". Nothing
  about a video can reject a capture: a video with no frame yet gets a "Video not loaded yet" tile, and the
  page is put back by the engine's own 30 s timer if html-to-image never settles (the widget abandons a
  capture at `captureTimeoutMs`), and a `toCanvas` that throws synchronously reaches the fallback. The video itself is left out of html-to-image's clone (hiding it is
  not enough: the clone draws it on a canvas whatever its `display`). Pure `fitRect` and `standInVideos`
  are exported.

## triage-ui v1.1.0 — 2026-10-13

- **The queue opens on "open + triaged".** The status filter gained an "open + triaged" choice,
  selected by default and listed first: everything not yet declined or resolved, in one list
  (Ken, PlacementImageViewer bug-20261013-195520-0f5869: "just call it 'open + triaged' and default
  to it"). The server filters one status at a time, so that choice sends no `status` parameter and
  the rows are narrowed to open and triaged in the browser. "All statuses" and each single status
  stay, and send what they sent before. Behaviour change to the default only: it used to open on
  Open. No new mount option; embedders pass nothing new and nothing they pass changes meaning.

## widget v1.6.5 — 2026-10-13

- **Fix: one `<img>` with no `src` made the whole screenshot fall back to the old engine, and the
  old engine draws a worse picture.** html-to-image rejects an image that has no source, so a page
  holding one (PlacementImageViewer's hidden placement-view picture, filled when the pane opens) never
  got a good capture: the widget fell back to html2canvas, which drew a stray "1." beside every
  `<summary>`, flat white blocks behind text, and the filter `<select>`s without their styling
  (Ken's report bug-20261013-195520-0f5869; found by logging which engine drew it, then bisecting
  the page). The engine now leaves an `<img>` with no or an empty `src` out of the capture, which
  draws nothing on the page anyway. Checked on that page in WebKit 26.6 and Chromium: html-to-image
  draws it, and the capture is clean.
- **Fix: a `<summary>` came out of an html-to-image capture with a stray "1."** (seen on the live
  page, not only in the fallback). The engine takes the disclosure marker off every `<summary>` for
  the capture and puts it back; a screenshot shows no triangle rather than a wrong number. Pure
  `pinSummaryMarkers` is exported.

## widget v1.6.4 — 2026-10-12

- **Fix: a same-origin frame that could not be drawn left a blank rectangle in a screenshot that
  said "Screenshot captured."** The frame step swallowed every frame's render or paste error so
  one bad frame would not lose the whole capture — right — but it said nothing, so a framed page
  (LeapHQ's plug-ins, PlacementImageViewer's Investigate overlay) could be filed blank with no
  word of it (found reviewing leap-timesheet#429). The capture still succeeds, and now says so:
  the dialog reads "Screenshot captured, but 1 embedded frame may be missing from the screenshot (…)" ("may":
  html-to-image draws same-origin frames itself, so the frame can still be there), and the same
  line is sent as `screenshotError` beside the screenshot (the spec now allows
  that; servers may ignore it, as the reference backends do). `captureScreenshotDetailed` adds
  `framesMissed` and `frameError` to a successful result; pure `describePartialCapture` is exported.

## widget v1.6.3 — 2026-10-12

- **Fix: a page using `color-mix()` (or `color()`, `oklch()`, `lab()`…) could not be screenshotted.**
  html2canvas 1.4.1 parses only `rgb()`/`hsl()` colours and throws on anything newer, and browsers
  report a `color-mix()` result as `color(srgb …)`, so one tinted badge failed the whole capture with
  "Attempting to parse an unsupported color function \"color\"" (PlacementImageViewer
  bug-20261012-185703; the seal viewer lost its screenshots for weeks the same way and worked round it
  by removing `color-mix()` from its CSS). Both html2canvas calls (the page and same-origin frames) now
  pass an `onclone` hook that rewrites each such value in the clone as the `rgba()` the browser resolves
  it to, by painting it into one canvas pixel. Proven in Chromium: the old widget fails with the
  reported error; this one captures, and a 16% `color-mix()` tint over white reads back exactly.
  Pure `plainColors` and `plainColorsInClone` are exported for tests.
- **When the capture engine's fallback fails too, the error says why html-to-image failed first.**
  `capture-engine.js` used to drop html-to-image's error and report only html2canvas's, so a report
  named the fallback's problem and hid the reason the better engine was not used.

## widget v1.6.2 — 2026-10-09

- **Fix: dark pages were screenshotted on white.** `capture-engine.js` painted every capture on
  `#ffffff` (and html2canvas got a transparent background that the encoder later flattened onto
  white), so a dark-themed page (light text on a dark body, found on the fleet dashboard) came
  out as near-invisible light-on-white text. The engine now uses the page's real background:
  the first opaque computed `background-color` of `<body>` then `<html>`, else the browser's dark
  canvas colour for `color-scheme: dark`, else white (light pages are unchanged). Colours with
  alpha below 1 and gradients over a transparent colour count as not opaque: gradients are not
  rendered, the nearest opaque colour is used. The "drew nothing" check compares against that
  colour, and the html2canvas fallback receives it too. Pure `pageBackground` is exported for
  tests. `encodeCanvasUnderCap` is unchanged: engine canvases are already opaque, so its white
  flatten under JPEG is a no-op for them.
- **Fix: `<noscript>` fallback content appeared in screenshots.** html-to-image renders the
  clone as an SVG image without scripting, which displays `<noscript>` content, so the daily
  report's no-JS fallback form showed up as a raw `<button type="submit" ...>go</button>` next to
  the live controls. The capture filter now skips `<noscript>` and `<template>` (inert, never
  shown); pure `neverShown` is exported for tests.

## widget v1.6.1 — 2026-10-09

- **Fix: pen and box did nothing with a mouse in 1.6.0.** The 1.6.0 viewer shows the screenshot
  in an `<img>`, and dragging over an image starts the browser's native image drag, which cancels
  the pointer stream — no stroke ever registered (zoom still worked, so it looked half-alive).
  Found by StoppageViewer's real-browser markup test, which fails on 1.6.0 and passes on this.
  The image is now non-draggable with `pointer-events:none`. Anyone on 1.6.0 should take 1.6.1.

## widget v1.6.0 — 2026-10-08

One widget for every app: leap-timesheet and the seal viewer each carried an older fork with
features the shared widget lacked. Everything below is opt-in or backwards-compatible; an app
that changes nothing files the same POST as 1.5.0, apart from the changes marked **visible**
and one field: a report with NO screenshot now carries `screenshotError` (a server that
rejects unknown keys would refuse exactly those reports; the reference backends ignore it).
Spec: `clientKey` and `screenshotError` added as optional `BugCreate` fields (the reference
backends ignore both; a contract test proves they still file the report).

- **Bounded submit** (`submitTimeoutMs`, default 30 s; `slowNoticeMs`, 8 s). The POST is
  aborted after the timeout; the button comes back and the text is kept. A `role="status"`
  line says "Sending… uploading a 1.2 MB screenshot", then "Still sending…". After a timeout
  the dialog says the report may already be filed. Why: leap-timesheet `bug-20260911-222709`,
  where the server stored the row and answered 201 in 296 ms, the browser never acted on the
  answer, and the reporter was left on "Submitting…" for a report that had been filed. **Visible.**
  The timeout is a total cap on the POST, not an inactivity timer: a multi-MB upload on a very
  slow link needs a larger `submitTimeoutMs`. A value of 0, NaN or undefined means the default.
- **Idempotency** (`idempotentSubmit: true`, off by default). Sends `clientKey`, one UUID per
  report (new if the text or marks change between attempts, so a deduping server cannot swallow
  an edit; the same on every retry of unchanged text), so a server that dedupes on it can return the existing
  row; the timeout message then adds "Submit again; it won't be filed twice". Off by default
  because the reference backends ignore the key — promising "won't be filed twice" to an app
  on them would be false, so without the option the message says submitting again could file
  it twice. The reference backends are not taught to dedupe: it needs a lookup by key in each
  of three stores in two languages, and the apps that need it (timesheet) have their own server.
- **Why there is no screenshot.** `captureScreenshotDetailed` resolves `{dataUrl, reason, …}`
  with `no-library | timeout | render-error | blank | encode-error | too-large | tainted`;
  `captureScreenshot` still returns `dataUrl | null`. The modal now reads "Screenshot
  unavailable (why) — you can still submit text." (**visible**), the POST carries
  `screenshotError` when there is no image, and a new `onClientEvent(kind, detail)` hook hears
  `screenshot-failure` and `submit-timeout` for apps that log. Why: leap-timesheet
  `bug-20260915-000612` — reports from one tab arrived with no screenshot and nothing said
  which of five causes it was. A cross-origin-tainted canvas used to read as "blank".
- **Escape closes the dialog**, except while the screenshot viewer is open (its own Escape
  closes it). The dialog gains `role="dialog"` and `aria-modal`. **Visible.**
- **`position` accepts `{top, left}`** (and any mix with `{bottom, right}`).
- **Lazy html2canvas** (`html2canvasUrl`). If `window.html2canvas` is absent when a screenshot
  is needed, the script is injected on first click and awaited inside `captureTimeoutMs`;
  quick repeats share one script, a failed load can be retried. No URL = 1.5.0 behaviour.
  Limit: `capture-engine.js` wraps an html2canvas that is already loaded when it runs, so a
  lazily loaded html2canvas is the plain one (the engine is not applied).
- **Reachable over a modal `<dialog>`** (`reachOverDialogs`, default **on**; `false` opts out).
  `showModal()` makes the rest of the page inert, so the button could not be clicked in the
  one situation where someone most wants to report what they see. The widget now moves its
  button, form, viewer and toast into the topmost open modal dialog and back. From the seal
  viewer's `keepBugUiReachable()`, plus: the toast follows, and a dialog removed from the page
  with them inside gives them back first. Apps that already do this themselves are unaffected.
  **Default on is a behaviour change** for pages with modal dialogs.
- **Viewer ids unchanged.** The viewer's DOM id stays `bug-report-viewer` whatever `idPrefix`
  is (apps' tests select it); documented rather than changed.
- **Viewer:** pinch-zoom with two fingers (Move tool); a second finger is ignored while
  drawing; the toolbar and viewer pad for `safe-area-inset-*`; marks are drawn on an overlay
  canvas and the image is composited once on Done, instead of repainting the whole image on
  every pointermove (a pen drag now costs one segment per move). `pointercancel` discards the
  half-drawn mark instead of keeping it.
- **Fixes found on the way:** a capture that finished after its modal was closed (or closed
  and reopened) could overwrite the new modal's screenshot; a late successful submit after
  Cancel closed whatever modal was open by then; the viewer's Escape could also close a host
  dialog around it. The capture's trailing timeout is cleared once it settles. The
  `screenshot-failure` event carries the path, never the query string (which can hold tokens).
- **`capture-engine.js`** exposes its pure parts (the "drew nothing" check and the options it
  hands html-to-image) to Node, and is unit-tested; browser behaviour is unchanged.
- **Not ported from the forks:** timesheet's capture-first ordering (it waits for the capture
  before showing the dialog; the shared widget shows the dialog at once and fills the preview
  in) and its own `/api/client-events` posting (the hook replaces it: the app posts).

## widget v1.5.0 — 2026-10-08

- **Zoom and mark up the screenshot.** Click the preview in the report modal to open a
  viewer: wheel/+/−/Fit/100% zoom, drag to pan, ✏️ pen, ▭ box, undo, clear. Done puts the
  marked-up image in place of the capture, so it is what gets filed (re-encoded through the
  same size ladder, so it still fits the backend's cap). Marks are kept in image pixels, so
  zoom never changes the file. `markup: false` in `BugReportConfig` turns it off.
  `BugReportWidget.openViewer(src, {annotate: false})` opens the same viewer view-only —
  for a triage screen that wants to zoom a filed screenshot. Why: a reporter could only
  describe in words which part of the screen was wrong, and a triager could not read small
  text in a downscaled preview (StoppageViewer, 2026-10-08).
- **Optional `capture-engine.js` + `html-to-image.js`.** html2canvas re-implements CSS layout
  and can get a real page wrong — StoppageViewer's came out with text piled on text and blocks
  out of place (`bug-20261008-203620`). Loading these two files (after html2canvas, before
  the widget) puts the browser's own renderer in front of it; html2canvas stays as the
  fallback if it fails or draws nothing. Inline SVG styled by CSS (a chart) needs its computed
  styles pinned for the capture or it paints black; the engine does that. It skips everything
  below the visible area, since its cost is per DOM node (on a 100-card page: ~5 s before,
  ~0.3 s after) — and apps with big pages should raise `captureTimeoutMs`. Not used unless loaded.
  Neither engine draws `<video>` frames or iframe content.

## backend-python v2.1.0 — 2026-10-03

- **`create_blueprint(actor_identity=…)`** — an optional `(request) -> str`
  returning the checked identity of whoever is filing. When an app passes
  it, that identity is the report's `actorEmail` and the payload's
  `actorEmail` is ignored (a page can claim any address); an empty answer,
  or one that raises, files the report with no actor rather than with the
  claim. Without it nothing changes, so this is a minor. Why: the shipped
  widget sends no `actorEmail`, so every app on this package recorded its
  reports with no filer, and LeapHQ's merged queue could not tell whose
  report was whose.

## backend-python v2.0.0 · triage-ui v1.0.0 · spec — 2026-09-16

**Breaking for `backend-python`, deliberately.** `PATCH /bugs/{id}` used to
validate only that the new status was a member of the enum, so
`open → resolved` went through. It is now gated on the lifecycle contract
and answers **409** for a move the contract does not allow, and **400** for
a decline with no reason. A same-status PATCH is still the documented
no-op. Upgrading will turn some previously-accepted requests into refusals
— which is the point, but it is a behaviour change and gets a major.

- **`packages/spec/report-contract.json` — the lifecycle.** `openapi.yaml`
  pins the wire *format* and was always quiet about the lifecycle. That
  silence cost something: every consuming app decided for itself what
  `PATCH` may do, so the same request was a 409 in one app and a 200 in the
  next, neither wrong by its own lights, and a queue merged across both was
  incoherent. The contract settles the transitions a server must accept
  *exactly*, which of those a UI should offer, where a reason is required,
  the `kind` vocabulary, and which fields a list row carries versus a
  detail read. Same rule as the wire format: change it here first.
- **`packages/triage-ui` — the admin queue, packaged.** Four apps had
  hand-written this screen, three by porting the first one's HTML. They had
  drifted: one collapsed two kinds into one, one rendered a field the others
  didn't, one had no screen at all. Dependency-free, framework-free, themed
  entirely by CSS custom properties. `kind` is consumed from the server,
  never re-derived from tags — that re-derivation is the drift it exists to
  end.
- **`kind` on every payload the blueprint returns**, derived once from tags.

## v1.4.0 — 2026-09-15

- **A dragged button is restored inside the viewport.** The drag handler clamped the
  button to the window, but the position restored from `localStorage` at mount was
  applied exactly as stored. A button dragged towards the bottom-right of a large
  window therefore came back off-screen in a smaller one — another browser window, a
  laptop unplugged from its monitor, a phone — and the widget looked permanently
  missing. The only cure was deleting `bug-report-button-position-v2` by hand, in
  every browser separately (storage is per-origin *and* per-browser). That is how
  the fleet dashboard lost its button for its main reporter.

  The stored position is now clamped to the current viewport (4px margin, the same
  rule the drag handler always used, now one helper: `clampPos`) at mount **and on
  every `resize`**. Clamping is for display only and never rewrites storage, so a
  spot chosen on a big window comes back when the window does.
- **`title` config** — the modal heading, default `"Report a bug"`. Upstreamed from
  leap-daily-report, which had patched its vendored copy to say "Report a problem";
  a re-copy of v1.3.0 would have silently reverted it.
- `npm test` ran `node --test tests/`, which Node 22 treats as a module path and
  fails with `Cannot find module`; it now globs `tests/*.test.js`.
- 44 unit tests (+7: restore into a smaller window, restore unchanged when it fits,
  negative positions, shrink-then-grow on resize with no storage write, no stored
  position, unknown geometry, the title). Mutation-checked: restoring the unclamped
  position fails 3 of them.

## backends v1.1.0 — 2026-08-21

- **Screenshots are served with the media type they actually are.** Both reference
  backends hard-coded `image/png` on `GET /bugs/{id}/screenshot`. That was already
  a lie for anything that fell down the widget's v1.3.0 fallback ladder — and on an
  image-heavy page that ladder is the ordinary path, not the exotic one: a real
  deployment (a placement-image viewer, 39 photographic tiles) produces a
  viewport-cropped capture whose PNG still exceeds the 5 MB cap, so what it stores
  is a 1.4 MB JPEG, served as `image/png`. Consumers that send
  `X-Content-Type-Options: nosniff` — the right header for a service that serves
  user-uploaded bytes back — were relying on browsers being lenient about image
  types to render their own triage queue.

  The type is now sniffed from the stored bytes (`screenshot_content_type` /
  `screenshotContentType`, new in the model layer of each backend): PNG, JPEG, GIF
  and WebP are recognised. **Sniffed, not stored** — deliberately, because the bytes
  are the only thing every `Store` implementation is guaranteed to have kept, so
  existing screenshots in existing stores are labelled correctly too, with no
  migration and no schema change. Bytes in no recognised format keep `image/png`,
  which is what every server returned before this, so nothing that rendered stops
  rendering.

  The data: URL's declared type is *not* trusted: it is the client's claim about
  bytes the server is about to store, and a mislabelled upload must not become a
  mislabelled download.

- Spec: the `200` on `GET /bugs/{id}/screenshot` now enumerates the image types a
  server may return instead of promising PNG, and `types.ts` no longer describes the
  `screenshot` field as PNG.
- +14 backend unit tests (7 per stack: every recognised format, the RIFF-but-not-WebP
  case, truncated magic, empty, junk) and +2 cross-stack contract scenarios, so the
  two implementations are held to the same sniffing behaviour.

## v1.3.0 — 2026-08-17

- **Screenshot capture is cropped to the viewport.** `captureScreenshot` rendered the
  whole `doc.body`, so the canvas scaled with scroll height: a long list page produced
  a viewport-wide × full-scroll-height PNG that (a) blew past the reference backend's
  5 MB decoded cap — the POST 413'd and the report dead-ended with no way forward —
  and (b) previewed in the modal as an unreadable vertical sliver. Capture now crops
  to the visible viewport at the current scroll offset (what the user was looking at
  when they hit the button, which is the bug context anyway). Falls back to the old
  uncropped capture when viewport geometry is unavailable. Iframe compositing
  positions were re-based accordingly (frame rects are viewport-relative, so under
  the crop they map to the canvas directly instead of via `bodyRect`).
- **Size-capped serialisation with a fallback ladder.** New `maxScreenshotBytes`
  config (default 5 MB decoded, mirroring the reference backend cap — the server
  stays authoritative). Serialisation walks PNG → JPEG 0.85 → JPEG 0.6 →
  half-resolution JPEG 0.6 and returns the first rung under the cap; if none fit,
  the report is submitted without a screenshot instead of dead-ending in a 413.
  JPEG rungs flatten onto white first (browsers composite alpha onto black).
  New exported helpers: `dataUrlBytes`, `encodeCanvasUnderCap`.
- 37 unit tests (+12: byte accounting, every ladder rung, white-flatten, viewport
  crop options, scroll-offset crop, iframe paste position under crop, oversized
  capture degrading to JPEG).

## v1.2.0 — 2026-07-27

- **Double-submit guard on the Submit button.** The click handler called `onSubmit`
  with no guard: the POST is async and nothing on screen changed on the first tap, so
  on a phone — small target, slow network — people tapped again. Real reports arrived
  2–3 times seconds apart (2026-07-01 ×3, 2026-07-23 ×2, 2026-07-27 ×2), roughly
  doubling the queue for mobile reporters and burying real signal under duplicates.
  The button now disables and reads "Submitting…" on the first tap.

  It re-enables on a *real* failure — validation, HTTP error, network error — so a
  failed report is still filable. Note the asymmetry that made this subtle:
  `onSubmit` calls `showError(null)` on the SUCCESS path to clear stale messages,
  immediately before the fetch, so only a truthy message may restore the button.
  Re-enabling on every `showError` call would undo the guard exactly when it matters.

## v1.1.0 — 2026-07-24

- **Screenshot: same-origin iframe content is now captured.** html2canvas renders an
  `<iframe>` as a blank rectangle, so any page whose main content is framed screenshotted
  as an empty white box — reported in the field as "the screenshot misses the content".
  `captureScreenshot` now re-renders each same-origin frame and composites it into the
  parent capture at the frame's position. Cross-origin frames are unreachable by design
  and still come out blank; a frame that fails to render no longer loses the whole
  screenshot, and `data-bug-report-exclude` is honoured on frames.
- **`buttonSize` config (default 52).** Diameter of the floating button in px, clamped to
  24–96; the glyph scales with it. Lets an app embedded inside another shell use a smaller
  button so it reads as chrome rather than page content. Existing consumers are unaffected.
- 21 unit tests (+4: compositing, cross-origin skip, failing-frame resilience, config).

## v1.0.1 — 2026-05-17

- **No code changes.** Adds this CHANGELOG. Used as the first Dependabot smoke-test target — pushing v1.0.1 verifies that `claude-tmux-dashboard`'s Dependabot config opens a PR bumping the git-tag pin in its `requirements.txt`.

## v1.0.0 — 2026-05-17

Initial release.

**Packages:**
- `packages/widget` — self-injecting JS widget + vendored html2canvas. 17 unit tests.
- `packages/spec` — OpenAPI 3.1 + TS + Python types. The contract.
- `packages/backend-python` (`bug-report-py`) — Flask blueprint, `Store` Protocol, `InMemoryStore`, `FilesystemStore`. 42 tests parametrized across both reference stores.
- `packages/backend-node` (`bug-report-node`) — Express router, `Store` interface, `InMemoryStore`, `FilesystemStore`. 31 tests.
- `packages/backend-node/azure-tables` (`bug-report-node/azure-tables`) — `AzureTablesStore`, ported from leap-timesheet. Optional peer deps on `@azure/data-tables` + `@azure/storage-blob`. 12 unit tests.
- `packages/cli` (`bug-report-cli`) — Python CLI. 7 tests.
- `e2e/` — cross-stack contract suite. 32 tests running the same scenarios against both backends.

**Consumers:**
- `claude-tmux-dashboard` consumes `bug-report-py` + `bug-report-cli` via the git-tag pin documented in [README.md](README.md).

**Design pattern:** see [bug-report-pattern.md](https://github.com/LeapAutomation/leaphq-design-patterns/blob/main/bug-report-pattern.md) for the full rationale and the variation observed across the four implementations in Ken's stack.
