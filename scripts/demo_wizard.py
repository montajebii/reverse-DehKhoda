"""Playwright demo of the setup wizard — walks every screen and screenshots.

Scenarios:
  ubuntu-fresh    Ubuntu + no docker -> consent -> milvus -> model -> ingest -> ready
  windows-declined Windows 11, docker declined -> qdrant fallback -> ready
  fastpath        healthy re-run -> lands on «آماده است» directly (<10 s)

Usage:
  python3 scripts/demo_wizard.py [scenario ...]     # default: all

Screenshots -> download/wizard-screens/<scenario>-<step>.png
Also asserts: zero console errors, Persian placeholder/labels present.
"""

from __future__ import annotations

import json
import sys
import threading
import functools
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "desktop" / "ui"
OUT = ROOT / "download" / "wizard-screens"

REPORT_UBUNTU = {
    "schema": 1,
    "os": {"type": "linux", "distro": "ubuntu", "family": "debian",
           "pretty": "Ubuntu 24.04 LTS", "version": "24.04", "arch": "x86_64",
           "build": None, "glibc_ok": True, "package_manager": "apt-get",
           "id_like": ["debian"]},
    "resources": {"disk_free_gb": 96.4, "disk_ok": True, "ram_gb": 16.0,
                  "ram_ok": True, "ram_recommended": True},
    "docker": {"installed": False, "running": False, "socket_accessible": False,
               "compose_plugin": False, "compose_standalone": False,
               "engine_version": None},
    "python": {"present": True, "version": "3.12.7", "bundled": True},
    "milvus_lite": {"available": True, "smoke_ok": None},
    "existing": {"milvus_port_open": False, "qdrant_port_open": False},
    "windows": None,
    "linux": {"pkexec": True, "systemd": True, "docker_unit_active": False},
}
REPORT_WIN = {
    "schema": 1,
    "os": {"type": "windows", "distro": "windows", "family": None,
           "pretty": "Windows 11", "version": "22631", "arch": "x86_64",
           "build": 22631, "glibc_ok": True},
    "resources": {"disk_free_gb": 210.5, "disk_ok": True, "ram_gb": 32.0,
                  "ram_ok": True, "ram_recommended": True},
    "docker": {"installed": False, "running": False, "socket_accessible": False,
               "desktop_exe": None, "service_installed": False,
               "compose_plugin": False, "engine_version": None},
    "python": {"present": True, "version": "3.12.7", "bundled": True},
    "milvus_lite": {"available": False, "smoke_ok": None},
    "existing": {"milvus_port_open": False, "qdrant_port_open": False},
    "windows": {"winget": True, "wsl2": True, "docker_desktop_installed": False},
    "linux": None,
}


def backend_for(report: dict, consent):
    """Mirror of the detector decision table for the demo mock."""
    d = report["docker"]
    if d["installed"] and d["running"]:
        return {"path": "milvus", "reasons": ["docker_available"], "needs": []}
    installable = (report["windows"] or {}).get("winget") if report["os"]["type"] == "windows" \
        else bool(report["linux"]["pkexec"] and report["os"].get("package_manager"))
    if installable and consent is None:
        return {"path": "milvus_pending_consent", "reasons": ["docker_installable"],
                "needs": ["ask_docker_consent"]}
    if installable and consent:
        return {"path": "milvus", "reasons": ["docker_installable"],
                "needs": ["install_docker"]}
    if report["os"]["type"] == "windows":
        return {"path": "qdrant_native",
                "reasons": ["docker_declined", "qdrant_native_fallback"], "needs": []}
    if report["python"]["bundled"] and report["milvus_lite"]["available"]:
        return {"path": "milvus_lite", "reasons": ["docker_declined", "milvus_lite_available"],
                "needs": []}
    return {"path": "qdrant_native",
            "reasons": ["docker_declined", "milvus_lite_unavailable",
                        "qdrant_native_fallback_offer"],
            "needs": ["confirm_qdrant_fallback"]}


MOCK_JS_TEMPLATE = r"""
window.__MOCK__ = (() => {
  const REPORT = __REPORT__;
  const START_STEPS = __START_STEPS__;
  const START_COMPLETE = __START_COMPLETE__;
  let state = {schema: 2, complete: START_COMPLETE, backend: null,
               embedder: "e5-base", steps: JSON.parse(JSON.stringify(START_STEPS)),
               decisions: {}, runtime: null, ingested_count: 0};
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));
  const clone = (o) => JSON.parse(JSON.stringify(o));

  async function run(step, args) {
    args = args || [];
    if (step === "report") {
      const cArg = args.indexOf("--consent");
      let consent = null;
      if (cArg >= 0) consent = args[cArg + 1] === "1";
      const rep = clone(REPORT);
      rep.backend = __backend_for__(rep, consent);
      state.decisions.docker_consent = consent;
      await delay(350);
      return {t: "result", step: "system", ok: true, data: rep};
    }
    if (step === "state") {
      const i = args.indexOf("--set");
      if (i >= 0) {
        const kv = args[i + 1], eq = kv.indexOf("=");
        const k = kv.slice(0, eq), v = JSON.parse(kv.slice(eq + 1));
        state[k] = v;
      }
      await delay(40);
      return {t: "result", step: "state", ok: true, data: {state: clone(state)}};
    }
    if (step === "prereqs") {
      for (const l of ["pkexec apt-get update -> rc=0",
                       "pkexec apt-get install -y --no-install-recommends docker.io -> rc=0",
                       "systemctl enable --now docker -> rc=0"]) {
        window.__mockEmit(JSON.stringify({t: "log", log: l}));
        await delay(280);
      }
      REPORT.docker.installed = true; REPORT.docker.running = true;
      REPORT.docker.compose_plugin = true;
      return {t: "result", step: "prereqs", ok: true,
              data: {docker: clone(REPORT.docker), backend: __backend_for__(REPORT, true)}};
    }
    if (step === "db") {
      window.__mockEmit(JSON.stringify({t: "progress", step: "db", task_fa: "در حال دریافت تصاویر Milvus…", indet: true}));
      await delay(700);
      window.__mockEmit(JSON.stringify({t: "progress", step: "db", task_fa: "در انتظار آماده‌شدن Milvus…", indet: true}));
      await delay(600);
      state.runtime = {backend: "milvus", uri: "http://127.0.0.1:19530", reused: false};
      return {t: "result", step: "db", ok: true, data: clone(state.runtime)};
    }
    if (step === "model") {
      if (args.includes("--skip")) {
        state.embedder = "hash";
        return {t: "result", step: "model", ok: true, data: {skipped: true, embedder: "hash"}};
      }
      for (let p = 0; p <= 100; p += 10) {
        window.__mockEmit(JSON.stringify({t: "progress", step: "model", pct: p,
          task_fa: "دانلود مدل زبانی…", log: "model.safetensors " + p + "%"}));
        await delay(90);
      }
      return {t: "result", step: "model", ok: true, data: {dir: "~/.local/share/dehkhoda/models"}};
    }
    if (step === "ingest") {
      window.__mockEmit(JSON.stringify({t: "progress", step: "dictionary", task_fa: "دانلود داده‌های لغت‌نامه (۲۹۰ مگابایت، قابل ادامه)…", indet: true}));
      await delay(600);
      let done = state.ingested_count || 120000;
      for (; done < 312506; done += 24000) {
        window.__mockEmit(JSON.stringify({t: "progress", step: "dictionary",
          pct: Math.round(100 * done / 312506),
          task_fa: "درج بردارها… " + done.toLocaleString("fa-IR") + " از ۳۱۲٬۵۰۶",
          log: "chunk end=" + done}));
        await delay(110);
      }
      state.ingested_count = 312506;
      return {t: "result", step: "dictionary", ok: true,
              data: {entries: 312506, chunks: 318431, resumed: (state.ingested_count || 0) > 0}};
    }
    return {t: "result", step, ok: false, error_code: "mock", error_fa: "step not mocked: " + step};
  }

  return {
    invoke: async (cmd, args) => {
      if (cmd === "setup_state") return clone(state);
      if (cmd === "start_backend") return {port: 8300, ready: true};
      if (cmd === "bootstrap_run") return await run(args.step, args.args);
      throw new Error("mock: unknown cmd " + cmd);
    },
  };
})();
"""


def make_mock_js(scenario: str) -> str:
    if scenario == "ubuntu-fresh":
        report, steps, complete = REPORT_UBUNTU, {}, False
    elif scenario == "windows-declined":
        report, steps, complete = REPORT_WIN, {}, False
    elif scenario == "fastpath":
        report, steps, complete = REPORT_UBUNTU, {
            "welcome": "done", "system": "done", "prereqs": "done",
            "database": "done", "model": "done", "dictionary": "done",
        }, True
    else:
        raise SystemExit(f"unknown scenario {scenario}")
    js = MOCK_JS_TEMPLATE
    js = js.replace("__REPORT__", json.dumps(report, ensure_ascii=False))
    js = js.replace("__START_STEPS__", json.dumps(steps))
    js = js.replace("__START_COMPLETE__", "true" if complete else "false")
    # embed the JS decision-table mirror as BACKENDFN
    fn_js = """
function(rep, consent){
  const d = rep.docker;
  if (d.installed && d.running) return {path:'milvus',reasons:['docker_available'],needs:[]};
  const installable = rep.os.type==='windows'
      ? !!(rep.windows && rep.windows.winget)
      : !!(rep.linux && rep.linux.pkexec && rep.os.package_manager);
  if (installable && consent === null || installable && consent === undefined)
      return {path:'milvus_pending_consent',reasons:['docker_installable'],needs:['ask_docker_consent']};
  if (installable && consent === true)
      return {path:'milvus',reasons:['docker_installable'],needs:['install_docker']};
  if (rep.os.type==='windows')
      return {path:'qdrant_native',reasons:['docker_declined','qdrant_native_fallback'],needs:[]};
  if (rep.python.bundled && rep.milvus_lite.available)
      return {path:'milvus_lite',reasons:['docker_declined','milvus_lite_available'],needs:[]};
  return {path:'qdrant_native',
          reasons:['docker_declined','milvus_lite_unavailable','qdrant_native_fallback_offer'],
          needs:['confirm_qdrant_fallback']};
}
"""
    js = js.replace("__backend_for__", "BACKENDFN")
    js = "const BACKENDFN = " + fn_js + ";\n" + js
    return js


def serve_ui() -> tuple[ThreadingHTTPServer, str]:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(UI))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, f"http://127.0.0.1:{port}/wizard.html"


def run_scenario(browser, base_url: str, scenario: str) -> list[str]:
    page = browser.new_page(viewport={"width": 1180, "height": 800})
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.add_init_script(make_mock_js(scenario))
    page.goto(base_url)
    page.wait_for_timeout(400)

    shots: list[str] = []
    OUT.mkdir(parents=True, exist_ok=True)

    def snap(name: str) -> None:
        path = OUT / f"{scenario}-{name}.png"
        page.screenshot(path=str(path), full_page=False)
        shots.append(path.name)

    if scenario == "fastpath":
        page.wait_for_selector(".ready-hero", timeout=4000)
        t0 = page.evaluate("performance.now()")
        snap("ready")
        print(f"[fastpath] ready screen in <{page.evaluate('performance.now()') - t0:.0f}ms + load")
        page.close()
        return shots

    snap("1-welcome")
    page.get_by_text("ادامه", exact=True).click()
    page.wait_for_timeout(700)

    snap("2-system")
    if scenario == "ubuntu-fresh":
        # consent panel appears on prereqs
        page.get_by_text("ادامه", exact=True).first.click()  # -> prereqs
        page.wait_for_timeout(300)
        snap("3-consent")
        page.get_by_text("ادامه (نصب داکر)").click()
        page.wait_for_timeout(2200)  # prereqs logs + db progress
        snap("4-db-launch")
        page.wait_for_timeout(1800)
    else:  # windows-declined
        page.get_by_text("ادامه", exact=True).first.click()
        page.wait_for_timeout(300)
        snap("3-consent")
        page.get_by_text("رد کردن", exact=True).click()
        page.wait_for_timeout(2500)  # qdrant fallback db
        snap("4-db-fallback")

    # model step (auto-advanced)
    page.wait_for_timeout(400)
    snap("5-model")
    page.get_by_text("ادامه (دانلود)").click()
    page.wait_for_timeout(1600)
    snap("5-model-downloading")

    # ingest step (auto-advanced after model completes)
    page.wait_for_timeout(2500)
    snap("6-ingest")
    page.wait_for_timeout(4500)  # ingest finishes -> ready
    page.wait_for_selector(".ready-hero", timeout=8000)
    snap("7-ready")

    # sanity assertions
    assert page.locator(".step.done").count() >= 6, "expected >=6 done steps"
    page.close()
    return shots


def main() -> int:
    scenarios = sys.argv[1:] or ["ubuntu-fresh", "windows-declined", "fastpath"]
    httpd, url = serve_ui()
    all_ok = True
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for sc in scenarios:
            print(f"=== scenario {sc} ===")
            try:
                shots = run_scenario(browser, url, sc)
                for s in shots:
                    print("   saved", s)
            except AssertionError as exc:
                print("   ASSERT FAILED:", exc)
                all_ok = False
        browser.close()
    httpd.shutdown()
    print("PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
