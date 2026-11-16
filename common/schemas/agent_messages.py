from dataclasses import dataclass, field
import json
from typing import Any


@dataclass(frozen=True)
class AgentRequest:
    content: str
    context: dict[str, Any] = field(default_factory=dict)
    previous_results: dict[str, Any] = field(default_factory=dict)

    def validate(self):
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("content must be a non-empty string")
        for name in ("context", "previous_results"):
            value = getattr(self, name)
            if not isinstance(value, dict):
                raise ValueError(f"{name} must be a dictionary")
            try:
                json.dumps(value, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must contain JSON-compatible data") from exc


@dataclass(frozen=True)
class ModelResult:
    text: str
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # [{id, name, arguments (JSON string)}]


@dataclass(frozen=True)
class AgentResult:
    agent_id: str
    text: str
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str = ""
    # TODO: Add domain output fields and validation once the business schema is defined.
