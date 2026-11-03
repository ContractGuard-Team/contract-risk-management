from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from common.exceptions import ConfigurationError
from common.schemas.contract import ContractChunk, ContractInput
from common.schemas.agent_messages import ModelResult
from hermes_integration.context_budget import ContextBudgetExceeded, MemoryPolicy, conservative_token_count
from hermes_integration.context_engine import ContractContextEngine, ContextEngine
from hermes_integration.summarizer import ModelMemorySummarizer
from monitoring.langfuse import LangfuseObserver, NullObserver
from orchestration.cache import ContractCache
from orchestration.pipeline import ContractRiskPipeline
from orchestration.result_aggregator import aggregate_contract
from test_agent_base import configured_agents


class RecordingProvider:
    def __init__(self):
        self.calls = []
        self.fail_content = None

    def generate(self, messages, *, inference_config=None):
        body = json.loads(messages[-1]["content"])
        self.calls.append(messages)
        if body["content"] == self.fail_content:
            raise RuntimeError("simulated model failure")
        return ModelResult("result:" + body["content"], {"inputTokens": 20, "outputTokens": 5}, "end_turn")


class SummaryStub:
    def __init__(self):
        self.sources = []

    def summarize(self, text, target_tokens, input_budget):
        self.sources.append(text)
        return "memory: Acme; cross-reference chunk-0; source retained in cache"


def contract(count=3, contract_id="contract-a"):
    return ContractInput(contract_id, "v1", [
        ContractChunk(f"chunk-{i}", i, f"clause-{i}: Acme; see clause 0", {"page": i + 1})
        for i in range(count)], count)


def pipeline(agents, *, cache=None, budget=100000, summarizer=None, observer=None):
    return ContractRiskPipeline(
        agents, cache=cache if cache is not None else ContractCache(),
        policies={key: MemoryPolicy(budget, min(200, budget // 4), 1) for key in agents},
        summarizers={key: summarizer or SummaryStub() for key in agents},
        observer=observer if observer is not None else NullObserver())


class WorkflowTests(unittest.TestCase):
    def test_hermes_engine_implements_real_extension_contract(self):
        memory = ContractContextEngine(MemoryPolicy(2000, 200), SummaryStub())
        self.assertIsInstance(memory, ContextEngine)
        self.assertEqual(memory.get_status()["context_length"], 2000)

    def test_chunks_in_order_memory_and_exact_handoffs(self):
        provider = RecordingProvider()
        with configured_agents(provider) as agents:
            flow = pipeline(agents)
            result = flow.run_contract(contract(), job_id="job-1")
            aggregate = result["results"]["agent_1_classification"]
            self.assertEqual([c["chunk_id"] for c in aggregate["chunks"]],
                             ["chunk-0", "chunk-1", "chunk-2"])
            self.assertEqual(len(provider.calls), 5)
            self.assertIn("result:clause-0", json.dumps(provider.calls[1]))
            agent2 = json.loads(provider.calls[3][-1]["content"])
            self.assertEqual(agent2["context"]["workflow"]["classification"], aggregate)
            agent3 = json.loads(provider.calls[4][-1]["content"])
            self.assertEqual(agent3["context"]["workflow"]["risk_scoring"],
                             result["results"]["agent_2_risk_scoring"])
            for c in contract().chunks:
                saved = flow.cache.get(result["cache_key"], "classification", c.chunk_id)
                self.assertEqual(saved["result"]["text"], "result:" + c.text)
                self.assertTrue(saved["memory"]["turns"])
            self.assertTrue(flow.cache.get(result["cache_key"], "agent_2_risk_scoring")["memory"]["turns"])
            self.assertTrue(flow.cache.get(result["cache_key"], "agent_3_recommendation")["memory"]["turns"])

    def test_more_than_default_graph_limit_and_bounded_context(self):
        provider, summary = RecordingProvider(), SummaryStub()
        with configured_agents(provider) as agents:
            flow = pipeline(agents, budget=1700, summarizer=summary)
            result = flow.run_contract(contract(40), job_id="long")
            self.assertEqual(len(result["results"]["agent_1_classification"]["chunks"]), 40)
            self.assertEqual(len(provider.calls), 42)
            self.assertTrue(summary.sources)
            self.assertTrue(all(conservative_token_count(call) <= 1700 for call in provider.calls))
            agent2 = json.loads(provider.calls[-2][-1]["content"])
            view = agent2["context"]["workflow"]["classification"]
            self.assertTrue(view["compacted"])
            self.assertEqual(view["chunk_count"], 40)
            self.assertIn("clause-39", summary.sources[-1])

    def test_failure_resume_uses_durable_results_and_memory(self):
        provider = RecordingProvider()
        provider.fail_content = contract().chunks[1].text
        with configured_agents(provider) as agents, TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite3"
            first = pipeline(agents, cache=ContractCache(path))
            with self.assertRaises(RuntimeError):
                first.run_contract(contract(), job_id="resume")
            self.assertEqual(len(provider.calls), 2)
            first.cache.close()
            provider.fail_content = None
            second = pipeline(agents, cache=ContractCache(path))
            result = second.run_contract(contract(), job_id="resume")
            self.assertEqual(len(provider.calls), 6)  # failed chunk retried, first chunk not recalled
            self.assertIn("result:clause-0", json.dumps(provider.calls[2]))
            self.assertEqual(result["status"], "completed")
            count = len(provider.calls)
            self.assertEqual(second.run_contract(contract(), job_id="resume"), result)
            self.assertEqual(len(provider.calls), count)
            second.cache.close()

    def test_cache_rejects_changed_input_and_changed_prompts(self):
        provider = RecordingProvider()
        with configured_agents(provider) as agents:
            flow = pipeline(agents)
            flow.run_contract(contract(1), job_id="same")
            changed = contract(1)
            changed.context["new"] = "different input"
            with self.assertRaises(ValueError):
                flow.run_contract(changed, job_id="same")
            (agents["agent_1_classification"].directory / "soul.md").write_text("changed", encoding="utf-8")
            with self.assertRaises(ValueError):
                flow.run_contract(contract(1), job_id="same")

    def test_no_cross_contract_context(self):
        provider = RecordingProvider()
        with configured_agents(provider) as agents:
            flow = pipeline(agents)
            flow.run_contract(contract(2, "private-A"), job_id="one")
            flow.run_contract(contract(1, "private-B"), job_id="one")
            self.assertNotIn("private-A", json.dumps(provider.calls[4]))
            self.assertNotIn('"role": "assistant"', json.dumps(provider.calls[4]))

    def test_missing_duplicate_reordered_and_empty_chunks_rejected(self):
        cases = [ContractInput("x", "1", [], 1),
                 ContractInput("x", "1", [ContractChunk("c", 1, "text")], 1),
                 ContractInput("x", "1", [ContractChunk("c", 0, "")], 1),
                 ContractInput("x", "1", [ContractChunk("c", 0, "x"), ContractChunk("c", 1, "y")], 2),
                 ContractInput("x", "1", [ContractChunk("c", 0, "x")], 2)]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                case.validate()
        with self.assertRaises(ValueError):
            aggregate_contract(asdict(contract()), ContractCache(), "not-processed")

    def test_oversized_single_chunk_does_not_call_agent2_or_drop_text(self):
        provider = RecordingProvider()
        large = ContractInput("huge", "1", [ContractChunk("0", 0, "x" * 5000)], 1)
        with configured_agents(provider) as agents:
            with self.assertRaises(ContextBudgetExceeded):
                pipeline(agents, budget=1700).run_contract(large, job_id="huge")
        self.assertEqual(provider.calls, [])

    def test_unset_memory_policy_fails_clearly(self):
        with configured_agents(RecordingProvider()) as agents:
            with self.assertRaises(ConfigurationError):
                ContractRiskPipeline(agents, cache=ContractCache())


class SummarizerTests(unittest.TestCase):
    def test_hierarchical_summary_covers_every_source_piece_with_bounded_requests(self):
        class SummaryProvider:
            def __init__(self):
                self.calls = []

            def generate(self, messages, **kwargs):
                self.calls.append(messages)
                return ModelResult("short summary", {}, "end_turn")

        with TemporaryDirectory() as directory:
            path = Path(directory) / "prompt.md"
            path.write_text("Test summary instruction", encoding="utf-8")
            provider = SummaryProvider()
            summarizer = ModelMemorySummarizer(provider, prompt_path=path)
            source = "SOURCE-" * 500
            self.assertEqual(summarizer.summarize(source, 30, 500), "short summary")
            self.assertTrue(all(conservative_token_count(m) <= 500 for m in provider.calls))
            pieces = [m[-1]["content"].split("Source memory:\n", 1)[1] for m in provider.calls]
            self.assertEqual("".join(pieces[:-1]), source)

    def test_missing_summary_prompt_never_calls_provider(self):
        provider = RecordingProvider()
        with self.assertRaises(ConfigurationError):
            ModelMemorySummarizer(provider).summarize("history", 20, 1000)
        self.assertEqual(provider.calls, [])


class FakeObservation:
    def __init__(self, events):
        self.events = events

    def update(self, **kwargs):
        self.events.append(("update", kwargs))


class FakeLangfuse:
    def __init__(self):
        self.events = []

    @contextmanager
    def start_as_current_observation(self, **kwargs):
        self.events.append(("start", kwargs))
        yield FakeObservation(self.events)
        self.events.append(("end", kwargs["name"]))

    def flush(self):
        self.events.append(("flush", None))


class ObservabilityTests(unittest.TestCase):
    def test_langfuse_contract_chunk_agent_usage_and_error_spans(self):
        client, provider = FakeLangfuse(), RecordingProvider()
        with configured_agents(provider) as agents:
            pipeline(agents, observer=LangfuseObserver(client)).run_contract(contract(2), job_id="traced")
        starts = [value for event, value in client.events if event == "start"]
        self.assertEqual(starts[0]["name"], "contract.workflow")
        self.assertEqual(len(starts[0]["trace_context"]["trace_id"]), 32)
        self.assertEqual(sum(s["name"] == "agent_1_classification.chunk" and s["as_type"] == "agent"
                             for s in starts), 2)
        updates = [v for e, v in client.events if e == "update"]
        self.assertTrue(any(v.get("usage_details") == {"input": 20, "output": 5} for v in updates))
        self.assertEqual(updates[0]["input"]["chunk_count"], 2)
        self.assertNotIn("clause-0", json.dumps(updates))
        self.assertEqual(client.events[-1][0], "flush")

    def test_tracing_failure_does_not_hide_application_error_or_repeat_work(self):
        client = FakeLangfuse()
        observer = LangfuseObserver(client)
        with self.assertRaisesRegex(RuntimeError, "business failure"):
            with observer.span("test"):
                raise RuntimeError("business failure")
        self.assertTrue(any(v.get("level") == "ERROR" for e, v in client.events if e == "update"))
        with patch.object(client, "start_as_current_observation", side_effect=RuntimeError("down")):
            with self.assertLogs("monitoring.langfuse", level="WARNING"):
                with observer.span("test"):
                    pass

    def test_disabled_by_default_and_requires_explicit_connection(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(LangfuseObserver.from_env(env_file=None), NullObserver)
            os.environ["LANGFUSE_ENABLED"] = "true"
            with self.assertRaises(ConfigurationError):
                LangfuseObserver.from_env(env_file=None)

    def test_installed_langfuse_sdk_offline_context_manager(self):
        from langfuse import Langfuse
        client = Langfuse(public_key="pk-lf-offline-test", secret_key="sk-lf-offline-test",
                          tracing_enabled=False)
        observer = LangfuseObserver(client)
        with observer.span("offline-sdk", kind="generation", trace_id="1" * 32, model="test") as span:
            span.update(usage_details={"input": 1, "output": 2})
        observer.flush()
        client.shutdown()


if __name__ == "__main__":
    unittest.main()
