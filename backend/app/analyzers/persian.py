"""Persian text analyzer — Hazm (primary), Parsivar (speed fallback), regex
(last resort).

Produces tokens suitable for both BM25 sparse encoding (through the
`milvus_model` `Analyzer` adapter) and general app use (query cleaning).

Design notes
------------
* Arabic characters are folded to their Persian forms (ي->ی, ك->ک, ...).
* Diacritics (tashkeel) are stripped; ZWNJ is preserved inside words by the
  normalizer and used as a token boundary hint by the tokenizer.
* Persian stopwords (Hazm list, 389 entries) are removed for the sparse lane
  only; the dense lane embeds the *normalized but unfiltered* text.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, List, Protocol

logger = logging.getLogger(__name__)

# --- Arabic -> Persian folding + noise removal -------------------------------
_ARABIC_MAP = str.maketrans(
    {
        "ي": "ی", "ك": "ک", "ؤ": "و", "إ": "ا", "أ": "ا", "ٱ": "ا",
        "ۀ": "ه", "ة": "ه", "ى": "ی", "ﻻ": "لا",
    }
)
_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")  # tashkeel + tatweel
_DIGITS_MAP = str.maketrans(
    {
        **{c: p for c, p in zip("0123456789", "۰۱۲۳۴۵۶۷۸۹")},
        **{c: p for c, p in zip("٠١٢٣٤٥٦٧٨٩", "۰۱۲۳۴۵۶۷۸۹")},
    }
)
_TOKEN_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u200c]+|\d+")


class TokenizerFn(Protocol):
    def __call__(self, text: str) -> List[str]: ...


def regex_normalize(text: str) -> str:
    """Zero-dependency Persian normalization (used when Hazm/Parsivar absent)."""
    text = text.translate(_ARABIC_MAP).translate(_DIGITS_MAP)
    text = _DIACRITICS_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def regex_tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall(text)


class PersianAnalyzer:
    """Facade over the selected engine. Exposes normalize()/tokenize()/analyze()."""

    def __init__(self, engine: str = "hazm") -> None:
        self.engine = engine
        self._norm: Callable[[str], str]
        self._tok: TokenizerFn
        self._stop: set[str]
        self._impl = None
        if engine == "regex":
            # explicit last-resort request: honor it directly
            self._init_regex()
            self.engine = "regex"
            return
        try:
            if engine == "hazm":
                self._init_hazm()
            elif engine == "parsivar":
                self._init_parsivar()
            else:
                raise ImportError(engine)
        except Exception as exc:  # graceful degradation chain: hazm -> parsivar -> regex
            logger.warning("analyzer engine %r unavailable (%s); falling back", engine, exc)
            try:
                self._init_parsivar()
                self.engine = "parsivar"
            except Exception:
                self._init_regex()
                self.engine = "regex"

    # -- engines ---------------------------------------------------------------
    def _init_hazm(self) -> None:
        from hazm import Normalizer, stopwords_list, word_tokenize

        self._hazm_norm = Normalizer()  # attribute, not closure: keeps pickling simple
        stops = set(stopwords_list())
        self._impl = "hazm"
        self._norm = self._norm_hazm
        self._tok = word_tokenize
        self._stop = stops

    def _init_parsivar(self) -> None:
        from parsivar import Normalizer as PNormalizer
        from parsivar import Tokenizer as PTokenizer

        self._pars_norm = PNormalizer()
        self._pars_tok = PTokenizer()
        stops: set[str] = set()
        self._impl = "parsivar"
        self._norm = self._norm_parsivar
        self._tok = self._tok_parsivar
        self._stop = stops

    def _init_regex(self) -> None:
        self._impl = "regex"
        self._norm, self._tok, self._stop = regex_normalize, regex_tokenize, set()

    # bound methods (picklable, engine-dispatched)
    def _norm_hazm(self, t: str) -> str:
        return self._hazm_norm.normalize(t)

    def _norm_parsivar(self, t: str) -> str:
        return self._pars_norm.normalize(t)

    def _tok_parsivar(self, t: str) -> List[str]:
        return self._pars_tok.tokenize_words(t)

    # -- public API ------------------------------------------------------------
    def normalize(self, text: str) -> str:
        return self._norm(text)

    def tokenize(self, text: str) -> List[str]:
        return self._tok(text)

    def analyze(self, text: str) -> List[str]:
        """normalize -> tokenize -> drop punctuation-only / empty tokens.
        Stopwords are NOT removed here (use analyze_for_bm25)."""
        toks = [t for t in self.tokenize(self.normalize(text)) if t.strip()]
        return toks

    def analyze_for_bm25(self, text: str) -> List[str]:
        """Like analyze() but also removes Persian stopwords — the lexical
        (sparse) lane should not waste dimensions on function words."""
        return [t for t in self.analyze(text) if t not in self._stop and not _is_punct(t)]

    @property
    def stopwords(self) -> set[str]:
        return self._stop


_PUNCT = set(".,!؟?;:؛،«»\"'()[]{}<>ـ-*+/\\|=#@%&$^~`…–—")


def _is_punct(tok: str) -> bool:
    return all(ch in _PUNCT for ch in tok)


# --- milvus_model Analyzer adapter (for BM25EmbeddingFunction) ----------------

def as_milvus_analyzer(analyzer: PersianAnalyzer):
    """Wrap PersianAnalyzer into a `pymilvus.model.sparse.bm25.tokenizers.Analyzer`.

    The BM25EmbeddingFunction only relies on analyzer(text) -> list[str],
    which our facade already satisfies directly (duck-typed).
    """
    return analyzer.analyze_for_bm25
