from agents.shared.tools import ToolHandler, invoke_tool


def legal_reference(payload: dict, *, handler: ToolHandler | None = None) -> dict:
    """Legal basis for a recommendation: served by MCP legal_search (rag/); same payload/result as Agent 2's
    legal_search. Handler bound at runtime (MCP_ALIASES)."""
    return invoke_tool("agent_3_recommendation.legal_reference", payload, handler)
