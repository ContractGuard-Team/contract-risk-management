"""Supabase article row -> Qdrant point (1 point per Điều, or per khoản-group of a long Điều)."""
import uuid

NAMESPACE = uuid.UUID("6f1c3a1e-4b9a-5d2e-9c41-0b7d2f6e8a10")


def point_id(symbol, article_no, valid_from, part):
    return str(uuid.uuid5(NAMESPACE, f"{symbol}|{article_no}|{valid_from}|{part}"))


def payload(document, article, part, text, *, amended_by=None):
    return {
        "document_id": document["id"], "symbol": document["symbol"], "doc_class": document["doc_class"],
        "title": document["title"], "status": document["status"], "effective_on": document.get("effective_on"),
        "article_no": article["article_no"], "heading": article.get("heading"), "chapter": article.get("chapter"),
        "part": part, "text": text, "valid_from": article.get("valid_from"), "valid_to": None, "replaced_on": None,
        "current": True,
        "amended_by": amended_by, "has_pending_amendment": bool(article.get("has_pending_amendment")),
        "version": article.get("version"), "page_from": article.get("page_from"),
        "legal_references": article.get("legal_references") or [],
        "s3_prefix": document.get("s3_prefix"), "source_url": document.get("source_url"),
    }
