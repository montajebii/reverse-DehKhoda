"""Prebuilt-index restore + offline rebuild — v1.3.1 full variant.

Three first-run behaviors behind one dictionary step (and one launch hook):

  restore_bundled()          gunzip-verify the bundled Qdrant snapshot and
                             restore it into the native sidecar (server
                             mode is REQUIRED — qdrant-client local mode
                             does not support snapshots at all). Target:
                             <60s end to end.
  rebuild_from_bundled_sql() the EXISTING production ingestion path, fed
                             from the bundled 33 .sql.gz dumps instead of
                             the network. Also the never-blocked fallback
                             for any restore failure.
  (lean)                     the existing download + ingest path, untouched.

index_state()/index.ready: the marker carries {qdrant_version,
model_revision, dataset_sha, record_count, snapshot_sha256} mirroring
pins.prebuilt_index; "stale" = present but mismatched. Subsequent runs
reach ready in <2s by reading the marker alone.

NEVER-SILENT rule: every branch emits an explicit dataset event
    {step: "dataset", mode: "restored" | "rebuilt_from_bundle" |
     "downloaded", record_count, duration_ms, source_sha256}
and any restore failure logs the exact error before falling through to
the rebuild path — the user is never blocked and never misled.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import paths, state

REQUIRED_READY_KEYS = ("qdrant_version", "model_revision", "dataset_sha",
                       "record_count")

ProgressFn = Callable[[dict], None]


class RestoreNeedsServerError(RuntimeError):
    """The bundled snapshot can only be restored into the native qdrant
    sidecar (server mode); qdrant-client local mode has no snapshot API."""


# =========================================================================
# pins + marker helpers
# =========================================================================
def prebuilt_pins() -> dict:
    from .model_fetcher import load_pins

    return load_pins().get("prebuilt_index") or {}


def read_ready_meta(data_dir: Optional[Path] = None) -> dict:
    """Parse data_dir/index.ready — {} when absent or corrupt."""
    base = data_dir or paths.data_dir()
    try:
        raw = (base / "index.ready").read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_ready(data_dir: Path, meta: dict) -> None:
    base = data_dir or paths.data_dir()
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / "index.ready.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(base / "index.ready")


def _pins_match(meta: dict, pins: dict) -> bool:
    return (
        bool(meta) and all(k in meta for k in REQUIRED_READY_KEYS)
        and int(meta.get("record_count") or 0) > 0
        and str(meta.get("qdrant_version")) == str(pins.get("qdrant_version"))
        and str(meta.get("model_revision")) == str(pins.get("embedding_model_revision"))
        and str(meta.get("dataset_sha")) == str(pins.get("dataset_commit_sha"))
        and (not pins.get("record_count")
             or int(meta["record_count"]) == int(pins["record_count"]))
    )


def index_state(data_dir: Optional[Path] = None) -> str:
    """absent | partial | ready | stale (marker-level, no engine contact).

    "partial" = a marker exists but is corrupt/incomplete (an interrupted
    write; writes are atomic tmp+replace so this is rare).
    "stale"   = complete marker whose metadata does not match the pins
                (qdrant upgraded, model changed, dataset changed).
    """
    base = data_dir or paths.data_dir()
    if not (base / "index.ready").exists():
        return "absent"
    meta = read_ready_meta(base)
    if not meta or any(k not in meta for k in REQUIRED_READY_KEYS) or \
            int(meta.get("record_count") or 0) <= 0:
        return "partial"
    try:
        pins = prebuilt_pins()
    except Exception:
        return "partial"
    if not pins:
        return "stale"
    return "ready" if _pins_match(meta, pins) else "stale"


def write_ready(data_dir: Path, record_count: int, mode: str,
                source_sha256: str, duration_ms: int,
                snapshot_sha256: str = "") -> dict:
    pins = prebuilt_pins()
    meta = {
        "qdrant_version": str(pins.get("qdrant_version") or ""),
        "model_revision": str(pins.get("embedding_model_revision") or ""),
        "dataset_sha": str(pins.get("dataset_commit_sha") or ""),
        "record_count": int(record_count),
        "snapshot_sha256": snapshot_sha256 or str(pins.get("snapshot_sha256") or ""),
        "mode": mode,
        "source_sha256": source_sha256,
        "duration_ms": int(duration_ms),
        "restored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "record_count_verified": bool(pins.get("record_count")),
    }
    _write_ready(data_dir, meta)
    return meta


# =========================================================================
# engine access (server mode REQUIRED for snapshot restore)
# =========================================================================
def _native_client(timeout: float = 600.0) -> tuple[Any, str]:
    """A QdrantClient pointed at the RUNNING native sidecar, or raise."""
    from . import backend_launcher

    st = state.load()
    runtime = st.get("runtime") or {}
    engine = runtime.get("engine") or st.get("backend_active") or ""
    url = runtime.get("url") or ""
    if engine == "qdrant_native" and url:
        from qdrant_client import QdrantClient

        return QdrantClient(url=url, timeout=timeout), url
    # no usable server runtime recorded — bring the chain up (the full
    # variant prefers the native sidecar when the bundle has an index)
    runtime = backend_launcher.ensure_runtime()
    if runtime.get("engine") == "qdrant_native" and runtime.get("url"):
        from qdrant_client import QdrantClient

        return QdrantClient(url=runtime["url"], timeout=timeout), runtime["url"]
    raise RestoreNeedsServerError(
        f"snapshot restore needs the native qdrant sidecar (server mode); "
        f"runtime engine is {runtime.get('engine')!r}")


# =========================================================================
# bundled BM25 stats installation (v1.3.1 Option C runtime closure)
# =========================================================================
def install_bundled_bm25(data_dir: Optional[Path] = None) -> dict:
    """Copy the bundled bm25_stats.pkl into the data dir — sha256-verified.

    The restored Qdrant snapshot carries the vectors but NOT the BM25 IDF
    statistics; the sidecar loads them from <data_dir>/bm25_stats.pkl
    (app.main). Without this copy the full variant would silently refit BM25
    on runtime data (or run with no sparse ranking at all) — breaking the
    zero-network, fully-working promise of v1.3.1. NEVER silent: a missing
    or hash-mismatched bundle pkl raises loudly (Persian message first —
    this is a user-facing desktop app).
    """
    from .model_fetcher import load_pins

    base = data_dir or paths.data_dir()
    pins_root = load_pins()
    expected = str((pins_root.get("bm25_stats") or {}).get("sha256") or "")
    src = paths.bundled_bm25_stats()
    if not src:
        raise RuntimeError(
            "\u0641\u0627\u06cc\u0644 \u0622\u0645\u0627\u0631\u0647\u0654 BM25 \u062f\u0631 \u0628\u0633\u062a\u0647 \u06cc\u0627\u0641\u062a \u0646\u0634\u062f "
            "(_internal/index/bm25_stats.pkl). \u0627\u06cc\u0646\u062f\u06a9\u0633 \u0628\u0627\u0632\u06cc\u0627\u0628\u06cc\u200c\u0634\u062f\u0647 \u0628\u062f\u0648\u0646 "
            "\u0622\u0646 \u0628\u0647\u200c\u0637\u0648\u0631 \u067e\u0646\u0647\u0627\u0646\u06cc \u0628\u0647\u200c\u0631\u0648\u0632\u0631\u0633\u0627\u0646\u06cc \u0645\u06cc\u200c\u0634\u0648\u062f \u2014 \u0627\u062c\u0627\u0632\u0647 \u0646\u062f\u0627\u0631\u062f. "
            f"(bundled bm25_stats.pkl missing; pinned sha256={expected or '<none>'})")
    actual = _sha256_file(src)
    if expected and actual != expected:
        raise RuntimeError(
            "\u0622\u0645\u0627\u0631\u0647\u0654 BM25 \u062f\u0631 \u0628\u0633\u062a\u0647 \u0628\u0627 \u0642\u0644\u0627\u0628 \u0645\u0637\u0627\u0628\u0642\u062a \u0646\u062f\u0627\u0631\u062f. "
            f"(bundled bm25_stats.pkl sha256 mismatch: expected {expected}, "
            f"got {actual})")
    base.mkdir(parents=True, exist_ok=True)
    dst = base / "bm25_stats.pkl"
    if not (dst.exists() and _sha256_file(dst) == actual):
        tmp = base / "bm25_stats.pkl.tmp"
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
    return {"installed": str(dst), "sha256": actual, "source": str(src)}


# =========================================================================
# restore from the bundled snapshot
# =========================================================================
def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def _snapshots_dir() -> Path:
    """The sidecar server's snapshots directory.

    qdrant >= 1.20 REFUSES file:// snapshot recovery from outside its own
    snapshots dir ("Forbidden: Snapshot file path must be inside the
    snapshots directory") — the launcher always starts the sidecar with
    storage <data>/qdrant-server, so the gunzipped tar is staged there.
    """
    import os

    override = os.environ.get("DEHKHODA_QDRANT_STORAGE")
    base = Path(override) if override else paths.data_dir() / "qdrant-server"
    d = base / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_snapshot(explicit: Optional[Path]) -> Path:
    if explicit:
        if not explicit.exists():
            raise FileNotFoundError(f"snapshot not found: {explicit}")
        return explicit
    bundled = paths.bundled_snapshot()
    if bundled:
        return bundled
    env = None
    import os

    env = os.environ.get("DEHKHODA_INDEX_SNAPSHOT")
    if env and Path(env).exists():
        return Path(env)
    raise FileNotFoundError(
        "no bundled index snapshot found (_internal/index/dehkhoda.snapshot.gz)")


def restore_bundled(snapshot_path: Optional[Path] = None,
                    data_dir: Optional[Path] = None,
                    on_progress: Optional[ProgressFn] = None,
                    recreate: bool = True) -> dict:
    """Verify + gunzip the bundled snapshot and restore it into the sidecar.

    Progress stages reported via on_progress({bytes_done, bytes_total,
    duration_ms, stage}): verify -> extract -> restore -> count -> ready.
    Raises on ANY failure (sha mismatch, engine not server-mode, count
    mismatch) — the caller decides the fallback; nothing here pretends.
    """
    t0 = time.time()
    base = data_dir or paths.data_dir()
    pins = prebuilt_pins()

    def prog(stage: str, done: int = 0, total: int = 0) -> None:
        if on_progress:
            on_progress({"bytes_done": done, "bytes_total": total,
                         "duration_ms": int((time.time() - t0) * 1000),
                         "stage": stage})

    # ---- 1. resolve + sha256-verify (bytes_total = gz size) -------------
    snap = _resolve_snapshot(snapshot_path)
    gz_bytes_total = snap.stat().st_size
    prog("verify", 0, gz_bytes_total)
    expected = str(pins.get("snapshot_sha256") or "")
    if not expected:
        sidecar = snap.with_suffix(snap.suffix + ".sha256")
        if sidecar.exists():
            expected = sidecar.read_text(encoding="utf-8").strip().split()[0]
    actual_sha = _sha256_file(snap)
    if expected and actual_sha != expected:
        raise RuntimeError(
            f"bundled snapshot sha256 mismatch: expected {expected}, "
            f"got {actual_sha} ({snap})")
    prog("verify", gz_bytes_total, gz_bytes_total)

    # ---- 2. gunzip into the SERVER's snapshots dir (+ plain-tar sha) ----
    # qdrant >= 1.20 only accepts file:// recovery from inside its own
    # snapshots directory — stage the tar there, zero-copy for the engine.
    prog("extract", 0, gz_bytes_total)
    snap_dir = _snapshots_dir()
    plain = snap_dir / "dehkhoda.restore.snapshot"
    plain_sha = hashlib.sha256()
    with gzip.open(snap, "rb") as src, open(plain, "wb") as dst:
        for chunk in iter(lambda: src.read(1 << 22), b""):
            dst.write(chunk)
            plain_sha.update(chunk)
    prog("extract", gz_bytes_total, gz_bytes_total)

    try:
        # ---- 3. clean local sidecar, server mode REQUIRED ---------------
        prog("restore", 0, gz_bytes_total)
        client, url = _native_client()
        cname = str(pins.get("collection_name") or "dehkhoda")
        if recreate and client.collection_exists(cname):
            client.delete_collection(cname)
        from qdrant_client import models  # noqa: F401  (import probe)

        try:
            prio = models.SnapshotPriority.SNAPSHOT
        except Exception:
            prio = None
        client.recover_snapshot(
            collection_name=cname,
            location=plain.resolve().as_uri(),
            checksum=plain_sha.hexdigest(),
            priority=prio,
            wait=True,
        )

        # ---- 4. verify record count (fail LOUDLY on truncation) --------
        prog("count", 0, gz_bytes_total)
        count = client.count(cname, exact=True).count
        client.close()
        pinned = int(pins.get("record_count") or 0)
        if pinned and count != pinned:
            raise RuntimeError(
                f"restored index has {count} records but the pin says "
                f"{pinned} — refusing to mark ready (truncated index is "
                "worse than no index)")
        prog("count", gz_bytes_total, gz_bytes_total)

        # ---- 5. install the bundled BM25 stats (NEVER silent) -----------
        prog("ready", gz_bytes_total, gz_bytes_total)
        bm25_install = install_bundled_bm25(base)

        # ---- 6. write the marker ---------------------------------------
        meta = write_ready(base, count, "restored", actual_sha,
                           int((time.time() - t0) * 1000), actual_sha)
        meta["bm25_sha256"] = bm25_install["sha256"]
        _write_ready(base, meta)
        return {"mode": "restored", "record_count": count, "url": url,
                "snapshot_sha256": actual_sha, "snapshot_size_bytes": gz_bytes_total,
                "bm25_sha256": bm25_install["sha256"],
                "duration_ms": meta["duration_ms"], "source_sha256": actual_sha}
    finally:
        # remove the staged plain tar (the snapshots dir must not accumulate
        # gigabytes of restore scratch)
        try:
            plain.unlink(missing_ok=True)
        except OSError:
            pass


# =========================================================================
# rebuild from the bundled SQL dumps (offline, future-model extensibility)
# =========================================================================
def rebuild_from_bundled_sql(data_dir: Optional[Path] = None,
                             on_progress: Optional[ProgressFn] = None,
                             dumps_dir: Optional[Path] = None,
                             recreate: bool = False) -> dict:
    """Run the EXISTING production ingestion from the bundled .sql.gz files.

    This is the path future releases use when the model or vector config
    changes (spec PART 4): fully offline, checkpointed, backend-agnostic
    (works on qdrant native/local and milvus alike).
    """
    from . import dehkhoda_ingest
    from .events import Emitter

    t0 = time.time()
    base = data_dir or paths.data_dir()
    src = dumps_dir or paths.bundled_dumps_dir()
    if not src:
        raise FileNotFoundError(
            "no bundled SQL dumps found (_internal/dumps/*.sql.gz) — "
            "rebuild-from-bundle requires DEHKHODA_BUNDLE_DATASET")
    files = sorted(Path(src).glob("*.sql.gz"))
    if not files:
        raise FileNotFoundError(f"bundled dumps dir has no .sql.gz files: {src}")

    em = Emitter("dictionary")
    stats = dehkhoda_ingest.run(em, source_dir=src, recreate=recreate)
    count = int(stats.get("chunks") or stats.get("count") or 0)
    pins = prebuilt_pins()
    pinned = int(pins.get("record_count") or 0)
    if pinned and count != pinned:
        raise RuntimeError(
            f"rebuilt index has {count} chunks but the pin says {pinned} — "
            "vector config drift; refusing to mark ready")
    meta = write_ready(base, count, "rebuilt_from_bundle",
                       str(pins.get("dataset_commit_sha") or ""),
                       int((time.time() - t0) * 1000))
    return {"mode": "rebuilt_from_bundle", "record_count": count,
            "entries": stats.get("entries"), "duration_ms": meta["duration_ms"],
            "source_sha256": meta["source_sha256"],
            "dumps": len(files)}


# =========================================================================
# verification
# =========================================================================
def verify_index(data_dir: Optional[Path] = None, live: bool = True) -> tuple[bool, dict]:
    """index.ready metadata + (best-effort) live record count. Exit 0/1 use."""
    base = data_dir or paths.data_dir()
    st = index_state(base)
    detail: dict[str, Any] = {"state": st, "marker": read_ready_meta(base)}
    if st != "ready":
        return False, detail
    if not live:
        return True, detail
    try:
        try:
            client, url = _native_client(timeout=30.0)
            cname = str(prebuilt_pins().get("collection_name") or "dehkhoda")
            count = client.count(cname, exact=True).count
            client.close()
        except RestoreNeedsServerError:
            # non-qdrant-native engines: count via the backend abstraction
            from app.vector_store import backend_probe
            from app.config import load_settings

            c = backend_probe(load_settings())
            count = c.count()
            c.close()
        detail["live_record_count"] = count
        marker_count = int(detail["marker"].get("record_count") or 0)
        detail["match"] = count == marker_count
        return count == marker_count, detail
    except Exception as exc:  # engine unreachable: metadata-only verdict
        detail["live_error"] = repr(exc)
        return True, detail


# =========================================================================
# the branch driver (wired into cli.step_ingest + backend_launcher.launch)
# =========================================================================
def bundle_capabilities() -> dict:
    return {
        "manifest": paths.bundled_manifest(),
        "model": paths.bundled_model_dir() is not None,
        "dataset": paths.bundled_dumps_dir() is not None,
        "index": paths.bundled_snapshot() is not None,
        "index_state": index_state(),
    }


def emit_dataset_event(em: Any, event: dict) -> None:
    payload = {"step": "dataset", **event}
    em.progress(task_fa="آماده‌سازی لغت‌نامه", log=f"dataset event: {json.dumps(payload)}",
                data=payload)


def ensure_index(em: Any, force_mode: Optional[str] = None,
                 recreate: bool = False) -> dict:
    """One dictionary step, three behaviors — never silently falls back.

    force_mode: None (auto per bundle) | "restored" | "rebuilt_from_bundle"
                | "downloaded" (the lean path, forced).
    """
    from .events import Emitter

    caps = bundle_capabilities()
    st = index_state()
    em.progress(log=f"index ensure: caps={caps} state={st}")

    auto = force_mode or (
        "restored" if (caps["index"] and st != "ready") else
        "rebuilt_from_bundle" if (caps["dataset"] and st != "ready") else
        "downloaded")

    if st == "ready" and force_mode is None:
        # fast path: marker matches pins — <2s, no engine contact
        meta = read_ready_meta()
        result = {"mode": meta.get("mode", "restored"),
                  "record_count": meta.get("record_count"),
                  "fast_path": True, "duration_ms": 0,
                  "source_sha256": meta.get("snapshot_sha256", "")}
        emit_dataset_event(em, result)
        return result

    if auto == "restored":
        try:
            t0 = time.time()

            def prog(p: dict) -> None:
                stage_fa = {"verify": "بررسی ایندکس همراه…",
                            "extract": "بازکردن ایندکس فشرده…",
                            "restore": "بازیابی ایندکس آماده…",
                            "count": "شمارش مدخل‌ها…",
                            "ready": "آماده‌سازی نهایی…"}.get(
                    p.get("stage"), "بازیابی ایندکس…")
                pct = int(100 * p["bytes_done"] / p["bytes_total"]) \
                    if p.get("bytes_total") else None
                em.progress(task_fa=stage_fa, pct=pct,
                            log=f"restore stage={p.get('stage')} "
                                f"{p.get('bytes_done')}/{p.get('bytes_total')} "
                                f"{p.get('duration_ms')}ms")

            result = restore_bundled(data_dir=paths.data_dir(), on_progress=prog)
            result["duration_ms"] = int((time.time() - t0) * 1000)
            emit_dataset_event(em, result)
            st_idx = state.set_value("index", {
                "mode": "restored", "record_count": result["record_count"],
                "source_sha256": result["snapshot_sha256"]})
            em.progress(task_fa="ایندکس آماده بازیابی شد",
                        log=f"restored {result['record_count']} records in "
                            f"{result['duration_ms']}ms")
            return result
        except Exception as exc:
            # NEVER-SILENT fallback: log the exact error, then rebuild
            import traceback

            em.progress(log="snapshot restore FAILED: " + repr(exc))
            em.progress(log="".join(traceback.format_exception(exc))[-1500:])
            if not caps["dataset"]:
                raise
            em.progress(task_fa="بازیابی ناموفق بود؛ ساخت مجدد از داده‌های همراه…",
                        log="falling back to rebuild_from_bundled_sql (explicit, "
                            "not silent)")
            auto = "rebuilt_from_bundle"

    if auto == "rebuilt_from_bundle":
        t0 = time.time()
        result = rebuild_from_bundled_sql(recreate=recreate)
        result["duration_ms"] = int((time.time() - t0) * 1000)
        emit_dataset_event(em, result)
        state.set_value("index", {
            "mode": "rebuilt_from_bundle",
            "record_count": result["record_count"],
            "source_sha256": result["source_sha256"]})
        return result

    # ---- lean path: the existing download + ingest pipeline --------------
    from . import dehkhoda_ingest

    t0 = time.time()
    stats = dehkhoda_ingest.run(em, recreate=recreate)
    count = int(stats.get("chunks") or stats.get("count") or 0)
    pins = prebuilt_pins()
    write_ready(paths.data_dir(), count, "downloaded",
                str(pins.get("dataset_commit_sha") or ""),
                int((time.time() - t0) * 1000))
    result = {"mode": "downloaded", "record_count": count,
              "entries": stats.get("entries"),
              "duration_ms": int((time.time() - t0) * 1000),
              "source_sha256": str(pins.get("dataset_commit_sha") or "")}
    emit_dataset_event(em, result)
    state.set_value("index", {"mode": "downloaded", "record_count": count})
    return result
