import json
import re

SENSITIVE_KEYS = {"password", "secret", "secret_key", "api_key", "authorization",
                  "access_token", "refresh_token", "aws_secret_access_key",
                  "langfuse_secret_key", "langfuse_public_key"}
KEY_PATTERN = re.compile(r"\b(?:sk|pk)-lf-[A-Za-z0-9-]+\b")
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if str(key).lower() in SENSITIVE_KEYS else redact(item, secrets)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = KEY_PATTERN.sub("[REDACTED]", value)
        value = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", value)
    return value


def make_export_mask(*secrets):
    """Mask SDK/OTel attributes at export, including JSON metadata in SDK v5."""
    def mask_otel_spans(*, params):
        from langfuse.types import MaskOtelSpansResult, OtelSpanPatch
        patches = {}
        for identifier, span in params.spans.items():
            updates = {}
            for key, value in span.attributes.items():
                if key.rsplit(".", 1)[-1].lower() in SENSITIVE_KEYS:
                    masked = json.dumps("[REDACTED]") if ".metadata." in key else "[REDACTED]"
                elif isinstance(value, str):
                    try:
                        decoded = json.loads(value)
                    except (TypeError, ValueError):
                        masked = redact(value, secrets)
                    else:
                        cleaned = redact(decoded, secrets)
                        masked = json.dumps(cleaned, ensure_ascii=False) if cleaned != decoded else value
                else:
                    masked = redact(value, secrets)
                if masked != value:
                    updates[key] = masked
            if updates:
                patches[identifier] = OtelSpanPatch(set_attributes=updates)
        return MaskOtelSpansResult(span_patches=patches) if patches else None
    return mask_otel_spans
