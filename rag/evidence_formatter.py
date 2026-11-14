"""Retrieved article -> evidence item for Agent 2/3: full Điều text, citation, validity, provenance and warnings.

Agents must cite `citation` + `evidence_id` and read `warnings` before relying on an item.
"""
MAX_TEXT = 6000  # characters per article; longer Điều are cut and flagged


def citation(p):
    name = p.get("title") or ""
    if p.get("doc_class") != "constitution" and p.get("symbol") and p["symbol"] not in name:
        name = f"{name} số {p['symbol']}"
    return f"Điều {p.get('article_no')} {name}".strip()


def warnings(p, truncated):
    notes = []
    if p.get("status") != "in_force":
        notes.append(f"Văn bản hiện ở trạng thái '{p.get('status')}', không phải đang có hiệu lực.")
    if p.get("current") is False:
        notes.append(f"Bản lịch sử: hết áp dụng từ {p.get('valid_to') or 'không rõ'}.")
    if p.get("has_pending_amendment"):
        notes.append("Điều này có sửa đổi chưa được áp dụng tự động (cần kiểm tra luật sửa đổi).")
    if p.get("amended_by"):
        notes.append(f"Nội dung đã được sửa đổi bởi {p['amended_by']}.")
    if truncated:
        notes.append(f"Nội dung bị cắt ở {MAX_TEXT} ký tự.")
    return notes


def evidence_item(article, retrieved_at):
    p, text = article["payload"], article["text"]
    truncated = len(text) > MAX_TEXT
    return {
        "evidence_id": f"{p.get('symbol')}#{p.get('article_no')}@{p.get('valid_from')}",
        "citation": citation(p), "symbol": p.get("symbol"), "title": p.get("title"), "doc_class": p.get("doc_class"),
        "status": p.get("status"), "article_no": p.get("article_no"), "heading": p.get("heading"),
        "chapter": p.get("chapter"), "text": text[:MAX_TEXT], "truncated": truncated,
        "valid_from": p.get("valid_from"), "valid_to": p.get("valid_to"), "effective_on": p.get("effective_on"),
        "replaced_on": p.get("replaced_on"), "current": p.get("current"), "version": p.get("version"),
        "amended_by": p.get("amended_by"), "has_pending_amendment": bool(p.get("has_pending_amendment")),
        "legal_references": p.get("legal_references") or [], "page_from": p.get("page_from"),
        "source_url": p.get("source_url"), "s3_prefix": p.get("s3_prefix"),
        "score": article.get("score"), "fusion_rank": article.get("rank"),
        "warnings": warnings(p, truncated), "retrieved_at": retrieved_at,
    }
