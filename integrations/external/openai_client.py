"""OpenAI model provider of the three agents, on the Responses API (official `openai` SDK).

GPT-6.1 Sol only does tool calling on the Responses API, so every agent uses it. Agents keep their chat-format
messages ({role, content}, assistant tool_calls, role "tool" results); this client converts them to Responses input
items and returns usage under the Chat Completions names (prompt_tokens, completion_tokens, *_details) that
memory, monitoring and the cache already read. The SDK is created on the first call, so agents start without a key;
a missing key/model fails before any request.
"""
from copy import deepcopy

from common.exceptions import ConfigurationError, ModelResponseError, TransientModelError
from common.schemas.agent_messages import ModelResult
from common.settings import OpenAISettings

# Agent config.yaml inference_config keys. max_completion_tokens is sent as max_output_tokens; reasoning_effort as
# reasoning.effort. `stop` is kept as a key only to fail clearly: the Responses API has no stop sequences.
INFERENCE_KEYS = ("max_completion_tokens", "temperature", "top_p", "stop", "reasoning_effort")
REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")  # gpt-6.1-sol has no "none"


def transient(exc):
    """Rate limit/outage/network after the SDK's own retries. Auth, bad request and exhausted quota stay permanent."""
    try:
        import openai
    except ImportError:
        return False
    if isinstance(exc, openai.RateLimitError):
        return getattr(exc, "code", None) != "insufficient_quota"
    return isinstance(exc, (openai.APIConnectionError, openai.InternalServerError))  # includes APITimeoutError


def to_input(messages):
    """Chat-format messages -> Responses input items (messages, function_call, function_call_output)."""
    items = []
    for message in messages:
        if message.get("role") == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": message["content"]})
            continue
        if message.get("content"):
            items.append({"role": message["role"], "content": message["content"]})
        for call in message.get("tool_calls") or []:
            items.append({"type": "function_call", "call_id": call["id"], "name": call["function"]["name"],
                          "arguments": call["function"]["arguments"]})
    return items


def to_request(inference_config, tools, tool_choice):
    config = deepcopy(inference_config or {})
    if config.get("stop") is not None:
        raise ConfigurationError("inference_config.stop is not supported by the OpenAI Responses API")
    config.pop("stop", None)
    request = {key: value for key, value in config.items() if key in ("temperature", "top_p")}
    if config.get("max_completion_tokens") is not None:
        request["max_output_tokens"] = config["max_completion_tokens"]
    if config.get("reasoning_effort") is not None:
        request["reasoning"] = {"effort": config["reasoning_effort"]}
    if tools:  # chat-style {"type":"function","function":{...}} -> Responses flat function tool
        request["tools"] = [{"type": "function", "name": spec["function"]["name"],
                             "description": spec["function"].get("description", ""),
                             "parameters": spec["function"].get("parameters") or {"type": "object", "properties": {}}}
                            for spec in tools]
        if tool_choice:
            request["tool_choice"] = tool_choice
    return request


def chat_usage(usage):
    """Responses usage -> the Chat Completions names the rest of the project reads."""
    usage = usage or {}
    out = {"prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens"),
           "total_tokens": usage.get("total_tokens"),
           "prompt_tokens_details": {"cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens")},
           "completion_tokens_details": {"reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens")}}
    out = {key: ({k: v for k, v in value.items() if v is not None} if isinstance(value, dict) else value)
           for key, value in out.items()}
    return {key: value for key, value in out.items() if value not in (None, {})}


class OpenAIChatClient:
    def __init__(self, settings: OpenAISettings, client=None):
        self.settings = settings
        self._client = client

    def generate(self, messages, *, inference_config=None, tools=None, tool_choice=None):
        self.settings.validate()
        request = to_request(inference_config, tools, tool_choice)
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise ConfigurationError("Install project dependencies to use OpenAI (openai)") from exc
            # 5 retries: the SDK waits as a 429's Retry-After says, so a per-minute limit can pass (2 failed in ~2 s).
            self._client = OpenAI(api_key=self.settings.api_key, timeout=120, max_retries=5)
        try:
            # store=False: nothing kept server-side; the transcript is resent each round (stateless agents).
            # shortcut: reasoning items are not passed back between tool rounds; send them if quality needs it.
            response = self._client.responses.create(model=self.settings.model, input=to_input(messages),
                                                     store=False, **request)
        except Exception as exc:  # SDK errors propagate; never fabricate a successful result
            if transient(exc):
                # Status and code only (e.g. 429 rate_limit_exceeded), never the request: enough to tell TPM from outage.
                raise TransientModelError(f"OpenAI temporarily unavailable: {type(exc).__name__} "
                                          f"status={getattr(exc, 'status_code', None)} code={getattr(exc, 'code', None)}") from exc
            raise
        usage = chat_usage(response.usage.model_dump(exclude_none=True) if response.usage is not None else {})
        if response.status != "completed":
            reason = getattr(getattr(response, "incomplete_details", None), "reason", None) or response.status
            raise ModelResponseError(f"OpenAI did not complete a response: {reason}")
        output = response.output or []
        text = "".join(part.text for item in output if item.type == "message"
                       for part in item.content if getattr(part, "type", "") == "output_text")
        calls = [{"id": item.call_id, "name": item.name, "arguments": item.arguments or "{}"}
                 for item in output if item.type == "function_call"]
        if calls:
            if not tools:
                raise ModelResponseError("OpenAI asked for a tool that was not offered")
            return ModelResult(text, usage, "tool_calls", calls)
        if not text.strip():
            raise ModelResponseError("OpenAI returned no text")
        return ModelResult(text, usage, "stop")
