from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import os
import json
import unittest
from unittest.mock import patch

from common.exceptions import ModelResponseError
from common.schemas.agent_messages import AgentRequest, ModelResult
from hermes_integration.summarizer import ModelMemorySummarizer
from monitoring.config import LangfuseSettings
from monitoring.privacy import make_export_mask
from monitoring.usage import bedrock_usage
from monitoring.langfuse import LangfuseObserver
from test_agent_base import configured_agents
from test_contract_workflow import FakeLangfuse, RecordingProvider, contract, pipeline


class LangfuseConfigTests(unittest.TestCase):
    def test_local_file_loaded_without_mutating_environment_and_env_wins(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text("LANGFUSE_ENABLED=true\nLANGFUSE_PUBLIC_KEY=pk-test\n"
                            "LANGFUSE_SECRET_KEY=local-private\nLANGFUSE_BASE_URL=https://cloud.langfuse.com\n",
                            encoding="utf-8")
            settings = LangfuseSettings.load(path)
            self.assertTrue(settings.enabled)
            self.assertEqual(settings.secret_key, "local-private")
            self.assertNotIn("local-private", repr(settings))
            self.assertNotIn("LANGFUSE_SECRET_KEY", os.environ)
            os.environ["LANGFUSE_ENABLED"] = "false"
            self.assertFalse(LangfuseSettings.load(path).enabled)

    def test_export_mask_removes_keys_and_email_inside_json(self):
        original = {"langfuse.observation.input": json.dumps(
            {"api_key": "private-value", "text": "person@example.com sk-lf-test-secret"}),
            "langfuse.observation.metadata.secret": json.dumps("private-value"),
            "langfuse.observation.model.name": "bedrock-model"}
        params = SimpleNamespace(spans={"span": SimpleNamespace(attributes=original)})
        result = make_export_mask("private-value")(params=params)
        patch = result.span_patches["span"].set_attributes
        encoded = json.dumps(patch)
        self.assertNotIn("person@example.com", encoded)
        self.assertNotIn("private-value", encoded)
        self.assertNotIn("sk-lf-test-secret", encoded)
        self.assertEqual(json.loads(patch["langfuse.observation.metadata.secret"]), "[REDACTED]")
        self.assertNotIn("langfuse.observation.model.name", patch)

    def test_usage_does_not_invent_missing_counts(self):
        self.assertEqual(bedrock_usage({}), {})
        self.assertEqual(bedrock_usage({"inputTokens": 12, "outputTokens": 3,
                                       "cacheReadInputTokens": 5, "cacheWriteInputTokens": 6}),
                         {"input": 12, "output": 3, "input_cache_read": 5, "input_cache_write": 6})

    def test_retry_has_new_trace_but_same_session_and_safe_cache_summary(self):
        class Observer(LangfuseObserver):
            @contextmanager
            def attributes(self, **kwargs):
                sessions.append(kwargs["session_id"])
                yield

        sessions = []
        client = FakeLangfuse()
        with configured_agents(RecordingProvider()) as agents:
            flow = pipeline(agents, observer=Observer(client))
            flow.run_contract(contract(1), job_id="repeat")
            flow.run_contract(contract(1), job_id="repeat")
        roots = [value for kind, value in client.events if kind == "start"
                 and value["name"] == "contract.workflow"]
        self.assertNotEqual(roots[0]["trace_context"]["trace_id"], roots[1]["trace_context"]["trace_id"])
        self.assertEqual(sessions[0], sessions[1])
        updates = [value for kind, value in client.events if kind == "update"]
        self.assertTrue(any(value.get("output", {}).get("cache_hit") is True for value in updates))

    def test_rejected_model_responses_still_record_usage_and_error_without_content(self):
        class TruncatedProvider:
            def generate(self, messages, **kwargs):
                return ModelResult("private-contract-text", {"inputTokens": 19, "outputTokens": 7}, "max_tokens")

        provider = TruncatedProvider()
        for operation in ("agent", "summary"):
            with self.subTest(operation=operation), configured_agents(provider) as agents:
                client = FakeLangfuse()
                observer = LangfuseObserver(client)
                agent = agents["agent_1_classification"]
                with self.assertRaises(ModelResponseError):
                    if operation == "agent":
                        agent.run(AgentRequest("private-input"), observer=observer)
                    else:
                        ModelMemorySummarizer(provider, observer, agent.directory / "soul.md").summarize(
                            "private-input", 10, 1000)
                updates = [value for event, value in client.events if event == "update"]
                self.assertTrue(any(value.get("usage_details") == {"input": 19, "output": 7} for value in updates))
                self.assertTrue(any(value.get("level") == "ERROR" for value in updates))
                self.assertNotIn("private-", json.dumps(updates))
