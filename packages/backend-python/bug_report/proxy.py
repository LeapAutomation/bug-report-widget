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
``person_provider()``, and ``X-Leap-App: <app>``. Any ``Authorization`` or
``X-Leap-*`` header the browser sent is dropped: the app's own identity, not
the page's claim, is what the service looks at. Standard library only.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import quote

from flask import Blueprint, Response, request

log = logging.getLogger(__name__)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 302 (a signed screenshot URL) goes back to the browser as it is."""
    def redirect_request(self, *a, **kw):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

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

    def _ascii(v: str) -> str:
        return quote(v or "", safe=" @.+-_'")

    @bp.route(url_prefix, defaults={"rest": ""}, methods=["GET", "POST", "PATCH", "DELETE"])
    @bp.route(f"{url_prefix}/<path:rest>", methods=["GET", "POST", "PATCH", "DELETE"])
    def forward(rest: str):
        if any(seg in ("..", ".") or "\\" in seg for seg in rest.split("/")):
            return _err(400, "invalid path")
        url = base + ("/" + rest if rest else "")
        if request.query_string:
            url += "?" + request.query_string.decode("latin-1")
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
        req = urllib.request.Request(url, data=data, headers=headers, method=request.method)
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                return _relay(r.status, r.read(), r.headers)
        except urllib.error.HTTPError as e:   # a 3xx/4xx/5xx from the service is its answer
            try:
                return _relay(e.code, e.read(), e.headers)
            finally:
                e.close()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            log.warning("reports proxy (%s): %s unreachable: %s", app, url, getattr(e, "reason", e))
            return _err(502, "the reports service is not reachable")

    def _relay(status, body, upstream_headers):
        resp = Response(body, status=status)
        for h in _BACK:
            v = upstream_headers.get(h)
            if v is not None:
                resp.headers[h] = v
        return resp

    def _err(status, message):
        return Response(json.dumps({"error": message}), status=status, mimetype="application/json")

    return bp
