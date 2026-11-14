"""Reject broken extractions before they reach the vector store; failures go to needs_review, not to agents."""
import re

ALLOWED = re.compile(r"[\wÀ-ỹ\s.,;:!?()\[\]“”\"'/%&+\-–—…§]", re.U)


def validate(document, preamble, articles):
    errors = []
    if not articles:
        errors.append("no_articles")
    numbers = [int(re.match(r"\d+", a["article_no"])[0]) for a in articles]
    plain = sorted({n for n, a in zip(numbers, articles) if a["article_no"].isdigit()})
    if plain and plain != list(range(plain[0], plain[-1] + 1)):
        errors.append(f"article_gap:{sorted(set(range(plain[0], plain[-1] + 1)) - set(plain))[:10]}")
    if plain and plain[0] != 1:
        errors.append(f"first_article:{plain[0]}")
    head = re.sub(r"\s+", "", preamble + "".join(a["text"] for a in articles[:2]))
    symbol = re.sub(r"\s+", "", document.get("symbol") or "")
    if document.get("doc_class") != "constitution" and symbol and symbol not in head:
        errors.append("symbol_not_in_text")
    body = preamble + "".join(a["text"] for a in articles)
    if body and sum(1 for c in body if ALLOWED.match(c)) / len(body) < 0.97:
        errors.append("garbled_text")
    issued, effective = document.get("issued_on"), document.get("effective_on")
    if issued and effective and effective < issued:
        errors.append("effective_before_issued")
    return errors
