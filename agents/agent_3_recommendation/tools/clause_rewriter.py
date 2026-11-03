from agents.shared.tools import ToolHandler, invoke_tool


def clause_rewriter(payload: dict, *, handler: ToolHandler | None = None) -> dict:
    """TODO: supply API connection or business rules and domain input/output schema."""
    return invoke_tool("agent_3_recommendation.clause_rewriter", payload, handler)
