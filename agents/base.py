from copy import deepcopy
import json
from pathlib import Path

from agents.shared.context import build_context
from common.exceptions import ModelResponseError
from common.ports.model_provider import ModelProvider
from common.schemas.agent_messages import AgentRequest, AgentResult
from agents.hermes.config_loader import follow_up_prompt, load_prompts
from agents.hermes.context_budget import ContextBudgetExceeded
from agents.hermes.tool_registry import ToolRegistry
from agents.shared.output import model_errors, parse_json
from monitoring.observer import NullObserver
from monitoring.usage import model_usage

MAX_TOOL_ROUNDS = 6        # model -> tools -> model rounds per evaluation pass
MAX_REEVALUATIONS = 2      # extra passes when the answer still lists evidence gaps
MAX_FORMAT_RETRIES = 2     # re-asks when the answer does not match the agent's business model (schemas/output.py)
MAX_TOOL_RESULT_CHARS = 20_000
EVIDENCE_GAPS = "evidence_gaps"  # top-level output field: what still lacks a source (empty = enough evidence)
# The follow-up instructions themselves are prompt files (prompts/fix_format.md, prompts/reevaluate.md).


def evidence_gaps(text) -> list:
    """Gaps the model itself reported in its JSON answer; [] when not JSON or none listed."""
    data = parse_json(text)
    gaps = data.get(EVIDENCE_GAPS) if data is not None else None
    return [gap for gap in gaps if gap] if isinstance(gaps, list) else []


DROPPED_TOOL_RESULT = json.dumps({"error": "DroppedForBudget",
                                  "message": "removed to stay within the input budget; call the tool again if needed"})


def _shorten(value, cap):
    if isinstance(value, str):
        return value if len(value) <= cap else value[:cap] + "...[truncated]"
    if isinstance(value, dict):
        return {key: _shorten(item, cap) for key, item in value.items()}
    if isinstance(value, list):
        return [_shorten(item, cap) for item in value]
    return value


def fit_tool_result(result, limit=MAX_TOOL_RESULT_CHARS) -> str:
    """JSON text of a tool result within `limit`: long strings are shortened, so the JSON stays valid and small
    fields such as next_offset survive. Too many items to fit -> an error the model can act on."""
    text = json.dumps(result, ensure_ascii=False, default=str)
    cap = limit
    while len(text) > limit and cap > 50:
        cap //= 2
        text = json.dumps(_shorten(result, cap), ensure_ascii=False, default=str)
    if len(text) > limit:
        return json.dumps({"error": "ResultTooLarge", "message": "narrow the query or page with offset/limit"})
    return text


def keep_in_budget(transcript, start, memory):
    """Tool results and follow-ups grow the transcript after memory.prepare(): drop the oldest tool results of this
    run first; still too big -> a clear error instead of a provider rejection."""
    if memory is None:
        return
    limit = memory.policy.max_input_tokens
    for message in transcript[start:]:
        if memory.counter(transcript) <= limit:
            return
        if message.get("role") == "tool":
            message["content"] = DROPPED_TOOL_RESULT
    if memory.counter(transcript) > limit:
        raise ContextBudgetExceeded("Tool results and follow-ups exceed this agent's input budget")


def add_usage(total: dict, usage: dict) -> dict:
    for key, value in (usage or {}).items():
        if isinstance(value, dict):
            total[key] = add_usage(dict(total.get(key) or {}), value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value
        else:
            total.setdefault(key, value)
    return total


def output_schema_text(model) -> str:
    """JSON Schema of the business output model, sent to the model as the binding output format."""
    return json.dumps(model.model_json_schema(), ensure_ascii=False, separators=(",", ":"))


class BaseAgent:
    agent_id = ""
    output_type = AgentResult
    output_model = None  # Pydantic business model of `text` (agents/<agent>/schemas/output.py); None = free text
    tool_functions = {}

    def __init__(self, provider: ModelProvider, *, directory=None, inference_config=None, tools=None, lessons=None):
        self.provider = provider
        self.directory = Path(directory) if directory is not None else Path(__file__).resolve().parent / self.agent_id
        self.inference_config = deepcopy(inference_config or {})
        self.tools = tools if tools is not None else ToolRegistry()
        self.lessons = lessons  # long-term memory (agents/memory/long_term.py LessonStore) or None

    def build_context(self, request: AgentRequest) -> dict:
        return build_context(request)

    def build_messages(self, request: AgentRequest) -> list[dict]:
        """System: soul, core prompt, external-services prompt, rule files, output schema, examples, approved
        long-term lessons; user: this request. Short-term memory (earlier turns of this contract) is inserted
        between them by the ContextEngine."""
        request.validate()
        prompts = load_prompts(self.directory)
        system = [prompts["soul"], prompts["core"], prompts["external"]]
        system += [f"# Rules: {name}\n{text}" for name, text in prompts["rules"]]
        if self.output_model is not None:
            system.append("# Output schema (JSON Schema, bắt buộc tuân thủ)\n" + output_schema_text(self.output_model))
        if prompts["examples"]:
            system.append("\n\n".join(["# Examples", *(f"## Example {i}\n{text}"
                                                       for i, text in enumerate(prompts["examples"], 1))]))
        # shortcut: lessons approved while a contract is running apply from its next model call; freeze per job if that matters.
        lessons = self.lessons.prompt_section(self.agent_id) if self.lessons is not None else ""
        if lessons:
            system.append(lessons)
        return [
            *({"role": "system", "content": text} for text in system),
            {"role": "user", "content": json.dumps(self.build_context(request),
                                                  ensure_ascii=False, allow_nan=False)},
        ]

    def output_problems(self, text, request: AgentRequest) -> list[str]:
        """Business-model errors of the answer, then this agent's own cross-checks; [] when valid or no model is set."""
        if self.output_model is None:
            return []
        data = parse_json(text)
        if data is None:
            return ["output is not one JSON object"]
        errors = model_errors(self.output_model, data)
        return errors or self.check_output(self.output_model.model_validate(data).model_dump(mode="json"), request)

    def check_output(self, data: dict, request: AgentRequest) -> list[str]:
        """Agent-specific rules JSON Schema cannot express (ids that must match the request, completeness)."""
        return []

    def reflect(self, contract: dict, output) -> dict:
        """Long-term memory: propose lessons (pending review) from this agent's finished run."""
        from agents.memory.long_term import reflect
        return reflect(self, contract, output)

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

    def _tool_message(self, call, observer) -> str:
        """Run one model tool call; errors go back to the model as data so it can search elsewhere, never invent."""
        try:
            arguments = json.loads(call.get("arguments") or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
            result = self.run_tool(call["name"], arguments, observer=observer)
        except Exception as exc:  # unknown tool, bad arguments, connector not configured, upstream failure
            result = {"error": type(exc).__name__, "message": str(exc)[:500]}
        return fit_tool_result(result)

    def _generate(self, messages, tools, *, final):
        kwargs = {"inference_config": deepcopy(self.inference_config)}
        if tools:  # providers without tools (Agent 1, tests) keep the plain call signature
            kwargs["tools"] = tools
            if final:
                kwargs["tool_choice"] = "none"  # tool budget spent: answer with what was found
        return self.provider.generate(messages, **kwargs)

    def run(self, request: AgentRequest, *, memory=None, observer=None, trace_metadata=None) -> AgentResult:
        current = self.build_messages(request)
        messages = memory.prepare(current) if memory is not None else current
        observer = observer if observer is not None else NullObserver()
        settings = getattr(self.provider, "settings", None)
        tools = self.tools.specs()
        with observer.span(self.agent_id, kind="generation", metadata=trace_metadata,
                           model=getattr(settings, "model", None)) as observation:
            observation.summary(input={"message_count": len(messages), "tool_count": len(tools),
                                       "content_characters": sum(len(m.get("content") or "") for m in messages)})
            observation.update(input=messages, model_parameters=deepcopy(self.inference_config))
            transcript, usage = list(messages), {}
            rounds = reevaluations = tool_calls = fixes = 0
            while True:
                keep_in_budget(transcript, len(messages), memory)
                response = self._generate(transcript, tools, final=rounds >= MAX_TOOL_ROUNDS)
                add_usage(usage, response.usage)  # billed even when the response is rejected below
                if response.tool_calls:
                    if not tools or rounds >= MAX_TOOL_ROUNDS:
                        raise ModelResponseError("Model requested tools after the tool budget was spent")
                    rounds += 1
                    tool_calls += len(response.tool_calls)
                    transcript.append({"role": "assistant", "content": response.text or None, "tool_calls": [
                        {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]},
                         # Gemini 3 rejects the next round if a function call comes back without its signature
                         **({"thought_signature": c["thought_signature"]} if c.get("thought_signature") else {})}
                        for c in response.tool_calls]})
                    transcript += [{"role": "tool", "tool_call_id": c["id"], "content": self._tool_message(c, observer)}
                                   for c in response.tool_calls]
                    continue
                if not isinstance(response.text, str) or not response.text.strip():
                    raise ModelResponseError("Model returned no text")
                if response.stop_reason != "stop":
                    raise ModelResponseError(f"Model did not complete: {response.stop_reason}")
                problems = self.output_problems(response.text, request)
                if problems:
                    if fixes >= MAX_FORMAT_RETRIES:
                        raise ModelResponseError(f"{self.agent_id} output does not match its schema: {problems[:3]}")
                    fixes += 1
                    transcript += [{"role": "assistant", "content": response.text},
                                   {"role": "user", "content": follow_up_prompt(
                                       self.directory, "fix_format", problems="; ".join(problems)[:3000])}]
                    continue
                gaps = evidence_gaps(response.text)
                if gaps and tools and reevaluations < MAX_REEVALUATIONS:
                    # Evidence missing: search again (fresh tool budget) and re-evaluate the whole answer.
                    reevaluations, rounds = reevaluations + 1, 0
                    transcript += [{"role": "assistant", "content": response.text},
                                   {"role": "user", "content": follow_up_prompt(
                                       self.directory, "reevaluate", gaps=json.dumps(gaps, ensure_ascii=False)[:2000])}]
                    continue
                break
            observation.summary(output={"text_characters": len(response.text), "stop_reason": response.stop_reason,
                                        "tool_calls": tool_calls, "reevaluations": reevaluations, "format_fixes": fixes,
                                        "evidence_gaps": len(evidence_gaps(response.text))})
            observation.update(output=response.text, usage_details=model_usage(usage))
        if memory is not None:
            memory.record(current, response)  # final answer only; tool transcripts are not kept in memory
        return self.output_type(self.agent_id, response.text, usage, response.stop_reason)
