// Storage interface + two reference implementations.
//
// Stores expose:
//   putBug(bug, screenshotBytes|null)  : Promise<void>
//   getBug(id)                         : Promise<BugRecord>            (throws NotFound)
//   listBugs({ status, sinceIso })     : AsyncIterable<BugRecord>
//   updateStatus(id, newStatus)        : Promise<BugRecord>
//   appendAudit(id, entry)             : Promise<void>
//   listAudit(id)                      : Promise<AuditEntry[]>
//   getScreenshot(id)                  : Promise<Buffer>               (throws NotFound)
//
// Wire-format DTOs are camelCase (matching openapi.yaml). BugRecord is the
// on-store shape; the router maps it to summary / full DTOs.

import { mkdir, readFile, readdir, writeFile, unlink, appendFile, open, rename, stat } from "node:fs/promises";
import { randomBytes } from "node:crypto";
import path from "node:path";
import { APP_PRODUCTS, KINDS, kindFromTags } from "./models.js";

export class StoreError extends Error {}
export class NotFound extends StoreError {}

// Bug record shape used internally.
export function makeBugRecord(o) {
  return {
    id: o.id,
    title: o.title,
    details: o.details,
    tags: Array.from(o.tags || []),
    status: o.status || "open",
    addedBy: o.addedBy || "web",
    actorEmail: o.actorEmail || "",
    actorName: o.actorName || "",
    addedAt: o.addedAt,
    metaUrl: o.metaUrl || "",
    metaUserAgent: o.metaUserAgent || "",
    metaBuildSha: o.metaBuildSha || "",
    screenshot: !!o.screenshot,
    transcript: o.transcript || null,
    // contract v3: null / empty on a record written before it
    app: o.app || null,
    kind: KINDS.includes(o.kind) ? o.kind : null,
    area: o.area || null,
    priority: o.priority || null,
    owner: o.owner || null,
    parentId: o.parentId || null,
    duplicateOf: o.duplicateOf || null,
    votes: Array.isArray(o.votes) ? o.votes : [],   // [{ voter, at }]
  };
}

export function toBugSummary(rec) {
  return {
    id: rec.id,
    title: rec.title,
    tags: [...rec.tags],
    status: rec.status,
    addedBy: rec.addedBy,
    actorEmail: rec.actorEmail,
    actorName: rec.actorName || "",
    addedAt: rec.addedAt,
    screenshot: !!rec.screenshot,
    kind: KINDS.includes(rec.kind) ? rec.kind : kindFromTags(rec.tags),
    app: rec.app || null,
    product: (rec.app && APP_PRODUCTS[rec.app]) || null,
    area: rec.area || null,
    priority: rec.priority || null,
    owner: rec.owner || null,
    parentId: rec.parentId || null,
    duplicateOf: rec.duplicateOf || null,
    votes: (rec.votes || []).length,
  };
}

const TRIAGE_ATTRS = ["area", "priority", "owner", "parentId", "duplicateOf"];

/** Apply triage changes / a vote to a record in place (shared by both stores). */
export function applyChanges(rec, changes) {
  for (const [k, v] of Object.entries(changes)) {
    if (!TRIAGE_ATTRS.includes(k)) throw new StoreError(`not a triage field: ${k}`);
    rec[k] = v;
  }
}
export function toggleVote(rec, voter, adding) {
  const v = String(voter).trim().toLowerCase();
  const has = rec.votes.some(x => x.voter === v);
  if (adding && !has) rec.votes.push({ voter: v, at: new Date().toISOString() });
  if (!adding && has) rec.votes = rec.votes.filter(x => x.voter !== v);
}

export function toBugFull(rec) {
  return {
    ...toBugSummary(rec),
    details: rec.details,
    metaUrl: rec.metaUrl,
    metaUserAgent: rec.metaUserAgent,
    metaBuildSha: rec.metaBuildSha,
    transcript: rec.transcript,
  };
}

// ----------------------------------------------------------------------
// InMemoryStore — tests + dev
// ----------------------------------------------------------------------

export class InMemoryStore {
  constructor() {
    this._bugs = new Map();
    this._audits = new Map();
    this._shots = new Map();
  }
  async putBug(bug, shot) {
    const rec = makeBugRecord(bug);
    if (shot && shot.length) { this._shots.set(rec.id, Buffer.from(shot)); rec.screenshot = true; }
    this._bugs.set(rec.id, rec);
  }
  async getBug(id) {
    const r = this._bugs.get(id);
    if (!r) throw new NotFound(id);
    return r;
  }
  async *listBugs({ status, sinceIso }) {
    for (const r of this._bugs.values()) {
      if (r.addedAt < sinceIso) continue;
      if (status && r.status !== status) continue;
      yield r;
    }
  }
  async updateStatus(id, newStatus) {
    const r = await this.getBug(id);
    r.status = newStatus;
    return r;
  }
  async updateFields(id, changes) {
    const r = await this.getBug(id);
    applyChanges(r, changes);
    return r;
  }
  async setVote(id, voter, adding) {
    const r = await this.getBug(id);
    toggleVote(r, voter, adding);
    return r.votes.length;
  }
  async appendAudit(id, entry) {
    if (!this._audits.has(id)) this._audits.set(id, []);
    this._audits.get(id).push(entry);
  }
  async listAudit(id) {
    return [...(this._audits.get(id) || [])];
  }
  async getScreenshot(id) {
    const s = this._shots.get(id);
    if (!s) throw new NotFound(id);
    return s;
  }
}

// ----------------------------------------------------------------------
// Atomic writes
// ----------------------------------------------------------------------

// writeFile truncates, then writes: a reader in between sees an empty or
// partial file, and a crash leaves one behind. Write a dot-prefixed ".tmp"
// beside the target (so the "bug-*.json" listing never matches it), fsync it,
// and rename it over the target. A share that refuses the rename (Azure Files
// can, while another handle holds the target open) gets the in-place write.
async function atomicWrite(file, data) {
  const tmp = path.join(path.dirname(file), `.${path.basename(file)}.${randomBytes(6).toString("hex")}.tmp`);
  try {
    const fh = await open(tmp, "w");
    try {
      await fh.writeFile(data);
      await fh.sync();
    } finally {
      await fh.close();
    }
  } catch (e) {
    await unlink(tmp).catch(() => {});
    throw e;   // the target is untouched: do NOT fall back to an in-place write
  }
  try {
    await rename(tmp, file);
  } catch (e) {
    await unlink(tmp).catch(() => {});
    console.warn(`bug store: rename refused for ${path.basename(file)} (${e.code}); writing in place`);
    await writeFile(file, data);
  }
}

const READ_ATTEMPTS = 5;
const READ_PAUSE_MS = 20;
const STALE_TMP_MS = 10 * 60 * 1000;

// Parse a JSON file, re-reading briefly if it will not parse (a writer that
// could not rename, or a report written by an older version of this store).
async function readJson(file, attempts = READ_ATTEMPTS) {
  for (let attempt = 1; ; attempt++) {
    try {
      return JSON.parse(await readFile(file, "utf8"));
    } catch (e) {
      if (e.code === "ENOENT" || attempt >= attempts) throw e;
      await new Promise(r => setTimeout(r, READ_PAUSE_MS));
    }
  }
}

// ----------------------------------------------------------------------
// FilesystemStore — single-host production
// ----------------------------------------------------------------------

export class FilesystemStore {
  // readAttempts: how often a report that will not parse is re-read (tests set 1)
  constructor(root, { readAttempts = READ_ATTEMPTS } = {}) {
    this.root = root;
    this._readAttempts = readAttempts;
    this._ready = mkdir(root, { recursive: true }).then(() => this._sweepStaleTemps());
    // Nobody may ever await this (a store built and dropped, e.g. a test whose request never reaches it):
    // mark it handled so a directory removed in the meantime is not an unhandledRejection. Callers that DO await it still see the error.
    this._ready.catch(() => {});
  }
  // A crash between creating a temp file and the rename leaves it behind.
  async _sweepStaleTemps() {
    const cutoff = Date.now() - STALE_TMP_MS;
    for (const name of await readdir(this.root).catch(() => [])) {
      if (!name.startsWith(".") || !name.endsWith(".tmp")) continue;
      const p = path.join(this.root, name);
      try { if ((await stat(p)).mtimeMs < cutoff) await unlink(p); } catch { /* raced, gone */ }
    }
  }
  _path(id, suffix) { return path.join(this.root, `${id}${suffix}`); }
  async putBug(bug, shot) {
    await this._ready;
    const rec = makeBugRecord(bug);
    if (shot && shot.length) { await atomicWrite(this._path(rec.id, ".png"), shot); rec.screenshot = true; }
    await atomicWrite(this._path(rec.id, ".json"), JSON.stringify(rec, null, 2));
  }
  async getBug(id) {
    await this._ready;
    try {
      return makeBugRecord(await readJson(this._path(id, ".json"), this._readAttempts));
    } catch (e) {
      if (e.code === "ENOENT") throw new NotFound(id);
      throw new StoreError(`could not read ${id}: ${e.message}`);
    }
  }
  async *listBugs({ status, sinceIso }) {
    await this._ready;
    const entries = await readdir(this.root);
    for (const name of entries) {
      if (!name.startsWith("bug-") || !name.endsWith(".json") || name.endsWith(".audit.jsonl")) continue;
      try {
        const rec = makeBugRecord(await readJson(path.join(this.root, name), this._readAttempts));
        if (rec.addedAt < sinceIso) continue;
        if (status && rec.status !== status) continue;
        yield rec;
      } catch { continue; }
    }
  }
  async updateStatus(id, newStatus) {
    const rec = await this.getBug(id);
    rec.status = newStatus;
    await atomicWrite(this._path(rec.id, ".json"), JSON.stringify(rec, null, 2));
    return rec;
  }
  async updateFields(id, changes) {
    const rec = await this.getBug(id);
    applyChanges(rec, changes);
    await atomicWrite(this._path(rec.id, ".json"), JSON.stringify(rec, null, 2));
    return rec;
  }
  async setVote(id, voter, adding) {
    const rec = await this.getBug(id);
    toggleVote(rec, voter, adding);
    await atomicWrite(this._path(rec.id, ".json"), JSON.stringify(rec, null, 2));
    return rec.votes.length;
  }
  async appendAudit(id, entry) {
    await appendFile(this._path(id, ".audit.jsonl"), JSON.stringify(entry) + "\n");
  }
  async listAudit(id) {
    try {
      const data = await readFile(this._path(id, ".audit.jsonl"), "utf8");
      return data.split("\n").filter(Boolean).map(line => JSON.parse(line));
    } catch (e) {
      if (e.code === "ENOENT") return [];
      throw e;
    }
  }
  async getScreenshot(id) {
    try {
      return await readFile(this._path(id, ".png"));
    } catch (e) {
      if (e.code === "ENOENT") throw new NotFound(id);
      throw e;
    }
  }
}
