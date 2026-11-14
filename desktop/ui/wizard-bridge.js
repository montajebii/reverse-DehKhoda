/* Dehkhoda Setup Wizard — environment bridge.
 *
 * Execution targets, in priority order:
 *   1. window.__MOCK__          — Playwright/browser demo (scripted steps + events)
 *   2. window.__TAURI__         — the real Tauri shell (v2: core.invoke, v1: invoke)
 *   3. window.__TAURI_INTERNALS__ — raw Tauri internals (invoke only, no events)
 *   4. plain browser            — report-only fallback (selftest/demo page)
 *
 * v1.3.1-hotfix2 (Fix E — lazy IPC bridge resolution):
 * WebKitGTK injects window.__TAURI__ *after* page scripts execute, so the
 * previous parse-time snapshot (`const tauri = window.__TAURI__`) permanently
 * missed the bridge and the wizard died at Step 2 with "no backend bridge"
 * even when app.withGlobalTauri was enabled (tauri.conf.json e584d07).
 * Every access now RESOLVES the bridge lazily, polling every 50 ms for up to
 * 2 s before giving up. On final failure the resolver attaches diagnostics
 * (bridge-object presence, userAgent, location, waited-ms) to the error and
 * streams them into the wizard's «جزئیات فنی» panel (Fix F contract).
 */
"use strict";

(function () {
  const BRIDGE_POLL_MS = 50;      // re-check cadence (WebKitGTK injection latency)
  const BRIDGE_TIMEOUT_MS = 2000; // give up after 2 s (v1.3.1-hotfix2 spec)
  const CANCEL_TIMEOUT_MS = 250;  // cancel must stay snappy; never block 2 s

  const BRIDGE_FAIL_MSG =
    "no backend bridge after " + BRIDGE_TIMEOUT_MS + " ms of polling " +
    "(open inside Tauri or the demo harness)";

  let eventSink = null;
  let eventAttached = false;
  let cached = null; // resolved bridge descriptor (flavor + bound fns)

  /* Locate the Tauri bridge RIGHT NOW (no caching, no waiting). */
  function findTauri() {
    const g = window.__TAURI__;
    if (g && g.core && typeof g.core.invoke === "function") {
      // Tauri v2 — global API surface: __TAURI__.core.invoke
      return {
        flavor: "v2",
        invoke: (cmd, args) => g.core.invoke(cmd, args),
        listen: (g.event && typeof g.event.listen === "function")
          ? g.event.listen : null,
      };
    }
    if (g && typeof g.invoke === "function") {
      // Tauri v1 — legacy surface: __TAURI__.invoke
      return {
        flavor: "v1",
        invoke: (cmd, args) => g.invoke(cmd, args),
        listen: (g.event && typeof g.event.listen === "function")
          ? g.event.listen : null,
      };
    }
    const it = window.__TAURI_INTERNALS__;
    if (it && typeof it.invoke === "function") {
      // Raw internals — last resort; no event API here.
      return { flavor: "internals", invoke: (cmd, args) => it.invoke(cmd, args), listen: null };
    }
    return null;
  }

  /* Poll for the bridge until it appears or the deadline passes. */
  async function resolveTauri(timeoutMs) {
    const deadline = Date.now() + (timeoutMs === undefined ? BRIDGE_TIMEOUT_MS : timeoutMs);
    for (;;) {
      const found = findTauri();
      if (found) { cached = found; return found; }
      if (Date.now() >= deadline) return null;
      await new Promise((r) => setTimeout(r, BRIDGE_POLL_MS));
    }
  }

  /* Fix F contract — capture the environment at the moment of failure. */
  function diagnostics(waitedMs) {
    return [
      "bridge diagnostics:",
      "  __TAURI__ present: " + !!window.__TAURI__,
      "  __TAURI_INTERNALS__ present: " + !!window.__TAURI_INTERNALS__,
      "  userAgent: " + (navigator && navigator.userAgent ? navigator.userAgent : "(unknown)"),
      "  location: " + (window.location && window.location.href ? window.location.href : "(unknown)"),
      "  waited: " + waitedMs + " ms (poll " + BRIDGE_POLL_MS + " ms, timeout " + BRIDGE_TIMEOUT_MS + " ms)",
    ].join("\n");
  }

  function emitDiagnostics(text) {
    try {
      if (eventSink) eventSink({ t: "log", log: text }, text);
    } catch { /* sink may not be wired yet — error object still carries it */ }
  }

  const mock = window.__MOCK__ || null;

  async function invoke(cmd, args = {}) {
    if (mock && mock.invoke) return await mock.invoke(cmd, args);
    const t = await resolveTauri();
    if (!t) {
      const d = diagnostics(BRIDGE_TIMEOUT_MS);
      emitDiagnostics(d);
      const err = new Error(BRIDGE_FAIL_MSG);
      err.code = "bridge_unavailable";
      err.diagnostics = d;
      throw err;
    }
    return await t.invoke(cmd, args);
  }

  /* Attach the wizard-event listener to whichever bridge flavor is present. */
  function attachEventListener() {
    if (eventAttached) return;
    const t = findTauri();
    if (t && t.listen) {
      t.listen("wizard-event", (e) => {
        let obj = null;
        try { obj = JSON.parse(e.payload); } catch { obj = { t: "log", log: e.payload }; }
        if (eventSink) eventSink(obj, e.payload);
      });
      eventAttached = true;
    }
  }

  function onWizardEvent(cb) {
    eventSink = cb;
    attachEventListener();
    if (!eventAttached && !(mock && mock.invoke)) {
      // Bridge not injected yet — keep trying in the background (WebKitGTK
      // race); when it lands, wizard-event streaming starts working.
      resolveTauri().then((t) => { if (t) attachEventListener(); }).catch(() => {});
    }
    // mock mode: mock pushes through window.__mockEmit
  }

  // mock support — the demo harness calls this
  window.__mockEmit = function (line) {
    let obj = null;
    try { obj = JSON.parse(line); } catch { obj = { t: "log", log: line }; }
    if (eventSink) eventSink(obj, line);
  };

  /** Run one bootstrap step; resolves with the terminal result object.
   *  NEVER throws: a dead/unavailable IPC bridge resolves with a structured
   *  error object (bridge flavor diagnostics embedded in `detail`) so the
   *  wizard renders an error card instead of hanging on the step skeleton
   *  forever (v1.3.1 regression on Parch Linux; polling added in hotfix2). */
  async function runStep(step, args) {
    const extra = (args || []).slice();
    if (mock && mock.invoke) {
      return await mock.invoke("bootstrap_run", { step, args: extra });
    }
    const t = await resolveTauri();
    if (!t) {
      const d = diagnostics(BRIDGE_TIMEOUT_MS);
      emitDiagnostics(d);
      return {
        t: "result", ok: false, error_code: "bridge",
        error_fa: "اتصال به پوستهٔ برنامه (IPC) برقرار نشد",
        detail: "window.__TAURI__ unavailable after " + BRIDGE_TIMEOUT_MS +
                " ms of polling — withGlobalTauri may be off in tauri.conf.json, " +
                "or the page was opened outside the Tauri shell.\n" + d
      };
    }
    try {
      const raw = await t.invoke("bootstrap_run", { step, args: extra });
      try {
        const obj = JSON.parse(raw);
        return obj;
      } catch {
        return { t: "result", ok: false, error_code: "parse", error_fa: "پاسخ نامعتبر" };
      }
    } catch (e) {
      // invoke() rejected — command panic/IPC failure; surface it, never hang
      return {
        t: "result", ok: false, error_code: "bridge",
        error_fa: "اتصال به پوستهٔ برنامه (IPC) برقرار نشد",
        detail: String((e && e.message) || e)
      };
    }
  }

  /** Kill the running wizard step (model download Cancel button).
   *  Partial downloads are preserved on purpose — the next start resumes. */
  async function cancelStep() {
    try {
      if (mock && mock.invoke) return await mock.invoke("bootstrap_cancel", {});
      const t = await resolveTauri(CANCEL_TIMEOUT_MS);
      if (!t) return false;
      return await t.invoke("bootstrap_cancel", {});
    } catch {
      return false;
    }
  }

  window.WizardBridge = {
    invoke,
    runStep,
    cancelStep,
    onWizardEvent,
    // Evaluated at ACCESS time (lazy) — true iff a bridge is visible right now.
    get isTauri() { return !!(mock && mock.invoke) || !!findTauri(); },
    // "v2" | "v1" | "internals" | null — which bridge flavor is visible now.
    get bridgeFlavor() { return (mock && mock.invoke) ? "mock" : (findTauri() || {}).flavor || null; },
    hasMock: !!mock,
    // Fix F / test hooks:
    diagnostics,
    resolveTauri,
    findTauri,
  };
})();
