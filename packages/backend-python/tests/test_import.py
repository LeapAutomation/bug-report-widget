"""import_reports: ids, audit, screenshots and votes survive; a second run copies nothing."""
from __future__ import annotations

import copy
import dataclasses

import pytest

from bug_report import FilesystemStore, ImportMismatch, InMemoryStore, import_reports
from bug_report.store import AuditEntry, BugRecord

SHOT = b"\x89PNG\r\n\x1a\nfake"


def seeded(root):
    src = FilesystemStore(root)
    specs = [("bug-20261101-100000-aaaaaa", "open", True), ("bug-20261102-100000-bbbbbb", "triaged", False),
             ("bug-20261103-100000-cccccc", "declined", False), ("bug-20261104-100000-dddddd", "open", False)]
    for i, (bid, status, shot) in enumerate(specs):
        src.put_bug(BugRecord(id=bid, title=bid, details="d", tags=["bug"], status=status, added_by="web",
                              actor_email="a@example.com", added_at=f"2026-11-0{i+1}T10:00:00.000000Z"),
                    SHOT if shot else None)
        for j in range(i + 1):
            src.append_audit(bid, AuditEntry(changed_at=f"2026-11-0{i+1}T10:0{j}:00Z", to_status=status,
                                             from_status=None if j == 0 else status, note=f"row {j}"))
    src.add_vote("bug-20261101-100000-aaaaaa", "v1@example.com")
    src.add_vote("bug-20261101-100000-aaaaaa", "v2@example.com")
    return src


def everything(store):
    out = {}
    for r in store.list_bugs(status=None, since_iso=""):
        try:
            shot = store.get_screenshot(r.id)
        except Exception:
            shot = None
        out[r.id] = (dataclasses.asdict(r), [a.to_dict() for a in store.list_audit(r.id)], shot,
                     sorted(v["voter"] for v in store.list_votes(r.id)))
    return out


def test_import_into_postgres_keeps_everything_and_a_second_run_copies_nothing(tmp_path, pg_store):
    src = seeded(tmp_path / "bugs")
    first = import_reports(src, pg_store, app="placements")
    assert first["copied"] == {"open": 2, "triaged": 1, "declined": 1}
    assert first["skipped"] == {}
    assert (first["audit"], first["screenshots"], first["votes"]) == (1 + 2 + 3 + 4, 1, 2)

    got, want = everything(pg_store), everything(src)
    snapshot = copy.deepcopy(got)
    assert got.keys() == want.keys()
    for bid in want:
        rec_want, audit_want, shot_want, votes_want = want[bid]
        rec_got, audit_got, shot_got, votes_got = got[bid]
        assert rec_got.pop("app") == "placements" and rec_want.pop("app") is None   # stamped, nothing else changed
        assert (rec_got, audit_got, shot_got, votes_got) == (rec_want, audit_want, shot_want, votes_want)

    second = import_reports(src, pg_store, app="placements")
    assert second["copied"] == {}
    assert second["skipped"] == first["copied"]            # the same counts, now all skipped
    assert (second["audit"], second["screenshots"], second["votes"]) == (0, 0, 0)
    assert everything(pg_store) == snapshot                     # and nothing changed under it


def test_a_source_edited_between_runs_raises_naming_the_ids_and_changes_nothing(tmp_path):
    src = seeded(tmp_path / "bugs")
    dst = InMemoryStore()
    import_reports(src, dst, app="report")
    before = everything(dst)
    before = copy.deepcopy(before)
    edits = {
        "status": lambda i: src.update_status(i, "triaged"),
        "triage field": lambda i: src.update_fields(i, {"area": "Belt"}),
        "vote": lambda i: src.add_vote(i, "late@example.com"),
        "audit row": lambda i: src.append_audit(i, AuditEntry(changed_at="2026-12-01T00:00:00Z", to_status="open", note="late")),
    }
    ids = ["bug-20261101-100000-aaaaaa", "bug-20261103-100000-cccccc", "bug-20261104-100000-dddddd", "bug-20261102-100000-bbbbbb"]
    for (label, edit), bid in zip(edits.items(), ids):
        edit(bid)
    # a report added to the source after the first run would be new, not a difference
    with pytest.raises(ImportMismatch) as e:
        import_reports(src, dst, app="report")
    assert sorted(e.value.ids) == sorted(ids)
    assert all(i in str(e.value) for i in ids)
    assert everything(dst) == before                       # nothing was written, new or old


def test_the_last_audit_row_is_compared_not_just_the_count(tmp_path):
    src = seeded(tmp_path / "bugs")
    dst = InMemoryStore()
    import_reports(src, dst, app="report")
    bid = "bug-20261103-100000-cccccc"
    audit_path = src._audit_path(bid)
    lines = audit_path.read_text().splitlines()
    lines[-1] = lines[-1].replace("row 2", "row EDITED")      # same number of rows, last one changed
    audit_path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ImportMismatch) as e:
        import_reports(src, dst, app="report")
    assert e.value.ids == [bid]


def test_a_run_that_died_half_way_is_reported_not_silently_finished(tmp_path):
    src = seeded(tmp_path / "bugs")
    dst = InMemoryStore()
    bid = "bug-20261104-100000-dddddd"
    dst.put_bug(dataclasses.replace(src.get_bug(bid), app="report"), None)     # the report, but no audit rows
    with pytest.raises(ImportMismatch) as e:
        import_reports(src, dst, app="report")
    assert e.value.ids == [bid]


@pytest.mark.parametrize("bad", ["", "nope", "Placements", None])
def test_the_app_must_be_one_the_contract_knows_and_nothing_is_touched(tmp_path, bad):
    src = seeded(tmp_path / "bugs")
    dst = InMemoryStore()
    with pytest.raises(ValueError):
        import_reports(src, dst, app=bad)
    assert list(dst.list_bugs(status=None, since_iso="")) == []


def test_an_app_already_on_a_report_is_kept(tmp_path):
    src = seeded(tmp_path / "bugs")
    rec = src.get_bug("bug-20261101-100000-aaaaaa")
    rec.app = "leaphq"
    src.put_bug(rec, None)
    dst = InMemoryStore()
    import_reports(src, dst, app="sealviewer")
    assert dst.get_bug(rec.id).app == "leaphq"
    assert dst.get_bug("bug-20261102-100000-bbbbbb").app == "sealviewer"


def test_the_copy_does_not_alias_the_source_record():
    src = InMemoryStore()
    rec = BugRecord(id="bug-20261101-100000-aaaaaa", title="t", details="d", tags=[], status="open",
                    added_by="web", actor_email="", added_at="2026-11-01T10:00:00.000000Z")
    src.put_bug(rec, None)
    dst = InMemoryStore()
    import_reports(src, dst, app="report")
    assert src.get_bug(rec.id).app is None and dst.get_bug(rec.id).app == "report"
