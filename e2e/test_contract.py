"""Cross-stack contract tests.

Every test runs against both backends via the parametrized ``backend_url``
fixture. If a behaviour passes for Python but fails for Node (or vice
versa), that's drift this suite exists to catch — fix the loser, don't
loosen the test.
"""

from __future__ import annotations

import base64
import re

import requests


TINY_PNG = bytes.fromhex(
    "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4890000000D"
    "49444154789C6300010000000500010D0A2DB40000000049454E44AE426082"
)
TINY_PNG_B64 = base64.b64encode(TINY_PNG).decode()
TINY_JPEG = bytes.fromhex("FFD8FFE000104A46494600010100000100010000FFD9")
TINY_JPEG_B64 = base64.b64encode(TINY_JPEG).decode()
ID_RE = re.compile(r"^bug-\d{8}-\d{6}-[a-f0-9]{6}$")


# ----- POST ----------------------------------------------------------

def test_post_minimal(backend_url):
    r = requests.post(backend_url, json={"details": "stuck in submit"})
    assert r.status_code == 201
    bid = r.json()["id"]
    assert ID_RE.match(bid), bid


def test_post_accepts_widget_1_6_optional_fields(backend_url):
    """Widget 1.6.0 may send `clientKey` and `screenshotError`. Both are optional,
    and a server that does not use them (these two) must still file the report."""
    r = requests.post(backend_url, json={
        "details": "from widget 1.6.0",
        "clientKey": "123e4567-e89b-12d3-a456-426614174000",
        "screenshotError": "capture timed out after 6s",
        "screenshot": None,
    })
    assert r.status_code == 201, r.text
    assert ID_RE.match(r.json()["id"])


def test_post_without_details(backend_url):
    assert requests.post(backend_url, json={}).status_code == 400


def test_post_oversized_details(backend_url):
    huge = "x" * (10 * 1024 + 1)
    assert requests.post(backend_url, json={"details": huge}).status_code == 413


def test_post_oversized_screenshot(backend_url):
    big = base64.b64encode(b"\0" * (5 * 1024 * 1024 + 1)).decode()
    assert requests.post(backend_url, json={"details": "x", "screenshot": big}).status_code == 413


def test_agent_auto_tags(backend_url):
    r = requests.post(backend_url, json={
        "details": "tool returned bad json", "addedBy": "agent", "tags": ["bug", "investigate"],
    })
    rec = requests.get(f"{backend_url}/{r.json()['id']}").json()
    assert "agent-self-report" in rec["tags"]
    assert "investigate" in rec["tags"]


def test_post_writes_initial_audit_row(backend_url):
    r = requests.post(backend_url, json={"details": "audit me"})
    rec = requests.get(f"{backend_url}/{r.json()['id']}").json()
    assert len(rec["audit"]) == 1
    assert rec["audit"][0]["fromStatus"] is None
    assert rec["audit"][0]["toStatus"] == "open"
    assert rec["audit"][0]["note"] == "filed"


# ----- GET list ------------------------------------------------------

def test_list_newest_first(backend_url):
    # Use unique titles to identify our 3 bugs amid whatever else is in the
    # shared FilesystemStore from earlier tests.
    marker = "TEST_LIST_NEWEST_FIRST"
    ids = []
    for i in range(3):
        r = requests.post(backend_url, json={"details": f"{marker} {i}"})
        ids.append(r.json()["id"])
    rows = requests.get(backend_url).json()["bugs"]
    rows = [r for r in rows if marker in r["title"]]
    assert [r["id"] for r in rows] == list(reversed(ids))


def test_list_filtered_by_status(backend_url):
    marker = "TEST_LIST_FILTERED"
    a = requests.post(backend_url, json={"details": f"{marker} A"}).json()["id"]
    b = requests.post(backend_url, json={"details": f"{marker} B"}).json()["id"]
    requests.patch(f"{backend_url}/{a}", json={"status": "triaged"})
    open_only = requests.get(f"{backend_url}?status=open").json()["bugs"]
    ids = [r["id"] for r in open_only if marker in r["title"]]
    assert ids == [b]


# ----- GET single ----------------------------------------------------

def test_get_single_returns_audit(backend_url):
    bid = requests.post(backend_url, json={"details": "single get"}).json()["id"]
    rec = requests.get(f"{backend_url}/{bid}").json()
    assert rec["id"] == bid
    assert isinstance(rec["audit"], list) and len(rec["audit"]) == 1


def test_get_invalid_id(backend_url):
    assert requests.get(f"{backend_url}/not-a-bug").status_code == 400


def test_get_unknown_id(backend_url):
    assert requests.get(f"{backend_url}/bug-20260101-000000-aaaaaa").status_code == 404


# ----- PATCH ---------------------------------------------------------

def test_patch_transitions_and_writes_audit(backend_url):
    bid = requests.post(backend_url, json={"details": "to triage"}).json()["id"]
    r = requests.patch(f"{backend_url}/{bid}", json={"status": "triaged", "note": "looking"})
    assert r.status_code == 200
    audit = requests.get(f"{backend_url}/{bid}").json()["audit"]
    assert len(audit) == 2
    assert audit[-1]["fromStatus"] == "open"
    assert audit[-1]["toStatus"] == "triaged"
    assert audit[-1]["note"] == "looking"


def test_patch_invalid_status(backend_url):
    bid = requests.post(backend_url, json={"details": "x"}).json()["id"]
    assert requests.patch(f"{backend_url}/{bid}", json={"status": "wat"}).status_code == 400


def test_patch_same_status_noop(backend_url):
    """Spec invariant: PATCH to current status MUST NOT add an audit row."""
    bid = requests.post(backend_url, json={"details": "noop"}).json()["id"]
    requests.patch(f"{backend_url}/{bid}", json={"status": "open"})
    audit = requests.get(f"{backend_url}/{bid}").json()["audit"]
    assert len(audit) == 1, "no-op PATCH must not add an audit row"


# ----- Screenshot ----------------------------------------------------

def test_screenshot_roundtrip(backend_url):
    bid = requests.post(backend_url, json={
        "details": "with shot",
        "screenshot": f"data:image/png;base64,{TINY_PNG_B64}",
    }).json()["id"]
    r = requests.get(f"{backend_url}/{bid}/screenshot")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content == TINY_PNG


def test_jpeg_screenshot_keeps_its_own_content_type(backend_url):
    """Both stacks must label a JPEG as a JPEG.

    The widget emits JPEG whenever a PNG capture would exceed the 5 MB cap,
    so this is the ordinary path for an image-heavy page — and a consumer
    sending `X-Content-Type-Options: nosniff` should not have to rely on
    browser leniency to render its own triage queue.
    """
    bid = requests.post(backend_url, json={
        "details": "jpeg shot",
        "screenshot": f"data:image/jpeg;base64,{TINY_JPEG_B64}",
    }).json()["id"]
    r = requests.get(f"{backend_url}/{bid}/screenshot")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")
    assert r.content == TINY_JPEG


def test_screenshot_type_is_sniffed_not_trusted(backend_url):
    """The data: URL's declared type is the client's claim about the bytes.
    Both stacks must describe what they actually stored."""
    bid = requests.post(backend_url, json={
        "details": "lying data url",
        "screenshot": f"data:image/png;base64,{TINY_JPEG_B64}",
    }).json()["id"]
    r = requests.get(f"{backend_url}/{bid}/screenshot")
    assert r.headers["content-type"].startswith("image/jpeg")


def test_screenshot_404_when_missing(backend_url):
    bid = requests.post(backend_url, json={"details": "no shot"}).json()["id"]
    assert requests.get(f"{backend_url}/{bid}/screenshot").status_code == 404


# ----- names (backends 2.2.0) ----------------------------------------
# Both backends keep and serve the display names; neither has a mailer in this
# harness, so `notify` must not produce a `notified` block and canNotify is false.

def test_names_are_stored_and_served(backend_url):
    bid = requests.post(backend_url, json={
        "details": "names", "actorEmail": "ana.reporter@example.com", "actorName": "Ana Reporter",
    }).json()["id"]
    requests.patch(f"{backend_url}/{bid}", json={
        "status": "triaged", "changedBy": "sam.closer@example.com", "changedByName": "Sam Closer",
    })
    rec = requests.get(f"{backend_url}/{bid}").json()
    assert rec["actorName"] == "Ana Reporter"
    assert rec["audit"][0]["changedByName"] == "Ana Reporter"
    assert (rec["audit"][1]["changedBy"], rec["audit"][1]["changedByName"]) == (
        "sam.closer@example.com", "Sam Closer")
    row = next(r for r in requests.get(backend_url).json()["bugs"] if r["id"] == bid)
    assert row["actorName"] == "Ana Reporter"


def test_unknown_names_are_empty_strings_not_missing(backend_url):
    bid = requests.post(backend_url, json={"details": "no names"}).json()["id"]
    rec = requests.get(f"{backend_url}/{bid}").json()
    assert rec["actorName"] == ""
    assert rec["audit"][0]["changedByName"] == ""


def test_without_a_mailer_can_notify_is_false_and_notify_is_inert(backend_url):
    bid = requests.post(backend_url, json={"details": "no mailer", "actorEmail": "ana@example.com"}).json()["id"]
    assert requests.get(f"{backend_url}/{bid}").json()["canNotify"] is False
    assert "canNotify" not in requests.get(backend_url).json()["bugs"][0]
    r = requests.patch(f"{backend_url}/{bid}", json={"status": "triaged", "note": "x", "notify": True})
    assert r.status_code == 200 and "notified" not in r.json()
    assert len(r.json()["audit"]) == 2  # filed, triaged: no emailed row
