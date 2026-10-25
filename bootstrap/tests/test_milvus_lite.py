"""Build-time capability smoke for Milvus Lite (decision-table rule 3).

Asserts the installed `pymilvus` wheel really supports our hybrid schema
(dense + sparse_float_vector + hybrid_search with RRFRanker). Runs inside
the release workflow's `python:3.12-slim` container with only `pymilvus`
installed, so it MUST NOT import any heavy backend dependency at module
level — `bootstrap.backend_launcher` keeps torch / pymilvus imports lazy
inside `smoke_milvus_lite`, which is what makes this thin wrapper safe.

Exit code 0 = smoke passed, 1 = failed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bootstrap.backend_launcher import smoke_milvus_lite  # noqa: E402


class _StdoutEmitter:
    """Minimal Emitter so smoke_milvus_lite can report progress."""

    def progress(self, **kw: object) -> None:
        print("   ", kw)


def main() -> int:
    ok = smoke_milvus_lite(_StdoutEmitter())
    print("milvus_lite smoke:", ok)
    assert ok, "Milvus Lite hybrid capability smoke FAILED"
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
