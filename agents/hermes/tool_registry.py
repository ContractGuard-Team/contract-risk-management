from agents.shared.tools import ToolHandler


class ToolRegistry:
    """Explicit handler bindings. No automatic model tool calls in this base implementation."""

    def __init__(self):
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, name: str, handler: ToolHandler):
        if not isinstance(name, str) or not name.strip() or not callable(handler):
            raise ValueError("A tool needs a non-empty name and a callable handler")
        if name in self._handlers:
            raise ValueError(f"Tool already registered: {name}")
        self._handlers[name] = handler

    def get(self, name: str) -> ToolHandler | None:
        return self._handlers.get(name)
