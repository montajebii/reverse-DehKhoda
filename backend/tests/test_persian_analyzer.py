"""Persian tokenization / normalization tests."""

from __future__ import annotations

import pytest

from app.analyzers.persian import PersianAnalyzer, regex_normalize, regex_tokenize


@pytest.fixture(scope="module")
def az():
    return PersianAnalyzer("hazm")


def test_engine_is_hazm(az):
    assert az.engine == "hazm"


def test_arabic_chars_folded_to_persian(az):
    assert az.normalize("ي ك") == "ی ک"
    assert az.normalize("كتاب") == "کتاب"


def test_diacritics_and_tatweel(az):
    assert az.normalize("کتـابِ زیبـا") == "کتاب زیبا"


def test_ascii_digits_to_persian(az):
    assert az.normalize("2024") == "۲۰۲۴"


def test_word_tokenize_basic(az):
    toks = az.analyze("کتابِ خوب را خواندم.")
    assert "کتاب" in toks and "خواندم" in toks


def test_bm25_lane_drops_stopwords_and_punct(az):
    toks = az.analyze_for_bm25("عشق را فروختم .")
    assert "را" not in toks
    assert all(t.strip(".,!؟") for t in toks)


def test_regex_fallback_engine():
    az = PersianAnalyzer("regex")
    assert az.engine == "regex"
    assert az.normalize("كتاب ي 2024") == "کتاب ی ۲۰۲۴"
    assert "کتاب" in regex_tokenize("این کتاب")


def test_parsivar_fallback_engine(az):
    az2 = PersianAnalyzer("parsivar")
    assert az2.engine in ("parsivar", "regex")  # regex if parsivar missing
    assert az2.analyze("عشق و کتاب")


def test_hazm_unavailable_falls_back():
    # simulate missing hazm by requesting a bogus engine
    az = PersianAnalyzer("does-not-exist")
    assert az.engine in ("parsivar", "regex")
    assert az.analyze("عشق")


def test_regex_normalize_no_dependency():
    assert regex_normalize("كِتـاب") == "کتاب"


# --- headword_key: canonical keys for exact fallback + grouping ---------------

def test_headword_key_strips_diacritics_and_marks():
    from app.analyzers.persian import headword_key

    assert headword_key("سُودا") == "سودا"
    assert headword_key("عِشق") == "عشق"
    assert headword_key("کتـابِ") == "کتاب"


def test_headword_key_removes_whitespace_and_zwnj():
    from app.analyzers.persian import headword_key

    assert headword_key("می‌ روم") == "میروم"
    assert headword_key("  عشق  ") == "عشق"
    assert headword_key("عشق.") == "عشق"


def test_headword_key_folds_arabic_chars():
    from app.analyzers.persian import headword_key

    assert headword_key("كتاب") == "کتاب"   # Arabic kaf -> Persian kaf
    assert headword_key("عـشـق") == "عشق"   # tatweel removed


def test_headword_key_is_engine_independent():
    """The key must be identical regardless of the analyzer engine, because
    keys written at ingestion time are matched at search time."""
    from app.analyzers.persian import headword_key, regex_normalize

    for w in ["سُودا", "وسواس", "می‌‌روم", "كِتـابِ", "اضطراب"]:
        assert headword_key(w) == headword_key(regex_normalize(w))
