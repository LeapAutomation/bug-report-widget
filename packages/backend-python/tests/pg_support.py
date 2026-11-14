"""A real PostgreSQL for the tests (practice: a store that talks to a database
is tested against one, never a fake).

``BUG_REPORT_PG_DSN`` names a server (CI's ``postgres:16`` service container).
Without it, a throwaway Homebrew ``postgresql@16`` is started on a free port
in a temp data dir. Without either the tests SKIP with the recipe, except
under ``CI=true``, where a missing Postgres must FAIL: a skipped suite is
green with nothing run.

Each test gets its own schema (``search_path``), created fresh and dropped
after, so tests never see each other's rows and need no TRUNCATE.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

RECIPE = ("set BUG_REPORT_PG_DSN=postgresql://user:pw@host:5432/db (a server you can create "
          "schemas on), or `brew install postgresql@16` (initdb/pg_ctl are found in "
          "/opt/homebrew/bin or /opt/homebrew/opt/postgresql@16/bin)")


def _bin(name: str):
    for d in ("/opt/homebrew/bin", "/opt/homebrew/opt/postgresql@16/bin",
              "/usr/local/opt/postgresql@16/bin", "/usr/lib/postgresql/16/bin"):
        p = Path(d) / name
        if p.exists():
            return str(p)
    return shutil.which(name)


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TempPostgres:
    """initdb + pg_ctl on a temp dir; stops and deletes it on exit."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="brpg-")
        self.port = _free_port()
        self.dsn = f"postgresql://postgres@127.0.0.1:{self.port}/postgres"

    def start(self) -> str:
        data = os.path.join(self.dir, "data")
        env = {**os.environ, "LC_ALL": "C"}   # macOS: "postmaster became multithreaded" without it
        subprocess.run([_bin("initdb"), "-D", data, "-U", "postgres", "--auth=trust",
                        "-E", "UTF8", "--locale=C"], check=True, capture_output=True, env=env)
        subprocess.run([_bin("pg_ctl"), "-D", data, "-w", "-l", os.path.join(self.dir, "log"),
                        "-o", f"-p {self.port} -c listen_addresses=127.0.0.1 -c unix_socket_directories= "
                              "-c fsync=off", "start"], check=True, capture_output=True, env=env)
        return self.dsn

    def stop(self):
        try:
            subprocess.run([_bin("pg_ctl"), "-D", os.path.join(self.dir, "data"), "-m", "immediate",
                            "stop"], capture_output=True, env={**os.environ, "LC_ALL": "C"})
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)


def server_dsn(tmp_holder: list) -> str | None:
    """The server's DSN, or None when there is no Postgres (caller skips/fails)."""
    dsn = os.environ.get("BUG_REPORT_PG_DSN")
    if dsn:
        return dsn
    if _bin("initdb") and _bin("pg_ctl"):
        pg = TempPostgres()
        tmp_holder.append(pg)   # stopped (and its dir removed) even when start fails
        return pg.start()
    return None


def missing_postgres():
    """Skip, or FAIL under CI, because there is no Postgres."""
    msg = f"no PostgreSQL available: {RECIPE}"
    if os.environ.get("CI", "").lower() in ("1", "true", "yes"):
        pytest.fail(msg + " (CI=true: a missing Postgres must fail, not skip)", pytrace=False)
    pytest.skip(msg)


class Schema:
    """One fresh schema on the server, and a connection factory pinned to it."""

    def __init__(self, dsn: str):
        import psycopg
        self.dsn = dsn
        self.name = f"t_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {self.name}")

    def connect(self):
        import psycopg
        return psycopg.connect(self.dsn, options=f"-c search_path={self.name}")

    def drop(self):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(f"DROP SCHEMA {self.name} CASCADE")
