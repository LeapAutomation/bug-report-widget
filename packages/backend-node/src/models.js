// Model-layer constants, ID helpers, validation. Pure JS; no Express, no fs.
// Mirror of packages/backend-python/bug_report/models.py — keep them in sync.

import { randomBytes } from "node:crypto";

export const BUG_DETAILS_MAX = 10 * 1024;          // 10 KB
export const BUG_SCREENSHOT_MAX = 5 * 1024 * 1024;  // 5 MB decoded

export const BUG_STATUSES = ["open", "triaged", "resolved", "declined"];

// Contract v3 (report-contract.json; tests/contract-drift.test.js keeps these in step).
export const KINDS = ["bug", "feature"];
export const KIND_TAGS = {
  feature: ["feature", "feature-request", "capability", "capability-request"],
  bug: ["bug", "bug-request"],
};
export const PRIORITIES = ["now", "next", "later"];
export const APP_PRODUCTS = {
  stoppages: "PikPak", fleet: "PikPak", report: "PikPak",
  placements: "Placement", sealviewer: "Seal", leaphq: "Internal",
};
export const TRIAGE_FIELDS = ["area", "priority", "owner", "parentId", "duplicateOf"];

/** The kind a report is, from its tags: a feature tag outranks `bug`. */
export function kindFromTags(tags) {
  const norm = new Set((tags || []).map(t => String(t).trim().toLowerCase()));
  return KIND_TAGS.feature.some(t => norm.has(t)) ? "feature" : "bug";
}

// The widget serialises PNG when it fits and falls back down a JPEG ladder when
// the capture would otherwise blow the cap above — so "the screenshot" is not
// always a PNG, and on an image-heavy page it usually is not. Serving every one
// of them as image/png mislabels the common case; a consumer that sends
// `X-Content-Type-Options: nosniff` is then relying on browsers being lenient
// about image types to render its own triage queue.
//
// Sniffed rather than stored, deliberately: the bytes are the only thing every
// Store implementation is guaranteed to have kept, so this also fixes the
// screenshots already sitting in existing stores. Unrecognised bytes keep the
// historical image/png, which is what every deployment before this returned.
const MAGIC = [
  [[0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a], "image/png"],
  [[0xff, 0xd8, 0xff], "image/jpeg"],
  [[0x47, 0x49, 0x46, 0x38, 0x37, 0x61], "image/gif"],   // GIF87a
  [[0x47, 0x49, 0x46, 0x38, 0x39, 0x61], "image/gif"],   // GIF89a
];

/** The media type of a stored screenshot, from its magic bytes. */
export function screenshotContentType(data) {
  if (!data || !data.length) return "image/png";
  const b = Buffer.isBuffer(data) ? data : Buffer.from(data);
  for (const [magic, mime] of MAGIC) {
    if (b.length >= magic.length && magic.every((byte, i) => b[i] === byte)) return mime;
  }
  // WebP is RIFF-framed: "RIFF" <4-byte size> "WEBP".
  if (b.length >= 12 && b.toString("latin1", 0, 4) === "RIFF" &&
      b.toString("latin1", 8, 12) === "WEBP") return "image/webp";
  return "image/png";
}

const BUG_ID_RE = /^bug-\d{8}-\d{6}-[a-f0-9]{6}$/;

export function validBugId(id) {
  return typeof id === "string" && BUG_ID_RE.test(id);
}

function pad(n, width) {
  return String(n).padStart(width, "0");
}

export function newBugId(date = new Date()) {
  const yyyy = date.getUTCFullYear();
  const mm = pad(date.getUTCMonth() + 1, 2);
  const dd = pad(date.getUTCDate(), 2);
  const HH = pad(date.getUTCHours(), 2);
  const MM = pad(date.getUTCMinutes(), 2);
  const SS = pad(date.getUTCSeconds(), 2);
  const suffix = randomBytes(3).toString("hex");
  return `bug-${yyyy}${mm}${dd}-${HH}${MM}${SS}-${suffix}`;
}

// Monotonic, microsecond-precision ISO-8601 timestamp. Mirrors the Python
// implementation's behaviour: bumps by 1µs when the clock doesn't move,
// so a tight POST loop produces strictly increasing timestamps. This is
// what makes "sort newest-first" stable across the cross-stack contract
// test even when bugs are filed faster than the wall clock can resolve.
let lastNs = 0n;

export function nowIso() {
  let ns = BigInt(Date.now()) * 1_000_000n + processNanos();
  if (ns <= lastNs) ns = lastNs + 1000n;
  lastNs = ns;
  const ms = Number(ns / 1_000_000n);
  const us = Number((ns / 1000n) % 1_000_000n);
  const d = new Date(ms);
  const ymd = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1, 2)}-${pad(d.getUTCDate(), 2)}`;
  const hms = `${pad(d.getUTCHours(), 2)}:${pad(d.getUTCMinutes(), 2)}:${pad(d.getUTCSeconds(), 2)}`;
  return `${ymd}T${hms}.${pad(us, 6)}Z`;
}

function processNanos() {
  // hrtime.bigint() is process-relative but high-resolution. We don't use it
  // for the absolute timestamp; we use it only as a tiebreaker scaled down.
  return process.hrtime.bigint() % 1_000_000n;
}

export function sinceIso(days) {
  const d = new Date(Date.now() - days * 86400 * 1000);
  const ymd = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1, 2)}-${pad(d.getUTCDate(), 2)}`;
  const hms = `${pad(d.getUTCHours(), 2)}:${pad(d.getUTCMinutes(), 2)}:${pad(d.getUTCSeconds(), 2)}`;
  return `${ymd}T${hms}.000000Z`;
}
