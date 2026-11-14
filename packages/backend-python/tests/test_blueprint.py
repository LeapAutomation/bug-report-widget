"""Contract tests for the Flask blueprint.

Runs the same scenarios against both reference stores (in-memory and
filesystem) via pytest parametrization. If you add a Store implementation,
add it to the ``store`` fixture and the same tests will exercise it.
"""

from __future__ import annotations

import base64
import re

import pytest
from flask import Flask

from bug_report import (
    FilesystemStore,
    InMemoryStore,
    create_blueprint,
)


# Smallest valid PNG: 1×1 transparent.
TINY_PNG = bytes.fromhex(
    "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4890000000D"
    "49444154789C6300010000000500010D0A2DB40000000049454E44AE426082"
)
TINY_PNG_B64 = base64.b64encode(TINY_PNG).decode()

# Smallest thing that sniffs as a JPEG: SOI + APP0. The widget emits these
# whenever a PNG capture would exceed the size cap, which on an image-heavy
# page is the normal path, not the exotic one.
TINY_JPEG = bytes.fromhex("FFD8FFE000104A46494600010100000100010000FFD9")
TINY_JPEG_B64 = base64.b64encode(TINY_JPEG).decode()


@pytest.fixture(params=["memory", "filesystem", "postgres"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryStore()
    if request.param == "postgres":
        return request.getfixturevalue("pg_store")
    return FilesystemStore(tmp_path / "bugs")


@pytest.fixture
def admin_allow():
    return lambda req: True


@pytest.fixture
def admin_deny():
    return lambda req: False


@pytest.fixture
def client(store, admin_allow):
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(store=store, is_admin=admin_allow))
    app.config["TESTING"] = True
    return app.test_client()


# ---------------------------------------------------------------------------
# POST /bugs
# ---------------------------------------------------------------------------

def test_post_minimal(client):
    r = client.post("/bugs", json={"details": "the kill button stopped working"})
    assert r.status_code == 201
    bid = r.get_json()["id"]
    assert re.match(r"^bug-\d{8}-\d{6}-[a-f0-9]{6}$", bid)


def test_post_requires_details(client):
    assert client.post("/bugs", json={}).status_code == 400
    assert client.post("/bugs", json={"details": "   "}).status_code == 400


def test_post_accepts_form_encoded(client):
    r = client.post("/bugs", data={"details": "from a form post"})
    assert r.status_code == 201


def test_post_rejects_oversized_details(client):
    huge = "x" * (10 * 1024 + 1)
    assert client.post("/bugs", json={"details": huge}).status_code == 413


def test_post_rejects_oversized_screenshot(client):
    big = base64.b64encode(b"\x00" * (5 * 1024 * 1024 + 1)).decode()
    r = client.post("/bugs", json={"details": "x", "screenshot": big})
    assert r.status_code == 413


def test_agent_auto_tags(client):
    """addedBy=agent triggers automatic 'agent-self-report' tag."""
    r = client.post("/bugs", json={
        "details": "I noticed the tool returned malformed JSON",
        "addedBy": "agent",
        "tags": ["bug", "investigate"],
    })
    bid = r.get_json()["id"]
    rec = client.get(f"/bugs/{bid}").get_json()
    assert "agent-self-report" in rec["tags"]
    assert "investigate" in rec["tags"]


def test_post_writes_initial_audit_row(client):
    bid = client.post("/bugs", json={"details": "audit me"}).get_json()["id"]
    rec = client.get(f"/bugs/{bid}").get_json()
    assert len(rec["audit"]) == 1
    assert rec["audit"][0]["fromStatus"] is None
    assert rec["audit"][0]["toStatus"] == "open"


def test_post_with_screenshot_persists_bytes(client):
    r = client.post("/bugs", json={
        "details": "with shot",
        "screenshot": f"data:image/png;base64,{TINY_PNG_B64}",
    })
    bid = r.get_json()["id"]
    shot = client.get(f"/bugs/{bid}/screenshot")
    assert shot.status_code == 200
    assert shot.mimetype == "image/png"
    assert shot.data == TINY_PNG


def test_jpeg_screenshot_is_served_as_jpeg(client):
    """A JPEG must not come back labelled image/png.

    The widget's fallback ladder emits JPEG whenever the PNG would exceed
    BUG_SCREENSHOT_MAX, so this is the ORDINARY case for any page with
    photographs on it. Serving it as image/png leaves a consumer that sends
    `X-Content-Type-Options: nosniff` depending on browser leniency to render
    its own triage queue.
    """
    bid = client.post("/bugs", json={
        "details": "jpeg shot",
        "screenshot": f"data:image/jpeg;base64,{TINY_JPEG_B64}",
    }).get_json()["id"]
    shot = client.get(f"/bugs/{bid}/screenshot")
    assert shot.status_code == 200
    assert shot.mimetype == "image/jpeg"
    assert shot.data == TINY_JPEG


def test_screenshot_type_is_sniffed_not_taken_from_the_data_url(client):
    """The data: URL's declared type is the CLIENT's claim about bytes we are
    about to store. The response type must describe what was actually stored,
    or a mislabelled upload becomes a mislabelled download."""
    bid = client.post("/bugs", json={
        "details": "lying data url",
        "screenshot": f"data:image/png;base64,{TINY_JPEG_B64}",   # says PNG, is JPEG
    }).get_json()["id"]
    assert client.get(f"/bugs/{bid}/screenshot").mimetype == "image/jpeg"


# ---------------------------------------------------------------------------
# GET /bugs
# ---------------------------------------------------------------------------

def test_list_empty(client):
    assert client.get("/bugs").get_json() == {"bugs": []}


def test_list_sorted_newest_first(client):
    ids = [client.post("/bugs", json={"details": f"b{n}"}).get_json()["id"] for n in range(3)]
    rows = client.get("/bugs").get_json()["bugs"]
    assert [r["id"] for r in rows] == list(reversed(ids))


def test_list_filters_by_status(client):
    a = client.post("/bugs", json={"details": "A"}).get_json()["id"]
    b = client.post("/bugs", json={"details": "B"}).get_json()["id"]
    client.patch(f"/bugs/{a}", json={"status": "triaged"})
    rows = client.get("/bugs?status=open").get_json()["bugs"]
    assert [r["id"] for r in rows] == [b]


# ---------------------------------------------------------------------------
# GET /bugs/<id>
# ---------------------------------------------------------------------------

def test_get_returns_audit(client):
    bid = client.post("/bugs", json={"details": "x"}).get_json()["id"]
    rec = client.get(f"/bugs/{bid}").get_json()
    assert rec["id"] == bid
    assert isinstance(rec["audit"], list) and len(rec["audit"]) == 1


def test_get_invalid_id(client):
    assert client.get("/bugs/not-a-bug").status_code == 400


def test_get_unknown_id(client):
    assert client.get("/bugs/bug-20260101-000000-aaaaaa").status_code == 404


# ---------------------------------------------------------------------------
# PATCH /bugs/<id>
# ---------------------------------------------------------------------------

def test_patch_transitions_and_writes_audit(client):
    bid = client.post("/bugs", json={"details": "to triage"}).get_json()["id"]
    r = client.patch(f"/bugs/{bid}", json={"status": "triaged", "note": "looking now"})
    assert r.status_code == 200
    assert r.get_json()["status"] == "triaged"
    audit = client.get(f"/bugs/{bid}").get_json()["audit"]
    assert len(audit) == 2
    assert audit[-1]["fromStatus"] == "open"
    assert audit[-1]["toStatus"] == "triaged"
    assert audit[-1]["note"] == "looking now"


def test_patch_rejects_unknown_status(client):
    bid = client.post("/bugs", json={"details": "x"}).get_json()["id"]
    assert client.patch(f"/bugs/{bid}", json={"status": "wat"}).status_code == 400


def test_patch_noop_when_same_status(client):
    """Spec invariant: PATCH to current status MUST NOT add an audit row."""
    bid = client.post("/bugs", json={"details": "x"}).get_json()["id"]
    client.patch(f"/bugs/{bid}", json={"status": "open"})
    audit = client.get(f"/bugs/{bid}").get_json()["audit"]
    assert len(audit) == 1


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------

def test_screenshot_404_when_missing(client):
    bid = client.post("/bugs", json={"details": "no shot"}).get_json()["id"]
    assert client.get(f"/bugs/{bid}/screenshot").status_code == 404


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------

def test_admin_gate_blocks_list_and_patch(store, admin_deny):
    """`is_admin` returning False MUST 403 the admin routes; POST stays open."""
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(store=store, is_admin=admin_deny))
    c = app.test_client()
    bid = c.post("/bugs", json={"details": "anyone can file"}).get_json()["id"]
    assert c.get("/bugs").status_code == 403
    assert c.patch(f"/bugs/{bid}", json={"status": "triaged"}).status_code == 403


# ---------------------------------------------------------------------------
# Build SHA
# ---------------------------------------------------------------------------

def test_build_sha_static(store):
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(store=store, build_sha="abc1234"))
    c = app.test_client()
    bid = c.post("/bugs", json={"details": "stamp me"}).get_json()["id"]
    assert c.get(f"/bugs/{bid}").get_json()["metaBuildSha"] == "abc1234"


def test_build_sha_callable(store):
    counter = {"n": 0}
    def sha():
        counter["n"] += 1
        return f"v{counter['n']}"
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(store=store, build_sha=sha))
    c = app.test_client()
    b1 = c.post("/bugs", json={"details": "x"}).get_json()["id"]
    b2 = c.post("/bugs", json={"details": "y"}).get_json()["id"]
    assert c.get(f"/bugs/{b1}").get_json()["metaBuildSha"] == "v1"
    assert c.get(f"/bugs/{b2}").get_json()["metaBuildSha"] == "v2"


# ----- actor_identity (2.1.0) ------------------------------------------
# The filer used to come from the payload, and the shipped widget sends none,
# so every app on this package recorded reports with no filer: a merged queue
# could not tell whose report was whose (LeapHQ, 2026-10-03). An app that can
# check who is asking now says so, and the page's claim stops counting.

def _app_with_identity(store, identity):
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(store=store, is_admin=lambda r: True, actor_identity=identity))
    return app.test_client()


def test_actor_identity_decides_the_filer_and_the_payload_claim_is_ignored(store):
    client = _app_with_identity(store, lambda req: req.headers.get("X-Who", ""))
    r = client.post("/bugs", json={"details": "d", "actorEmail": "claimed@x.ai"}, headers={"X-Who": "checked@x.ai"})
    assert r.status_code == 201
    assert store.get_bug(r.get_json()["id"]).actor_email == "checked@x.ai"


def test_actor_identity_answering_nothing_files_with_no_actor_never_the_claim(store):
    client = _app_with_identity(store, lambda req: "")
    r = client.post("/bugs", json={"details": "d", "actorEmail": "claimed@x.ai"})
    assert store.get_bug(r.get_json()["id"]).actor_email == ""


def test_actor_identity_that_raises_files_the_report_with_no_actor(store):
    def boom(req):
        raise RuntimeError("principal unreadable")
    client = _app_with_identity(store, boom)
    r = client.post("/bugs", json={"details": "d", "actorEmail": "claimed@x.ai"})
    assert r.status_code == 201
    assert store.get_bug(r.get_json()["id"]).actor_email == ""


def test_without_actor_identity_the_payload_is_used_as_before(client, store):
    r = client.post("/bugs", json={"details": "d", "actorEmail": "page@x.ai"})
    assert store.get_bug(r.get_json()["id"]).actor_email == "page@x.ai"
