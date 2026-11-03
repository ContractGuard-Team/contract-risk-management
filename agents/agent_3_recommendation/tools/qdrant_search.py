from agents.shared.tools import ToolHandler, invoke_tool


def qdrant_search(payload: dict, *, handler: ToolHandler | None = None) -> dict:
    """TODO: supply API connection or business rules and domain input/output schema."""
    return invoke_tool("agent_3_recommendation.qdrant_search", payload, handler)
