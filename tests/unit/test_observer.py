import unittest
from types import SimpleNamespace

from monitoring.observer import NullObserver
from monitoring.usage import model_usage
from monitoring.worker_observer import WorkerObserver


class ObserverTests(unittest.TestCase):
    def test_progress_drops_content_and_emits_only_stage_counts(self):
        events = []
        observer = WorkerObserver(SimpleNamespace(emit=lambda stage, **fields: events.append((stage, fields))))
        with observer.span("agent_1_classification.chunk") as observation:
            observation.update(input="private contract", output="private response")
            observation.summary(input={"chunk_index": 1, "chunk_count": 2, "content": "private contract"},
                                output={"cache_hit": True})
        self.assertEqual(events[-1], ("agent_1", {"progress": 60, "details": {
            "state": "completed", "chunk_index": 1, "chunk_count": 2, "cache_hit": True}}))
        self.assertNotIn("private", repr(events))

    def test_failure_propagates_without_emitting_completed_stage(self):
        events = []
        worker = WorkerObserver(SimpleNamespace(emit=lambda stage, **fields: events.append((stage, fields))))
        for observer in (NullObserver(), worker):
            with self.assertRaisesRegex(RuntimeError, "model failed"):
                with observer.span("agent_2_risk_scoring.stage"):
                    raise RuntimeError("model failed")
        self.assertEqual(events, [("agent_2", {"progress": 65, "details": {"state": "started"}})])

    def test_usage_preserves_reported_counts_without_inventing_missing_values(self):
        self.assertEqual(model_usage({}), {})
        self.assertEqual(model_usage({"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15,
                                      "prompt_tokens_details": {"cached_tokens": 5},
                                      "completion_tokens_details": {"reasoning_tokens": 2}}),
                         {"input": 12, "output": 3, "input_cache_read": 5, "output_reasoning": 2})
