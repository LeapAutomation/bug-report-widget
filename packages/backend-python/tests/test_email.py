"""bug_report.email: render() and AcsNotifier, with the Azure SDK stubbed. No network."""

from __future__ import annotations

import sys
import types

import pytest

from bug_report.email import AcsNotifier, Notice, render
from bug_report.models import first_name, person_name


def notice(**kw):
    base = dict(to_email="ana.reporter@example.com", to_name="Ana Reporter", title="Jam light stays on",
                note="Fixed in build 41.", status="resolved", bug_id="bug-20261112-124952-abcdef",
                filed_at="2026-11-12T12:49:52.000000Z", page_url="https://sv.example/day?x=1&y=2",
                app_url="https://app.example/", closer_email="sam.closer@example.com",
                closer_name="Sam Closer")
    base.update(kw)
    return Notice(**base)


# ----- names -----------------------------------------------------------------

@pytest.mark.parametrize("name,email,full,first", [
    ("Ana Reporter", "x@y", "Ana Reporter", "Ana"),
    ("", "ana.reporter@x.com", "Ana Reporter", "Ana"),
    ("", "sam@x.com", "Sam", "Sam"),
    (None, "a_b-c.d@x.com", "A B C D", "A"),
    ("", "", "", ""),
])
def test_name_fallback_rule(name, email, full, first):
    assert person_name(name, email) == full
    assert first_name(name, email) == first


# ----- render ----------------------------------------------------------------

def test_subject_per_status():
    assert render(notice())[0] == "Fixed: Jam light stays on"
    assert render(notice(status="declined"))[0] == \
        "Your bug report: Jam light stays on — not taken forward"


def test_other_statuses_have_no_email():
    with pytest.raises(ValueError):
        render(notice(status="triaged"))


def test_body_has_first_name_note_signature_date_link_and_id():
    _, html = render(notice())
    assert "Hi Ana," in html
    assert "Fixed in build 41." in html
    assert "— Sam Closer" in html
    assert "12 November 2026" in html
    assert 'href="https://sv.example/day?x=1&amp;y=2"' in html   # the page they filed from
    assert "bug-20261112-124952-abcdef" in html


def test_link_falls_back_to_the_app_when_there_is_no_page_url():
    _, html = render(notice(page_url=""))
    assert 'href="https://app.example/"' in html


def test_no_link_when_neither_is_http():
    _, html = render(notice(page_url="javascript:alert(1)", app_url=""))
    assert "href" not in html and "javascript" not in html


def test_everything_from_the_report_is_escaped():
    evil = "<script>alert(1)</script>"
    sub, html = render(notice(to_name=evil + " Q", title=evil, note=evil, closer_name=evil,
                              bug_id=evil, page_url='https://x/"><img src=x>'))
    assert "<script>" not in html and "<img" not in html
    assert "&lt;script&gt;" in html
    assert "<script>" in sub  # a subject is plain text, not HTML


def test_urls_in_the_note_become_links_and_newlines_stay():
    _, html = render(notice(note='See https://git.example/pr/7, then\nreload "now"'))
    assert '<a href="https://git.example/pr/7">https://git.example/pr/7</a>,' in html
    assert "then<br>\nreload &quot;now&quot;" in html


def test_a_note_url_cannot_break_out_of_its_href():
    _, html = render(notice(note="https://a.example/x\"onmouseover=\"y"))
    assert 'onmouseover="' not in html


def test_title_newlines_do_not_reach_the_subject():
    assert "\n" not in render(notice(title="a\r\nBcc: evil@example.com"))[0]


def test_first_name_from_the_address_when_the_name_is_missing():
    _, html = render(notice(to_name=""))
    assert "Hi Ana," in html


def test_signed_by_the_derived_closer_name_or_the_service():
    assert "— Sam Closer" in render(notice(closer_name=""))[1]
    assert "— Leap bug reports" in render(notice(closer_name="", closer_email=""))[1]


# ----- AcsNotifier --------------------------------------------------------------

class FakePoller:
    def __init__(self, result):
        self.result_value, self.timeout = result, "unset"

    def result(self, timeout=None):
        self.timeout = timeout
        return self.result_value


class FakeClient:
    def __init__(self, result=None):
        self.result = result if result is not None else {"id": "op-1", "status": "Succeeded"}
        self.messages, self.poller = [], None

    def begin_send(self, message):
        self.messages.append(message)
        self.poller = FakePoller(self.result)
        return self.poller


def make(client=None, **kw):
    base = dict(sender="bugs@example.com", domains="example.com", client=client or FakeClient())
    base.update(kw)
    return AcsNotifier(**base)


def test_send_builds_the_message_and_returns_the_id():
    c = FakeClient()
    assert make(c)(notice()) == "op-1"
    m = c.messages[0]
    assert m["senderAddress"] == "bugs@example.com"
    assert m["recipients"]["to"] == [{"address": "ana.reporter@example.com", "displayName": "Ana Reporter"}]
    assert m["replyTo"] == [{"address": "sam.closer@example.com", "displayName": "Sam Closer"}]
    assert m["content"]["subject"] == "Fixed: Jam light stays on" and "Hi Ana," in m["content"]["html"]
    assert c.poller.timeout is not None  # the wait is bounded


def test_no_reply_to_without_a_closer_address():
    c = FakeClient()
    make(c)(notice(closer_email="", closer_name=""))
    assert "replyTo" not in c.messages[0]


@pytest.mark.parametrize("to", ["ana@evil.test", "ana@example.com.evil.test", "nobody", ""])
def test_an_address_outside_the_domains_is_refused_before_any_send(to):
    c = FakeClient()
    with pytest.raises(RuntimeError):
        make(c)(notice(to_email=to))
    assert c.messages == []


def test_domain_match_ignores_case_and_a_leading_at():
    c = FakeClient()
    make(c, domains=" @Example.COM , other.com")(notice(to_email="Ana@EXAMPLE.com"))
    assert len(c.messages) == 1


def test_no_domains_refuses_everything_fail_closed():
    c = FakeClient()
    with pytest.raises(RuntimeError, match="BUG_NOTIFY_DOMAINS"):
        make(c, domains="")(notice())
    assert c.messages == []


def test_no_sender_refuses_fail_closed():
    c = FakeClient()
    with pytest.raises(RuntimeError, match="BUG_NOTIFY_SENDER"):
        make(c, sender="")(notice())
    assert c.messages == []


def test_a_send_that_does_not_succeed_raises():
    with pytest.raises(RuntimeError, match="Failed"):
        make(FakeClient({"id": "x", "status": "Failed"}))(notice())


def test_the_sdk_error_propagates_so_the_blueprint_can_audit_it():
    class Boom(FakeClient):
        def begin_send(self, message):
            raise OSError("429 slow down")
    with pytest.raises(OSError, match="429"):
        make(Boom())(notice())


# --- from_env and the real client's construction, with the Azure modules stubbed

GOOD = {"ACS_ENDPOINT": "https://comms.example", "BUG_NOTIFY_SENDER": "bugs@example.com",
        "BUG_NOTIFY_DOMAINS": "example.com"}


@pytest.mark.parametrize("drop", ["BUG_NOTIFY_SENDER", "BUG_NOTIFY_DOMAINS", "ACS_ENDPOINT"])
def test_from_env_is_none_when_not_fully_configured(drop):
    env = {k: v for k, v in GOOD.items() if k != drop}
    assert AcsNotifier.from_env(env) is None


def test_from_env_is_none_with_nothing_set():
    assert AcsNotifier.from_env({}) is None


def test_from_env_reads_the_config():
    n = AcsNotifier.from_env({**GOOD, "BUG_NOTIFY_DOMAINS": "a.com, b.com", "AZURE_CLIENT_ID": "cid"})
    assert (n.sender, n.domains, n.endpoint, n.client_id) == (
        "bugs@example.com", ("a.com", "b.com"), "https://comms.example", "cid")


@pytest.fixture
def azure(monkeypatch):
    calls = {}

    class EmailClient:
        def __init__(self, endpoint, credential, **kw):
            calls["endpoint"] = (endpoint, credential, kw)

        @classmethod
        def from_connection_string(cls, conn, **kw):
            calls["conn"] = (conn, kw)
            return types.SimpleNamespace(begin_send=lambda m: FakePoller({"id": "s", "status": "Succeeded"}))

        def begin_send(self, m):
            calls["sent"] = m
            return FakePoller({"id": "e", "status": "Succeeded"})

    def dac(**kw):
        calls["cred"] = kw
        return "CRED"

    mods = {"azure": types.ModuleType("azure"),
            "azure.communication": types.ModuleType("azure.communication"),
            "azure.communication.email": types.SimpleNamespace(EmailClient=EmailClient),
            "azure.identity": types.SimpleNamespace(DefaultAzureCredential=dac)}
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)
    return calls


def test_endpoint_uses_managed_identity_with_retries_off(azure):
    n = AcsNotifier.from_env({**GOOD, "AZURE_CLIENT_ID": "cid"})
    assert n(notice()) == "e"
    assert azure["cred"] == {"managed_identity_client_id": "cid"}
    assert azure["endpoint"] == ("https://comms.example", "CRED", {"retry_total": 0})


def test_endpoint_without_a_client_id_uses_the_default_credential(azure):
    AcsNotifier.from_env(GOOD)(notice())
    assert azure["cred"] == {}


def test_connection_string_is_the_fallback_with_retries_off(azure):
    env = {"ACS_CONNECTION_STRING": "endpoint=x;accesskey=y", "BUG_NOTIFY_SENDER": "bugs@example.com",
           "BUG_NOTIFY_DOMAINS": "example.com"}
    assert AcsNotifier.from_env(env)(notice()) == "s"
    assert azure["conn"] == ("endpoint=x;accesskey=y", {"retry_total": 0})


def test_the_endpoint_wins_over_the_connection_string(azure):
    AcsNotifier.from_env({**GOOD, "ACS_CONNECTION_STRING": "endpoint=x"})(notice())
    assert "endpoint" in azure and "conn" not in azure


def test_a_refused_domain_never_builds_the_client(azure):
    with pytest.raises(RuntimeError):
        AcsNotifier.from_env(GOOD)(notice(to_email="x@evil.test"))
    assert "endpoint" not in azure and "conn" not in azure
