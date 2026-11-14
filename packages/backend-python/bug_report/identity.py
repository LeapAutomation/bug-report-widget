"""Who is asking: the signed-in person behind a request.

Moved here from the apps (stoppageviewer's ``trusted_identity`` /
``trusted_person``, the near-identical copies in PIV, seal and the fleet
dashboard) so there is one. Nothing reads the environment: an app passes its
own flags, usually ``trust_easyauth=env_flag("TRUST_EASYAUTH")``.

Two sources, both only when the app says to believe them:

* **EasyAuth** (``trust_easyauth=True``): Container Apps / App Service
  validates the sign-in and injects ``X-Ms-Client-Principal`` (base64 JSON,
  claims), stripping any client-supplied copy. The flag is explicit rather
  than inferred: believing that header when EasyAuth is NOT attached lets
  anyone mint an identity.
* **Tailscale Serve** (``loopback_headers=True``): Serve injects
  ``Tailscale-User-Login`` / ``-Name`` and dials the backend from loopback.
  Apps bind 0.0.0.0 (WSL2), which also exposes the port to the LAN, so the
  loopback peer is required as well.
"""
from __future__ import annotations

import base64
import json
import os

LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
TAILSCALE_HEADERS = ("Tailscale-User-Login", "Tailscale-User-Name")

#: Claim types EasyAuth may use for the signed-in address, in preference order.
EMAIL_CLAIMS = (
    "preferred_username",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
    "emails",
    "email",
    "name",
)


def env_flag(name: str, environ=None) -> bool:
    """``1`` / ``true`` / ``yes`` in the environment variable ``name``."""
    return (environ if environ is not None else os.environ).get(name, "").strip().lower() in (
        "1", "true", "yes")


def entra_claims(request) -> dict:
    """EasyAuth's ``X-Ms-Client-Principal`` decoded to ``{claim type: value}``;
    ``{}`` when absent or malformed (a malformed header is "no identity")."""
    raw = request.headers.get("X-Ms-Client-Principal")
    if not raw:
        return {}
    try:
        data = json.loads(base64.b64decode(raw).decode("utf-8"))
        return {c.get("typ"): c.get("val") for c in data.get("claims", [])}
    except Exception:  # noqa: BLE001
        return {}


def entra_identity(request) -> str:
    """The signed-in address from the EasyAuth principal, or ""."""
    claims = entra_claims(request)
    for typ in EMAIL_CLAIMS:
        v = claims.get(typ)
        if isinstance(v, str) and "@" in v:
            return v.strip()
    return ""


def _on_loopback(request) -> bool:
    return (request.remote_addr or "") in LOOPBACK


def trusted_identity(request, *, trust_easyauth: bool = False,
                     loopback_headers: bool = True,
                     identity_headers=TAILSCALE_HEADERS) -> str:
    """The caller's address, or "" when it cannot be trusted."""
    if trust_easyauth:
        who = entra_identity(request)
        if who:
            return who
    if loopback_headers and _on_loopback(request):
        for h in identity_headers:
            v = request.headers.get(h)
            if v:
                return v
    return ""


def trusted_person(request, *, trust_easyauth: bool = False,
                   loopback_headers: bool = True,
                   identity_headers=TAILSCALE_HEADERS) -> dict:
    """The trusted caller as ``{"email", "name"}``: who filed a report and who
    moved it on, named. The name comes from the same sign-in as the address,
    never from the page: Entra's ``name`` claim, or Tailscale's
    ``Tailscale-User-Name``. "" when the sign-in carries none (the backend then
    derives one from the address)."""
    email = trusted_identity(request, trust_easyauth=trust_easyauth,
                             loopback_headers=loopback_headers,
                             identity_headers=identity_headers)
    name = ""
    if email:
        if trust_easyauth and entra_identity(request) == email:
            name = entra_claims(request).get("name") or ""
        elif loopback_headers and _on_loopback(request) and len(identity_headers) > 1:
            name = request.headers.get(identity_headers[1], "")
    name = name.strip() if isinstance(name, str) else ""
    return {"email": email, "name": "" if "@" in name else name}


def person_provider(**options):
    """``() -> dict`` for ``create_proxy_blueprint``: the current Flask
    request's trusted person, built from these options."""
    def provider() -> dict:
        from flask import request
        return trusted_person(request, **options)
    return provider
