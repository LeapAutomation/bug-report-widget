/**
 * TypeScript types matching openapi.yaml. Hand-written; small enough that
 * a generator would be heavier than the payoff. If you edit one, edit the
 * other — CI greps for divergence.
 */

export type Status = "open" | "triaged" | "resolved" | "declined";
export type Kind = "bug" | "feature";
export type Priority = "now" | "next" | "later";
export type Product = "PikPak" | "Seal" | "Placement" | "Internal";
export type App = "stoppages" | "fleet" | "report" | "placements" | "sealviewer" | "leaphq";

export interface TranscriptTurn {
  ts?: string;
  model?: string;
  userMessage?: string;
  assistantResponse?: string;
  toolCalls?: Array<{ name: string }>;
}

export interface BugCreate {
  title?: string;
  details: string;
  tags?: string[] | string;
  addedBy?: string;
  actorEmail?: string;
  /** Display name of the person filing, when not derivable from auth (backends 2.2.0). */
  actorName?: string;
  metaUrl?: string;
  metaUserAgent?: string;
  metaBuildSha?: string;
  /** data: URL or raw base64 image (PNG or JPEG). ≤ 5 MB decoded. */
  screenshot?: string;
  /** Why there is no screenshot (widget 1.6.0+), or why part of it may be missing (1.6.4+). ≤ 200 chars. */
  screenshotError?: string;
  /** Idempotency key (widget 1.6.0+, opt-in): the same on every retry of one report. ≤ 64 chars. */
  clientKey?: string;
  transcript?: TranscriptTurn[];
  /** Stored when given; derived from `tags` when not (contract v3). */
  kind?: Kind;
}

export interface BugSummary {
  id: string;
  title: string;
  tags: string[];
  status: Status;
  addedBy: string;
  actorEmail?: string;
  /** Display name of whoever filed it; "" when unknown (backends 2.2.0). */
  actorName?: string;
  addedAt: string;
  screenshot: boolean;
  /** Contract v3 (backends 3.0.0). A v2 record reads back with these null and `votes` 0. */
  kind?: Kind;
  /** Stamped by the proxy from the caller's identity. */
  app?: App | null;
  /** Derived from `app` (report-contract.json appProducts). */
  product?: Product | null;
  area?: string | null;
  priority?: Priority | null;
  /** An email. */
  owner?: string | null;
  parentId?: string | null;
  duplicateOf?: string | null;
  votes?: number;
}

export interface AuditEntry {
  changedAt: string;
  changedBy?: string;
  /** Display name of changedBy; "" when unknown (backends 2.2.0). */
  changedByName?: string;
  fromStatus: Status | null;
  toStatus: Status;
  note?: string;
}

export interface Bug extends BugSummary {
  details: string;
  metaUrl?: string;
  metaUserAgent?: string;
  metaBuildSha?: string;
  transcript?: TranscriptTurn[] | null;
  /** GET /bugs/{id} only: the server can email the reporter and the report has an address (backends 2.2.0). */
  canNotify?: boolean;
  /** PATCH response only, when `notify` was requested (backends 2.2.0). */
  notified?: Notified;
  audit: AuditEntry[];
}

export interface Notified {
  ok: boolean;
  to: string;
  error?: string;
}

/** POST/DELETE /bugs/{id}/vote response (backends 3.0.0). */
export interface Voted {
  votes: number;
  voted: boolean;
}

/** POST /bugs 201 body. `app` is the app the report was filed under (backends 3.0.0). */
export interface BugCreated {
  id: string;
  app: App | null;
  /** The X-Leap-App a trusted app sent that lost to its token. */
  appIgnored?: string;
}

/** PATCH /bugs/{id} body. `notify` emails the reporter on a change to resolved/declined and needs `note`. */
export interface BugPatch {
  /** Required unless a triage field is sent (backends 3.0.0). */
  status?: Status;
  /** Triage fields (3.0.0); null or "" clears. `duplicateOf` also declines: note "duplicate of <id>". */
  area?: string | null;
  priority?: Priority | null;
  owner?: string | null;
  parentId?: string | null;
  duplicateOf?: string | null;
  note?: string;
  changedBy?: string;
  changedByName?: string;
  notify?: boolean;
}

export interface ApiError {
  error: string;
}
