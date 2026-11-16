"""OpenAI Chat Completions client used as the model provider of the three agents (official `openai` SDK).

Project messages are already in OpenAI chat format ({role, content}); no conversion. The SDK is created on the
first call, so agents start without a key; a missing key/model fails before any request.
"""
from copy import deepcopy

from common.exceptions import ConfigurationError, ModelResponseError, TransientModelError
from common.schemas.agent_messages import ModelResult
from common.settings import OpenAISettings

# Agent config.yaml inference_config keys, passed through unchanged.
INFERENCE_KEYS = ("max_completion_tokens", "temperature", "top_p", "stop")


def transient(exc):
    """Rate limit/outage/network after the SDK's own retries. Auth, bad request and exhausted quota stay permanent."""
    try:
        import openai
    except ImportError:
        return False
    if isinstance(exc, openai.RateLimitError):
        return getattr(exc, "code", None) != "insufficient_quota"
    return isinstance(exc, (openai.APIConnectionError, openai.InternalServerError))  # includes APITimeoutError


class OpenAIChatClient:
    def __init__(self, settings: OpenAISettings, client=None):
        self.settings = settings
        self._client = client

    def generate(self, messages, *, inference_config=None, tools=None, tool_choice=None):
        self.settings.validate()
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise ConfigurationError("Install project dependencies to use OpenAI (openai)") from exc
            self._client = OpenAI(api_key=self.settings.api_key, timeout=120, max_retries=2)
        try:
            extra = {"tools": deepcopy(tools)} if tools else {}
            if tools and tool_choice:
                extra["tool_choice"] = tool_choice
            response = self._client.chat.completions.create(model=self.settings.model, messages=deepcopy(messages),
                                                            **deepcopy(inference_config or {}), **extra)
        except Exception as exc:  # SDK errors propagate; never fabricate a successful result
            if transient(exc):
                raise TransientModelError(f"OpenAI temporarily unavailable: {type(exc).__name__}") from exc
            raise
        choice = response.choices[0] if response.choices else None
        reason = (choice.finish_reason or "") if choice else ""
        usage = response.usage.model_dump(exclude_none=True) if response.usage is not None else {}
        if reason == "tool_calls" and tools and choice.message.tool_calls:
            calls = [{"id": c.id, "name": c.function.name, "arguments": c.function.arguments or "{}"}
                     for c in choice.message.tool_calls]
            return ModelResult(choice.message.content or "", usage, reason, calls)
        if reason != "stop":
            raise ModelResponseError(f"OpenAI did not complete a text response: {reason}")
        text = choice.message.content or ""
        if not text.strip():
            raise ModelResponseError("OpenAI returned no text")
        return ModelResult(text, usage, reason)
