/* Regression test for v1.3.1-hotfix2 Fix E (lazy IPC bridge resolution)
 * and Fix F (self-diagnosing bridge failure), desktop/ui/wizard-bridge.js.
 *
 * Run: node desktop/ui/tests/wizard-bridge.test.mjs   (Node >= 18, no deps)
 *
 * Each scenario builds a fresh vm sandbox that mimics the Tauri WebKitGTK
 * environment (window / navigator / setTimeout injected; Date is native).
 * The late-injection scenario reproduces the actual production race:
 * window.__TAURI__ appears AFTER wizard-bridge.js has executed.
 */
"use strict";

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const BRIDGE_SRC = fs.readFileSync(
  path.join(__dirname, "..", "wizard-bridge.js"), "utf8");

function freshContext({ injectAfterMs = null, tauriFactory = null } = {}) {
  const sandbox = { console, result: undefined };
  sandbox.window = sandbox; // window === globalThis-of-sandbox
  sandbox.navigator = { userAgent: "wizard-bridge.test/1.0 (node vm)" };
  sandbox.setTimeout = setTimeout;
  sandbox.clearTimeout = clearTimeout;
  if (tauriFactory) {
    if (injectAfterMs === null) {
      Object.assign(sandbox, tauriFactory());
    } else {
      setTimeout(() => Object.assign(sandbox, tauriFactory()), injectAfterMs);
    }
  }
  vm.createContext(sandbox);
  vm.runInContext(BRIDGE_SRC, sandbox, { filename: "wizard-bridge.js" });
  return sandbox;
}

const tests = [];
function test(name, fn) { tests.push({ name, fn }); }

/* 1 — Tauri v2 surface, present immediately */
test("v2 bridge: invoke/runStep/cancel + flavor", async () => {
  const sb = freshContext({ tauriFactory: () => ({
    __TAURI__: {
      core: { invoke: async (cmd, args) =>
        cmd === "bootstrap_cancel" ? true : JSON.stringify({ cmd, args, ok: true }) },
      event: { listen: () => {} },
    },
  }) });
  const B = sb.window.WizardBridge;
  assert.equal(B.bridgeFlavor, "v2");
  assert.equal(B.isTauri, true);
  const raw = await B.invoke("setup_state", { a: 1 });
  assert.deepEqual(JSON.parse(raw), { cmd: "setup_state", args: { a: 1 }, ok: true });
  const res = await B.runStep("report", []);
  assert.equal(res.ok, true);
  assert.equal(await B.cancelStep(), true);
});

/* 2 — Tauri v1 legacy surface (__TAURI__.invoke, no .core) */
test("v1 bridge: legacy invoke resolves", async () => {
  const sb = freshContext({ tauriFactory: () => ({
    __TAURI__: { invoke: async (cmd) => "v1:" + cmd },
  }) });
  const B = sb.window.WizardBridge;
  assert.equal(B.bridgeFlavor, "v1");
  assert.equal(await B.invoke("setup_state"), "v1:setup_state");
});

/* 3 — raw internals fallback (__TAURI_INTERNALS__.invoke) */
test("internals bridge: last-resort invoke", async () => {
  const sb = freshContext({ tauriFactory: () => ({
    __TAURI_INTERNALS__: { invoke: async (cmd) =>
      cmd === "bootstrap_cancel" ? true : JSON.stringify({ t: "result", ok: true, cmd }) },
  }) });
  const B = sb.window.WizardBridge;
  assert.equal(B.bridgeFlavor, "internals");
  assert.equal(B.isTauri, true);
  const res = await B.runStep("report", []);
  assert.equal(res.ok, true);
  assert.equal(await B.cancelStep(), true);
});

/* 4 — THE PRODUCTION RACE: bridge injected late (WebKitGTK), resolver polls */
test("late injection (<200ms) succeeds via polling", async () => {
  const sb = freshContext({
    injectAfterMs: 150,
    tauriFactory: () => ({
      __TAURI__: { core: { invoke: async (cmd) => JSON.stringify({ t: "result", ok: true, cmd }) } },
    }),
  });
  const B = sb.window.WizardBridge;
  assert.equal(B.bridgeFlavor, null); // not injected yet at parse time
  const t0 = Date.now();
  const raw = await B.invoke("setup_state");
  const elapsed = Date.now() - t0;
  assert.equal(JSON.parse(raw).ok, true);
  assert.ok(elapsed < 1900, `polling resolved late injection, took ${elapsed}ms`);
  assert.equal(B.bridgeFlavor, "v2");
});

/* 5 — NO bridge at all: invoke rejects, runStep returns structured error,
 *      diagnostics (Fix F contract) are embedded everywhere */
test("timeout without bridge: reject + structured error + diagnostics", async () => {
  const sb = freshContext({});
  const B = sb.window.WizardBridge;
  assert.equal(B.bridgeFlavor, null);
  assert.equal(B.isTauri, false);

  const t0 = Date.now();
  await assert.rejects(
    () => B.invoke("setup_state"),
    (e) => {
      assert.equal(e.code, "bridge_unavailable");
      assert.match(e.message, /no backend bridge/);
      assert.match(e.diagnostics, /__TAURI__ present: false/);
      assert.match(e.diagnostics, /__TAURI_INTERNALS__ present: false/);
      assert.match(e.diagnostics, /wizard-bridge\.test\/1\.0/);       // userAgent
      assert.match(e.diagnostics, /waited: \d+ ms/);
      return true;
    });
  const waited = Date.now() - t0;
  assert.ok(waited >= 1900, `resolver must poll ~2000ms, waited ${waited}ms`);

  const res = await B.runStep("report", []); // structured, NEVER throws
  assert.equal(res.ok, false);
  assert.equal(res.error_code, "bridge");
  assert.match(res.error_fa, /اتصال به پوستهٔ برنامه/);
  assert.match(res.detail, /__TAURI__ present: false/);
  assert.match(res.detail, /withGlobalTauri/);

  assert.equal(await B.cancelStep(), false); // never blocks 2s, returns false

  const diag = B.diagnostics(1234);
  assert.match(diag, /location: /);
  assert.match(diag, /userAgent: /);
});

/* 6 — mock harness (__MOCK__) wins over everything, zero polling delay */
test("mock bridge: immediate invoke, flavor=mock", async () => {
  const sb = freshContext();
  sb.window.__MOCK__ = { invoke: async (cmd) => (cmd === "setup_state" ? { complete: false } : null) };
  vm.runInContext(BRIDGE_SRC, sb, { filename: "wizard-bridge.js (re-run with mock)" });
  const B = sb.window.WizardBridge;
  assert.equal(B.bridgeFlavor, "mock");
  assert.equal(B.hasMock, true);
  const st = await B.invoke("setup_state");
  assert.deepEqual(st, { complete: false });
});

/* 7 — wizard-event plumbing through the v2 event API + mock emit path */
test("onWizardEvent: parses payloads, falls back to raw log", async () => {
  const listeners = [];
  const sb = freshContext({ tauriFactory: () => ({
    __TAURI__: {
      core: { invoke: async () => "x" },
      event: { listen: (name, cb) => { listeners.push(cb); return Promise.resolve(); } },
    },
  }) });
  const B = sb.window.WizardBridge;
  const got = [];
  B.onWizardEvent((obj, raw) => got.push([obj, raw]));
  await new Promise((r) => setTimeout(r, 20));
  assert.ok(listeners.length >= 1, "listener attached to v2 event API");
  const payload = JSON.stringify({ t: "log", log: "hello" });
  listeners[0]({ payload });
  listeners[0]({ payload: "not-json" });
  // objects were built inside the vm realm — compare via JSON round-trip
  assert.deepEqual(JSON.parse(JSON.stringify(got[0][0])), { t: "log", log: "hello" });
  assert.equal(got[0][1], payload);
  assert.deepEqual(JSON.parse(JSON.stringify(got[1][0])), { t: "log", log: "not-json" });
});

/* ---- runner ---- */
(async () => {
  let failed = 0;
  for (const { name, fn } of tests) {
    const t0 = Date.now();
    try {
      await fn();
      console.log(`PASS  ${name}  (${Date.now() - t0}ms)`);
    } catch (e) {
      failed++;
      console.error(`FAIL  ${name}\n${e && e.stack ? e.stack : e}`);
    }
  }
  console.log(failed ? `\n${failed} test(s) FAILED` : "\nall bridge tests passed");
  process.exit(failed ? 1 : 0);
})();
