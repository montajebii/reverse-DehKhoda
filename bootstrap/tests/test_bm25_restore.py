"""bm25 runtime-wiring tests — v1.3.1 Option C ship-blocker closure.

Contract under test (user-mandated):
  * after index_restore on a fixture bundle, data_dir/bm25_stats.pkl EXISTS
    and its sha256 matches pins.bm25_stats.sha256;
  * a missing or hash-mismatched bundle pkl fails LOUDLY with a Persian
    message — never a silent refit;
  * the copy is idempotent and atomic (tmp + replace).

install_bundled_bm25() is the exact function restore_bundled() calls between
the record-count verification and the index.ready marker write, so asserting
it directly asserts the restore path without needing a live qdrant server
(server mode is unavailable to unit tests by design).

Run:  python3 bootstrap/tests/test_bm25_restore.py   (exit 0 = all pass)
"""

from __future__ import annotations

import hashlib
import pickle
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from bootstrap import index_restore, model_fetcher, paths  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

PERSIAN_MARKER = "BM25"


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _fake_pins(bm25_sha: str) -> dict:
    return {
        "prebuilt_index": {
            "qdrant_version": "v1.20.0",
            "embedding_model_revision": "d12875rev",
            "dataset_commit_sha": "185ed68dsnapshot",
            "collection_name": "dehkhoda",
            "record_count": 1234,
            "snapshot_sha256": "",
        },
        "bm25_stats": {"sha256": bm25_sha, "sample_size": 4},
    }


class _Patches:
    """Patch model_fetcher.load_pins + paths.bundled_bm25_stats hermetically."""

    def __init__(self, pins: dict, bundled: Path) -> None:
        self.pins = pins
        self.bundled = bundled
        self._orig_pins = model_fetcher.load_pins
        self._orig_bundled = paths.bundled_bm25_stats

    def __enter__(self) -> "_Patches":
        model_fetcher.load_pins = lambda: dict(self.pins)  # type: ignore[assignment]
        paths.bundled_bm25_stats = lambda: self.bundled  # type: ignore[assignment]
        return self

    def __exit__(self, *a: object) -> None:
        model_fetcher.load_pins = self._orig_pins  # type: ignore[assignment]
        paths.bundled_bm25_stats = self._orig_bundled  # type: ignore[assignment]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # ---- fixture bundle: _internal/index/bm25_stats.pkl ----------------
        bundle = root / "app" / "_internal" / "index"
        bundle.mkdir(parents=True)
        stats = {"idf": [0.1, 0.2, 0.3], "corpus_size": 4, "avgdl": 7.5,
                 "k1": 1.5, "b": 0.75, "epsilon": 0.25}
        pkl = bundle / "bm25_stats.pkl"
        pkl.write_bytes(pickle.dumps(stats))
        pkl_sha = _sha(pkl)
        data_dir = root / "data"

        # 1) happy path: bundled pkl -> data_dir, sha256 matches the pin
        with _Patches(_fake_pins(pkl_sha), pkl):
            info = index_restore.install_bundled_bm25(data_dir)
        dst = data_dir / "bm25_stats.pkl"
        check("restored bundle installs bm25_stats.pkl into data_dir",
              dst.exists(), str(dst))
        check("installed pkl sha256 == pins.bm25_stats.sha256",
              dst.exists() and _sha(dst) == pkl_sha,
              f"{_sha(dst)[:12] if dst.exists() else '-'} vs {pkl_sha[:12]}")
        check("install report carries the pinned sha", info.get("sha256") == pkl_sha)
        check("restored pickle is semantically the fitted stats",
              dst.exists() and pickle.loads(dst.read_bytes()) == stats)

        # 2) idempotent: a second install does not rewrite identical bytes
        mtime = dst.stat().st_mtime_ns
        with _Patches(_fake_pins(pkl_sha), pkl):
            index_restore.install_bundled_bm25(data_dir)
        check("re-install is idempotent (no rewrite when identical)",
              dst.stat().st_mtime_ns == mtime)

        # 3) pin mismatch -> loud failure with Persian message
        with _Patches(_fake_pins("0" * 64), pkl):
            try:
                index_restore.install_bundled_bm25(data_dir)
                check("mismatched pin raises", False, "no exception")
            except RuntimeError as exc:
                msg = str(exc)
                check("mismatched pin raises", True)
                check("mismatch failure carries the Persian message",
                      "مطابقت ندارد" in msg and PERSIAN_MARKER in msg,
                      msg[:80])
                check("mismatch failure names both hashes",
                      "0" * 12 in msg and pkl_sha[:12] in msg)

        # 4) missing bundle pkl -> loud failure, data_dir untouched
        data_dir2 = root / "data2"
        with _Patches(_fake_pins(pkl_sha), None):
            try:
                index_restore.install_bundled_bm25(data_dir2)
                check("missing bundle pkl raises", False, "no exception")
            except RuntimeError as exc:
                msg = str(exc)
                check("missing bundle pkl raises", True)
                check("missing failure carries the Persian message",
                      "یافت نشد" in msg and PERSIAN_MARKER in msg, msg[:80])
        check("failed install leaves data_dir without a pkl",
              not (data_dir2 / "bm25_stats.pkl").exists())

        # 5) restore-flow integration: restore_bundled calls the installer
        #    between count-verify and marker write — assert by source order
        src = (ROOT / "bootstrap" / "index_restore.py").read_text(encoding="utf-8")
        i_install = src.find("bm25_install = install_bundled_bm25(base)")
        i_marker = src.find("meta = write_ready(base, count")
        i_count = src.find('prog("count", gz_bytes_total, gz_bytes_total)')
        check("installer runs before the index.ready marker write",
              0 < i_count < i_install < i_marker,
              f"count@{i_count} install@{i_install} marker@{i_marker}")
        check("restore flow has no silent except around the installer",
              "except" not in src[i_install:i_marker])

        # 6) app.main startup assertion exists and is Persian-first
        main_src = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
        check("startup assertion present in app.main",
              "refusing to silently refit BM25" in main_src
              and "bm25_stats.pkl" in main_src)
        check("startup assertion is Persian-first",
              "فایل آمارهٔ BM25" in main_src)

        # 7) packaging stages the pkl beside the snapshot (both installers)
        sh_src = (ROOT / "installer" / "linux" / "appimage.sh").read_text(encoding="utf-8")
        check("linux stager stages _internal/index/bm25_stats.pkl",
              "_internal/index/bm25_stats.pkl" in sh_src
              and "DEHKHODA_BM25_STATS" in sh_src)
        wf_src = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        check("windows staging copies bm25_stats.pkl with sha assert",
              "_internal/index/bm25_stats.pkl" in wf_src
              and "bm25 stats sha mismatch" in wf_src)
        check("merge job asserts the artifact contains bm25_stats.pkl",
              "index-snapshot artifact contains bm25_stats.pkl" in wf_src)

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        for name, _, detail in failed:
            print(f"  FAILED: {name} {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
