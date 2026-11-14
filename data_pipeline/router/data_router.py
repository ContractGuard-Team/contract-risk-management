"""Data rules: a new version replaces the old one; amendments are applied article by article when unambiguous.

Amending laws are lists of instructions ("1. Sửa đổi, bổ sung khoản 3 Điều 4 như sau: “3. …”"), sometimes with
a) b) sub-items under one article. Only instructions whose target and new wording are certain are applied
automatically; point-level edits, phrase substitutions, annex replacements and multi-target instructions become
needs_review, so a base law is never rewritten on a guess. Keywords are matched on accent-free text because OCR
turns "Sửa đổi, bổ sung" into "Sửa đối, bỗ sung".
"""
import re

from data_pipeline.ingestion.chunker import ARTICLE, ascii_fold, clauses

TARGET_LAW = re.compile(r"cua\s+((?:bo\s+)?luat\s+[^,;:.]+?)(?:\s+so\s+(\d+\s*/\s*\d{4}\s*/\s*qh\s*\d+))?(?:\s+da duoc|\s*[,;:.]|$)")
ITEM = re.compile(r"^(\d+|[Il])\.\s+(.*)$")
SUB_ITEM = re.compile(r"^([a-zđ])\)\s+(.*)$")
REPLACED = re.compile(r"((?:bo\s+)?luat\s+[^,;.]{2,120}?)\s+so\s+(\d+/\d{4}/qh\d+)[^.;]{0,160}?het hieu luc")
OPEN, CLOSE = "“", "”"


def _ocr_numbers(text):
    """'khoản I' / 'Điều l' are OCR for the digit 1."""
    return re.sub(r"\b(khoan|dieu|diem)\s+[il]\b", r"\1 1", text)


def _symbol(text):
    return re.sub(r"\s+", "", text.replace("⁄", "/")).upper() if text else None


def _symbol_near(title, text):
    """'… Luật Đầu tư số 143/2025/QH15' in the preamble when the article heading names the law without a number."""
    match = re.search(re.escape(title) + r"\s+so\s+(\d+\s*[/⁄]+\s*\d{4}\s*[/⁄]+\s*qh\s*\d+)", ascii_fold(text))
    return _symbol(re.sub(r"[/⁄]+", "/", match[1])) if match else None


def _quoted(text):
    """Text inside the outermost “…” pairs (nested quotes stay inside)."""
    out, depth, buffer = [], 0, []
    for char in text:
        if char == OPEN:
            depth += 1
            if depth == 1:
                buffer = []
                continue
        elif char == CLOSE and depth:
            depth -= 1
            if depth == 0:
                out.append("".join(buffer).strip())
                continue
        if depth:
            buffer.append(char)
    if depth and buffer:  # OCR lost the closing quote: keep what follows the opening one
        out.append("".join(buffer).strip())
    return out


def _split(lines, pattern):
    """Group lines into items that start with `pattern` outside quotes."""
    items, depth = [], 0
    for line in lines:
        match = pattern.match(line) if depth == 0 else None
        if match:
            items.append([line])
        elif items:
            items[-1].append(line)
        depth = max(0, depth + line.count(OPEN) - line.count(CLOSE))
    return items


def parse_instruction(item, parent_article=None):
    """One instruction -> {operation, article, clause, after_article, content, auto}."""
    first = item.split("\n", 1)[0]
    head = re.sub(r"^(\d+|[Il]|[a-zđ])[.)]\s+", "", first.split(OPEN, 1)[0])
    text = _ocr_numbers(ascii_fold(head))
    quotes = _quoted(item)
    articles = re.findall(r"dieu\s+(\d+[a-z]?)", text) or ([parent_article] if parent_article else [])
    clause_nums = re.findall(r"khoan\s+(\d+[a-z]?)", text)
    result = {"instruction": first.strip()[:1000], "content": "\n".join(quotes), "operation": "other",
              "article": None, "clause": None, "after_article": None, "auto": False}
    if "cum tu" in text:
        return {**result, "operation": "rephrase"}
    point_level = bool(re.search(r"\bdiem\b", text))
    if text.startswith("bai bo"):
        operation = "repeal"
    elif text.startswith("bo sung") and ("vao sau" in text or "vao dieu" in text or "vao cuoi" in text):
        operation = "insert"
    elif text.startswith(("sua doi", "bo sung")) and "nhu sau" in text:
        operation = "replace"
    else:
        return result
    if operation == "insert":
        # "Bổ sung Điều 5a vào sau Điều 5" | "Bổ sung khoản 2a vào sau khoản 2 [Điều 6]"
        new_article = articles[0] if len(articles) == 2 else None
        article = articles[-1] if articles else None
        clause = clause_nums[0] if clause_nums and not new_article else None
        single = article and (new_article or clause) and not point_level and quotes
        return {**result, "operation": "insert", "article": new_article or article, "clause": clause,
                "after_article": article if new_article else None, "auto": bool(single)}
    single = len(set(articles)) == 1 and len(set(clause_nums)) <= 1 and not point_level
    return {**result, "operation": operation, "article": articles[0] if articles else None,
            "clause": clause_nums[0] if clause_nums else None,
            "auto": bool(single and (quotes or operation == "repeal"))}


def _instructions(body_lines):
    """Numbered items; an item with a) b) sub-items yields one instruction per sub-item, inheriting its article."""
    for item in _split(body_lines, ITEM):
        subs = _split(item[1:], SUB_ITEM)
        if not subs:
            yield parse_instruction("\n".join(item))
            continue
        parent = _ocr_numbers(ascii_fold(item[0]))
        parent_article = (re.findall(r"dieu\s+(\d+[a-z]?)", parent) or [None])[0]
        for sub in subs:
            yield parse_instruction("\n".join(sub), parent_article)


def parse_amendment(articles, own_title="", preamble=""):
    """Instructions of an amending law grouped by target law: [{target_title, target_symbol, ...instruction}]."""
    ops = []
    for article in articles:
        lines = article["text"].split("\n")
        header = ascii_fold(lines[0])
        target = TARGET_LAW.search(header)
        if not any(k in header for k in ("sua doi", "bo sung", "bai bo")) or not target:
            continue
        for op in _instructions(lines[1:]):
            title = target[1].strip()
            symbol = _symbol(target[2]) or _symbol_near(title, preamble + " " + article["text"])
            ops.append({"target_title": title, "target_symbol": symbol, **op})
    if not ops:  # single-law amendments may put instructions straight into Điều 1 without "của Luật …"
        own = TARGET_LAW.search(ascii_fold(own_title))
        for article in articles[:1]:
            for op in _instructions(article["text"].split("\n")[1:]):
                title = own[1].strip() if own else None
                symbol = (_symbol(own[2]) or _symbol_near(title, preamble)) if own else None
                ops.append({"target_title": title, "target_symbol": symbol, **op})
    return ops


CITATION = re.compile(r"\b(?:hien phap|bo luat|luat)\b[^.;:\n]{0,120}?\bso\s+(\d+\s*[/⁄]+\s*\d{4}\s*[/⁄]+\s*qh\s*\d+)")


def legal_references(text, own_symbol=None):
    """Laws an article cites ("… Luật Đầu tư số 61/2020/QH14 …") as normalized symbols, without its own."""
    found = {_symbol(re.sub(r"[/⁄]+", "/", m)) for m in CITATION.findall(ascii_fold(text))}
    return sorted(found - {own_symbol})


def replaced_laws(text):
    """'Luật … số 61/2020/QH14 hết hiệu lực kể từ ngày Luật này có hiệu lực' -> [(ascii title, symbol)]."""
    return [(m[1].strip(), _symbol(m[2])) for m in REPLACED.finditer(ascii_fold(text))]


class Unresolvable(ValueError):
    pass


def _rebuild(head, items):
    return "\n".join([head, *(text for _, text in items)]).strip()


def _copy(article):
    return {k: article.get(k) for k in ("article_no", "heading", "chapter", "position", "page_from")}


def apply_instruction(current, op):
    """current: {article_no: article row}. Return (closed article rows, new article dicts) or raise Unresolvable."""
    article = current.get(op["article"]) if op["article"] else None
    content = op["content"]
    if op["operation"] == "insert":
        if op.get("after_article"):
            if op["article"] in current or op["after_article"] not in current:
                raise Unresolvable("insert target exists or anchor article missing")
            match = ARTICLE.match(content.split("\n", 1)[0])
            if not match or match[1].lower() != op["article"]:
                raise Unresolvable("inserted text does not start with the announced article")
            anchor = current[op["after_article"]]
            return [], [{"article_no": op["article"], "heading": match[3].strip(), "chapter": anchor.get("chapter"),
                         "position": anchor["position"], "page_from": None, "text": content}]
        if article is None:
            raise Unresolvable("article to extend not found")
        head, items = clauses(article["text"])
        if any(number == op["clause"] for number, _ in items):
            raise Unresolvable("clause already exists")
        if not content.startswith(f"{op['clause']}."):
            raise Unresolvable("new clause text does not start with the clause number")
        anchor = re.search(r"vao sau khoan\s+(\d+[a-z]?)", _ocr_numbers(ascii_fold(op["instruction"])))
        at = next((i + 1 for i, (n, _) in enumerate(items) if anchor and n == anchor[1]), len(items))
        return [article], [{**_copy(article), "text": _rebuild(head, items[:at] + [(op["clause"], content)] + items[at:])}]
    if article is None:
        raise Unresolvable("target article not found")
    if op["operation"] == "repeal":
        if not op["clause"]:
            return [article], []
        head, items = clauses(article["text"])
        kept = [(n, t) for n, t in items if n != op["clause"]]
        if len(kept) == len(items):
            raise Unresolvable("clause to repeal not found")
        return [article], [{**_copy(article), "text": _rebuild(head, kept)}]
    if not op["clause"]:  # whole article replaced
        match = ARTICLE.match(content.split("\n", 1)[0])
        if not match or match[1].lower() != op["article"]:
            raise Unresolvable("new article text does not start with the same article number")
        return [article], [{**_copy(article), "heading": match[3].strip(), "text": content}]
    head, items = clauses(article["text"])
    if not any(number == op["clause"] for number, _ in items):
        raise Unresolvable("clause to replace not found")
    if not content.startswith(f"{op['clause']}."):
        raise Unresolvable("new clause text does not start with the clause number")
    return [article], [{**_copy(article), "text": _rebuild(head, [(n, content if n == op["clause"] else t) for n, t in items])}]
