"""An app's ``/api/bugs`` that forwards to the central reports service.

    from bug_report import create_proxy_blueprint
    from bug_report.identity import person_provider

    app.register_blueprint(create_proxy_blueprint(
        central_url="https://leaphq-reports.internal/api/bugs",
        app="placements",
        token_provider=lambda: credential.get_token(SCOPE).token,
        person_provider=person_provider(trust_easyauth=True),
    ), url_prefix="/api")

Every route (POST, list, detail, PATCH, vote, screenshot) is forwarded with
its method, query, body and content type, and the service's status code, body
and content type come back unchanged. Each forwarded request carries
``Authorization: Bearer <token_provider()>``, ``X-Leap-Actor-Email`` /
``X-Leap-Actor-Name`` (percent-encoded; the service decodes) from
``person_provider()``, and ``X-Leap-App: <app>``. A list with no ``app`` query is
asked for this app's own reports. A 401/403 the service marks as a refusal of the
APP (``X-Leap-Trust-Refusal``) comes back as a 502; its other 401/403 (not an
admin, sign in to vote) pass through. Any ``Authorization`` or
``X-Leap-*`` header the browser sent is dropped: the app's own identity, not
the page's claim, is what the service looks at. Standard library only.

The request goes out over ``http.client`` and never says ``Connection: close``
(3.0.2). Azure Container Apps' internal ingress cuts a large response short when
the request asks to close the connection - ``urllib`` always does - so a 761 KB
screenshot arrived as 65-311 KB and the proxy crashed (2026-11-15). A body that
still arrives short is a 502, never a truncated 200.
"""
from __future__ import annotations

import http.client
import json
import logging
from typing import Callable
from urllib.parse import quote, urlsplit

from flask import Blueprint, Response, request

from .trust import TRUST_REFUSAL_HEADER

log = logging.getLogger(__name__)


#: request headers passed on from the browser; everything else is dropped
_FORWARD = ("Content-Type", "Accept")
#: response headers passed back: the contract's, plus the redirect target
_BACK = ("Content-Type", "Location", "Cache-Control", "X-Leap-App-Used", "X-Leap-App-Ignored")


def create_proxy_blueprint(
    central_url: str,
    app: str,
    token_provider: Callable[[], str],
    person_provider: Callable[[], dict | None],
    *,
    url_prefix: str = "/bugs",
    timeout: float = 30,
) -> Blueprint:
    bp = Blueprint(f"bug_report_proxy_{app}", __name__)
    base = central_url.rstrip("/")
    target = urlsplit(base)
    if target.scheme not in ("http", "https") or not target.hostname:
        raise ValueError(f"central_url must be an http(s) URL: {central_url!r}")
    connection = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection

    def _ascii(v: str) -> str:
        return quote(v or "", safe=" @.+-_'")

    @bp.route(url_prefix, defaults={"rest": ""}, methods=["GET", "POST", "PATCH", "DELETE"])
    @bp.route(f"{url_prefix}/<path:rest>", methods=["GET", "POST", "PATCH", "DELETE"])
    def forward(rest: str):
        if any(seg in ("..", ".") or "\\" in seg for seg in rest.split("/")):
            return _err(400, "invalid path")
        path = target.path + ("/" + rest if rest else "")
        query = request.query_string.decode("latin-1")
        # The list is this app's own reports unless the caller says otherwise (3.0.1): the open count and
        # the queue must not show every app's, which is what the service gives a trusted app with no filter.
        if request.method == "GET" and not rest and "app" not in request.args:
            query = (query + "&" if query else "") + "app=" + quote(app, safe="")
        if query:
            path += "?" + query
        try:
            person = person_provider() or {}
            headers = {h: request.headers[h] for h in _FORWARD if h in request.headers}
            headers.update({
                "Authorization": f"Bearer {token_provider()}",
                "X-Leap-Actor-Email": _ascii(person.get("email", "")),
                "X-Leap-Actor-Name": _ascii(person.get("name", "")),
                "X-Leap-App": app,
            })
        except Exception as e:  # noqa: BLE001 — no token / identity: say so, do not guess
            log.warning("reports proxy (%s): could not get a token or identity: %s", app, e)
            return _err(502, "could not authenticate to the reports service")
        data = request.get_data() if request.method in ("POST", "PATCH", "DELETE") else None
        conn = connection(target.hostname, target.port, timeout=timeout)
        try:
            # Any status - 3xx (a signed screenshot URL goes back as it is), 4xx, 5xx - is the service's answer.
            conn.request(request.method, path, body=data, headers=headers)
            r = conn.getresponse()
            return _relay(r.status, r.read(), r.headers)
        except http.client.IncompleteRead as e:
            log.warning("reports proxy (%s): %s came back short: %d bytes, %s more expected",
                        app, path, len(e.partial), e.expected)
            return _err(502, "the reports service's answer came back incomplete")
        except (http.client.HTTPException, OSError, TimeoutError) as e:
            log.warning("reports proxy (%s): %s unreachable: %s", app, path, e)
            return _err(502, "the reports service is not reachable")
        finally:
            conn.close()

    def _relay(status, body, upstream_headers):
        if status in (401, 403) and upstream_headers.get(TRUST_REFUSAL_HEADER):
            # The service refused THIS APP (a setup mistake), not the person. A 401 here would read, to a
            # client that treats 401 as "your sign-in failed" (a seal laptop's outbox), as the person's.
            log.warning("reports proxy (%s): the service refused this app: %s %s", app, status, body[:300])
            return _err(502, "the reports service refused this app")
        resp = Response(body, status=status)
        for h in _BACK:
            v = upstream_headers.get(h)
            if v is not None:
                resp.headers[h] = v
        return resp

    def _err(status, message):
        return Response(json.dumps({"error": message}), status=status, mimetype="application/json")

    return bp
