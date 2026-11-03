from typing import Protocol

from common.schemas.agent_messages import ModelResult


class ModelProvider(Protocol):
    def generate(self, messages: list[dict], *, inference_config: dict | None = None) -> ModelResult:
        ...
