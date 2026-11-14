"""``PostgresStore``: the ``Store`` protocol over PostgreSQL (psycopg 3).

An optional extra: ``pip install "bug-report-py[postgres]"``. Ported from the
fleet dashboard's ``bug_store_pg.py`` (in production since 2026-09-09), with
the central store's tables:

* ``reports(id pk, app, kind, status, added_at timestamptz, area, priority,
  owner, parent_id, duplicate_of, record jsonb, screenshot bytea)``
* ``report_audit(id, seq, entry jsonb)``
* ``report_votes(id, voter, at)``

``record`` is exactly what ``FilesystemStore`` writes to its JSON file
(``dataclasses.asdict(BugRecord)``), so a report reads back equal, and the
columns are copies lifted out of it only where a list filters or sorts on
them. The record stays the source: ``kind`` is the EFFECTIVE kind (stored,
else derived from the tags) so the filter agrees with the other stores.
jsonb does not keep key order or whitespace; "byte-equal" means the same JSON.

Connections: the constructor takes ``connect``, a callable returning a psycopg
connection (or a pool's ``connection()`` context). One is taken per operation
and used as a context manager (commit, then close/return), so a service whose
password is a short-lived Entra token just passes a factory that fetches a
fresh one; this module has no token code.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Callable

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError as e:  # pragma: no cover
    raise ImportError(
        'PostgresStore needs psycopg 3: pip install "bug-report-py[postgres]"') from e

from .store import (AuditEntry, BugRecord, NotFound, StoreError, apply_changes,
                    normal_voter, vote_time)

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id           text PRIMARY KEY,
    app          text,
    kind         text,
    status       text NOT NULL,
    added_at     timestamptz,
    area         text,
    priority     text,
    owner        text,
    parent_id    text,
    duplicate_of text,
    record       jsonb NOT NULL,
    screenshot   bytea
);
CREATE INDEX IF NOT EXISTS reports_status_added ON reports (status, added_at);
CREATE INDEX IF NOT EXISTS reports_app ON reports (app);
CREATE INDEX IF NOT EXISTS reports_parent ON reports (parent_id);
CREATE TABLE IF NOT EXISTS report_audit (
    id    text NOT NULL,
    seq   integer NOT NULL,
    entry jsonb NOT NULL,
    PRIMARY KEY (id, seq)
);
CREATE TABLE IF NOT EXISTS report_votes (
    id    text NOT NULL,
    voter text NOT NULL,
    at    text NOT NULL,
    PRIMARY KEY (id, voter)
);
"""


def _instant(s: str):
    """The record's added_at as an instant for the column (the record keeps the
    text it was given)."""
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def _audit_entry(d: dict) -> AuditEntry:
    return AuditEntry(changed_at=d.get("changedAt", ""), to_status=d.get("toStatus", ""),
                      from_status=d.get("fromStatus"), changed_by=d.get("changedBy", ""),
                      changed_by_name=d.get("changedByName", ""), note=d.get("note", ""))


class PostgresStore:
    def __init__(self, connect: Callable):
        self._connect = connect

    def ensure_schema(self) -> None:
        """Create the tables and indexes if they are not there. Idempotent."""
        self._run(lambda cur: cur.execute(SCHEMA))

    # -- plumbing ------------------------------------------------------------
    def _run(self, fn):
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    return fn(cur)
        except psycopg.Error as e:
            raise StoreError(f"database error: {e}") from e

    # -- the Store protocol --------------------------------------------------
    def put_bug(self, bug: BugRecord, screenshot_png: bytes | None) -> None:
        if screenshot_png is not None:
            bug.screenshot = True
        rec = dataclasses.asdict(bug)
        cols = (bug.id, bug.app, bug.effective_kind(), bug.status, _instant(bug.added_at),
                bug.area, bug.priority, bug.owner, bug.parent_id, bug.duplicate_of, Jsonb(rec))
        upsert = (
            "INSERT INTO reports(id, app, kind, status, added_at, area, priority, owner, "
            "parent_id, duplicate_of, record{shot_col}) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s{shot_val}) "
            "ON CONFLICT (id) DO UPDATE SET app=EXCLUDED.app, kind=EXCLUDED.kind, "
            "status=EXCLUDED.status, added_at=EXCLUDED.added_at, area=EXCLUDED.area, "
            "priority=EXCLUDED.priority, owner=EXCLUDED.owner, parent_id=EXCLUDED.parent_id, "
            "duplicate_of=EXCLUDED.duplicate_of, record=EXCLUDED.record{shot_set}")
        if screenshot_png is not None:
            sql = upsert.format(shot_col=", screenshot", shot_val=",%s",
                                shot_set=", screenshot=EXCLUDED.screenshot")
            args = cols + (screenshot_png,)
        else:
            # a status change or a re-save keeps the screenshot it has
            sql = upsert.format(shot_col="", shot_val="", shot_set="")
            args = cols
        self._run(lambda cur: cur.execute(sql, args))

    def get_bug(self, bug_id: str) -> BugRecord:
        def go(cur):
            cur.execute("SELECT record FROM reports WHERE id=%s", (bug_id,))
            return cur.fetchone()
        row = self._run(go)
        if row is None:
            raise NotFound(bug_id)
        return BugRecord.from_dict(row[0])

    def list_bugs(self, *, status, since_iso, app=None, kind=None, area=None, priority=None):
        # `since_iso` is an instant: compared on the `added_at` column (indexed),
        # not as text. A report whose added_at will not parse has a NULL there
        # and is not listed once a window is given.
        where, args = [], []
        since = _instant(since_iso)
        if since is not None:
            where.append("added_at >= %s")
            args.append(since)
        for col, val in (("status", status), ("app", app), ("kind", kind),
                         ("area", area), ("priority", priority)):
            if val:
                where.append(f"{col} = %s")
                args.append(val)

        def go(cur):
            cur.execute("SELECT record FROM reports" + (" WHERE " + " AND ".join(where) if where else "")
                        + " ORDER BY added_at, id", args)
            return cur.fetchall()
        for (rec,) in self._run(go):
            yield BugRecord.from_dict(rec)

    def update_status(self, bug_id: str, new_status: str) -> BugRecord:
        return self.update_bug(bug_id, status=new_status)

    def update_fields(self, bug_id: str, changes: dict) -> BugRecord:
        return self.update_bug(bug_id, changes=changes)

    def update_bug(self, bug_id: str, *, status: str | None = None,
                   changes: dict | None = None) -> BugRecord:
        """One transaction: lock the row (``SELECT ... FOR UPDATE``), change the
        record, write it back with one UPDATE. Two PATCHes of different fields
        of one report queue behind each other instead of the second putting
        back the first's old values."""
        def go(cur):
            cur.execute("SELECT record FROM reports WHERE id=%s FOR UPDATE", (bug_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound(bug_id)
            rec = BugRecord.from_dict(row[0])
            apply_changes(rec, changes or {}, status)
            cur.execute(
                "UPDATE reports SET app=%s, kind=%s, status=%s, added_at=%s, area=%s, priority=%s, "
                "owner=%s, parent_id=%s, duplicate_of=%s, record=%s WHERE id=%s",
                (rec.app, rec.effective_kind(), rec.status, _instant(rec.added_at), rec.area,
                 rec.priority, rec.owner, rec.parent_id, rec.duplicate_of,
                 Jsonb(dataclasses.asdict(rec)), bug_id))
            return rec
        return self._run(go)

    def append_audit(self, bug_id: str, entry: AuditEntry) -> None:
        def go(cur):
            # one writer per report at a time, so `seq` never collides
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (bug_id,))
            cur.execute("INSERT INTO report_audit(id, seq, entry) "
                        "SELECT %s, COALESCE(MAX(seq), 0) + 1, %s FROM report_audit WHERE id=%s",
                        (bug_id, Jsonb(entry.to_dict()), bug_id))
        self._run(go)

    def list_audit(self, bug_id: str) -> list:
        def go(cur):
            cur.execute("SELECT entry FROM report_audit WHERE id=%s ORDER BY seq", (bug_id,))
            return cur.fetchall()
        return [_audit_entry(d) for (d,) in self._run(go)]

    def get_screenshot(self, bug_id: str) -> bytes:
        def go(cur):
            cur.execute("SELECT screenshot FROM reports WHERE id=%s", (bug_id,))
            return cur.fetchone()
        row = self._run(go)
        if row is None or row[0] is None:
            raise NotFound(bug_id)
        return bytes(row[0])

    # -- votes ---------------------------------------------------------------
    def _exists(self, cur, bug_id):
        cur.execute("SELECT 1 FROM reports WHERE id=%s", (bug_id,))
        if cur.fetchone() is None:
            raise NotFound(bug_id)

    def add_vote(self, bug_id: str, voter: str, at: str | None = None) -> bool:
        def go(cur):
            self._exists(cur, bug_id)
            cur.execute("INSERT INTO report_votes(id, voter, at) VALUES(%s,%s,%s) "
                        "ON CONFLICT DO NOTHING", (bug_id, normal_voter(voter), at or vote_time()))
            return cur.rowcount == 1
        return self._run(go)

    def remove_vote(self, bug_id: str, voter: str) -> bool:
        def go(cur):
            self._exists(cur, bug_id)
            cur.execute("DELETE FROM report_votes WHERE id=%s AND voter=%s",
                        (bug_id, normal_voter(voter)))
            return cur.rowcount == 1
        return self._run(go)

    def vote_counts(self, bug_ids: list[str]) -> dict[str, int]:
        if not bug_ids:
            return {}

        def go(cur):
            cur.execute("SELECT id, count(*) FROM report_votes WHERE id = ANY(%s) GROUP BY id",
                        (list(bug_ids),))
            return dict(cur.fetchall())
        got = self._run(go)
        return {i: int(got.get(i, 0)) for i in bug_ids}

    def list_votes(self, bug_id: str) -> list[dict]:
        def go(cur):
            cur.execute("SELECT voter, at FROM report_votes WHERE id=%s ORDER BY at, voter", (bug_id,))
            return cur.fetchall()
        return [{"voter": v, "at": at} for v, at in self._run(go)]
