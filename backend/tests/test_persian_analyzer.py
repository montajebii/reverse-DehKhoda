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
