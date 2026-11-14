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


def caller(headers=None, json_body=None, method="POST", verifier=verify, trusted=TRUSTED, remote="203.0.113.9"):
    with Flask(__name__).test_request_context("/", method=method, headers=headers or {}, json=json_body,
                                              environ_base={"REMOTE_ADDR": remote}):
        return trusted_caller(request, trusted, verifier)


def app_headers(azp=PLACEMENTS_ID, claim="azp", **extra):
    return {"Authorization": f"Bearer ok:{claim}={azp}", "X-Leap-Actor-Email": "chris@example.com",
            "X-Leap-Actor-Name": "Chris H", **extra}


def test_a_trusted_app_is_believed_and_the_app_name_comes_from_its_token():
    c = caller(app_headers(**{"X-Leap-App": "placements"}))
    assert (c.error, c.app, c.email, c.name, c.app_ignored) == (
        None, "placements", "chris@example.com", "Chris H", None)


def test_the_appid_claim_spelling_works_too():
    c = caller(app_headers(azp=SEAL_ID, claim="appid"))
    assert (c.error, c.app) == (None, "sealviewer")


def test_x_leap_app_loses_to_the_token_and_the_caller_says_which_won():
    c = caller(app_headers(**{"X-Leap-App": "leaphq"}))
    assert c.app == "placements" and c.app_ignored == "leaphq"


def test_a_percent_encoded_name_is_decoded():
    c = caller({**app_headers(), "X-Leap-Actor-Name": "Se%C3%A1n O%27Brien"})
    assert c.name == "Seán O'Brien"






def test_a_token_for_an_untrusted_app_is_refused_403_and_never_falls_back():
    c = caller({**app_headers(azp="client-stranger"),
                "X-Ms-Client-Principal": principal(preferred_username="real@example.com")})
    assert c.error[0] == 403 and c.email == "" and c.app is None


def test_a_verifier_that_raises_is_401_and_never_falls_back():
    c = caller({"Authorization": "Bearer forged", "X-Leap-Actor-Email": "boss@example.com",
                "X-Ms-Client-Principal": principal(preferred_username="real@example.com")})
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


# ---- no person path (Ken, 2026-11-14): every request needs an app token --------

def test_no_bearer_token_is_401_even_with_actor_headers_and_a_forged_gateway_principal():
    forged = {"X-Ms-Client-Principal": principal(preferred_username="boss@example.com", name="Boss"),
              "X-Leap-Actor-Email": "boss@example.com", "X-Leap-Actor-Name": "Boss", "X-Leap-App": "leaphq",
              "Tailscale-User-Login": "boss@example.com"}
    for remote in ("203.0.113.9", "127.0.0.1"):           # a loopback peer changes nothing either
        c = caller(forged, remote=remote)
        assert c.error[0] == 401 and (c.email, c.name, c.app) == ("", "", None), remote
    assert caller({}).error[0] == 401


def test_there_is_no_option_to_turn_the_gateway_principal_on():
    # `trust_easyauth` belongs to an app's own trusted_person(); asking the service for it is an error, not a no-op
    with Flask(__name__).test_request_context("/", headers={"X-Ms-Client-Principal": principal(preferred_username="b@example.com")}):
        with pytest.raises(TypeError):
            trusted_caller(request, TRUSTED, verify, trust_easyauth=True)
        with pytest.raises(TypeError):
            TrustedCallers(TRUSTED, verify, trust_easyauth=True)


def test_the_guard_refuses_a_forged_principal_with_401_on_the_blueprint():
    from bug_report import InMemoryStore, create_blueprint
    callers = TrustedCallers(TRUSTED, verify)
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(store=InMemoryStore(), guard=callers.guard,
                                            actor_identity=callers.actor_identity, app_stamp=callers.app_stamp))
    c = app.test_client()
    r = c.post("/bugs", json={"details": "x"}, headers={"X-Ms-Client-Principal": principal(preferred_username="b@example.com")})
    assert r.status_code == 401
    for method, path in (("get", "/bugs"), ("get", "/bugs/bug-20261101-100000-aaaaaa"), ("get", "/bugs/bug-20261101-100000-aaaaaa/screenshot"),
                         ("post", "/bugs/bug-20261101-100000-aaaaaa/vote")):
        assert getattr(c, method)(path).status_code == 401, path
