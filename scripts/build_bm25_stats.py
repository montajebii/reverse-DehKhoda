#!/usr/bin/env python3
"""bm25_stats.pkl pre-job builder (v1.3.1 Option C).

Option C splits ingestion across 5 shard jobs; a per-shard BM25 fit would
silently poison cross-shard ranking (each shard would see a different IDF
sample). This script builds the stats ONCE for the whole corpus, exactly the
way the monolithic production path did, and the shard jobs load the pinned
pickle instead of fitting.

Fit-corpus parity (byte-exactness contract):
    the monolithic path (DehkhodaLoader.ingest) fits on
        iter_staging(limit=30_000) -> meaning_clean   # ORDER BY id LIMIT 30000
    This script replays EXACTLY that — same staging builder (per-file
    checkpoints, INSERT OR REPLACE), same row order, same field, same
    BM25Encoder.fit(sample_size=30_000) — so the pickle bytes are
    reproducible cross-process (proven twice locally before the pin was
    committed; CI must match pins.bm25_stats.sha256 or the pre-job dies
    fast).

Usage:
    # local first run (defines the pin):
    python3 scripts/build_bm25_stats.py --dumps scripts/work/dumps \
        --out /tmp/bm25_stats.pkl
    # CI (verifies against the committed pin):
    python3 scripts/build_bm25_stats.py --dumps dh-dumps/dumps \
        --out bm25_stats.pkl --expect-sha <pins.bm25_stats.sha256>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SAMPLE_SIZE = 30_000  # MUST equal the monolithic fit_bm25_sample default


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def main() -> int:
    from bootstrap import dehkhoda_ingest
    from bootstrap.events import Emitter

    ap = argparse.ArgumentParser()
    ap.add_argument("--dumps", required=True, type=Path,
                    help="dir with the 33 pinned .sql.gz files")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--expect-sha", default="",
                    help="fail when the built pickle does not hash to this")
    args = ap.parse_args()

    dumps = sorted(Path(args.dumps).glob("*.sql.gz"))
    if len(dumps) != 33:
        print(f"FATAL: expected 33 dumps, found {len(dumps)} in {args.dumps}",
              flush=True)
        return 1

    em = Emitter("bm25-stats")
    t0 = time.time()
    n = dehkhoda_ingest.build_staging(em, dumps)
    if n != dehkhoda_ingest.USABLE_ROWS:
        print(f"FATAL: staging rows {n} != {dehkhoda_ingest.USABLE_ROWS}",
              flush=True)
        return 1
    print(f"staging ready: {n} rows ({time.time()-t0:.0f}s)", flush=True)

    # ---- fit on the EXACT production sample --------------------------------
    from app.analyzers.persian import PersianAnalyzer
    from app.config import load_settings
    from app.search.bm25 import BM25Encoder

    s = load_settings()
    analyzer = PersianAnalyzer(s.analyzer_engine)
    bm25 = BM25Encoder(analyzer, k1=s.bm25_k1, b=s.bm25_b,
                       stats_path=s.resolved_data_dir() / "bm25_stats.pkl")

    # DehkhodaLoader.ingest fits on iter_staging(limit=30_000).meaning_clean —
    # replay that verbatim (same ORDER BY id, same field).
    from app.ingestion.dehkhoda_loader import DehkhodaLoader

    probe_loader = DehkhodaLoader.__new__(DehkhodaLoader)  # chunk/fit only path
    probe_loader.s = s
    sample = [m for (_, _, m, _) in probe_loader.iter_staging(limit=SAMPLE_SIZE)]
    if len(sample) != SAMPLE_SIZE:
        print(f"FATAL: fit sample {len(sample)} != {SAMPLE_SIZE}", flush=True)
        return 1
    fitted = bm25.fit(sample, sample_size=SAMPLE_SIZE)
    print(f"bm25 fitted on {fitted} docs", flush=True)

    # persist to the requested location (BM25Encoder.save uses its own
    # stats_path; save via the same pickle schema)
    stats = {
        "idf": bm25._ef.idf,
        "corpus_size": bm25._ef.corpus_size,
        "avgdl": bm25._ef.avgdl,
        "k1": bm25._ef.k1,
        "b": bm25._ef.b,
        "epsilon": bm25._ef.epsilon,
    }
    import pickle

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "wb") as fh:
        pickle.dump(stats, fh)
    sha = sha256_file(args.out)
    print("BM25-STATS " + json.dumps({
        "path": str(args.out), "bytes": args.out.stat().st_size,
        "corpus_size": stats["corpus_size"], "avgdl": stats["avgdl"],
        "sha256": sha}), flush=True)

    if args.expect_sha:
        if sha != args.expect_sha:
            print(f"FATAL: bm25_stats.pkl sha256 {sha} != expected "
                  f"{args.expect_sha} — dataset/analyzer/pymilvus drift; "
                  "do NOT ingest shards with these stats", flush=True)
            return 1
        print("bm25 stats verified against the committed pin", flush=True)
    else:
        print("PIN-FILL: bm25_stats.sha256=" + sha, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
