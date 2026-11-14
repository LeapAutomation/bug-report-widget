"""Copy one store's reports into another (the migration into the central store).

    counts = import_reports(FilesystemStore("/data/bugs"), PostgresStore(connect), app="placements")

**One run, at cutover, with the app's writes frozen** (Ken, 2026-11-14). It
keeps ids, audit rows, screenshots and votes. A second run over a finished
import copies nothing: each report already in the destination is compared with
the source (status, triage fields, votes, audit length and last audit row),
and if anything differs the run raises ``ImportMismatch`` listing the ids and
changes nothing. That is how a source edited after the first run is noticed
instead of silently half-merged. There is no resume: a run that died half-way
leaves reports the second run reports as differing.
"""
from __future__ import annotations

import dataclasses

from . import contract
from .store import NotFound

_COMPARED = ("status",) + tuple(contract.TRIAGE_WIRE_TO_ATTR.values())


class ImportMismatch(Exception):
    """A report already in the destination differs from the source. ``ids`` lists them."""

    def __init__(self, ids: list[str]):
        self.ids = ids
        super().__init__(f"{len(ids)} report(s) differ between source and destination: {', '.join(ids)}")


def _fingerprint(store, rec) -> tuple:
    audit = store.list_audit(rec.id)
    votes = sorted(v["voter"] for v in store.list_votes(rec.id)) if hasattr(store, "list_votes") else []
    return (tuple(getattr(rec, a) for a in _COMPARED), votes, len(audit),
            audit[-1].to_dict() if audit else None)


def import_reports(src_store, dst_store, app: str) -> dict:
    """Returns ``{"copied": {status: n}, "skipped": {status: n}, "audit": n,
    "screenshots": n, "votes": n}``; the last three count what this run wrote.
    A report that has no ``app`` gets ``app``, which must be one of
    ``contract.APP_PRODUCTS`` (ValueError otherwise, before anything is read or written)."""
    if app not in contract.APP_PRODUCTS:
        raise ValueError(f"app must be one of {sorted(contract.APP_PRODUCTS)}, not {app!r}")
    sources = sorted(src_store.list_bugs(status=None, since_iso=""), key=lambda r: r.added_at)

    # Pass 1: read-only. Which are already there, and do those still match?
    present, differing = {}, []
    for src in sources:
        try:
            dst = dst_store.get_bug(src.id)
        except NotFound:
            continue
        present[src.id] = True
        if _fingerprint(src_store, src) != _fingerprint(dst_store, dst):
            differing.append(src.id)
    if differing:
        raise ImportMismatch(differing)

    # Pass 2: copy what is not there.
    out = {"copied": {}, "skipped": {}, "audit": 0, "screenshots": 0, "votes": 0}
    for src in sources:
        if src.id in present:
            out["skipped"][src.status] = out["skipped"].get(src.status, 0) + 1
            continue
        out["copied"][src.status] = out["copied"].get(src.status, 0) + 1
        try:
            png = src_store.get_screenshot(src.id)
        except NotFound:
            png = None
        # a copy: never alias the source's object
        dst_store.put_bug(dataclasses.replace(src, screenshot=png is not None, app=src.app or app), png)
        out["screenshots"] += 1 if png is not None else 0
        for entry in src_store.list_audit(src.id):
            dst_store.append_audit(src.id, entry)
            out["audit"] += 1
        if hasattr(src_store, "list_votes") and hasattr(dst_store, "add_vote"):
            for v in src_store.list_votes(src.id):
                if dst_store.add_vote(src.id, v["voter"], at=v.get("at")):
                    out["votes"] += 1
    return out
