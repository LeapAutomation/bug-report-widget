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
    other = c.post("/api/bugs", json={"details": "other"}).get_json()["id"]
    triaged = c.patch(f"/api/bugs/{bid}", json={"area": "Belt", "priority": "now", "owner": "a@example.com",
                                                "parentId": other}).get_json()
    voted = c.post(f"/api/bugs/{bid}/vote").get_json()
    dup = c.patch(f"/api/bugs/{other}", json={"duplicateOf": bid}).get_json()
    created = c.post("/api/bugs", json={"details": "again"}).get_json()
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
    # v3: the triage fields, the duplicate decline and the new response shapes
    for resp in (triaged, dup):
        assert set(resp) - bug_spec == set()
        for row in resp["audit"]:
            assert set(row) - audit_spec == set()
    assert {"area", "priority", "owner", "parentId", "duplicateOf", "votes", "app", "product", "kind"} <= set(summary)
    assert set(voted) == _schema_properties("Voted")
    assert set(created) <= _schema_properties_of_created()


def _schema_properties_of_created() -> set[str]:
    """The 201 body's properties, read from the createBug response (an inline schema)."""
    text = SPEC.read_text()
    block = text[text.index("operationId: createBug"):text.index('"400":')]
    after = block[block.index('"201":'):]
    return set(re.findall(r"^ {18}(\w+):\s*$", after, re.M))


def test_every_contract_summary_field_is_a_bugsummary_property_in_the_spec():
    import json
    contract_json = json.loads((SPEC.parent / "report-contract.json").read_text())
    assert set(contract_json["summaryFields"]) - _schema_properties("BugSummary") == set()


def test_the_vote_route_the_list_filters_and_the_patch_fields_are_in_the_spec():
    text = SPEC.read_text()
    assert "/bugs/{id}/vote:" in text and "operationId: voteBug" in text and "operationId: unvoteBug" in text
    for filt in ("app", "kind", "area", "priority"):
        assert f"name: {filt}\n" in text
    patch = text[text.index("operationId: patchBug"):text.index("/bugs/{id}/vote:")]
    for field in ("area:", "priority:", "owner:", "parentId:", "duplicateOf:"):
        assert field in patch
    assert "required: [status]" not in patch


def test_types_py_and_types_ts_name_every_v3_summary_field():
    import json
    fields = json.loads((SPEC.parent / "report-contract.json").read_text())["summaryFields"]
    # inside BugSummary only: BugPatch and BugCreate name some of the same fields
    py = (SPEC.parent / "types.py").read_text()
    py = py[py.index("class BugSummary"):py.index("class AuditEntry")]
    ts = (SPEC.parent / "types.ts").read_text()
    ts = ts[ts.index("export interface BugSummary"):ts.index("export interface AuditEntry")]
    for f in fields:
        assert re.search(rf"^\s+{f}\??:", ts, re.M), f"types.ts BugSummary lacks {f}"
        assert re.search(rf"^\s+{f}:", py, re.M), f"types.py BugSummary lacks {f}"
