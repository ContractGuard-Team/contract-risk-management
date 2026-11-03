from copy import deepcopy
from typing import Callable

from common.exceptions import IntegrationNotConfigured

ToolHandler = Callable[[dict], dict]


def invoke_tool(name: str, payload: dict, handler: ToolHandler | None = None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError(f"{name} payload must be a dictionary")
    if handler is None:
        raise IntegrationNotConfigured(f"TODO: connect API/business rules for {name}")
    result = handler(deepcopy(payload))
    if not isinstance(result, dict):
        raise ValueError(f"{name} handler must return a dictionary")
    return result
