from copy import deepcopy
import json
from pathlib import Path

from agents.shared.context import build_context
from common.exceptions import ModelResponseError
from common.ports.model_provider import ModelProvider
from common.schemas.agent_messages import AgentRequest, AgentResult
from hermes_integration.config_loader import load_instructions
from hermes_integration.tool_registry import ToolRegistry


class BaseAgent:
    agent_id = ""
    task_prompt = ""
    output_type = AgentResult
    tool_functions = {}

    def __init__(self, provider: ModelProvider, *, directory=None, inference_config=None, tools=None):
        self.provider = provider
        self.directory = Path(directory) if directory is not None else Path(__file__).resolve().parent / self.agent_id
        self.inference_config = deepcopy(inference_config or {})
        self.tools = tools if tools is not None else ToolRegistry()

    def build_context(self, request: AgentRequest) -> dict:
        return build_context(request)

    def build_messages(self, request: AgentRequest) -> list[dict]:
        request.validate()
        instructions = load_instructions(self.directory, self.task_prompt)
        return [
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(self.build_context(request),
                                                  ensure_ascii=False, allow_nan=False)},
        ]

    def run_tool(self, name: str, payload: dict) -> dict:
        if name not in self.tool_functions:
            raise ValueError(f"Unknown tool for {self.agent_id}: {name}")
        return self.tool_functions[name](payload, handler=self.tools.get(name))

    def run(self, request: AgentRequest) -> AgentResult:
        response = self.provider.generate(self.build_messages(request),
                                          inference_config=deepcopy(self.inference_config))
        if not isinstance(response.text, str) or not response.text.strip():
            raise ModelResponseError("Model returned no text")
        if response.stop_reason not in ("end_turn", "stop_sequence"):
            raise ModelResponseError(f"Model did not complete: {response.stop_reason}")
        # TODO: Parse/validate domain JSON here once the output schema is specified.
        return self.output_type(self.agent_id, response.text, deepcopy(response.usage), response.stop_reason)
