"""index_restore unit tests — marker state machine + guard rails (offline).

Replays the v1.3.1 index.ready semantics without any engine:

  * index_state transitions: absent -> ready (after a simulated restore
    write) -> stale (pin drift: model revision / dataset sha / qdrant
    version / record count) -> partial (corrupt marker);
  * write_ready mirrors pins.prebuilt_index into the marker;
  * restore_bundled refuses a snapshot whose sha256 mismatches the pin
    BEFORE touching any engine (fail loudly, never ship truncated);
  * rebuild_from_bundled_sql fails with a clear error when no bundled
    dumps exist;
  * bundle_capabilities / ensure_index fast path with a ready marker.

Run:  python3 bootstrap/tests/test_index_restore.py   (exit 0 = all pass)
"""

from __future__ import annotations

import gzip
import hashlib
import json
import pickle
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from bootstrap import index_restore, model_fetcher, paths  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


FAKE_PINS = {
    "qdrant_version": "v1.20.0",
    "embedding_model_revision": "d12875rev",
    "dataset_commit_sha": "185ed68dsnapshot",
    "collection_name": "dehkhoda",
    "record_count": 1234,
    "snapshot_sha256": "",
    "snapshot_size_bytes": 0,
}

# full pins for the bm25 install path (model_fetcher.load_pins is patched to
# return this during the restore happy-path simulation)
FAKE_PINS_FULL = dict(FAKE_PINS, bm25_stats={"sha256": "", "sample_size": 2})
# NOTE: install_bundled_bm25 reads pins.bm25_stats.sha256; the happy-path
# test rewrites the sha256 at runtime to the fixture pickle's real digest.


class _PinsPatch:
    """Monkeypatch index_restore.prebuilt_pins for hermetic tests."""

    def __init__(self, pins: dict) -> None:
        self.pins = pins
        self._orig = index_restore.prebuilt_pins

    def __enter__(self) -> "_PinsPatch":
        index_restore.prebuilt_pins = lambda: dict(self.pins)  # type: ignore[assignment]
        return self

    def __exit__(self, *a: object) -> None:
        index_restore.prebuilt_pins = self._orig  # type: ignore[assignment]


def main() -> int:
    from bootstrap import state as state_mod

    with _PinsPatch(FAKE_PINS):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)

            # ---- 1. absent -------------------------------------------------
            check("fresh data dir -> index_state absent",
                  index_restore.index_state(base) == "absent")

            # ---- 2. write_ready + ready ------------------------------------
            meta = index_restore.write_ready(base, record_count=1234,
                                             mode="restored",
                                             source_sha256="ab" * 32,
                                             duration_ms=42_000,
                                             snapshot_sha256="cd" * 32)
            check("write_ready mirrors pins metadata",
                  meta["qdrant_version"] == FAKE_PINS["qdrant_version"]
                  and meta["model_revision"] == FAKE_PINS["embedding_model_revision"]
                  and meta["dataset_sha"] == FAKE_PINS["dataset_commit_sha"]
                  and meta["record_count"] == 1234)
            on_disk = json.loads((base / "index.ready").read_text(encoding="utf-8"))
            check("marker on disk matches returned meta", on_disk == meta)
            check("index_state -> ready after restore write",
                  index_restore.index_state(base) == "ready")

            # ---- 3. stale: any pin drift ------------------------------------
            drifted = dict(FAKE_PINS, embedding_model_revision="NEWrev")
            with _PinsPatch(drifted):
                check("model revision drift -> stale",
                      index_restore.index_state(base) == "stale")
            with _PinsPatch(dict(FAKE_PINS, dataset_commit_sha="other")):
                check("dataset sha drift -> stale",
                      index_restore.index_state(base) == "stale")
            with _PinsPatch(dict(FAKE_PINS, qdrant_version="v1.21.0")):
                check("qdrant version drift -> stale",
                      index_restore.index_state(base) == "stale")
            with _PinsPatch(dict(FAKE_PINS, record_count=999)):
                check("record_count drift -> stale",
                      index_restore.index_state(base) == "stale")

            # ---- 4. partial: corrupt / incomplete marker --------------------
            (base / "index.ready").write_text("{not json", encoding="utf-8")
            check("corrupt marker -> partial",
                  index_restore.index_state(base) == "partial")
            (base / "index.ready").write_text(json.dumps({"record_count": 5}),
                                              encoding="utf-8")
            check("incomplete marker -> partial",
                  index_restore.index_state(base) == "partial")

            # ---- 5. restore refuses sha mismatch before any engine touch ----
            with tempfile.TemporaryDirectory() as td5:
                base5 = Path(td5)
                with _PinsPatch(dict(FAKE_PINS, snapshot_sha256="ff" * 32)):
                    payload = b"not-a-real-snapshot"
                    bad = base5 / "dehkhoda.snapshot.gz"
                    with gzip.open(bad, "wb") as fh:
                        fh.write(payload)
                    try:
                        index_restore.restore_bundled(snapshot_path=bad,
                                                      data_dir=base5,
                                                      on_progress=lambda p: None)
                        check("restore fails loudly on sha mismatch", False,
                              "no raise")
                    except RuntimeError as exc:
                        check("restore fails loudly on sha mismatch",
                              "sha256 mismatch" in str(exc), str(exc)[:120])
                    check("failed restore never writes the marker",
                          not (base5 / "index.ready").exists())

            # ---- 6. restore happy path via a FAKE engine client -------------
            # (real end-to-end restore runs against a live qdrant in
            # test_index_restore_live.py; here the engine boundary is faked)
            snapshot_tar = base / "mini.snapshot"
            snapshot_tar.write_bytes(b"PK-mini-snapshot-bytes")
            gz_path = base / "mini.snapshot.gz"
            with gzip.open(gz_path, "wb") as fh:
                fh.write(snapshot_tar.read_bytes())
            gz_sha = hashlib.sha256(gz_path.read_bytes()).hexdigest()

            calls: list[str] = []

            class FakeClient:
                def collection_exists(self, name: str) -> bool:
                    calls.append("exists")
                    return True

                def delete_collection(self, name: str) -> None:
                    calls.append("delete")

                def recover_snapshot(self, **kw: object) -> None:
                    calls.append(f"recover:{kw.get('location')}:"
                                 f"checksum={kw.get('checksum')}")
                    if kw.get("wait") is not True:
                        raise AssertionError("recover must wait")

                def count(self, name: str, exact: bool = False):
                    calls.append("count")

                    class R:
                        pass

                    R.count = 1234
                    return R()

                def close(self) -> None:
                    calls.append("close")

            orig_client = index_restore._native_client
            orig_snapdir = index_restore._snapshots_dir
            # v1.3.1 Option C fixture: the bundle must carry bm25_stats.pkl —
            # restore installs it into the data dir (ship-blocker closure)
            bm25_src = base / "bm25_stats_fixture.pkl"
            bm25_src.write_bytes(pickle.dumps(
                {"idf": [0.1, 0.2], "corpus_size": 2, "avgdl": 3.0,
                 "k1": 1.5, "b": 0.75, "epsilon": 0.25}))
            bm25_sha = hashlib.sha256(bm25_src.read_bytes()).hexdigest()
            orig_bundled_bm25 = paths.bundled_bm25_stats
            orig_load_pins = model_fetcher.load_pins
            paths.bundled_bm25_stats = lambda: bm25_src  # type: ignore[assignment]
            model_fetcher.load_pins = lambda: dict(
                FAKE_PINS_FULL,
                bm25_stats={"sha256": bm25_sha, "sample_size": 2})  # type: ignore[assignment]
            snap_stage = base / "server-snapshots"
            snap_stage.mkdir()
            index_restore._native_client = lambda timeout=600.0: (FakeClient(),  # type: ignore[assignment]
                                                                 "http://127.0.0.1:6333")
            index_restore._snapshots_dir = lambda: snap_stage  # type: ignore[assignment]
            try:
                with _PinsPatch(dict(FAKE_PINS, snapshot_sha256=gz_sha)):
                    stages: list[str] = []
                    res = index_restore.restore_bundled(
                        snapshot_path=gz_path, data_dir=base,
                        on_progress=lambda p: stages.append(p["stage"]))
                    check("restore reports mode=restored",
                          res["mode"] == "restored" and res["record_count"] == 1234)
                    check("restore observed all 5 stages",
                          set(stages) == {"verify", "extract", "restore", "count",
                                          "ready"}, ",".join(stages))
                    check("recover used file:// URI + checksum",
                          any(c.startswith("recover:file://") and "checksum=" in c
                              for c in calls), "|".join(calls[:4]))
                    ready = json.loads((base / "index.ready").read_text())
                    check("marker written with restored meta",
                          ready["mode"] == "restored"
                          and ready["record_count"] == 1234
                          and ready["snapshot_sha256"] == gz_sha)
                    # v1.3.1 Option C: the restore must install the BM25 stats
                    installed = base / "bm25_stats.pkl"
                    check("restore installed bm25_stats.pkl into the data dir",
                          installed.exists())
                    check("installed bm25 stats sha256 matches the pin",
                          installed.exists()
                          and hashlib.sha256(installed.read_bytes()).hexdigest()
                          == bm25_sha)
                    check("restore result + marker carry bm25_sha256",
                          res.get("bm25_sha256") == bm25_sha
                          and ready.get("bm25_sha256") == bm25_sha)
                    check("staged plain tar cleaned up",
                          not (index_restore._snapshots_dir()
                               / "dehkhoda.restore.snapshot").exists())
            finally:
                index_restore._native_client = orig_client  # type: ignore[assignment]
                index_restore._snapshots_dir = orig_snapdir  # type: ignore[assignment]
                paths.bundled_bm25_stats = orig_bundled_bm25  # type: ignore[assignment]
                model_fetcher.load_pins = orig_load_pins  # type: ignore[assignment]

            # ---- 7. rebuild without bundled dumps fails clearly -------------
            orig_dumps = paths.bundled_dumps_dir
            paths.bundled_dumps_dir = lambda: None  # type: ignore[assignment]
            try:
                try:
                    index_restore.rebuild_from_bundled_sql(data_dir=base)
                    check("rebuild w/o dumps fails loudly", False, "no raise")
                except FileNotFoundError as exc:
                    check("rebuild w/o dumps fails loudly",
                          "no bundled SQL dumps" in str(exc))
            finally:
                paths.bundled_dumps_dir = orig_dumps  # type: ignore[assignment]

            # ---- 8. verify_index metadata-only ------------------------------
            ok, detail = index_restore.verify_index(base, live=False)
            check("verify_index ready marker -> True", ok and detail["state"] == "ready")
            (base / "index.ready").unlink()
            ok, detail = index_restore.verify_index(base, live=False)
            check("verify_index absent -> False", not ok and detail["state"] == "absent")

        # ---- 9. fast-path ensure_index with a ready marker ------------------
        with tempfile.TemporaryDirectory() as td2:
            base = Path(td2)
            with _PinsPatch(FAKE_PINS):
                index_restore.write_ready(base, 1234, "restored", "ab" * 32, 100,
                                          "cd" * 32)
            orig_dd = paths.data_dir
            paths.data_dir = lambda: base  # type: ignore[assignment]
            orig_dd2 = index_restore.paths.data_dir

            class FakeEm:
                def __init__(self) -> None:
                    self.events: list[dict] = []

                def progress(self, **kw: object) -> None:
                    self.events.append(dict(kw))

            try:
                em = FakeEm()
                res = index_restore.ensure_index(em)
                check("fast path returns without engine contact",
                      res.get("fast_path") is True and res["record_count"] == 1234)
                ds = [e for e in em.events if
                      isinstance(e.get("data"), dict)
                      and e["data"].get("step") == "dataset"]
                check("explicit dataset event emitted (never silent)",
                      len(ds) == 1 and ds[0]["data"]["mode"] in
                      ("restored", "rebuilt_from_bundle"))
            finally:
                paths.data_dir = orig_dd  # type: ignore[assignment]

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
