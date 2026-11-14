"""trusted_caller: a trusted app's headers are believed only behind a verified token."""
import base64
import json

import pytest
from flask import Flask, request

from bug_report import contract
from bug_report.trust import TrustedCallers, trusted_caller

PLACEMENTS_ID, SEAL_ID = "client-placements", "client-seal"
TRUSTED = {PLACEMENTS_ID: "placements", SEAL_ID: "sealviewer"}


def verify(token):
    """A fake verifier: tokens are `ok:<claim>=<value>`; anything else raises."""
    if not token.startswith("ok:"):
        raise ValueError("bad signature")
    k, v = token[3:].split("=", 1)
    return {k: v}


def principal(**claims):
    body = {"claims": [{"typ": k, "val": v} for k, v in claims.items()]}
    return base64.b64encode(json.dumps(body).encode()).decode()


def caller(headers=None, json_body=None, method="POST", verifier=verify, trusted=TRUSTED, **opts):
    with Flask(__name__).test_request_context("/", method=method, headers=headers or {}, json=json_body,
                                              environ_base={"REMOTE_ADDR": "203.0.113.9"}):
        return trusted_caller(request, trusted, verifier, **opts)


def app_headers(azp=PLACEMENTS_ID, claim="azp", **extra):
    return {"Authorization": f"Bearer ok:{claim}={azp}", "X-Leap-Actor-Email": "chris@example.com",
            "X-Leap-Actor-Name": "Chris H", **extra}


def test_a_trusted_app_is_believed_and_the_app_name_comes_from_its_token():
    c = caller(app_headers(**{"X-Leap-App": "placements"}))
    assert (c.via, c.error, c.app, c.email, c.name, c.app_ignored) == (
        "app", None, "placements", "chris@example.com", "Chris H", None)


def test_the_appid_claim_spelling_works_too():
    c = caller(app_headers(azp=SEAL_ID, claim="appid"))
    assert (c.error, c.app) == (None, "sealviewer")


def test_x_leap_app_loses_to_the_token_and_the_caller_says_which_won():
    c = caller(app_headers(**{"X-Leap-App": "leaphq"}))
    assert c.app == "placements" and c.app_ignored == "leaphq"


def test_a_percent_encoded_name_is_decoded():
    c = caller({**app_headers(), "X-Leap-Actor-Name": "Se%C3%A1n O%27Brien"})
    assert c.name == "Seán O'Brien"


def test_a_forged_actor_header_with_no_token_is_ignored():
    c = caller({"X-Leap-Actor-Email": "boss@example.com", "X-Leap-Actor-Name": "Boss",
                "X-Leap-App": "leaphq"})
    assert (c.error, c.email, c.name, c.app, c.via) == (None, "", "", None, "anonymous")


def test_a_forged_actor_header_does_not_beat_the_gateway_principal():
    c = caller({"X-Ms-Client-Principal": principal(preferred_username="real@example.com", name="Real P"),
                "X-Leap-Actor-Email": "boss@example.com"}, trust_easyauth=True)
    assert (c.email, c.name, c.via, c.app) == ("real@example.com", "Real P", "person", None)


def test_a_token_for_an_untrusted_app_is_refused_403_and_never_falls_back():
    c = caller({**app_headers(azp="client-stranger"),
                "X-Ms-Client-Principal": principal(preferred_username="real@example.com")}, trust_easyauth=True)
    assert c.error[0] == 403 and c.via == "refused" and c.email == "" and c.app is None


def test_a_verifier_that_raises_is_401_and_never_falls_back():
    c = caller({"Authorization": "Bearer forged", "X-Leap-Actor-Email": "boss@example.com",
                "X-Ms-Client-Principal": principal(preferred_username="real@example.com")}, trust_easyauth=True)
    assert c.error[0] == 401 and c.email == ""


@pytest.mark.parametrize("auth", ["Bearer ", "Bearer    "])
def test_an_empty_bearer_is_401(auth):
    assert caller({"Authorization": auth}).error[0] == 401


def test_a_verifier_answering_something_other_than_claims_is_401():
    assert caller(app_headers(), verifier=lambda t: "yes").error[0] == 401


def test_a_bearer_with_no_verifier_configured_is_401_not_trusted():
    assert caller(app_headers(), verifier=None).error[0] == 401


def test_a_token_with_no_azp_or_appid_is_403():
    assert caller({"Authorization": "Bearer ok:sub=someone"}).error[0] == 403


def test_a_person_through_the_gateway_may_name_a_known_app_in_the_body_only():
    h = {"X-Ms-Client-Principal": principal(preferred_username="p@example.com")}
    assert caller(h, {"details": "x", "app": "leaphq"}, trust_easyauth=True).app == "leaphq"
    assert caller(h, {"details": "x", "app": "made-up"}, trust_easyauth=True).app is None
    assert caller(h, {"details": "x"}, trust_easyauth=True).app is None
    assert caller(h, {"app": "leaphq"}, method="GET", trust_easyauth=True).app is None


def test_a_trusted_apps_body_app_is_ignored():
    assert caller(app_headers(), {"details": "x", "app": "leaphq"}).app == "placements"


def test_every_trusted_app_name_the_contract_knows_is_a_real_app():
    # the service's TRUSTED_APPS values must be apps with a product
    for app in ("stoppages", "fleet", "report", "placements", "sealviewer", "leaphq"):
        assert contract.product_for_app(app)


def test_trusted_callers_caches_per_request_and_exposes_the_hooks():
    calls = []
    tc = TrustedCallers(TRUSTED, lambda t: (calls.append(t), verify(t))[1])
    with Flask(__name__).test_request_context("/", headers=app_headers()):
        assert tc.guard(request) is None
        assert tc.app_stamp(request) == "placements"
        assert tc.actor_identity(request) == {"email": "chris@example.com", "name": "Chris H"}
    assert len(calls) == 1      # the token is verified once per request


def test_is_admin_in_is_about_the_person_not_the_app_and_fails_closed():
    tc = TrustedCallers(TRUSTED, verify)
    admin = tc.is_admin_in(["Chris@Example.com"])
    with Flask(__name__).test_request_context("/", headers=app_headers()):
        assert admin(request) is True
    with Flask(__name__).test_request_context("/", headers={**app_headers(), "X-Leap-Actor-Email": "o@example.com"}):
        assert admin(request) is False
    with Flask(__name__).test_request_context("/", headers=app_headers(azp="client-stranger")):
        assert admin(request) is False
    with Flask(__name__).test_request_context("/", headers=app_headers()):
        assert tc.is_admin_in([])(request) is False
