"""Python TypedDicts matching openapi.yaml.

Hand-maintained; tiny enough that hand-edit is cheaper than codegen.
If you edit a field here, edit ``types.ts`` and ``openapi.yaml`` to match.
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

Status = Literal["open", "triaged", "resolved", "declined"]


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


class BugPatch(TypedDict, total=False):
    status: Status  # required
    note: str
    changedBy: str
    changedByName: str
    notify: bool  # email the reporter on resolved/declined; needs note (backends 2.2.0)


class ApiError(TypedDict):
    error: str
