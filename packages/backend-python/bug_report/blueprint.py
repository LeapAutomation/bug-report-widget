"""Flask blueprint implementing the bug-report API spec.

Mount with ``app.register_blueprint(create_blueprint(store=...), url_prefix='/api')``.
"""

from __future__ import annotations

import base64
import io
import logging
import time
from typing import Callable

from flask import Blueprint, abort, jsonify, request, send_file

from . import contract
from .email import Notice
from .models import (
    BUG_DETAILS_MAX,
    BUG_SCREENSHOT_MAX,
    BUG_STATUSES,
    new_bug_id,
    person_name,
    screenshot_content_type,
    valid_bug_id,
)
from .store import (
    AuditEntry,
    BugRecord,
    NotFound,
    Store,
)

log = logging.getLogger(__name__)

_last_now_ns = 0


def _now_iso() -> str:
    """ISO-8601 UTC with microsecond precision, monotonically increasing
    within the process. Microseconds matter for sort stability when many
    reports land in the same second (test loops, agent bursts); the
    monotonic guard ensures even faster-than-clock-resolution bursts get
    distinct timestamps."""
    global _last_now_ns
    ns = time.time_ns()
    if ns <= _last_now_ns:
        ns = _last_now_ns + 1000  # bump by 1µs
    _last_now_ns = ns
    s, us = divmod(ns, 1_000_000_000)
    us //= 1000
    return f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(s))}.{us:06d}Z"


def _since_iso(days: int) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%S.000000Z",
        time.gmtime(time.time() - days * 86400),
    )


def create_blueprint(
    *,
    store: Store,
    is_admin: Callable | None = None,
    default_actor_email: str = "",
    actor_identity: Callable | None = None,
    build_sha: str | Callable[[], str] = "",
    url_prefix: str = "/bugs",
    notifier: Callable | None = None,
    app_url: str = "",
) -> Blueprint:
    """Build a Flask blueprint that serves the bug-report API.

    Args:
        store: An object implementing the ``Store`` protocol.
        is_admin: ``(request) -> bool`` gate for ``GET /bugs`` and ``PATCH``.
                  Default: allow everything (suitable for single-user apps).
        default_actor_email: Email stamped on reports filed without one.
        actor_identity: ``(request) -> str | dict`` returning the CHECKED identity
                  of the person filing (e.g. from EasyAuth's principal): an
                  address, or ``{"email": ..., "name": ...}`` (2.2.0). When
                  given, it alone decides the report's ``actorEmail``: the
                  payload's ``actorEmail`` is ignored, because a page can
                  claim any address, and an empty answer files the report
                  with no actor rather than with the claim. Without it the
                  payload's value is used, as before. Added in 2.1.0. The
                  same identity stamps ``changedBy``/``changedByName`` on a
                  status change (2.2.0), replacing the request body's claim.
        build_sha: String or zero-arg callable returning the current build SHA.
                   Stamped on each report. Empty string means "unknown".
        url_prefix: Mount path within the blueprint. Outer app typically
                    mounts the whole blueprint under ``/api``.
        notifier: ``(Notice) -> str`` that emails the reporter when a PATCH
                  closes their report with ``notify: true``; returns a message
                  id, raises on failure (2.2.0). See ``bug_report.email``.
                  ``None``: no email is ever sent.
        app_url: Link used in the email when the report has no page URL.
    """
    if is_admin is None:
        is_admin = lambda req: True  # noqa: E731 — single-user default

    bp = Blueprint("bug_report", __name__)

    def _resolve_build_sha() -> str:
        if callable(build_sha):
            try:
                return build_sha() or ""
            except Exception:
                return ""
        return build_sha or ""

    def _identity(req) -> tuple[str, str]:
        """(email, name) from the identity hook; a str answer is an address
        only. An unreadable identity is no identity, never a 500."""
        try:
            who = actor_identity(req)
        except Exception:
            return "", ""
        if isinstance(who, dict):
            return (str(who.get("email") or "").strip()[:200],
                    str(who.get("name") or "").strip()[:200])
        return (who or "").strip()[:200], ""

    # ----- POST /bugs ---------------------------------------------------

    @bp.route(url_prefix, methods=["POST"])
    def api_create():
        if request.is_json:
            payload = request.get_json(silent=True) or {}
        else:
            payload = {k: v for k, v in request.form.items()}

        details = (payload.get("details") or "").strip()
        if not details:
            return jsonify({"error": "details is required"}), 400
        if len(details.encode("utf-8")) > BUG_DETAILS_MAX:
            return jsonify({"error": f"details exceeds {BUG_DETAILS_MAX} bytes"}), 413

        title = (payload.get("title") or details.splitlines()[0])[:100].strip()
        added_by = (payload.get("addedBy") or payload.get("added_by") or "web").strip()[:32] or "web"
        if actor_identity is not None:
            actor_email, actor_name = _identity(request)
        else:
            actor_email = (
                payload.get("actorEmail") or payload.get("actor_email") or default_actor_email
            ).strip()[:200]
            actor_name = str(payload.get("actorName") or payload.get("actor_name") or "").strip()[:200]

        tags = payload.get("tags") or ["bug"]
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        if not isinstance(tags, list):
            tags = ["bug"]
        if added_by == "agent" and "agent-self-report" not in tags:
            tags.append("agent-self-report")

        shot_bytes: bytes | None = None
        raw = payload.get("screenshot")
        if raw and isinstance(raw, str):
            if raw.startswith("data:"):
                raw = raw.split(",", 1)[1] if "," in raw else ""
            try:
                shot_bytes = base64.b64decode(raw, validate=False) if raw else None
            except Exception:
                shot_bytes = None
            if shot_bytes and len(shot_bytes) > BUG_SCREENSHOT_MAX:
                return jsonify({"error": f"screenshot exceeds {BUG_SCREENSHOT_MAX} bytes"}), 413
            if shot_bytes is not None and not shot_bytes:
                shot_bytes = None

        transcript = payload.get("transcript")
        if transcript is not None and not isinstance(transcript, list):
            transcript = None

        bug = BugRecord(
            id=new_bug_id(),
            title=title,
            details=details,
            tags=tags,
            status="open",
            added_by=added_by,
            actor_email=actor_email,
            actor_name=actor_name,
            added_at=_now_iso(),
            meta_url=(payload.get("metaUrl") or payload.get("meta_url") or "")[:500],
            meta_user_agent=(payload.get("metaUserAgent") or payload.get("meta_user_agent") or "")[:300],
            meta_build_sha=(payload.get("metaBuildSha") or payload.get("meta_build_sha") or _resolve_build_sha())[:40],
            transcript=transcript,
        )
        store.put_bug(bug, shot_bytes)
        store.append_audit(bug.id, AuditEntry(
            changed_at=_now_iso(),
            to_status="open",
            from_status=None,
            changed_by=actor_email or added_by,
            changed_by_name=actor_name,
            note="filed",
        ))
        return jsonify({"id": bug.id}), 201

    # ----- GET /bugs ----------------------------------------------------

    @bp.route(url_prefix, methods=["GET"])
    def api_list():
        if not is_admin(request):
            abort(403)
        status_filter = (request.args.get("status") or "").strip() or None
        if status_filter and status_filter not in BUG_STATUSES:
            return jsonify({"error": f"status must be one of {BUG_STATUSES}"}), 400
        try:
            days = max(1, min(365, int(request.args.get("days") or 30)))
        except ValueError:
            return jsonify({"error": "days must be an integer"}), 400
        rows = [contract.with_kind(b.to_summary())
                for b in store.list_bugs(status=status_filter, since_iso=_since_iso(days))]
        rows.sort(key=lambda r: r["addedAt"], reverse=True)
        return jsonify({"bugs": rows})

    # ----- GET /bugs/<id> ----------------------------------------------

    @bp.route(f"{url_prefix}/<bug_id>", methods=["GET"])
    def api_get(bug_id: str):
        if not valid_bug_id(bug_id):
            return jsonify({"error": "invalid id"}), 400
        try:
            bug = store.get_bug(bug_id)
        except NotFound:
            return jsonify({"error": "not found"}), 404
        full = bug.to_full()
        full["canNotify"] = bool(notifier is not None and bug.actor_email)
        full["audit"] = [a.to_dict() for a in store.list_audit(bug_id)]
        return jsonify(full)

    # ----- PATCH /bugs/<id> --------------------------------------------

    @bp.route(f"{url_prefix}/<bug_id>", methods=["PATCH"])
    def api_patch(bug_id: str):
        if not is_admin(request):
            abort(403)
        if not valid_bug_id(bug_id):
            return jsonify({"error": "invalid id"}), 400
        try:
            bug = store.get_bug(bug_id)
        except NotFound:
            return jsonify({"error": "not found"}), 404

        payload = request.get_json(silent=True) or {}
        new_status = (payload.get("status") or "").strip()
        if new_status not in BUG_STATUSES:
            return jsonify({"error": f"status must be one of {BUG_STATUSES}"}), 400
        note = (payload.get("note") or "")[:500]
        old = bug.status
        # The lifecycle gate. This route used to check only that the
        # status was a member of the enum, which let `open -> resolved`
        # through here and 409 in a sibling app reading the same queue.
        # A same-status PATCH is the documented no-op below, so it is not
        # a transition and is exempt.
        if new_status != old and not contract.is_valid_transition(old, new_status):
            return jsonify({"error": f"cannot transition {old} -> {new_status}"}), 409
        if (new_status in contract.REASON_REQUIRED_FOR and new_status != old
                and not note.strip()):
            return jsonify({"error": f"a reason is required to mark a report {new_status}"}), 400
        will_notify = (payload.get("notify") is True and new_status != old
                       and new_status in ("resolved", "declined"))
        if will_notify and not note.strip():
            return jsonify({"error": "a note for the reporter is required to email them"}), 400
        if actor_identity is not None:
            # the checked identity, not the body's claim
            closer_email, changed_by_name = _identity(request)
            changed_by = (closer_email or default_actor_email or "admin")[:200]
        else:
            changed_by = (
                payload.get("changedBy") or payload.get("changed_by") or default_actor_email or "admin"
            )[:200]
            changed_by_name = str(
                payload.get("changedByName") or payload.get("changed_by_name") or ""
            ).strip()[:200]
            closer_email = changed_by if "@" in changed_by else ""

        old_status = old
        if new_status == old_status:
            # No-op: don't write a meaningless audit row.
            full = contract.with_kind(bug.to_full())
            full["audit"] = [a.to_dict() for a in store.list_audit(bug_id)]
            return jsonify(full)

        bug = store.update_status(bug_id, new_status)
        store.append_audit(bug_id, AuditEntry(
            changed_at=_now_iso(),
            to_status=new_status,
            from_status=old_status,
            changed_by=changed_by,
            changed_by_name=changed_by_name,
            note=note,
        ))
        notified = None
        if will_notify:
            notified = _notify(bug, new_status, note, closer_email, changed_by, changed_by_name)
        full = contract.with_kind(bug.to_full())
        full["audit"] = [a.to_dict() for a in store.list_audit(bug_id)]
        if notified is not None:
            full["notified"] = notified
        return jsonify(full)

    def _notify(bug, new_status, note, closer_email, changed_by, changed_by_name) -> dict:
        """Email the reporter after the change is safely written. Never raises
        and never undoes the change; the outcome is its own audit row."""
        to = bug.actor_email
        who = person_name(bug.actor_name, to) or "the reporter"
        error = ""
        if notifier is None:
            error = "email is not set up on this server"
        elif not to:
            error = "this report has no address to email"
        else:
            try:
                notifier(Notice(
                    to_email=to, to_name=person_name(bug.actor_name, to),
                    title=bug.title, note=note.strip(), status=new_status,
                    bug_id=bug.id, filed_at=bug.added_at, page_url=bug.meta_url,
                    app_url=app_url, closer_email=closer_email,
                    closer_name=person_name(changed_by_name, closer_email),
                ))
            except Exception as e:
                log.warning("bug notify: %s: %s", bug.id, e)
                error = (str(e) or type(e).__name__)[:200]
        label = f"{who} <{to}>" if to else who
        store.append_audit(bug.id, AuditEntry(
            changed_at=_now_iso(),
            to_status=new_status,
            from_status=new_status,
            changed_by=changed_by,
            changed_by_name=changed_by_name,
            note=(f"email to {label} failed: {error}" if error else f"emailed {label}")[:500],
        ))
        out = {"ok": not error, "to": to}
        if error:
            out["error"] = error
        return out

    # ----- GET /bugs/<id>/screenshot -----------------------------------

    @bp.route(f"{url_prefix}/<bug_id>/screenshot", methods=["GET"])
    def api_screenshot(bug_id: str):
        if not valid_bug_id(bug_id):
            abort(400)
        try:
            data = store.get_screenshot(bug_id)
        except NotFound:
            abort(404)
        return send_file(io.BytesIO(data), mimetype=screenshot_content_type(data))

    return bp
