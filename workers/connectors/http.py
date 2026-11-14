"""Bounded JSON responses and explicit remote error handling; never log response bodies."""
import json
from urllib.parse import urlsplit
import httpx
from workers.contracts import ConnectionConfigurationError, TransientRemoteError


def validate_url(url, name):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ConnectionConfigurationError(f"{name} must be an HTTP(S) URL without credentials/query/fragment")


def request_json(client, method, url, *, max_bytes=8 * 1024 * 1024, accepted=(200,), **kwargs):
    try:
        with client.stream(method, url, **kwargs) as response:
            if response.status_code == 429 or response.status_code >= 500:
                raise TransientRemoteError(f"Remote service returned HTTP {response.status_code}")
            if response.status_code not in accepted:
                raise ConnectionConfigurationError(f"Unexpected remote HTTP status {response.status_code}")
            content = bytearray()
            for part in response.iter_bytes():
                content.extend(part)
                if len(content) > max_bytes:
                    raise ValueError("Remote response exceeds the configured size limit")
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError("Remote response must be a JSON object")
            return value
    except httpx.TransportError as exc:
        raise TransientRemoteError("Remote transport failed") from exc
