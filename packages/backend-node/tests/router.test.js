// Tests for the Express router + storage interface.
//
// Parametrized over both reference stores. Same 18 scenarios run twice.

import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import express from "express";
import { createBugsRouter, InMemoryStore, FilesystemStore } from "../src/index.js";

// 1x1 transparent PNG.
const TINY_PNG = Buffer.from(
  "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4890000000D" +
  "49444154789C6300010000000500010D0A2DB40000000049454E44AE426082",
  "hex"
);
const TINY_PNG_B64 = TINY_PNG.toString("base64");

// Smallest thing that sniffs as a JPEG: SOI + APP0. The widget emits these
// whenever a PNG capture would exceed the size cap, which on an image-heavy
// page is the normal path, not the exotic one.
const TINY_JPEG = Buffer.from("FFD8FFE000104A46494600010100000100010000FFD9", "hex");
const TINY_JPEG_B64 = TINY_JPEG.toString("base64");

const STORES = [
  ["memory", () => ({ store: new InMemoryStore(), cleanup: () => {} })],
  ["filesystem", () => {
    const dir = mkdtempSync(path.join(tmpdir(), "bug-report-fs-"));
    return { store: new FilesystemStore(dir), cleanup: () => rmSync(dir, { recursive: true, force: true }) };
  }],
];

function makeApp({ store, isAdmin }) {
  const app = express();
  app.use("/bugs", createBugsRouter({ store, isAdmin }));
  return app;
}

async function startServer(app) {
  return new Promise((resolve) => {
    const server = app.listen(0, "127.0.0.1", () => resolve(server));
  });
}

function url(server, path) {
  const { port } = server.address();
  return `http://127.0.0.1:${port}${path}`;
}

async function withApp(makeStore, isAdmin, fn) {
  const { store, cleanup } = makeStore();
  const app = makeApp({ store, isAdmin });
  const server = await startServer(app);
  try {
    await fn({ server, store });
  } finally {
    await new Promise(r => server.close(r));
    cleanup();
  }
}

// ---------------------------------------------------------------------
// Parametrized contract suite
// ---------------------------------------------------------------------

for (const [name, makeStore] of STORES) {
  test(`[${name}] POST minimal returns id + 201`, async () => {
    await withApp(makeStore, null, async ({ server }) => {
      const r = await fetch(url(server, "/bugs"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ details: "the kill button stopped working" }),
      });
      assert.equal(r.status, 201);
      const { id } = await r.json();
      assert.match(id, /^bug-\d{8}-\d{6}-[a-f0-9]{6}$/);
    });
  });

  test(`[${name}] POST without details => 400`, async () => {
    await withApp(makeStore, null, async ({ server }) => {
      const r = await fetch(url(server, "/bugs"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      assert.equal(r.status, 400);
    });
  });

  test(`[${name}] POST oversized details => 413`, async () => {
    await withApp(makeStore, null, async ({ server }) => {
      const huge = "x".repeat(10 * 1024 + 1);
      const r = await fetch(url(server, "/bugs"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ details: huge }),
      });
      assert.equal(r.status, 413);
    });
  });

  test(`[${name}] addedBy=agent auto-tags agent-self-report`, async () => {
    await withApp(makeStore, null, async ({ server }) => {
      const r = await fetch(url(server, "/bugs"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ details: "I noticed an issue", addedBy: "agent", tags: ["bug", "investigate"] }),
      });
      const { id } = await r.json();
      const got = await (await fetch(url(server, `/bugs/${id}`))).json();
      assert.ok(got.tags.includes("agent-self-report"));
      assert.ok(got.tags.includes("investigate"));
    });
  });

  test(`[${name}] GET / lists newest first`, async () => {
    await withApp(makeStore, null, async ({ server }) => {
      const ids = [];
      for (const i of [0, 1, 2]) {
        const r = await fetch(url(server, "/bugs"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ details: `b${i}` }),
        });
        ids.push((await r.json()).id);
      }
      const { bugs } = await (await fetch(url(server, "/bugs"))).json();
      assert.deepEqual(bugs.map(b => b.id), [...ids].reverse());
    });
  });

  test(`[${name}] GET / filters by status`, async () => {
    await withApp(makeStore, null, async ({ server }) => {
      const a = (await (await fetch(url(server, "/bugs"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ details: "A" }),
      })).json()).id;
      const b = (await (await fetch(url(server, "/bugs"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ details: "B" }),
      })).json()).id;
      await fetch(url(server, `/bugs/${a}`), {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "triaged" }),
      });
      const open = await (await fetch(url(server, "/bugs?status=open"))).json();
      assert.deepEqual(open.bugs.map(x => x.id), [b]);
    });
  });

  test(`[${name}] GET single returns audit`, async () => {
    await withApp(makeStore, null, async ({ server }) => {
      const id = (await (await fetch(url(server, "/bugs"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ details: "x" }),
      })).json()).id;
      const rec = await (await fetch(url(server, `/bugs/${id}`))).json();
      assert.equal(rec.id, id);
      assert.ok(Array.isArray(rec.audit) && rec.audit.length === 1);
      assert.equal(rec.audit[0].fromStatus, null);
      assert.equal(rec.audit[0].toStatus, "open");
    });
  });

  test(`[${name}] GET invalid id => 400`, async () => {
    await withApp(makeStore, null, async ({ server }) => {
      const r = await fetch(url(server, "/bugs/not-a-bug"));
      assert.equal(r.status, 400);
    });
  });

  test(`[${name}] GET unknown valid id => 404`, async () => {
    await withApp(makeStore, null, async ({ server }) => {
      const r = await fetch(url(server, "/bugs/bug-20260101-000000-aaaaaa"));
      assert.equal(r.status, 404);
    });
  });

  test(`[${name}] PATCH transitions and writes audit`, async () => {
    await withApp(makeStore, null, async ({ server }) => {
      const id = (await (await fetch(url(server, "/bugs"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ details: "to triage" }),
      })).json()).id;
      const r = await fetch(url(server, `/bugs/${id}`), {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "triaged", note: "looking" }),
      });
      assert.equal(r.status, 200);
      const updated = await r.json();
      assert.equal(updated.status, "triaged");
      const audit = (await (await fetch(url(server, `/bugs/${id}`))).json()).audit;
      assert.equal(audit.length, 2);
      assert.equal(audit[audit.length - 1].fromStatus, "open");
      assert.equal(audit[audit.length - 1].toStatus, "triaged");
      assert.equal(audit[audit.length - 1].note, "looking");
    });
  });

  test(`[${name}] PATCH same status is a no-op (no audit row)`, async () => {
    await withApp(makeStore, null, async ({ server }) => {
      const id = (await (await fetch(url(server, "/bugs"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ details: "x" }),
      })).json()).id;
      await fetch(url(server, `/bugs/${id}`), {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "open" }),
      });
      const audit = (await (await fetch(url(server, `/bugs/${id}`))).json()).audit;
      assert.equal(audit.length, 1, "no-op PATCH must not add an audit row");
    });
  });

  test(`[${name}] PATCH invalid status => 400`, async () => {
    await withApp(makeStore, null, async ({ server }) => {
      const id = (await (await fetch(url(server, "/bugs"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ details: "x" }),
      })).json()).id;
      const r = await fetch(url(server, `/bugs/${id}`), {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "wat" }),
      });
      assert.equal(r.status, 400);
    });
  });

  test(`[${name}] POST with screenshot persists bytes`, async () => {
    await withApp(makeStore, null, async ({ server }) => {
      const id = (await (await fetch(url(server, "/bugs"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ details: "with shot", screenshot: `data:image/png;base64,${TINY_PNG_B64}` }),
      })).json()).id;
      const r = await fetch(url(server, `/bugs/${id}/screenshot`));
      assert.equal(r.status, 200);
      assert.equal(r.headers.get("content-type")?.split(";")[0], "image/png");
      const bytes = Buffer.from(await r.arrayBuffer());
      assert.deepEqual(bytes, TINY_PNG);
    });
  });

  test(`[${name}] a JPEG screenshot is served as JPEG, not PNG`, async () => {
    // The widget's fallback ladder emits JPEG whenever the PNG would exceed
    // BUG_SCREENSHOT_MAX — the ordinary case for any page with photographs on
    // it. Serving it as image/png leaves a consumer that sends
    // `X-Content-Type-Options: nosniff` depending on browser leniency to
    // render its own triage queue.
    await withApp(makeStore, null, async ({ server }) => {
      const id = (await (await fetch(url(server, "/bugs"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ details: "jpeg shot", screenshot: `data:image/jpeg;base64,${TINY_JPEG_B64}` }),
      })).json()).id;
      const r = await fetch(url(server, `/bugs/${id}/screenshot`));
      assert.equal(r.status, 200);
      assert.equal(r.headers.get("content-type")?.split(";")[0], "image/jpeg");
      assert.deepEqual(Buffer.from(await r.arrayBuffer()), TINY_JPEG);
    });
  });

  test(`[${name}] the type is sniffed, not taken from the data: URL`, async () => {
    // The data: URL's declared type is the CLIENT's claim about bytes we are
    // about to store. The response must describe what was actually stored.
    await withApp(makeStore, null, async ({ server }) => {
      const id = (await (await fetch(url(server, "/bugs"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ details: "lying data url", screenshot: `data:image/png;base64,${TINY_JPEG_B64}` }),
      })).json()).id;
      const r = await fetch(url(server, `/bugs/${id}/screenshot`));
      assert.equal(r.headers.get("content-type")?.split(";")[0], "image/jpeg");
    });
  });

  test(`[${name}] screenshot 404 when missing`, async () => {
    await withApp(makeStore, null, async ({ server }) => {
      const id = (await (await fetch(url(server, "/bugs"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ details: "no shot" }),
      })).json()).id;
      const r = await fetch(url(server, `/bugs/${id}/screenshot`));
      assert.equal(r.status, 404);
    });
  });

  test(`[${name}] isAdmin gate blocks list + patch but not POST`, async () => {
    await withApp(makeStore, () => false, async ({ server }) => {
      const r = await fetch(url(server, "/bugs"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ details: "anyone can file" }),
      });
      assert.equal(r.status, 201);
      const id = (await r.json()).id;
      assert.equal((await fetch(url(server, "/bugs"))).status, 403);
      assert.equal((await fetch(url(server, `/bugs/${id}`), {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "triaged" }),
      })).status, 403);
    });
  });
}

// ---------------------------------------------------------------------
// Names (backends 2.2.0): passed through; no mailer, so notify is ignored
// ---------------------------------------------------------------------

for (const [name, makeStore] of STORES) {
  test(`[${name}] names are stored and served; notify is ignored (no notified, canNotify false)`, async () => {
    await withApp(makeStore, null, async ({ server }) => {
      const json = { "Content-Type": "application/json" };
      const id = (await (await fetch(url(server, "/bugs"), {
        method: "POST", headers: json,
        body: JSON.stringify({ details: "d", actorEmail: "ana@example.com", actorName: "Ana Reporter" }),
      })).json()).id;
      const patched = await (await fetch(url(server, `/bugs/${id}`), {
        method: "PATCH", headers: json,
        body: JSON.stringify({ status: "declined", note: "no", notify: true, changedBy: "sam@example.com", changedByName: "Sam Closer" }),
      })).json();
      assert.equal("notified" in patched, false);
      const got = await (await fetch(url(server, `/bugs/${id}`))).json();
      assert.equal(got.actorName, "Ana Reporter");
      assert.equal(got.canNotify, false);
      assert.equal(got.audit[0].changedByName, "Ana Reporter");
      assert.deepEqual([got.audit[1].changedBy, got.audit[1].changedByName], ["sam@example.com", "Sam Closer"]);
      const list = await (await fetch(url(server, "/bugs"))).json();
      assert.equal(list.bugs[0].actorName, "Ana Reporter");
    });
  });

  test(`[${name}] a report filed without a name says "" and its audit rows say ""`, async () => {
    await withApp(makeStore, null, async ({ server }) => {
      const id = (await (await fetch(url(server, "/bugs"), {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ details: "d" }),
      })).json()).id;
      const got = await (await fetch(url(server, `/bugs/${id}`))).json();
      assert.equal(got.actorName, "");
      assert.equal(got.audit[0].changedByName, "");
    });
  });
}

// ---------------------------------------------------------------------
// Single-store sanity tests
// ---------------------------------------------------------------------

test("createBugsRouter without a store throws", () => {
  assert.throws(() => createBugsRouter({}), /store is required/);
});
