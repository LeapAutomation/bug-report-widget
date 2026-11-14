"""Contract v3 through the blueprint, on every store: triage fields, duplicates,
votes, filters, and a v2 record reading back."""
from __future__ import annotations

import json

import pytest
from flask import Flask

from bug_report import FilesystemStore, create_blueprint, contract
from bug_report.store import BugRecord

ADMIN = "admin@example.com"


def make_client(store, *, who=ADMIN, app_stamp=None):
    """A client whose caller is `who` (a mutable one-item list, so a test can switch person)."""
    cur = [who]
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(
        store=store, is_admin=lambda r: True,
        actor_identity=lambda r: {"email": cur[0], "name": ""} if cur[0] else "",
        app_stamp=app_stamp,
    ), url_prefix="/api")
    c = app.test_client()
    c.person = cur
    return c


@pytest.fixture
def client(any_store):
    return make_client(any_store)


def file_bug(c, **kw):
    r = c.post("/api/bugs", json={"details": "something broke", **kw})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def patch(c, bid, **body):
    return c.patch(f"/api/bugs/{bid}", json=body)


# ---- triage fields ---------------------------------------------------------

def test_triage_fields_set_without_a_status_change_and_audited(client):
    bid = file_bug(client)
    r = patch(client, bid, area="Cutter", priority="now", owner="sam@example.com")
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert (d["area"], d["priority"], d["owner"], d["status"]) == ("Cutter", "now", "sam@example.com", "open")
    rows = d["audit"]
    assert len(rows) == 2                      # filed + ONE row for the fields
    assert rows[1]["note"] == "area: none -> Cutter; priority: none -> now; owner: none -> sam@example.com"
    assert rows[1]["fromStatus"] == rows[1]["toStatus"] == "open"
    assert rows[1]["changedBy"] == ADMIN
    # read back through GET and the list
    assert client.get(f"/api/bugs/{bid}").get_json()["area"] == "Cutter"
    assert client.get("/api/bugs?days=1").get_json()["bugs"][0]["priority"] == "now"


def test_triage_fields_with_a_status_change_write_two_rows(client):
    bid = file_bug(client)
    d = patch(client, bid, status="triaged", note="looking", area="Belt").get_json()
    assert [(a["toStatus"], a["note"]) for a in d["audit"][1:]] == [
        ("triaged", "looking"), ("triaged", "area: none -> Belt")]


def test_an_unchanged_triage_value_is_a_no_op_with_no_audit_row(client):
    bid = file_bug(client)
    patch(client, bid, area="Belt")
    d = patch(client, bid, area="Belt").get_json()
    assert len(d["audit"]) == 2


def test_null_or_empty_clears_a_field_and_says_so(client):
    bid = file_bug(client)
    patch(client, bid, area="Belt", priority="later")
    d = patch(client, bid, area=None, priority="").get_json()
    assert d["area"] is None and d["priority"] is None
    assert d["audit"][-1]["note"] == "area: Belt -> none; priority: later -> none"


@pytest.mark.parametrize("body", [
    {"priority": "urgent"}, {"owner": "not-an-email"}, {"area": "x" * 101},
    {"parentId": "nope"}, {"duplicateOf": "nope"},
])
def test_bad_triage_values_are_400(client, body):
    bid = file_bug(client)
    assert patch(client, bid, **body).status_code == 400


def test_a_patch_with_neither_status_nor_triage_field_is_still_400(client):
    bid = file_bug(client)
    assert patch(client, bid, note="hi").status_code == 400


def test_parent_must_exist_not_be_itself_and_not_loop(client):
    a, b = file_bug(client), file_bug(client)
    assert patch(client, a, parentId="bug-20200101-000000-aaaaaa").status_code == 404
    assert patch(client, a, parentId=a).status_code == 400
    assert patch(client, a, parentId=b).status_code == 200      # b is a's parent
    r = patch(client, b, parentId=a)                             # a is b's parent too: a loop
    assert r.status_code == 400 and "cycle" in r.get_json()["error"]
    assert client.get(f"/api/bugs/{a}").get_json()["parentId"] == b


def test_duplicate_of_declines_with_the_note(client):
    a, b = file_bug(client), file_bug(client)
    d = patch(client, b, duplicateOf=a).get_json()
    assert d["status"] == "declined" and d["duplicateOf"] == a
    assert d["audit"][-1]["toStatus"] == "declined"
    assert d["audit"][-1]["note"] == f"duplicate of {a}"
    assert len(d["audit"]) == 2             # the decline row IS the record of the change


def test_duplicate_of_conflicting_status_is_400_and_a_resolved_report_cannot_be_declined(client):
    a, b, c3 = file_bug(client), file_bug(client), file_bug(client)
    assert patch(client, b, duplicateOf=a, status="triaged").status_code == 400
    assert client.get(f"/api/bugs/{b}").get_json()["status"] == "open"     # nothing half-applied
    patch(client, c3, status="triaged")
    patch(client, c3, status="resolved")
    r = patch(client, c3, duplicateOf=a)
    assert r.status_code == 409
    assert client.get(f"/api/bugs/{c3}").get_json()["duplicateOf"] is None


def test_duplicate_of_on_an_already_declined_report_records_the_field(client):
    a, b = file_bug(client), file_bug(client)
    patch(client, b, status="declined", note="no")
    d = patch(client, b, duplicateOf=a).get_json()
    assert d["status"] == "declined" and d["duplicateOf"] == a
    assert d["audit"][-1]["note"] == f"duplicateOf: none -> {a}"


# ---- kind, app, product ----------------------------------------------------

def test_kind_is_stored_when_given_and_derived_from_tags_when_not(client):
    plain = file_bug(client)
    tagged = file_bug(client, tags=["feature-request"])
    said = file_bug(client, kind="feature", tags=["bug"])
    kinds = {r["id"]: r["kind"] for r in client.get("/api/bugs?days=1").get_json()["bugs"]}
    assert (kinds[plain], kinds[tagged], kinds[said]) == ("bug", "feature", "feature")
    assert client.get(f"/api/bugs/{said}").get_json()["kind"] == "feature"


def test_app_comes_from_the_hook_never_the_body_and_product_follows(any_store):
    c = make_client(any_store, app_stamp=lambda r: "sealviewer")
    r = c.post("/api/bugs", json={"details": "x", "app": "leaphq"})
    assert r.get_json()["app"] == "sealviewer"
    d = c.get(f"/api/bugs/{r.get_json()['id']}").get_json()
    assert (d["app"], d["product"]) == ("sealviewer", "Seal")
    # an app the contract does not know is dropped, not stored
    c2 = make_client(any_store, app_stamp=lambda r: "made-up")
    assert c2.post("/api/bugs", json={"details": "x"}).get_json()["app"] is None
    # no hook: a body claim is ignored
    c3 = make_client(any_store)
    bid = c3.post("/api/bugs", json={"details": "x", "app": "leaphq"}).get_json()["id"]
    assert c3.get(f"/api/bugs/{bid}").get_json()["app"] is None


# ---- list filters ----------------------------------------------------------

def test_list_filters_app_kind_area_priority(any_store):
    apps = {"v": "stoppages"}
    c = make_client(any_store, app_stamp=lambda r: apps["v"])
    a = file_bug(c, tags=["feature"])
    apps["v"] = "placements"
    b = file_bug(c)
    patch(c, a, area="Clips", priority="now")
    patch(c, b, area="Belt", priority="later")

    def ids(q):
        return {r["id"] for r in c.get(f"/api/bugs?days=1&{q}").get_json()["bugs"]}
    assert ids("app=stoppages") == {a}
    assert ids("app=placements") == {b}
    assert ids("kind=feature") == {a} and ids("kind=bug") == {b}
    assert ids("area=Belt") == {b}
    assert ids("priority=now") == {a}
    assert ids("app=stoppages&priority=later") == set()
    assert c.get("/api/bugs?kind=wat").status_code == 400
    assert c.get("/api/bugs?priority=wat").status_code == 400


# ---- votes -----------------------------------------------------------------

def test_one_vote_per_person_and_delete_takes_it_back(client):
    bid = file_bug(client)
    r = client.post(f"/api/bugs/{bid}/vote")
    assert (r.status_code, r.get_json()) == (200, {"votes": 1, "voted": True})
    assert client.post(f"/api/bugs/{bid}/vote").get_json()["votes"] == 1    # twice = once
    client.person[0] = "Chris@example.com"
    assert client.post(f"/api/bugs/{bid}/vote").get_json()["votes"] == 2
    client.person[0] = "chris@EXAMPLE.com"                                   # same person, other case
    assert client.post(f"/api/bugs/{bid}/vote").get_json()["votes"] == 2
    assert client.get(f"/api/bugs/{bid}").get_json()["votes"] == 2
    assert client.get("/api/bugs?days=1").get_json()["bugs"][0]["votes"] == 2
    r = client.delete(f"/api/bugs/{bid}/vote")
    assert r.get_json() == {"votes": 1, "voted": False}
    assert client.delete(f"/api/bugs/{bid}/vote").get_json()["votes"] == 1  # nothing to take back


def test_voting_needs_someone_signed_in_and_a_real_report(client):
    bid = file_bug(client)
    client.person[0] = ""
    assert client.post(f"/api/bugs/{bid}/vote").status_code == 401
    assert client.delete(f"/api/bugs/{bid}/vote").status_code == 401
    client.person[0] = ADMIN
    assert client.post("/api/bugs/bug-20200101-000000-aaaaaa/vote").status_code == 404
    assert client.post("/api/bugs/junk/vote").status_code == 400


def test_a_non_admin_can_still_vote(any_store):
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(
        store=any_store, is_admin=lambda r: False, actor_identity=lambda r: ADMIN))
    c = app.test_client()
    bid = c.post("/bugs", json={"details": "x"}).get_json()["id"]
    assert c.post(f"/bugs/{bid}/vote").status_code == 200
    assert c.patch(f"/bugs/{bid}", json={"area": "x"}).status_code == 403


def test_without_an_identity_hook_the_voter_is_the_payload_claim(any_store):
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(store=any_store))
    c = app.test_client()
    bid = c.post("/bugs", json={"details": "x"}).get_json()["id"]
    assert c.post(f"/bugs/{bid}/vote").status_code == 401
    assert c.post(f"/bugs/{bid}/vote", json={"actorEmail": "a@example.com"}).get_json()["votes"] == 1
    assert c.delete(f"/bugs/{bid}/vote?actorEmail=a@example.com").get_json()["votes"] == 0


# ---- a v2 record -----------------------------------------------------------

V2 = {
    "id": "bug-20261001-100000-abcdef", "title": "old one", "details": "from 2026-10", "tags": ["bug"],
    "status": "open", "added_by": "web", "actor_email": "a@example.com",
    "added_at": "2026-11-13T10:00:00.000000Z",
    "meta_url": "", "meta_user_agent": "", "meta_build_sha": "", "screenshot": False,
    "transcript": None, "actor_name": "A",
}


def seed_v2(store):
    """A record exactly as a v2 store wrote it: none of the v3 keys."""
    if isinstance(store, FilesystemStore):
        (store.root / f"{V2['id']}.json").write_text(json.dumps(V2))
    elif hasattr(store, "_connect"):
        from psycopg.types.json import Jsonb
        with store._connect() as conn:
            conn.execute("INSERT INTO reports(id, status, added_at, record) VALUES(%s,%s,%s,%s)",
                         (V2["id"], "open", "2026-11-13T10:00:00Z", Jsonb(V2)))
    else:
        store.put_bug(BugRecord.from_dict(V2), None)


def test_a_v2_record_reads_back_with_the_new_fields_null_and_zero_votes(any_store):
    seed_v2(any_store)
    c = make_client(any_store)
    for d in (c.get(f"/api/bugs/{V2['id']}").get_json(),
              c.get("/api/bugs?days=3650").get_json()["bugs"][0]):
        assert d["id"] == V2["id"]
        assert [d[k] for k in ("app", "product", "area", "priority", "owner", "parentId", "duplicateOf")] == [None] * 7
        assert d["votes"] == 0 and d["kind"] == "bug"
    # and it can be triaged like any other
    assert patch(c, V2["id"], area="Old").get_json()["area"] == "Old"


def test_a_record_with_keys_this_version_does_not_know_still_loads():
    r = BugRecord.from_dict({**V2, "from_the_future": 1})
    assert r.id == V2["id"] and not hasattr(r, "from_the_future")


def test_every_triage_field_in_the_contract_has_a_record_attribute():
    for wire in contract.TRIAGE_FIELDS:
        attr = {"parentId": "parent_id", "duplicateOf": "duplicate_of"}.get(wire, wire)
        assert attr in BugRecord.__dataclass_fields__
