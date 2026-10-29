#!/usr/bin/env python3
"""Chunker evidence pipeline — answers Q1a-Q1d from the ACTUAL data.

Runs the EXACT chunk_definition source extracted (via ast) from
backend/app/ingestion/dehkhoda_loader.py at runtime, over the full pinned
dataset (cleaned exactly like production: clean_html on every dump row),
then tokenizes every produced chunk with the pinned e5 tokenizer.

Outputs:
  scripts/work/chunker_evidence.json   — full stats
  scripts/work/chunker_evidence_report.txt — human-readable summary
"""

import ast
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path("/home/z/my-project")
WORK = ROOT / "scripts" / "work"
DUMPS = WORK / "dumps"

sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from app.ingestion.mysql_dump_parser import clean_html, iter_dump  # noqa: E402

# ---------------------------------------------------------------------------
# 1) Extract the REAL chunk_definition from the repo file (no hand-copying)
# ---------------------------------------------------------------------------
LOADER = ROOT / "backend" / "app" / "ingestion" / "dehkhoda_loader.py"
src = LOADER.read_text(encoding="utf-8")
tree = ast.parse(src)
fn_src = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "chunk_definition":
        fn_src = ast.get_source_segment(src, node)
        break
assert fn_src, "chunk_definition not found in loader source"
ns: dict = {}
exec(compile(fn_src, str(LOADER), "exec"), ns)  # noqa: S102
real_chunk_definition = ns["chunk_definition"]

# stub settings: production defaults from backend/app/config.py
# (chunk_definition accesses self.s.chunk_chars / self.s.chunk_overlap)
stub = SimpleNamespace(s=SimpleNamespace(chunk_chars=1200, chunk_overlap=150))

# ---------------------------------------------------------------------------
# 2) Tokenizer (pinned revision, sha256-verified at download)
# ---------------------------------------------------------------------------
from tokenizers import Tokenizer  # noqa: E402

tk = Tokenizer.from_file(str(WORK / "tokenizer" / "tokenizer.json"))
MAX_LEN = 512  # sentence_bert_config.json (pinned): {"max_seq_length": 512}
tk_trunc = Tokenizer.from_file(str(WORK / "tokenizer" / "tokenizer.json"))
tk_trunc.enable_truncation(max_length=MAX_LEN)

# ---------------------------------------------------------------------------
# 3) Replay over the full dataset
# ---------------------------------------------------------------------------
MINIMAL_FIX_PATCH = '''
def fixed_chunk_definition(self, definition):
    """Minimal fix: else branch runs the SAME hard-split while-loop."""
    max_len = int(self.s.chunk_chars)
    overlap = int(self.s.chunk_overlap)
    definition = definition.strip()
    if len(definition) <= max_len:
        return [definition] if definition else []
    import re
    sentences = re.split(r"(?<=[\\u06d4.\\u061f!\\n])\\s*", definition)
    chunks = []
    cur = ""
    for sent in sentences:
        if len(cur) + len(sent) + 1 <= max_len or not cur:
            cur = f"{cur} {sent}".strip()
            while len(cur) > max_len:
                chunks.append(cur[:max_len])
                cur = cur[max_len - overlap:]
        else:
            chunks.append(cur)
            tail = cur[-overlap:] if overlap > 0 else ""
            cur = f"{tail} {sent}".strip()
            while len(cur) > max_len:          # <-- THE FIX
                chunks.append(cur[:max_len])
                cur = cur[max_len - overlap:]
    if cur:
        chunks.append(cur)
    return chunks
'''

# sanity: sentence-split regex of the real function
import re as _re
m = _re.search(r're\.split\(r"([^"]+)"', fn_src)
SENT_RE_RAW = m.group(1) if m else "NOT FOUND"
sent_re = _re.compile(SENT_RE_RAW.replace("(?<=", "(?<="))


def fixed_chunk_definition(definition: str, max_len=1200, overlap=150) -> list[str]:
    definition = definition.strip()
    if len(definition) <= max_len:
        return [definition] if definition else []
    sentences = _re.split(r"(?<=[۔.؟!\n])\s*", definition)
    chunks: list[str] = []
    cur = ""
    for sent in sentences:
        if len(cur) + len(sent) + 1 <= max_len or not cur:
            cur = f"{cur} {sent}".strip()
            while len(cur) > max_len:
                chunks.append(cur[:max_len])
                cur = cur[max_len - overlap:]
        else:
            chunks.append(cur)
            tail = cur[-overlap:] if overlap > 0 else ""
            cur = f"{tail} {sent}".strip()
            while len(cur) > max_len:
                chunks.append(cur[:max_len])
                cur = cur[max_len - overlap:]
    if cur:
        chunks.append(cur)
    return chunks


def main() -> int:
    t0 = time.time()
    rows_total = 0
    rows_used = 0
    total_chunks = 0
    oversize_rows: list[dict] = []      # rows producing any chunk > chunk_chars
    longest_defs: list[dict] = []       # top definitions by cleaned length
    chunk_texts: list[str] = []         # "passage: " + chunk for ALL chunks
    chunk_meta: list[tuple] = []        # (row_id, word, ci, char_len)

    fixed_total_chunks = 0

    files = sorted(DUMPS.glob("*.sql.gz"))
    assert len(files) == 33, f"expected 33 dumps, got {len(files)}"

    for path in files:
        for entry in iter_dump(path):
            rows_total += 1
            cleaned = clean_html(entry.meaning)
            if not cleaned:
                continue
            rows_used += 1
            chunks = real_chunk_definition(stub, cleaned)
            fixed_total_chunks += len(fixed_chunk_definition(cleaned))
            total_chunks += len(chunks)
            lens = [len(c) for c in chunks]
            mx = max(lens) if lens else 0
            if len(cleaned) > 3000:
                longest_defs.append({
                    "id": entry.id, "word": entry.word,
                    "clean_len": len(cleaned), "n_chunks": len(chunks),
                    "max_chunk": mx,
                })
            if mx > 1200:
                # diagnose: is the trigger sentence the FIRST sentence?
                sents = sent_re.split(cleaned.strip())
                sents = [s for s in sents if s and s.strip()]
                giant_idx = max(range(len(sents)), key=lambda i: len(sents[i]))
                oversize_rows.append({
                    "id": entry.id, "word": entry.word,
                    "clean_len": len(cleaned),
                    "n_chunks": len(chunks), "max_chunk": mx,
                    "n_sentences": len(sents),
                    "giant_sent_len": len(sents[giant_idx]),
                    "giant_sent_is_first": giant_idx == 0,
                    "giant_sent_head": sents[giant_idx][:120],
                })
            for ci, c in enumerate(chunks):
                chunk_texts.append("passage: " + c)
                chunk_meta.append((entry.id, entry.word, ci, len(c)))
        print(f"  parsed {path.name}  rows={rows_total} used={rows_used} "
              f"chunks={total_chunks} oversize_rows={len(oversize_rows)} "
              f"({time.time()-t0:.0f}s)", flush=True)

    # ------------------------------------------------------------------
    # 4) Tokenize ALL chunks (true length, no truncation)
    # ------------------------------------------------------------------
    print(f"tokenizing {len(chunk_texts)} chunks ...", flush=True)
    tok_counts: list[int] = []
    B = 4096
    over512: list[tuple] = []
    for i in range(0, len(chunk_texts), B):
        batch = chunk_texts[i:i + B]
        encs = tk.encode_batch(batch, add_special_tokens=True)
        for j, e in enumerate(encs):
            n = len(e.ids)
            tok_counts.append(n)
            if n > MAX_LEN:
                rid, word, ci, clen = chunk_meta[i + j]
                over512.append((rid, word, ci, clen, n))
        if (i // B) % 10 == 0:
            print(f"  tokenized {i+len(batch)}/{len(chunk_texts)} "
                  f"({time.time()-t0:.0f}s)", flush=True)

    tok_counts_sorted = sorted(tok_counts)
    n = len(tok_counts_sorted)

    def pct(p: float) -> int:
        return tok_counts_sorted[min(n - 1, int(p * n))]

    stats = {
        "sentence_regex": SENT_RE_RAW,
        "chunk_chars": 1200, "chunk_overlap": 150,
        "max_seq_length": MAX_LEN,
        "rows_total": rows_total,
        "rows_used": rows_used,
        "total_chunks": total_chunks,
        "fixed_chunker_total_chunks": fixed_total_chunks,
        "rows_with_oversize_chunk": len(oversize_rows),
        "oversize_rows": sorted(oversize_rows,
                                key=lambda d: -d["max_chunk"])[:60],
        "longest_definitions": sorted(longest_defs,
                                      key=lambda d: -d["clean_len"])[:20],
        "token_stats": {
            "p50": pct(0.50), "p90": pct(0.90), "p99": pct(0.99),
            "p999": pct(0.999), "max": tok_counts_sorted[-1],
        },
        "chunks_over_512_tokens": len(over512),
        "over_512_samples": [
            {"id": rid, "word": w, "chunk_index": ci, "chars": cl,
             "tokens": tn} for rid, w, ci, cl, tn in
            sorted(over512, key=lambda t: -t[4])[:80]
        ],
        "over_512_char_distribution": {},
        "elapsed_s": round(time.time() - t0, 1),
    }
    # char-length histogram of the >512-token chunks
    buckets = {}
    for rid, w, ci, cl, tn in over512:
        b = "<=1200" if cl <= 1200 else ("1201-2400" if cl <= 2400 else
            ("2401-4800" if cl <= 4800 else ">4800"))
        buckets[b] = buckets.get(b, 0) + 1
    stats["over_512_char_distribution"] = buckets

    (WORK / "chunker_evidence.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = []
    a = lines.append
    a(f"sentences regex: r\"{SENT_RE_RAW}\"")
    a(f"rows: total={rows_total} used={rows_used} chunks={total_chunks} "
      f"(fixed-chunker chunk total={fixed_total_chunks})")
    a(f"rows with an oversize (>1200 char) chunk: {len(oversize_rows)}")
    a(f"chunks over 512 tokens (true count, prefix included): {len(over512)} "
      f"of {total_chunks}")
    a(f"token percentiles: {stats['token_stats']}")
    a(f"char-buckets of >512-token chunks: {buckets}")
    a("")
    a("== top oversize rows ==")
    for d in sorted(oversize_rows, key=lambda d: -d["max_chunk"])[:20]:
        a(f"  id={d['id']} {d['word'][:24]!r} clean={d['clean_len']} "
          f"chunks={d['n_chunks']} max_chunk={d['max_chunk']} "
          f"sentences={d['n_sentences']} giant_len={d['giant_sent_len']} "
          f"giant_first={d['giant_sent_is_first']}")
    a("")
    a("== longest definitions ==")
    for d in stats["longest_definitions"][:10]:
        a(f"  id={d['id']} {d['word'][:24]!r} clean={d['clean_len']} "
          f"chunks={d['n_chunks']} max_chunk={d['max_chunk']}")
    report = "\n".join(lines)
    (WORK / "chunker_evidence_report.txt").write_text(report, encoding="utf-8")
    print(report)
    print(f"DONE in {stats['elapsed_s']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
