"""Dense embedding providers.

Locked options (spec):
  * intfloat/multilingual-e5-base — 768 dims (DEFAULT: best size/quality for a
    desktop app; requires "query: "/"passage: " prefixes)
  * BAAI/bge-m3 — 1024 dims (higher quality, larger; no prefixes)
  * hash — deterministic feature-hashing embedder (tests/CI only, no download)

Models load lazily so the sidecar's /health is instant and ingestion can start
before the model is warm.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# v1.3.1 zero-network guarantee (PART 2 §1): sentence-transformers and
# transformers contact huggingface.co on import/model-load unless told not
# to. These switches MUST be set before the first sentence_transformers
# import. Mirrors bootstrap.net_guard.OFFLINE_ENV (kept in sync by
# bootstrap/tests/test_pins_sync.py — this module cannot import bootstrap:
# the backend tests run with backend/ alone on sys.path).
HF_OFFLINE_ENV: dict[str, str] = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
    "DISABLE_TELEMETRY": "1",
}


def apply_hf_offline_env() -> None:
    """Set the HF offline switches (idempotent, setdefault semantics).

    Called from provider._ensure_model right before the heavy import and
    from the sidecar entrypoints (bootstrap.cli.main / entry.main via
    net_guard.activate) so NO transformers import ever happens without it.
    """
    for k, v in HF_OFFLINE_ENV.items():
        os.environ.setdefault(k, v)

_MODEL_DIMS = {"e5-base": 768, "bge-m3": 1024, "hash": 256}

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class EmbeddingProvider:
    """Base class: embed_query / embed_documents, lazy model loading."""

    name = "abstract"
    dim: int = 0
    query_prefix = ""
    doc_prefix = ""

    def __init__(self, model_name: str, dim: int, device: str = "cpu") -> None:
        self.model_name = model_name
        self.dim = dim
        self.device = device
        self._model = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _ensure_model(self):  # pragma: no cover - heavy path
        if self._model is None:
            apply_hf_offline_env()  # MUST precede the sentence_transformers import
            from sentence_transformers import SentenceTransformer

            # local_files_only=True: never resolve from the hub, even as a
            # "just check for updates" round-trip. The model id is either a
            # local directory (bundled/downloaded weights) or, in dev, the
            # hub id — with the offline env set the hub path fails fast
            # instead of silently phoning home.
            local_only = Path(self.model_name).is_dir()
            logger.info("loading embedding model %s on %s (local_files_only=%s) ...",
                        self.model_name, self.device, local_only)
            self._model = SentenceTransformer(self.model_name, device=self.device,
                                              local_files_only=local_only)
        return self._model

    def _encode(self, texts: list[str], prefix: str, batch_size: int = 32) -> list[list[float]]:
        model = self._ensure_model()
        vecs = model.encode(
            [prefix + t for t in texts],
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vecs]

    def embed_query(self, text: str) -> list[float]:
        return self._encode([text], self.query_prefix)[0]

    def embed_documents(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        return self._encode(texts, self.doc_prefix, batch_size)


class E5Embedder(EmbeddingProvider):
    name = "e5-base"

    def __init__(self, device: str = "cpu", model_id: str = "intfloat/multilingual-e5-base") -> None:
        super().__init__(model_id, 768, device)
        self.query_prefix = "query: "
        self.doc_prefix = "passage: "


class BGEM3Embedder(EmbeddingProvider):
    name = "bge-m3"

    def __init__(self, device: str = "cpu", model_id: str = "BAAI/bge-m3") -> None:
        super().__init__(model_id, 1024, device)  # dense output of bge-m3


class HashingEmbedder(EmbeddingProvider):
    """Deterministic, dependency-free embedder for tests/CI.

    Feature hashing over word unigrams + character 3-grams, L2-normalized.
    Not semantically deep, but stable and fast — good for verifying plumbing.
    """

    name = "hash"

    def __init__(self, dim: int = 256) -> None:
        super().__init__("hash", dim, "cpu")

    def _feat(self, s: str) -> float:
        h = hashlib.md5(s.encode("utf-8")).digest()
        sign = 1.0 if h[0] % 2 else -1.0
        return sign

    def _bucket(self, s: str) -> int:
        h = hashlib.md5(s.encode("utf-8")).digest()
        return int.from_bytes(h[:4], "little") % self.dim

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        norm = text.lower().replace("\u200c", " ")
        toks = _TOKEN_RE.findall(norm)
        feats: list[str] = []
        feats.extend(toks)
        for t in toks:
            padded = f"^{t}$"
            feats.extend(padded[i : i + 3] for i in range(max(1, len(padded) - 2)))
        for f in feats:
            vec[self._bucket(f)] += self._feat(f)
        norm_val = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm_val for v in vec]

    def _encode(self, texts: list[str], prefix: str, batch_size: int = 32) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]


def get_embedder(settings, device: str = "cpu") -> EmbeddingProvider:
    kind = (settings.embedder or "e5-base").lower()
    if kind == "e5-base":
        # Offline desktop installs point at the bootstrap-fetched weights dir.
        model_dir = str(getattr(settings, "model_dir", "") or "").strip()
        if model_dir and Path(model_dir).is_dir() and \
                (Path(model_dir) / "config.json").exists():
            return E5Embedder(device=device, model_id=model_dir)
        return E5Embedder(device=device)
    if kind == "bge-m3":
        return BGEM3Embedder(device=device)
    if kind == "hash":
        return HashingEmbedder(dim=int(settings.dense_dim) or 256)
    raise ValueError(f"unknown embedder {kind!r} (e5-base | bge-m3 | hash)")


def embedder_for_model(model: str) -> Optional[str]:
    return {"e5-base": "e5-base", "bge-m3": "bge-m3"}.get(model)
