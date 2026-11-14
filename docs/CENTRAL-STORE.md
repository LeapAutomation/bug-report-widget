# One store for every app's bug and feature reports

**Status:** design, not built. **Decided (Ken, 2026-11-14):** one central store on Postgres (option A), not federation.
**Still assumed (not yet confirmed by Ken):**
- Product → App → Area as the levels, with products PikPak, Seal, Placement and Internal;
- feature requests in the same store as bugs, with votes as a field.

**Supersedes:** the federation in `leap-timesheet/documents/report-queue-common-design.md`. That doc's contract work stands; its "LeapHQ federates five sources read-only" does not.

## Why

Today six apps file reports into five stores: three Azure Files shares, one Postgres table and LeapHQ's Azure Tables. Behind them are four server implementations and four triage viewers.
- LeapHQ merges the five read-only, over HTTP, on every page load.
- A report can only be closed in the app it came from, and the seal viewer has no triage page at all.
- Every field we would add for a hierarchy (area, priority, owner, parent, duplicate) would have to go into five stores in three languages.

The survey behind this is in the hand-over note "bug-feature-reports-across-apps.md", 2026-11-14.

## Shape

```
browser 🐞 / BugQueue ──► app's /api/bugs ──(proxy)──► leaphq-reports /api/bugs ──► Postgres `reports` db
agents, MCP tools ──(through an app, or as their own app identity)──► leaphq-reports   (leaphq-pg)
LeapHQ Review ──(LeapHQ's own identity, a token)──► leaphq-reports (one source, not five)
```

- **The central service, `leaphq-reports`.**
  - It is the `bug-report-py` blueprint plus a new `PostgresStore`, and nothing else app-specific.
  - It is its own Container App with internal ingress and **no gateway route**: nobody reaches it directly. Every caller is an app, with a token (Ken, 2026-11-14).
  - It is the only place reports are stored, emails are sent and triage rights are checked: one `BUG_ADMINS`, and one sending identity (`leaphq-bug-notify`).
- **The apps keep their URL.** Each app's `/api/bugs` becomes `create_proxy_blueprint(central_url, app="placements")` from the library.
  - It forwards every route (POST, list, detail, PATCH and screenshot) with its body unchanged.
  - The widget, BugQueue and every page that links to a report keep working, with no front-end change.
  - The app stamps `app`; the caller cannot set it.
- **Who is asking.** Every request carries a token: the calling app's **own managed identity** (an Entra token). There is no person path on the service, so a request with no token is 401 and the service never reads `X-Ms-Client-Principal` or Tailscale headers.
  - The app passes the signed-in person, from its own `trusted_person()`, in two headers (`X-Leap-Actor-Email`, `X-Leap-Actor-Name`).
  - The service verifies the token with **PyJWT** (signature, issuer, audience and expiry: a decode-only check would make every trusted client id spoofable), then believes those headers only from identities on its `TRUSTED_APPS` list (client id → app name). The app name comes from the token, not the request.
  - **LeapHQ calls as its own identity** like every other app (it is `leaphq` in `TRUSTED_APPS`), not through a gateway route.
  - This replaces the near-identical `trusted_identity()` / `trusted_person()` / `is_admin()` copies in SV, PIV, seal and the fleet dashboard. `trusted_person()` moves into the library once, for each app's proxy to use behind its own gateway.
- **Seal laptops.** Their outbox forwards to the seal viewer's proxy exactly as it does today, so nothing changes on a station.

## Store

`bug_report.pg.PostgresStore` (an optional extra, `bug-report-py[postgres]`), promoted from the fleet dashboard's `bug_store_pg.py`, which has run in production since 2026-09-09.
- `reports(id text pk, app text, kind text, status text, added_at timestamptz, area text, priority text, owner text, parent_id text, duplicate_of text, record jsonb, screenshot bytea)`.
- `report_audit(id, seq, entry jsonb)`.
- `report_votes(id, voter, at)`.
- Columns are lifted out of `record` only where the list filters or sorts on them. The record stays the JSON the file store writes, so an imported report reads back byte for byte (the rule fleet's store already follows).
- **Database:** a new database `reports` on `leaphq-pg`, owned by the identity `leaphq-reports`. It is not inside `fleet`, so fleet's ownership stays as it is.

## Contract v3 (adds; nothing removed)

| Field | Values | Set by |
|---|---|---|
| `app` | stoppages, placements, sealviewer, report, fleet, leaphq | the proxy, from the caller's identity |
| `product` | PikPak, Seal, Placement, Internal | derived from `app` by a table in the contract |
| `kind` | bug, feature | stored; still derived from tags when the filer does not say |
| `area` | per-app list, kept in the store, editable in the queue | triage |
| `priority` | now, next, later (the roadmap's "now / when an engineer is back / wait") | triage |
| `owner` | email | triage |
| `parentId` | a report id: a feature can hold bugs and smaller features (an epic) | triage |
| `duplicateOf` | a report id; it closes the report as `declined` with the note "duplicate of …" | triage |
| `votes` | count, from LeapHQ's feature-request votes | anyone signed in |

Statuses stay at four. LeapHQ's feature-request `shipped` folds into `resolved`.

## Viewer

`@leap/ui` BugQueue is the one viewer.
- It gains filters for `app`, `kind`, `area` and `priority`.
- It shows parent, children and duplicate links.
- It gains a **roll-up view**: open, "now" and older-than-30-days counts per Product → App → Area, and clicking a count opens those items.
- In an app it opens filtered to that app; in LeapHQ it opens unfiltered.

`triage-ui`, the fleet dashboard's tab and LeapHQ's `queues.js` retire once their apps have moved.

## Drift

- Each app pins a library tag; nothing is vendored.
- A CI job in this repo reads each consumer's pin and contract hash (the list is README "Who uses it") and fails when one falls more than one minor version behind.
- The service's own e2e runs the existing both-backend scenarios against `PostgresStore`.

## Migration (one app at a time; order: seal, PIV, SV, report, fleet, LeapHQ)

For each app:
1. `tools/import_reports.py <app>` copies its store into `reports`, keeping ids, audit rows, screenshots and votes. **It is one run, at cutover, with the app's writes frozen.** A second run copies nothing: it compares each report already there (status, triage fields, votes, audit length and last audit row) and, if anything differs, raises with the ids that differ and changes nothing.
2. It checks the counts per status match, and that every imported report reads back equal.
3. Deploy the app with the proxy. Its old store is kept read-only for 30 days, then deleted (Ken confirms each deletion).
4. LeapHQ drops that app from its federation list; when the last one goes, so does `plugin-bugs.js`.

LeapHQ goes last. Its Azure Tables rows (bugs and feature requests) import the same way, and its Review tab points at the service.

## What Ken does (identity and data; the auto-mode classifier refuses these)

- Create the managed identity `leaphq-reports` and the database `reports` with that identity as its owner. A one-off script, in the style of `pg-owner-step.sh`, will be written for him to run.
- Give `leaphq-reports` the ACS sending role (or attach `leaphq-bug-notify` to it).
- Register each app's managed identity (and LeapHQ's) as a trusted client id in `TRUSTED_APPS`, and give the service its Entra audience and issuer. There is no gateway route to add.
- Approve each old store's deletion after its 30 days.

## Not doing

- No message queue and no sync job: the app forwards synchronously, as LeapHQ's federation already does. If the service is down, filing fails with a message, and the seal laptops' outbox still queues.
- No new front-end framework: the existing BugQueue, extended.
- No attachments or `context` field (contract v2 left them out for good reasons).

## Order of work

1. This doc, reviewed.
2. Library:
   - `PostgresStore`, contract v3 and `create_proxy_blueprint`, plus moving `trusted_person` in;
   - both-backend e2e;
   - tag `backends-v3.0.0`.
3. `LeapAutomation/leap-reports`: a ~100-line app, a deploy script and Ken's one-off identity and database script. Deploy it empty.
4. Seal migrates first. That gives it a tick box: its BugQueue page arrives with the proxy, the same page PIV has.
5. PIV, SV, the daily report and the fleet dashboard follow, each with its own PR, import and count check.
6. BugQueue's roll-up and hierarchy fields (`@leap/ui`), then LeapHQ moves over.
