"""Streaming parser for phpMyAdmin/MySQL dump files (Dehkhoda-SQL dataset).

The source files are gzip-compressed MySQL dumps produced by phpMyAdmin 4.7.7
(MariaDB 10.1.30).  Each file contains exactly one table `words` with three
columns (id INT PK, word TEXT, meaning TEXT) and one giant multi-row INSERT
statement:

    INSERT INTO `words` (`id`, `word`, `meaning`) VALUES
    (1, 'آ', '<span class=\\"hlight\\">آ.</span> ...'),
    (2, '...', '...');

Strings use MySQL escaping rules (backslash escapes ON: \\" \\' \\\\ \\n \\r \\t
\\0 \\b \\Z) plus the doubled-quote convention '' -> '.

The parser is streaming (file at a time, rows yielded lazily) and needs no
third-party dependencies.
"""

from __future__ import annotations

import gzip
import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

# Matches one MySQL single-quoted string body: plain chars, backslash escapes,
# or doubled quotes.
_SQL_STRING = r"'((?:[^'\\]|\\.|'')*)'"
# Matches one value tuple of the words table: (id, 'word', 'meaning')
_TUPLE_RE = re.compile(rf"\(\s*(\d+)\s*,\s*{_SQL_STRING}\s*,\s*{_SQL_STRING}\s*\)", re.DOTALL)
# The INSERT header is skipped; we simply scan the whole dump for value tuples.
_INSERT_RE = re.compile(r"INSERT\s+INTO\s+`?words`?")

_SIMPLE_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "0": "\0",
    "b": "\b",
    "Z": "\x1a",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "%": "\\%",
    "_": "\\_",
}

_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u200c]{2,}")
_NL_RE = re.compile(r"\n{3,}")


@dataclass
class RawEntry:
    """A single row of the Dehkhoda `words` table."""

    id: int
    word: str
    meaning: str  # raw, still containing HTML markup


def mysql_unescape(s: str) -> str:
    """Unescape a MySQL string body (both backslash escapes and '' pairs)."""
    if "\\" not in s and "''" not in s:
        return s
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            out.append(_SIMPLE_ESCAPES.get(nxt, nxt))
            i += 2
        elif c == "'" and i + 1 < n and s[i + 1] == "'":
            out.append("'")
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def clean_html(meaning: str) -> str:
    """Convert the HTML markup embedded in `meaning` into plain Persian text.

    * <br />        -> newline
    * other tags    -> removed
    * HTML entities -> decoded
    * whitespace    -> collapsed (newlines preserved, max 2 consecutive)
    """
    text = _BR_RE.sub("\n", meaning)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    # Normalise exotic whitespace but keep line structure for readability.
    text = text.replace("\r", "").replace("\u00a0", " ")
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


def parse_rows_from_sql(sql_text: str) -> Iterator[RawEntry]:
    """Yield RawEntry rows found in a decompressed SQL dump text."""
    for m in _TUPLE_RE.finditer(sql_text):
        yield RawEntry(
            id=int(m.group(1)),
            word=mysql_unescape(m.group(2)),
            meaning=mysql_unescape(m.group(3)),
        )


def iter_dump(path: str | Path, encoding: str = "utf-8") -> Iterator[RawEntry]:
    """Stream rows from a single .sql or .sql.gz dump file."""
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding=encoding, errors="replace") as fh:
        # Files are up to ~75 MB decompressed; process in one text blob per
        # file.  regex scanning is C-speed and stays well within memory budget.
        sql_text = fh.read()
    yield from parse_rows_from_sql(sql_text)


def iter_directory(directory: str | Path, files: Optional[list[str]] = None) -> Iterator[RawEntry]:
    """Stream rows from every .sql.gz dump in a directory (sorted order)."""
    directory = Path(directory)
    paths = sorted(directory.glob("*.sql.gz")) if files is None else [directory / f for f in files]
    for p in paths:
        yield from iter_dump(p)


def make_document(word: str, definition: str) -> str:
    """Convert one entry into the canonical RAG document form.

    Document format (locked by spec):  "واژه: {headword}\\nتعریف: {definition}"
    """
    return f"واژه: {word}\nتعریف: {definition}"
