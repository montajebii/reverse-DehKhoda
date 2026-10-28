"""Pin/constant sync assertions — v1.3.1.

Guards the cross-layer constants that MUST stay identical but live in
modules that cannot import each other:

  * backend/app/embeddings/provider.py HF_OFFLINE_ENV
    == bootstrap/net_guard.py OFFLINE_ENV
    (the backend tests run with backend/ alone on sys.path, so provider.py
    carries its own copy — this test is the sync check);
  * backend/app/ingestion/dehkhoda_loader.py BASE_URL is pinned to the
    immutable dataset commit in bootstrap/pins.json (no more `master`);
  * dehkhoda_loader.DUMP_FILES == pins.dehkhoda_dataset.files (33 files,
    same order);
  * USABLE_ROWS/EXPECTED_ROWS agree between bootstrap.dehkhoda_ingest and
    the backend loader.

Run:  python3 bootstrap/tests/test_pins_sync.py   (exit 0 = all pass)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from bootstrap import net_guard  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    from app.embeddings import provider as emb_provider
    from app.ingestion import dehkhoda_loader as loader

    pins = json.loads((ROOT / "bootstrap" / "pins.json").read_text(encoding="utf-8"))

    # ---- 1. HF offline env sets are identical ---------------------------
    check("provider.HF_OFFLINE_ENV == net_guard.OFFLINE_ENV",
          emb_provider.HF_OFFLINE_ENV == net_guard.OFFLINE_ENV,
          f"{sorted(emb_provider.HF_OFFLINE_ENV)} vs {sorted(net_guard.OFFLINE_ENV)}")
    required = {"HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE",
                "HF_HUB_DISABLE_TELEMETRY", "HF_HUB_DISABLE_IMPLICIT_TOKEN",
                "DISABLE_TELEMETRY"}
    check("all six PART 2 §1 switches present",
          required <= set(emb_provider.HF_OFFLINE_ENV))

    # ---- 2. dataset URL pinned to the pins.json commit ------------------
    ds = pins["dehkhoda_dataset"]
    commit = ds["commit"]
    check("loader BASE_URL uses the pinned commit (not master)",
          commit in loader.BASE_URL and "/master" not in loader.BASE_URL,
          loader.BASE_URL)
    check("loader BASE_URL points at the pinned repo",
          f"raw.githubusercontent.com/{ds['repo']}/" in loader.BASE_URL,
          loader.BASE_URL)

    # ---- 3. dump file lists identical -----------------------------------
    pinned_names = [f["name"] for f in ds["files"]]
    check("loader.DUMP_FILES matches pins (names + order)",
          loader.DUMP_FILES == pinned_names,
          f"{len(loader.DUMP_FILES)} vs {len(pinned_names)}")
    check("exactly 33 pinned dumps", len(pinned_names) == 33)

    # ---- 4. row-count constants agree -----------------------------------
    from bootstrap import dehkhoda_ingest

    check("USABLE_ROWS agrees (bootstrap vs backend loader)",
          dehkhoda_ingest.USABLE_ROWS == loader.USABLE_ROWS,
          f"{dehkhoda_ingest.USABLE_ROWS} vs {loader.USABLE_ROWS}")
    check("USABLE_ROWS == EXPECTED_ROWS - 1",
          loader.USABLE_ROWS == loader.EXPECTED_ROWS - 1)
    check("pins.prebuilt_index dense_dim == embedding_model.dim",
          pins["prebuilt_index"]["dense_dim"] == pins["embedding_model"]["dim"])
    check("pins.prebuilt_index model_revision == embedding_model.revision",
          pins["prebuilt_index"]["embedding_model_revision"]
          == pins["embedding_model"]["revision"])
    check("pins.prebuilt_index dataset_commit_sha == dehkhoda_dataset.commit",
          pins["prebuilt_index"]["dataset_commit_sha"] == ds["commit"])

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
