// AzureTablesStore — Store implementation backed by Azure Tables + Blob.
//
// Ported from leap-timesheet's api/bugs/store.js. Same on-disk schema, so
// leap can adopt it later without data migration. Other Azure-hosted Node
// apps can plug it in immediately.
//
// On-disk schema (lift-and-shift compatible with leap):
//   Table BugReports
//     partitionKey = yyyy-mm-dd (derived from addedAt UTC)
//     rowKey       = bug-id
//     Title, Details, Tags (CSV), Status, AddedBy, AddedAt,
//     ActorEmail, MetaUrl, MetaUserAgent, MetaBuildSha, ScreenshotBlobPath,
//     LastTransitionAt?, LastTransitionBy?
//   Table BugReportsAudit
//     partitionKey = bug-id
//     rowKey       = ISO timestamp with `:`/`.` swapped + `-` + toStatus|"create"
//     BugId, FromStatus, ToStatus, ChangedBy, ChangedAt, Note?, Action
//   Blob container `screenshots`
//     One object per bug at path `{bug-id}.png`.
//
// Peer dependencies: @azure/data-tables, @azure/storage-blob. The package
// declares them as optional so consumers who don't need this store don't
// pay for them.

import { NotFound, StoreError, makeBugRecord } from "../store.js";

const DEFAULT_TABLE = "BugReports";
const DEFAULT_AUDIT_TABLE = "BugReportsAudit";
const DEFAULT_BLOB_CONTAINER = "screenshots";

// Lazy-load to keep startup cheap and allow consumers without Azure to
// import the package's main entry without paying for the SDKs.
let _dataTablesMod = null;
let _storageBlobMod = null;
async function _loadAzure() {
  if (!_dataTablesMod) _dataTablesMod = await import("@azure/data-tables");
  if (!_storageBlobMod) _storageBlobMod = await import("@azure/storage-blob");
  return { tables: _dataTablesMod, blob: _storageBlobMod };
}

// ── Helpers ─────────────────────────────────────────────────────────

function partitionKeyForAddedAt(addedAtIso) {
  // First 10 chars of an ISO date is yyyy-mm-dd. Robust to micro/nanosecond
  // suffixes the model layer now adds.
  return (addedAtIso || "").slice(0, 10);
}

function tagsToCsv(tags) {
  if (!Array.isArray(tags)) return "bug";
  const seen = new Set();
  const out = [];
  for (const t of tags) {
    if (typeof t !== "string") continue;
    const v = t.toLowerCase().replace(/[^a-z0-9-]/g, "").slice(0, 32);
    if (v && !seen.has(v)) { seen.add(v); out.push(v); }
  }
  if (!seen.has("bug")) out.unshift("bug");
  return out.join(",");
}

function csvToTags(csv) {
  return typeof csv === "string" ? csv.split(",").filter(Boolean) : [];
}

function entityToBugRecord(e) {
  return makeBugRecord({
    id: e.rowKey,
    title: e.Title || "",
    details: e.Details || "",
    tags: csvToTags(e.Tags),
    status: e.Status || "open",
    addedBy: e.AddedBy || "web",
    actorEmail: e.ActorEmail || "",
    actorName: e.ActorName || "",
    addedAt: e.AddedAt || "",
    metaUrl: e.MetaUrl || "",
    metaUserAgent: e.MetaUserAgent || "",
    metaBuildSha: e.MetaBuildSha || "",
    screenshot: !!e.ScreenshotBlobPath,
    transcript: null,  // leap's schema doesn't store transcripts
  });
}

function bugRecordToEntity(rec) {
  return {
    partitionKey: partitionKeyForAddedAt(rec.addedAt),
    rowKey: rec.id,
    Title: rec.title,
    Details: rec.details,
    Tags: tagsToCsv(rec.tags),
    Status: rec.status,
    AddedBy: rec.addedBy,
    AddedAt: rec.addedAt,
    ActorEmail: rec.actorEmail || "",
    ActorName: rec.actorName || "",
    MetaUrl: rec.metaUrl || "",
    MetaUserAgent: rec.metaUserAgent || "",
    MetaBuildSha: rec.metaBuildSha || "",
    ScreenshotBlobPath: rec.screenshot ? `${rec.id}.png` : "",
  };
}

function escapeOData(s) {
  // Per the OData spec, single quotes are doubled inside string literals.
  // The previous leap implementation used .replace(/'/g, "") which stripped
  // them — that was the bug fixed in leap PR #20.
  return String(s || "").replace(/'/g, "''");
}

function auditRowKey(toStatus, isoNow) {
  const safe = (isoNow || new Date().toISOString()).replace(/[:.]/g, "-");
  return `${safe}-${toStatus || "noop"}`;
}

// ── AzureTablesStore ────────────────────────────────────────────────

export class AzureTablesStore {
  /**
   * Construct with either:
   *   new AzureTablesStore({ connectionString, ... })
   *   new AzureTablesStore({ tableClient, auditTableClient, blobContainer, ... })  // for tests
   *
   * Options:
   *   table         (default 'BugReports')
   *   auditTable    (default 'BugReportsAudit')
   *   blobContainer (default 'screenshots')
   *   sasTtlSec     (default 300)  — only used if you call getScreenshotUrl()
   */
  constructor(opts = {}) {
    this.opts = {
      table: opts.table || DEFAULT_TABLE,
      auditTable: opts.auditTable || DEFAULT_AUDIT_TABLE,
      blobContainer: opts.blobContainer || DEFAULT_BLOB_CONTAINER,
      sasTtlSec: opts.sasTtlSec || 300,
    };
    this._connStr = opts.connectionString || null;
    this._tableClient = opts.tableClient || null;
    this._auditClient = opts.auditTableClient || null;
    this._blobContainer = opts.blobContainer instanceof Object ? opts.blobContainer : null;
    this._injected = !!(opts.tableClient || opts.auditTableClient || opts.blobContainer instanceof Object);
  }

  async _tables() {
    if (this._tableClient && this._auditClient) {
      return { primary: this._tableClient, audit: this._auditClient };
    }
    if (!this._connStr) throw new StoreError("AzureTablesStore requires a connectionString");
    const { tables: az } = await _loadAzure();
    this._tableClient = az.TableClient.fromConnectionString(this._connStr, this.opts.table);
    this._auditClient = az.TableClient.fromConnectionString(this._connStr, this.opts.auditTable);
    return { primary: this._tableClient, audit: this._auditClient };
  }

  async _blobs() {
    if (this._blobContainer && typeof this._blobContainer.getBlockBlobClient === "function") {
      return this._blobContainer;
    }
    if (!this._connStr) throw new StoreError("AzureTablesStore requires a connectionString for screenshots");
    const { blob: az } = await _loadAzure();
    const svc = az.BlobServiceClient.fromConnectionString(this._connStr);
    this._blobContainer = svc.getContainerClient(this.opts.blobContainer);
    return this._blobContainer;
  }

  // ── Store protocol ────────────────────────────────────────────────

  async putBug(bug, shotBytes) {
    const rec = makeBugRecord(bug);
    if (shotBytes && shotBytes.length) rec.screenshot = true;
    const entity = bugRecordToEntity(rec);

    if (shotBytes && shotBytes.length) {
      const container = await this._blobs();
      if (typeof container.createIfNotExists === "function") {
        await container.createIfNotExists().catch(() => {});
      }
      const blob = container.getBlockBlobClient(`${rec.id}.png`);
      await blob.uploadData(shotBytes, {
        blobHTTPHeaders: { blobContentType: "image/png" },
      });
    }

    const { primary } = await this._tables();
    if (typeof primary.createTable === "function") {
      await primary.createTable().catch(() => {});
    }
    await primary.createEntity(entity);
  }

  async getBug(id) {
    const { primary } = await this._tables();
    // RowKey is unique. We don't index PK separately; scan within the recent
    // window via filter. Same approach leap uses.
    try {
      const iter = primary.listEntities({
        queryOptions: { filter: `RowKey eq '${escapeOData(id)}'` },
      });
      for await (const e of iter) return entityToBugRecord(e);
    } catch (err) {
      if (err && err.statusCode === 404) throw new NotFound(id);
      throw err;
    }
    throw new NotFound(id);
  }

  async *listBugs({ status, sinceIso }) {
    const { primary } = await this._tables();
    const cutoffPK = (sinceIso || "").slice(0, 10);
    const parts = [];
    if (cutoffPK) parts.push(`PartitionKey ge '${escapeOData(cutoffPK)}'`);
    if (status) parts.push(`Status eq '${escapeOData(status)}'`);
    const filter = parts.length ? parts.join(" and ") : undefined;

    try {
      const iter = primary.listEntities(filter ? { queryOptions: { filter } } : undefined);
      for await (const e of iter) yield entityToBugRecord(e);
    } catch (err) {
      if (err && err.statusCode === 404) return;
      throw err;
    }
  }

  async updateStatus(id, newStatus) {
    const { primary } = await this._tables();
    // Read the row, write it back with the new Status. updateEntity needs
    // the partition key, which we get from getBug's first hit.
    const rec = await this.getBug(id);
    const entity = bugRecordToEntity(rec);
    entity.Status = newStatus;
    entity.LastTransitionAt = new Date().toISOString();
    await primary.updateEntity(entity, "Replace");
    rec.status = newStatus;
    return rec;
  }

  async appendAudit(id, entry) {
    const { audit } = await this._tables();
    if (typeof audit.createTable === "function") {
      await audit.createTable().catch(() => {});
    }
    await audit.createEntity({
      partitionKey: id,
      rowKey: auditRowKey(entry.toStatus, entry.changedAt),
      BugId: id,
      FromStatus: entry.fromStatus || "",
      ToStatus: entry.toStatus || "",
      ChangedBy: entry.changedBy || "",
      ChangedByName: entry.changedByName || "",
      ChangedAt: entry.changedAt || new Date().toISOString(),
      Note: entry.note || "",
      Action: entry.fromStatus === null || entry.fromStatus === "" ? "create" : "transition",
    });
  }

  async listAudit(id) {
    const { audit } = await this._tables();
    const rows = [];
    try {
      const iter = audit.listEntities({
        queryOptions: { filter: `PartitionKey eq '${escapeOData(id)}'` },
      });
      for await (const e of iter) {
        rows.push({
          changedAt: e.ChangedAt || "",
          fromStatus: e.FromStatus || null,
          toStatus: e.ToStatus || "",
          changedBy: e.ChangedBy || "",
          changedByName: e.ChangedByName || "",
          note: e.Note || "",
        });
      }
    } catch (err) {
      if (err && err.statusCode === 404) return [];
      throw err;
    }
    rows.sort((a, b) => (a.changedAt < b.changedAt ? -1 : 1));
    return rows;
  }

  async getScreenshot(id) {
    const container = await this._blobs();
    const blob = container.getBlockBlobClient(`${id}.png`);
    try {
      const buf = await blob.downloadToBuffer();
      return buf;
    } catch (err) {
      if (err && (err.statusCode === 404 || err.code === "BlobNotFound")) {
        throw new NotFound(id);
      }
      throw err;
    }
  }

  /**
   * Optional: signed-URL helper for consumers that prefer a 302 redirect
   * over downloading bytes through the Node process. Returns a short-TTL
   * SAS URL the browser can fetch directly. The router currently doesn't
   * use this (it always calls getScreenshot), but apps can wire it in
   * themselves if Azure egress is a concern.
   */
  async getScreenshotUrl(id) {
    const container = await this._blobs();
    const blob = container.getBlockBlobClient(`${id}.png`);
    if (typeof blob.generateSasUrl !== "function") {
      throw new StoreError("Blob client doesn't support generateSasUrl");
    }
    const { blob: blobMod } = await _loadAzure();
    return blob.generateSasUrl({
      permissions: blobMod.BlobSASPermissions.parse("r"),
      expiresOn: new Date(Date.now() + this.opts.sasTtlSec * 1000),
    });
  }
}

// Re-export for convenience.
export { tagsToCsv, csvToTags, partitionKeyForAddedAt, escapeOData };
