# `bug-report-py` — Python backend

Flask blueprint implementing the [bug-report API spec](../spec/openapi.yaml). Pluggable storage; file, in-memory and PostgreSQL stores ship in the box. Version 3 adds the central-store pieces: triage fields, votes, `PostgresStore`, the proxy, the service-side trust helper and `import_reports` (sections below).

## Install

```bash
pip install "bug-report-py @ git+https://github.com/LeapAutomation/bug-report-widget.git#subdirectory=packages/backend-python"
```

## Use

```python
from flask import Flask, request
from bug_report import create_blueprint, FilesystemStore

app = Flask(__name__)

def is_admin(req):
    # Whatever your auth model is. For a single-user app, return True.
    return req.headers.get("X-Admin") == "yes"

app.register_blueprint(create_blueprint(
    store=FilesystemStore("/var/lib/bugs"),
    is_admin=is_admin,
    default_actor_email="ops@example.com",
    build_sha=lambda: read_build_sha_from_disk(),  # optional
), url_prefix="/api")
```

Routes mounted (under the prefix you pass):

| Method | Path                       | Auth-gated      |
|--------|----------------------------|-----------------|
| POST   | /bugs                      | open            |
| GET    | /bugs                      | `is_admin`      |
| GET    | /bugs/{id}                 | open            |
| PATCH  | /bugs/{id}                 | `is_admin`      |
| GET    | /bugs/{id}/screenshot      | open            |
| POST   | /bugs/{id}/vote            | signed in (3.0.0) |
| DELETE | /bugs/{id}/vote            | signed in (3.0.0) |

## Triage fields, votes and filters (3.0.0)

`PATCH /bugs/{id}` takes `area`, `priority` (`now|next|later`), `owner` (an email), `parentId` and `duplicateOf`
with or without a `status` (a null or empty value clears a field). Each change writes an audit row whose note says
what changed (`area: none -> Cutter; priority: none -> now`); a value equal to the current one writes nothing.
`duplicateOf` also declines the report with the note `duplicate of <id>`. A `parentId` that would make a cycle is 400.

`POST /bugs/{id}/vote` and `DELETE /bugs/{id}/vote` need only a signed-in caller (the `actor_identity` email; without
that hook, the body's `actorEmail`), one vote per person, case-insensitively. `GET /bugs` filters by `app`, `kind`,
`area` and `priority` as well as `status` and `days`. Every summary carries `kind`, `app`, `product`, `area`,
`priority`, `owner`, `parentId`, `duplicateOf` and `votes`; a record written before v3 has them null and `votes` 0.
`create_blueprint(app_stamp=lambda req: "placements")` stamps the app a report is filed under (the body's `app` is
never read); `guard=` refuses a request before any route runs.

## PostgresStore (3.0.0)

```python
# pip install "bug-report-py[postgres]"     # psycopg 3
import psycopg
from bug_report import create_blueprint
from bug_report.pg import PostgresStore

store = PostgresStore(lambda: psycopg.connect(dsn))   # a connection factory: one per operation
store.ensure_schema()                                  # idempotent: reports, report_audit, report_votes
create_blueprint(store=store, ...)
```

The factory is called for every operation and the connection used as a context manager (commit, then close), so a
service whose password is a short-lived Entra token passes a factory that fetches a fresh one; a pool's
`pool.connection` works too. The `record` jsonb column holds what `FilesystemStore` writes (the same JSON, key for key;
jsonb does not keep key order or whitespace); `app`, `kind`, `status`, `added_at`, `area`, `priority`, `owner`,
`parent_id` and `duplicate_of` are copies lifted out for the list filters, and `kind` there is the effective kind
(stored, else derived from the tags). Database errors surface as `StoreError`.

## Moving an app's reports into the central store (3.0.0)

```python
from bug_report import FilesystemStore, import_reports
from bug_report.pg import PostgresStore

counts = import_reports(FilesystemStore("/data/bugs"), PostgresStore(connect), app="placements")
# {"copied": {"open": 12, "declined": 3}, "skipped": {}, "audit": 41, "screenshots": 9, "votes": 0}
```

Keeps ids, audit rows, screenshots and votes; a report that has no `app` gets this one. Idempotent and resumable: a
report already there is skipped (and counted under `skipped`), but audit rows or votes it is missing are filled in,
so a second run over a finished import copies nothing.

## An app's `/api/bugs` as a proxy to the central service (3.0.0)

```python
from bug_report import create_proxy_blueprint
from bug_report.identity import person_provider

app.register_blueprint(create_proxy_blueprint(
    central_url="https://leaphq-reports.internal/api/bugs",
    app="placements",
    token_provider=lambda: credential.get_token(SCOPE).token,        # the app's own managed identity
    person_provider=person_provider(trust_easyauth=True),            # the signed-in person
), url_prefix="/api")
```

Every route (POST, list, detail, PATCH, vote, screenshot) is forwarded with its method, query, body and content type,
and the service's status, body, content type and `Location` come back unchanged (a 302 to a signed screenshot URL is
not followed). Each forwarded request carries `Authorization: Bearer <token_provider()>`, `X-Leap-Actor-Email` and
`X-Leap-Actor-Name` (percent-encoded) from `person_provider()`, and `X-Leap-App: <app>`; the browser's own
`Authorization` and `X-Leap-*` headers are dropped. A service that cannot be reached is a 502 with a message; a token or
identity that cannot be produced is a 502 and nothing is sent. Standard library only (urllib).

## The service side: who is calling (3.0.0)

```python
from bug_report import TrustedCallers, create_blueprint

callers = TrustedCallers(
    trusted_apps={"<placements client id>": "placements", "<seal client id>": "sealviewer"},
    verify_token=verify_entra_token,          # YOURS: raw bearer token -> claims dict, or raise. No JWT code here.
    trust_easyauth=True,                      # for the person-through-the-gateway path
)
create_blueprint(store=store, actor_identity=callers.actor_identity, app_stamp=callers.app_stamp,
                 guard=callers.guard, is_admin=callers.is_admin_in(BUG_ADMINS))
```

`trusted_caller(request, trusted_apps, verify_token)` is the function underneath. With a bearer token: it is verified
(a verifier that raises, or returns no claims, is 401); its `azp` or `appid` claim must be in `trusted_apps` (else 403);
only then are `X-Leap-Actor-Email` / `-Name` believed and `app` is the mapped name. A bad or untrusted token never falls
back to the headers. A disagreeing `X-Leap-App` is ignored, and the response says which won: `X-Leap-App-Used` and
`X-Leap-App-Ignored` headers, `app` and `appIgnored` in the POST body. With no bearer token the caller is a person
through the gateway: `X-Leap-Actor-*` are not read (anyone could send them); the actor is what
`identity.trusted_person` finds, and `app` is null unless the body names a known one.

`bug_report.identity` holds that person logic, moved out of the apps (`trusted_person`, `trusted_identity`,
`entra_claims`, `env_flag`): `trust_easyauth=True` believes EasyAuth's `X-Ms-Client-Principal`; `loopback_headers=True`
believes Tailscale Serve's headers from a loopback peer only. Nothing reads the environment for you.

## Emailing the reporter on close (2.2.0)

```python
# pip install "bug-report-py[email]"   # azure-communication-email, azure-identity
from bug_report import create_blueprint, AcsNotifier

create_blueprint(
    store=store,
    actor_identity=lambda req: {"email": signed_in_address(req), "name": signed_in_name(req)},
    notifier=AcsNotifier.from_env(),   # None when not configured: no emails, no tick box
    app_url="https://sv.example.com",  # linked when the report has no page URL
)
```

`PATCH {status, note, notify: true}` on a change to `resolved`/`declined` writes the change and its audit row, then
emails the reporter and writes a second audit row with the outcome. A failed send never undoes the change; the
response carries `notified: {ok, to, error?}`. Environment for `AcsNotifier.from_env()`: `ACS_ENDPOINT` (managed
identity; `BUG_NOTIFY_CLIENT_ID` for a user-assigned one) or `ACS_CONNECTION_STRING`, `BUG_NOTIFY_SENDER`,
`BUG_NOTIFY_DOMAINS` (comma list; an address outside it is refused). Any `notifier(notice) -> message id` that
raises on failure works in its place.

Open-by-default for POST and single-bug GET is deliberate: any user (or agent) can file, and the user who filed should be able to read back their own report. Tighten in your own auth wrapper if your threat model needs it.

## Stores

| Class | Purpose |
|---|---|
| `InMemoryStore` | Tests, dev. Dict-backed; lost on process restart. |
| `FilesystemStore(root)` | Single-host production. One JSON per bug + audit JSONL + votes JSONL + PNG. What `claude-tmux-dashboard` runs in production. |
| `bug_report.pg.PostgresStore(connect)` | The central store (3.0.0); see above. Needs `bug-report-py[postgres]`. |

To plug in another backend (SQLite, S3, …) implement the `Store` protocol — see [`bug_report/store.py`](bug_report/store.py). The blueprint depends on the protocol only.

## Tests

```bash
cd packages/backend-python
pip install -e ".[test]"
pytest
```

The blueprint suite runs against every store (in-memory, filesystem, PostgreSQL). Any new `Store` implementation should be added to the `store` fixture in `test_blueprint.py` and the `any_store` fixture in `tests/conftest.py` and will be exercised by the same contract.

The PostgreSQL tests use a real server: set `BUG_REPORT_PG_DSN` (a server you can create schemas on; each test gets its own schema), or have Homebrew's `postgresql@16` installed and a throwaway one is started on a free port in a temp directory. With neither, they skip and say why; under `CI=true` a missing Postgres fails instead.

## Invariants enforced

- **Every successful PATCH writes an audit row.** A no-op (status == current status) MUST NOT add a row.
- **Screenshot is optional always.** POST without one returns 201.
- **`addedBy=agent` auto-adds `agent-self-report` tag.** Agent-filed bugs are first-class entries in the same queue, marked so admins can spot them.
- **Body limits server-enforced.** 10 KB details, 5 MB screenshot. Form/JSON both supported.
