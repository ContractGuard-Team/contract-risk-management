from copy import deepcopy
from typing import Callable

from common.exceptions import IntegrationNotConfigured

ToolHandler = Callable[[dict], dict]

# Tools served by the MCP gateway (mcp_server/); only Agent 2/3 list them.
MCP_TOOLS = ("legal_search", "web_search", "qdrant_search", "postgres_query", "s3_get_object", "s3_list_objects", "qdrant_collection_info",
             "supabase_select", "supabase_list_tables")

# Agent tool name -> MCP tool when they differ (Agent 3 asks for "legal_reference", served by the RAG tool).
MCP_ALIASES = {"legal_reference": "legal_search"}


def external_tool(name: str):
    """Agent tool function whose handler is bound to the MCP gateway at runtime."""
    def call(payload: dict, *, handler: "ToolHandler | None" = None) -> dict:
        return invoke_tool(name, payload, handler)
    return call


def invoke_tool(name: str, payload: dict, handler: ToolHandler | None = None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError(f"{name} payload must be a dictionary")
    if handler is None:
        raise IntegrationNotConfigured(f"TODO: connect API/business rules for {name}")
    result = handler(deepcopy(payload))
    if not isinstance(result, dict):
        raise ValueError(f"{name} handler must return a dictionary")
    return result
