#!/usr/bin/env python3
"""Option C merge driver (v1.3.1) — scroll + upsert, never snapshot-chaining.

The earlier idea of feeding shard snapshots through recover_snapshot into ONE
collection was EMPIRICALLY FALSIFIED before implementation (same-name
recovery/collision semantics are not a merge primitive), so this driver:

  1. aggregates the 5 shard reports and asserts the partition + the two
     sums (SAFEGUARD 3): contiguous [0, 312506) coverage, sum(chunks) ==
     351,156 (+-0), sum(known_truncations) == 384 (+-0);
  2. restores every shard snapshot into its OWN temp collection shard_{i}
     (server mode REQUIRED — qdrant-client local mode has no snapshot API)
     and asserts its point count against the shard report;
  3. creates the production main collection via the REAL backend path
     (ensure_collection: dense 768 cosine + sparse IP + word index);
  4. scrolls each temp collection (with_vectors + with_payload) and upserts
     into main in DEHKHODA_MERGE_BATCH-sized batches (default 1,000 points —
     Qdrant's 32MiB JSON payload limit rejects ~57MB 5,000-point batches)
     with a progress line per batch:
         {"shard": i, "batch": j, "total_upserted": X, "total_target": 351156}
     per-shard cumulative assert; 60-min soft timeout -> WARNING + continue,
     90-min hard timeout -> FAIL ("something is wrong") — SAFEGUARD 4;
  5. SAFEGUARD 2 semantic probes on the MERGED collection — 3 fixed Persian
     reverse-dictionary queries, each MUST: return >=1 hybrid hit, top dense
     (exact=True) cosine score > 0.5, non-empty definition payload; top-3 of
     every query lands in the job log. Sparse-only zero hits = WARN (a real
     matching row can exist while its term is absent from the first-30k
     BM25 vocab — deviation #2), never a kill switch. ANY hard probe failure
     -> merge FAILS BEFORE the final snapshot is taken;
  6. takes the final snapshot, gzips it, sha256-verifies-or-defines the
     pins.prebuilt_index pin (PIN-FILL prints for CP3), and packs the
     index-snapshot artifact = snapshot + meta + bm25_stats.pkl.

Usage (CI merge job):
    DEHKHODA_DATA_DIR=$PWD/dh-merge python3 bootstrap/merge_index.py \
        --shards-artifacts shards/ --bm25-stats bm25_stats.pkl --out merge-artifacts
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

USABLE_ROWS = 312_506
EXPECTED_TOTAL_ROWS = 312_506
EXPECTED_TOTAL_CHUNKS = 351_156   # the canary: fixed chunker on the full corpus
EXPECTED_TOTAL_TRUNCATIONS = 384
# SAFEGUARD 4 batch size. 5000 points × ~11.4KB/point ≈ 57MB > Qdrant's
# 32MB JSON payload limit (HTTP 400 — attempts 1-4 root cause). 1000 ≈ ~14MB.
BATCH = int(os.environ.get("DEHKHODA_MERGE_BATCH", "1000"))
assert BATCH > 0 and BATCH <= 5000, f"DEHKHODA_MERGE_BATCH out of range: {BATCH}"
SOFT_TIMEOUT_S = 60 * 60          # WARNING, keep going
HARD_TIMEOUT_S = 90 * 60          # FAIL — something is wrong
N_SHARDS = 5

# SAFEGUARD 2 — fixed Persian reverse-dictionary probes (user-approved)
PROBES = [
    "احساس بی‌قراری و اضطراب",
    "حالت ترس و وحشت شدید",
    "کسی که بسیار سخاوتمند است",
]
MIN_DENSE_COSINE = 0.5            # deviation #1: dense channel only


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def die(msg: str) -> None:
    print("MERGE-FATAL:", msg, flush=True)
    raise SystemExit(1)


def qdrant_snapshots_dir() -> Path:
    """The server's snapshots dir (qdrant >= 1.20 refuses file:// outside it)."""
    import os

    from bootstrap import paths

    override = os.environ.get("DEHKHODA_QDRANT_STORAGE")
    base = Path(override) if override else paths.data_dir() / "qdrant-server"
    d = base / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


# =========================================================================
# 1. shard report aggregation (SAFEGUARD 3)
# =========================================================================
def aggregate_reports(art_dir: Path) -> list[dict]:
    reports = []
    for i in range(N_SHARDS):
        p = art_dir / f"shard_report_{i}.json"
        if not p.exists():
            die(f"shard report missing: {p}")
        reports.append(json.loads(p.read_text(encoding="utf-8")))
    reports.sort(key=lambda r: r["shard"])
    for i, r in enumerate(reports):
        if r["shard"] != i:
            die(f"shard report index mismatch at position {i}: {r['shard']}")

    total_rows = sum(r["rows_count"] for r in reports)
    if total_rows != EXPECTED_TOTAL_ROWS:
        die(f"sum(rows_count) {total_rows} != {EXPECTED_TOTAL_ROWS}")

    # contiguity: [0, USABLE) with no overlap and no gap
    pos = 0
    for r in reports:
        if r["start_row"] != pos:
            die(f"partition gap/overlap at shard {r['shard']}: "
                f"start {r['start_row']} != expected {pos}")
        pos = r["end_row"]
    if pos != EXPECTED_TOTAL_ROWS:
        die(f"partition end {pos} != {EXPECTED_TOTAL_ROWS}")

    total_chunks = sum(r["chunks"] for r in reports)
    if total_chunks != EXPECTED_TOTAL_CHUNKS:
        die(f"CANARY FAILED: sum(shard chunks) {total_chunks} != "
            f"{EXPECTED_TOTAL_CHUNKS} (+-0) — the fixed chunker produced a "
            "different number on the full corpus; STOP before any snapshot")
    total_trunc = sum(r["known_truncations"] for r in reports)
    if total_trunc != EXPECTED_TOTAL_TRUNCATIONS:
        die(f"sum(known_truncations) {total_trunc} != "
            f"{EXPECTED_TOTAL_TRUNCATIONS} (+-0)")
    print("MERGE-AGGREGATE " + json.dumps({
        "rows": total_rows, "chunks": total_chunks,
        "known_truncations": total_trunc,
        "per_shard_chunks": [r["chunks"] for r in reports],
        "per_shard_truncations": [r["known_truncations"] for r in reports]}),
        flush=True)
    return reports


# =========================================================================
# 2. restore shard snapshots into temp collections
# =========================================================================
def restore_shards(client, art_dir: Path, reports: list[dict]) -> None:
    from qdrant_client import models

    snap_dir = qdrant_snapshots_dir()
    for r in reports:
        i = r["shard"]
        gz = art_dir / f"shard_{i}.snapshot.gz"
        if not gz.exists():
            die(f"shard snapshot artifact missing: {gz}")
        gz_sha = sha256_file(gz)
        if gz_sha != r["snapshot_sha256"]:
            die(f"shard {i} snapshot sha256 {gz_sha} != report "
                f"{r['snapshot_sha256']} — artifact transfer corrupted")
        plain = snap_dir / f"shard_{i}.restore.snapshot"
        plain_sha = hashlib.sha256()
        with gzip.open(gz, "rb") as src, open(plain, "wb") as dst:
            for chunk in iter(lambda: src.read(1 << 22), b""):
                dst.write(chunk)
                plain_sha.update(chunk)
        tmp_name = f"shard_{i}"
        if client.collection_exists(tmp_name):
            client.delete_collection(tmp_name)
        try:
            prio = models.SnapshotPriority.SNAPSHOT
        except Exception:
            prio = None
        t0 = time.time()
        client.recover_snapshot(
            collection_name=tmp_name,
            location=plain.resolve().as_uri(),
            checksum=plain_sha.hexdigest(),
            priority=prio,
            wait=True,
        )
        count = client.count(tmp_name, exact=True).count
        if count != r["chunks"]:
            die(f"temp collection {tmp_name} has {count} points, shard report "
                f"says {r['chunks']}")
        print(f"restored shard_{i}: {count} points in {time.time()-t0:.1f}s",
              flush=True)
        plain.unlink(missing_ok=True)


# =========================================================================
# 4. scroll + upsert (SAFEGUARD 4)
# =========================================================================
def _vector_dict(v: "object") -> dict:
    """Normalize a scrolled vector payload to {dense: [...], sparse: SV}."""
    from qdrant_client import models

    if isinstance(v, dict):
        return v
    named = getattr(v, "vectors", None)  # NamedVectors container
    if named is not None and hasattr(named, "__dict__"):
        return dict(vars(named))
    return v  # type: ignore[return-value]


def merge_shard(client, tmp_name: str, main_name: str, shard_idx: int,
                expected: int, deadline0: float) -> int:
    from qdrant_client import models

    offset = None
    batch_no = 0
    upserted = 0
    warned_soft = False
    last_page_first_id = None
    max_batches = (expected // BATCH) * 3 + 20  # stall guard
    while True:
        elapsed = time.time() - deadline0
        if elapsed > HARD_TIMEOUT_S:
            die(f"hard timeout {HARD_TIMEOUT_S}s exceeded while merging shard "
                f"{shard_idx} (upserted {upserted}/{expected}) — something is "
                "wrong; FAIL")
        if elapsed > SOFT_TIMEOUT_S and not warned_soft:
            print(f"WARNING: merge soft timeout {SOFT_TIMEOUT_S}s exceeded "
                  f"(shard {shard_idx}, upserted {upserted}/{expected}) — "
                  "continuing per SAFEGUARD 4", flush=True)
            warned_soft = True
        if batch_no > max_batches:
            die(f"scroll pagination stalled on shard {shard_idx}: {batch_no} "
                f"batches for {expected} points — no forward progress; FAIL")
        points, offset = client.scroll(
            collection_name=tmp_name,
            limit=BATCH,
            offset=offset,
            with_vectors=True,
            with_payload=True,
        )
        if not points:
            break
        # SAFEGUARD 4 stall detection: a page whose first id repeats the
        # previous page's first id means the offset is not advancing
        first_id = points[0].id
        if last_page_first_id is not None and first_id == last_page_first_id:
            die(f"scroll offset not advancing on shard {shard_idx} (batch "
                f"{batch_no + 1} restarts at {first_id}) — something is wrong; FAIL")
        last_page_first_id = first_id
        batch = []
        for p in points:
            vec = _vector_dict(p.vector)
            dense = vec.get("dense")
            sparse = vec.get("sparse")
            if dense is None or sparse is None:
                die(f"point {p.id} in {tmp_name} is missing dense/sparse vectors")
            if not isinstance(sparse, models.SparseVector):
                sparse = models.SparseVector(
                    indices=list(sparse.indices), values=list(sparse.values))
            batch.append(models.PointStruct(
                id=p.id, vector={"dense": list(dense), "sparse": sparse},
                payload=p.payload))
        # defensive: measure the would-be REST JSON payload size so a 32MiB
        # rejection shows up in the log as bytes, not as a bare HTTP 400
        nbytes = sum(len(json.dumps(
            {"id": p.id, "payload": p.payload,
             "vector": {"dense": list(p.vector["dense"]),
                        "sparse": {"indices": list(p.vector["sparse"].indices),
                                   "values": list(p.vector["sparse"].values)}}},
            ensure_ascii=False).encode("utf-8")) for p in batch)
        try:
            client.upsert(main_name, points=batch, wait=True)
        except Exception as e:
            print(f"::error::MERGE-UPSERT-FAIL shard={shard_idx} "
                  f"batch={batch_no + 1} points={len(batch)} bytes={nbytes} "
                  f"first_id={points[0].id} err={type(e).__name__}: {e}",
                  flush=True)
            die(f"upsert failed on shard {shard_idx} batch {batch_no + 1} "
                f"({len(batch)} points, {nbytes} bytes): {e}")
        batch_no += 1
        upserted += len(batch)
        print(f"::notice::MERGE-UPSERT shard={shard_idx} batch={batch_no} "
              f"points={len(batch)} bytes={nbytes}", flush=True)
        print(f"MERGE-PROGRESS {{\"shard\": {shard_idx}, \"batch\": {batch_no}, "
              f"\"total_upserted\": {upserted}, \"total_target\": "
              f"{EXPECTED_TOTAL_CHUNKS}}}", flush=True)
        if offset is None:
            # qdrant contract: next_page_offset=None means END OF RESULTS —
            # even when the final page came back full. The old empty-page
            # check alone loops forever on exact-size pages (smoke-proven).
            break
    if upserted != expected:
        die(f"shard {shard_idx}: upserted {upserted} != expected {expected}")
    return upserted


# =========================================================================
# 5. SAFEGUARD 2 semantic probes (deviations #1/#2/#3)
# =========================================================================
def run_probes(client, collection: str, embedder, bm25, analyzer) -> list[dict]:
    from qdrant_client import models

    results = []
    for qi, query in enumerate(PROBES):
        cleaned = analyzer.normalize(query)
        dense = embedder.embed_query(cleaned)
        sparse = bm25.encode_query(cleaned)
        indices = sorted(sparse)
        values = [sparse[i] for i in indices]

        # dense channel, EXACT search (deviation #3: probes verify content,
        # not ANN recall; production runtime stays ANN)
        dres = client.query_points(
            collection_name=collection,
            query=dense, using="dense", limit=3, with_payload=True,
            search_params=models.SearchParams(exact=True),
        )
        dense_top = [
            {"score": float(p.score),
             "word": str((p.payload or {}).get("word", "")),
             "doc_id": str((p.payload or {}).get("doc_id", "")),
             "definition_head": str((p.payload or {}).get("definition", ""))[:160]}
            for p in dres.points
        ]
        # hybrid RRF channel, exact prefetches
        hres = client.query_points(
            collection_name=collection,
            prefetch=[
                models.Prefetch(query=dense, using="dense", limit=24,
                                params=models.SearchParams(exact=True)),
                models.Prefetch(query=models.SparseVector(indices=indices, values=values),
                                using="sparse", limit=24,
                                params=models.SearchParams(exact=True)),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=3, with_payload=True,
        )
        hybrid_top = [
            {"score": float(p.score),
             "word": str((p.payload or {}).get("word", ""))}
            for p in hres.points
        ]
        # sparse-only channel — WARN-only (deviation #2)
        sres = client.query_points(
            collection_name=collection,
            query=models.SparseVector(indices=indices, values=values),
            using="sparse", limit=3, with_payload=True,
            search_params=models.SearchParams(exact=True),
        )
        sparse_hits = len(sres.points)

        entry = {"query": query, "dense_top": dense_top,
                 "hybrid_top": hybrid_top, "sparse_only_hits": sparse_hits}
        results.append(entry)
        print("MERGE-PROBE " + json.dumps(entry, ensure_ascii=False), flush=True)

        if not dense_top:
            die(f"probe {qi + 1} ({query}): dense channel returned 0 hits")
        if dense_top[0]["score"] <= MIN_DENSE_COSINE:
            die(f"probe {qi + 1} ({query}): top dense cosine "
                f"{dense_top[0]['score']:.4f} <= {MIN_DENSE_COSINE}")
        if not dense_top[0]["definition_head"].strip():
            die(f"probe {qi + 1} ({query}): top hit has an empty definition "
                "payload — wrong-content-with-right-counts failure mode")
        if not hybrid_top:
            die(f"probe {qi + 1} ({query}): hybrid RRF returned 0 hits")
        if sparse_hits == 0:
            print(f"WARN: probe {qi + 1} ({query}): sparse-only channel "
                  "returned 0 hits — query term may be absent from the "
                  "first-30k BM25 vocab (deviation #2: WARN, not fatal)",
                  flush=True)
    return results


# =========================================================================
# 6. final snapshot + meta + PIN-FILL
# =========================================================================
def take_final_snapshot(client, collection: str, out_dir: Path,
                        bm25_sha: str, base_url: str) -> dict:
    from bootstrap.model_fetcher import load_pins

    pins = load_pins()["prebuilt_index"]
    t0 = time.time()
    snap = client.create_snapshot(collection, wait=True)
    name = snap.name
    print(f"final snapshot created: {name} in {time.time()-t0:.1f}s", flush=True)

    raw = out_dir / "dehkhoda.snapshot"
    url = f"{base_url}/collections/{collection}/snapshots/{name}"
    with urllib.request.urlopen(url, timeout=1800) as r, open(raw, "wb") as f:
        shutil.copyfileobj(r, f)
    gz = out_dir / "dehkhoda.snapshot.gz"
    with open(raw, "rb") as src, gzip.GzipFile(fileobj=open(gz, "wb"),
                                               compresslevel=9) as dst:
        shutil.copyfileobj(src, dst)
    raw.unlink()
    sha = sha256_file(gz)
    count = client.count(collection, exact=True).count

    from datetime import datetime, timezone

    meta = {
        "qdrant_version": pins["qdrant_version"],
        "model_revision": pins["embedding_model_revision"],
        "dataset_commit_sha": pins["dataset_commit_sha"],
        "collection_name": collection,
        "dense_dim": pins["dense_dim"],
        "distance": pins["distance"],
        "sparse_metric": pins["sparse_metric"],
        "record_count": count,
        "known_truncations": EXPECTED_TOTAL_TRUNCATIONS,
        "snapshot_sha256": sha,
        "snapshot_size_bytes": gz.stat().st_size,
        "bm25_stats_sha256": bm25_sha,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (out_dir / "prebuilt_index_meta.json").write_text(
        json.dumps(meta, indent=1), encoding="utf-8")
    print(json.dumps(meta, indent=1), flush=True)

    if pins.get("snapshot_sha256"):
        if sha != pins["snapshot_sha256"]:
            die(f"snapshot sha256 {sha} != pin {pins['snapshot_sha256']} "
                "(shard snapshots are cached; if ingestion code changed, "
                "bump the cache r<N> key)")
        print("snapshot verified against the committed pin", flush=True)
    else:
        print("PIN-FILL: record_count=" + str(count))
        print("PIN-FILL: snapshot_sha256=" + sha)
        print("PIN-FILL: snapshot_size_bytes=" + str(gz.stat().st_size))
        print("PIN-FILL: built_at=" + meta["built_at"])
    return meta


# ---------------------------------------------------------------------------
# Option A — snapshot ACQUISITION (byte-exact REUSE, not rebuild).
#
# Root cause (run 41178302834 MERGE-FATAL): Qdrant snapshot containers embed a
# runtime collection UUID and timestamps, so re-merging IDENTICAL shard data
# produces DIFFERENT archive bytes on every run (run 41143446238 -> 1718d119…,
# run 41178302834 -> 0ebf98a7…; same record_count/truncations/probe scores,
# container +1,304,288 bytes of entropy). The pin's semantic is therefore
# "the EXACT BYTES of the file we ship" — convergence requires REUSE, never
# REBUILD. A rebuild is only legitimate when the underlying data changes
# (model, chunker, corpus): a pin-bump event (new cache r<N> key), not a retry.

SNAPSHOT_PROBE_REFERENCE = [0.8556, 0.8596, 0.8540]  # pinned dense top scores
SNAPSHOT_PROBE_TOLERANCE = 0.01


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Opener that surfaces 3xx as HTTPError so we can read Location and
    re-request WITHOUT forwarding Authorization to the signed blob URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _api_get(url: str, token: str, timeout: int = 120):
    """GET with auth; returns (status, headers, body). No redirect follow."""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "dehkhoda-merge-index/1.1",
    })
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _identity_check(meta: dict, report: dict | None, pin: dict) -> None:
    """Fast data-identity assertion on an ACQUIRED snapshot (no engine needed).

    Re-verifies the data fields the SAFEGUARDs computed when the snapshot was
    built: record_count, known_truncations, and the 3 SAFEGUARD-2 probe dense
    top scores. Byte-identity (sha256 == pin) is asserted by the caller.
    """
    bad = []
    if meta.get("record_count") != pin.get("record_count"):
        bad.append(f"record_count={meta.get('record_count')} "
                   f"expected {pin.get('record_count')}")
    if meta.get("known_truncations") != pin.get("known_truncations"):
        bad.append(f"known_truncations={meta.get('known_truncations')} "
                   f"expected {pin.get('known_truncations')}")
    scores = (report or {}).get("probe_top_scores") or []
    if len(scores) != len(SNAPSHOT_PROBE_REFERENCE):
        bad.append(f"probe_top_scores={scores} expected "
                   f"{len(SNAPSHOT_PROBE_REFERENCE)} scores near "
                   f"{SNAPSHOT_PROBE_REFERENCE}")
    else:
        for i, (got, want) in enumerate(zip(scores, SNAPSHOT_PROBE_REFERENCE)):
            if abs(float(got) - want) > SNAPSHOT_PROBE_TOLERANCE:
                bad.append(f"probe_score[{i}]={got} expected {want} "
                           f"(+-{SNAPSHOT_PROBE_TOLERANCE})")
    if bad:
        for b in bad:
            print(f"::error::SNAPSHOT-IDENTITY-FAIL: {b}", flush=True)
        die("SNAPSHOT-IDENTITY-FAIL: " + "; ".join(bad))


def _emit_snapshot_source(src: str) -> None:
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write(f"snapshot_source={src}\n")


def _download_snapshot_artifact(pin: dict, out_dir: Path, token: str) -> None:
    """Download the pinned source artifact (index-snapshot) from the source
    run and unpack its files flat into out_dir (zip-slip safe)."""
    name = pin.get("source_artifact") or "index-snapshot"
    run_id = int(pin["source_run"])
    if not token:
        die(f"SNAPSHOT-SOURCE needs a token (GITHUB_TOKEN) to download "
            f"artifact {name} from run {run_id}")
    repo = os.environ.get("GITHUB_REPOSITORY", "montajebii/reverse-DehKhoda")
    list_url = (f"https://api.github.com/repos/{repo}"
                f"/actions/runs/{run_id}/artifacts?per_page=100")
    st, _h, body = _api_get(list_url, token)
    if st != 200:
        die(f"artifact list for run {run_id} -> HTTP {st}: {body[:200]!r}")
    arts = json.loads(body).get("artifacts", [])
    hit = next((a for a in arts
                if a["name"] == name and not a.get("expired")), None)
    if hit is None:
        die(f"artifact {name!r} not found unexpired on run {run_id}; "
            f"got {[a['name'] for a in arts]}")
    print(f"SNAPSHOT-SOURCE: downloading artifact {name} "
          f"(id={hit['id']}, {hit['size_in_bytes'] / 1e6:.1f} MB) "
          f"from run {run_id}", flush=True)
    zip_url = f"https://api.github.com/repos/{repo}/actions/artifacts/{hit['id']}/zip"
    st, headers, body = _api_get(zip_url, token, timeout=120)
    if st not in (301, 302, 303, 307):
        die(f"artifact zip request -> HTTP {st} (expected redirect): {body[:200]!r}")
    loc = headers.get("Location")
    if not loc:
        die("artifact zip redirect has no Location header")
    ztmp = out_dir / ".index-snapshot.zip"
    # signed blob URL: NO auth header (urllib forwards it on auto-redirect)
    with urllib.request.urlopen(loc, timeout=1800) as r, open(ztmp, "wb") as f:
        shutil.copyfileobj(r, f)
    with zipfile.ZipFile(ztmp) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            with zf.open(info) as src, open(out_dir / Path(info.filename).name, "wb") as f:
                shutil.copyfileobj(src, f)
    ztmp.unlink()
    print(f"SNAPSHOT-SOURCE: unpacked {[p.name for p in sorted(out_dir.iterdir())]}",
          flush=True)


def acquire_snapshot(pins_root: dict, out_dir: Path, token: str = "") -> str:
    """Materialize the byte-exact pinned snapshot in out_dir (Option A).

    Order: (1) actions/cache restore of out_dir (caller restores before this
    runs) -> cache-hit; (2) download the pinned source_run artifact;
    (3) nothing reusable -> signal REBUILD (caller runs the full merge path).
    Returns "cache" | "download" | "rebuild". For cache/download the snapshot
    sha256 == prebuilt_index.snapshot_sha256 AND the data-identity fields
    (record_count / known_truncations / probe top scores) are asserted before
    returning. Emits SNAPSHOT-SOURCE notices + GITHUB_OUTPUT.snapshot_source.
    """
    pin = pins_root["prebuilt_index"]
    out_dir.mkdir(parents=True, exist_ok=True)
    snap = out_dir / "dehkhoda.snapshot.gz"
    want = str(pin.get("snapshot_sha256") or "")

    def _load_meta_report():
        meta = json.loads((out_dir / "prebuilt_index_meta.json")
                          .read_text(encoding="utf-8"))
        rp = out_dir / "merge_report.json"
        report = (json.loads(rp.read_text(encoding="utf-8"))
                  if rp.exists() else None)
        return meta, report

    # 1) cache-hit: actions/cache restored out_dir before this ran
    if want and snap.exists():
        got = sha256_file(snap)
        if got == want:
            meta, report = _load_meta_report()
            _identity_check(meta, report, pin)
            print(f"::notice::SNAPSHOT-SOURCE: cache-hit sha={got}", flush=True)
            _emit_snapshot_source("cache")
            return "cache"

    # 2) byte-exact reuse from the pinned source run's artifact
    if want and pin.get("source_run"):
        _download_snapshot_artifact(pin, out_dir, token)
        got = sha256_file(snap)
        if got != want:
            print(f"::error::SNAPSHOT-IDENTITY-FAIL: snapshot_sha256={got} "
                  f"expected {want}", flush=True)
            die(f"SNAPSHOT-IDENTITY-FAIL: downloaded snapshot sha256 {got} "
                f"!= pin {want} (source artifact bytes drifted — refuse)")
        meta, report = _load_meta_report()
        _identity_check(meta, report, pin)
        print(f"::notice::SNAPSHOT-SOURCE: downloaded from run "
              f"{pin['source_run']} sha={got}", flush=True)
        _emit_snapshot_source("download")
        return "download"

    # 3) nothing reusable — caller rebuilds via the full merge path
    print("::notice::SNAPSHOT-SOURCE: rebuilding (no reusable pin)", flush=True)
    _emit_snapshot_source("rebuild")
    return "rebuild"


def acquire_cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="merge_index acquire",
        description="Materialize the pinned prebuilt snapshot: actions/cache "
                    "restore -> source_run artifact download -> rebuild signal.")
    ap.add_argument("--pins", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    pins_root = json.loads(args.pins.read_text(encoding="utf-8"))
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_PAT")
             or os.environ.get("PAT") or "")
    acquire_snapshot(pins_root, args.out, token)
    return 0


def main() -> int:
    from bootstrap.model_fetcher import load_pins

    ap = argparse.ArgumentParser()
    ap.add_argument("--shards-artifacts", required=True, type=Path)
    ap.add_argument("--bm25-stats", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    t0 = time.time()
    args.out.mkdir(parents=True, exist_ok=True)

    # ---- artifact-set gate (commit-5 CI assert) ----------------------------
    pins_root = load_pins()
    bm25_pin = str((pins_root.get("bm25_stats") or {}).get("sha256") or "")
    if not args.bm25_stats.exists():
        die(f"bm25 stats artifact missing: {args.bm25_stats}")
    bm25_sha = sha256_file(args.bm25_stats)
    if bm25_pin and bm25_sha != bm25_pin:
        die(f"bm25_stats.pkl sha256 {bm25_sha} != pin {bm25_pin}")
    print(f"bm25 stats verified: sha256={bm25_sha}", flush=True)

    # ---- 1. aggregate + assert (SAFEGUARD 3) -------------------------------
    reports = aggregate_reports(args.shards_artifacts)

    from bootstrap import state_env
    from app.analyzers.persian import PersianAnalyzer
    from app.config import load_settings
    from app.embeddings.provider import get_embedder
    from app.search.bm25 import BM25Encoder
    from app.vector_store.qdrant_backend import QdrantBackend
    from qdrant_client import QdrantClient

    # mirror the wizard/server path: seeded state -> DEHKHODA_* env -> settings
    state_env.apply_env()
    s = load_settings()
    main_name = s.collection_name
    client = QdrantClient(url=s.qdrant_url, timeout=300.0)

    # ---- 2. restore temp collections ---------------------------------------
    restore_shards(client, args.shards_artifacts, reports)

    # ---- 3. main collection via the REAL production path -------------------
    backend = QdrantBackend(s)
    backend.connect()
    backend.ensure_collection(dim=int(pins_root["embedding_model"]["dim"]),
                              recreate=True)
    backend.close()

    # ---- 4. scroll + upsert (SAFEGUARD 4) ----------------------------------
    upsert_t0 = time.time()
    total_upserted = 0
    for r in reports:
        i = r["shard"]
        n = merge_shard(client, f"shard_{i}", main_name, i, r["chunks"], upsert_t0)
        total_upserted += n
        print(f"::notice::MERGE-SHARD-DONE {{\"shard\": {i}, \"upserted\": {n}, "
              f"\"cumulative\": {total_upserted}, \"target\": "
              f"{EXPECTED_TOTAL_CHUNKS}}}", flush=True)

    live = client.count(main_name, exact=True).count
    if live != EXPECTED_TOTAL_CHUNKS:
        die(f"merged collection has {live} points != {EXPECTED_TOTAL_CHUNKS} "
            f"(upserted total {total_upserted})")
    print(f"MERGE-COUNT-OK {live} == {EXPECTED_TOTAL_CHUNKS}", flush=True)

    # ---- 5. SAFEGUARD 2 probes (before ANY snapshot) ------------------------
    analyzer = PersianAnalyzer(s.analyzer_engine)
    embedder = get_embedder(s)
    bm25 = BM25Encoder(analyzer, k1=s.bm25_k1, b=s.bm25_b,
                       stats_path=s.resolved_data_dir() / "bm25_stats.pkl")
    if not bm25.load():
        die("bm25.load() failed in merge — probes need the pinned stats")
    try:
        probe_results = run_probes(client, main_name, embedder, bm25, analyzer)
    except SystemExit:
        raise
    except Exception as e:
        print(f"::error::MERGE-PROBE-FAIL err={type(e).__name__}: {e}",
              flush=True)
        die(f"probes crashed before completing SAFEGUARD 2: {e}")

    # ---- 6. final snapshot + meta ------------------------------------------
    meta = take_final_snapshot(client, main_name, args.out, bm25_sha,
                               s.qdrant_url)

    # cleanup temp collections
    for r in reports:
        try:
            client.delete_collection(f"shard_{r['shard']}")
        except Exception:
            pass
    client.close()

    report = {
        "record_count": meta["record_count"],
        "snapshot_sha256": meta["snapshot_sha256"],
        "snapshot_size_bytes": meta["snapshot_size_bytes"],
        "chunks_total": total_upserted,
        "known_truncations_total": sum(r["known_truncations"] for r in reports),
        "probe_top_words": [p["dense_top"][0]["word"] for p in probe_results],
        "probe_top_scores": [p["dense_top"][0]["score"] for p in probe_results],
        "durations": {"merge_upsert_s": round(time.time() - upsert_t0, 1),
                      "total_s": round(time.time() - t0, 1)},
    }
    (args.out / "merge_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("MERGE_REPORT: " + json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "acquire":
        raise SystemExit(acquire_cli(sys.argv[2:]))
    raise SystemExit(main())
