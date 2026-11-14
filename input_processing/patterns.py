"""Structural recognizers adapted from the supplied contract_preprocessor.py."""
import re
from typing import Optional

_RE_CHAPTER = re.compile(
    r"^(?P<kw>CHƯƠNG|Chương|PHẦN|Phần|MỤC|Mục|CHAPTER|Chapter|PART|Part)\s+"
    r"(?P<num>[IVXLCDM]+|\d+)\b[\.:]?\s*(?P<title>.*)$"
)
_RE_ARTICLE = re.compile(
    r"^(?P<kw>Điều|ĐIỀU|Article|ARTICLE|Section|SECTION)\s+(?P<num>\d+[a-z]?)"
    r"(?P<sep>\s*[\.:\-–]\s*|\s+|$)(?P<title>.*)$"
)
_RE_APPENDIX = re.compile(r"^(PHỤ LỤC|Phụ lục|APPENDIX|Appendix|ANNEX|Annex|SCHEDULE|Schedule)\b.*$")
_RE_NUM_CLAUSE = re.compile(r"^\d+(?:\.\d+){1,3}[\.\)]?\s+\S")
_RE_NUM1_CLAUSE = re.compile(r"^\d{1,2}[\.\)]\s+\S")
_RE_LETTER_CLAUSE = re.compile(r"^[a-zđ][\.\)]\s+\S")
_RE_BULLET = re.compile(r"^[-•–+*]\s+\S")
_RE_FIELD = re.compile(r"^(?:\S+ ){0,3}\S+:\s*\S")  # "Bên A: ...", "Địa chỉ: ..." -> dòng khai báo, không nối vào dòng trước


def _is_table_line(line: str) -> bool:
    s = line.strip()
    # Native PDF layout extraction uses wide spaces between columns. Preserve such
    # rows verbatim; this is a layout heuristic, not proof of a semantic table.
    return s.startswith("|") or " | " in s or len(re.findall(r" {3,}", s)) >= 2


def _is_article_heading(line: str) -> Optional[re.Match]:
    m = _RE_ARTICLE.match(line.strip())
    if not m:
        return None
    sep, title = m.group("sep"), m.group("title").strip()
    if len(line.strip()) > 160:
        return None
    # "Điều 5 của hợp đồng này..." là câu văn, không phải tiêu đề
    if title and not re.search(r"[\.:\-–]", sep) and title[0].islower():
        return None
    return m


def _is_caps_heading(line: str) -> bool:
    s = line.strip()
    letters = [c for c in s if c.isalpha()]
    return (
        2 <= len(s.split()) <= 14
        and len(s) <= 100
        and len(letters) >= 4
        and all(c.isupper() for c in letters)
        and not re.search(r"[:;\.]$|\d", s)
    )


def _is_heading_like(line: str) -> bool:
    s = line.strip()
    return bool(
        _RE_CHAPTER.match(s) or _is_article_heading(s) or _RE_APPENDIX.match(s)
        or _is_caps_heading(s) or _is_table_line(s)
    )


def _is_structural(line: str) -> bool:
    s = line.strip()
    return bool(
        _RE_CHAPTER.match(s) or _is_article_heading(s) or _RE_APPENDIX.match(s)
        or _RE_NUM_CLAUSE.match(s) or _RE_NUM1_CLAUSE.match(s)
        or _RE_LETTER_CLAUSE.match(s) or _RE_BULLET.match(s) or _RE_FIELD.match(s)
        or _is_caps_heading(s) or _is_table_line(s)
    )


