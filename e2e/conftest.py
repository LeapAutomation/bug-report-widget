"""Cross-stack test fixtures.

Boots the Python and Node reference backends as separate processes (Python
via Werkzeug, Node via Express). Tests are parametrized over both URLs.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
PY_PKG = ROOT / "packages" / "backend-python"
NODE_PKG = ROOT / "packages" / "backend-node"

# The Postgres helper the backend-python tests use: one real server for the run.
sys.path.insert(0, str(PY_PKG / "tests"))
import pg_support  # noqa: E402


@pytest.fixture(scope="session")
def pg_dsn():
    """A real Postgres (BUG_REPORT_PG_DSN, or a throwaway local one); missing = skip, or FAIL under CI."""
    holder: list = []
    dsn = pg_support.server_dsn(holder)
    try:
        if dsn is None:
            pg_support.missing_postgres()
        yield dsn
    finally:
        for pg in holder:
            pg.stop()


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_alive(url: str, timeout: float = 10) -> None:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            r = requests.post(url, json={"details": "ping"}, timeout=1)
            if r.status_code in (201, 400, 413):
                return
        except requests.RequestException as e:
            last_err = e
        time.sleep(0.1)
    raise RuntimeError(f"server at {url} never came up: {last_err}")


# ---------------------------------------------------------------------------
# Python backend — Werkzeug subprocess
# ---------------------------------------------------------------------------

PY_SERVER_SCRIPT = """
import os, sys
sys.path.insert(0, r"{pkg}")
from flask import Flask
from bug_report import create_blueprint, FilesystemStore

{store_init}

app = Flask(__name__)
app.register_blueprint(create_blueprint(
    store=store,
    is_admin=lambda req: True,
    {hooks}
))
app.run(host="127.0.0.1", port={port}, use_reloader=False)
"""

FS_INIT = 'store = FilesystemStore(r"{state}")'
PG_INIT = """import psycopg
from bug_report.pg import PostgresStore
store = PostgresStore(lambda: psycopg.connect({dsn!r}, options="-c search_path={schema}"))
store.ensure_schema()"""
# v3 harness: who is calling and which app are test headers (a real service takes them from a verified token)
V3_HOOKS = """actor_identity=lambda r: {"email": r.headers.get("X-Test-User", ""), "name": ""},
    app_stamp=lambda r: r.headers.get("X-Test-App") or None,"""


def _boot_python(tmp_path_factory, label, store_init, hooks=""):
    port = _free_port()
    script = PY_SERVER_SCRIPT.format(pkg=str(PY_PKG), store_init=store_init, hooks=hooks, port=port)
    log = tmp_path_factory.mktemp(f"{label}-log") / "out.log"
    return port, script, log


@pytest.fixture(scope="session")
def python_server(tmp_path_factory):
    yield from _serve_python(tmp_path_factory, "py", FS_INIT.format(state=tmp_path_factory.mktemp("py-state")))


def _serve_python(tmp_path_factory, label, store_init, hooks=""):
    """Boot a Python backend subprocess; yield its /bugs URL; stop it."""
    port, script, log = _boot_python(tmp_path_factory, label, store_init, hooks)
    with open(log, "w") as logf:
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=logf, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}/bugs"
    try:
        try:
            _wait_alive(base)
        except RuntimeError:
            tail = log.read_text()[-2000:] if log.exists() else "(no log)"
            raise RuntimeError(f"python backend ({label}) failed to start. log tail:\n{tail}")
        yield base
    finally:
        proc.terminate()
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired: proc.kill()


@pytest.fixture(scope="session")
def pg_schema_name(pg_dsn):
    """Two fresh schemas' worth of names for the Postgres-backed servers (created now, dropped at the end)."""
    schemas = [pg_support.Schema(pg_dsn), pg_support.Schema(pg_dsn)]
    yield schemas
    for s in schemas:
        s.drop()


@pytest.fixture(scope="session")
def python_pg_server(tmp_path_factory, pg_dsn, pg_schema_name):
    yield from _serve_python(tmp_path_factory, "pypg", PG_INIT.format(dsn=pg_dsn, schema=pg_schema_name[0].name))


@pytest.fixture(scope="session")
def python_v3_server(tmp_path_factory):
    yield from _serve_python(tmp_path_factory, "pyv3", FS_INIT.format(state=tmp_path_factory.mktemp("py-v3-state")),
                             V3_HOOKS)


@pytest.fixture(scope="session")
def python_pg_v3_server(tmp_path_factory, pg_dsn, pg_schema_name):
    yield from _serve_python(tmp_path_factory, "pypgv3",
                             PG_INIT.format(dsn=pg_dsn, schema=pg_schema_name[1].name), V3_HOOKS)


# ---------------------------------------------------------------------------
# Python backend with a mailer — the notify path over real HTTP (2.2.0)
# ---------------------------------------------------------------------------

PY_MAILER_SCRIPT = """
import json, sys
sys.path.insert(0, r"{pkg}")
from flask import Flask, request
from bug_report import create_blueprint, FilesystemStore

def notifier(n):
    # A stand-in for AcsNotifier: records what it was asked to send, and fails
    # on cue so the failure path is driven over HTTP too.
    if "FAIL" in n.note:
        raise RuntimeError("mail server said no")
    with open(r"{outbox}", "a") as f:
        f.write(json.dumps(n.__dict__) + "\\n")
    return "msg-1"

def who(req):
    return {{"email": req.headers.get("X-Test-User", ""), "name": req.headers.get("X-Test-Name", "")}}

app = Flask(__name__)
app.register_blueprint(create_blueprint(
    store=FilesystemStore(r"{state}"), is_admin=lambda req: True,
    actor_identity=who, notifier=notifier, app_url="https://app.example/",
))
app.run(host="127.0.0.1", port={port}, use_reloader=False)
"""


@pytest.fixture(scope="session")
def python_mailer_server(tmp_path_factory):
    port = _free_port()
    state = tmp_path_factory.mktemp("py-mail-state")
    outbox = tmp_path_factory.mktemp("py-mail-out") / "outbox.jsonl"
    script = PY_MAILER_SCRIPT.format(pkg=str(PY_PKG), state=str(state), port=port, outbox=str(outbox))
    log = tmp_path_factory.mktemp("py-mail-log") / "out.log"
    with open(log, "w") as logf:
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=logf, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}/bugs"
    try:
        try:
            _wait_alive(base)
        except RuntimeError:
            tail = log.read_text()[-2000:] if log.exists() else "(no log)"
            raise RuntimeError(f"python mailer backend failed to start. log tail:\n{tail}")
        yield base, outbox
    finally:
        proc.terminate()
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired: proc.kill()


# ---------------------------------------------------------------------------
# Node backend — Express subprocess
# ---------------------------------------------------------------------------

NODE_SERVER_JS = """
import express from "express";
import {{ createBugsRouter, FilesystemStore }} from "./src/index.js";

const app = express();
app.use("/bugs", createBugsRouter({{
  store: new FilesystemStore({state!r}),
  isAdmin: () => true,
  {hooks}
}}));
app.listen({port}, "127.0.0.1", () => console.log("ready"));
"""
NODE_V3_HOOKS = """actorIdentity: (req) => req.get("x-test-user") || "",
  appStamp: (req) => req.get("x-test-app") || null,"""


def _serve_node(tmp_path_factory, label, hooks=""):
    """Boot the Node backend. Writes a launcher .mjs *inside* the package
    directory so Node's normal upward resolution finds `node_modules/express`.
    NODE_PATH doesn't help for ESM imports — it's CommonJS-only."""
    port = _free_port()
    state = tmp_path_factory.mktemp(f"{label}-state")
    # Tag the script with the PID so parallel runs don't collide.
    script_path = NODE_PKG / f".e2e-{label}-{os.getpid()}.mjs"
    script_path.write_text(NODE_SERVER_JS.format(state=str(state), port=port, hooks=hooks))
    log = tmp_path_factory.mktemp(f"{label}-log") / "out.log"
    with open(log, "w") as logf:
        proc = subprocess.Popen(
            ["node", script_path.name],
            cwd=str(NODE_PKG),
            stdout=logf, stderr=subprocess.STDOUT,
        )
    base = f"http://127.0.0.1:{port}/bugs"
    try:
        try:
            _wait_alive(base)
        except RuntimeError:
            tail = log.read_text()[-2000:] if log.exists() else "(no log)"
            raise RuntimeError(f"node backend failed to start. log:\n{tail}")
        yield base
    finally:
        proc.terminate()
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired: proc.kill()
        try: script_path.unlink()
        except FileNotFoundError: pass


@pytest.fixture(scope="session")
def node_server(tmp_path_factory):
    yield from _serve_node(tmp_path_factory, "node")


@pytest.fixture(scope="session")
def node_v3_server(tmp_path_factory):
    yield from _serve_node(tmp_path_factory, "nodev3", NODE_V3_HOOKS)


@pytest.fixture(params=["python", "python-postgres", "node"])
def backend_url(request):
    """Each test runs once per backend: Python on files, Python on Postgres, Node."""
    return request.getfixturevalue({"python": "python_server", "python-postgres": "python_pg_server",
                                    "node": "node_server"}[request.param])


@pytest.fixture(params=["python", "python-postgres", "node"])
def v3_url(request):
    """The v3 scenarios: the same three backends, each with a caller (X-Test-User) and an app stamp (X-Test-App)."""
    return request.getfixturevalue({"python": "python_v3_server", "python-postgres": "python_pg_v3_server",
                                    "node": "node_v3_server"}[request.param])
