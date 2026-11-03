from agents.shared.tools import ToolHandler, invoke_tool


def legal_search(payload: dict, *, handler: ToolHandler | None = None) -> dict:
    """TODO: supply API connection or business rules and domain input/output schema."""
    return invoke_tool("agent_2_risk_scoring.legal_search", payload, handler)
