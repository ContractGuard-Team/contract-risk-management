from copy import deepcopy
import json
from pathlib import Path

from agents.shared.context import build_context
from common.exceptions import ModelResponseError
from common.ports.model_provider import ModelProvider
from common.schemas.agent_messages import AgentRequest, AgentResult
from hermes_integration.config_loader import load_instructions
from hermes_integration.tool_registry import ToolRegistry
from monitoring.langfuse import NullObserver
from monitoring.usage import bedrock_usage


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

    def run_tool(self, name: str, payload: dict, *, observer=None) -> dict:
        if name not in self.tool_functions:
            raise ValueError(f"Unknown tool for {self.agent_id}: {name}")
        observer = observer if observer is not None else NullObserver()
        with observer.span(self.agent_id + ".tool." + name, kind="tool") as observation:
            observation.summary(input={"field_count": len(payload)})
            observation.update(input=payload)
            result = self.tool_functions[name](payload, handler=self.tools.get(name))
            observation.summary(output={"status": "completed", "field_count": len(result)})
            observation.update(output=result)
            return result

    def run(self, request: AgentRequest, *, memory=None, observer=None, trace_metadata=None) -> AgentResult:
        current = self.build_messages(request)
        messages = memory.prepare(current) if memory is not None else current
        observer = observer if observer is not None else NullObserver()
        settings = getattr(self.provider, "settings", None)
        with observer.span(self.agent_id, kind="generation", metadata=trace_metadata,
                           model=getattr(settings, "model_id", None)) as observation:
            observation.summary(input={"message_count": len(messages),
                                       "content_characters": sum(len(m.get("content", "")) for m in messages)})
            observation.update(input=messages, model_parameters=deepcopy(self.inference_config))
            response = self.provider.generate(messages, inference_config=deepcopy(self.inference_config))
            # Record billed usage even when the response is later rejected (e.g. token limit).
            observation.summary(output={"text_characters": len(response.text) if isinstance(response.text, str) else 0,
                                        "stop_reason": response.stop_reason})
            observation.update(output=response.text, usage_details=bedrock_usage(response.usage))
            if not isinstance(response.text, str) or not response.text.strip():
                raise ModelResponseError("Model returned no text")
            if response.stop_reason not in ("end_turn", "stop_sequence"):
                raise ModelResponseError(f"Model did not complete: {response.stop_reason}")
        if memory is not None:
            memory.record(current, response)
        # TODO: Parse/validate domain JSON here once the output schema is specified.
        return self.output_type(self.agent_id, response.text, deepcopy(response.usage), response.stop_reason)
