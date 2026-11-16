from agents.shared.tools import ToolHandler


class ToolRegistry:
    """Explicit handler bindings. Tools with a spec are offered to the model (BaseAgent.run tool loop)."""

    def __init__(self):
        self._handlers: dict[str, ToolHandler] = {}
        self._specs: dict[str, dict] | None = None
        self.spec_loader = None  # () -> {name: OpenAI function spec}; set by bind_mcp_tools, read on first use

    def register(self, name: str, handler: ToolHandler):
        if not isinstance(name, str) or not name.strip() or not callable(handler):
            raise ValueError("A tool needs a non-empty name and a callable handler")
        if name in self._handlers:
            raise ValueError(f"Tool already registered: {name}")
        self._handlers[name] = handler

    def get(self, name: str) -> ToolHandler | None:
        return self._handlers.get(name)

    def specs(self) -> list[dict]:
        """OpenAI `tools` list for bound handlers; [] when nothing is bound (the model then answers directly)."""
        if not self._handlers or self.spec_loader is None:
            return []
        if self._specs is None:  # MCP may start after the agent: fetch once, on the first model call
            self._specs = self.spec_loader()
        return [self._specs[name] for name in self._handlers if name in self._specs]
