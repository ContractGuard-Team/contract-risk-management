"""MCP client for Agent 2/3: calls one tool on the MCP gateway over streamable HTTP."""
import json
import os

import anyio
import httpx


class McpToolError(RuntimeError):
    pass


class McpToolClient:
    def __init__(self, url=None, token=None, *, timeout=60):
        self.url = url or os.environ["MCP_URL"]
        self.token = token or os.environ["MCP_API_TOKEN"]
        self.timeout = timeout

    async def _call(self, name, arguments):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        headers = {"Authorization": "Bearer " + self.token}
        async with httpx.AsyncClient(headers=headers, timeout=self.timeout, follow_redirects=False) as http, \
                streamable_http_client(self.url, http_client=http) as (read, write, _), \
                ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
        text = " ".join(getattr(item, "text", "") for item in result.content)
        if result.isError:
            raise McpToolError(f"{name}: {text[:500]}")
        return result.structuredContent if result.structuredContent is not None else json.loads(text)

    async def _specs(self, names):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        headers = {"Authorization": "Bearer " + self.token}
        async with httpx.AsyncClient(headers=headers, timeout=self.timeout, follow_redirects=False) as http, \
                streamable_http_client(self.url, http_client=http) as (read, write, _), \
                ClientSession(read, write) as session:
            await session.initialize()
            served = {tool.name: tool for tool in (await session.list_tools()).tools}
        return {name: {"type": "function", "function": {"name": name, "description": served[mcp_name].description or "",
                                                         "parameters": served[mcp_name].inputSchema}}
                for name, mcp_name in names.items() if mcp_name in served}

    def tool_specs(self, names):
        """{agent tool name: OpenAI function spec} from the gateway's own tool list (name -> MCP tool name)."""
        return anyio.run(self._specs, dict(names))

    def call(self, name, arguments):
        return anyio.run(self._call, name, dict(arguments))

    def handler(self, name):
        return lambda payload: self.call(name, payload)


def bind_mcp_tools(agent, client=None):
    """Register MCP-backed handlers for the agent's external tools. No-op when MCP_URL is unset."""
    from agents.shared.tools import MCP_ALIASES, MCP_TOOLS
    if client is None:
        if not os.getenv("MCP_URL"):
            return []
        client = McpToolClient()
    names = [name for name in (*MCP_TOOLS, *MCP_ALIASES) if name in agent.tool_functions]
    for name in names:
        agent.tools.register(name, client.handler(MCP_ALIASES.get(name, name)))
    if names:
        agent.tools.spec_loader = lambda: client.tool_specs({name: MCP_ALIASES.get(name, name) for name in names})
    return names
