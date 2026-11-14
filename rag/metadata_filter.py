"""Agent search criteria -> Qdrant filter over the legal_documents payload (fields indexed by the data pipeline).

Default: current text of laws in force. as_of=YYYY-MM-DD: the text in force on that date (a contract's signing date),
including versions later amended or laws later replaced.
"""
from datetime import date
import re

DOC_CLASSES = ("constitution", "code", "law", "amendment")
SYMBOL = re.compile(r"[\w./-]{1,64}")
ARTICLE = re.compile(r"\w{1,10}")


def _match(m, key, value):
    return m.FieldCondition(key=key, match=m.MatchValue(value=value))


def build_filter(*, symbol="", article_no="", doc_class="", as_of="", include_inactive=False):
    from qdrant_client import models as m
    must, must_not = [], []
    if symbol:
        if not SYMBOL.fullmatch(symbol):
            raise ValueError("symbol must look like 61/2020/QH14 or HP-2013")
        must.append(_match(m, "symbol", symbol.upper()))
    if article_no:
        article_no = str(article_no).strip()
        if not ARTICLE.fullmatch(article_no):
            raise ValueError("article_no must look like 5 or 5a")
        must.append(_match(m, "article_no", article_no.lower()))
    if doc_class:
        classes = [c.strip() for c in doc_class.split(",") if c.strip()]
        if not classes or any(c not in DOC_CLASSES for c in classes):
            raise ValueError(f"doc_class must be one or more of {', '.join(DOC_CLASSES)}")
        must.append(m.FieldCondition(key="doc_class", match=m.MatchAny(any=classes)))
    if as_of:
        try:
            day = date.fromisoformat(as_of).isoformat()
        except ValueError:
            raise ValueError("as_of must be a date YYYY-MM-DD") from None
        # In force on `day`: started on/before it, not closed by an amendment or replaced on/before it.
        must.append(m.FieldCondition(key="valid_from", range=m.DatetimeRange(lte=day)))
        must_not += [m.FieldCondition(key="valid_to", range=m.DatetimeRange(lte=day)),
                     m.FieldCondition(key="replaced_on", range=m.DatetimeRange(lte=day))]
    else:
        must.append(_match(m, "current", True))
        if not include_inactive:
            must.append(_match(m, "status", "in_force"))
    return m.Filter(must=must, must_not=must_not or None)
