# `bug-report-node` — Node backend

Express router implementing the [bug-report API spec](../spec/openapi.yaml). Pluggable storage; same shape as the Python backend so contract tests pin the wire format on both sides.

## Install

```bash
npm install github:LeapAutomation/bug-report-widget#main
# (uses the package at packages/backend-node via its package.json entry)
```

## Use — plain Express

```js
import express from "express";
import { createBugsRouter, FilesystemStore } from "bug-report-node";

const app = express();
app.use("/api/bugs", createBugsRouter({
  store: new FilesystemStore("/var/lib/bugs"),
  isAdmin: (req) => req.header("x-admin") === "yes",   // your auth
  defaultActorEmail: "ops@example.com",
  buildSha: () => process.env.BUILD_SHA || "",
}));
```

## Use — Azure Functions (leap-style)

The Functions runtime doesn't take an Express app directly, but you can adapt:

```js
// api/bugs/index.js
import { createBugsRouter, FilesystemStore } from "bug-report-node";
import { createServer } from "node:http";

const router = createBugsRouter({ store: yourAzureTablesStore, isAdmin });
const handler = createServer((req, res) => router(req, res, () => res.status(404).end()));

export default async function (context, req) {
  // ...delegate to handler, translate context.res, etc.
}
```

In practice the leap-timesheet migration plan is to lift `createBugsRouter` and wire it through a small Azure Functions shim that maps `context.req` / `context.res`. The existing `api/bugs/store.js` (Azure Tables + Blob) becomes an `AzureTablesStore` implementation of the `Store` protocol and lands here in a follow-up commit.

## Routes

| Method | Path                  | Auth-gated  |
|--------|-----------------------|-------------|
| POST   | /                     | open        |
| GET    | /                     | `isAdmin`   |
| GET    | /:id                  | open        |
| PATCH  | /:id                  | `isAdmin`   |
| GET    | /:id/screenshot       | open        |

## Stores

| Class | Purpose |
|---|---|
| `InMemoryStore` | Tests, dev. Map-backed; lost on process exit. |
| `FilesystemStore(root)` | Single-host production. One JSON per bug + audit JSONL + PNG. |
| `AzureTablesStore` | Azure Tables + Blob backend. Same on-disk schema as leap-timesheet's original implementation. |

Implement the `Store` shape (`putBug`, `getBug`, `listBugs`, `updateStatus`, `appendAudit`, `listAudit`, `getScreenshot`) for any other backend. See [`src/store.js`](src/store.js).

### `AzureTablesStore`

Ported from leap-timesheet's `api/bugs/store.js`. Lift-and-shift compatible: same `BugReports` + `BugReportsAudit` tables, same column names, same `screenshots` blob container layout, same `partitionKey = yyyy-mm-dd` partitioning. Existing leap data is readable; future Azure-hosted Node apps can adopt it directly.

```js
import { createBugsRouter } from "bug-report-node";
import { AzureTablesStore } from "bug-report-node/azure-tables";

const store = new AzureTablesStore({
  connectionString: process.env.AZURE_STORAGE_CONN,
  // table:         "BugReports",        // optional overrides
  // auditTable:    "BugReportsAudit",
  // blobContainer: "screenshots",
  // sasTtlSec:     300,
});

app.use("/api/bugs", createBugsRouter({ store, isAdmin }));
```

Peer dependencies (declared as optional in `package.json` — consumers who don't use Azure don't pay for them):
- `@azure/data-tables` ≥ 13.3.2
- `@azure/storage-blob` ≥ 12.31.0

The store imports them lazily; if you construct an `AzureTablesStore` without installing them, the first call that needs Azure will throw with a clear error.

Tests inject in-memory doubles via the constructor (`{ tableClient, auditTableClient, blobContainer }`) — the same shape leap's `tests/helpers/mock-azure-tables.js` uses. 12 unit tests cover the round-trips and the OData escaping invariant.

## Tests

```bash
cd packages/backend-node
npm install
npm test
```

31 tests: 15 contract tests run twice (once per built-in store) + a sanity check. Adding a `Store` impl means appending to the `STORES` array and you get the contract coverage for free.

## Invariants enforced

Identical to the Python side — see the same list at [`../backend-python/README.md`](../backend-python/README.md#invariants-enforced). If a behavior differs between the two backends, that's a spec bug or a contract-test gap; the cross-stack suite in [`../../e2e`](../../e2e) hunts these.

## Contract v3 (backends 3.0.0)

The router stores and echoes the v3 fields (`kind`, `app`, `product`, `area`, `priority`, `owner`, `parentId`,
`duplicateOf`, `votes`), takes them on `PATCH` (audit row per change; `duplicateOf` declines with the note
`duplicate of <id>`), filters the list by `app`, `kind`, `area` and `priority`, and serves `POST`/`DELETE /:id/vote`.
Options: `appStamp(req)` (the app a report is filed under; the body's `app` is never read) and `actorIdentity(req)`
(the signed-in email, who votes). `FilesystemStore` and `InMemoryStore` keep them; the Azure Tables store does not yet
and the router answers 501 for triage fields and votes on it. The Python library is the reference for the central store.
