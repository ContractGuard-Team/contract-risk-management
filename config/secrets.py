"""Per-principal alias policy; callers cannot supply arbitrary AWS secret IDs.

Production keys live in ONE Secrets Manager secret (API_KEYS_SECRET_ID, e.g. contract-risk/prod/api-keys) holding a
JSON object; each alias in config/policy.json picks one `json_key` of it. The raw secret is cached per secret ID for
`ttl` seconds, so one service start costs one GetSecretValue, not one per key.
"""
import json
import os
from threading import RLock
import time
import boto3
from botocore.config import Config

AWS_CONFIG = Config(connect_timeout=5, read_timeout=10, retries={"mode": "standard", "total_max_attempts": 3})


class SecretNotConfigured(LookupError):
    """The alias is authorised but its secret ID variable is empty (e.g. local run without AWS)."""


def read_secret_json(secret_id, *, session=None):
    """Whole JSON secret from Secrets Manager (used by the data pipeline, which runs outside Docker)."""
    client = (session or boto3.Session()).client("secretsmanager", region_name=os.getenv("AWS_REGION") or None,
                                                 config=AWS_CONFIG)
    value = json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])
    if not isinstance(value, dict):
        raise ValueError("Secret must be a JSON object")
    return value


class SecretStore:
    def __init__(self, policy, *, session=None, ttl=30, clock=time.monotonic):
        if not isinstance(policy, dict) or not all(isinstance(v, dict) for v in policy.values()):
            raise ValueError("Secret policy must map principals to aliases")
        self.policy, self.session, self.ttl, self.clock = policy, session or boto3.Session(), ttl, clock
        self.cache, self.lock = {}, RLock()
        for aliases in policy.values():
            for spec in aliases.values():
                if not isinstance(spec, dict) or spec.get("provider") not in ("secretsmanager", "ssm"):
                    raise ValueError("Invalid secret provider")
                if not isinstance(spec.get("id_env"), str) or not spec["id_env"]:
                    raise ValueError("Missing configured secret ID environment variable")

    def _raw(self, provider, secret_id):
        key = (provider, secret_id)
        with self.lock:
            cached = self.cache.get(key)
            if cached and self.clock() < cached[0]:
                return cached[1]
        client = self.session.client(provider, region_name=os.getenv("AWS_REGION") or None, config=AWS_CONFIG)
        if provider == "secretsmanager":
            value = client.get_secret_value(SecretId=secret_id).get("SecretString")
        else:
            value = client.get_parameter(Name=secret_id, WithDecryption=True)["Parameter"]["Value"]
        with self.lock:
            self.cache[key] = (self.clock() + self.ttl, value)
        return value

    def get(self, principal, alias):
        spec = self.policy.get(principal, {}).get(alias)
        if spec is None:
            raise PermissionError("Secret alias not authorized")
        secret_id = os.getenv(spec["id_env"], "").strip()
        if not secret_id:
            raise SecretNotConfigured(f"{spec['id_env']} is not set")
        value = self._raw(spec["provider"], secret_id)
        if spec.get("json_key"):
            value = json.loads(value).get(spec["json_key"])
        if not isinstance(value, str) or not value:
            raise ValueError("Secret is not a non-empty string")
        return value
