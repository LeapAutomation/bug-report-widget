import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # pg_support, under any import mode
import pg_support


@pytest.fixture(scope="session")
def pg_dsn():
    holder: list = []
    dsn = pg_support.server_dsn(holder)
    try:
        if dsn is None:
            pg_support.missing_postgres()
        yield dsn
    finally:
        for pg in holder:
            pg.stop()


@pytest.fixture
def pg_schema(pg_dsn):
    """A fresh, empty schema (no tables yet) on the real server."""
    s = pg_support.Schema(pg_dsn)
    try:
        yield s
    finally:
        s.drop()


@pytest.fixture
def pg_store(pg_schema):
    from bug_report.pg import PostgresStore
    st = PostgresStore(pg_schema.connect)
    st.ensure_schema()
    return st


@pytest.fixture(params=["memory", "filesystem", "postgres"])
def any_store(request, tmp_path):
    """Every Store, for the scenarios all of them must agree on."""
    from bug_report import FilesystemStore, InMemoryStore
    if request.param == "memory":
        return InMemoryStore()
    if request.param == "postgres":
        return request.getfixturevalue("pg_store")
    return FilesystemStore(tmp_path / "bugs")
