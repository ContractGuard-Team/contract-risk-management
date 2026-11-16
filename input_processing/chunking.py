"""Budgeted structural chunks, including long words and oversized table rows."""
from dataclasses import replace
import hashlib
import re

from input_processing.schemas.processed_contract import Block, JobError


def _render(blocks):
    return "\n".join(block.text for block in blocks)


def _split_block(block, limit, count):
    remaining = block
    result = []
    while count(remaining.text) > limit:
        low, high, best = 1, len(remaining.text), 0
        while low <= high:
            middle = (low + high) // 2
            if count(remaining.text[:middle]) <= limit:
                best, low = middle, middle + 1
            else:
                high = middle - 1
        if not best:
            raise JobError("A single character cannot fit max_tokens with this counter")
        # Prefer sentence/word boundaries without dropping delimiters or punctuation.
        boundaries = [m.end() for m in re.finditer(r"\s+", remaining.text[:best])]
        boundary = next((p for p in reversed(boundaries) if p >= best // 2), best)
        piece = remaining.text[:boundary]
        if count(piece) > limit:
            boundary, piece = best, remaining.text[:best]
        result.append(replace(remaining, text=piece, end=remaining.start + boundary))
        remaining = replace(remaining, text=remaining.text[boundary:], start=remaining.start + boundary)
    if remaining.text:
        result.append(remaining)
    return result


def _table_units(block, limit, count, warnings):
    if count(block.text) <= limit:
        return [[block]]
    rows, offset = [], block.start
    for text in block.text.split("\n"):
        rows.append(Block("table", text, offset, offset + len(text)))
        offset += len(text) + 1
    # Markdown separator identifies a proven header. Plain-text first rows are data.
    has_header = len(rows) > 1 and bool(re.fullmatch(r"[| :\-]+", rows[1].text)) and "-" in rows[1].text
    header = rows[:2] if has_header else []
    units, current = [], []
    for row in rows:
        if current and count(_render(current + [row])) > limit:
            units.append(current)
            current = header[:] if has_header and count(_render(header + [row])) <= limit else []
        if count(_render(current + [row])) <= limit:
            current.append(row)
        else:
            if current:
                units.append(current)
                current = []
            warnings.append("Oversized table row split; use source_spans to reconstruct the original table.")
            units.extend([[piece] for piece in _split_block(row, limit, count)])
    if current:
        units.append(current)
    return units


def chunk_section(section, options, count, warnings):
    limit = options.max_tokens
    units = []
    for block in section.blocks:
        if block.kind == "table":
            units.extend(_table_units(block, limit, count, warnings))
        else:
            units.extend([[piece] for piece in _split_block(block, limit, count)])
    groups, current = [], []
    for unit in units:
        if current and count(_render(current + unit)) > limit:
            groups.append(current)
            carry = []
            for block in reversed(current):
                if block.kind == "heading" or count(_render([block] + carry)) > options.overlap_tokens:
                    break
                carry.insert(0, block)
            prefix = [section.heading] if options.prepend_heading and section.heading else []
            while carry and count(_render(prefix + carry + unit)) > limit:
                carry.pop(0)
            if count(_render(prefix + carry + unit)) > limit:
                prefix = []
                warnings.append("Repeated heading omitted to respect max_tokens; heading_path is retained.")
            current = prefix + carry
        current.extend(unit)
        if count(_render(current)) > limit:
            raise JobError("Chunk cannot fit max_tokens with the supplied token counter")
    if current:
        groups.append(current)
    return groups


def _source_spans(blocks, document):
    spans, seen = [], set()
    for block in blocks:
        for line in document.lines:
            if line.start >= block.end or line.end <= block.start:
                continue
            start, end = max(line.start, block.start), min(line.end, block.end)
            for source in line.sources:
                key = (start, end, source["raw_start"], source["raw_end"])
                if key not in seen:
                    spans.append({**source, "cleaned_start": start, "cleaned_end": end})
                    seen.add(key)
    return spans


def build_chunks(sections, job, document, count, warnings):
    chunks = []
    # min_tokens is a soft target: never merge distinct article/appendix identities.
    for section in sections:
        groups = chunk_section(section, job.options, count, warnings)
        for part, blocks in enumerate(groups):
            text = _render(blocks)
            index = len(chunks)
            spans = _source_spans(blocks, document)
            chunks.append({
                "chunk_id": f"{job.contract_id}::v{job.contract_version}::c{index:04d}",
                "contract_id": job.contract_id, "contract_version": job.contract_version, "chunk_index": index,
                "text": text, "token_count": count(text), "heading_path": section.heading_path[:],
                "articles": section.labels[:], "part_index": part, "part_count": len(groups),
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "hash_algorithm": "sha256", "source_spans": spans,
                "pages": sorted({span["page"] for span in spans if span["page"] is not None}),
                "source_metadata": job.source_metadata,
                "block_types": list(dict.fromkeys(block.kind for block in blocks)),
            })
    for chunk in chunks:
        chunk["total_chunks"] = len(chunks)
    return chunks
