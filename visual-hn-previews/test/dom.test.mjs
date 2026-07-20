// Lightweight unit tests for the DOM-free helpers in src/dom.js — the core of
// the one-thumbnail-per-story fix. No jsdom needed: we eval dom.js against a
// minimal `window` shim and exercise the pure functions.
//
// Run: node --test test/dom.test.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "src", "dom.js"), "utf8");

// Eval dom.js in a sandbox with a fake window/document. document is only
// referenced inside findRows' default arg, which these tests don't trigger.
const sandbox = { window: {}, document: {} };
vm.runInNewContext(src, sandbox);
const VHN = sandbox.window.VHN;

test("idFromHref extracts the HN item id", () => {
  assert.equal(
    VHN.idFromHref("https://news.ycombinator.com/item?id=12345"),
    12345
  );
  assert.equal(VHN.idFromHref("https://example.com/x"), null);
  assert.equal(VHN.idFromHref(""), null);
  assert.equal(VHN.idFromHref(null), null);
});

test("dedupeById keeps exactly one entry per story id (first wins)", () => {
  // Simulates a single hckr story exposing THREE item?id anchors (metrics,
  // score, comments), each resolving to a different container element.
  const a = { tag: "metrics" };
  const b = { tag: "meta" };
  const c = { tag: "comments" };
  const candidates = [
    { id: 100, row: a, anchor: {} },
    { id: 100, row: b, anchor: {} },
    { id: 100, row: c, anchor: {} },
    { id: 200, row: {}, anchor: {} },
  ];
  const out = VHN.dedupeById(candidates);
  assert.equal(out.length, 2);
  assert.equal(out[0].id, 100);
  assert.equal(out[0].row, a); // first occurrence wins
  assert.equal(out[1].id, 200);
});

test("dedupeById skips null / id-less candidates", () => {
  const out = VHN.dedupeById([null, { id: null }, { id: 5, row: {} }]);
  assert.equal(out.length, 1);
  assert.equal(out[0].id, 5);
});
