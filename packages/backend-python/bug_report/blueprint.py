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
from .trust import ENVIRON_KEY
from .store import (
    AuditEntry,
    BugRecord,
    NotFound,
    Store,
)

log = logging.getLogger(__name__)

#: the list filters a Store takes: the contract's, less the two the route itself reads
_STORE_FILTERS = tuple(f for f in contract.LIST_FILTERS if f not in ("status", "days"))

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
    app_stamp: Callable | None = None,
    guard: Callable | None = None,
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
        app_stamp: ``(request) -> str | None`` giving the app a new report is
                  filed under (contract v3 ``app``). The caller cannot set it:
                  the request body's ``app`` is never read here, only by a
                  hook that has checked who is asking (``TrustedCallers``).
                  Unknown apps (not in ``contract.APP_PRODUCTS``) are dropped.
                  Without the hook ``app`` stays null. Added in 3.0.0.
        guard: ``(request) -> None | (status, message)`` run before every
                  route; a tuple refuses the request (``TrustedCallers.guard``:
                  a bad or untrusted bearer token). Added in 3.0.0.
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

    @bp.before_request
    def _guard():
        if guard is None:
            return None
        refused = guard(request)
        if refused:
            status, message = refused
            return jsonify({"error": message}), status
        return None

    @bp.after_request
    def _say_which_app_won(resp):
        # Set by TrustedCallers: the app the token says, and the one the caller
        # claimed in X-Leap-App when the two disagreed (the claim lost).
        caller = request.environ.get(ENVIRON_KEY)
        if caller is not None and getattr(caller, "app", None):
            resp.headers["X-Leap-App-Used"] = caller.app
            if getattr(caller, "app_ignored", None):
                resp.headers["X-Leap-App-Ignored"] = caller.app_ignored
        return resp

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

        kind = contract.effective_kind(payload.get("kind"), tags)
        app = None
        if app_stamp is not None:
            try:
                app = app_stamp(request)
            except Exception:
                app = None
            app = app if app in contract.APP_PRODUCTS else None

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
            app=app,
            kind=kind,
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
        out = {"id": bug.id, "app": app}
        caller = request.environ.get(ENVIRON_KEY)
        if getattr(caller, "app_ignored", None):
            out["appIgnored"] = caller.app_ignored
        return jsonify(out), 201

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
        # the store's filters are the contract's (`listFilters`) minus the two handled above
        wanted = {f: (request.args.get(f) or "").strip() or None for f in _STORE_FILTERS}
        if wanted.get("kind") and wanted["kind"] not in contract.KINDS:
            return jsonify({"error": f"kind must be one of {contract.KINDS}"}), 400
        if wanted.get("priority") and wanted["priority"] not in contract.PRIORITIES:
            return jsonify({"error": f"priority must be one of {contract.PRIORITIES}"}), 400
        bugs = list(store.list_bugs(status=status_filter, since_iso=_since_iso(days), **wanted))
        counts = _vote_counts([b.id for b in bugs])
        rows = [contract.with_kind({**b.to_summary(), "votes": counts.get(b.id, 0)}) for b in bugs]
        rows.sort(key=lambda r: r["addedAt"], reverse=True)
        return jsonify({"bugs": rows})

    def _vote_counts(ids):
        """Votes per report; a store that predates votes counts none."""
        counter = getattr(store, "vote_counts", None)
        return counter(ids) if counter and ids else {}

    def _detail(bug) -> dict:
        full = contract.with_kind(bug.to_full())
        full["votes"] = _vote_counts([bug.id]).get(bug.id, 0)
        full["audit"] = [a.to_dict() for a in store.list_audit(bug.id)]
        return full

    # ----- GET /bugs/<id> ----------------------------------------------

    @bp.route(f"{url_prefix}/<bug_id>", methods=["GET"])
    def api_get(bug_id: str):
        if not valid_bug_id(bug_id):
            return jsonify({"error": "invalid id"}), 400
        try:
            bug = store.get_bug(bug_id)
        except NotFound:
            return jsonify({"error": "not found"}), 404
        full = _detail(bug)
        full["canNotify"] = bool(notifier is not None and bug.actor_email)
        return jsonify(full)

    # ----- PATCH /bugs/<id> --------------------------------------------

    def _triage_changes(bug, payload):
        """Validate the triage fields in a PATCH body and return
        ``({attr: new value}, error)``: only fields that actually change, and a
        ``(status, message)`` refusal for the first bad one. A null or empty
        value clears a field."""
        changes: dict = {}
        for wire in contract.TRIAGE_FIELDS:
            if wire not in payload:
                continue
            raw = payload[wire]
            val = None if raw is None else str(raw).strip()
            val = val or None
            attr = contract.TRIAGE_WIRE_TO_ATTR[wire]
            if wire == "area" and val and len(val) > 100:
                return None, (400, "area is at most 100 characters")
            if wire == "priority" and val and val not in contract.PRIORITIES:
                return None, (400, f"priority must be one of {contract.PRIORITIES}")
            if wire == "owner" and val and ("@" not in val or len(val) > 200):
                return None, (400, "owner must be an email address")
            if wire in ("parentId", "duplicateOf") and val:
                if not valid_bug_id(val):
                    return None, (400, f"{wire} must be a report id")
                if val == bug.id:
                    return None, (400, f"{wire} cannot be the report itself")
                try:
                    store.get_bug(val)
                except NotFound:
                    return None, (404, f"{wire}: no such report")
            if wire == "parentId" and val:
                # an epic holds its children: a parent chain must never loop back
                seen, up = set(), val
                while up and up not in seen:
                    if up == bug.id:
                        return None, (400, "parentId would make a cycle")
                    seen.add(up)
                    try:
                        up = store.get_bug(up).parent_id
                    except NotFound:
                        break
            if getattr(bug, attr) != val:
                changes[attr] = val
        return changes, None

    def _describe(bug, changes) -> str:
        shown = lambda v: "none" if v is None else str(v)  # noqa: E731
        return "; ".join(
            f"{contract.TRIAGE_ATTR_TO_WIRE[a]}: {shown(getattr(bug, a))} -> {shown(v)}" for a, v in changes.items()
        )[:500]

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
        has_triage = any(f in payload for f in contract.TRIAGE_FIELDS)
        if not (has_triage and not new_status) and new_status not in BUG_STATUSES:
            return jsonify({"error": f"status must be one of {BUG_STATUSES}"}), 400
        note = (payload.get("note") or "")[:500]
        old = bug.status
        changes, refused = _triage_changes(bug, payload)
        if refused:
            return jsonify({"error": refused[1]}), refused[0]
        # A duplicate is a decline: setting `duplicateOf` closes the report with
        # the note "duplicate of <id>". (Clearing it changes nothing else.)
        dup = changes.get("duplicate_of")
        if dup:
            if new_status and new_status != "declined":
                return jsonify({"error": "duplicateOf declines the report; status must be declined or omitted"}), 400
            new_status = "declined"
            note = f"duplicate of {dup}" + (f": {note}" if note.strip() else "")
        elif not new_status:
            new_status = old
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
        if new_status == old_status and not changes:
            # No-op: don't write a meaningless audit row.
            return jsonify(_detail(bug))

        # What the audit row for the fields says, worked out against the record as it is NOW.
        decline_row = new_status != old_status
        described = _describe(bug, {a: v for a, v in changes.items()
                                    if not (decline_row and a == "duplicate_of")})
        if changes or decline_row:
            # one atomic write of status and fields together (no lost update)
            bug = store.update_bug(bug_id, status=new_status if decline_row else None, changes=changes)
        if decline_row:
            store.append_audit(bug_id, AuditEntry(
                changed_at=_now_iso(),
                to_status=new_status,
                from_status=old_status,
                changed_by=changed_by,
                changed_by_name=changed_by_name,
                note=note,
            ))
        if described:
            store.append_audit(bug_id, AuditEntry(
                changed_at=_now_iso(),
                to_status=bug.status,
                from_status=bug.status,
                changed_by=changed_by,
                changed_by_name=changed_by_name,
                note=described,
            ))
        notified = None
        if will_notify:
            notified = _notify(bug, new_status, note, closer_email, changed_by, changed_by_name)
        full = _detail(bug)
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

    # ----- POST / DELETE /bugs/<id>/vote (3.0.0) -----------------------

    def _voter(req) -> str:
        """Who is voting: the checked identity when the app gave one, else (as
        for filing) the request's own claim. "" means nobody is signed in."""
        if actor_identity is not None:
            return _identity(req)[0]
        body = req.get_json(silent=True) if req.is_json else None
        claim = (body or {}).get("actorEmail") or req.args.get("actorEmail") or default_actor_email
        return str(claim or "").strip()[:200]

    def _vote(bug_id: str, adding: bool):
        if not valid_bug_id(bug_id):
            return jsonify({"error": "invalid id"}), 400
        voter = _voter(request)
        if not voter:
            return jsonify({"error": "sign in to vote"}), 401
        if not hasattr(store, "add_vote"):
            return jsonify({"error": "this store does not keep votes"}), 501
        try:
            (store.add_vote if adding else store.remove_vote)(bug_id, voter)
        except NotFound:
            return jsonify({"error": "not found"}), 404
        return jsonify({"votes": store.vote_counts([bug_id]).get(bug_id, 0), "voted": adding})

    @bp.route(f"{url_prefix}/<bug_id>/vote", methods=["POST"])
    def api_vote(bug_id: str):
        return _vote(bug_id, True)

    @bp.route(f"{url_prefix}/<bug_id>/vote", methods=["DELETE"])
    def api_unvote(bug_id: str):
        return _vote(bug_id, False)

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
