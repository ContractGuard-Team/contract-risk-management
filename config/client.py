"""Load explicitly configured secret aliases at startup, never at build/import time.

Every service lists the keys it needs in CONFIG_SECRET_BINDINGS (set per service in docker-compose.yml), e.g.
'["INTERNAL_API_TOKEN", "MCP_API_TOKEN"]' (alias = key name) or {"TARGET_ENV": "alias"}. A key already set in the
environment (root .env -> Compose) is kept; only EMPTY keys are fetched from the Config service, which reads them
from AWS Secrets Manager (API_KEYS_SECRET_ID). The Config service itself reads Secrets Manager directly.
"""
import json
import logging
import os
from pathlib import Path
from dotenv import dotenv_values
from common.service_client import RemoteServiceError, ServiceClient

log = logging.getLogger("config.client")
# Keys of the AWS Secrets Manager secret contract-risk/prod/api-keys, plus legacy runtime targets.
API_KEYS = ("TAVILY_API_KEY", "QDRANT_URL", "QDRANT_API_KEY", "SUPABASE_URL",
            "SUPABASE_PUBLISHABLE_KEY", "SUPABASE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
            "MCP_API_TOKEN", "INTERNAL_API_TOKEN")
ALLOWED_TARGETS = {*API_KEYS, "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "GEMINI_MODEL",
                   "POSTGRES_DSN"}


def _bindings():
    raw = json.loads(os.getenv("CONFIG_SECRET_BINDINGS", "{}") or "{}")
    bindings = {name: name for name in raw} if isinstance(raw, list) else raw
    if not isinstance(bindings, dict) or set(bindings) - ALLOWED_TARGETS:
        raise ValueError("Invalid CONFIG_SECRET_BINDINGS targets")
    for alias in bindings.values():
        if not isinstance(alias, str) or not alias.replace("_", "").replace("-", "").isalnum():
            raise ValueError("Invalid secret alias")
    return bindings


def _fill_secrets(service):
    # A value already present (root .env for local development) wins; secrets are never overwritten.
    missing = {target: alias for target, alias in _bindings().items() if not os.getenv(target, "").strip()}
    if not missing:
        return
    if service == "config":  # the broker cannot call itself over HTTP
        from config.secrets import SecretNotConfigured, SecretStore
        policy = json.loads(Path(os.getenv("CONFIG_POLICY_PATH", "config/policy.json")).read_text(encoding="utf-8"))
        store = SecretStore({"config": policy.get("config", {})})

        def fetch(alias):
            try:
                return store.get("config", alias)
            except SecretNotConfigured:
                return None
        close = lambda: None
    else:
        client = ServiceClient(os.environ["CONFIG_URL"], token=os.environ["CONFIG_TOKEN"], timeout=15)

        def fetch(alias):
            try:
                value = client.request("GET", "/v1/secrets/" + alias)
            except RemoteServiceError as exc:
                if str(exc) == "Remote HTTP status 404":  # no API_KEYS_SECRET_ID: keep running on .env values
                    return None
                raise
            if not isinstance(value.get("value"), str) or not value["value"]:
                raise ValueError("Secret must be a non-empty string")
            return value["value"]
        close = client.close
    try:
        skipped = []
        for target, alias in missing.items():
            value = fetch(alias)
            if value is None:
                skipped.append(target)
            else:
                os.environ[target] = value
        if skipped:  # names only, never values
            log.warning("secret source not configured; still empty: %s", ", ".join(sorted(skipped)))
    finally:
        close()


def bootstrap(service, *, load_env=True):
    os.environ["SERVICE_NAME"] = service
    # Local CLI only. Docker excludes .env; Compose injects per-service variables.
    path = Path(__file__).resolve().parents[1] / ".env"
    if load_env and path.is_file():
        for key, value in dotenv_values(path).items():
            if value:
                os.environ.setdefault(key, value)
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_ENDPOINT_URL"):
        if key in os.environ and not os.environ[key].strip():
            del os.environ[key]
    if os.getenv("MODEL_PROVIDER") == "fixture" and os.getenv("APP_ENV", "production") != "local":
        raise ValueError("Fixture models are permitted only with APP_ENV=local")
    _fill_secrets(service)
    if len(os.getenv("INTERNAL_API_TOKEN", "")) < 32:
        raise ValueError("Set INTERNAL_API_TOKEN (root .env or Secrets Manager) to a random token of at least 32 characters")
