def bedrock_usage(usage):
    """Preserve reported usage only: missing usage is unknown, not zero."""
    fields = {"inputTokens": "input", "outputTokens": "output",
              "cacheReadInputTokens": "input_cache_read",
              "cacheWriteInputTokens": "input_cache_write"}
    return {target: usage[source] for source, target in fields.items()
            if source in usage and type(usage[source]) is int and usage[source] >= 0}
