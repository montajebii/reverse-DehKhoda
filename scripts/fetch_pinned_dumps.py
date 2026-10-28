#!/usr/bin/env python3
"""Fetch the 33 pinned Dehkhoda .sql.gz dumps and verify sizes against pins.json.

Used by the CI build-index-snapshot job (which needs the dumps to run the
production ingestion) and by the dataset-bundling step of the release
packaging:

  # download into a flat dir
  python3 scripts/fetch_pinned_dumps.py --dest /tmp/dumps

  # ...and/or pack a tarball with the layout the full-variant stager
  # extracts into _internal/ (dumps/<name>.sql.gz):
  python3 scripts/fetch_pinned_dumps.py --dest /tmp/dumps --tar dumps.tar.gz

  # pack an EXISTING downloaded dir (verifies sizes first)
  python3 scripts/fetch_pinned_dumps.py --from-dir /tmp/dumps --tar dumps.tar.gz

Exit 0 only when exactly the 33 pinned files exist with the pinned sizes.
Resume: bootstrap.model_fetcher.download_file does HTTP Range .part resume,
so an interrupted CI step continues instead of restarting.

Note: the dataset pins carry SIZES only (progress/sanity), not sha256s —
the integrity anchor is the immutable commit sha in
dehkhoda_dataset.commit, which is part of every URL. Row-count integrity
(312,506 usable rows) is asserted later by the staging phase.
"""
from __future__ import annotations

import argparse
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bootstrap import model_fetcher  # noqa: E402


def fetch_into(dest: Path) -> None:
    files = model_fetcher.dump_pin_list()
    base = model_fetcher.load_pins()["dehkhoda_dataset"]["raw_base"]
    for f in files:
        out = dest / f"{f['name']}.sql.gz"
        url = f"{base}/{f['name']}.sql.gz"
        model_fetcher.download_file(url, out, expected_size=f["size"])
        actual = out.stat().st_size
        if actual != f["size"]:
            print(f"SIZE FAIL: {f['name']}.sql.gz pinned={f['size']} got={actual}",
                  file=sys.stderr)
            return 1  # type: ignore[return-value]
        print(f"fetched {f['name']}.sql.gz ({f['size']} B)", flush=True)


def verify(dest: Path) -> list[str]:
    bad: list[str] = []
    files = model_fetcher.dump_pin_list()
    for f in files:
        out = dest / f"{f['name']}.sql.gz"
        if not out.exists() or out.stat().st_size != f["size"]:
            bad.append(f"{f['name']}.sql.gz (want {f['size']} B)")
    extra = [p.name for p in sorted(dest.glob("*.sql.gz"))
             if p.stem.replace(".sql", "") not in {f["name"] for f in files}]
    for e in extra:
        bad.append(f"unexpected extra file {e}")
    for b in bad:
        print(f"VERIFY FAIL: {b}", file=sys.stderr)
    return bad


def make_tar(src_dir: Path, tar_path: Path) -> None:
    """Tar with top-level dumps/ — what appimage.sh extracts into
    _internal/ so the runtime finds _internal/dumps/<name>.sql.gz."""
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    if tar_path.exists():
        tar_path.unlink()
    with tarfile.open(tar_path, "w:gz") as tf:
        for p in sorted(src_dir.glob("*.sql.gz")):
            tf.add(p, arcname=f"dumps/{p.name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", type=Path, default=None,
                    help="download the pinned dumps into this dir")
    ap.add_argument("--from-dir", type=Path, default=None,
                    help="use an existing (already downloaded) dir")
    ap.add_argument("--tar", type=Path, default=None,
                    help="also write a dumps/ tarball here")
    args = ap.parse_args()

    src = args.from_dir or args.dest
    if src is None:
        ap.error("pass --dest and/or --from-dir")
    if args.dest is not None:
        err = fetch_into(args.dest)
        if err:
            return 1
    bad = verify(src)
    if bad:
        print(f"{len(bad)} dump(s) failed verification", file=sys.stderr)
        return 1
    n = len(model_fetcher.dump_pin_list())
    total = sum(f["size"] for f in model_fetcher.dump_pin_list())
    print(f"verified {n} pinned dumps ({total} B) in {src}")
    if args.tar is not None:
        make_tar(src, args.tar)
        print(f"tarball: {args.tar} ({args.tar.stat().st_size} B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
