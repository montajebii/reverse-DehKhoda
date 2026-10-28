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
    try:
        rep["model"] = model_report()
    except Exception as exc:  # never let the model section break detection
        em.progress(log=f"model_report unavailable: {exc}")
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
        # defer_index_restore=True: the wizard's dictionary step owns the
        # restore progress UI (db step only brings the engine up)
        data = backend_launcher.launch(em, defer_index_restore=True)
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


FA_MODEL_ERR = {
    "disk": "فضای دیسک برای دانلود مدل کافی نیست. چند گیگابایت آزاد کنید و «تلاش مجدد» را بزنید.",
    "dns": "اتصال به سرور مدل (huggingface.co) برقرار نشد. اتصال اینترنت را بررسی کنید؛ نسخهٔ کامل (آفلاین) به اینترنت نیاز ندارد.",
    "timeout": "اتصال به سرور مدل بیش از حد طول کشید. اتصال خود را بررسی کرده و «تلاش مجدد» را بزنید.",
    "http": "سرور مدل پاسخ خطا داد. بعداً دوباره امتحان کنید یا از نسخهٔ کامل (آفلاین) استفاده کنید.",
    "checksum": "فایل دانلودشده با اثر انگشت رسمی مطابقت ندارد و پاک شد. «تلاش مجدد» دانلود را از نو امن می‌کند.",
    "download": "دانلود مدل ناتمام ماند. اتصال برقرار است؟ «تلاش مجدد» از همان‌جا ادامه می‌دهد.",
}


def _model_variant() -> str:
    """full = the package ships the model; lean = first-run download."""
    from . import paths

    return "full" if paths.bundled_model_dir() else "lean"


def model_report() -> dict:
    """Model-variant summary for the system-report step (no side effects)."""
    from . import model_download, paths

    bundled = paths.bundled_model_dir()
    try:
        em = model_download.model_pin()
        rev = em["revision"]
        total = model_download.total_size_bytes()
        repo_id = em.get("repo_id") or em["id"]
    except Exception:  # pins unreadable must never break the report
        rev, total, repo_id = "", 0, ""
    return {
        "variant": "full" if bundled else "lean",
        "repo_id": repo_id,
        "revision": rev,
        "total_size_bytes": total,
        "bundled_dir": str(bundled) if bundled else None,
        "target_dir": str(model_download.target_dir()),
        "ready": model_download.model_ready() is not None,
        "partial": model_download.detect_partial(),
    }


def step_model(args) -> None:
    """دانلود مدل زبانی — the v1.3.0 dual-variant step.

    Modes (flags on --step model):
      (none)      resolve-or-download: bundled → verify+mark; .ready → skip;
                  seeded/downloaded dir → verify+mark; else download
      --preflight report only (disk 2x + HEAD 5s + partial detection)
      --start     begin/resume the download (lean)
      --restart   discard .partial residue, then download from scratch
      --check     verify an existing dir (bundled or data dir), no download
      --skip      escape hatch: hash embedder (keyword-only search)
    """
    from . import model_download, paths

    em = Emitter("model")
    variant = _model_variant()

    if args.skip:
        state.set_value("embedder", "hash")
        st = state.load()
        st.setdefault("decisions", {})["model_skipped"] = True
        state.save(st)
        em.result(True, data={"skipped": True, "embedder": "hash"})
        return

    if args.preflight:
        em.result(True, data={"variant": variant,
                              "preflight": model_download.preflight()})
        return

    ready = model_download.model_ready()
    if ready and not args.restart:
        state.set_value("embedder", "e5-base")
        state.mark_step("model")
        em.result(True, data={"status": "ready", "cached": True,
                              "source": ready.get("source"),
                              "revision": ready.get("revision")})
        return

    bundled = paths.bundled_model_dir()
    if variant == "full" and bundled:
        em.progress(task_fa="بررسی نسخهٔ همراه مدل…", indet=True)
        bad = model_download.verify_dir(bundled)
        if bad:
            # spec: log a warning; do NOT block startup on a bundled-dir
            # checksum mismatch (the engine chain still works keyword-only)
            em.progress(log="bundled model checksum mismatch: "
                            + ", ".join(bad))
        else:
            em.progress(log=f"bundled model verified against pins.json: "
                            f"{bundled}")
        st = state.load()
        st["model"] = {"source": "bundled", "dir": str(bundled),
                       "revision": model_download.revision(),
                       "verified": not bad}
        state.save(st)
        state.set_value("embedder", "e5-base")
        state.mark_step("model")
        if not bad:
            model_download.mark_ready("bundled")
        em.result(True, data={"status": "bundled", "dir": str(bundled),
                              "verified": not bad,
                              "revision": model_download.revision(),
                              "message_fa": "مدل زبانی همراه بسته نصب شد."})
        return

    # ------- lean: an existing complete dir (seeded / earlier download)? --
    tdir = model_download.target_dir()
    if not args.restart and model_download.valid_dir(tdir) and \
            not model_download.detect_partial():
        bad = model_download.verify_dir(tdir, delete_bad=True)
        if not bad:
            model_download.ensure_model_ready("downloaded")
            st = state.load()
            st["model"] = {"source": "downloaded", "dir": str(tdir),
                           "revision": model_download.revision(),
                           "verified": True}
            state.save(st)
            state.set_value("embedder", "e5-base")
            state.mark_step("model")
            em.result(True, data={"status": "ready", "dir": str(tdir),
                                  "source": "downloaded",
                                  "revision": model_download.revision(),
                                  "verified": True})
            return
        em.progress(log="incomplete model dir removed: " + ", ".join(bad))

    partial = model_download.detect_partial()

    if args.check:
        # verification-only mode (CI acceptance): never download; a partial
        # download does NOT count as present
        em.result(False, error_code="model_missing",
                  error_fa=("مدل زبانی در دسترس نیست؛ نسخهٔ سبک آن را در اولین "
                            "اجرا دانلود می‌کند."),
                  data={"variant": variant, "target_dir": str(tdir),
                        "partial": partial})
        return

    if partial and not (args.start or args.restart):
        # let the wizard show «دانلود مدل ناقص است. ادامه می‌دهید؟» first
        em.result(True, data={"status": "partial", "partial": partial,
                              "preflight": model_download.preflight()})
        return

    if args.restart:
        model_download.discard_partial()
        partial = None

    # ------------------------------ the actual (resumable) download -------
    em.progress(task_fa="آماده‌سازی دانلود…", log=f"variant=lean revision="
                f"{model_download.revision()} target={tdir}")

    def cb(p: dict) -> None:
        em.progress(
            pct=int(p["overall_percent"]),
            task_fa=f"در حال دانلود: {p['file']} — "
                    f"{p['bytes_done'] // 1048576} از "
                    f"{p['bytes_total'] // 1048576} مگابایت",
            log=(f"{p['file']} {p['bytes_done']}/{p['bytes_total']} "
                 f"speed={p['speed_bps']}B/s eta={p['eta_seconds']}s "
                 f"phase={p['phase']}"),
            data=p)

    try:
        pf = model_download.preflight()
        if not pf["disk_ok"]:
            raise model_download.ModelDownloadError(
                f"need {pf['need_bytes']} bytes free, "
                f"have {pf['disk_free_bytes']}", kind="disk")
        if not pf["network_ok"]:
            raise model_download.ModelDownloadError(
                pf["network_error"], kind=_net_kind(pf["network_error"]))
        d = model_download.download(progress_cb=cb)
        model_download.ensure_model_ready("downloaded")
        st = state.load()
        st["model"] = {"source": "downloaded", "dir": str(d),
                       "revision": model_download.revision(),
                       "verified": True}
        state.save(st)
        state.set_value("embedder", "e5-base")
        state.mark_step("model")
        em.result(True, data={"status": "ready", "dir": str(d),
                              "source": "downloaded",
                              "revision": model_download.revision(),
                              "verified": True,
                              "message_fa": "مدل زبانی دانلود و تأیید شد."})
    except model_download.DownloadCancelled:
        partial = model_download.detect_partial() or {}
        em.result(False, error_code="cancelled",
                  error_fa="دانلود متوقف شد؛ فایل‌های ناقص برای ادامه حفظ شدند.",
                  data={"partial": partial,
                        "target_dir": str(model_download.target_dir())})
    except model_download.ModelDownloadError as exc:
        _log_exception(em, exc)
        em.progress(log=exc.technical())
        em.result(False, error_code=f"model_download_{exc.kind}",
                  error_fa=FA_MODEL_ERR.get(exc.kind, FA_MODEL_ERR["download"]),
                  data={"technical": exc.technical(), "url": exc.url,
                        "http_status": exc.http_status, "kind": exc.kind,
                        "resume_bytes": (partial or {}).get("bytes", 0)})
    except Exception as exc:
        _log_exception(em, exc)
        em.result(False, error_code="model_download_failed",
                  error_fa="دانلود مدل ناموفق بود؛ «تلاش مجدد» از همان‌جا ادامه می‌دهد.")


def _net_kind(net_err: str) -> str:
    for kind in ("dns", "timeout", "http"):
        if f"kind={kind}" in net_err:
            return kind
    return "download"


def step_ingest(args) -> None:
    """آماده‌سازی لغت‌نامه دهخدا — v1.3.1: one step, three behaviors.

    full (bundled snapshot):  restore (target <60s) — any failure falls
                              through to rebuild-from-bundle, NEVER silently
    full (bundled dumps only): rebuild from the bundled SQL (checkpointed)
    lean:                     the existing resumable download + ingest path
    Every branch emits the explicit dataset event {step:"dataset", mode:…}.
    """
    from . import index_restore

    em = Emitter("dictionary")
    apply_env_from_state()
    try:
        result = index_restore.ensure_index(em, recreate=args.recreate)
        state.mark_step("dictionary")
        em.result(True, data=result)
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


def step_index(args) -> None:
    """v1.3.1 index maintenance step (offline; requires the full variant).

    Modes (flags on --step index):
      (none)            ensure_index per the bundle manifest (auto branch)
      --bundle-index    force restore from the bundled snapshot on first run
      --bundle-dataset  force rebuild from the bundled SQL on first run
      --verify-index    verify index.ready + metadata + record_count; exit 0/1
      --rebuild-index   ignore the bundled snapshot, rebuild from the
                        bundled SQL (future model upgrades; requires the
                        dataset bundle)
      --diagnostics     single .zip with OS/glibc info, engine chain log,
                        qdrant log, backend log, last wizard state,
                        /health JSON, one sample hybrid search result,
                        pins metadata and index.ready contents
    """
    from . import index_restore

    em = Emitter("index")
    apply_env_from_state()

    if args.verify_index:
        ok, detail = index_restore.verify_index(live=True)
        em.result(ok, data=detail,
                  error_code="" if ok else "index_not_ready",
                  error_fa=("ایندکس آماده نیست یا با نسخهٔ pinned مطابقت ندارد."
                            if not ok else ""))
        if not ok:
            sys.exit(1)
        return

    if args.diagnostics:
        path = _diagnostics_zip(em)
        em.result(True, data={"diagnostics": str(path)})
        return

    force = None
    if args.rebuild_index:
        force = "rebuilt_from_bundle"
    elif args.bundle_dataset:
        force = "rebuilt_from_bundle"
    elif args.bundle_index:
        force = "restored"
    try:
        result = index_restore.ensure_index(em, force_mode=force,
                                            recreate=args.recreate)
        em.result(True, data=result)
    except Exception as exc:
        _log_exception(em, exc)
        em.result(False, error_code="index_step_failed",
                  error_fa="عملیات ایندکس ناموفق بود؛ جزئیات در «جزئیات فنی» آمده است.")
        sys.exit(1)


def _diagnostics_zip(em: Emitter) -> Path:
    """Everything support needs, in one archive (replaces the HANDOFF
    'paste this' blocks)."""
    import io
    import platform
    import zipfile

    out = paths.data_dir() / "dehkhoda-diagnostics.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        info: dict[str, object] = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "glibc": "",
            "bundle": paths.bundled_manifest(),
            "index_state": index_restore.index_state(),
        }
        try:
            import ctypes

            libc = ctypes.CDLL("libc.so.6")
            buf2 = ctypes.create_string_buffer(128)
            libc.gnu_get_libc_version(buf2, 128)
            info["glibc"] = buf2.value.decode()  # type: ignore[union-attr]
        except Exception:
            pass
        z.writestr("system.json", json.dumps(info, ensure_ascii=False, indent=1))
        try:
            z.writestr("setup-state.json", paths.state_path().read_text("utf-8"))
        except OSError:
            pass
        try:
            z.writestr("index.ready", paths.index_ready_path().read_text("utf-8"))
        except OSError:
            pass
        for name in ("backend.log", "qdrant.log", "wizard.log"):
            p = paths.data_dir() / "logs" / name
            if p.exists():
                z.write(p, f"logs/{name}")
        try:
            from .model_fetcher import load_pins

            pins = load_pins()
            pins["embedding_model"]["files"] = "<elided>"
            z.writestr("pins-metadata.json", json.dumps(
                {k: pins[k] for k in ("prebuilt_index", "embedding_model")
                 if k in pins}, ensure_ascii=False, indent=1))
        except Exception:
            pass
        # live /health + one sample hybrid search (best-effort, local only)
        try:
            import urllib.request as _ur

            st = state.load()
            url = (st.get("runtime") or {}).get("url") or "http://127.0.0.1:6333"
            with _ur.urlopen(f"{url}/healthz", timeout=5) as r:
                z.writestr("health.json", r.read().decode("utf-8", "replace"))
        except Exception as exc:
            z.writestr("health.json", f"unavailable: {exc!r}")
        try:
            import urllib.request as _ur

            st = state.load()
            url = (st.get("runtime") or {}).get("url") or ""
            if url:
                q = json.dumps({"query": "احساس بی‌قراری و اضطراب", "top_k": 3})
                req = _ur.Request(f"{url}/search", data=q.encode(),
                                  headers={"Content-Type": "application/json"})
                with _ur.urlopen(req, timeout=30) as r:
                    z.writestr("sample-search.json", r.read().decode("utf-8", "replace"))
        except Exception as exc:
            z.writestr("sample-search.json", f"unavailable: {exc!r}")
    out.write_bytes(buf.getvalue())
    em.progress(log=f"diagnostics written: {out}")
    return out


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
    "index": step_index,
}


def main(argv: list[str] | None = None) -> int:
    # v1.3.1 zero-network activation FIRST — before any step (and before any
    # HF-adjacent import): the full variant sets the HF offline env switches
    # and installs the socket guard; lean builds are a no-op.
    try:
        from .net_guard import activate

        _ = activate()
    except Exception:  # the guard must never brick the wizard itself
        pass

    ap = argparse.ArgumentParser(prog="dehkhoda-server bootstrap")
    ap.add_argument("--step", default=None, choices=sorted(STEPS),
                    help="wizard step; inferred from the v1.3.1 index flags "
                         "when omitted")
    ap.add_argument("--consent", type=int, default=None, choices=(0, 1))
    ap.add_argument("--skip", action="store_true")
    ap.add_argument("--recreate", action="store_true")
    # v1.3.0 model-step modes (--step model)
    ap.add_argument("--preflight", action="store_true",
                    help="model: report-only pre-flight (disk 2x + HEAD 5s)")
    ap.add_argument("--start", action="store_true",
                    help="model: begin/resume the download")
    ap.add_argument("--restart", action="store_true",
                    help="model: discard .partial residue, start over")
    ap.add_argument("--check", action="store_true",
                    help="model: verify an existing dir, never download")
    ap.add_argument("--engine", default=None,
                    choices=("qdrant_local", "qdrant_native", "milvus_lite",
                             "milvus", "dense"),
                    help="engine-smoke: force a single engine probe "
                         "(dense = real e5 model probe)")
    ap.add_argument("--preferred", default=None,
                    help="engine-smoke: start the chain at this engine")
    # v1.3.1 index-step modes (--step index; --step inferred when omitted)
    ap.add_argument("--bundle-index", action="store_true",
                    help="index: force restore from the bundled snapshot")
    ap.add_argument("--bundle-dataset", action="store_true",
                    help="index: force rebuild from the bundled SQL")
    ap.add_argument("--verify-index", action="store_true",
                    help="index: verify index.ready + metadata + count "
                         "(exit 0/1)")
    ap.add_argument("--rebuild-index", action="store_true",
                    help="index: ignore the bundled snapshot, rebuild from "
                         "the bundled SQL (future model upgrades)")
    ap.add_argument("--diagnostics", action="store_true",
                    help="index: write dehkhoda-diagnostics.zip to the "
                         "data dir")
    ap.add_argument("--set", dest="set", default=None)
    args = ap.parse_args(argv)

    # v1.3.1: the index flags imply --step index
    if args.step is None:
        if args.bundle_index or args.bundle_dataset or args.verify_index \
                or args.rebuild_index or args.diagnostics:
            args.step = "index"
        else:
            ap.error("--step is required (or pass one of --bundle-index / "
                     "--bundle-dataset / --verify-index / --rebuild-index / "
                     "--diagnostics)")

    emit({"t": "hello", "step": args.step, "pid": os.getpid()})
    STEPS[args.step](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
