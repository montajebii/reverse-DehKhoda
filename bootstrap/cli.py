"""`dehkhoda-server bootstrap --step <x>` — wizard step dispatcher.

Runs INSIDE the PyInstaller sidecar binary; stdout carries the JSONL event
protocol (events.py). The Tauri shell spawns this instead of the HTTP server
during first-run setup, forwarding events to the wizard UI.

Steps:
  report    fast system report + decision table (no side effects)
  prereqs   install missing prerequisites (Docker engine / Docker Desktop /
            WSL2) — every privileged action goes through pkexec / UAC
  db        start the chosen vector engine and wait for health (runs the
            automatic fallback chain with per-engine diagnostics)
  model     download e5 weights (or --skip -> hash embedder)
  ingest    Dehkhoda pipeline (resumable)
  health    final store-level health check
  engine-smoke  hybrid-search capability probe (build gate / container tests)
  state     read/write setup-state.json (--get | --set key=value)
  selftest  offline selftests (detector / installer_linux / pins)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import paths, state
from .events import Emitter, emit

# make `app` importable in dev mode (frozen bundles already ship it)
if not getattr(sys, "frozen", False):
    _root = Path(__file__).resolve().parent.parent
    _backend = _root / "backend"
    for p in (_backend, _root):
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))


# =========================================================================
# env application (must precede any app.* import)
# =========================================================================
def apply_env_from_state() -> dict:
    from .state_env import apply_env

    return apply_env()


# =========================================================================
# steps
# =========================================================================
def step_report(args) -> None:
    from . import detector

    em = Emitter("system")
    consent = args.consent if args.consent in (0, 1) else None
    rep = detector.build_report(consent=None if consent is None else bool(consent))
    em.result(True, data=rep)


def step_prereqs(args) -> None:
    from . import detector

    em = Emitter("prereqs")
    st = state.load()
    consent = bool(st.get("decisions", {}).get("docker_consent", False))

    # v1.2.1: the default engine (Qdrant) needs NO prerequisites. Docker is
    # only relevant when the user explicitly picked the advanced Milvus mode.
    chosen = st.get("backend") or ""
    if chosen != "milvus":
        em.result(True, data={"skipped": True,
                              "reason": "default engine (Qdrant) needs no prereqs",
                              "backend": chosen or None})
        return

    rep = detector.build_report(consent=True if consent else False)
    backend = rep["backend"]
    needs = set(backend.get("needs") or [])

    if backend["path"] == "impossible":
        em.result(False, error_code="unsupported_platform",
                  error_fa="این سامانه پشتیبانی نمی‌شود؛ لطفاً راهنمای نصب را ببینید.")
        return

    if rep["os"]["type"] == "linux":
        if not rep["os"].get("glibc_ok", True):
            em.result(False, error_code="musl_not_supported",
                      error_fa="توزیع‌های مبتنی بر musl (مثل Alpine) پشتیبانی نمی‌شوند.")
            return
        if not consent:
            em.result(True, data={"skipped": True, "backend": backend})
            return
        try:
            from .installer_linux import LinuxInstaller, UnsupportedDistro

            inst = LinuxInstaller(rep["os"]["distro"], rep["os"].get("family"))
        except UnsupportedDistro:
            em.result(False, error_code="distro_unsupported",
                      error_fa="توزیع شما در فهرست پشتیبانی نیست (Ubuntu، Debian، Arch، Fedora، openSUSE).")
            return
        if not rep["docker"]["installed"]:
            em.progress(task_fa="نصب پیش‌نیازها (پنجرهٔ رمز سیستم‌عامل باز می‌شود)…",
                        indet=True)
            ok = inst.install_engine(
                log=lambda l: em.progress(log=f"install: {l[:300]}"))
            if not ok:
                em.result(False, error_code="engine_install_failed",
                          error_fa="نصب پیش‌نیاز ناموفق بود؛ با «تلاش مجدد» دوباره امتحان کنید.")
                return
        if not rep["docker"]["running"]:
            ok = inst.start_service(
                log=lambda l: em.progress(log=f"service: {l[:300]}"))
            if not ok:
                em.result(False, error_code="daemon_start_failed",
                          error_fa="راه‌اندازی سرویس داکر ناموفق بود؛ با «تلاش مجدد» دوباره امتحان کنید.")
                return
        # re-detect after install
        rep2 = detector.build_report()
        em.result(True, data={"docker": rep2["docker"], "backend": rep2["backend"]})

    else:  # windows
        from . import installer_win

        if "install_wsl2" in needs and not rep["windows"].get("wsl2"):
            em.progress(task_fa="فعال‌سازی WSL2 (نیاز به راه‌اندازی مجدد دارد)…",
                        indet=True)
            r = installer_win.enable_wsl2()
            if r.get("reboot_required"):
                em.result(True, data={"reboot_required": True,
                                      "message_fa": "برای کامل شدن فعال‌سازی WSL2 یک‌بار رایانه را راه‌اندازی مجدد کنید."})
                return
        if rep["docker"].get("installed") is False and consent:
            em.progress(task_fa="نصب Docker Desktop (پنجرهٔ تأیید ویندوز باز می‌شود)…",
                        indet=True)
            r = installer_win.install_docker_desktop()
            if not r.get("ok"):
                _log_exception(em, RuntimeError(str(r)))
                em.result(False, error_code="docker_desktop_install_failed",
                          error_fa="نصب Docker Desktop ناموفق بود؛ می‌توانید «رد کردن» را برگزینید تا از موتور جایگزین استفاده شود.")
                return
        rep2 = detector.build_report()
        if rep2["docker"].get("installed") and not rep2["docker"].get("running"):
            em.progress(task_fa="راه‌اندازی Docker Desktop…", indet=True)
            installer_win.start_docker_desktop()
            rep2 = detector.build_report()
        em.result(True, data={"docker": rep2["docker"], "backend": rep2["backend"],
                              "windows": rep2.get("windows")})


def step_db(args) -> None:
    from . import backend_launcher

    em = Emitter("db")
    apply_env_from_state()
    try:
        data = backend_launcher.launch(em)
        state.mark_step("database")
        em.result(True, data=data)
    except backend_launcher.EngineUnavailable as exc:
        # requirement: NEVER a generic message — carry every attempt's full
        # traceback + exact missing shared library to the technical log.
        for a in exc.attempts:
            em.progress(log=f"[{a.get('engine')}] "
                            f"{'skipped' if a.get('skipped') else 'FAILED'}: "
                            f"{a.get('error')}")
            if a.get("missing_libs"):
                em.progress(log=f"[{a.get('engine')}] missing shared libraries: "
                                + ", ".join(a["missing_libs"]))
            if a.get("traceback"):
                em.progress(log=a["traceback"][-1500:])
        import shutil as _shutil

        docker_present = bool(_shutil.which("docker"))
        em.result(False, error_code="engine_unavailable",
                  error_fa=("هیچ‌کدام از موتورهای جست‌وجو آماده نشد. جزئیات دقیق هر "
                            "خطا در «جزئیات فنی» آمده است؛ با «تلاش مجدد» دوباره "
                            "امتحان کنید."),
                  data={"attempts": exc.attempts, "docker_available": docker_present})
    except Exception as exc:
        _log_exception(em, exc)
        em.result(False, error_code="db_launch_failed",
                  error_fa="راه‌اندازی موتور برداری ناموفق بود؛ با «تلاش مجدد» دوباره امتحان کنید یا موتور جایگزین را برگزینید.")


def step_model(args) -> None:
    from . import model_fetcher

    em = Emitter("model")
    pins = model_fetcher.load_pins()["embedding_model"]
    if args.skip:
        state.set_value("embedder", "hash")
        st = state.load()
        st.setdefault("decisions", {})["model_skipped"] = True
        state.save(st)
        em.result(True, data={"skipped": True, "embedder": "hash"})
        return
    if model_fetcher.model_complete(pins["revision"]):
        em.progress(task_fa="مدل از قبل دانلود شده است")
        state.set_value("embedder", "e5-base")
        em.result(True, data={"dir": str(paths.models_dir()), "cached": True})
        return
    try:
        d = model_fetcher.fetch_model(
            progress_cb=lambda done, total, msg: em.progress(
                pct=int(100 * done / max(1, total)), task_fa="دانلود مدل زبانی…",
                log=msg))
        state.set_value("embedder", "e5-base")
        state.mark_step("model")
        em.result(True, data={"dir": str(d)})
    except Exception as exc:
        _log_exception(em, exc)
        em.result(False, error_code="model_download_failed",
                  error_fa="دانلود مدل ناموفق بود؛ اتصال اینترنت را بررسی کرده و «تلاش مجدد» را بزنید.")


def step_ingest(args) -> None:
    from . import dehkhoda_ingest

    em = Emitter("dictionary")
    apply_env_from_state()
    try:
        stats = dehkhoda_ingest.run(em, recreate=args.recreate)
        state.mark_step("dictionary")
        em.result(True, data=stats)
    except Exception as exc:
        _log_exception(em, exc)
        em.result(False, error_code="ingest_failed",
                  error_fa="آماده‌سازی لغت‌نامه ناموفق بود؛ نگران نباشید — از آخرین نقطهٔ پیشرفت ادامه می‌دهیم.")


def step_health(args) -> None:
    from . import backend_launcher

    em = Emitter("health")
    apply_env_from_state()
    em.result(True, data=backend_launcher.health_check(em))


def step_engine_smoke(args) -> None:
    """Hybrid-search capability probe — the build/CI gate (exit 1 on fail)."""
    from . import engine_smoke

    apply_env_from_state()
    rc = engine_smoke.main_cli(args.engine, args.preferred)
    if rc != 0:
        sys.exit(rc)


def step_state(args) -> None:
    em = Emitter("state")
    if args.set:
        key, _, value = args.set.partition("=")
        try:
            value = json.loads(value)
        except ValueError:
            pass
        st = state.set_value(key, value)
    else:
        st = state.load()
    em.result(True, data={"state": st})


def step_selftest(args) -> None:
    em = Emitter("selftest")
    from . import detector, installer_linux, model_fetcher

    em.result(True, data={
        "detector": detector.selftest_reports(),
        "installer_linux": installer_linux.selftest(),
        "model_fetcher": model_fetcher.selftest(),
    })


def _log_exception(em: Emitter, exc: Exception) -> None:
    import traceback

    em.progress(log="".join(traceback.format_exception(exc))[-1500:])


STEPS = {
    "report": step_report,
    "prereqs": step_prereqs,
    "db": step_db,
    "model": step_model,
    "ingest": step_ingest,
    "health": step_health,
    "engine-smoke": step_engine_smoke,
    "state": step_state,
    "selftest": step_selftest,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dehkhoda-server bootstrap")
    ap.add_argument("--step", required=True, choices=sorted(STEPS))
    ap.add_argument("--consent", type=int, default=None, choices=(0, 1))
    ap.add_argument("--skip", action="store_true")
    ap.add_argument("--recreate", action="store_true")
    ap.add_argument("--engine", default=None,
                    choices=("qdrant_local", "qdrant_native", "milvus_lite", "milvus"),
                    help="engine-smoke: force a single engine probe")
    ap.add_argument("--preferred", default=None,
                    help="engine-smoke: start the chain at this engine")
    ap.add_argument("--set", dest="set", default=None)
    args = ap.parse_args(argv)

    emit({"t": "hello", "step": args.step, "pid": os.getpid()})
    STEPS[args.step](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
