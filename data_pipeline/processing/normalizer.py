"""Clean extracted law text into (page, line) pairs: Unicode NFC, Công báo headers, page numbers, e-signature stamps."""
import re
import unicodedata

NOISE = [
    re.compile(r"^\s*-?\s*\d{1,4}\s*-?\s*$"),                          # bare page number
    re.compile(r"^\s*CÔNG BÁO\b.*$", re.I),                             # Công báo running header
    re.compile(r"^\s*(Ký bởi|Cơ quan|Thời gian ký|Email|Signed by)\s*:.*$", re.I),  # e-signature stamp
]
SPACES = re.compile(r"[ \t  -​ 　]+")


def clean_pages(pages):
    lines = []
    for page in pages:
        for raw in unicodedata.normalize("NFC", page["text"]).replace("﻿", "").splitlines():
            line = SPACES.sub(" ", raw).strip()
            if line and not any(p.match(line) for p in NOISE):
                lines.append((page["page"], line))
    return lines
