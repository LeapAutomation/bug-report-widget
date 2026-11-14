"""Service side of the proxy: who is calling, and which app.

Every request to the central service carries a token: an app's proxy
(``create_proxy_blueprint``) calls it with the app's OWN identity as a bearer
token, and names the signed-in person in two headers. The service believes
those headers only from an app it trusts, and takes the app's NAME from the
token, never from the request. There is no person path: a request with no
token is refused, and the service never looks at ``X-Ms-Client-Principal`` or
Tailscale headers (Ken, 2026-11-14). Those belong to an APP's own
``identity.trusted_person()``, behind that app's gateway.

The service injects ``verify_token``: a callable that takes the raw bearer
token and returns its claims dict, or raises. This library adds no JWT
dependency and does not validate tokens itself, so **``verify_token`` must
check the signature, the issuer, the audience and the expiry**. A verifier
that only decodes the token makes every trusted client id spoofable: anyone
can write ``azp`` into a token they made up.

* **No bearer token:** 401.
* **A bad token** (the verifier raises, or returns no claims): 401. **A token
  from an app not in ``trusted_apps``**: 403. Neither falls back to anything.
* **A good token from a trusted app:** the app is the token's ``azp`` or
  ``appid`` claim, looked up in ``trusted_apps`` (client id -> app name);
  ``X-Leap-Actor-Email`` / ``-Name`` are believed; a ``X-Leap-App`` header that
  disagrees is ignored, and the response says which won (``X-Leap-App-Used`` /
  ``X-Leap-App-Ignored``; the POST body carries ``app`` and ``appIgnored``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from urllib.parse import unquote

#: where the resolved caller is cached for the life of one request
ENVIRON_KEY = "bug_report.caller"


@dataclass
class Caller:
    email: str = ""
    name: str = ""
    app: str | None = None
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


def trusted_caller(request, trusted_apps: dict, verify_token: Callable | None) -> Caller:
    """Resolve who is calling (see the module docstring)."""
    token = _bearer(request)
    if token is None:
        return Caller(error=(401, "a bearer token is required"))
    if not token or verify_token is None:
        return Caller(error=(401, "bearer token could not be verified"))
    try:
        claims = verify_token(token)
    except Exception:  # noqa: BLE001 — any failure to verify is a bad token
        return Caller(error=(401, "invalid bearer token"))
    if not isinstance(claims, dict):
        return Caller(error=(401, "invalid bearer token"))
    client = claims.get("azp") or claims.get("appid")
    app = trusted_apps.get(client) if isinstance(client, str) else None
    if not app:
        return Caller(error=(403, "caller is not a trusted app"))
    claimed = request.headers.get("X-Leap-App", "").strip()
    return Caller(
        email=_header(request, "X-Leap-Actor-Email"),
        name=_header(request, "X-Leap-Actor-Name"),
        app=app,
        app_ignored=claimed if claimed and claimed != app else None,
    )


class TrustedCallers:
    """``trusted_caller`` made ready for ``create_blueprint``::

        callers = TrustedCallers(TRUSTED_APPS, verify_token)
        create_blueprint(store=store, actor_identity=callers.actor_identity,
                         app_stamp=callers.app_stamp, guard=callers.guard,
                         is_admin=callers.is_admin_in(BUG_ADMINS))
    """

    def __init__(self, trusted_apps: dict, verify_token: Callable | None):
        self.trusted_apps = dict(trusted_apps)
        self.verify_token = verify_token

    def caller(self, request) -> Caller:
        c = request.environ.get(ENVIRON_KEY)
        if c is None:
            c = trusted_caller(request, self.trusted_apps, self.verify_token)
            request.environ[ENVIRON_KEY] = c
        return c

    def guard(self, request):
        """``create_blueprint(guard=...)``: refuse a missing, bad or untrusted token."""
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
