# `bug-report-spec`

The wire-level contract every bug-report backend honors. Single source of truth.

- `report-contract.json` — the **lifecycle**: the statuses, which transitions a server must accept, which of those a UI should offer, where a reason is required, and the `kind` vocabulary. See below.
- `openapi.yaml` — full API definition. The Python and Node backend libs implement this; the cross-stack e2e suite in `../../e2e` runs the same scenarios against both.
- `types.ts` — TypeScript types for the JSON DTOs (handy if your Node backend or frontend wants type-checked clients).
- `types.py` — Python `TypedDict`s for the same DTOs.

If you add a field to the wire format, change it here first. The e2e contract suite, run in CI on every PR, fails the build if the backends drift from the spec.

## Audit-flow invariant

The spec is intentionally quiet about *who* can call each route — that's the consuming app's call. But it pins down one behavior every backend MUST get right: **every successful `PATCH` MUST append an audit row**, and a no-op patch (same → same) MUST NOT. The contract test in `../../e2e/contract.spec.ts` enforces both.


## The lifecycle contract

`openapi.yaml` pins the wire FORMAT — routes, field names, shapes. It was
always deliberately quiet about the LIFECYCLE, and that gap cost
something: every consuming app decided for itself what `PATCH` may do, so
one enforced `open → triaged → resolved` while another checked only that
the status was a member of the enum. The same request was a 409 in one
app and a 200 in the next, neither was wrong by its own lights, and a
merged queue across both was incoherent.

`report-contract.json` settles it:

- **`transitions`** — what a server MUST accept, *exactly*. An earlier
  draft said "may accept more, never less", which permitted the very
  disagreement it was written to end.
- **`offered`** — what a triage UI should show: a subset, because not
  every legal move deserves a button (`triaged → open` is legal and is
  not offered — a Reopen button on a triaged row reads as a mistake).
- **`reasonRequiredFor`** — declining without saying why makes the queue a
  bin. The reason rides on the audit row, never on the report, so a later
  reopen cannot erase the record of why somebody said no.
- **`kinds`** + **`kindTags`** — a queue holds more than bugs, and the
  choice was surviving only as a tag, so every reader re-derived it and
  they disagreed. One derivation, server-side; readers consume it.
- **`summaryFields` / `detailFields`** — what a list row carries versus a
  detail read. A list of thirty reports must not ship thirty bodies.

Same rule as the wire format: **change it here first.** Each backend
carries a mirror (`bug_report/contract.py`, and the equivalents in the
consuming apps) with a test asserting the two agree.

The field names are the ones the implementations already spoke, not the
ones any single one of them used internally — `screenshot` is a boolean
("is there one"), because that is what every backend here emits.

## People are named; closing can email the reporter (backends 2.2.0)

All additive and optional, so older data and older clients keep working:

- `actorName` on a report and `changedByName` on an audit row: the display name beside the address. `""` when unknown. A reader derives a name from the address when it is empty (split the local part on `.` `_` `-`, capitalise each part: `chris.hamblin@x` is "Chris Hamblin", `ken@x` is "Ken"); the first name is the first word.
- `canNotify` on `GET /bugs/{id}` only: this server can email the reporter and the report has an `actorEmail`.
- `PATCH` takes `notify: true` (default false). It is acted on only when the status actually changes to `resolved` or `declined` (reopening never emails), needs a non-empty `note`, and writes the change and its audit row before the send. The response gains `notified: {ok, to, error?}`; a second audit row (from = to = the new status) records `emailed <name> <address>` or `email to <name> <address> failed: <reason>`. The Node backend has no mailer: it ignores `notify` and returns no `notified`.
