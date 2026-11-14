"""Conservative normalization with source-line provenance."""
import re
import unicodedata

from input_processing.patterns import _is_heading_like, _is_structural, _is_table_line
from input_processing.schemas.processed_contract import CleanedDocument, CleanLine

_PAGE_LABEL = re.compile(r"^(?:trang|page)\s*\d+(?:\s*(?:/|of|trên)\s*\d+)?$", re.I)
_CONTROLS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")
_SPACES = re.compile(r"[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")


def normalize_text(raw: str) -> CleanedDocument:
    lines = []
    page = 1 if "\f" in raw else None
    cursor, number = 0, 1
    for match in re.finditer(r"\r\n|[\r\n\v\f]|\Z", raw):
        value = raw[cursor:match.start()]
        text = unicodedata.normalize("NFC", value)
        text = _CONTROLS.sub("", text).replace("\ufeff", "").replace("\u200b", "")
        text = _SPACES.sub(" ", text).strip(" ")
        # Keep empty cells and column order. Aligned PDF rows remain verbatim.
        if "\t" in text:
            text = "| " + " | ".join(cell.strip() for cell in text.split("\t")) + " |"
        elif not _is_table_line(text):
            text = re.sub(r" {2,}", " ", text)
        source = {"raw_start": cursor, "raw_end": match.start(), "line": number, "page": page}
        lines.append(CleanLine(text, [source]))
        if match.group() == "\f":
            page += 1
        cursor = match.end()
        number += 1

    stats = {"page_noise_lines_removed": 0, "wrapped_lines_joined": 0}
    # Only explicit page labels at physical page boundaries. Repeated headings,
    # standalone amounts and clause numbers are contract content, not proven noise.
    edges = set()
    if page is not None:
        by_page = {}
        for i, line in enumerate(lines):
            if line.text:
                by_page.setdefault(line.sources[0]["page"], []).append(i)
        for indices in by_page.values():
            edges.update((indices[0], indices[-1]))
    joined = []
    for i, line in enumerate(lines):
        if i in edges and _PAGE_LABEL.fullmatch(line.text):
            stats["page_noise_lines_removed"] += 1
            continue
        prev = joined[-1] if joined else None
        if (prev and prev.text and line.text
                and prev.sources[-1]["page"] == line.sources[0]["page"]
                and not _is_heading_like(prev.text) and not _is_structural(line.text)
                and not re.search(r"[.;:!?\-–]$", prev.text)
                and line.text[0].islower()):
            prev.text += " " + line.text
            prev.sources.extend(line.sources)
            stats["wrapped_lines_joined"] += 1
        else:
            joined.append(line)
    while joined and not joined[0].text:
        joined.pop(0)
    while joined and not joined[-1].text:
        joined.pop()
    offset = 0
    for line in joined:
        line.start, line.end = offset, offset + len(line.text)
        offset = line.end + 1
    return CleanedDocument("\n".join(line.text for line in joined), joined, stats)


def clean_text(raw: str) -> tuple[str, dict]:
    document = normalize_text(raw)
    return document.text, document.stats
