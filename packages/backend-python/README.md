# `bug-report-py` — Python backend

Flask blueprint implementing the [bug-report API spec](../spec/openapi.yaml). Pluggable storage; two reference implementations ship in the box.

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
identity; `AZURE_CLIENT_ID` for a user-assigned one) or `ACS_CONNECTION_STRING`, `BUG_NOTIFY_SENDER`,
`BUG_NOTIFY_DOMAINS` (comma list; an address outside it is refused). Any `notifier(notice) -> message id` that
raises on failure works in its place.

Open-by-default for POST and single-bug GET is deliberate: any user (or agent) can file, and the user who filed should be able to read back their own report. Tighten in your own auth wrapper if your threat model needs it.

## Stores

| Class | Purpose |
|---|---|
| `InMemoryStore` | Tests, dev. Dict-backed; lost on process restart. |
| `FilesystemStore(root)` | Single-host production. One JSON per bug + audit JSONL + PNG. What `claude-tmux-dashboard` runs in production. |

To plug in another backend (SQLite, Postgres, S3, …) implement the `Store` protocol — see [`bug_report/store.py`](bug_report/store.py). The blueprint depends on the protocol only.

## Tests

```bash
cd packages/backend-python
pip install -e ".[test]"
pytest
```

The suite is parametrized over both built-in stores, so the same 21 scenarios run twice — 42 tests total. Any new `Store` implementation should be added to the `store` fixture and will be exercised by the same contract.

## Invariants enforced

- **Every successful PATCH writes an audit row.** A no-op (status == current status) MUST NOT add a row.
- **Screenshot is optional always.** POST without one returns 201.
- **`addedBy=agent` auto-adds `agent-self-report` tag.** Agent-filed bugs are first-class entries in the same queue, marked so admins can spot them.
- **Body limits server-enforced.** 10 KB details, 5 MB screenshot. Form/JSON both supported.
