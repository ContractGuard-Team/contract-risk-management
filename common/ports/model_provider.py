from typing import Protocol

from common.schemas.agent_messages import ModelResult


class ModelProvider(Protocol):
    def generate(self, messages: list[dict], *, inference_config: dict | None = None) -> ModelResult:
        """Agents with tools also pass tools=[chat-style function specs] and tool_choice; then the result may carry
        tool_calls (stop_reason "tool_calls") instead of final text."""
        ...
