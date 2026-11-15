"""An app's ``/api/bugs`` that forwards to the central reports service.

    from bug_report import create_proxy_blueprint
    from bug_report.identity import person_provider

    app.register_blueprint(create_proxy_blueprint(
        central_url="https://leaphq-reports.internal/api/bugs",
        app="placements",
        token_provider=lambda: credential.get_token(SCOPE).token,
        person_provider=person_provider(trust_easyauth=True),
    ), url_prefix="/api")

The core is ``forward()`` (3.1.0), a plain function with no web framework in it, for an app that
is not Flask - the daily report's standard-library ``http.server`` calls it directly. The blueprint
below is a thin Flask wrapper over it, so the rules live once.

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
from typing import Callable, Mapping
from urllib.parse import quote, urlsplit

from .trust import TRUST_REFUSAL_HEADER

log = logging.getLogger(__name__)


#: request headers passed on from the browser; everything else is dropped
_FORWARD = ("Content-Type", "Accept")
#: response headers passed back: the contract's, plus the redirect target
_BACK = ("Content-Type", "Location", "Cache-Control", "X-Leap-App-Used", "X-Leap-App-Ignored")
#: methods that carry a body to the service
_WITH_BODY = ("POST", "PATCH", "DELETE")


def _target(central_url: str):
    base = central_url.rstrip("/")
    target = urlsplit(base)
    if target.scheme not in ("http", "https") or not target.hostname:
        raise ValueError(f"central_url must be an http(s) URL: {central_url!r}")
    return target


def _ascii(v: str) -> str:
    return quote(v or "", safe=" @.+-_'")


def _err(status: int, message: str) -> tuple[int, bytes, dict]:
    return status, json.dumps({"error": message}).encode(), {"Content-Type": "application/json"}


def forward(
    method: str,
    rest: str,
    query: str,
    body: bytes | None,
    browser_headers: Mapping[str, str],
    *,
    central_url: str,
    app: str,
    token_provider: Callable[[], str],
    person_provider: Callable[[], dict | None],
    timeout: float = 30,
) -> tuple[int, bytes, dict]:
    """Forward one ``/bugs`` request to the central service; return ``(status, body, headers)``.

    ``rest`` is the path after ``/bugs`` ("" for the collection, "<id>", "<id>/vote", ...), ``query``
    the raw query string without the "?", ``body`` the request body (sent for POST, PATCH and DELETE
    only), ``browser_headers`` the browser's request headers (only Content-Type and Accept are taken,
    any case). Never raises for a service or network problem: those are 502s.
    """
    target = _target(central_url)
    method = method.upper()
    if any(seg in ("..", ".") or "\\" in seg for seg in rest.split("/")):
        return _err(400, "invalid path")
    path = target.path.rstrip("/") + ("/" + rest if rest else "")
    # The list is this app's own reports unless the caller says otherwise (3.0.1): the open count and
    # the queue must not show every app's, which is what the service gives a trusted app with no filter.
    if method == "GET" and not rest and "app" not in {p.split("=", 1)[0] for p in query.split("&") if p}:
        query = (query + "&" if query else "") + "app=" + quote(app, safe="")
    if query:
        path += "?" + query
    try:
        person = person_provider() or {}
        lowered = {k.lower(): v for k, v in browser_headers.items()}
        headers = {h: lowered[h.lower()] for h in _FORWARD if h.lower() in lowered}
        headers.update({
            "Authorization": f"Bearer {token_provider()}",
            "X-Leap-Actor-Email": _ascii(person.get("email", "")),
            "X-Leap-Actor-Name": _ascii(person.get("name", "")),
            "X-Leap-App": app,
        })
    except Exception as e:  # noqa: BLE001 - no token / identity: say so, do not guess
        log.warning("reports proxy (%s): could not get a token or identity: %s", app, e)
        return _err(502, "could not authenticate to the reports service")
    data = body if method in _WITH_BODY else None
    connection = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    conn = connection(target.hostname, target.port, timeout=timeout)
    try:
        # Any status - 3xx (a signed screenshot URL goes back as it is), 4xx, 5xx - is the service's answer.
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        status, payload, upstream = r.status, r.read(), r.headers
    except http.client.IncompleteRead as e:
        log.warning("reports proxy (%s): %s came back short: %d bytes, %s more expected",
                    app, path, len(e.partial), e.expected)
        return _err(502, "the reports service's answer came back incomplete")
    except (http.client.HTTPException, OSError, TimeoutError) as e:
        log.warning("reports proxy (%s): %s unreachable: %s", app, path, e)
        return _err(502, "the reports service is not reachable")
    finally:
        conn.close()
    if status in (401, 403) and upstream.get(TRUST_REFUSAL_HEADER):
        # The service refused THIS APP (a setup mistake), not the person. A 401 here would read, to a
        # client that treats 401 as "your sign-in failed" (a seal laptop's outbox), as the person's.
        log.warning("reports proxy (%s): the service refused this app: %s %s", app, status, payload[:300])
        return _err(502, "the reports service refused this app")
    return status, payload, {h: upstream[h] for h in _BACK if upstream.get(h) is not None}


def create_proxy_blueprint(
    central_url: str,
    app: str,
    token_provider: Callable[[], str],
    person_provider: Callable[[], dict | None],
    *,
    url_prefix: str = "/bugs",
    timeout: float = 30,
):
    from flask import Blueprint, Response, request

    _target(central_url)    # a bad URL fails at start-up, not on the first report
    bp = Blueprint(f"bug_report_proxy_{app}", __name__)

    @bp.route(url_prefix, defaults={"rest": ""}, methods=["GET", "POST", "PATCH", "DELETE"])
    @bp.route(f"{url_prefix}/<path:rest>", methods=["GET", "POST", "PATCH", "DELETE"])
    def _forward(rest: str):
        status, payload, headers = forward(
            request.method, rest, request.query_string.decode("latin-1"), request.get_data(),
            request.headers, central_url=central_url, app=app, token_provider=token_provider,
            person_provider=person_provider, timeout=timeout)
        resp = Response(payload, status=status)
        for k, v in headers.items():
            resp.headers[k] = v
        return resp

    return bp
