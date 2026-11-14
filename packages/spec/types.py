"""Python TypedDicts matching openapi.yaml.

Hand-maintained; tiny enough that hand-edit is cheaper than codegen.
If you edit a field here, edit ``types.ts`` and ``openapi.yaml`` to match.
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

Status = Literal["open", "triaged", "resolved", "declined"]
Kind = Literal["bug", "feature"]
Priority = Literal["now", "next", "later"]
Product = Literal["PikPak", "Seal", "Placement", "Internal"]
App = Literal["stoppages", "fleet", "report", "placements", "sealviewer", "leaphq"]


class TranscriptTurn(TypedDict, total=False):
    ts: str
    model: str
    userMessage: str
    assistantResponse: str
    toolCalls: list[dict]  # [{"name": "..."}]


class BugCreate(TypedDict, total=False):
    title: str
    details: str  # required
    tags: list[str] | str
    addedBy: str
    actorEmail: str
    actorName: str  # display name of the person filing, when not derivable from auth (backends 2.2.0)
    metaUrl: str
    metaUserAgent: str
    metaBuildSha: str
    screenshot: str  # data URL or raw base64
    screenshotError: str  # why there is no screenshot (1.6.0+), or why part of it is blank (1.6.4+); <= 200 chars
    clientKey: str  # idempotency key (widget 1.6.0+, opt-in); <= 64 chars
    transcript: list[TranscriptTurn]
    kind: Kind  # stored when given; derived from `tags` when not (contract v3)


class BugSummary(TypedDict):
    id: str
    title: str
    tags: list[str]
    status: Status
    addedBy: str
    actorEmail: NotRequired[str]
    actorName: NotRequired[str]  # "" when unknown (backends 2.2.0)
    addedAt: str
    screenshot: bool
    # Contract v3 (backends 3.0.0). A v2 record reads back with these null and votes 0.
    kind: NotRequired[Kind]
    app: NotRequired[App | None]  # stamped by the proxy from the caller's identity
    product: NotRequired[Product | None]  # derived from `app` (report-contract.json appProducts)
    area: NotRequired[str | None]
    priority: NotRequired[Priority | None]
    owner: NotRequired[str | None]  # an email
    parentId: NotRequired[str | None]
    duplicateOf: NotRequired[str | None]
    votes: NotRequired[int]


class AuditEntry(TypedDict):
    changedAt: str
    changedBy: NotRequired[str]
    changedByName: NotRequired[str]  # "" when unknown (backends 2.2.0)
    fromStatus: Status | None
    toStatus: Status
    note: NotRequired[str]


class Bug(BugSummary):
    details: str
    metaUrl: NotRequired[str]
    metaUserAgent: NotRequired[str]
    metaBuildSha: NotRequired[str]
    transcript: NotRequired[list[TranscriptTurn] | None]
    canNotify: NotRequired[bool]  # GET /bugs/<id> only (backends 2.2.0)
    notified: NotRequired["Notified"]  # PATCH response only, when notify was requested (backends 2.2.0)
    audit: list[AuditEntry]


class Notified(TypedDict):
    ok: bool
    to: str
    error: NotRequired[str]


class Voted(TypedDict):
    votes: int
    voted: bool


class BugCreated(TypedDict):
    id: str
    app: App | None  # the app the report was filed under (backends 3.0.0)
    appIgnored: NotRequired[str]  # the X-Leap-App a trusted app sent that lost to its token


class BugPatch(TypedDict, total=False):
    status: Status  # required unless a triage field is sent (backends 3.0.0)
    area: str | None  # triage fields (3.0.0); null or "" clears
    priority: Priority | None
    owner: str | None
    parentId: str | None
    duplicateOf: str | None  # also declines the report: note "duplicate of <id>"
    note: str
    changedBy: str
    changedByName: str
    notify: bool  # email the reporter on resolved/declined; needs note (backends 2.2.0)


class ApiError(TypedDict):
    error: str
