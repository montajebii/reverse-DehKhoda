"""PART 2 §1 — bundled model loads with ZERO non-loopback socket connects.

The highest-risk network source is the HuggingFace hub client inside
sentence-transformers/transformers. This test proves the closure at the
socket level:

  1. monkeypatch socket.socket.connect (and create_connection /
     getaddrinfo) so ANY non-loopback address raises immediately;
  2. load the pinned e5 weights FROM DISK (DEHKHODA_MODEL_DIR, the bundled
     dir, or a models/<revision> dir under the data dir);
  3. embed one Persian query + one document;
  4. assert the model produced 768-dim vectors with zero connect() calls
     to non-loopback addresses.

Skips cleanly (exit 0, reason printed) when no local model dir exists —
CI's release build gate always provides one (model tarball extracted), so
the skip only ever happens in dev sandboxes without the weights.

Run:  python3 bootstrap/tests/test_offline_guard.py   (exit 0 = pass/skip)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from bootstrap import net_guard, paths  # noqa: E402


def find_model_dir() -> str:
    env = os.environ.get("DEHKHODA_MODEL_DIR", "")
    if env and (Path(env) / "model.safetensors").exists():
        return env
    bundled = paths.bundled_model_dir()
    if bundled:
        return str(bundled)
    root = paths.models_root()
    ready = paths.read_ready()
    rev = str(ready.get("revision") or "")
    if rev and (root / rev / "model.safetensors").exists():
        return str(root / rev)
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if (d / "model.safetensors").exists():
            return str(d)
    return ""


def main() -> int:
    model_dir = find_model_dir()
    if not model_dir:
        print("SKIP: no local model dir (set DEHKHODA_MODEL_DIR); "
              "CI release gate provides one")
        return 0

    net_guard.apply_offline_env()
    net_guard.install()
    try:
        from app.embeddings.provider import get_embedder
        from app.config import Settings

        s = Settings(backend="qdrant", embedder="e5-base", dense_dim=768,
                     model_dir=model_dir, embed_batch_size=4)
        emb = get_embedder(s)
        q = emb.embed_query("مایعی که از آسمان می‌بارد")
        d = emb.embed_documents(["آب: مایعی بی‌رنگ که در باران و دریاست."])
        assert len(q) == 768, f"unexpected query dim {len(q)}"
        assert len(d) == 1 and len(d[0]) == 768, f"unexpected doc dim {len(d[0])}"
        assert net_guard.guard_active(), "guard not installed during load"
        print(f"PASS: e5 loaded from {model_dir} with zero non-loopback "
              f"connect() calls (768-dim, query+doc embedded)")
        return 0
    except net_guard.NetworkAttemptError as exc:
        print("FAIL: a non-loopback connection was attempted during the "
              f"model load: {exc}")
        return 1
    finally:
        net_guard.uninstall()


if __name__ == "__main__":
    raise SystemExit(main())
