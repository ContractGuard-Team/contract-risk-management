def model_usage(usage):
    """OpenAI usage -> Langfuse usage_details. Only reported values: missing usage is unknown, not zero."""
    usage = usage or {}
    values = {"input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens"),
              "input_cache_read": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
              "output_reasoning": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")}
    return {key: value for key, value in values.items() if type(value) is int and value >= 0}
