import { test } from "node:test";
import assert from "node:assert/strict";
import { validBugId } from "../src/index.js";

test("an id ends in 6 or 12 lowercase hex, and nothing else (3.2.0)", () => {
  for (const n of [6, 12]) assert.equal(validBugId(`bug-20261115-101010-${"a1b2c3d4e5f6".slice(0, n)}`), true);
  for (const n of [5, 7, 11, 13]) assert.equal(validBugId(`bug-20261115-101010-${"a1b2c3d4e5f60".slice(0, n)}`), false);
  assert.equal(validBugId("bug-20261115-101010-A1B2C3"), false);
});
