"""Dehkhoda ingestion pipeline.

Stages (all streaming, memory-bounded):
  1. download_dumps()      — fetch the 33 alphabet-split .sql.gz files
  2. import_to_staging()   — parse MySQL dumps -> SQLite staging DB (resumable)
  3. ingest()              — staging rows -> chunk -> embed (dense+BM25 sparse)
                             -> batch insert (1000..5000) -> flush() at end
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Iterator, Optional

from ..analyzers.persian import headword_key
from ..analyzers.persian import PersianAnalyzer
from ..embeddings.provider import EmbeddingProvider
from ..search.bm25 import BM25Encoder
from ..vector_store.base import Record, VectorStoreBackend
from .mysql_dump_parser import clean_html, iter_dump, make_document

logger = logging.getLogger(__name__)

# v1.3.1 (PART 2 closure): the dataset URL is PINNED to the immutable
# commit sha from bootstrap/pins.json -> dehkhoda_dataset.commit — the old
# `master` branch ref made this code path a moving target and an accidental
# network fetch of arbitrary bytes. Kept in sync with pins.json by
# bootstrap/tests/test_pins_sync.py (this module cannot import bootstrap).
BASE_URL = ("https://raw.githubusercontent.com/nimah79/Dehkhoda-SQL/"
            "185ed68bc6ad7c4244c5f4cc1c9968aa8aefa0f4")
DUMP_FILES = [
    "01-A", "02-Alef", "03-Be", "04-Pe", "05-Te", "06-Se", "07-Jim", "08-Che",
    "09-He", "10-Khe", "11-Dal", "12-Zal", "13-Re", "14-Ze", "15-Zhe", "16-Sin",
    "17-Shin", "18-Sad", "19-Zad", "20-Ta", "21-Za", "22-Ain", "23-Qain",
    "24-Fe", "25-Qaf", "26-Kaf", "27-Gaf", "28-Lam", "29-Mim", "30-Noun",
    "31-Vav", "32-He", "33-Ye",
]
EXPECTED_ROWS = 312_507
# The source dump contains exactly one row with an empty meaning, which the
# loader filters out; a complete staging DB therefore holds EXPECTED_ROWS - 1.
USABLE_ROWS = EXPECTED_ROWS - 1

_SENT_SPLIT_RE = None  # built lazily


class DehkhodaLoader:
    def __init__(
        self,
        settings,
        analyzer: PersianAnalyzer,
        embedder: EmbeddingProvider,
        bm25: BM25Encoder,
        backend: VectorStoreBackend,
    ) -> None:
        self.s = settings
        self.analyzer = analyzer
        self.embedder = embedder
        self.bm25 = bm25
        self.backend = backend

    # -- 1) download ------------------------------------------------------------
    def download_dumps(self, dest: Optional[Path] = None) -> list[Path]:
        import urllib.request

        dest = Path(dest or (self.s.resolved_data_dir() / "dehkhoda_dumps"))
        dest.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for name in DUMP_FILES:
            out = dest / f"{name}.sql.gz"
            if not out.exists() or out.stat().st_size == 0:
                url = f"{BASE_URL}/{name}.sql.gz"
                logger.info("downloading %s", url)
                urllib.request.urlretrieve(url, out)
            paths.append(out)
        return paths

    # -- 2) relational staging ----------------------------------------------------
    def _staging_conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.s.staging_db_path)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS words (
                id INTEGER PRIMARY KEY,
                word TEXT NOT NULL,
                meaning_clean TEXT NOT NULL,
                doc TEXT NOT NULL,
                word_norm TEXT
            )
            """
        )
        con.commit()
        return con

    @staticmethod
    def _ensure_word_norm_column(con: sqlite3.Connection) -> bool:
        """Migrate pre-1.1 staging DBs: add the `word_norm` column if missing.
        Returns True when a migration was performed."""
        cols = {row[1] for row in con.execute("PRAGMA table_info(words)")}
        if "word_norm" in cols:
            return False
        con.execute("ALTER TABLE words ADD COLUMN word_norm TEXT")
        con.commit()
        return True

    def import_to_staging(
        self, dumps: Optional[Iterable[Path]] = None, batch: int = 10_000
    ) -> int:
        """Parse dumps into the SQLite staging DB. Skips work if already full."""
        con = self._staging_conn()
        self._ensure_word_norm_column(con)
        have = con.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        if have >= USABLE_ROWS:
            logger.info("staging already complete (%d rows) — skipping import", have)
            con.close()
            return have
        # only fetch the 52 MB of dumps when staging actually needs (re)building
        files = list(dumps) if dumps is not None else self.download_dumps()
        con.execute("DELETE FROM words")
        t0, total, buf = time.time(), 0, []
        for path in files:
            logger.info("parsing %s", path)
            for entry in iter_dump(path):
                cleaned = clean_html(entry.meaning)
                if not cleaned:
                    continue  # skip the single empty-meaning row
                buf.append(
                    (
                        entry.id,
                        entry.word,
                        cleaned,
                        make_document(entry.word, cleaned),
                        headword_key(entry.word),
                    )
                )
                if len(buf) >= batch:
                    total += self._stage_flush(con, buf)
        if buf:
            total += self._stage_flush(con, buf)
        con.commit()
        logger.info("staging import done: %d rows in %.0fs", total, time.time() - t0)
        con.close()
        return total

    @staticmethod
    def _stage_flush(con: sqlite3.Connection, buf: list[tuple]) -> int:
        con.executemany(
            "INSERT OR REPLACE INTO words (id, word, meaning_clean, doc, word_norm)"
            " VALUES (?,?,?,?,?)",
            buf,
        )
        con.commit()
        n = len(buf)
        buf.clear()
        return n

    def iter_staging(self, limit: Optional[int] = None) -> Iterator[tuple[int, str, str, str]]:
        con = self._staging_conn()
        sql = "SELECT id, word, meaning_clean, doc FROM words ORDER BY id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        try:
            for row in con.execute(sql):
                yield row
        finally:
            con.close()

    def iter_staging_ids(self, ids: list[int]) -> Iterator[tuple[int, str, str, str]]:
        """Yield staging rows by explicit primary-key list (chunked IN queries)."""
        con = self._staging_conn()
        try:
            for i in range(0, len(ids), 500):
                part = ids[i : i + 500]
                q = "SELECT id, word, meaning_clean, doc FROM words WHERE id IN (%s)" % \
                    ",".join("?" * len(part))
                for row in con.execute(q, part):
                    yield row
        finally:
            con.close()

    # -- reverse-dictionary lookups ---------------------------------------------
    def ensure_word_norm_index(self) -> bool:
        """Backfill word_norm (pre-1.1 staging DBs) and create the lookup index.

        Safe to call at every startup: with the column and index in place it is
        two cheap queries.  Returns True when the index is ready for use.
        """
        if not Path(self.s.staging_db_path).exists():
            return False
        con = self._staging_conn()
        try:
            migrated = self._ensure_word_norm_column(con)
            if migrated:
                t0 = time.time()
                rows = con.execute("SELECT id, word FROM words").fetchall()
                con.executemany(
                    "UPDATE words SET word_norm = ? WHERE id = ?",
                    [(headword_key(w), wid) for wid, w in rows],
                )
                con.commit()
                logger.info(
                    "word_norm backfill: %d rows in %.1fs", len(rows), time.time() - t0
                )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_words_norm ON words(word_norm)"
            )
            con.commit()
            return True
        except Exception as exc:
            logger.warning("word_norm index unavailable: %s", exc)
            return False
        finally:
            con.close()

    def find_exact(self, key: str) -> Optional[tuple[int, str, str]]:
        """Exact headword lookup by canonical key -> (id, word, meaning_clean).
        Picks the lowest row id when several entries share the headword."""
        if not key or not Path(self.s.staging_db_path).exists():
            return None
        con = self._staging_conn()
        try:
            row = con.execute(
                "SELECT id, word, meaning_clean FROM words WHERE word_norm = ?"
                " ORDER BY id LIMIT 1",
                (key,),
            ).fetchone()
            return (int(row[0]), row[1], row[2]) if row else None
        except sqlite3.Error:
            return None
        finally:
            con.close()

    def fetch_meanings(self, word_ids: list[int]) -> dict[int, str]:
        """Full cleaned definitions for staging row ids (chunked IN queries)."""
        out: dict[int, str] = {}
        if not word_ids or not Path(self.s.staging_db_path).exists():
            return out
        con = self._staging_conn()
        try:
            for i in range(0, len(word_ids), 500):
                part = word_ids[i : i + 500]
                q = "SELECT id, meaning_clean FROM words WHERE id IN (%s)" % \
                    ",".join("?" * len(part))
                for wid, meaning in con.execute(q, part):
                    out[int(wid)] = meaning
            return out
        except sqlite3.Error:
            return out
        finally:
            con.close()

    # -- chunking ---------------------------------------------------------------
    def chunk_definition(self, definition: str) -> list[str]:
        """Split long definitions at Persian sentence boundaries with overlap."""
        max_len = int(self.s.chunk_chars)
        overlap = int(self.s.chunk_overlap)
        definition = definition.strip()
        if len(definition) <= max_len:
            return [definition] if definition else []

        import re

        sentences = re.split(r"(?<=[۔.؟!\n])\s*", definition)
        chunks: list[str] = []
        cur = ""
        for sent in sentences:
            if len(cur) + len(sent) + 1 <= max_len or not cur:
                cur = f"{cur} {sent}".strip()
                # a single sentence longer than max_len: hard-split it
                while len(cur) > max_len:
                    chunks.append(cur[:max_len])
                    cur = cur[max_len - overlap :]
            else:
                chunks.append(cur)
                tail = cur[-overlap:] if overlap > 0 else ""
                cur = f"{tail} {sent}".strip()
                # v1.3.1 regression fix: a sentence that does not fit after
                # the overlap-tail can still leave cur oversized (a >1049-char
                # sentence arriving while cur is non-empty). Without this
                # hard-split the oversized cur was flushed as-is, producing
                # chunks of up to ~13k chars whose e5 vector silently covered
                # only the first 512 tokens. Mirrors the branch-1 loop, so
                # EVERY emitted chunk is <= max_len chars.
                while len(cur) > max_len:
                    chunks.append(cur[:max_len])
                    cur = cur[max_len - overlap :]
        if cur:
            chunks.append(cur)
        return chunks

    # -- 3) vector ingestion ------------------------------------------------------
    def ingest(
        self,
        limit: Optional[int] = None,
        recreate: bool = False,
        fit_bm25_sample: int = 30_000,
        word_filter: Optional[set[str]] = None,
        ids: Optional[list[int]] = None,
    ) -> dict:
        """Full pipeline into the configured vector backend.

        * Bounded batch inserts (settings.batch_size, clamped 1000..5000)
        * flush() called once at the end (locked requirement)
        """
        t0 = time.time()
        self.backend.ensure_collection(dim=self.embedder.dim, recreate=recreate)

        if not self.bm25.load():
            sample: list[str] = []
            for _, _, meaning_clean, _ in self.iter_staging(limit=fit_bm25_sample or None):
                sample.append(meaning_clean)
            if not sample:
                raise RuntimeError("staging DB is empty — run import_to_staging() first")
            self.bm25.fit(sample, sample_size=fit_bm25_sample)
            self.bm25.save()

        staged = self.s.staging_db_path.exists() and self._staging_count() or 0
        if staged == 0:
            raise RuntimeError("staging DB is empty — run import_to_staging() first")

        batch_rows: list[tuple[int, str, str, str]] = []
        inserted = 0
        entries = 0
        batch_size = max(1000, min(5000, int(self.s.batch_size)))

        def _flush_batch() -> None:
            nonlocal inserted, entries
            if not batch_rows:
                return
            chunked: list[tuple[int, str, int, str, str]] = []
            for word_id, word, meaning, _doc in batch_rows:
                parts = self.chunk_definition(meaning)
                for ci, part in enumerate(parts):
                    chunked.append((word_id, word, ci, part, make_document(word, part)))
            entries += len(batch_rows)
            # REVERSE DICTIONARY (v1.1): dense + sparse vectors are generated
            # from the DEFINITION text only — the headword stays a stored
            # payload field so retrieval matches meanings, not word forms.
            dense = self.embedder.embed_documents([c[3] for c in chunked])
            sparse = self.bm25.encode_documents([c[3] for c in chunked])
            records = [
                Record(
                    doc_id=f"w{wid}c{ci}",
                    word_id=wid,
                    word=word,
                    chunk_index=ci,
                    definition=part,
                    doc=doc,
                    dense=dense[i],
                    sparse=sparse[i],
                )
                for i, (wid, word, ci, part, doc) in enumerate(chunked)
            ]
            inserted += self.backend.insert(records, batch_size=batch_size)
            batch_rows.clear()

        row_iter = (
            self.iter_staging_ids(ids) if ids is not None else self.iter_staging(limit=limit)
        )
        for row in row_iter:
            if word_filter is not None and row[1] not in word_filter:
                continue
            batch_rows.append(row)
            if len(batch_rows) >= 512:  # embedding micro-batch
                _flush_batch()
                if entries % 20_000 < 512:
                    logger.info("ingest progress: %d entries / %d chunks", entries, inserted)
        _flush_batch()

        # Locked: flush() after ingestion so data survives restarts.
        self.backend.flush()
        stats = {
            "entries": entries,
            "chunks_inserted": inserted,
            "backend": self.backend.name,
            "embedder": self.embedder.name,
            "elapsed_s": round(time.time() - t0, 1),
            "count": self.backend.count(),
        }
        logger.info("ingestion finished: %s", stats)
        return stats

    def _staging_count(self) -> int:
        con = self._staging_conn()
        try:
            return con.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        finally:
            con.close()
