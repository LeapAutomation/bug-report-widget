"""The report LIFECYCLE contract — the half the wire spec doesn't cover.

`packages/spec/openapi.yaml` pins the wire FORMAT: routes, field names,
shapes. It has always been deliberately quiet about the lifecycle, which
left every consuming app to decide for itself what `PATCH` may do — and
they decided differently. One enforced `open -> triaged -> resolved`,
another validated only that the status was a member of the enum, so the
same request was a 409 in one app and a 200 in the next. Nothing was
wrong by its own lights, and a merged queue across both was incoherent.

`packages/spec/report-contract.json` settles it, and this module is the
mirror the backend reads. `tests/test_contract.py` fails the build if the
two disagree.

Two things live here rather than in `models.py`: the transition map,
because a status enum cannot express a MOVE, and `kind`, because a queue
holds more than bugs and the choice was surviving only as a tag — so
every reader re-derived it, and they disagreed.
"""
from __future__ import annotations

STATUSES = ("open", "triaged", "resolved", "declined")

#: Closed means "nobody is waiting on this". A status not listed here
#: counts as open, INCLUDING one this contract has never heard of: a
#: triage list that silently drops an unrecognised report is the worse of
#: the two failures.
CLOSED_STATUSES = ("resolved", "declined")

#: What a server MUST accept, exactly — not a floor. An earlier draft said
#: "may accept more", which is how two implementations behaved differently
#: and both passed.
TRANSITIONS = {
    "open": ("triaged", "declined"),
    "triaged": ("resolved", "declined", "open"),
    "resolved": ("open",),
    "declined": ("open",),
}

#: What a triage UI shows — a subset. `triaged -> open` is legal but not
#: offered: a Reopen button on a triaged row reads as a mistake.
OFFERED = {
    "open": ("triaged", "declined"),
    "triaged": ("resolved", "declined"),
    "resolved": ("open",),
    "declined": ("open",),
}

#: Declining without saying why makes the queue a bin. The reason rides on
#: the audit row, never on the report: a later reopen must not erase the
#: record of why somebody said no.
REASON_REQUIRED_FOR = ("declined",)

KINDS = ("bug", "feature")
DEFAULT_KIND = "bug"

#: Derived from tags rather than migrated — every row in every existing
#: store predates the field. `capability` was retired as a kind across the
#: suite that drove this; its tag survives and reads as `feature`, so old
#: rows say what they always meant instead of falling back to `bug`.
KIND_TAGS = {
    "feature": ("feature", "feature-request", "capability", "capability-request"),
    "bug": ("bug", "bug-request"),
}


def kind_from_tags(tags) -> str:
    """The kind a report is, from its tags.

    A feature tag outranks `bug` because the widget adds `bug`
    unconditionally — so "has a bug tag" says nothing about what the
    report actually is, in either tag order.
    """
    norm = {str(t).strip().lower() for t in (tags or [])}
    if norm & set(KIND_TAGS["feature"]):
        return "feature"
    return DEFAULT_KIND


def is_valid_transition(from_status: str, to_status: str) -> bool:
    """The server gate. Exactly the map above — no wider."""
    return to_status in TRANSITIONS.get((from_status or "open").lower(), ())


def is_closed(status: str) -> bool:
    return (status or "open").lower() in CLOSED_STATUSES


def effective_kind(kind, tags) -> str:
    """The stored kind when it is a declared one, else derived from the tags."""
    return kind if kind in KINDS else kind_from_tags(tags)


def with_kind(payload: dict) -> dict:
    """Stamp `kind` onto a wire payload: the kind it already carries when that
    is a declared one (v3 stores it), else derived from its tags. Idempotent."""
    payload["kind"] = effective_kind(payload.get("kind"), payload.get("tags"))
    return payload


# --- v3 (2026-11-14) ---------------------------------------------------------
# Mirrors report-contract.json `products`, `appProducts`, `priorities`,
# `triageFields`, `listFilters`; tests/test_contract.py fails if they drift.

PRODUCTS = ("PikPak", "Seal", "Placement", "Internal")

#: app -> product. `product` is DERIVED from `app` by this table, never stored.
APP_PRODUCTS = {
    "stoppages": "PikPak",
    "fleet": "PikPak",
    "report": "PikPak",
    "placements": "Placement",
    "sealviewer": "Seal",
    "leaphq": "Internal",
}

PRIORITIES = ("now", "next", "later")

#: What PATCH may set besides the status: wire name -> the record's attribute. The ONE map;
#: everything else (the blueprint, the stores, audit notes) derives from it.
TRIAGE_WIRE_TO_ATTR = {
    "area": "area",
    "priority": "priority",
    "owner": "owner",
    "parentId": "parent_id",
    "duplicateOf": "duplicate_of",
}
TRIAGE_ATTR_TO_WIRE = {a: w for w, a in TRIAGE_WIRE_TO_ATTR.items()}
TRIAGE_FIELDS = tuple(TRIAGE_WIRE_TO_ATTR)

LIST_FILTERS = ("status", "days", "app", "kind", "area", "priority")


def product_for_app(app):
    """The product an app belongs to, or None for no / an unknown app."""
    return APP_PRODUCTS.get(app) if app else None
