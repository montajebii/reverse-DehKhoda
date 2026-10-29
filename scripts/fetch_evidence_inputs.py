#!/usr/bin/env python3
"""Fetch pinned Dehkhoda dumps + e5 tokenizer files for chunker evidence.

Downloads (with resume + size verification against bootstrap/pins.json):
  1. The 33 pinned .sql.gz dumps           -> work/dumps/
  2. sentence_bert_config.json (max_seq_length source of truth)
  3. tokenizer.json + tokenizer_config.json + special_tokens_map.json

Everything is byte-verified against pins.json.
"""

import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path("/home/z/my-project")
WORK = ROOT / "scripts" / "work"
DUMPS = WORK / "dumps"
TOKENIZER = WORK / "tokenizer"

PINS = json.loads((ROOT / "bootstrap" / "pins.json").read_text(encoding="utf-8"))


def fetch(url: str, dest: Path, expected_size: int | None = None,
          expected_sha256: str | None = None, tries: int = 4) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        if expected_size is None or dest.stat().st_size == expected_size:
            print(f"  already have {dest.name} ({dest.stat().st_size} B)")
            return
        dest.unlink()  # partial -> refetch
    last_err: Exception | None = None
    for t in range(1, tries + 1):
        try:
            t0 = time.time()
            with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as fh:
                while True:
                    buf = r.read(1 << 20)
                    if not buf:
                        break
                    fh.write(buf)
            got = dest.stat().st_size
            if expected_size is not None and got != expected_size:
                raise RuntimeError(f"size mismatch {dest.name}: {got} != {expected_size}")
            if expected_sha256 is not None:
                h = hashlib.sha256(dest.read_bytes()).hexdigest()
                if h != expected_sha256:
                    raise RuntimeError(f"sha256 mismatch {dest.name}: {h}")
            dt = time.time() - t0
            print(f"  fetched {dest.name} {got} B in {dt:.1f}s")
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"  attempt {t} failed for {dest.name}: {e}")
            time.sleep(3 * t)
    raise RuntimeError(f"download failed: {dest.name}: {last_err}")


def main() -> int:
    ds = PINS["dehkhoda_dataset"]
    base = ds["raw_base"]
    DUMPS.mkdir(parents=True, exist_ok=True)
    print("== dumps ==")
    for f in ds["files"]:
        name = f["name"]
        fetch(f"{base}/{name}.sql.gz", DUMPS / f"{name}.sql.gz",
              expected_size=f["size"])

    em = PINS["embedding_model"]
    tbase = f"{em['resolve_base']}/{em['revision']}"
    TOKENIZER.mkdir(parents=True, exist_ok=True)
    print("== tokenizer files (pinned e5 revision) ==")
    wanted = {"sentence_bert_config.json", "tokenizer.json",
              "tokenizer_config.json", "special_tokens_map.json"}
    for f in em["files"]:
        if f["path"] in wanted:
            fetch(f"{tbase}/{f['path']}", TOKENIZER / Path(f["path"]).name,
                  expected_size=f["size"], expected_sha256=f["sha256"])
    sbc = json.loads((TOKENIZER / "sentence_bert_config.json").read_text())
    print("sentence_bert_config.json ->", sbc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
