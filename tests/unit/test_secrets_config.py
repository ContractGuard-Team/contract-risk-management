"""AWS Secrets Manager contract-risk/prod/api-keys -> Config service -> each service only its own keys."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import boto3
from fastapi.testclient import TestClient
from moto import mock_aws
import yaml

from common.service_client import ServiceClient
from config.client import API_KEYS, bootstrap
from config.secrets import SecretNotConfigured, SecretStore

ROOT = Path(__file__).resolve().parents[2]
POLICY = json.loads((ROOT / "config/policy.json").read_text(encoding="utf-8"))
SECRET = {key: f"value-of-{key.lower()}-" + "x" * 32 for key in API_KEYS}
TOKENS = {name: name[0] * 40 + name for name in POLICY if name != "config"}
CLEAN = {key: "" for key in (*API_KEYS, "GEMINI_MODEL")}


class SecretsConfigTests(unittest.TestCase):
    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.env = patch.dict(os.environ, {**CLEAN, "AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x",
                                           "AWS_DEFAULT_REGION": "ap-southeast-1", "AWS_REGION": "ap-southeast-1",
                                           "API_KEYS_SECRET_ID": "contract-risk/prod/api-keys",
                                           "CONFIG_POLICY_PATH": str(ROOT / "config/policy.json"),
                                           "CONFIG_CLIENT_TOKENS": json.dumps(TOKENS)})
        self.env.start()
        self.addCleanup(self.env.stop)
        boto3.client("secretsmanager").create_secret(Name="contract-risk/prod/api-keys", SecretString=json.dumps(SECRET))

    def test_policy_covers_the_secret_and_compose_requests_exactly_the_policy(self):
        self.assertEqual({k for aliases in POLICY.values() for k in aliases}, set(API_KEYS))
        services = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
        for name, service in services.items():
            with self.subTest(service=name):
                self.assertEqual(json.loads(service["environment"]["CONFIG_SECRET_BINDINGS"]), list(POLICY[name]))
        self.assertEqual(set(POLICY["agent-1"]), {"INTERNAL_API_TOKEN"})  # Gemini key: not in Secrets Manager (test)
        self.assertNotIn("SUPABASE_SECRET_KEY", POLICY["agent-2"])  # agents reach data only through mcp

    def test_each_principal_gets_only_its_keys_one_aws_call(self):
        store = SecretStore(POLICY)
        self.assertEqual(store.get("mcp", "TAVILY_API_KEY"), SECRET["TAVILY_API_KEY"])
        self.assertEqual(store.get("mcp", "QDRANT_URL"), SECRET["QDRANT_URL"])
        self.assertEqual(len(store.cache), 1)  # one GetSecretValue for every key of the JSON secret
        with self.assertRaises(PermissionError):
            store.get("agent-1", "TAVILY_API_KEY")
        with patch.dict(os.environ, {"API_KEYS_SECRET_ID": ""}), self.assertRaises(SecretNotConfigured):
            SecretStore(POLICY).get("mcp", "QDRANT_URL")

    def bootstrap_via_http(self, service, env):
        from config.service import create_app
        principal_env = {"CONFIG_URL": "http://testserver", "CONFIG_TOKEN": TOKENS[service],
                         "CONFIG_SECRET_BINDINGS": json.dumps(list(POLICY[service])), **env}
        with patch.dict(os.environ, principal_env), TestClient(create_app()) as http:
            client = ServiceClient("http://testserver", token=TOKENS[service], client=http)
            with patch("config.client.ServiceClient", return_value=client):
                bootstrap(service, load_env=False)
            return {key: os.environ.get(key, "") for key in API_KEYS}

    def test_services_fill_empty_keys_from_secrets_manager_and_keep_env_values(self):
        got = self.bootstrap_via_http("agent-2", {})
        self.assertEqual({k: v for k, v in got.items() if v}, {k: SECRET[k] for k in POLICY["agent-2"]})
        local = "local-mcp-token-from-dotenv-" + "y" * 10
        got = self.bootstrap_via_http("agent-3", {"MCP_API_TOKEN": local})
        self.assertEqual(got["MCP_API_TOKEN"], local)  # a value in .env wins, never overwritten
        self.assertEqual(got["INTERNAL_API_TOKEN"], SECRET["INTERNAL_API_TOKEN"])

    def test_without_secret_id_services_run_on_dotenv_values(self):
        token = "t" * 40
        got = self.bootstrap_via_http("monitoring", {"API_KEYS_SECRET_ID": "", "INTERNAL_API_TOKEN": token})
        self.assertEqual((got["INTERNAL_API_TOKEN"], got["LANGFUSE_PUBLIC_KEY"]), (token, ""))  # 404 -> kept empty
        with self.assertRaisesRegex(ValueError, "INTERNAL_API_TOKEN"):
            self.bootstrap_via_http("worker", {"API_KEYS_SECRET_ID": ""})

    def test_config_service_reads_its_own_token_directly(self):
        with patch.dict(os.environ, {"CONFIG_SECRET_BINDINGS": '["INTERNAL_API_TOKEN"]'}):
            bootstrap("config", load_env=False)
            self.assertEqual(os.environ["INTERNAL_API_TOKEN"], SECRET["INTERNAL_API_TOKEN"])

    def test_data_pipeline_reads_the_same_secret_for_empty_keys(self):
        from data_pipeline import pipeline
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("QDRANT_URL=https://from-dotenv\nQDRANT_API_KEY=\n", encoding="utf-8")
            with patch.dict(os.environ, {"DATA_PIPELINE_ENV_FILE": str(env_file), "QDRANT_URL": ""}):
                del os.environ["QDRANT_URL"]
                pipeline.load_environment()
                self.assertEqual(os.environ["QDRANT_URL"], "https://from-dotenv")
                self.assertEqual(os.environ["QDRANT_API_KEY"], SECRET["QDRANT_API_KEY"])
                self.assertEqual(os.environ["SUPABASE_SECRET_KEY"], SECRET["SUPABASE_SECRET_KEY"])
                self.assertEqual(os.environ.get("TAVILY_API_KEY"), "")  # pipeline takes only its own keys


if __name__ == "__main__":
    unittest.main()
