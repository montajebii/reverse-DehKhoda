/* Dehkhoda Setup Wizard — environment bridge.
 *
 * Execution targets, in priority order:
 *   1. window.__MOCK__  — Playwright/browser demo (scripted steps + events)
 *   2. window.__TAURI__ — the real Tauri v2 shell (invoke + event listen)
 *   3. plain browser    — report-only fallback (selftest/demo page)
 */
"use strict";

(function () {
  const tauri = window.__TAURI__;
  const isTauri = !!(tauri && tauri.core && tauri.core.invoke);
  const mock = window.__MOCK__ || null;

  let eventSink = null;

  async function invoke(cmd, args = {}) {
    if (mock && mock.invoke) return await mock.invoke(cmd, args);
    if (isTauri) return await tauri.core.invoke(cmd, args);
    throw new Error("no backend bridge (open inside Tauri or the demo harness)");
  }

  function onWizardEvent(cb) {
    eventSink = cb;
    if (isTauri && tauri.event && tauri.event.listen) {
      tauri.event.listen("wizard-event", (e) => {
        let obj = null;
        try { obj = JSON.parse(e.payload); } catch { obj = { t: "log", log: e.payload }; }
        cb(obj, e.payload);
      });
    }
    // mock mode: mock pushes through window.__mockEmit
  }

  // mock support — the demo harness calls this
  window.__mockEmit = function (line) {
    let obj = null;
    try { obj = JSON.parse(line); } catch { obj = { t: "log", log: line }; }
    if (eventSink) eventSink(obj, line);
  };

  /** Run one bootstrap step; resolves with the terminal result object. */
  async function runStep(step, args) {
    const extra = (args || []).slice();
    if (mock && mock.invoke) {
      return await mock.invoke("bootstrap_run", { step, args: extra });
    }
    const raw = await tauri.core.invoke("bootstrap_run", { step, args: extra });
    try {
      const obj = JSON.parse(raw);
      return obj;
    } catch {
      return { t: "result", ok: false, error_code: "parse", error_fa: "پاسخ نامعتبر" };
    }
  }

  window.WizardBridge = { invoke, runStep, onWizardEvent, isTauri, hasMock: !!mock };
})();
