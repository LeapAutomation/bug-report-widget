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


def test_a_non_admin_person_sees_features_and_their_own_through_the_proxy(front):
    """3.2.0: the list and the detail are filtered by the checked person; triage is still 403."""
    mine = file(front)                                    # filed by the admin
    feat = file(front, tags=["feature"])
    front.person[0] = {"email": "someone@example.com", "name": ""}
    own = file(front)
    ids = lambda r: {b["id"] for b in r.get_json()["bugs"]}          # noqa: E731
    r = front.c.get("/api/bugs?days=1")
    assert r.status_code == 200 and ids(r) == {feat, own}            # not the admin's bug
    assert front.c.get(f"/api/bugs/{mine}").status_code == 404
    assert front.c.get(f"/api/bugs/{own}").status_code == 200
    assert front.c.post(f"/api/bugs/{feat}/vote").get_json()["voted"] is True
    assert front.c.patch(f"/api/bugs/{own}", json={"area": "x"}).status_code == 403
    front.person[0] = {"email": "admin@example.com", "name": ""}
    assert ids(front.c.get("/api/bugs?days=1")) == {mine, feat, own}  # admin unchanged


def test_an_untrusted_app_is_a_502_on_every_route_not_a_403(front):
    """The app was refused, not the person: a 403 here would read as the person's (3.0.1)."""
    bid = file(front)
    front.token[0] = "ok:client-stranger"
    for method, path in (("post", "/api/bugs"), ("get", "/api/bugs"), ("get", f"/api/bugs/{bid}"),
                         ("patch", f"/api/bugs/{bid}"), ("post", f"/api/bugs/{bid}/vote"),
                         ("get", f"/api/bugs/{bid}/screenshot")):
        r = getattr(front.c, method)(path, json={"details": "x", "status": "triaged"} if method != "get" else None)
        assert r.status_code == 502, (method, path)
        assert r.get_json() == {"error": "the reports service refused this app"}


def test_a_token_that_does_not_verify_is_a_502_not_a_401(front):
    """A 401 from the proxy reads, to a seal laptop's outbox, as "your station pass was refused" (3.0.1)."""
    front.token[0] = "forged"
    r = front.c.post("/api/bugs", json={"details": "x"})
    assert r.status_code == 502 and r.get_json() == {"error": "the reports service refused this app"}


def test_the_real_status_and_body_of_a_refusal_go_to_the_log(front, caplog):
    front.token[0] = "ok:client-stranger"
    with caplog.at_level("WARNING"):
        front.c.get("/api/bugs")
    assert "403" in caplog.text and "caller is not a trusted app" in caplog.text


def test_not_signed_in_to_vote_is_still_a_401_through_the_proxy(front):
    bid = file(front)
    front.person[0] = {"email": "", "name": ""}
    assert front.c.post(f"/api/bugs/{bid}/vote").status_code == 401   # the person's, not the app's


def test_a_list_is_this_apps_own_reports_unless_it_asks_otherwise(central):
    s, store, _ = central
    seal = Front(s.url, app_name="sealviewer")
    seal.token[0] = "ok:client-seal"
    plc = Front(s.url)
    mine = file(seal)
    theirs = file(plc)
    ids = lambda r: [b["id"] for b in r.get_json()["bugs"]]            # noqa: E731
    assert ids(seal.c.get("/api/bugs?status=open&days=365")) == [mine]    # what the open count asks
    assert ids(plc.c.get("/api/bugs?status=open&days=365")) == [theirs]
    assert ids(seal.c.get("/api/bugs")) == [mine]
    assert sorted(ids(seal.c.get("/api/bugs?app=placements"))) == [theirs]  # an explicit app passes as given
    assert store.get_bug(theirs).app == "placements"


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


# ---- 3.0.2: never ask the service to close the connection; a short body is a 502 -------------------

def test_the_forwarded_request_never_asks_to_close_the_connection(central, front):
    # Azure Container Apps' internal ingress cut a 761 KB screenshot short whenever the request said
    # "Connection: close" (urllib always does): 3 of 3 short, 3 of 3 whole without it (2026-11-15).
    _, _, seen = central
    file(front)
    front.c.get("/api/bugs")
    assert seen and all(h.get("Connection", "").lower() != "close" for h in seen), [h.get("Connection") for h in seen]


def test_a_body_that_arrives_short_is_a_502_not_a_crash_or_a_short_200():
    import socket

    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(1)
    port = lsock.getsockname()[1]

    def serve_short():
        conn, _ = lsock.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 100000\r\n\r\n" + b"x" * 1000)
        conn.close()

    t = threading.Thread(target=serve_short, daemon=True)
    t.start()
    flask_app = Flask(__name__)
    flask_app.register_blueprint(create_proxy_blueprint(
        f"http://127.0.0.1:{port}/bugs", "placements", lambda: "ok:client-placements",
        lambda: {"email": "admin@example.com"}), url_prefix="/api")
    r = flask_app.test_client().get("/api/bugs/bug-20261103-133031-e19b68/screenshot")
    t.join(3)
    lsock.close()
    assert r.status_code == 502 and "incomplete" in r.get_json()["error"]


def test_can_triage_and_my_vote_pass_through_the_proxy_unchanged(front):
    feat = file(front, tags=["feature"])
    front.c.post(f"/api/bugs/{feat}/vote")
    listed = front.c.get("/api/bugs?days=1").get_json()
    assert listed["canTriage"] is True and listed["bugs"][0]["myVote"] is True
    front.person[0] = {"email": "someone@example.com", "name": ""}
    listed = front.c.get("/api/bugs?days=1").get_json()
    assert listed["canTriage"] is False and listed["bugs"][0]["myVote"] is False
    assert front.c.get(f"/api/bugs/{feat}").get_json()["canTriage"] is False
