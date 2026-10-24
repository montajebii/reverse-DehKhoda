"""MySQL dump parser + ingestion unit tests."""

from __future__ import annotations

import sqlite3

from app.ingestion.dehkhoda_loader import DehkhodaLoader
from app.ingestion.mysql_dump_parser import (
    clean_html,
    iter_dump,
    make_document,
    mysql_unescape,
)
from tests.conftest import SAMPLE_ENTRIES


def test_mysql_unescape():
    assert mysql_unescape(r'html with \"quotes\" and \'apostrophe\'') == (
        'html with "quotes" and \'apostrophe\''
    )
    assert mysql_unescape("a''b") == "a'b"
    assert mysql_unescape("line\\nbreak") == "line\nbreak"
    assert mysql_unescape("back\\\\slash") == "back\\slash"
    assert mysql_unescape("plain") == "plain"


def test_clean_html_strips_markup():
    raw = '<span class=\\"hlight\\">عشق .</span> <font color=\\"Red\\">مولوی.</font><br />بعد از فوت'
    out = clean_html(raw)
    assert "<span" not in out and "<font" not in out and "<br" not in out
    assert "مولوی." in out and "\n" in out


def test_make_document_locked_format():
    doc = make_document("عشق", "تعلق قلب به کسی .")
    assert doc == "واژه: عشق\nتعریف: تعلق قلب به کسی ."


def test_parse_synthetic_dump(sample_dump):
    rows = list(iter_dump(sample_dump))
    assert len(rows) == len(SAMPLE_ENTRIES)
    by_id = {r.id: r for r in rows}
    assert by_id[183618].word == "عشق"
    assert "تعلق قلب" in by_id[183618].meaning


def test_staging_import(staging_db):
    con = sqlite3.connect(staging_db)
    n = con.execute("SELECT COUNT(*) FROM words").fetchone()[0]
    con.close()
    assert n == len(SAMPLE_ENTRIES)


def test_chunking_short_definition(analyzer, settings):
    loader = DehkhodaLoader(settings, analyzer, None, None, None)
    parts = loader.chunk_definition("کوتاه است.")
    assert parts == ["کوتاه است."]


def test_chunking_long_definition_with_overlap(analyzer, settings):
    settings.chunk_chars = 300
    settings.chunk_overlap = 50
    loader = DehkhodaLoader(settings, analyzer, None, None, None)
    long_def = " . ".join(f"جملهٔ شمارهٔ {i} دربارهٔ معنای واژه" for i in range(60))
    parts = loader.chunk_definition(long_def)
    assert len(parts) > 1
    assert all(len(p) <= 300 for p in parts)
    # overlap: consecutive chunks share tail/head content
    assert parts[1][:20] in parts[0] or parts[0][-50:] in parts[1]


def test_download_file_list_is_exact():
    from app.ingestion.dehkhoda_loader import DUMP_FILES, EXPECTED_ROWS

    assert len(DUMP_FILES) == 33
    assert DUMP_FILES[0] == "01-A" and DUMP_FILES[-1] == "33-Ye"
    assert EXPECTED_ROWS == 312_507
