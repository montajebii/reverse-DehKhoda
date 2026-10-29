#!/usr/bin/env python3
"""Exact global post-fix >512-token count: fixed chunker over ALL rows."""
import ast
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path("/home/z/my-project")
WORK = ROOT / "scripts" / "work"
sys.path.insert(0, str(ROOT / "backend"))
from app.ingestion.mysql_dump_parser import clean_html, iter_dump  # noqa: E402

import re as _re
from tokenizers import Tokenizer  # noqa: E402

tk = Tokenizer.from_file(str(WORK / "tokenizer" / "tokenizer.json"))


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


t0 = time.time()
total_chunks = 0
over = []
sample_text = None
for path in sorted((WORK / "dumps").glob("*.sql.gz")):
    for e in iter_dump(path):
        cleaned = clean_html(e.meaning)
        if not cleaned:
            continue
        chunks = fixed_chunk(cleaned)
        total_chunks += len(chunks)
        for ci, c in enumerate(chunks):
            n = len(tk.encode("passage: " + c).ids)
            if n > 512:
                over.append((e.id, e.word, ci, len(c), n))
                if sample_text is None:
                    sample_text = c
    print(f"  {path.name} ({time.time()-t0:.0f}s) chunks={total_chunks} "
          f"over512={len(over)}", flush=True)

over.sort(key=lambda t: -t[4])
print(f"\nFIXED chunker: total_chunks={total_chunks}  over512={len(over)}")
print("worst:", over[:8])
print("\nsample token-dense chunk (first found):")
print(repr(sample_text[:400]))
import json
json.dump({"fixed_total_chunks": total_chunks, "fixed_over512": len(over),
           "worst": over[:20]},
          open(WORK / "postfix_global.json", "w"), ensure_ascii=False,
          indent=1, default=str)
