"""index_restore LIVE test — real qdrant server, real snapshot round-trip.

This is the Step 7 local-sanity proof for the restore path:

  1. start the pinned qdrant binary (musl, from bootstrap pins) on a
     scratch port;
  2. build a MINI 'dehkhoda'-shaped collection (dense+sparse, a handful of
     points) — the real 312k-point snapshot is built by the CI
     build-index-snapshot job, the mechanics are identical;
  3. create_snapshot -> download -> gzip -> (restore) through
     index_restore.restore_bundled with the REAL recover_snapshot path;
  4. assert record_count, index.ready metadata, and verify_index(live).

Run:  python3 bootstrap/tests/test_index_restore_live.py [--keep-engine]
Skips cleanly when no qdrant binary is available (pass
DEHKHODA_QDRANT_LIVE_ENGINE=/path/to/qdrant to point at one).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from bootstrap import index_restore, model_fetcher, paths  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def find_engine() -> str:
    env = os.environ.get("DEHKHODA_QDRANT_LIVE_ENGINE", "")
    if env and Path(env).exists():
        return env
    local = Path("/tmp/dh-engine/qdrant")
    if local.exists():
        return str(local)
    # data-dir fallback (downloaded by the runtime fallback chain)
    cand = paths.bin_dir() / "qdrant"
    if cand.exists():
        return str(cand)
    return ""


def main() -> int:
    engine = find_engine()
    if not engine:
        print("SKIP: no qdrant binary available "
              "(set DEHKHODA_QDRANT_LIVE_ENGINE)")
        return 0

    from qdrant_client import QdrantClient, models

    pins = dict(index_restore.prebuilt_pins())
    port = 16333
    work = Path(tempfile.mkdtemp(prefix="dh-live-"))
    storage = work / "storage"
    storage.mkdir()
    env = {**os.environ,
           "QDRANT__SERVICE__HTTP_PORT": str(port),
           "QDRANT__SERVICE__HOST": "127.0.0.1",
           "QDRANT__STORAGE__STORAGE_PATH": str(storage),
           # snapshots_path is its OWN setting (default ./snapshots relative
           # to CWD) — pin it so index_restore._snapshots_dir() matches
           "QDRANT__STORAGE__SNAPSHOTS_PATH": str(storage / "snapshots"),
           "QDRANT__TELEMETRY_DISABLED": "true"}
    logf = open(work / "qdrant.log", "wb")
    proc = subprocess.Popen([engine], env=env, stdout=logf,
                            stderr=subprocess.STDOUT,
                            start_new_session=True)
    live_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{live_url}/readyz", timeout=2) as r:
                    if r.status in (200, 204):
                        break
            except OSError:
                time.sleep(0.4)
        else:
            raise RuntimeError("qdrant did not become ready in 60s")
        check("qdrant server ready on scratch port", True, live_url)

        # ---- build a mini production-shaped collection --------------------
        cname = pins.get("collection_name") or "dehkhoda"
        client = QdrantClient(url=live_url, timeout=60.0)
        client.create_collection(
            cname,
            vectors_config={"dense": models.VectorParams(
                size=4, distance=models.Distance.COSINE)},
            sparse_vectors_config={"sparse": models.SparseVectorParams(
                index=models.SparseIndexParams(on_disk=False))},
        )
        client.upsert(cname, [
            models.PointStruct(id=i, payload={"word": w, "doc": f"def {w}"},
                               vector={"dense": [i / 4, 0.5, 0.25, 0.125],
                                       "sparse": models.SparseVector(
                                           indices=[i % 7], values=[1.0])})
            for i, w in enumerate(["آب", "آتش", "خاک", "باد"], start=1)
        ], wait=True)
        truth = client.count(cname, exact=True).count
        check("mini collection built", truth == 4, f"{truth} points")

        # ---- snapshot -> gzip (the CI job's exact steps) -------------------
        snap = client.create_snapshot(cname, wait=True)
        raw = work / "mini.snapshot"
        with urllib.request.urlopen(
                f"{live_url}/collections/{cname}/snapshots/{snap.name}",
                timeout=120) as r, open(raw, "wb") as f:
            shutil.copyfileobj(r, f)
        gz = work / "dehkhoda.snapshot.gz"
        with open(raw, "rb") as src, gzip.GzipFile(fileobj=open(gz, "wb"),
                                                   compresslevel=9) as dst:
            shutil.copyfileobj(src, dst)
        raw.unlink()
        gz_sha = hashlib.sha256(gz.read_bytes()).hexdigest()
        client.delete_collection(cname)  # prove restore truly recreates
        client.close()

        # ---- restore through index_restore (REAL recover path) ------------
        # the staged plain tar must live inside the LIVE server's snapshots
        # dir (qdrant >= 1.20 refuses file:// recovery from outside it)
        os.environ["DEHKHODA_QDRANT_STORAGE"] = str(storage)
        pins2 = dict(pins, snapshot_sha256=gz_sha,
                     snapshot_size_bytes=gz.stat().st_size,
                     record_count=truth)
        patch = index_restore.prebuilt_pins
        index_restore.prebuilt_pins = lambda: dict(pins2)  # type: ignore[assignment]
        orig_client = index_restore._native_client
        index_restore._native_client = lambda timeout=600.0: (  # type: ignore[assignment]
            QdrantClient(url=live_url, timeout=120.0), live_url)
        stages: list[str] = []
        try:
            res = index_restore.restore_bundled(
                snapshot_path=gz, data_dir=work,
                on_progress=lambda p: stages.append(p["stage"]))
            check("restore_bundled: mode + record_count",
                  res["mode"] == "restored" and res["record_count"] == truth,
                  f"{res['record_count']} in {res['duration_ms']}ms")
            check("all 5 progress stages fired",
                  set(stages) == {"verify", "extract", "restore", "count", "ready"})
            ready = index_restore.read_ready_meta(work)
            check("index.ready written with matching pin metadata",
                  ready.get("record_count") == truth
                  and ready.get("snapshot_sha256") == gz_sha
                  and ready.get("mode") == "restored")
            ok, detail = index_restore.verify_index(work, live=True)
            check("verify_index(live) passes with matching count",
                  ok and detail.get("live_record_count") == truth,
                  json.dumps({k: detail.get(k) for k in
                              ("state", "live_record_count", "match",
                               "live_error")}))

            # hybrid search on the restored collection (Test A mechanics)
            c2 = QdrantClient(url=live_url, timeout=30.0)
            res_q = c2.query_points(
                cname,
                prefetch=[
                    models.Prefetch(query=[1.0, 0.5, 0.25, 0.125],
                                    using="dense", limit=2),
                    models.Prefetch(query=models.SparseVector(
                        indices=[1], values=[1.0]), using="sparse", limit=2),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=2, with_payload=True)
            check("hybrid search returns results after restore",
                  len(res_q.points) >= 1)
            c2.close()
        finally:
            index_restore.prebuilt_pins = patch  # type: ignore[assignment]
            index_restore._native_client = orig_client  # type: ignore[assignment]
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass
        logf.close()
        if not os.environ.get("DH_LIVE_KEEP"):
            shutil.rmtree(work, ignore_errors=True)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
