#!/usr/bin/env python3
"""Verify + complete the embedding_model section of bootstrap/pins.json.

* downloads every pinned file (small/mid ones fully; the 1.1 GB safetensors
  via the HF tree API whose lfs.oid IS the sha256),
* fills the missing sha256 entries,
* asserts repo_id / total_size_bytes fields exist.

Run from the repo root:  python3 scripts/verify_model_pins.py [--full]
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PINS = ROOT / "bootstrap" / "pins.json"
TMP = Path("/tmp/dh-pins")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while True:
            b = fh.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def fetch(url: str, dest: Path, timeout: float = 60.0) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "dh-pins/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    return dest


def tree_lfs(repo_id: str, revision: str) -> dict[str, dict]:
    """Authoritative per-file metadata from the HF tree API.

    For LFS/xet-backed files `lfs.oid` is the real sha256 — no 1 GB download
    needed to cross-check the pin. Non-LFS files return size only.
    """
    url = f"https://huggingface.co/api/models/{repo_id}/tree/{revision}"
    req = urllib.request.Request(url, headers={"User-Agent": "dh-pins/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        entries = json.load(r)
    return {e["path"]: e for e in entries}


def main() -> int:
    full = "--full" in sys.argv
    pins = json.loads(PINS.read_text(encoding="utf-8"))
    em = pins["embedding_model"]
    base = f"{em['resolve_base']}/{em['revision']}"
    changed = False

    # normalise the top-level keys the release spec requires
    if "repo_id" not in em:
        em["repo_id"] = em["id"]
        changed = True
    if "total_size_bytes" not in em:
        em["total_size_bytes"] = sum(f["size"] for f in em["files"])
        changed = True

    ok = True
    tree = tree_lfs(em["repo_id"], em["revision"])
    for f in em["files"]:
        url = f"{base}/{f['path']}"
        dest = TMP / em["revision"] / f["path"]
        te = tree.get(f["path"]) or {}
        # size sanity from the authoritative tree API
        if te.get("size") not in (None, f["size"]):
            print(f"BAD size {f['path']}: pin={f['size']} tree={te.get('size')}")
            ok = False
        if f.get("sha256"):
            if te.get("lfs", {}).get("oid") and not full:
                got = te["lfs"]["oid"].lower()
                match = got == f["sha256"].lower()
                print(f"{'OK ' if match else 'BAD'} tree-lfs-sha256 {f['path']}")
                ok &= match
                continue
            # mid-size pinned files: verify for real
            if not dest.exists() or dest.stat().st_size != f["size"]:
                print(f"downloading {f['path']} ({f['size']} B)...")
                fetch(url, dest)
            got = sha256_file(dest)
            match = got == f["sha256"]
            print(f"{'OK ' if match else 'BAD'} sha256 {f['path']}")
            ok &= match
            continue
        # missing sha256 (small text files): download + fill
        print(f"fetching sha256 for {f['path']} ...")
        fetch(url, dest)
        got = sha256_file(dest)
        f["sha256"] = got
        changed = True
        print(f"OK  filled sha256 {f['path']}: {got}")

    if changed:
        PINS.write_text(json.dumps(pins, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
        print("pins.json updated")
    print("ALL OK" if ok else "MISMATCH FOUND")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
