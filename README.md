# bug-report-widget

A portable, in-app bug-reporting feature. One floating 🐛 button captures the screen, lets users (and AI agents) file structured reports, and drops them into a review queue with an audit trail.

The design pattern that motivated this lives at [design-patterns/bug-report-pattern.md](https://github.com/LeapAutomation/leaphq-design-patterns) — read that first if you want the *why*. This repo is the implementation: a portable widget plus reference backends in two languages, kept honest by a shared API spec and a cross-stack test suite.

Owned by Leap Automation Ltd and released under the MIT License (see [LICENSE](LICENSE) and [NOTICE](NOTICE)). Created by Ken Moynihan and assigned to Leap Automation Ltd on 4 November 2026, when the repository moved from `kmoy007` to `LeapAutomation`.

## What's in the box

| Package | Language | What it gives you |
|---|---|---|
| [`packages/widget`](packages/widget) | Vanilla JS | The floating button + capture + modal. Self-injecting; one `<script>` tag per page. Vendors html2canvas; zoom + markup on the screenshot; an optional more faithful capture engine. |
| [`packages/triage-ui`](packages/triage-ui) | Vanilla JS + CSS | The admin queue: the screen someone works reports through. Dependency-free, themed by CSS custom properties. |
| [`packages/spec`](packages/spec) | OpenAPI + TS + Python types + lifecycle contract | The single source of truth for the wire format **and** the report lifecycle. Backends honor it; e2e tests enforce it. |
| [`packages/backend-python`](packages/backend-python) | Python (Flask blueprint) | `from bug_report import blueprint, FilesystemStore` — wire it into any Flask app. |
| [`packages/backend-node`](packages/backend-node) | Node (Express + Azure adapter) | `import { createBugsRouter } from 'bug-report-node'` — wire it into Express, Azure Functions, or your own server. |
| [`packages/cli`](packages/cli) | Python | `report-bug "title" "details"` — what your tmux-spawned Claudes and CI jobs call to file bugs. |
| [`e2e/`](e2e) | pytest + requests | Boots the Python and Node backends and drives the same scenarios against both. |

Every PR and push to `main` runs the `test` check ([`.github/workflows/test.yml`](.github/workflows/test.yml)): every package's unit tests, the widget's browser tests and the `e2e/` contract suite. A new package's tests go in that workflow too.

## Who uses it

Every app that shows a 🐛 button vendors a copy of `packages/widget/src` (see each repo's
`VENDOR.md`: version, commit and sha256 per file). **Update this list when an app adopts the
widget**, and start any rollout from it. To re-derive it, search for the config object:

```bash
gh search code --owner LeapAutomation BugReportConfig
```

| App | Where its copy lives |
|---|---|
| StoppageViewer | `static/` |
| pikpak-fleet-dashboard | `static/` |
| pikpak-fleet-api | `static/` (Swagger docs page) |
| PlacementImageViewer | `static/` |
| leap-daily-report | `static/` |
| seal-verification-image-viewer | `seal_viewer/static/` |
| leap-timesheet | `lib/` (the ancestor of this widget; a clean vendored copy since 1.6.3, leap-timesheet#429) |

## Who's using it

- **leap-timesheet** — the original; Node + Azure Functions.
- **claude-tmux-dashboard** — Python/Flask; the seed for the Python backend.
- **katja-schedule** — Python/Flask (in-flight migration).
- **pikpak-platform** — Python (planned).

If you build something new and want bug reporting in an hour, see [docs/integrating.md](docs/integrating.md).

## Quick start

### Frontend (any stack)

```html
<script src="/lib/html2canvas.min.js" defer></script>
<script src="/lib/bug-report.js" defer></script>
```

Copy `packages/widget/dist/{bug-report.js,html2canvas.min.js}` into your `public/` (or `static/`) and you're done. The widget self-injects on `DOMContentLoaded` and posts to `/api/bugs`. Configure via `window.BugReportConfig = {endpoint, idPrefix, theme}` before the script tag if you want different.

### Backend — Python (Flask)

```python
from flask import Flask
from bug_report import create_blueprint, FilesystemStore

app = Flask(__name__)
app.register_blueprint(create_blueprint(
    store=FilesystemStore("/var/lib/bugs"),
    is_admin=lambda req: req.headers.get("X-Admin") == "yes",  # your auth
))
```

### Backend — Node (Express)

```js
import express from "express";
import { createBugsRouter, FilesystemStore } from "bug-report-node";

const app = express();
app.use("/api/bugs", createBugsRouter({
  store: new FilesystemStore("/var/lib/bugs"),
  isAdmin: (req) => req.header("X-Admin") === "yes",  // your auth
}));
```

### CLI (Python; for agents and humans)

```bash
pip install git+https://github.com/LeapAutomation/bug-report-widget.git#subdirectory=packages/cli

report-bug -t "tool call returned malformed JSON" -d "Repro: …" --tag investigate
```

## Design principles (the boring stuff that matters)

These come from the pattern doc, restated as testable contracts:

1. **One review queue.** Web-filed, agent-filed, CLI-filed bugs all land in the same store. No privileged "agent feedback" channel. The contract test in `e2e/` verifies this.
2. **Every transition writes an audit row.** No silent state changes. The Python and Node stores both enforce this; cross-stack test pins it down.
3. **Same backend, different storage.** Backends accept a `Store` interface. Reference implementations shipping today: `InMemoryStore` (tests), `FilesystemStore` (single-host apps), `AzureTablesStore` (Node only, ported from leap-timesheet). Drop in your own (Postgres, SQLite, S3 …) by implementing the `Store` shape.
4. **Screenshot is optional, always.** Servers MUST accept reports without one. The widget falls back gracefully on Safari canvas-taint or timeout.
5. **Spec is the contract.** If a behavior matters, it's in `packages/spec/openapi.yaml` and the cross-stack contract test will fail if either backend drifts.

## License

MIT. See [LICENSE](LICENSE).
