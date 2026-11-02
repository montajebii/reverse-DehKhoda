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

  /** Run one bootstrap step; resolves with the terminal result object.
   *  NEVER throws: a dead/unavailable IPC bridge (e.g. window.__TAURI__
   *  missing because withGlobalTauri was off) resolves with a structured
   *  error object so the wizard renders an error card instead of hanging
   *  on the step skeleton forever (v1.3.1 regression on Parch Linux). */
  async function runStep(step, args) {
    const extra = (args || []).slice();
    if (mock && mock.invoke) {
      return await mock.invoke("bootstrap_run", { step, args: extra });
    }
    if (!isTauri) {
      return {
        t: "result", ok: false, error_code: "bridge",
        error_fa: "اتصال به پوستهٔ برنامه (IPC) برقرار نشد",
        detail: "window.__TAURI__ is unavailable — the Tauri IPC bridge was not "
              + "injected. Check app.withGlobalTauri in tauri.conf.json, or the "
              + "page was opened outside the Tauri shell."
      };
    }
    try {
      const raw = await tauri.core.invoke("bootstrap_run", { step, args: extra });
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
      return await tauri.core.invoke("bootstrap_cancel", {});
    } catch {
      return false;
    }
  }

  window.WizardBridge = { invoke, runStep, cancelStep, onWizardEvent, isTauri, hasMock: !!mock };
})();
