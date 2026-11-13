"""Every field the Python backend sends is in openapi.yaml (practice T5).

The spec is the documentation the other stacks and the UI are built from; a
field the server sends but the spec never names is a field nobody else knows
about. Added with backends 2.2.0, whose new fields (actorName, canNotify,
notified, changedByName) were documented by hand.
"""
from __future__ import annotations

import pathlib
import re

from flask import Flask

from bug_report import FilesystemStore, create_blueprint

SPEC = pathlib.Path(__file__).resolve().parents[2] / "spec" / "openapi.yaml"


def _schema_properties(name: str) -> set[str]:
    """Property names of one components/schemas entry, including those of an
    allOf branch, read by indentation (no YAML dependency in this package)."""
    lines = SPEC.read_text().splitlines()
    start = lines.index(f"    {name}:")
    props: set[str] = set()
    stack: list[int] = []  # key indents of the open `properties:` blocks; only the outermost counts
    for line in lines[start + 1:]:
        if re.match(r"^ {0,4}\S", line):
            break  # the next schema
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        while stack and indent < stack[-1]:
            stack.pop()
        k = re.match(r"^\s+([A-Za-z_]\w*):", line)
        if k and len(stack) == 1 and indent == stack[0]:
            props.add(k.group(1))
        if re.match(r"^\s+properties:\s*$", line):
            stack.append(indent + 2)
    return props


def _client(tmp_path):
    def notifier(n):
        return "id-1"

    app = Flask(__name__)
    app.register_blueprint(create_blueprint(
        store=FilesystemStore(tmp_path), is_admin=lambda r: True, notifier=notifier,
        actor_identity=lambda r: {"email": "chris.hamblin@helloleap.ai", "name": "Chris Hamblin"},
    ), url_prefix="/api")
    return app.test_client()


def test_every_field_the_server_sends_is_in_the_spec(tmp_path):
    c = _client(tmp_path)
    bid = c.post("/api/bugs", json={"title": "t", "details": "d", "metaUrl": "https://x/"}).get_json()["id"]
    c.patch(f"/api/bugs/{bid}", json={"status": "triaged", "note": ""})
    closed = c.patch(f"/api/bugs/{bid}", json={"status": "resolved", "note": "done", "notify": True}).get_json()
    detail = c.get(f"/api/bugs/{bid}").get_json()
    summary = c.get("/api/bugs?days=1").get_json()["bugs"][0]

    summary_spec = _schema_properties("BugSummary")
    bug_spec = summary_spec | _schema_properties("Bug")
    audit_spec = _schema_properties("AuditEntry")
    assert {"actorName", "changedByName"} & (summary_spec | audit_spec) == {"actorName", "changedByName"}

    assert set(summary) - summary_spec == set()
    assert set(detail) - bug_spec == set()
    assert set(closed) - bug_spec == set()
    for row in closed["audit"]:
        assert set(row) - audit_spec == set()
    assert set(closed["notified"]) <= {"ok", "to", "error"}
