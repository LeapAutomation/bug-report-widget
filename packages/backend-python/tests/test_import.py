"""import_reports: ids, audit, screenshots and votes survive; a second run copies nothing."""
from __future__ import annotations

import copy
import dataclasses

from bug_report import FilesystemStore, InMemoryStore, import_reports
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


def test_a_run_that_died_half_way_is_finished_not_duplicated(tmp_path):
    src = seeded(tmp_path / "bugs")
    dst = InMemoryStore()
    bid = "bug-20261104-100000-dddddd"
    dst.put_bug(dataclasses.replace(src.get_bug(bid), app="x"), None)     # the report, but no audit rows
    dst.append_audit(bid, src.list_audit(bid)[0])                          # and one of its four
    out = import_reports(src, dst, app="x")
    assert out["skipped"] == {"open": 1} and out["copied"] == {"open": 1, "triaged": 1, "declined": 1}
    assert [a.note for a in dst.list_audit(bid)] == [f"row {j}" for j in range(4)]
    assert import_reports(src, dst, app="x")["audit"] == 0


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
