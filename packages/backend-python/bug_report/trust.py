"""Service side of the proxy: who is calling, and which app.

An app's proxy (``create_proxy_blueprint``) calls the central service with its
OWN identity as a bearer token, and names the signed-in person in two headers.
The service believes those headers only from an app it trusts, and takes the
app's NAME from the token, never from the request.

The service injects ``verify_token``: a callable that takes the raw bearer
token and returns its claims dict, or raises. This library adds no JWT
dependency and does not validate tokens itself. (Container Apps here have no
EasyAuth of their own, so ``X-Ms-Client-Principal`` is not proof of an app.)

* **A bearer token:** verified; a bad token is 401 and a token from an app not
  in ``trusted_apps`` is 403. Neither falls back to the headers. The app is the
  token's ``azp`` or ``appid`` claim, looked up in ``trusted_apps`` (client id
  -> app name). Only then are ``X-Leap-Actor-Email`` / ``-Name`` believed. A
  ``X-Leap-App`` header that disagrees is ignored, and the response says which
  won (``X-Leap-App-Used`` / ``X-Leap-App-Ignored``; the POST body carries
  ``app`` and ``appIgnored``).
* **No bearer token:** a person through the gateway. The actor is the one
  ``identity.trusted_person`` finds (the ``X-Leap-Actor-*`` headers are not
  read: anyone could send them), and ``app`` comes from the request body when
  it names a known app, else null.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from urllib.parse import unquote

from . import contract
from .identity import trusted_person

#: where the resolved caller is cached for the life of one request
ENVIRON_KEY = "bug_report.caller"


@dataclass
class Caller:
    email: str = ""
    name: str = ""
    app: str | None = None
    #: "app" (a trusted app calling for a person) | "person" | "anonymous" | "refused"
    via: str = "anonymous"
    #: the X-Leap-App value a trusted app sent that disagreed with its token (it lost)
    app_ignored: str | None = None
    #: (status, message) when the request must be refused
    error: tuple[int, str] | None = None


def _bearer(request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip() or ""
    return None


def _header(request, name: str) -> str:
    # The proxy percent-encodes (a name may not be latin-1); always decode.
    return unquote(request.headers.get(name, "")).strip()


def _body_app(request) -> str | None:
    body = None
    if request.method == "POST":
        if request.is_json:
            body = request.get_json(silent=True)
        else:
            body = request.form
    app = (body or {}).get("app") if hasattr(body, "get") else None
    return app if app in contract.APP_PRODUCTS else None


def trusted_caller(request, trusted_apps: dict, verify_token: Callable | None,
                   **identity_options) -> Caller:
    """Resolve who is calling (see the module docstring). ``identity_options``
    are ``identity.trusted_person``'s (``trust_easyauth``, ...)."""
    token = _bearer(request)
    if token is not None:
        if not token or verify_token is None:
            return Caller(via="refused", error=(401, "bearer token could not be verified"))
        try:
            claims = verify_token(token)
        except Exception:  # noqa: BLE001 — any failure to verify is a bad token
            return Caller(via="refused", error=(401, "invalid bearer token"))
        if not isinstance(claims, dict):
            return Caller(via="refused", error=(401, "invalid bearer token"))
        client = claims.get("azp") or claims.get("appid")
        app = trusted_apps.get(client) if isinstance(client, str) else None
        if not app:
            return Caller(via="refused", error=(403, "caller is not a trusted app"))
        claimed = request.headers.get("X-Leap-App", "").strip()
        return Caller(
            email=_header(request, "X-Leap-Actor-Email"),
            name=_header(request, "X-Leap-Actor-Name"),
            app=app, via="app",
            app_ignored=claimed if claimed and claimed != app else None,
        )
    person = trusted_person(request, **identity_options)
    return Caller(email=person["email"], name=person["name"], app=_body_app(request),
                  via="person" if person["email"] else "anonymous")


class TrustedCallers:
    """``trusted_caller`` made ready for ``create_blueprint``::

        callers = TrustedCallers(TRUSTED_APPS, verify_token, trust_easyauth=True)
        create_blueprint(store=store, actor_identity=callers.actor_identity,
                         app_stamp=callers.app_stamp, guard=callers.guard,
                         is_admin=callers.is_admin_in(BUG_ADMINS))
    """

    def __init__(self, trusted_apps: dict, verify_token: Callable | None, **identity_options):
        self.trusted_apps = dict(trusted_apps)
        self.verify_token = verify_token
        self.identity_options = identity_options

    def caller(self, request) -> Caller:
        c = request.environ.get(ENVIRON_KEY)
        if c is None:
            c = trusted_caller(request, self.trusted_apps, self.verify_token,
                               **self.identity_options)
            request.environ[ENVIRON_KEY] = c
        return c

    def guard(self, request):
        """``create_blueprint(guard=...)``: refuse a bad or untrusted token."""
        return self.caller(request).error

    def actor_identity(self, request) -> dict:
        c = self.caller(request)
        return {"email": c.email, "name": c.name}

    def app_stamp(self, request):
        return self.caller(request).app

    def is_admin_in(self, admins) -> Callable:
        """``is_admin`` for ``create_blueprint``: the person (not the proxying
        app) is on the list. Fails closed on an empty list."""
        allow = {a.strip().lower() for a in admins if a and a.strip()}

        def is_admin(request) -> bool:
            c = self.caller(request)
            return c.error is None and bool(c.email) and c.email.lower() in allow
        return is_admin
