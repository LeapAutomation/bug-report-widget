"""Closing a report emails the person who filed it — over real HTTP (2.2.0).

Python only: the Node backend has no notifier (it answers ``canNotify: false``
and ignores ``notify``; test_contract.py pins that on both stacks). This drives
the ``notifier=`` wiring through a running server with a stand-in mailer, so a
break between the blueprint and the notifier fails here, not in production.
"""
from __future__ import annotations

import json

import requests

CHRIS = {"X-Test-User": "chris.hamblin@helloleap.ai", "X-Test-Name": "Chris Hamblin"}
KEN = {"X-Test-User": "ken.moynihan@helloleap.ai", "X-Test-Name": "Ken Moynihan"}


def _file_and_triage(base: str, title: str) -> str:
    r = requests.post(base, json={"title": title, "details": "d", "metaUrl": "https://app.example/day"},
                      headers=CHRIS, timeout=5)
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    assert requests.patch(f"{base}/{bid}", json={"status": "triaged", "note": ""}, headers=KEN,
                          timeout=5).status_code == 200
    return bid


def _sent(outbox) -> list[dict]:
    return [json.loads(line) for line in outbox.read_text().splitlines()] if outbox.exists() else []


def test_resolving_with_notify_emails_the_filer_and_audits_it(python_mailer_server):
    base, outbox = python_mailer_server
    bid = _file_and_triage(base, "Time of issue not set")
    assert requests.get(f"{base}/{bid}", headers=KEN, timeout=5).json()["canNotify"] is True
    before = len(_sent(outbox))

    r = requests.patch(f"{base}/{bid}", json={"status": "resolved", "note": "Fixed — it asks first now.",
                                              "notify": True}, headers=KEN, timeout=5)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "resolved"
    assert body["notified"] == {"ok": True, "to": "chris.hamblin@helloleap.ai"}

    sent = _sent(outbox)[before:]
    assert len(sent) == 1
    n = sent[0]
    assert (n["to_email"], n["to_name"], n["closer_email"], n["closer_name"]) == (
        "chris.hamblin@helloleap.ai", "Chris Hamblin", "ken.moynihan@helloleap.ai", "Ken Moynihan")
    assert (n["status"], n["note"], n["page_url"], n["app_url"]) == (
        "resolved", "Fixed — it asks first now.", "https://app.example/day", "https://app.example/")

    change, outcome = body["audit"][-2:]
    assert (change["fromStatus"], change["toStatus"], change["changedByName"]) == ("triaged", "resolved", "Ken Moynihan")
    assert outcome["note"] == "emailed Chris Hamblin <chris.hamblin@helloleap.ai>"


def test_a_failed_send_still_resolves_and_says_so(python_mailer_server):
    base, outbox = python_mailer_server
    bid = _file_and_triage(base, "Send fails")
    before = len(_sent(outbox))

    r = requests.patch(f"{base}/{bid}", json={"status": "resolved", "note": "FAIL on purpose", "notify": True},
                       headers=KEN, timeout=5)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "resolved"
    assert body["notified"]["ok"] is False and "mail server said no" in body["notified"]["error"]
    assert body["audit"][-1]["note"].startswith("email to Chris Hamblin <chris.hamblin@helloleap.ai> failed:")
    assert len(_sent(outbox)) == before
    # the change stuck: a fresh read agrees
    assert requests.get(f"{base}/{bid}", headers=KEN, timeout=5).json()["status"] == "resolved"


def test_without_notify_nothing_is_sent(python_mailer_server):
    base, outbox = python_mailer_server
    bid = _file_and_triage(base, "Quiet close")
    before = len(_sent(outbox))
    r = requests.patch(f"{base}/{bid}", json={"status": "resolved", "note": "done"}, headers=KEN, timeout=5)
    assert r.status_code == 200 and "notified" not in r.json()
    assert len(_sent(outbox)) == before
