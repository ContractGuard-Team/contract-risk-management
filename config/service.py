"""Authenticated Config Service. No secret enumeration or unauthenticated reads."""
import hmac
import json
import os
from pathlib import Path
from fastapi import Request, HTTPException
from common.service_http import base_app, serve
from config.secrets import SecretNotConfigured, SecretStore


def create_app():
    tokens = json.loads(os.environ["CONFIG_CLIENT_TOKENS"])
    if not isinstance(tokens, dict) or not tokens or any(not isinstance(v, str) or len(v) < 32 for v in tokens.values()):
        raise ValueError("Configure a random CONFIG_CLIENT_TOKENS token for each principal")
    if len(set(tokens.values())) != len(tokens):
        raise ValueError("Config principals must use different tokens")
    policy = json.loads(Path(os.getenv("CONFIG_POLICY_PATH", "config/policy.json")).read_text(encoding="utf-8"))
    # "config" is the broker's own principal: used in-process by its bootstrap, never served over HTTP.
    if set(policy) - set(tokens) - {"config"}:
        raise ValueError("Policy contains unknown principals")
    store = SecretStore(policy)
    app = base_app("config", authenticate=False)

    @app.get("/v1/secrets/{alias}")
    def secret(alias: str, request: Request):
        from monitoring.client import emit
        header = request.headers.get("authorization", "")
        principal = next((name for name, token in tokens.items() if hmac.compare_digest(header, "Bearer " + token)), None)
        if principal is None:
            emit("config", "secret_access", "denied", request_id=request.state.request_id)
            raise HTTPException(401, "Authentication required")
        try:
            value = store.get(principal, alias)
        except PermissionError:
            emit("config", "secret_access", "denied", request_id=request.state.request_id)
            raise HTTPException(403, "Secret alias not authorized") from None
        except SecretNotConfigured:  # API_KEYS_SECRET_ID empty: callers keep their .env values
            raise HTTPException(404, "Secret source not configured") from None
        except Exception:
            raise HTTPException(503, "Secret provider unavailable") from None
        # Only principal/alias and outcome; never include the value in telemetry.
        emit("config", "secret_access", "completed", request_id=request.state.request_id)
        return {"value": value}

    return app


if __name__ == "__main__":
    serve(create_app, "config")
