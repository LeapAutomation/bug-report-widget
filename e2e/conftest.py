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

app = Flask(__name__)
app.register_blueprint(create_blueprint(
    store=FilesystemStore(r"{state}"),
    is_admin=lambda req: True,
))
app.run(host="127.0.0.1", port={port}, use_reloader=False)
"""


@pytest.fixture(scope="session")
def python_server(tmp_path_factory):
    port = _free_port()
    state = tmp_path_factory.mktemp("py-state")
    script = PY_SERVER_SCRIPT.format(pkg=str(PY_PKG), state=str(state), port=port)
    log = tmp_path_factory.mktemp("py-log") / "out.log"
    with open(log, "w") as logf:
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=logf, stderr=subprocess.STDOUT,
        )
    base = f"http://127.0.0.1:{port}/bugs"
    try:
        try:
            _wait_alive(base)
        except RuntimeError:
            tail = log.read_text()[-2000:] if log.exists() else "(no log)"
            raise RuntimeError(f"python backend failed to start. log tail:\n{tail}")
        yield base
    finally:
        proc.terminate()
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired: proc.kill()


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
}}));
app.listen({port}, "127.0.0.1", () => console.log("ready"));
"""


@pytest.fixture(scope="session")
def node_server(tmp_path_factory):
    """Boot the Node backend. Writes a launcher .mjs *inside* the package
    directory so Node's normal upward resolution finds `node_modules/express`.
    NODE_PATH doesn't help for ESM imports — it's CommonJS-only."""
    port = _free_port()
    state = tmp_path_factory.mktemp("node-state")
    # Tag the script with the PID so parallel runs don't collide.
    script_path = NODE_PKG / f".e2e-server-{os.getpid()}.mjs"
    script_path.write_text(NODE_SERVER_JS.format(state=str(state), port=port))
    log = tmp_path_factory.mktemp("node-log") / "out.log"
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


@pytest.fixture(params=["python", "node"])
def backend_url(request, python_server, node_server):
    """Each test runs once per backend."""
    return python_server if request.param == "python" else node_server
