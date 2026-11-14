"""create_proxy_blueprint, end to end: a real central blueprint served over HTTP,
the proxy app in front of it, fake token and person providers."""
from __future__ import annotations

import base64
import json
import threading

import pytest
from flask import Flask, request
from werkzeug.serving import make_server

from bug_report import FilesystemStore, TrustedCallers, create_blueprint, create_proxy_blueprint

PNG = bytes.fromhex("89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4890000000D"
                    "49444154789C6300010000000500010D0A2DB40000000049454E44AE426082")
JPEG = bytes.fromhex("FFD8FFE000104A46494600010100000100010000FFD9")
APP_CLIENTS = {"client-placements": "placements", "client-seal": "sealviewer"}
ADMINS = ["admin@example.com"]


def verify(token):
    if not token.startswith("ok:"):
        raise ValueError("bad token")
    return {"azp": token[3:]}


class Serving:
    def __init__(self, flask_app):
        self.srv = make_server("127.0.0.1", 0, flask_app, threaded=True)
        self.port = self.srv.server_port
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.srv.shutdown()
        self.t.join(3)


@pytest.fixture
def central(tmp_path):
    seen = []
    store = FilesystemStore(tmp_path / "central")
    callers = TrustedCallers(APP_CLIENTS, verify)
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(
        store=store, is_admin=callers.is_admin_in(ADMINS), actor_identity=callers.actor_identity,
        app_stamp=callers.app_stamp, guard=callers.guard))

    @app.before_request
    def spy():
        seen.append({k: v for k, v in request.headers.items()})

    s = Serving(app)
    yield s, store, seen
    s.stop()


class Front:
    """The app side: a proxy mounted at /api, with providers a test can change."""
    def __init__(self, central_url, app_name="placements"):
        self.token = ["ok:client-placements"]
        self.person = [{"email": "admin@example.com", "name": "Ann Admin"}]
        flask_app = Flask(__name__)
        flask_app.register_blueprint(create_proxy_blueprint(
            central_url + "/bugs", app_name, lambda: self.token[0], lambda: self.person[0]),
            url_prefix="/api")
        self.c = flask_app.test_client()


@pytest.fixture
def front(central):
    return Front(central[0].url)


def file(front, **kw):
    r = front.c.post("/api/bugs", json={"details": "it broke", **kw})
    assert r.status_code == 201, r.data
    return r.get_json()["id"]


def test_post_is_stamped_with_the_apps_name_and_the_signed_in_person(front, central):
    _, store, _ = central
    r = front.c.post("/api/bugs", json={"details": "it broke", "app": "leaphq",
                                        "actorEmail": "liar@example.com"})
    assert r.status_code == 201 and r.content_type == "application/json"
    assert r.get_json()["app"] == "placements"
    rec = store.get_bug(r.get_json()["id"])
    assert (rec.app, rec.actor_email, rec.actor_name) == ("placements", "admin@example.com", "Ann Admin")


def test_the_forwarded_request_carries_the_four_headers(front, central):
    _, _, seen = central
    front.c.post("/api/bugs", json={"details": "x"},
                 headers={"Authorization": "Bearer browser-claim", "X-Leap-Actor-Email": "liar@example.com",
                          "X-Leap-App": "leaphq"})
    h = seen[-1]
    assert h["Authorization"] == "Bearer ok:client-placements"       # the app's, not the page's
    assert h["X-Leap-Actor-Email"] == "admin@example.com"
    assert h["X-Leap-Actor-Name"] == "Ann Admin"
    assert h["X-Leap-App"] == "placements"


def test_every_route_goes_through_with_status_body_and_content_type_unchanged(front, central):
    bid = file(front, screenshot=base64.b64encode(JPEG).decode(), tags=["feature"])
    # list (admin)
    r = front.c.get("/api/bugs?days=1&kind=feature&app=placements")
    assert r.status_code == 200 and [b["id"] for b in r.get_json()["bugs"]] == [bid]
    # detail
    d = front.c.get(f"/api/bugs/{bid}").get_json()
    assert d["app"] == "placements" and d["product"] == "Placement" and d["votes"] == 0
    # patch: status and triage fields
    r = front.c.patch(f"/api/bugs/{bid}", json={"status": "triaged", "area": "Belt", "priority": "now"})
    assert r.status_code == 200 and r.get_json()["area"] == "Belt"
    assert r.get_json()["audit"][-1]["changedBy"] == "admin@example.com"
    # vote and unvote
    assert front.c.post(f"/api/bugs/{bid}/vote").get_json() == {"votes": 1, "voted": True}
    assert front.c.delete(f"/api/bugs/{bid}/vote").get_json() == {"votes": 0, "voted": False}
    # screenshot: bytes and the sniffed type
    r = front.c.get(f"/api/bugs/{bid}/screenshot")
    assert (r.status_code, r.content_type, r.data) == (200, "image/jpeg", JPEG)
    # errors keep their status and body
    r = front.c.get("/api/bugs/bug-20200101-000000-aaaaaa")
    assert (r.status_code, r.get_json()) == (404, {"error": "not found"})
    assert front.c.get("/api/bugs/junk").status_code == 400
    assert front.c.post("/api/bugs", json={}).status_code == 400
    big = front.c.post("/api/bugs", json={"details": "x" * (10 * 1024 + 1)})
    assert big.status_code == 413


def test_a_non_admin_person_gets_the_central_403_through_the_proxy(front):
    bid = file(front)
    front.person[0] = {"email": "someone@example.com", "name": ""}
    r = front.c.get("/api/bugs")
    assert r.status_code == 403
    assert front.c.patch(f"/api/bugs/{bid}", json={"area": "x"}).status_code == 403
    assert front.c.post(f"/api/bugs/{bid}/vote").status_code == 200      # voting is for anyone signed in


def test_an_untrusted_app_token_is_refused_403_on_every_route(front):
    bid = file(front)
    front.token[0] = "ok:client-stranger"
    for method, path in (("post", "/api/bugs"), ("get", "/api/bugs"), ("get", f"/api/bugs/{bid}"),
                         ("patch", f"/api/bugs/{bid}"), ("post", f"/api/bugs/{bid}/vote"),
                         ("get", f"/api/bugs/{bid}/screenshot")):
        r = getattr(front.c, method)(path, json={"details": "x", "status": "triaged"} if method != "get" else None)
        assert r.status_code == 403, (method, path)
        assert r.get_json()["error"] == "caller is not a trusted app"


def test_a_token_that_does_not_verify_is_401(front):
    front.token[0] = "forged"
    r = front.c.post("/api/bugs", json={"details": "x"})
    assert r.status_code == 401


def test_x_leap_app_that_disagrees_with_the_token_loses_and_the_response_says_so(central):
    s, store, _ = central
    f = Front(s.url, app_name="leaphq")          # the proxy CLAIMS leaphq...
    f.token[0] = "ok:client-seal"                # ...but its token is the seal viewer's
    r = f.c.post("/api/bugs", json={"details": "x"})
    assert r.status_code == 201
    body = r.get_json()
    assert body["app"] == "sealviewer" and body["appIgnored"] == "leaphq"
    assert r.headers["X-Leap-App-Used"] == "sealviewer" and r.headers["X-Leap-App-Ignored"] == "leaphq"
    assert store.get_bug(body["id"]).app == "sealviewer"


def test_a_name_with_accents_survives_the_header(front, central):
    _, store, _ = central
    front.person[0] = {"email": "sean@example.com", "name": "Seán Ó Briain"}
    bid = file(front)
    assert store.get_bug(bid).actor_name == "Seán Ó Briain"


def test_a_central_that_is_down_is_a_502_with_a_message():
    f = Front("http://127.0.0.1:9")
    r = f.c.post("/api/bugs", json={"details": "x"})
    assert r.status_code == 502 and r.get_json() == {"error": "the reports service is not reachable"}   # no host, no reason


def test_a_token_provider_that_fails_is_a_502_never_an_unauthenticated_call(central):
    s, _, seen = central
    def boom():
        raise RuntimeError("no managed identity here")
    app = Flask(__name__)
    app.register_blueprint(create_proxy_blueprint(s.url + "/bugs", "placements", boom, lambda: {}), url_prefix="/api")
    r = app.test_client().post("/api/bugs", json={"details": "x"})
    assert r.status_code == 502 and r.get_json() == {"error": "could not authenticate to the reports service"}
    assert "managed identity" not in r.get_data(as_text=True)    # the exception text stays in the log
    assert seen == []                    # nothing reached the service


def test_a_redirect_from_the_service_is_passed_on_not_followed():
    other = Flask(__name__)

    @other.route("/bugs/<i>/screenshot")
    def shot(i):
        from flask import redirect
        return redirect("https://blob.example/signed?sig=1", 302)

    s = Serving(other)
    try:
        f = Front(s.url)
        r = f.c.get("/api/bugs/bug-20261101-100000-aaaaaa/screenshot")
        assert r.status_code == 302 and r.headers["Location"] == "https://blob.example/signed?sig=1"
    finally:
        s.stop()


def test_a_non_json_body_and_content_type_come_back_untouched():
    other = Flask(__name__)

    @other.route("/bugs")
    def lst():
        return "plain text ✓", 200, {"Content-Type": "text/plain; charset=utf-8"}

    s = Serving(other)
    try:
        r = Front(s.url).c.get("/api/bugs")
        assert (r.status_code, r.content_type, r.data.decode()) == (200, "text/plain; charset=utf-8", "plain text ✓")
    finally:
        s.stop()


def test_a_dot_dot_path_segment_is_refused_400_and_never_forwarded(front, central):
    _, _, seen = central
    n = len(seen)
    for path in ("/api/bugs/../admin", "/api/bugs/bug-20261101-100000-aaaaaa/../../x", "/api/bugs/./x"):
        r = front.c.get(path)
        assert r.status_code in (400, 404), path          # werkzeug may normalise `..` away before the route
    r = front.c.open("/api/bugs/%2e%2e/admin")
    assert r.status_code == 400 and r.get_json() == {"error": "invalid path"}
    r = front.c.open("/api/bugs/x%5Cy")
    assert r.status_code == 400
    assert len(seen) == n


def test_the_central_service_itself_refuses_a_request_with_no_token(central):
    import urllib.error
    import urllib.request
    s, _, _ = central
    for headers in ({}, {"X-Leap-Actor-Email": "admin@example.com", "X-Leap-App": "leaphq"},
                    {"X-Ms-Client-Principal": base64.b64encode(json.dumps(
                        {"claims": [{"typ": "preferred_username", "val": "admin@example.com"}]}).encode()).decode()}):
        req = urllib.request.Request(s.url + "/bugs", data=b'{"details": "x"}', headers={"Content-Type": "application/json", **headers})
        try:
            urllib.request.urlopen(req)
            raise AssertionError("expected 401")
        except urllib.error.HTTPError as e:
            assert e.code == 401
