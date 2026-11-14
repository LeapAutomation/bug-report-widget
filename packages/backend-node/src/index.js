// Express router implementing the bug-report API spec.
//
// Use:
//   import express from "express";
//   import { createBugsRouter, FilesystemStore } from "bug-report-node";
//   const app = express();
//   app.use("/api/bugs", createBugsRouter({
//     store: new FilesystemStore("/var/lib/bugs"),
//     isAdmin: req => req.header("x-admin") === "yes",
//   }));

import express from "express";
import {
  BUG_DETAILS_MAX,
  BUG_SCREENSHOT_MAX,
  BUG_STATUSES,
  APP_PRODUCTS,
  KINDS,
  PRIORITIES,
  TRIAGE_FIELDS,
  kindFromTags,
  newBugId,
  nowIso,
  screenshotContentType,
  sinceIso,
  validBugId,
} from "./models.js";
import {
  NotFound,
  toBugFull,
  toBugSummary,
} from "./store.js";

export { InMemoryStore, FilesystemStore, NotFound, StoreError } from "./store.js";
export { BUG_DETAILS_MAX, BUG_SCREENSHOT_MAX, BUG_STATUSES, newBugId, validBugId } from "./models.js";
export { APP_PRODUCTS, KINDS, PRIORITIES, TRIAGE_FIELDS, kindFromTags } from "./models.js";

const DEFAULT_ALLOW = () => true;

// A row written before backends 2.2.0 has no name; the wire says "" then.
const withChangedByName = (a) => ({ ...a, changedByName: a.changedByName || "" });

export function createBugsRouter(opts = {}) {
  const store = opts.store;
  if (!store) throw new Error("createBugsRouter: opts.store is required");
  const isAdmin = opts.isAdmin || DEFAULT_ALLOW;
  const defaultActorEmail = opts.defaultActorEmail || "";
  const buildShaSource = opts.buildSha || "";
  // Contract v3 hooks, same meaning as the Python blueprint's: appStamp(req) -> app
  // (the body's `app` is never read); actorIdentity(req) -> the checked email,
  // which is who votes. Without actorIdentity the voter is the body's actorEmail.
  const appStamp = opts.appStamp || null;
  const actorIdentity = opts.actorIdentity || null;

  function resolveBuildSha() {
    try { return typeof buildShaSource === "function" ? (buildShaSource() || "") : (buildShaSource || ""); }
    catch { return ""; }
  }

  const router = express.Router();

  // Generous JSON limit so screenshots fit. The model layer enforces the
  // real cap (5 MB) after base64-decode.
  router.use(express.json({ limit: "10mb" }));
  router.use(express.urlencoded({ extended: false, limit: "10mb" }));

  // ----- POST / ------------------------------------------------------

  router.post("/", async (req, res) => {
    const payload = req.body || {};
    const details = String(payload.details || "").trim();
    if (!details) return res.status(400).json({ error: "details is required" });
    if (Buffer.byteLength(details, "utf8") > BUG_DETAILS_MAX) {
      return res.status(413).json({ error: `details exceeds ${BUG_DETAILS_MAX} bytes` });
    }

    let title = (payload.title || details.split("\n")[0]).slice(0, 100).trim();
    let addedBy = String(payload.addedBy || "web").trim().slice(0, 32) || "web";
    const actorEmail = String(payload.actorEmail || defaultActorEmail || "").trim().slice(0, 200);
    const actorName = String(payload.actorName || "").trim().slice(0, 200);

    let tags = payload.tags || ["bug"];
    if (typeof tags === "string") tags = tags.split(",").map(s => s.trim()).filter(Boolean);
    if (!Array.isArray(tags)) tags = ["bug"];
    if (addedBy === "agent" && !tags.includes("agent-self-report")) tags.push("agent-self-report");

    let shotBytes = null;
    if (payload.screenshot && typeof payload.screenshot === "string") {
      let raw = payload.screenshot;
      if (raw.startsWith("data:")) raw = raw.includes(",") ? raw.slice(raw.indexOf(",") + 1) : "";
      if (raw) {
        try {
          shotBytes = Buffer.from(raw, "base64");
          if (shotBytes.length === 0) shotBytes = null;
        } catch { shotBytes = null; }
        if (shotBytes && shotBytes.length > BUG_SCREENSHOT_MAX) {
          return res.status(413).json({ error: `screenshot exceeds ${BUG_SCREENSHOT_MAX} bytes` });
        }
      }
    }

    let transcript = payload.transcript;
    if (transcript !== undefined && !Array.isArray(transcript)) transcript = null;

    const kind = KINDS.includes(payload.kind) ? payload.kind : kindFromTags(tags);
    let app = null;
    if (appStamp) { try { app = appStamp(req) || null; } catch { app = null; } }
    if (!(app in APP_PRODUCTS)) app = null;

    const bug = {
      id: newBugId(),
      title,
      details,
      tags,
      status: "open",
      addedBy,
      actorEmail,
      actorName,
      addedAt: nowIso(),
      metaUrl: String(payload.metaUrl || "").slice(0, 500),
      metaUserAgent: String(payload.metaUserAgent || "").slice(0, 300),
      metaBuildSha: String(payload.metaBuildSha || resolveBuildSha()).slice(0, 40),
      transcript: transcript || null,
      app,
      kind,
    };

    await store.putBug(bug, shotBytes);
    await store.appendAudit(bug.id, {
      changedAt: nowIso(),
      toStatus: "open",
      fromStatus: null,
      changedBy: actorEmail || addedBy,
      changedByName: actorName,
      note: "filed",
    });

    res.status(201).json({ id: bug.id, app });
  });

  // ----- GET / -------------------------------------------------------

  router.get("/", async (req, res) => {
    if (!isAdmin(req)) return res.status(403).json({ error: "forbidden" });
    const status = (req.query.status || "").toString().trim() || null;
    if (status && !BUG_STATUSES.includes(status)) {
      return res.status(400).json({ error: `status must be one of ${BUG_STATUSES.join(", ")}` });
    }
    let days = parseInt(req.query.days, 10);
    if (isNaN(days)) days = 30;
    days = Math.max(1, Math.min(365, days));
    const since = sinceIso(days);
    const q = (k) => (req.query[k] || "").toString().trim() || null;
    const kind = q("kind"), priority = q("priority");
    if (kind && !KINDS.includes(kind)) return res.status(400).json({ error: `kind must be one of ${KINDS.join(", ")}` });
    if (priority && !PRIORITIES.includes(priority)) return res.status(400).json({ error: `priority must be one of ${PRIORITIES.join(", ")}` });
    const rows = [];
    for await (const r of store.listBugs({ status, sinceIso: since })) {
      const s = toBugSummary(r);
      if (q("app") && s.app !== q("app")) continue;
      if (kind && s.kind !== kind) continue;
      if (q("area") && s.area !== q("area")) continue;
      if (priority && s.priority !== priority) continue;
      rows.push(s);
    }
    rows.sort((a, b) => (a.addedAt < b.addedAt ? 1 : -1));
    res.json({ bugs: rows });
  });

  // ----- GET /:id ----------------------------------------------------

  router.get("/:id", async (req, res) => {
    if (!validBugId(req.params.id)) return res.status(400).json({ error: "invalid id" });
    let rec;
    try { rec = await store.getBug(req.params.id); }
    catch (e) {
      if (e instanceof NotFound) return res.status(404).json({ error: "not found" });
      throw e;
    }
    const full = toBugFull(rec);
    full.canNotify = false;  // this backend has no mailer
    full.audit = (await store.listAudit(rec.id)).map(withChangedByName);
    res.json(full);
  });

  // ----- PATCH /:id --------------------------------------------------

  router.patch("/:id", async (req, res) => {
    if (!isAdmin(req)) return res.status(403).json({ error: "forbidden" });
    if (!validBugId(req.params.id)) return res.status(400).json({ error: "invalid id" });
    let rec;
    try { rec = await store.getBug(req.params.id); }
    catch (e) {
      if (e instanceof NotFound) return res.status(404).json({ error: "not found" });
      throw e;
    }
    const payload = req.body || {};
    let newStatus = String(payload.status || "").trim();
    const hasTriage = TRIAGE_FIELDS.some(f => f in payload);
    if (!(hasTriage && !newStatus) && !BUG_STATUSES.includes(newStatus)) {
      return res.status(400).json({ error: `status must be one of ${BUG_STATUSES.join(", ")}` });
    }
    let note = String(payload.note || "").slice(0, 500);
    const changedBy = String(payload.changedBy || defaultActorEmail || "admin").slice(0, 200);
    const changedByName = String(payload.changedByName || "").trim().slice(0, 200);
    // `notify` is accepted and ignored: no mailer here, so no `notified` either.

    // triage fields: the same validation as the Python blueprint
    const changes = {};
    for (const wire of TRIAGE_FIELDS) {
      if (!(wire in payload)) continue;
      const raw = payload[wire];
      const val = (raw === null || raw === undefined ? "" : String(raw).trim()) || null;
      if (wire === "area" && val && val.length > 100) return res.status(400).json({ error: "area is at most 100 characters" });
      if (wire === "priority" && val && !PRIORITIES.includes(val)) return res.status(400).json({ error: `priority must be one of ${PRIORITIES.join(", ")}` });
      if (wire === "owner" && val && (!val.includes("@") || val.length > 200)) return res.status(400).json({ error: "owner must be an email address" });
      if ((wire === "parentId" || wire === "duplicateOf") && val) {
        if (!validBugId(val)) return res.status(400).json({ error: `${wire} must be a report id` });
        if (val === rec.id) return res.status(400).json({ error: `${wire} cannot be the report itself` });
        try { await store.getBug(val); }
        catch (e) { if (e instanceof NotFound) return res.status(404).json({ error: `${wire}: no such report` }); throw e; }
      }
      if (wire === "parentId" && val) {
        const seen = new Set();
        for (let up = val; up && !seen.has(up);) {
          if (up === rec.id) return res.status(400).json({ error: "parentId would make a cycle" });
          seen.add(up);
          try { up = (await store.getBug(up)).parentId; } catch { break; }
        }
      }
      if ((rec[wire] || null) !== val) changes[wire] = val;
    }
    if (Object.keys(changes).length && !store.updateFields) {
      return res.status(501).json({ error: "this store does not keep triage fields" });
    }
    const oldStatus = rec.status;
    if (changes.duplicateOf) {
      if (newStatus && newStatus !== "declined") return res.status(400).json({ error: "duplicateOf declines the report; status must be declined or omitted" });
      newStatus = "declined";
      note = `duplicate of ${changes.duplicateOf}` + (note.trim() ? `: ${note}` : "");
      if (oldStatus !== "declined" && !["open", "triaged"].includes(oldStatus)) {
        return res.status(409).json({ error: `cannot transition ${oldStatus} -> declined` });
      }
    } else if (!newStatus) {
      newStatus = oldStatus;
    }

    if (newStatus === oldStatus && !Object.keys(changes).length) {
      // No-op: skip audit.
      const full = toBugFull(rec);
      full.audit = (await store.listAudit(rec.id)).map(withChangedByName);
      return res.json(full);
    }

    const shown = (v) => (v === null || v === undefined ? "none" : String(v));
    const declineRow = newStatus !== oldStatus;
    const described = Object.entries(changes)
      .filter(([k]) => !(declineRow && k === "duplicateOf"))
      .map(([k, v]) => `${k}: ${shown(rec[k])} -> ${shown(v)}`).join("; ").slice(0, 500);

    let updated = rec;
    if (Object.keys(changes).length) updated = await store.updateFields(rec.id, changes);
    if (declineRow) {
      updated = await store.updateStatus(rec.id, newStatus);
      await store.appendAudit(rec.id, {
        changedAt: nowIso(), toStatus: newStatus, fromStatus: oldStatus, changedBy, changedByName, note,
      });
    }
    if (described) {
      await store.appendAudit(rec.id, {
        changedAt: nowIso(), toStatus: updated.status, fromStatus: updated.status, changedBy, changedByName,
        note: described,
      });
    }

    const full = toBugFull(updated);
    full.audit = (await store.listAudit(rec.id)).map(withChangedByName);
    res.json(full);
  });

  // ----- POST / DELETE /:id/vote -------------------------------------

  async function vote(req, res, adding) {
    if (!validBugId(req.params.id)) return res.status(400).json({ error: "invalid id" });
    let voter = "";
    if (actorIdentity) { try { voter = String(actorIdentity(req) || "").trim(); } catch { voter = ""; } }
    else voter = String((req.body || {}).actorEmail || req.query.actorEmail || defaultActorEmail || "").trim();
    if (!voter) return res.status(401).json({ error: "sign in to vote" });
    if (!store.setVote) return res.status(501).json({ error: "this store does not keep votes" });
    try {
      const votes = await store.setVote(req.params.id, voter, adding);
      res.json({ votes, voted: adding });
    } catch (e) {
      if (e instanceof NotFound) return res.status(404).json({ error: "not found" });
      throw e;
    }
  }
  router.post("/:id/vote", (req, res) => vote(req, res, true));
  router.delete("/:id/vote", (req, res) => vote(req, res, false));

  // ----- GET /:id/screenshot -----------------------------------------

  router.get("/:id/screenshot", async (req, res) => {
    if (!validBugId(req.params.id)) return res.sendStatus(400);
    try {
      const shot = await store.getScreenshot(req.params.id);
      res.type(screenshotContentType(shot)).send(shot);
    } catch (e) {
      if (e instanceof NotFound) return res.sendStatus(404);
      throw e;
    }
  });

  return router;
}
