from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from common.exceptions import ConfigurationError
from common.schemas.contract import ContractChunk, ContractInput, canonical
from common.schemas.agent_messages import ModelResult
from agents.hermes.context_budget import ContextBudgetExceeded, MemoryPolicy, conservative_token_count
from agents.hermes.context_engine import ContractContextEngine, ContextEngine
from agents.hermes.summarizer import ModelMemorySummarizer
from monitoring.observer import NullObserver
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
        return ModelResult("result:" + body["content"], {"prompt_tokens": 20, "completion_tokens": 5}, "stop")


class SummaryStub:
    def __init__(self):
        self.sources = []

    def summarize(self, text, target_tokens, input_budget):
        self.sources.append(text)
        return "memory: Acme; cross-reference chunk-0; source retained in cache"


def run_key(job_id, contract_id="contract-a", version="v1"):
    return hashlib.sha256(canonical([job_id, contract_id, version]).encode()).hexdigest()


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


    def test_workflow_accepts_prepared_manifest_without_preprocessing_entrypoint(self):
        provider = RecordingProvider()
        with configured_agents(provider) as agents:
            flow = pipeline(agents)
            try:
                self.assertFalse(hasattr(flow, "run_preprocessing_job"))
                result = flow.run_contract(contract(2), job_id="prepared")
                self.assertEqual(result["status"], "completed")
                self.assertEqual(len(provider.calls), 4)
                first = json.loads(provider.calls[0][-1]["content"])
                self.assertEqual(first["context"]["workflow"]["source_metadata"], {"page": 1})
            finally:
                flow.cache.close()

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
            # LangGraph aggregates with the job id and hands it to Agent 2 and Agent 3.
            self.assertEqual((aggregate["job_id"], agent2["context"]["workflow"]["job_id"],
                              agent3["context"]["workflow"]["job_id"]), ("job-1",) * 3)
            for c in contract().chunks:
                saved = flow.cache.get(result["cache_key"], "classification", c.chunk_id)
                self.assertEqual(saved["result"]["text"], "result:" + c.text)
                self.assertEqual(saved["scope"], {"contract_id": "contract-a", "contract_version": "v1",
                                                  "chunk_id": c.chunk_id, "chunk_index": c.index})
                self.assertNotIn("memory", saved)  # released once the aggregate was confirmed
            # Chunk 2 saw both earlier chunks of the same contract through Agent 1 memory.
            self.assertIn("result:clause-0", json.dumps(provider.calls[2]))
            self.assertIn("result:clause-1", json.dumps(provider.calls[2]))
            for stage in ("agent_2_risk_scoring", "agent_3_recommendation"):
                saved = flow.cache.get(result["cache_key"], stage)
                self.assertTrue(saved["result"]["text"])
                self.assertNotIn("memory", saved)  # released after the final result was stored

    def test_invalid_final_output_is_never_cached_as_complete(self):
        provider = RecordingProvider()
        with configured_agents(provider) as agents:
            flow = pipeline(agents)
            def broken(result):
                raise ValueError("final output invalid")
            with self.assertRaisesRegex(ValueError, "final output invalid"):
                flow.run_contract(contract(), job_id="job-f", finalize=broken)
            calls = len(provider.calls)
            done = flow.run_contract(contract(), job_id="job-f", finalize=lambda r: {**r, "extraction": {"ok": True}})
        self.assertEqual(done["extraction"], {"ok": True})  # rebuilt on retry, not served from a bad "complete"
        self.assertEqual(len(provider.calls), calls)  # retry reuses the cached agent outputs: no new model call

    def test_runtime_isolates_the_cache_but_agents_get_the_job_id(self):
        provider = RecordingProvider()
        with configured_agents(provider) as agents:
            flow = pipeline(agents)
            first = flow.run_contract(contract(), job_id="job-1", runtime_id="r1")
            second = flow.run_contract(contract(), job_id="job-1", runtime_id="r2")
        self.assertNotEqual(first["cache_key"], second["cache_key"])  # a new attempt never reuses another's cache
        self.assertEqual(json.loads(provider.calls[-1][-1]["content"])["context"]["workflow"]["job_id"], "job-1")

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
            key = run_key("resume")
            # Contract not finished: Agent 1 memory of processed chunks must stay for resume.
            self.assertTrue(first.cache.get(key, "classification", "chunk-0")["memory"]["turns"])
            self.assertIsNone(first.cache.get(key, "aggregate"))
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

    def test_agent_1_rejects_memory_from_another_contract(self):
        provider = RecordingProvider()
        provider.fail_content = contract(2).chunks[1].text
        with configured_agents(provider) as agents:
            flow = pipeline(agents)
            with self.assertRaises(RuntimeError):
                flow.run_contract(contract(2), job_id="mixed")
            key = run_key("mixed")
            row = flow.cache.get(key, "classification", "chunk-0")
            row["scope"]["contract_id"] = "contract-b"  # memory/result now claims another contract
            with flow.cache._db:
                flow.cache._db.execute("UPDATE entries SET value=? WHERE run_key=? AND stage='classification' AND item_key='chunk-0'",
                                       (json.dumps(row), key))
            provider.fail_content, calls = None, len(provider.calls)
            with self.assertRaises(ValueError):
                flow.run_contract(contract(2), job_id="mixed")
            self.assertEqual(len(provider.calls), calls)  # stopped before any model call
            flow.cache.close()

    def test_release_memory_is_idempotent_and_keeps_results(self):
        cache = ContractCache()
        cache.put("k", "classification", {"scope": {}, "result": {"text": "r"}, "memory": {"turns": [1]}}, "c0")
        cache.release_memory("k", "classification")
        cache.release_memory("k", "classification")
        self.assertEqual(cache.get("k", "classification", "c0"), {"scope": {}, "result": {"text": "r"}})
        cache.close()

    def test_cache_rejects_changed_input_and_changed_prompts(self):
        provider = RecordingProvider()
        with configured_agents(provider) as agents:
            flow = pipeline(agents)
            flow.run_contract(contract(1), job_id="same")
            changed = contract(1)
            changed.context["new"] = "different input"
            with self.assertRaises(ValueError):
                flow.run_contract(changed, job_id="same")
            (agents["agent_1_classification"].directory / "prompts/core.md").write_text("changed", encoding="utf-8")
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
        from orchestration.pipeline import load_memory_policies
        with TemporaryDirectory() as directory:
            empty = Path(directory) / "memory.yaml"
            empty.write_text("agent_1_classification:\n  max_input_tokens:\n", encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_memory_policies(empty)
        # The shipped budgets load for all three agents and stay under the models' 922,000 input-token limit.
        policies = load_memory_policies()
        self.assertEqual(set(policies), {"agent_1_classification", "agent_2_risk_scoring", "agent_3_recommendation"})
        self.assertTrue(all(p.max_input_tokens <= 922_000 for p in policies.values()))


class SummarizerTests(unittest.TestCase):
    def test_hierarchical_summary_covers_every_source_piece_with_bounded_requests(self):
        class SummaryProvider:
            def __init__(self):
                self.calls = []

            def generate(self, messages, **kwargs):
                self.calls.append(messages)
                return ModelResult("short summary", {}, "stop")

        with TemporaryDirectory() as directory:
            path = Path(directory) / "prompt.md"
            path.write_text("Test summary instruction", encoding="utf-8")
            provider = SummaryProvider()
            summarizer = ModelMemorySummarizer(provider, path)
            source = "SOURCE-" * 500
            self.assertEqual(summarizer.summarize(source, 30, 500), "short summary")
            self.assertTrue(all(conservative_token_count(m) <= 500 for m in provider.calls))
            pieces = [m[-1]["content"].split("Source memory:\n", 1)[1] for m in provider.calls]
            self.assertEqual("".join(pieces[:-1]), source)

    def test_missing_summary_prompt_never_calls_provider(self):
        provider = RecordingProvider()
        with TemporaryDirectory() as directory, self.assertRaises(ConfigurationError):
            empty = Path(directory) / "short_term_summary.md"
            empty.write_text("<!-- TODO -->", encoding="utf-8")
            ModelMemorySummarizer(provider, empty).summarize("history", 20, 1000)
        self.assertEqual(provider.calls, [])

    def test_each_agent_has_its_own_filled_summary_prompt(self):
        texts = {agent: Path(f"agents/{agent}/memory/short_term_summary.md").read_text(encoding="utf-8")
                 for agent in ("agent_1_classification", "agent_2_risk_scoring", "agent_3_recommendation")}
        self.assertEqual(len(set(texts.values())), 3)
        self.assertIn("clause_id", texts["agent_1_classification"])
        self.assertIn("finding_id", texts["agent_3_recommendation"])


if __name__ == "__main__":
    unittest.main()
