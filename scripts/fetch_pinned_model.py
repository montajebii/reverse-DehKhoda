#!/usr/bin/env python3
"""Fetch the pinned embedding model and verify every byte against pins.json.

Used by CI (model-cache job + full-variant staging) and by local packaging:

  # download into a flat dir (relative paths preserved: 1_Pooling/config.json…)
  python3 scripts/fetch_pinned_model.py --dest /tmp/model

  # …and/or pack a tarball with the top-level layout used by the stager:
  #   models/multilingual-e5-base/<files>
  python3 scripts/fetch_pinned_model.py --dest /tmp/model --tar /tmp/model-e5.tar.gz

  # pack an EXISTING verified dir into the standard tarball layout
  python3 scripts/fetch_pinned_model.py --from-dir /tmp/model --tar /tmp/model-e5.tar.gz

Exit 0 only when every pinned file exists with the pinned size + sha256.
Resume: uses bootstrap.model_fetcher.download_file (HTTP Range .part resume),
so re-running an interrupted CI step continues instead of restarting.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bootstrap import model_fetcher, model_download  # noqa: E402


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while True:
            b = fh.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def fetch_into(dest: Path) -> None:
    em = model_fetcher.load_pins()["embedding_model"]
    base = f"{em['resolve_base']}/{em['revision']}"
    for f in em["files"]:
        out = dest / f["path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        url = f"{base}/{f['path']}"
        model_fetcher.download_file(url, out, sha256=f["sha256"],
                                    expected_size=f["size"])
        print(f"fetched {f['path']} ({f['size']} B)", flush=True)


def verify(dest: Path) -> list[str]:
    bad = model_download.verify_dir(dest)
    for b in bad:
        print(f"VERIFY FAIL: {b}", file=sys.stderr)
    return bad


def make_tar(src_dir: Path, tar_path: Path) -> None:
    """Tar with top-level models/<revision>/ — the SAME layout the lean
    downloader uses (data dir) and the full stager extracts into
    _internal/, so one artifact serves both variants."""
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    if tar_path.exists():
        tar_path.unlink()
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(src_dir, arcname=f"models/{model_download.revision()}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", type=Path, default=None,
                    help="download the pinned files into this dir")
    ap.add_argument("--from-dir", type=Path, default=None,
                    help="use an existing (already downloaded) dir")
    ap.add_argument("--tar", type=Path, default=None,
                    help="also write a models/<dirname> tarball here")
    args = ap.parse_args()

    src = args.from_dir or args.dest
    if src is None:
        ap.error("pass --dest and/or --from-dir")
    if args.dest is not None:
        fetch_into(args.dest)
    bad = verify(src)
    if bad:
        print(f"{len(bad)} file(s) failed pin verification", file=sys.stderr)
        return 1
    print(f"verified {len(model_fetcher.load_pins()['embedding_model']['files'])} "
          f"pinned files in {src}")
    if args.tar:
        make_tar(src, args.tar)
        print(f"tarball: {args.tar} ({args.tar.stat().st_size} B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
