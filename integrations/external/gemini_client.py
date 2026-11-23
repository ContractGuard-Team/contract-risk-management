"""Gemini model provider of the three agents: Gemini API `generateContent` over REST (httpx, no extra SDK).

Agents keep their chat-format messages ({role, content}, assistant tool_calls, role "tool" results); this client
converts them to Gemini `contents` and returns usage under the Chat Completions names (prompt_tokens,
completion_tokens, *_details) that memory, monitoring and the cache already read. Gemini 3 rejects a tool round whose
function calls come back without their `thoughtSignature`, so each tool call carries it as "thought_signature"
(agents/base.py keeps it in the transcript). The HTTP client is created on the first call, so agents start without
network; a missing key/model fails before any request.
"""
from copy import deepcopy
import json
import re
import time
import uuid

from common.exceptions import ConfigurationError, ModelResponseError, TransientModelError
from common.schemas.agent_messages import ModelResult
from common.settings import GeminiSettings

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Agent config.yaml inference_config keys (provider-neutral names): max_completion_tokens -> maxOutputTokens
# (thinking tokens count toward it), reasoning_effort -> thinkingConfig.thinkingLevel, stop -> stopSequences.
INFERENCE_KEYS = ("max_completion_tokens", "temperature", "top_p", "stop", "reasoning_effort")
REASONING_EFFORTS = ("minimal", "low", "medium", "high")  # Gemini 3 thinkingLevel values
TOOL_CHOICE = {"auto": "AUTO", "required": "ANY", "none": "NONE"}
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 5   # like the OpenAI SDK before: a per-minute 429 usually clears within the retries
MAX_DELAY = 60.0


def _args(text):
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _response(content):
    """Tool result string -> functionResponse.response (must be an object)."""
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        value = content
    return value if isinstance(value, dict) else {"result": value}


def to_contents(messages):
    """Chat-format messages -> (systemInstruction parts, contents)."""
    system, contents, names = [], [], {}
    for message in messages:
        role = message.get("role")
        if role == "system":
            if message.get("content"):
                system.append({"text": message["content"]})
            continue
        if role == "tool":  # Gemini pairs results with calls by name and order; consecutive results share one turn
            part = {"functionResponse": {"name": names.get(message["tool_call_id"], ""),
                                         "response": _response(message.get("content"))}}
            last = contents[-1] if contents else None
            if last and last["role"] == "user" and all("functionResponse" in p for p in last["parts"]):
                last["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})
            continue
        parts = [{"text": message["content"]}] if message.get("content") else []
        for call in message.get("tool_calls") or []:
            names[call["id"]] = call["function"]["name"]
            part = {"functionCall": {"name": call["function"]["name"], "args": _args(call["function"].get("arguments"))}}
            if call.get("thought_signature"):
                part["thoughtSignature"] = call["thought_signature"]
            parts.append(part)
        if parts:
            contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})
    return system, contents


def to_request(inference_config, tools, tool_choice):
    config = deepcopy(inference_config or {})
    generation = {}
    if config.get("max_completion_tokens") is not None:
        generation["maxOutputTokens"] = config["max_completion_tokens"]
    if config.get("temperature") is not None:
        generation["temperature"] = config["temperature"]
    if config.get("top_p") is not None:
        generation["topP"] = config["top_p"]
    if config.get("stop"):
        generation["stopSequences"] = config["stop"] if isinstance(config["stop"], list) else [config["stop"]]
    if config.get("reasoning_effort") is not None:
        if config["reasoning_effort"] not in REASONING_EFFORTS:
            raise ConfigurationError(f"reasoning_effort must be one of {list(REASONING_EFFORTS)}")
        generation["thinkingConfig"] = {"thinkingLevel": config["reasoning_effort"]}
    request = {"generationConfig": generation} if generation else {}
    if tools:  # chat-style {"type":"function","function":{...}} -> Gemini function declarations (full JSON Schema)
        request["tools"] = [{"functionDeclarations": [
            {"name": spec["function"]["name"], "description": spec["function"].get("description", ""),
             "parametersJsonSchema": spec["function"].get("parameters") or {"type": "object", "properties": {}}}
            for spec in tools]}]
        if tool_choice:
            request["toolConfig"] = {"functionCallingConfig": {"mode": TOOL_CHOICE.get(tool_choice, "AUTO")}}
    return request


def chat_usage(usage):
    """Gemini usageMetadata -> the Chat Completions names the rest of the project reads (thinking = output)."""
    usage = usage or {}
    prompt = usage.get("promptTokenCount")
    if prompt is not None and usage.get("toolUsePromptTokenCount"):
        prompt += usage["toolUsePromptTokenCount"]
    candidates, thoughts = usage.get("candidatesTokenCount"), usage.get("thoughtsTokenCount")
    completion = None if candidates is None and thoughts is None else (candidates or 0) + (thoughts or 0)
    out = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": usage.get("totalTokenCount"),
           "prompt_tokens_details": {"cached_tokens": usage.get("cachedContentTokenCount")},
           "completion_tokens_details": {"reasoning_tokens": thoughts}}
    out = {key: ({k: v for k, v in value.items() if v is not None} if isinstance(value, dict) else value)
           for key, value in out.items()}
    return {key: value for key, value in out.items() if value not in (None, {})}


def _error(response):
    try:
        error = response.json().get("error") or {}
    except ValueError:
        error = {}
    delay = None
    for detail in error.get("details") or []:
        match = re.fullmatch(r"([\d.]+)s", str(detail.get("retryDelay", "")))
        if match:
            delay = float(match.group(1))
    return error.get("status"), str(error.get("message", ""))[:300], delay


class GeminiChatClient:
    def __init__(self, settings: GeminiSettings, client=None, sleep=time.sleep):
        self.settings = settings
        self._client = client
        self._sleep = sleep

    def _post(self, body):
        import httpx
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(300, connect=20))
        url = API_URL.format(model=self.settings.model)
        headers = {"x-goog-api-key": self.settings.api_key, "Content-Type": "application/json"}
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self._client.post(url, headers=headers, json=body)
            except httpx.TransportError as exc:  # connection/timeout: retry, then transient
                if attempt == MAX_RETRIES:
                    raise TransientModelError(f"Gemini temporarily unavailable: {type(exc).__name__}") from exc
                self._sleep(min(2 ** attempt, MAX_DELAY))
                continue
            if response.status_code == 200:
                return response.json()
            status, message, delay = _error(response)
            if response.status_code in RETRY_STATUS:
                if attempt == MAX_RETRIES:
                    # Status only, never the request: enough to tell a rate limit from an outage.
                    raise TransientModelError(f"Gemini temporarily unavailable: status={response.status_code} "
                                              f"code={status}")
                self._sleep(min(delay if delay is not None else 2 ** attempt, MAX_DELAY))
                continue
            raise ModelResponseError(f"Gemini request failed: status={response.status_code} code={status} {message}")

    def generate(self, messages, *, inference_config=None, tools=None, tool_choice=None):
        self.settings.validate()
        request = to_request(inference_config, tools, tool_choice)
        system, contents = to_contents(messages)
        body = {"contents": contents, **request}
        if system:
            body["systemInstruction"] = {"parts": system}
        data = self._post(body)
        usage = chat_usage(data.get("usageMetadata"))
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise ModelResponseError(f"Gemini blocked the prompt: {blocked}")
        candidates = data.get("candidates") or []
        if not candidates:
            raise ModelResponseError("Gemini returned no candidate")
        candidate = candidates[0]
        reason = candidate.get("finishReason")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
        calls = [{"id": f"call_{uuid.uuid4().hex[:24]}", "name": p["functionCall"]["name"],
                  "arguments": json.dumps(p["functionCall"].get("args") or {}, ensure_ascii=False),
                  **({"thought_signature": p["thoughtSignature"]} if p.get("thoughtSignature") else {})}
                 for p in parts if "functionCall" in p]
        if reason not in (None, "STOP"):
            raise ModelResponseError(f"Gemini did not complete a response: {reason}")
        if calls:
            if not tools:
                raise ModelResponseError("Gemini asked for a tool that was not offered")
            return ModelResult(text, usage, "tool_calls", calls)
        if not text.strip():
            raise ModelResponseError("Gemini returned no text")
        return ModelResult(text, usage, "stop")
