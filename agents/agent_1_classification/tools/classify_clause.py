from agents.shared.tools import ToolHandler, invoke_tool


def classify_clause(payload: dict, *, handler: ToolHandler | None = None) -> dict:
    """TODO: supply API connection or business rules and domain input/output schema."""
    return invoke_tool("agent_1_classification.classify_clause", payload, handler)
