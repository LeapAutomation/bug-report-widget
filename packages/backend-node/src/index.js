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

const DEFAULT_ALLOW = () => true;

// A row written before backends 2.2.0 has no name; the wire says "" then.
const withChangedByName = (a) => ({ ...a, changedByName: a.changedByName || "" });

export function createBugsRouter(opts = {}) {
  const store = opts.store;
  if (!store) throw new Error("createBugsRouter: opts.store is required");
  const isAdmin = opts.isAdmin || DEFAULT_ALLOW;
  const defaultActorEmail = opts.defaultActorEmail || "";
  const buildShaSource = opts.buildSha || "";

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

    res.status(201).json({ id: bug.id });
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
    const rows = [];
    for await (const r of store.listBugs({ status, sinceIso: since })) rows.push(toBugSummary(r));
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
    const newStatus = String(payload.status || "").trim();
    if (!BUG_STATUSES.includes(newStatus)) {
      return res.status(400).json({ error: `status must be one of ${BUG_STATUSES.join(", ")}` });
    }
    const note = String(payload.note || "").slice(0, 500);
    const changedBy = String(payload.changedBy || defaultActorEmail || "admin").slice(0, 200);
    const changedByName = String(payload.changedByName || "").trim().slice(0, 200);
    // `notify` is accepted and ignored: no mailer here, so no `notified` either.

    const oldStatus = rec.status;
    if (newStatus === oldStatus) {
      // No-op: skip audit.
      const full = toBugFull(rec);
      full.audit = (await store.listAudit(rec.id)).map(withChangedByName);
      return res.json(full);
    }

    const updated = await store.updateStatus(rec.id, newStatus);
    await store.appendAudit(rec.id, {
      changedAt: nowIso(),
      toStatus: newStatus,
      fromStatus: oldStatus,
      changedBy,
      changedByName,
      note,
    });

    const full = toBugFull(updated);
    full.audit = (await store.listAudit(rec.id)).map(withChangedByName);
    res.json(full);
  });

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
