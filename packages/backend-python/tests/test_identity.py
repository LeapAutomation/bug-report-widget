"""bug_report.identity: who the signed-in person is (moved from the apps)."""
import base64
import json

import pytest
from flask import Flask

from bug_report import identity


def principal(**claims):
    body = {"claims": [{"typ": k, "val": v} for k, v in claims.items()]}
    return base64.b64encode(json.dumps(body).encode()).decode()


def person(headers=None, remote="203.0.113.9", **opts):
    with Flask(__name__).test_request_context("/", headers=headers or {},
                                              environ_base={"REMOTE_ADDR": remote}):
        from flask import request
        return identity.trusted_person(request, **opts)


def test_easyauth_header_is_believed_only_when_the_app_says_so():
    h = {"X-Ms-Client-Principal": principal(preferred_username="chris@example.com", name="Chris H")}
    assert person(h) == {"email": "", "name": ""}                       # not attached: anyone could send it
    assert person(h, trust_easyauth=True) == {"email": "chris@example.com", "name": "Chris H"}


def test_a_name_that_is_an_address_is_no_name():
    h = {"X-Ms-Client-Principal": principal(preferred_username="c@example.com", name="c@example.com")}
    assert person(h, trust_easyauth=True) == {"email": "c@example.com", "name": ""}


def test_email_claim_preference_order_and_malformed_principal():
    h = {"X-Ms-Client-Principal": principal(email="b@example.com", preferred_username="a@example.com")}
    assert person(h, trust_easyauth=True)["email"] == "a@example.com"
    assert person({"X-Ms-Client-Principal": "%%%not-base64"}, trust_easyauth=True) == {"email": "", "name": ""}


def test_tailscale_headers_need_a_loopback_peer():
    h = {"Tailscale-User-Login": "ken@example.com", "Tailscale-User-Name": "Ken M"}
    assert person(h, remote="203.0.113.9", loopback_headers=True) == {"email": "", "name": ""}   # a LAN peer could forge them
    assert person(h, remote="127.0.0.1", loopback_headers=True) == {"email": "ken@example.com", "name": "Ken M"}
    assert person(h, remote="::1", loopback_headers=True)["email"] == "ken@example.com"


def test_tailscale_headers_are_off_by_default_even_from_loopback():
    h = {"Tailscale-User-Login": "ken@example.com"}
    assert person(h, remote="127.0.0.1")["email"] == ""


def test_env_flag():
    assert identity.env_flag("X", {"X": "Yes"}) and identity.env_flag("X", {"X": "1"})
    assert not identity.env_flag("X", {"X": "0"}) and not identity.env_flag("X", {})


def test_person_provider_reads_the_current_request():
    prov = identity.person_provider(trust_easyauth=True)
    with Flask(__name__).test_request_context(
            "/", headers={"X-Ms-Client-Principal": principal(preferred_username="z@example.com")}):
        assert prov() == {"email": "z@example.com", "name": ""}
