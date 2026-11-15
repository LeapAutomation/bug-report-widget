"""bug_report.proxy.forward(): the framework-free core (3.1.0), called directly against a real central
blueprint served over HTTP. No Flask on the calling side: this is how a standard-library app uses it."""
from __future__ import annotations

import base64
import json
import socket
import threading

from bug_report.proxy import forward

from test_proxy import JPEG, Serving, central  # noqa: F401  (central is a fixture)


class Caller:
    def __init__(self, central_url, app="placements"):
        self.url = central_url + "/bugs"
        self.app = app
        self.token = "ok:client-placements"
        self.person = {"email": "admin@example.com", "name": "Ann Admin"}

    def __call__(self, method, rest="", query="", body=None, headers=None):
        return forward(method, rest, query, body, headers or {}, central_url=self.url, app=self.app,
                       token_provider=lambda: self.token, person_provider=lambda: self.person)

    def file(self, **kw):
        status, body, _ = self("POST", body=json.dumps({"details": "it broke", **kw}).encode(),
                               headers={"content-type": "application/json"})
        assert status == 201, body
        return json.loads(body)["id"]


def test_every_route_returns_status_body_and_headers(central):
    c = Caller(central[0].url)
    bid = c.file(screenshot=base64.b64encode(JPEG).decode(), tags=["feature"])
    status, body, h = c("GET", query="days=1&kind=feature&app=placements")
    assert status == 200 and h["Content-Type"] == "application/json"
    assert [b["id"] for b in json.loads(body)["bugs"]] == [bid]
    assert json.loads(c("GET", bid)[1])["app"] == "placements"
    status, body, _ = c("PATCH", bid, body=b'{"status": "triaged", "area": "Belt"}',
                        headers={"Content-Type": "application/json"})
    assert status == 200 and json.loads(body)["area"] == "Belt"
    assert json.loads(c("POST", f"{bid}/vote")[1]) == {"votes": 1, "voted": True}
    assert json.loads(c("DELETE", f"{bid}/vote")[1]) == {"votes": 0, "voted": False}
    status, body, h = c("GET", f"{bid}/screenshot")
    assert (status, h["Content-Type"], body) == (200, "image/jpeg", JPEG)
    assert c("GET", "bug-20200101-000000-aaaaaa")[0] == 404
    assert c("GET", "junk")[0] == 400


def test_the_service_sees_the_token_the_actor_and_the_app_and_only_two_browser_headers(central):
    _, store, seen = central
    c = Caller(central[0].url)
    status, body, _ = c("POST", body=b'{"details": "x", "app": "leaphq"}',
                        headers={"content-type": "application/json", "ACCEPT": "application/json",
                                 "Authorization": "Bearer browser-claim", "X-Leap-Actor-Email": "liar@example.com",
                                 "X-Leap-App": "leaphq", "Cookie": "session=1"})
    assert status == 201
    h = seen[-1]
    assert h["Authorization"] == "Bearer ok:client-placements"
    assert (h["X-Leap-Actor-Email"], h["X-Leap-Actor-Name"], h["X-Leap-App"]) == ("admin@example.com", "Ann Admin", "placements")
    assert "Cookie" not in h and h["Accept"] == "application/json"
    rec = store.get_bug(json.loads(body)["id"])
    assert (rec.app, rec.actor_email) == ("placements", "admin@example.com")


def test_an_accented_name_is_percent_encoded_and_survives(central):
    _, store, _ = central
    c = Caller(central[0].url)
    c.person = {"email": "sean@example.com", "name": "Seán Ó Briain"}
    assert store.get_bug(c.file()).actor_name == "Seán Ó Briain"


def test_a_list_is_this_apps_own_unless_it_names_an_app(central):
    s, _, _ = central
    mine = Caller(s.url, "sealviewer")
    mine.token = "ok:client-seal"
    theirs = Caller(s.url)
    a, b = mine.file(), theirs.file()
    ids = lambda r: [x["id"] for x in json.loads(r[1])["bugs"]]    # noqa: E731
    assert ids(mine("GET", query="status=open&days=365")) == [a]
    assert ids(mine("GET")) == [a]
    assert ids(mine("GET", query="app=placements")) == [b]


def test_dot_dot_dot_and_backslash_are_400_and_never_sent(central):
    _, _, seen = central
    c = Caller(central[0].url)
    n = len(seen)
    for rest in ("../admin", "x/../../y", "./x", "x\\y", "a/..\\b"):
        assert c("GET", rest)[0] == 400, rest
    assert c("GET", "..")[1] == b'{"error": "invalid path"}'
    assert len(seen) == n


def test_a_refusal_of_the_app_is_a_502_with_a_generic_body_and_the_real_one_is_logged(central, caplog):
    c = Caller(central[0].url)
    c.token = "ok:client-stranger"
    with caplog.at_level("WARNING"):
        status, body, _ = c("GET")
    assert status == 502 and json.loads(body) == {"error": "the reports service refused this app"}
    assert "caller is not a trusted app" in caplog.text and "caller is not a trusted app" not in body.decode()
    c.token = "forged"
    assert c("POST", body=b'{"details": "x"}')[0] == 502


def test_a_persons_403_and_401_pass_through(central):
    c = Caller(central[0].url)
    bid = c.file()
    c.person = {"email": "someone@example.com", "name": ""}
    assert c("GET")[0] == 403
    c.person = {"email": "", "name": ""}
    assert c("POST", f"{bid}/vote")[0] == 401


def test_a_token_or_identity_that_fails_is_a_502_and_nothing_is_sent(central):
    _, _, seen = central
    def boom():
        raise RuntimeError("no managed identity here")
    for kw in ({"token_provider": boom, "person_provider": lambda: {}},
               {"token_provider": lambda: "ok:client-placements", "person_provider": boom}):
        status, body, _ = forward("POST", "", "", b"{}", {}, central_url=central[0].url + "/bugs", app="placements", **kw)
        assert status == 502 and json.loads(body) == {"error": "could not authenticate to the reports service"}
        assert b"managed identity" not in body
    assert seen == []


def test_a_service_that_is_down_is_a_502_with_no_host_in_it():
    status, body, _ = forward("GET", "", "", None, {}, central_url="http://127.0.0.1:9/bugs", app="a",
                              token_provider=lambda: "t", person_provider=lambda: {})
    assert status == 502 and json.loads(body) == {"error": "the reports service is not reachable"}


def test_the_request_never_says_connection_close(central):
    _, _, seen = central
    c = Caller(central[0].url)
    c.file()
    c("GET")
    assert seen and all(h.get("Connection", "").lower() != "close" for h in seen)


def test_a_short_body_is_a_502():
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(1)

    def serve_short():
        conn, _ = lsock.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 100000\r\n\r\n" + b"x" * 1000)
        conn.close()

    t = threading.Thread(target=serve_short, daemon=True)
    t.start()
    status, body, _ = forward("GET", "x/screenshot", "", None, {}, central_url=f"http://127.0.0.1:{lsock.getsockname()[1]}/bugs",
                              app="a", token_provider=lambda: "t", person_provider=lambda: {})
    t.join(3)
    lsock.close()
    assert status == 502 and "incomplete" in json.loads(body)["error"]


def test_only_the_back_headers_come_back_and_a_redirect_is_not_followed():
    from flask import Flask, redirect
    other = Flask(__name__)

    @other.route("/bugs/<i>/screenshot")
    def shot(i):
        r = redirect("https://blob.example/signed?sig=1", 302)
        r.headers["Set-Cookie"] = "s=1"
        r.headers["X-Secret"] = "no"
        return r

    s = Serving(other)
    try:
        status, _, h = forward("GET", "bug-20261101-100000-aaaaaa/screenshot", "", None, {}, central_url=s.url + "/bugs",
                               app="a", token_provider=lambda: "t", person_provider=lambda: {})
        assert status == 302 and h["Location"] == "https://blob.example/signed?sig=1"
        assert set(h) <= {"Content-Type", "Location", "Cache-Control", "X-Leap-App-Used", "X-Leap-App-Ignored"}
    finally:
        s.stop()


def test_the_app_the_service_used_comes_back_in_the_headers(central):
    c = Caller(central[0].url, "leaphq")     # claims leaphq with the placements token
    status, _, h = c("POST", body=b'{"details": "x"}', headers={"Content-Type": "application/json"})
    assert status == 201 and h["X-Leap-App-Used"] == "placements" and h["X-Leap-App-Ignored"] == "leaphq"


def test_a_bad_central_url_raises():
    import pytest
    with pytest.raises(ValueError):
        forward("GET", "", "", None, {}, central_url="ftp://x", app="a", token_provider=lambda: "t", person_provider=lambda: {})
