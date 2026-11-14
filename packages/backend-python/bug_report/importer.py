"""Copy one store's reports into another (the migration into the central store).

    counts = import_reports(FilesystemStore("/data/bugs"), PostgresStore(connect), app="placements")

Keeps ids, audit rows, screenshots and votes. Idempotent and resumable: a
report already in the destination is not copied again, but audit rows or
votes it is missing (a run that died half-way) are filled in, so a second run
over a finished import copies nothing.
"""
from __future__ import annotations

import dataclasses

from .store import NotFound


def import_reports(src_store, dst_store, app: str) -> dict:
    """Returns ``{"copied": {status: n}, "skipped": {status: n}, "audit": n,
    "screenshots": n, "votes": n}``. ``audit``, ``screenshots`` and ``votes``
    count what this run wrote; a report that already had ``app`` keeps it, one
    without gets ``app``."""
    out = {"copied": {}, "skipped": {}, "audit": 0, "screenshots": 0, "votes": 0}
    for src in sorted(src_store.list_bugs(status=None, since_iso=""), key=lambda r: r.added_at):
        try:
            dst_store.get_bug(src.id)
            present = True
        except NotFound:
            present = False
        bucket = "skipped" if present else "copied"
        out[bucket][src.status] = out[bucket].get(src.status, 0) + 1
        if not present:
            try:
                png = src_store.get_screenshot(src.id)
            except NotFound:
                png = None
            rec = dataclasses.replace(src, screenshot=png is not None,
                                      app=src.app or app)   # a copy: never alias the source's object
            dst_store.put_bug(rec, png)
            out["screenshots"] += 1 if png is not None else 0
        have = len(dst_store.list_audit(src.id))
        for entry in src_store.list_audit(src.id)[have:]:
            dst_store.append_audit(src.id, entry)
            out["audit"] += 1
        if hasattr(src_store, "list_votes") and hasattr(dst_store, "add_vote"):
            for v in src_store.list_votes(src.id):
                if dst_store.add_vote(src.id, v["voter"], at=v.get("at")):
                    out["votes"] += 1
    return out
