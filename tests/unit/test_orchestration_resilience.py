import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
import openai
from langgraph.types import RetryPolicy

from common.exceptions import TransientModelError
from common.schemas.agent_messages import ModelResult
from common.service_client import RemoteServiceError
from common.service_jobs import JobStore
from common.settings import OpenAISettings
from integrations.external.openai_client import OpenAIChatClient
from orchestration import workflow
from orchestration.cache import ContractCache
from orchestration.checkpoint import resume_chunk
from orchestration.retry_policy import is_transient
from test_agent_base import configured_agents
from test_contract_workflow import contract, pipeline, run_key

FAST = RetryPolicy(initial_interval=0.01, max_interval=0.02, max_attempts=3, jitter=False, retry_on=is_transient)


class FlakyProvider:
    """Agent 2 (handoff content 'contract:...') fails with a chosen error a set number of times."""
    def __init__(self, error=None, failures=0):
        self.error, self.failures, self.calls = error, failures, []

    def generate(self, messages, *, inference_config=None):
        content = json.loads(messages[-1]["content"])["content"]
        self.calls.append(content)
        if content.startswith("contract:") and "risk" not in json.dumps(messages[-1]) and self.failures:
            self.failures -= 1
            raise self.error
        return ModelResult("result:" + content, {"prompt_tokens": 5, "completion_tokens": 5}, "stop")


def agent2_calls(provider):
    return [c for c in provider.calls if c.startswith("contract:")]


class RetryAndResumeTests(unittest.TestCase):
    def test_transient_agent_failure_is_retried(self):
        provider = FlakyProvider(RemoteServiceError("Remote job failed: TransientModelError"), failures=2)
        with patch.object(workflow, "AGENT_STAGE_RETRY", FAST), configured_agents(provider) as agents:
            flow = pipeline(agents)
            self.assertEqual(flow.run_contract(contract(2), job_id="retry")["status"], "completed")
            self.assertEqual(len(provider.calls), 2 + 3 + 1)  # 2 chunks, Agent 2 x3 (2 transient), Agent 3

    def test_permanent_failure_is_not_retried(self):
        provider = FlakyProvider(RemoteServiceError("Remote job failed: ModelResponseError"), failures=5)
        with patch.object(workflow, "AGENT_STAGE_RETRY", FAST), configured_agents(provider) as agents:
            flow = pipeline(agents)
            with self.assertRaises(RemoteServiceError):
                flow.run_contract(contract(2), job_id="permanent")
            self.assertEqual(len(provider.calls), 2 + 1)

    def test_resume_point_and_ram_state_released(self):
        provider = FlakyProvider(RuntimeError("Agent 2 down"), failures=1)
        with configured_agents(provider) as agents, TemporaryDirectory() as directory:
            flow = pipeline(agents, cache=ContractCache(Path(directory) / "c.sqlite3"))
            key, raw = run_key("resume2"), {"chunks": [{"chunk_id": f"chunk-{i}"} for i in range(3)],
                                            "expected_chunk_count": 3}
            self.assertEqual(resume_chunk(flow.cache, key, raw), 0)
            with self.assertRaises(RuntimeError):
                flow.run_contract(contract(3), job_id="resume2")
            self.assertEqual(resume_chunk(flow.cache, key, raw), 3)  # Agent 1 done: next run starts at Agent 2
            self.assertIsNone(flow.checkpointer.get_tuple({"configurable": {"thread_id": key}}))
            before = len(provider.calls)
            result = flow.run_contract(contract(3), job_id="resume2")
            self.assertEqual(provider.calls[before:], ["contract:contract-a contract_version:v1"] * 2)  # Agent 2, Agent 3 only
            self.assertEqual(result["status"], "completed")
            self.assertIsNone(flow.checkpointer.get_tuple({"configurable": {"thread_id": key}}))
            flow.cache.close()

    def test_retry_classification(self):
        self.assertTrue(is_transient(RemoteServiceError("Remote service unavailable after retries")))
        self.assertTrue(is_transient(RemoteServiceError("Remote job failed: TimeoutError")))
        self.assertFalse(is_transient(RemoteServiceError("Remote job failed: ValueError")))
        self.assertFalse(is_transient(TimeoutError("Internal job exceeded deadline")))

    def test_openai_rate_limit_and_outage_become_transient(self):
        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

        def status(cls, code, error_code):
            return cls("x", response=httpx.Response(code, request=request), body={"code": error_code})

        def client(error):
            def create(**_):
                raise error
            stub = type("Stub", (), {})()
            stub.chat = type("Chat", (), {"completions": type("C", (), {"create": staticmethod(create)})})()
            return OpenAIChatClient(OpenAISettings("m", "sk-test"), client=stub)
        for error in (status(openai.RateLimitError, 429, "rate_limit_exceeded"),
                      status(openai.InternalServerError, 503, None), openai.APITimeoutError(request=request),
                      openai.APIConnectionError(request=request)):
            with self.subTest(error=type(error).__name__), self.assertRaises(TransientModelError):
                client(error).generate([])
        for error in (status(openai.RateLimitError, 429, "insufficient_quota"),  # billing: retry cannot help
                      status(openai.AuthenticationError, 401, "invalid_api_key"),
                      status(openai.BadRequestError, 400, None)):
            with self.subTest(error=error.code or type(error).__name__), self.assertRaises(type(error)):
                client(error).generate([])

    def test_job_store_requeues_transient_model_failure(self):
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            payload = {"identity": {"job_id": "j"}, "x": 1}
            store.submit("k", payload)
            key, owner, _ = store.claim()
            store.update(key, owner, stage="failed", progress=0, status="failed", error_type="TransientModelError")
            self.assertEqual(store.submit("k", payload)["status"], "queued")
            store.close()


if __name__ == "__main__":
    unittest.main()
