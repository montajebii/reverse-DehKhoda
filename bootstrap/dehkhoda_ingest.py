"""Idempotent, checkpoint-resumable Dehkhoda ingestion driver.

Acceptance requirement: killing the app mid-ingest and restarting resumes
from the last checkpoint, not from zero. Two checkpoint layers:

  1. dumps      — .part resume inside model_fetcher (per-file sizes pinned)
  2. staging    — per-dump-file checkpoint rows in the staging DB
                  (`bootstrap_ck` table); inserts are INSERT OR REPLACE, so
                  re-processing a file is always safe
  3. vectors    — a `vector_last_id` watermark: staging ids are processed in
                  ascending chunks via loader.ingest(ids=...); doc ids are
                  deterministic ("w<id>c<k>"), so re-inserting an overlap
                  window upserts instead of duplicating.

Heavy app imports (hazm, pymilvus, sentence_transformers) stay lazy — the
CLI step must start fast inside the bundled runtime.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from . import model_fetcher, paths, state
from .events import Emitter

CK_TABLE = "bootstrap_ck"
# keep in sync with app.ingestion.dehkhoda_loader
USABLE_ROWS = 312_506
ID_CHUNK = 25_000


# =========================================================================
# checkpoints (live in the staging DB alongside the words table)
# =========================================================================
def _ck_conn() -> sqlite3.Connection:
    con = sqlite3.connect(paths.data_dir() / "dehkhoda_stage.db")
    con.execute(f"CREATE TABLE IF NOT EXISTS {CK_TABLE}"
                " (name TEXT PRIMARY KEY, value TEXT)")
    con.commit()
    return con


def ck_get(con: sqlite3.Connection, name: str) -> Optional[str]:
    try:
        row = con.execute(f"SELECT value FROM {CK_TABLE} WHERE name=?",
                          (name,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def ck_set(con: sqlite3.Connection, name: str, value: str) -> None:
    con.execute(f"INSERT OR REPLACE INTO {CK_TABLE} (name, value) VALUES (?,?)",
                (name, value))
    con.commit()


# =========================================================================
# phase 1: dumps
# =========================================================================
def ensure_dumps(em: Emitter, source_dir: Optional[Path] = None) -> list[Path]:
    if source_dir:
        files = sorted(Path(source_dir).glob("*.sql.gz"))
        if files:
            em.progress(task_fa="استفاده از فایل‌های موجود", log=f"{len(files)} dumps")
            return files
    em.progress(task_fa="دانلود داده‌های لغت‌نامه (۲۹۰ مگابایت، قابل ادامه)…", indet=True)
    return model_fetcher.fetch_dumps(
        progress_cb=lambda d, t, m: em.progress(log=m),
        per_file=lambda n, done, tot: em.progress(
            pct=None, log=f"{n}: {done}/{tot}"),
    )


# =========================================================================
# phase 2: staging import (per-file checkpoints)
# =========================================================================
def _staging_count(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COUNT(*) FROM words").fetchone()[0]


def build_staging(em: Emitter, dumps: list[Path]) -> int:
    con = _ck_conn()
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS words (
                   id INTEGER PRIMARY KEY,
                   word TEXT NOT NULL,
                   meaning_clean TEXT NOT NULL,
                   doc TEXT NOT NULL,
                   word_norm TEXT)"""
        )
        have = _staging_count(con)
        if have >= USABLE_ROWS and ck_get(con, "staging") == "complete":
            em.progress(task_fa="آماده‌سازی رابطه‌ای از قبل کامل است",
                        log=f"staging rows={have}")
            return have

        from app.ingestion.mysql_dump_parser import clean_html, iter_dump, make_document
        from app.analyzers.persian import headword_key

        done_files = set((ck_get(con, "staging_files") or "").split("|")) - {""}
        total_files = len(dumps)
        buf: list[tuple] = []
        total = 0

        def flush() -> None:
            nonlocal total
            if not buf:
                return
            con.executemany(
                "INSERT OR REPLACE INTO words (id, word, meaning_clean, doc, word_norm)"
                " VALUES (?,?,?,?,?)", buf)
            con.commit()
            total += len(buf)
            buf.clear()

        for idx, path in enumerate(sorted(dumps)):
            name = path.name[: -len(".sql.gz")]
            if name in done_files:
                em.progress(log=f"skip parsed file {name}")
                continue
            t0 = time.time()
            rows = 0
            for entry in iter_dump(path):
                cleaned = clean_html(entry.meaning)
                if not cleaned:
                    continue  # the single empty-meaning row
                buf.append((entry.id, entry.word, cleaned,
                            make_document(entry.word, cleaned),
                            headword_key(entry.word)))
                rows += 1
                if len(buf) >= 10_000:
                    flush()
            flush()
            done_files.add(name)
            ck_set(con, "staging_files", "|".join(sorted(done_files)))
            em.progress(task_fa=f"پردازش فایل {idx + 1} از {total_files}…",
                        pct=int(100 * (idx + 1) / total_files),
                        log=f"{name}: {rows} rows in {time.time() - t0:.0f}s")

        have = _staging_count(con)
        ck_set(con, "staging", "complete" if have >= USABLE_ROWS else f"partial:{have}")
        em.progress(task_fa="پایگاه دادهٔ رابطه‌ای آماده است",
                    log=f"staging rows={have}")
        return have
    finally:
        con.close()


# =========================================================================
# phase 3: vector ingestion (id watermark)
# =========================================================================
def vector_ingest(em: Emitter, recreate: bool = False) -> dict[str, Any]:
    from app.config import load_settings
    from app.analyzers.persian import PersianAnalyzer
    from app.embeddings.provider import get_embedder
    from app.search.bm25 import BM25Encoder
    from app.ingestion.dehkhoda_loader import DehkhodaLoader
    from app.vector_store import backend_probe

    s = load_settings()
    analyzer = PersianAnalyzer(s.analyzer_engine)
    backend = backend_probe(s)
    embedder = get_embedder(s)
    bm25 = BM25Encoder(analyzer, k1=s.bm25_k1, b=s.bm25_b,
                       stats_path=s.resolved_data_dir() / "bm25_stats.pkl")
    bm25.load()
    loader = DehkhodaLoader(s, analyzer, embedder, bm25, backend)

    con = _ck_conn()
    try:
        ids = [r[0] for r in con.execute("SELECT id FROM words ORDER BY id")]
    finally:
        con.close()
    if not ids:
        raise RuntimeError("staging DB empty — run the staging phase first")

    last = int(ck_get(_ck_conn(), "vector_last_id") or 0)
    pending = [i for i in ids if i > last]
    total = len(ids)
    done = total - len(pending)
    em.progress(task_fa="آغاز درج بردارها…", log=f"total={total} pending={len(pending)}",
                pct=int(100 * done / max(1, total)) if total else 100)

    if not pending:
        # v1.3.1 fix: count BEFORE closing the backend — the old code called
        # backend.close() first, so the resumed-path count() ran against a
        # closed client (error or 0) and the resumed stats lied.
        count = backend.count()
        backend.close()
        return {"entries": total, "chunks": count, "resumed": True,
                "count": count}

    first = True
    for start in range(0, len(pending), ID_CHUNK):
        chunk = pending[start:start + ID_CHUNK]
        stats = loader.ingest(ids=chunk,
                              recreate=(recreate and first),
                              fit_bm25_sample=30_000)
        first = False
        done += len(chunk)
        ck = _ck_conn()
        ck_set(ck, "vector_last_id", str(chunk[-1]))
        ck.close()
        em.progress(task_fa=f"درج بردارها… {done:,} از {total:,}",
                    pct=int(100 * done / total),
                    log=f"chunk end={chunk[-1]} entries={stats['entries']} "
                        f"chunks={stats['chunks_inserted']}")
    count = backend.count()
    backend.close()
    st = state.set_value("ingested_count", count)
    return {"entries": total, "chunks": count, "resumed": last > 0}


# =========================================================================
# driver
# =========================================================================
def run(em: Emitter, source_dir: Optional[Path] = None,
        recreate: bool = False) -> dict[str, Any]:
    dumps = ensure_dumps(em, source_dir)
    build_staging(em, dumps)
    stats = vector_ingest(em, recreate=recreate)
    em.progress(task_fa="لغت‌نامه دهخدا آماده است")
    return stats
