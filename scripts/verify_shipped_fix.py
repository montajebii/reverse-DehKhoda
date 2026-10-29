#!/usr/bin/env python3
"""Verify the SHIPPED (fixed) repo chunk_definition over the full corpus."""
import ast
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path("/home/z/my-project")
WORK = ROOT / "scripts" / "work"
sys.path.insert(0, str(ROOT / "backend"))
from app.ingestion.mysql_dump_parser import clean_html, iter_dump  # noqa: E402

from tokenizers import Tokenizer  # noqa: E402

LOADER = ROOT / "backend" / "app" / "ingestion" / "dehkhoda_loader.py"
src = LOADER.read_text(encoding="utf-8")
fn_src = None
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.FunctionDef) and node.name == "chunk_definition":
        fn_src = ast.get_source_segment(src, node)
        break
assert "while len(cur) > max_len" in fn_src and fn_src.count("while len(cur) > max_len") == 2, \
    "shipped chunk_definition does NOT contain the fix (2 hard-split loops expected)"
ns: dict = {}
exec(compile(fn_src, str(LOADER), "exec"), ns)
chunk = ns["chunk_definition"]
stub = SimpleNamespace(s=SimpleNamespace(chunk_chars=1200, chunk_overlap=150))

tk = Tokenizer.from_file(str(WORK / "tokenizer" / "tokenizer.json"))

t0 = time.time()
total, mx, over, ahad = 0, 0, 0, None
for path in sorted((WORK / "dumps").glob("*.sql.gz")):
    for e in iter_dump(path):
        cleaned = clean_html(e.meaning)
        if not cleaned:
            continue
        parts = chunk(stub, cleaned)
        total += len(parts)
        for p in parts:
            L = len(p)
            if L > mx:
                mx = L
            if L > 1200:
                over += 1
        if e.id == 18506:
            ahad = (len(parts), max(len(p) for p in parts))
    print(f"  {path.name} ({time.time()-t0:.0f}s) chunks={total} "
          f"max={mx} over1200={over}", flush=True)

print(f"\nSHIPPED FIXED CODE: total_chunks={total}  max_chunk_chars={mx}  "
      f"chunks_over_1200_chars={over}")
print(f"'ahd' id=18506: chunks={ahad[0]} max_chunk={ahad[1]}")
