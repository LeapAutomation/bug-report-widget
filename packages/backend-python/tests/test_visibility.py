"""3.2.0: 12-hex ids (reports imported from LeapHQ) and what a non-admin may see:
every feature request plus their own reports; triage stays admin-only. Every store."""
from __future__ import annotations

import time

import pytest
from flask import Flask

from bug_report import create_blueprint, valid_bug_id
from bug_report.store import BugRecord

from test_blueprint import TINY_PNG

ADMIN, ANN, BOB = "admin@example.com", "ann@example.com", "bob@example.com"


def stamp():
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def rec(bid, kind, email, added_by="web", **kw):
    return BugRecord(id=bid, title=f"{kind} by {email}", details="d", tags=[kind], status="open",
                     added_by=added_by, actor_email=email, added_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     kind=kind, **kw)


@pytest.fixture
def world(any_store):
    who = [ADMIN]
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(
        store=any_store, is_admin=lambda r: who[0] == ADMIN,
        actor_identity=lambda r: {"email": who[0], "name": ""}), url_prefix="/api")
    c = app.test_client()
    c.who = who
    ids = {
        "feat_bob": f"bug-{stamp()}-aaaaaa",
        "bug_ann": f"bug-{stamp()}-bbbbbbbbbbbb",      # 12 hex: an imported id
        "bug_bob": f"bug-{stamp()}-cccccccccccc",
        "agent": f"bug-{stamp()}-dddddd",
    }
    any_store.put_bug(rec(ids["feat_bob"], "feature", BOB), TINY_PNG)
    any_store.put_bug(rec(ids["bug_ann"], "bug", ANN), TINY_PNG)
    any_store.put_bug(rec(ids["bug_bob"], "bug", BOB), TINY_PNG)
    any_store.put_bug(rec(ids["agent"], "bug", "", added_by="agent"), TINY_PNG)   # no actor: nobody's own
    c.ids = ids
    return c


def listed(c):
    r = c.get("/api/bugs?days=1")
    assert r.status_code == 200
    return {b["id"] for b in r.get_json()["bugs"]}


@pytest.mark.parametrize("suffix,ok", [("a1b2c3", True), ("a1b2c3d4e5f6", True), ("a1b2c3d", False),
                                       ("a1b2c3d4e5f", False), ("a1b2c3d4e5f60", False), ("A1B2C3", False)])
def test_id_is_six_or_twelve_hex_and_nothing_else(suffix, ok):
    assert valid_bug_id(f"bug-20261115-101010-{suffix}") is ok


def test_a_12_hex_id_works_on_detail_patch_screenshot_and_listing(world):
    c, bid = world, world.ids["bug_ann"]
    assert bid in listed(c)
    assert c.get(f"/api/bugs/{bid}").status_code == 200
    assert c.get(f"/api/bugs/{bid}/screenshot").data == TINY_PNG
    assert c.patch(f"/api/bugs/{bid}", json={"status": "triaged"}).get_json()["status"] == "triaged"


@pytest.mark.parametrize("suffix", ["a1b2c3d", "a1b2c3d4e5f", "a1b2c3d4e5f60"])
def test_7_11_13_hex_ids_are_refused(world, suffix):
    bid = f"bug-20261115-101010-{suffix}"
    assert world.get(f"/api/bugs/{bid}").status_code == 400
    assert world.get(f"/api/bugs/{bid}/screenshot").status_code == 400
    assert world.patch(f"/api/bugs/{bid}", json={"status": "triaged"}).status_code == 400


def test_admin_sees_everything(world):
    assert listed(world) == set(world.ids.values())
    for bid in world.ids.values():
        assert world.get(f"/api/bugs/{bid}").status_code == 200


def test_a_non_admin_sees_features_and_their_own_and_no_one_elses(world):
    c = world
    c.who[0] = ANN
    assert listed(c) == {c.ids["feat_bob"], c.ids["bug_ann"]}
    assert c.get(f"/api/bugs/{c.ids['feat_bob']}").status_code == 200       # a feature
    assert c.get(f"/api/bugs/{c.ids['bug_ann']}").status_code == 200        # her own
    assert c.get(f"/api/bugs/{c.ids['bug_ann']}/screenshot").status_code == 200
    other = c.ids["bug_bob"]
    for call in (lambda i: c.get(f"/api/bugs/{i}"), lambda i: c.get(f"/api/bugs/{i}/screenshot"),
                 lambda i: c.post(f"/api/bugs/{i}/vote"), lambda i: c.delete(f"/api/bugs/{i}/vote")):
        hidden, missing = call(other), call("bug-20200101-000000-ffffff")
        assert hidden.status_code == missing.status_code == 404
        assert hidden.data == missing.data and hidden.content_type == missing.content_type   # no existence leak


def test_the_open_count_of_a_non_admin_counts_only_what_they_see(world):
    world.who[0] = ANN
    r = world.get("/api/bugs?status=open&days=1")
    assert len(r.get_json()["bugs"]) == 2         # not 4


def test_triage_stays_admin_only_even_on_ones_own_report(world):
    c = world
    c.who[0] = ANN
    assert c.patch(f"/api/bugs/{c.ids['bug_ann']}", json={"status": "triaged"}).status_code == 403


def test_voting_on_a_visible_feature_works(world):
    c = world
    c.who[0] = ANN
    fid = c.ids["feat_bob"]
    assert c.post(f"/api/bugs/{fid}/vote").get_json() == {"votes": 1, "voted": True}
    assert {b["id"]: b["votes"] for b in c.get("/api/bugs?days=1").get_json()["bugs"]}[fid] == 1
    c.who[0] = ANN.upper()                                  # the same person, other case: still one vote
    assert c.post(f"/api/bugs/{fid}/vote").get_json()["votes"] == 1
    c.who[0] = ANN
    assert c.delete(f"/api/bugs/{fid}/vote").get_json()["voted"] is False


def test_the_caller_is_matched_ignoring_case_and_spaces(world):
    world.who[0] = "  ANN@Example.COM "
    assert world.ids["bug_ann"] in listed(world)
    assert world.get(f"/api/bugs/{world.ids['bug_ann']}").status_code == 200


def test_a_report_filed_under_a_mixed_case_address_is_its_filers(world, any_store):
    bid = f"bug-{stamp()}-eeeeee"
    any_store.put_bug(rec(bid, "bug", "Ann@Example.COM"), TINY_PNG)
    world.who[0] = ANN
    assert bid in listed(world)
    world.who[0] = BOB
    assert bid not in listed(world)


def test_the_app_default_actor_is_not_a_caller_nobody_sees_its_reports(any_store):
    """default_actor_email credits an unsigned filing; it must not make an anonymous asker that person."""
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(store=any_store, is_admin=lambda r: False,
                                            default_actor_email="owner@example.com"))
    c = app.test_client()
    bid = c.post("/bugs", json={"details": "filed with no one signed in"}).get_json()["id"]
    assert any_store.get_bug(bid).actor_email == "owner@example.com"
    assert c.get("/bugs").get_json() == {"bugs": []}
    assert c.get(f"/bugs/{bid}").status_code == 404


def test_nobody_signed_in_sees_only_features(world):
    world.who[0] = ""
    assert listed(world) == {world.ids["feat_bob"]}
