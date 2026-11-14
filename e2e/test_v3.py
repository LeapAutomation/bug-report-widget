"""Contract v3 over real HTTP, against every backend (Python on files, Python on
Postgres, Node): new summary fields, triage PATCH, duplicates, votes, filters.

Each backend here is booted with a caller (`X-Test-User`) and an app stamp
(`X-Test-App`); a real service takes both from a verified token, see
bug_report.trust. Reports are found by a per-test marker in the title, because
the servers are shared across tests.
"""
from __future__ import annotations

import uuid

import requests

ME = {"X-Test-User": "admin@example.com"}
V3_SUMMARY = ("kind", "app", "product", "area", "priority", "owner", "parentId", "duplicateOf", "votes")


def mark() -> str:
    return "V3-" + uuid.uuid4().hex[:8]


def file(url, m, app=None, **kw):
    h = {**ME, **({"X-Test-App": app} if app else {})}
    r = requests.post(url, json={"details": f"{m} {kw.pop('d', 'x')}", **kw}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def patch(url, bid, **body):
    return requests.patch(f"{url}/{bid}", json=body, headers=ME)


def listing(url, m, q=""):
    rows = requests.get(f"{url}?days=1{q}").json()["bugs"]
    return [r["id"] for r in rows if m in r["title"]]


def test_a_new_report_carries_every_v3_field_with_defaults(v3_url):
    bid = file(v3_url, mark())
    for rec in (requests.get(f"{v3_url}/{bid}").json(),
                next(r for r in requests.get(f"{v3_url}?days=1").json()["bugs"] if r["id"] == bid)):
        assert all(k in rec for k in V3_SUMMARY), rec
        assert (rec["app"], rec["product"], rec["area"], rec["priority"], rec["owner"],
                rec["parentId"], rec["duplicateOf"], rec["votes"], rec["kind"]) == (
            None, None, None, None, None, None, None, 0, "bug")


def test_the_app_stamp_comes_from_the_caller_and_product_follows(v3_url):
    r = requests.post(v3_url, json={"details": "x", "app": "leaphq"}, headers={**ME, "X-Test-App": "sealviewer"})
    assert r.json()["app"] == "sealviewer"
    rec = requests.get(f"{v3_url}/{r.json()['id']}").json()
    assert (rec["app"], rec["product"]) == ("sealviewer", "Seal")
    # the body's claim alone stamps nothing
    bid = requests.post(v3_url, json={"details": "x", "app": "leaphq"}, headers=ME).json()["id"]
    assert requests.get(f"{v3_url}/{bid}").json()["app"] is None


def test_kind_is_stored_or_derived(v3_url):
    m = mark()
    a = file(v3_url, m, tags=["feature-request"])
    b = file(v3_url, m, kind="feature", tags=["bug"])
    c = file(v3_url, m)
    kinds = {r["id"]: r["kind"] for r in requests.get(f"{v3_url}?days=1").json()["bugs"]}
    assert (kinds[a], kinds[b], kinds[c]) == ("feature", "feature", "bug")


def test_triage_patch_without_a_status_and_its_audit_row(v3_url):
    bid = file(v3_url, mark())
    r = patch(v3_url, bid, area="Cutter", priority="now", owner="sam@example.com")
    assert r.status_code == 200, r.text
    rec = r.json()
    assert (rec["area"], rec["priority"], rec["owner"], rec["status"]) == ("Cutter", "now", "sam@example.com", "open")
    assert len(rec["audit"]) == 2
    assert rec["audit"][1]["note"] == "area: none -> Cutter; priority: none -> now; owner: none -> sam@example.com"
    assert rec["audit"][1]["fromStatus"] == rec["audit"][1]["toStatus"] == "open"
    # unchanged: no new row; cleared: says so
    assert len(patch(v3_url, bid, area="Cutter").json()["audit"]) == 2
    assert patch(v3_url, bid, area=None).json()["audit"][-1]["note"] == "area: Cutter -> none"


def test_triage_patch_validation(v3_url):
    bid = file(v3_url, mark())
    for body in ({"priority": "urgent"}, {"owner": "nobody"}, {"parentId": "junk"}, {"duplicateOf": bid}, {"parentId": bid}):
        assert patch(v3_url, bid, **body).status_code == 400, body
    assert patch(v3_url, bid, parentId="bug-20200101-000000-aaaaaa").status_code == 404
    assert requests.patch(f"{v3_url}/{bid}", json={"note": "x"}, headers=ME).status_code == 400


def test_parent_chain_cannot_loop(v3_url):
    a, b = file(v3_url, mark()), file(v3_url, mark())
    assert patch(v3_url, a, parentId=b).status_code == 200
    assert patch(v3_url, b, parentId=a).status_code == 400


def test_duplicate_of_declines_with_the_note(v3_url):
    a, b = file(v3_url, mark()), file(v3_url, mark())
    rec = patch(v3_url, b, duplicateOf=a).json()
    assert (rec["status"], rec["duplicateOf"]) == ("declined", a)
    assert rec["audit"][-1]["note"] == f"duplicate of {a}" and rec["audit"][-1]["toStatus"] == "declined"
    assert patch(v3_url, file(v3_url, mark()), duplicateOf=a, status="triaged").status_code == 400


def test_list_filters(v3_url):
    m = mark()
    a = file(v3_url, m, app="stoppages", tags=["feature"])
    b = file(v3_url, m, app="placements")
    patch(v3_url, a, area="Clips", priority="now")
    patch(v3_url, b, area="Belt", priority="later")
    assert listing(v3_url, m, "&app=stoppages") == [a]
    assert listing(v3_url, m, "&kind=feature") == [a]
    assert listing(v3_url, m, "&kind=bug") == [b]
    assert listing(v3_url, m, "&area=Belt") == [b]
    assert listing(v3_url, m, "&priority=now") == [a]
    assert listing(v3_url, m, "&app=stoppages&priority=later") == []
    assert requests.get(f"{v3_url}?kind=wat").status_code == 400
    assert requests.get(f"{v3_url}?priority=wat").status_code == 400


def test_votes_one_per_person(v3_url):
    bid = file(v3_url, mark())
    vote = lambda who, method="post": getattr(requests, method)(  # noqa: E731
        f"{v3_url}/{bid}/vote", headers={"X-Test-User": who} if who else {})
    assert vote("a@example.com").json() == {"votes": 1, "voted": True}
    assert vote("A@example.com").json()["votes"] == 1
    assert vote("b@example.com").json()["votes"] == 2
    assert requests.get(f"{v3_url}/{bid}").json()["votes"] == 2
    assert vote("a@example.com", "delete").json() == {"votes": 1, "voted": False}
    assert vote("").status_code == 401
    assert requests.post(f"{v3_url}/bug-20200101-000000-aaaaaa/vote", headers=ME).status_code == 404
