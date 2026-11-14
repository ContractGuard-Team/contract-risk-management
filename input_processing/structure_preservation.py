"""Recognize chapters/articles/appendices without merging different article scopes."""
from input_processing.patterns import (
    _RE_APPENDIX, _RE_BULLET, _RE_CHAPTER, _RE_LETTER_CLAUSE, _RE_NUM_CLAUSE,
    _RE_NUM1_CLAUSE, _is_article_heading, _is_caps_heading, _is_table_line,
)
from input_processing.schemas.processed_contract import Block, CleanedDocument, Section


def parse_structure(document: CleanedDocument) -> tuple[list[Section], dict]:
    sections, scope = [], []
    current = Section([], [])
    stats = {"articles_detected": 0, "chapters_detected": 0, "tables_detected": 0}
    lines, i = document.lines, 0

    def flush():
        if current.blocks:
            sections.append(current)

    while i < len(lines):
        line = lines[i]
        text = line.text
        if not text:
            i += 1
            continue
        if _is_table_line(text):
            end = i + 1
            while end < len(lines) and _is_table_line(lines[end].text):
                end += 1
            current.blocks.append(Block("table", document.text[line.start:lines[end-1].end],
                                        line.start, lines[end-1].end))
            stats["tables_detected"] += 1
            i = end
            continue
        chapter, article = _RE_CHAPTER.match(text), _is_article_heading(text)
        appendix = _RE_APPENDIX.match(text)
        if chapter or article or appendix:
            flush()
            heading_end = line.end
            # Only clearly marked uppercase titles may be consumed from the next line.
            title_missing = (chapter and not chapter.group("title")) or (article and not article.group("title"))
            if title_missing and i + 1 < len(lines) and _is_caps_heading(lines[i + 1].text):
                i += 1
                heading_end = lines[i].end
            heading = Block("heading", document.text[line.start:heading_end], line.start, heading_end)
            if chapter or appendix:
                scope = [heading.text]
                labels = []
                path = scope[:]
                stats["chapters_detected"] += int(bool(chapter))
            else:
                labels = [f"{article.group('kw').capitalize()} {article.group('num')}"]
                path = scope + [heading.text]
                stats["articles_detected"] += 1
            current = Section(path, labels, [heading], heading)
        else:
            clause = any(p.match(text) for p in (_RE_NUM_CLAUSE, _RE_NUM1_CLAUSE, _RE_LETTER_CLAUSE, _RE_BULLET))
            current.blocks.append(Block("clause" if clause else "paragraph", text, line.start, line.end))
        i += 1
    flush()
    stats["sections"] = len(sections)
    return sections, stats
