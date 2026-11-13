"""Closing a report can email the reporter (2.2.0): the blueprint's side.

A fake notifier stands in for ACS; nothing here touches a network."""

from __future__ import annotations

import json

import pytest
from flask import Flask

from bug_report import FilesystemStore, InMemoryStore, create_blueprint
from bug_report.store import AuditEntry, BugRecord

ANA = {"email": "ana.reporter@example.com", "name": "Ana Reporter"}
SAM = {"email": "sam.closer@example.com", "name": "Sam Closer"}


@pytest.fixture(params=["memory", "filesystem"])
def store(request, tmp_path):
    return InMemoryStore() if request.param == "memory" else FilesystemStore(tmp_path / "bugs")


class FakeNotifier:
    def __init__(self, fail=None):
        self.sent = []
        self.fail = fail

    def __call__(self, notice):
        if self.fail:
            raise self.fail
        self.sent.append(notice)
        return "msg-1"


def make(store, notifier):
    """A client whose identity is read from the X-As header: 'ana' or 'sam'."""
    people = {"ana": ANA, "sam": SAM}
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(
        store=store, is_admin=lambda r: True, notifier=notifier, app_url="https://app.example/",
        actor_identity=lambda r: people.get(r.headers.get("X-As", ""), "")))
    return app.test_client()


def file_as_ana(c):
    r = c.post("/bugs", json={"details": "Jam light stays on\nmore", "metaUrl": "https://sv.example/day"},
               headers={"X-As": "ana"})
    return r.get_json()["id"]


def patch(c, bid, **body):
    return c.patch(f"/bugs/{bid}", json=body, headers={"X-As": "sam"})


def to_triaged(c, bid):
    assert patch(c, bid, status="triaged").status_code == 200


# ----- sending -------------------------------------------------------------

def test_notify_sends_once_to_the_reporter_with_the_closer_as_reply_to(store):
    n = FakeNotifier()
    c = make(store, n)
    bid = file_as_ana(c)
    to_triaged(c, bid)
    r = patch(c, bid, status="resolved", note="Fixed in build 41, thanks Ana", notify=True)
    assert r.status_code == 200
    assert len(n.sent) == 1
    s = n.sent[0]
    assert (s.to_email, s.to_name) == (ANA["email"], "Ana Reporter")
    assert (s.closer_email, s.closer_name) == (SAM["email"], "Sam Closer")
    assert s.status == "resolved" and s.note == "Fixed in build 41, thanks Ana"
    assert s.title == "Jam light stays on" and s.bug_id == bid
    assert s.page_url == "https://sv.example/day" and s.app_url == "https://app.example/"
    assert r.get_json()["notified"] == {"ok": True, "to": ANA["email"]}


def test_declined_sends_with_declined_status(store):
    n = FakeNotifier()
    c = make(store, n)
    bid = file_as_ana(c)
    patch(c, bid, status="declined", note="Works as designed", notify=True)
    assert [s.status for s in n.sent] == ["declined"]


def test_the_change_and_its_audit_row_come_first_then_an_emailed_row(store):
    c = make(store, FakeNotifier())
    bid = file_as_ana(c)
    to_triaged(c, bid)
    audit = patch(c, bid, status="resolved", note="done", notify=True).get_json()["audit"]
    assert [(a["fromStatus"], a["toStatus"]) for a in audit] == [
        (None, "open"), ("open", "triaged"), ("triaged", "resolved"), ("resolved", "resolved")]
    last = audit[-1]
    assert last["note"] == "emailed Ana Reporter <ana.reporter@example.com>"
    # who: the signed-in address and name, never the request body
    assert (last["changedBy"], last["changedByName"]) == (SAM["email"], "Sam Closer")
    assert (audit[2]["changedBy"], audit[2]["changedByName"]) == (SAM["email"], "Sam Closer")


def test_a_failing_send_still_resolves_and_writes_the_failure_row(store):
    c = make(store, FakeNotifier(fail=RuntimeError("ACS said no")))
    bid = file_as_ana(c)
    to_triaged(c, bid)
    r = patch(c, bid, status="resolved", note="done", notify=True)
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "resolved"
    assert body["notified"] == {"ok": False, "to": ANA["email"], "error": "ACS said no"}
    assert body["audit"][-1]["note"] == "email to Ana Reporter <ana.reporter@example.com> failed: ACS said no"
    assert store.get_bug(bid).status == "resolved"


def test_a_notifier_that_raises_with_no_message_still_gives_a_reason(store):
    c = make(store, FakeNotifier(fail=RuntimeError()))
    bid = file_as_ana(c)
    to_triaged(c, bid)
    assert patch(c, bid, status="resolved", note="x", notify=True).get_json()["notified"]["error"] == "RuntimeError"


# ----- when it must NOT send -----------------------------------------------

@pytest.mark.parametrize("body", [
    {"status": "resolved", "note": "done"},                      # notify absent
    {"status": "resolved", "note": "done", "notify": False},
    {"status": "resolved", "note": "done", "notify": "true"},    # only a real true counts
])
def test_without_notify_true_nothing_is_sent_and_no_notified_key(store, body):
    n = FakeNotifier()
    c = make(store, n)
    bid = file_as_ana(c)
    to_triaged(c, bid)
    r = patch(c, bid, **body)
    assert n.sent == [] and "notified" not in r.get_json()
    assert len(r.get_json()["audit"]) == 3  # filed, triaged, resolved: no emailed row


def test_reopening_never_emails_even_with_notify(store):
    n = FakeNotifier()
    c = make(store, n)
    bid = file_as_ana(c)
    patch(c, bid, status="declined", note="no")
    r = patch(c, bid, status="open", note="back on", notify=True)
    assert r.status_code == 200
    assert n.sent == [] and "notified" not in r.get_json()


def test_notify_on_a_status_that_is_not_a_close_is_ignored(store):
    n = FakeNotifier()
    c = make(store, n)
    bid = file_as_ana(c)
    r = patch(c, bid, status="triaged", notify=True)
    assert r.status_code == 200 and n.sent == [] and "notified" not in r.get_json()


def test_the_same_status_is_a_no_op_and_sends_nothing(store):
    n = FakeNotifier()
    c = make(store, n)
    bid = file_as_ana(c)
    patch(c, bid, status="declined", note="no")
    r = patch(c, bid, status="declined", note="again", notify=True)
    assert n.sent == [] and "notified" not in r.get_json()


@pytest.mark.parametrize("note", [None, "", "   "])
def test_notify_needs_a_note_and_changes_nothing_without_one(store, note):
    n = FakeNotifier()
    c = make(store, n)
    bid = file_as_ana(c)
    to_triaged(c, bid)
    body = {"status": "resolved", "notify": True}
    if note is not None:
        body["note"] = note
    r = patch(c, bid, **body)
    assert r.status_code == 400
    assert r.get_json() == {"error": "a note for the reporter is required to email them"}
    assert store.get_bug(bid).status == "triaged" and n.sent == []


# ----- no notifier / no address ---------------------------------------------

def test_no_notifier_configured_gives_ok_false_and_an_audit_row(store):
    c = make(store, None)
    bid = file_as_ana(c)
    to_triaged(c, bid)
    r = patch(c, bid, status="resolved", note="done", notify=True)
    body = r.get_json()
    assert r.status_code == 200 and body["status"] == "resolved"
    assert body["notified"]["ok"] is False and "not set up" in body["notified"]["error"]
    assert body["audit"][-1]["note"].startswith("email to Ana Reporter <ana.reporter@example.com> failed:")


def test_a_report_with_no_address_cannot_be_emailed(store):
    n = FakeNotifier()
    c = make(store, n)
    bid = c.post("/bugs", json={"details": "anonymous"}).get_json()["id"]  # no X-As: no identity
    patch(c, bid, status="triaged")
    body = patch(c, bid, status="resolved", note="done", notify=True).get_json()
    assert n.sent == []
    assert body["notified"] == {"ok": False, "to": "", "error": "this report has no address to email"}
    assert "failed: this report has no address" in body["audit"][-1]["note"]


# ----- canNotify -------------------------------------------------------------

def test_can_notify_needs_a_notifier_and_an_address(store):
    with_n = make(store, FakeNotifier())
    with_addr = file_as_ana(with_n)
    no_addr = with_n.post("/bugs", json={"details": "x"}).get_json()["id"]
    assert with_n.get(f"/bugs/{with_addr}").get_json()["canNotify"] is True
    assert with_n.get(f"/bugs/{no_addr}").get_json()["canNotify"] is False
    assert make(store, None).get(f"/bugs/{with_addr}").get_json()["canNotify"] is False


def test_can_notify_is_on_the_detail_not_the_list(store):
    c = make(store, FakeNotifier())
    file_as_ana(c)
    assert "canNotify" not in c.get("/bugs").get_json()["bugs"][0]


# ----- names ------------------------------------------------------------------

def test_actor_name_is_stored_at_filing_and_served(store):
    c = make(store, None)
    bid = file_as_ana(c)
    assert store.get_bug(bid).actor_name == "Ana Reporter"
    assert c.get(f"/bugs/{bid}").get_json()["actorName"] == "Ana Reporter"
    assert c.get("/bugs").get_json()["bugs"][0]["actorName"] == "Ana Reporter"
    assert c.get(f"/bugs/{bid}").get_json()["audit"][0]["changedByName"] == "Ana Reporter"


def test_a_str_returning_actor_identity_still_works(store):
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(store=store, actor_identity=lambda r: "old.style@example.com"))
    c = app.test_client()
    bid = c.post("/bugs", json={"details": "d"}).get_json()["id"]
    rec = store.get_bug(bid)
    assert (rec.actor_email, rec.actor_name) == ("old.style@example.com", "")
    r = c.patch(f"/bugs/{bid}", json={"status": "triaged", "changedBy": "claimed"})
    audit = r.get_json()["audit"][-1]
    assert (audit["changedBy"], audit["changedByName"]) == ("old.style@example.com", "")


def test_the_identity_not_the_body_decides_changed_by(store):
    c = make(store, None)
    bid = file_as_ana(c)
    r = c.patch(f"/bugs/{bid}", json={"status": "triaged", "changedBy": "mallory", "changedByName": "Mallory"},
                headers={"X-As": "sam"})
    a = r.get_json()["audit"][-1]
    assert (a["changedBy"], a["changedByName"]) == (SAM["email"], "Sam Closer")


def test_without_a_hook_the_body_still_supplies_changed_by_and_its_name(store):
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(store=store))
    c = app.test_client()
    bid = c.post("/bugs", json={"details": "d", "actorName": "Pat Lee", "actorEmail": "pat@example.com"}
                 ).get_json()["id"]
    a = c.patch(f"/bugs/{bid}", json={"status": "triaged", "changedBy": "sam@example.com", "changedByName": "Sam"}
                ).get_json()
    assert store.get_bug(bid).actor_name == "Pat Lee"
    assert (a["audit"][-1]["changedBy"], a["audit"][-1]["changedByName"]) == ("sam@example.com", "Sam")


def test_the_name_falls_back_to_the_address_in_the_notice_and_the_audit_note(store):
    n = FakeNotifier()
    c = make(store, n)
    # reporter with an address but no stored name (filed before 2.2.0)
    store.put_bug(BugRecord(id="bug-20260101-000000-aaaaaa", title="t", details="d", tags=["bug"],
                            status="open", added_by="web", actor_email="ana.reporter@example.com",
                            added_at="2099-01-01T00:00:00.000000Z"), None)
    body = patch(c, "bug-20260101-000000-aaaaaa", status="declined", note="no", notify=True).get_json()
    assert n.sent[0].to_name == "Ana Reporter"
    assert body["audit"][-1]["note"] == "emailed Ana Reporter <ana.reporter@example.com>"


# ----- old data ------------------------------------------------------------------

def test_a_report_and_audit_on_disk_without_the_new_fields_still_load(tmp_path):
    st = FilesystemStore(tmp_path)
    old = {"id": "bug-20260101-000000-bbbbbb", "title": "t", "details": "d", "tags": ["bug"],
           "status": "open", "added_by": "web", "actor_email": "a@example.com",
           "added_at": "2099-01-01T00:00:00.000000Z", "meta_url": "", "meta_user_agent": "",
           "meta_build_sha": "", "screenshot": False, "transcript": None}
    (tmp_path / "bug-20260101-000000-bbbbbb.json").write_text(json.dumps(old))
    (tmp_path / "bug-20260101-000000-bbbbbb.audit.jsonl").write_text(json.dumps(
        {"changedAt": "2099-01-01T00:00:00.000000Z", "changedBy": "a@example.com", "fromStatus": None,
         "toStatus": "open", "note": "filed"}) + "\n")
    assert st.get_bug(old["id"]).actor_name == ""
    assert st.list_audit(old["id"])[0].changed_by_name == ""
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(store=st))
    full = app.test_client().get(f"/bugs/{old['id']}").get_json()
    assert full["actorName"] == "" and full["audit"][0]["changedByName"] == ""


def test_the_old_constructors_still_work():
    BugRecord("i", "t", "d", [], "open", "web", "a@example.com", "2099")
    AuditEntry("2099", "open")
