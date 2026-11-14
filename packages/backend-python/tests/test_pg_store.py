"""PostgresStore against a real Postgres (see pg_support.py)."""
from __future__ import annotations

import dataclasses

import pytest

from bug_report import FilesystemStore
from bug_report.pg import PostgresStore
from bug_report.store import AuditEntry, BugRecord, NotFound, StoreError


def bug(i=1, **over):
    r = BugRecord(id=f"bug-2026110{i}-120000-abc00{i}", title=f"bug {i}", details="it broke",
                  tags=["ui"], status="open", added_by="Ken", actor_email="ken@example.com",
                  added_at=f"2026-11-0{i}T12:00:00.000000Z", meta_url="https://hq/x",
                  transcript=[{"role": "user", "text": "hi"}], actor_name="Ken")
    for k, v in over.items():
        setattr(r, k, v)
    return r


def test_the_record_jsonb_is_what_the_filesystem_store_writes(pg_store, tmp_path):
    b = bug(1, app="stoppages", area="Clips", priority="now", parent_id=None)
    fs = FilesystemStore(tmp_path)
    fs.put_bug(dataclasses.replace(b), b"\x89PNG..")
    pg_store.put_bug(dataclasses.replace(b), b"\x89PNG..")
    import json
    on_disk = json.loads((tmp_path / f"{b.id}.json").read_text())
    with pg_store._connect() as conn:
        (stored,) = conn.execute("SELECT record FROM reports WHERE id=%s", (b.id,)).fetchone()
    assert stored == on_disk                      # same JSON, key for key
    assert pg_store.get_bug(b.id) == fs.get_bug(b.id)
    assert pg_store.get_screenshot(b.id) == b"\x89PNG.."


def test_lifted_columns_follow_the_record(pg_store):
    b = bug(1, app="fleet", area="Belt", priority="later", owner="o@example.com",
            parent_id="bug-20261102-120000-abc002", duplicate_of="bug-20261103-120000-abc003",
            tags=["feature"])
    pg_store.put_bug(b, None)
    with pg_store._connect() as conn:
        row = conn.execute("SELECT app, kind, status, area, priority, owner, parent_id, duplicate_of "
                           "FROM reports WHERE id=%s", (b.id,)).fetchone()
    assert row == ("fleet", "feature", "open", "Belt", "later", "o@example.com",
                   "bug-20261102-120000-abc002", "bug-20261103-120000-abc003")


def test_not_found(pg_store):
    with pytest.raises(NotFound):
        pg_store.get_bug("bug-20200101-000000-aaaaaa")
    with pytest.raises(NotFound):
        pg_store.get_screenshot("bug-20200101-000000-aaaaaa")
    pg_store.put_bug(bug(1), None)
    with pytest.raises(NotFound):
        pg_store.get_screenshot(bug(1).id)           # a report without one
    with pytest.raises(NotFound):
        pg_store.add_vote("bug-20200101-000000-aaaaaa", "a@example.com")


def test_a_resave_keeps_the_screenshot_it_has(pg_store):
    b = bug(1)
    pg_store.put_bug(b, b"shot")
    pg_store.update_status(b.id, "triaged")
    pg_store.update_fields(b.id, {"area": "x"})
    assert pg_store.get_screenshot(b.id) == b"shot"
    assert pg_store.get_bug(b.id).screenshot is True


def test_list_filters_agree_with_the_filesystem_store(pg_store, tmp_path):
    fs = FilesystemStore(tmp_path)
    rows = [bug(1, app="fleet", area="A", priority="now"),
            bug(2, status="triaged", app="sealviewer", tags=["feature"]),
            bug(3, app="fleet", priority="later", kind="feature")]
    for st in (pg_store, fs):
        for r in rows:
            st.put_bug(dataclasses.replace(r), None)
    queries = [dict(status=None, since_iso=""), dict(status="open", since_iso=""),
               dict(status=None, since_iso="2026-11-02T00:00:00Z"),
               dict(status=None, since_iso="", app="fleet"), dict(status=None, since_iso="", kind="feature"),
               dict(status=None, since_iso="", area="A"), dict(status=None, since_iso="", priority="later"),
               dict(status="open", since_iso="", app="fleet", kind="feature")]
    for q in queries:
        assert sorted(r.id for r in pg_store.list_bugs(**q)) == sorted(r.id for r in fs.list_bugs(**q)), q
    assert [r.id for r in pg_store.list_bugs(status=None, since_iso="")] == [r.id for r in rows]   # oldest first


def test_audit_keeps_order_and_every_field(pg_store):
    b = bug(1)
    pg_store.put_bug(b, None)
    entries = [AuditEntry(changed_at=f"2026-11-0{i}T00:00:00Z", to_status="open", from_status=None,
                          changed_by="a@example.com", changed_by_name="A", note=f"n{i}") for i in range(1, 6)]
    for e in entries:
        pg_store.append_audit(b.id, e)
    assert pg_store.list_audit(b.id) == entries
    assert pg_store.list_audit("bug-20200101-000000-aaaaaa") == []


def test_votes_are_one_per_person_counted_and_listed(pg_store):
    a, b = bug(1), bug(2)
    pg_store.put_bug(a, None)
    pg_store.put_bug(b, None)
    assert pg_store.add_vote(a.id, "X@example.com") is True
    assert pg_store.add_vote(a.id, "x@example.com") is False
    assert pg_store.add_vote(a.id, "y@example.com", at="2026-01-01T00:00:00Z") is True
    assert pg_store.vote_counts([a.id, b.id, "nope"]) == {a.id: 2, b.id: 0, "nope": 0}
    assert pg_store.vote_counts([]) == {}
    assert pg_store.list_votes(a.id)[0] == {"voter": "y@example.com", "at": "2026-01-01T00:00:00Z"}
    assert pg_store.remove_vote(a.id, "Y@example.com") is True
    assert pg_store.remove_vote(a.id, "y@example.com") is False


def test_update_fields_refuses_what_is_not_a_triage_field(pg_store):
    pg_store.put_bug(bug(1), None)
    with pytest.raises(StoreError):
        pg_store.update_fields(bug(1).id, {"status": "resolved"})


def test_ensure_schema_is_idempotent_and_keeps_rows(pg_store):
    pg_store.put_bug(bug(1), None)
    pg_store.ensure_schema()
    pg_store.ensure_schema()
    assert pg_store.get_bug(bug(1).id).title == "bug 1"


def test_a_connection_is_taken_per_operation_from_the_factory(pg_schema):
    taken = []

    def factory():
        taken.append(1)
        return pg_schema.connect()
    st = PostgresStore(factory)
    st.ensure_schema()
    st.put_bug(bug(1), None)
    st.get_bug(bug(1).id)
    assert len(taken) == 3        # so a service can hand out a fresh Entra-token password each time


def test_a_database_that_is_down_is_a_store_error_not_a_driver_exception(pg_dsn):
    import psycopg
    st = PostgresStore(lambda: psycopg.connect("postgresql://postgres@127.0.0.1:1/x", connect_timeout=1))
    with pytest.raises(StoreError):
        st.get_bug("bug-20200101-000000-aaaaaa")
