// The Node mirror of report-contract.json v3 agrees with the spec (practice T5):
// a constant that drifts here is a backend that answers differently from Python.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { APP_PRODUCTS, BUG_STATUSES, KINDS, KIND_TAGS, PRIORITIES, TRIAGE_FIELDS, kindFromTags } from "../src/models.js";

const SPEC = JSON.parse(readFileSync(new URL("../../spec/report-contract.json", import.meta.url), "utf8"));

test("the mirror matches report-contract.json", () => {
  assert.equal(SPEC.version, 3);
  assert.deepEqual(BUG_STATUSES, SPEC.statuses);
  assert.deepEqual(KINDS, SPEC.kinds);
  assert.deepEqual(KIND_TAGS, SPEC.kindTags);
  assert.deepEqual(PRIORITIES, SPEC.priorities);
  assert.deepEqual(APP_PRODUCTS, SPEC.appProducts);
  assert.deepEqual(TRIAGE_FIELDS, SPEC.triageFields);
});

test("kind is derived from tags as in the contract", () => {
  assert.equal(kindFromTags([]), "bug");
  assert.equal(kindFromTags(["bug", "feature-request"]), "feature");
  assert.equal(kindFromTags(["capability-request"]), "feature");
});
