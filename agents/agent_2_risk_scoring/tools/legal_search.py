from agents.shared.tools import ToolHandler, invoke_tool


def legal_search(payload: dict, *, handler: ToolHandler | None = None) -> dict:
    """Legal RAG (rag/ via MCP legal_search): {query, limit, symbol, article_no, doc_class, as_of, include_inactive}
    -> {evidence: [{evidence_id, citation, text, validity, source, warnings}], found}. Handler bound at runtime."""
    return invoke_tool("agent_2_risk_scoring.legal_search", payload, handler)
