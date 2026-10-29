#!/usr/bin/env python3
"""Follow-up: (1) 'احد' id=18506 chunk-level token detail; (2) prove the
pinned tokenizer truncates to exactly 512 (embedder behavior);
(3) characterize the 377 <=1200-char over-512 chunks; (4) exact post-fix
residual (>512-token) count for the 310 affected rows."""

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path("/home/z/my-project")
WORK = ROOT / "scripts" / "work"
DUMPS = WORK / "dumps"

sys.path.insert(0, str(ROOT / "backend"))
from app.ingestion.mysql_dump_parser import clean_html, iter_dump  # noqa: E402

import ast
LOADER = ROOT / "backend" / "app" / "ingestion" / "dehkhoda_loader.py"
src = LOADER.read_text(encoding="utf-8")
fn_src = None
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.FunctionDef) and node.name == "chunk_definition":
        fn_src = ast.get_source_segment(src, node)
        break
ns: dict = {}
exec(compile(fn_src, str(LOADER), "exec"), ns)
real_chunk = ns["chunk_definition"]
stub = SimpleNamespace(s=SimpleNamespace(chunk_chars=1200, chunk_overlap=150))

import re as _re


def fixed_chunk(definition: str) -> list[str]:
    max_len, overlap = 1200, 150
    definition = definition.strip()
    if len(definition) <= max_len:
        return [definition] if definition else []
    sentences = _re.split(r"(?<=[۔.؟!\n])\s*", definition)
    chunks, cur = [], ""
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


from tokenizers import Tokenizer  # noqa: E402

tk = Tokenizer.from_file(str(WORK / "tokenizer" / "tokenizer.json"))
tk_tr = Tokenizer.from_file(str(WORK / "tokenizer" / "tokenizer.json"))
tk_tr.enable_truncation(max_length=512)

TARGET_IDS = {18506}          # 'احد' 34,783 — the user's named definition
AFFECTED = set()              # fill: rows with an oversize (buggy) chunk
FIXED_CHUNKS: dict[int, list[tuple[str, str]]] = {}  # id -> [(word, text)]
charwise_over512: list[tuple] = []   # (id, word, ci, chars, tokens) <=1200

t0 = time.time()
files = sorted(DUMPS.glob("*.sql.gz"))
for path in files:
    for e in iter_dump(path):
        cleaned = clean_html(e.meaning)
        if not cleaned:
            continue
        chunks = real_chunk(stub, cleaned)
        is_bad = any(len(c) > 1200 for c in chunks)
        if e.id in TARGET_IDS:
            FIXED_CHUNKS[e.id] = [(e.word, c) for c in chunks]
        if is_bad:
            AFFECTED.add(e.id)
            FIXED_CHUNKS[e.id] = [(e.word, c) for c in fixed_chunk(cleaned)]
        if is_bad or e.id in TARGET_IDS:
            # per-chunk true token counts for detail tables
            for ci, c in enumerate(chunks):
                n = len(tk.encode("passage: " + c).ids)
                if n > 512 and len(c) <= 1200:
                    charwise_over512.append((e.id, e.word, ci, len(c), n))
    print(f"  {path.name} done ({time.time()-t0:.0f}s)", flush=True)

print(f"\naffected rows: {len(AFFECTED)}")

# ---- (1) 'احد' detail -----------------------------------------------------
out = {}
for wid in TARGET_IDS:
    pairs = FIXED_CHUNKS[wid]
    toks = [len(tk.encode("passage: " + c).ids) for _, c in pairs]
    out["ahad"] = {
        "id": wid,
        "n_chunks_buggy": len(pairs),
        "chunk_tokens": toks,
        "chunks_over_512": sum(1 for t in toks if t > 512),
        "max_tokens": max(toks),
    }
    print("\n== 'احد' id=18506 (clean_len=34783) ==")
    print(f"  buggy chunker: {len(pairs)} chunks, tokens per chunk: {toks}")
    print(f"  chunks over 512 tokens: {out['ahad']['chunks_over_512']}")
    fz = fixed_chunk  # noqa: F841
print(f"  affected rows: {len(AFFECTED)}")

# ---- (4) post-fix residual for affected rows ------------------------------
resid = 0
resid_rows = set()
worst = []
for wid, pairs in FIXED_CHUNKS.items():
    for w, c in pairs:
        n = len(tk.encode("passage: " + c).ids)
        if n > 512:
            resid += 1
            resid_rows.add(wid)
            worst.append((n, len(c), wid, w))
worst.sort(reverse=True)
print(f"\n== post-minimal-fix residual ==")
print(f"  affected-row chunks still >512 tokens after fix: {resid}")
print(f"  rows still affected: {len(resid_rows)}")
print(f"  worst post-fix chunks: {worst[:6]}")

# ---- (2) truncation demonstration on the worst chunk ----------------------
print("\n== truncation demo (pinned tokenizer, embedder behavior) ==")
demo_src = None
for wid, pairs in FIXED_CHUNKS.items():
    for w, c in pairs:
        if len(tk.encode("passage: " + c).ids) > 512:
            if demo_src is None or len(c) > len(demo_src[2]):
                demo_src = (wid, w, c)
if demo_src:
    wid, w, c = demo_src
    full = tk.encode("passage: " + c)
    tr = tk_tr.encode("passage: " + c)
    print(f"  worst chunk: id={wid} word={w!r} chars={len(c)} "
          f"true_tokens={len(full.ids)} truncated_tokens={len(tr.ids)}")
    print(f"  truncated tail == full head? {tr.ids == full.ids[:512]}")

print(f"\n<=1200-char over-512 chunks among affected/target rows: "
      f"{len(charwise_over512)}")
for rid, w, ci, cl, tn in charwise_over512[:8]:
    print(f"  id={rid} {w[:20]!r} chunk#{ci} chars={cl} tokens={tn}")

json.dump({
    "ahad": out.get("ahad"),
    "affected_rows": len(AFFECTED),
    "post_fix_residual_chunks_affected_rows": resid,
    "post_fix_residual_rows": len(resid_rows),
    "worst_post_fix": worst[:10],
    "small_over512_examples": charwise_over512[:20],
}, open(WORK / "chunker_followup.json", "w"), ensure_ascii=False, indent=1,
    default=str)
print("\nsaved -> scripts/work/chunker_followup.json")
