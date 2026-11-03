from agents.shared.tools import ToolHandler, invoke_tool


def compact_context(context: dict, *, compactor: ToolHandler | None = None) -> dict:
    """TODO: Supply token budget and compaction policy; never silently discard context."""
    return invoke_tool("agent_1_classification.compact_context", context, compactor)
