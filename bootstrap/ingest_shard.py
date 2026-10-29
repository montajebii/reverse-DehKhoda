#!/usr/bin/env python3
"""Option C shard ingestion driver (v1.3.1) — one SOURCE-ROW range per CI job.

Why: the monolithic CI ingestion (run #33) is arithmetically infeasible —
e5 fp32 on a 4-vCPU runner ingests ~7 texts/s, so 351,156 chunks need ~13.9h
against a 6h wall. Five parallel shard jobs (~2.8h embedding each) close the
gap; the merge job (merge_index.py) then scrolls every shard collection and
upserts into the main one.

Determinism contract (SAFEGUARD 3 — user-approved, non-negotiable):
  * shards partition the USABLE staging rows POSITIONALLY over
    ``SELECT id FROM words ORDER BY id`` — no overlap, no gap, stable across
    runs: 312,506 rows -> base=62,501 +1 remainder to shard 0;
  * every shard job reports {start_row, end_row, rows_count, first_id,
    last_id} and FAILS on any partition violation;
  * the merge job asserts sum(rows_count) == 312,506, sum(chunks) == 351,156
    (the fixed-chunker replay total) and sum(known_truncations) == 384.

BM25 anti-poisoning: this script NEVER fits BM25. The pre-job
(scripts/build_bm25_stats.py) builds bm25_stats.pkl once for the whole
corpus and pins its sha256; the shard job verifies the file against the pin
BEFORE ingestion and the loader's bm25.load() picks it up. A missing or
hash-mismatched pkl is a hard STOP — a per-shard refit would silently poison
cross-shard ranking.

Chunker canary (shard level): the chunks are re-derived from the staging
rows and re-counted with the pinned e5 tokenizer ("passage: " prefix,
add_special_tokens=True, no truncation, >512 = truncated). The recount must
equal the ingested chunk count exactly — a fixed-chunker regression on the
full corpus can then only surface as a merge-time sum(351,156) failure,
never as a silently wrong snapshot.

Usage (CI ingest-shard job i of 5):
    DEHKHODA_DATA_DIR=$PWD/dh-shard python3 bootstrap/ingest_shard.py \
        --shard 0 --shards 5 --dumps dh-dumps/dumps \
        --bm25-stats bm25_stats.pkl --out shard-artifacts
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bootstrap.events import Emitter  # noqa: E402

USABLE_ROWS = 312_506
EXPECTED_TOTAL_CHUNKS = 351_156   # fixed-chunker full-corpus replay
MAX_SEQ_LEN = 512                 # sentence_bert_config.json (pinned model)
DOC_PREFIX = "passage: "          # e5 document prefix — MUST match production

SAMPLE_QUERY = "احساس بی‌قراری و اضطراب"   # SAFEGUARD 2 probe #1 (cheap shard variant)


def shard_range(shard: int, n_shards: int, total: int) -> tuple[int, int]:
    """Deterministic positional range [start, end) over `total` usable rows.

    base = total // n_shards; the remainder rows go to the LOWEST shards so
    the split is stable no matter which jobs finish first. For
    (5, 312506): shard 0 = [0, 62502), shard i = [i*62501 + 1, ...).
    """
    base, rem = divmod(total, n_shards)
    start = shard * base + min(shard, rem)
    end = start + base + (1 if shard < rem else 0)
    return start, end


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def die(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print("SHARD-FATAL:", msg, flush=True)
    raise SystemExit(1)


# -------------------------------------------------------------------------
# heartbeat: one observable line per minute (4h of silence is unmonitorable)
# -------------------------------------------------------------------------
def _start_heartbeat(get_count, stop: threading.Event) -> threading.Thread:
    def beat() -> None:
        t0 = time.time()
        while not stop.wait(60.0):
            try:
                n = get_count()
            except Exception:
                n = -1
            print(f"HEARTBEAT {{\"elapsed_s\": {int(time.time()-t0)}, "
                  f"\"chunks_so_far\": {n}}}", flush=True)
    th = threading.Thread(target=beat, daemon=True)
    th.start()
    return th


# -------------------------------------------------------------------------
# chunker recount + truncation count (pinned tokenizer, production prefix)
# -------------------------------------------------------------------------
def recount_chunks(loader, rows, tokenizer_path: Path) -> tuple[int, int]:
    """Re-derive every chunk the way ingest() does and tokenize it.

    Returns (total_chunks, chunks_over_512_tokens). The tokenizer is the
    PINNED one (sha256-verified model tarball); text = "passage: " + chunk,
    add_special_tokens=True, NO truncation — exactly the chunker_evidence
    methodology that produced 351,156 / 384 for the full corpus.
    """
    from tokenizers import Tokenizer

    tk = Tokenizer.from_file(str(tokenizer_path))
    total = 0
    over = 0
    B = 2048
    texts: list[str] = []
    for wid, word, meaning_clean, doc in rows:
        parts = loader.chunk_definition(meaning_clean)
        total += len(parts)
        texts.extend(DOC_PREFIX + p for p in parts)
        while len(texts) >= B:
            batch, texts = texts[:B], texts[B:]
            for e in tk.encode_batch(batch, add_special_tokens=True):
                if len(e.ids) > MAX_SEQ_LEN:
                    over += 1
    for e in tk.encode_batch(texts, add_special_tokens=True):
        if len(e.ids) > MAX_SEQ_LEN:
            over += 1
    return total, over


# -------------------------------------------------------------------------
# per-shard sample probe (SAFEGUARD 2, cheap variant — deviation #1/#2/#3)
# -------------------------------------------------------------------------
def sample_probe(client, collection: str, embedder, bm25, analyzer) -> dict:
    from qdrant_client import models

    cleaned = analyzer.normalize(SAMPLE_QUERY)
    dense = embedder.embed_query(cleaned)
    sparse = bm25.encode_query(cleaned)

    # dense channel — EXACT search (deviation #3: fresh bulk-insert HNSW
    # recall is a known qdrant limitation; the probe verifies CONTENT)
    res = client.query_points(
        collection_name=collection,
        query=dense,
        using="dense",
        limit=3,
        with_payload=True,
        search_params=models.SearchParams(exact=True),
    )
    dense_top = [
        {"score": float(p.score),
         "word": str((p.payload or {}).get("word", "")),
         "doc_id": str((p.payload or {}).get("doc_id", "")),
         "definition_head": str((p.payload or {}).get("definition", ""))[:120]}
        for p in res.points
    ]
    # hybrid RRF channel (exact prefetches as well)
    indices = sorted(sparse)
    values = [sparse[i] for i in indices]
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
        limit=3,
        with_payload=True,
    )
    hybrid_top = [
        {"score": float(p.score),
         "word": str((p.payload or {}).get("word", ""))}
        for p in hres.points
    ]
    return {"query": SAMPLE_QUERY, "dense_top": dense_top,
            "hybrid_top": hybrid_top}


def main() -> int:
    from bootstrap import dehkhoda_ingest, paths
    from bootstrap.model_fetcher import load_pins

    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, required=True)
    ap.add_argument("--shards", type=int, default=5)
    ap.add_argument("--dumps", required=True, type=Path)
    ap.add_argument("--bm25-stats", required=True, type=Path,
                    help="bm25_stats.pkl built by the pre-job (never refit here)")
    ap.add_argument("--out", required=True, type=Path,
                    help="artifact dir for the shard snapshot + report")
    args = ap.parse_args()

    em = Emitter("ingest-shard")
    t0 = time.time()
    shard, n_shards = args.shard, args.shards
    if not 0 <= shard < n_shards:
        die(f"shard index {shard} out of range for {n_shards} shards")
    if shard >= n_shards:
        die("too many shards")

    data_dir = paths.data_dir()
    print(f"SHARD-START shard={shard}/{n_shards} data_dir={data_dir}", flush=True)

    # ---- 1. BM25 anti-poison gate (BEFORE any ingestion) -------------------
    pins = load_pins()
    bm25_pin = str((pins.get("bm25_stats") or {}).get("sha256") or "")
    pkl_dst = data_dir / "bm25_stats.pkl"
    if not args.bm25_stats.exists():
        die(f"bm25 stats artifact missing: {args.bm25_stats}")
    actual_sha = sha256_file(args.bm25_stats)
    if bm25_pin and actual_sha != bm25_pin:
        die(f"bm25_stats.pkl sha256 {actual_sha} != pin {bm25_pin} — "
            "a per-shard refit would poison cross-shard ranking; STOP")
    shutil.copyfile(args.bm25_stats, pkl_dst)
    print(f"bm25 stats verified + staged: sha256={actual_sha}", flush=True)

    # ---- 2. staging (full corpus — cheap, per-file checkpoints) ------------
    dumps = sorted(Path(args.dumps).glob("*.sql.gz"))
    if len(dumps) != 33:
        die(f"expected 33 dumps, found {len(dumps)} in {args.dumps}")
    n = dehkhoda_ingest.build_staging(em, dumps)
    if n != USABLE_ROWS:
        die(f"staging rows {n} != pinned usable rows {USABLE_ROWS}")

    import sqlite3

    con = sqlite3.connect(data_dir / "dehkhoda_stage.db")
    all_ids = [r[0] for r in con.execute("SELECT id FROM words ORDER BY id")]
    con.close()
    if len(all_ids) != USABLE_ROWS:
        die(f"staging id list {len(all_ids)} != {USABLE_ROWS}")

    # ---- 3. SAFEGUARD 3: partition + slice ---------------------------------
    start_row, end_row = shard_range(shard, n_shards, USABLE_ROWS)
    rows_count = end_row - start_row
    shard_ids = all_ids[start_row:end_row]
    if len(shard_ids) != rows_count:
        die("id slice length mismatch")
    print("SHARD-PARTITION " + json.dumps({
        "shard": shard, "start_row": start_row, "end_row": end_row,
        "rows_count": rows_count, "first_id": shard_ids[0],
        "last_id": shard_ids[-1]}), flush=True)

    # ---- 4. ingest the shard range (EXACT production pipeline) -------------
    from bootstrap import state_env
    from app.analyzers.persian import PersianAnalyzer
    from app.config import load_settings
    from app.embeddings.provider import get_embedder
    from app.search.bm25 import BM25Encoder
    from app.ingestion.dehkhoda_loader import DehkhodaLoader
    from app.vector_store import backend_probe

    # mirror the wizard/server path: seeded state -> DEHKHODA_* env -> settings
    state_env.apply_env()
    s = load_settings()
    analyzer = PersianAnalyzer(s.analyzer_engine)
    backend = backend_probe(s)
    embedder = get_embedder(s)
    bm25 = BM25Encoder(analyzer, k1=s.bm25_k1, b=s.bm25_b,
                       stats_path=s.resolved_data_dir() / "bm25_stats.pkl")
    if not bm25.load():
        die("bm25.load() returned False — refusing to ingest with a refit")
    loader = DehkhodaLoader(s, analyzer, embedder, bm25, backend)

    stop = threading.Event()
    hb = _start_heartbeat(lambda: backend.count(), stop)
    stats = loader.ingest(ids=shard_ids, recreate=True)
    stop.set()
    hb.join(timeout=2)
    chunks = int(stats["chunks_inserted"])
    print("SHARD-INGEST-DONE " + json.dumps({
        "shard": shard, "entries": stats["entries"], "chunks": chunks,
        "elapsed_s": stats["elapsed_s"]}), flush=True)

    # ---- 5. collection count self-consistency ------------------------------
    col = s.collection_name
    live = backend.count()
    if live != chunks:
        die(f"collection {col} count {live} != inserted {chunks}")
    backend.close()

    # ---- 6. chunker canary: recount + truncation count ---------------------
    con = sqlite3.connect(data_dir / "dehkhoda_stage.db")
    rows = list(con.execute(
        "SELECT id, word, meaning_clean, doc FROM words ORDER BY id"))[
        start_row:end_row]
    con.close()
    if len(rows) != rows_count:
        die("recount row slice mismatch")
    rev = pins["embedding_model"]["revision"]
    tokenizer_path = paths.models_root() / rev / "tokenizer.json"
    if not tokenizer_path.exists():
        die(f"pinned tokenizer not found: {tokenizer_path}")
    recount_total, known_truncations = recount_chunks(loader, rows, tokenizer_path)
    if recount_total != chunks:
        die(f"chunker canary FAILED on shard {shard}: recount {recount_total} "
            f"!= ingested {chunks} — chunker drift; STOP before merge")
    print("SHARD-RECOUNT " + json.dumps({
        "shard": shard, "recount_chunks": recount_total,
        "known_truncations": known_truncations}), flush=True)

    # ---- 7. per-shard sample probe -----------------------------------------
    from qdrant_client import QdrantClient

    client = QdrantClient(url=s.qdrant_url, timeout=120.0)
    probe = sample_probe(client, col, embedder, bm25, analyzer)
    client.close()
    if not probe["dense_top"] or probe["dense_top"][0]["score"] <= 0.5:
        die(f"sample probe dense score {probe['dense_top'][:1]} <= 0.5 — STOP")
    if not probe["hybrid_top"]:
        die("sample probe hybrid returned 0 hits — STOP")
    top = probe["dense_top"][0]
    if not top["definition_head"].strip():
        die("sample probe top hit has an empty definition payload — STOP")
    print("SHARD-PROBE " + json.dumps(probe, ensure_ascii=False), flush=True)

    # ---- 8. snapshot the shard collection ----------------------------------
    client = QdrantClient(url=s.qdrant_url, timeout=600.0)
    snap = client.create_snapshot(col, wait=True)
    name = snap.name
    print(f"snapshot created: {name} in {time.time()-t0:.1f}s cumulative", flush=True)
    client.close()

    raw = Path(f"shard_{shard}.snapshot")
    url = f"{s.qdrant_url}/collections/{col}/snapshots/{name}"
    with urllib.request.urlopen(url, timeout=900) as r, open(raw, "wb") as f:
        shutil.copyfileobj(r, f)
    gz = args.out / f"shard_{shard}.snapshot.gz"
    args.out.mkdir(parents=True, exist_ok=True)
    with open(raw, "rb") as src, gzip.GzipFile(fileobj=open(gz, "wb"),
                                               compresslevel=9) as dst:
        shutil.copyfileobj(src, dst)
    raw.unlink()
    gz_sha = sha256_file(gz)

    report = {
        "shard": shard, "n_shards": n_shards,
        "start_row": start_row, "end_row": end_row,
        "rows_count": rows_count, "first_id": shard_ids[0],
        "last_id": shard_ids[-1],
        "chunks": chunks, "known_truncations": known_truncations,
        "bm25_sha256": actual_sha,
        "snapshot_sha256": gz_sha, "snapshot_size_bytes": gz.stat().st_size,
        "collection": col,
        "probe": {"query": probe["query"],
                  "dense_top_score": probe["dense_top"][0]["score"],
                  "dense_top_word": probe["dense_top"][0]["word"],
                  "hybrid_hits": len(probe["hybrid_top"])},
        "durations": {"total_s": round(time.time() - t0, 1),
                      "ingest_s": stats["elapsed_s"]},
    }
    (args.out / f"shard_report_{shard}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("SHARD_REPORT: " + json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
