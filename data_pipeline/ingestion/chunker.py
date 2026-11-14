"""Split a Vietnamese law into Điều (articles); long articles are cut by khoản for embedding.

Built for OCR output (2/3 of source PDFs are scans): heading spelling varies ("Điêu 14.", "Điểu"), quotes are often
unbalanced, cross-references wrap to the start of a line. An "Điều N" line opens a new article only when N moves
forward in sequence; quotes are tracked only in amending laws, where whole new articles are quoted.
"""
import re
import unicodedata

ARTICLE = re.compile(r"^[ĐđD][iìíỉĩịI][êềếểễệeèéẻẽẹÊỀẾỂỄỆ]?[uưU]\s+(\d{1,3}[a-zđ]?)\s*([.:])?\s*(.*)$", re.I)
CHAPTER = re.compile(r"^(Chương|CHƯƠNG|Phần|PHẦN)\s+([IVXLCDM]+|\d+)\b\.?\s*(.*)$")
CLAUSE = re.compile(r"^(\d+[a-zđ]?)\.\s")
INSTRUCTION = re.compile(r"^(\d+|[Il])\.\s+(sua doi|bo sung|bai bo|thay the|thay|bo)\b")
OPEN, CLOSE = "“", "”"
MAX_GAP = 3  # a lost OCR heading should not swallow the rest of the law


def ascii_fold(text):
    """Lowercase, no diacritics, đ->d: keyword matching that survives OCR accent errors."""
    text = unicodedata.normalize("NFD", text.replace("đ", "d").replace("Đ", "D"))
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).lower().split())


def _number(article_no):
    return int(re.match(r"\d+", article_no)[0])


def split_articles(lines, *, amendment=False):
    """lines: [(page, text)] -> (preamble, [article])."""
    articles, preamble, chapter, current, depth = [], [], None, None, 0
    for page, line in lines:
        if amendment and INSTRUCTION.match(ascii_fold(line)):
            depth = 0  # a new instruction never starts inside a quote: recover from a lost closing quote
        free = depth == 0 and not line.startswith((OPEN, '"'))
        heading = line.lstrip("_.,;:|'`~•·-–— ")  # OCR specks before "Điều 21."; the stored line is untouched
        match = ARTICLE.match(heading) if free else None
        if match:
            number, last = _number(match[1]), _number(current["article_no"]) if current else 0
            forward = last < number <= last + MAX_GAP or (number == last and match[1][-1].isalpha())
            if not (forward and (match[2] or number == last + 1)):
                match = None  # cross-reference such as "Điều 5 của Luật này" wrapped to the line start
        chapter_match = CHAPTER.match(heading) if free and not match else None
        if chapter_match:
            chapter = " ".join(chapter_match.group(0).split())
        elif match:
            current = {"article_no": match[1].lower(), "heading": match[3].strip(), "chapter": chapter,
                       "page_from": page, "position": len(articles) + 1, "lines": []}
            articles.append(current)
        elif current is not None:
            current["lines"].append(line)
        else:
            preamble.append(line)
        if amendment:
            depth = max(0, depth + line.count(OPEN) - line.count(CLOSE))
    for article in articles:
        article["text"] = "\n".join([f"Điều {article['article_no']}. {article['heading']}".strip(), *article.pop("lines")])
    return "\n".join(preamble), articles


def clauses(article_text):
    """Split an article body into its numbered khoản: (head, [(clause_no, text)]). Quoted text is not split."""
    head, parts, depth = [], [], 0
    for line in article_text.splitlines():
        match = CLAUSE.match(line) if depth == 0 else None
        if match:
            parts.append([match[1], [line]])
        elif parts:
            parts[-1][1].append(line)
        else:
            head.append(line)
        depth = max(0, depth + line.count(OPEN) - line.count(CLOSE))
    return "\n".join(head), [(number, "\n".join(body)) for number, body in parts]


def embedding_parts(article, max_chars=1500):  # multilingual-e5 reads 512 tokens; Vietnamese law text ~3.6 chars/token
    """Article text cut on khoản boundaries; every part repeats the article title for context."""
    if len(article["text"]) <= max_chars:
        return [article["text"]]
    head, items = clauses(article["text"])
    parts, buffer = [], head
    for _, text in items:
        if buffer != head and len(buffer) + len(text) + 1 > max_chars:
            parts.append(buffer)
            buffer = head
        buffer = f"{buffer}\n{text}"
    parts.append(buffer)
    # shortcut: a single khoản longer than max_chars is cut hard; add sentence splitting if OCR output shows this.
    return [p[i:i + max_chars] for p in parts for i in range(0, len(p), max_chars)]
