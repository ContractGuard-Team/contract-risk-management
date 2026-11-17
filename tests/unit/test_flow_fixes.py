"""Regression tests for the flow review (2026-11-17): each test reproduces one reported failure case."""
import json
import os
import socket
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import boto3
from moto import mock_aws
import uvicorn

from agents.base import DROPPED_TOOL_RESULT, fit_tool_result, keep_in_budget
from agents.hermes.context_budget import ContextBudgetExceeded, MemoryPolicy, conservative_token_count
from agents.shared.tools import JOB_CONTEXT
from monitoring.service_observer import SafeObservation
from orchestration.context_transfer import handoff_request
from workers.contracts import SourceError
from workers.schemas import QueueJob

TOKEN = "t" * 40


class WorkerTests(unittest.TestCase):
    @mock_aws
    def test_unregistered_job_reads_as_absent_so_failed_can_be_sent(self):
        from workers.connectors.dynamodb_tables import DynamoTables
        from workers.runtime.contract_job_store import CurrentClient
        client = boto3.client("dynamodb", region_name="us-east-1")
        for table, keys in (("jobs", ["job_id"]), ("events", ["job_id", "event_id"])):
            client.create_table(TableName=table, BillingMode="PAY_PER_REQUEST",
                                KeySchema=[{"AttributeName": k, "KeyType": "HASH" if i == 0 else "RANGE"} for i, k in enumerate(keys)],
                                AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k in keys])
        self.assertIsNone(CurrentClient(DynamoTables("jobs", "events", "us-east-1", client=client)).get("never-registered"))

    def test_metadata_must_be_an_object(self):
        body = {"job_id": "j", "contract_id": "c", "source_file_key": "a.pdf", "metadata": []}
        with self.assertRaisesRegex(SourceError, "metadata must be an object"):
            QueueJob.from_body(body, default_bucket="b")

    def test_preprocessing_result_must_be_the_requested_version(self):
        from workers.connectors.processing_service import ProcessingServiceClient
        client = ProcessingServiceClient("http://processing:8000", token=TOKEN)
        job = SimpleNamespace(contract_id="c", contract_version="1", preprocessing={}, context={})
        file = SimpleNamespace(filename="a.pdf", data=b"x", metadata={})
        with patch.object(client, "run_job", return_value={"contract": {"contract_id": "c", "contract_version": "999"}}):
            with self.assertRaisesRegex(ValueError, "contract_version"):
                client.process(job, file, {}, guard=lambda: None)


class FakePartner:
    def read(self, key, offset=0, max_chars=15000, *, allowed=()):
        return {"key": key, "allowed": list(allowed)}


class McpJobBindingTests(unittest.TestCase):
    def test_partner_document_gets_only_this_jobs_keys_and_events_carry_the_job(self):
        from integrations.external.mcp_client import McpToolClient
        from mcp_server.service import FACTORIES, ToolGateway, create_app
        with patch.dict(os.environ, {"MCP_API_TOKEN": TOKEN, "MONITORING_URL": ""}), \
                patch("mcp_server.service.emit") as emitted, socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            server = uvicorn.Server(uvicorn.Config(create_app(ToolGateway({**FACTORIES, "partner_document": FakePartner})),
                                                   host="127.0.0.1", port=port, log_level="error"))
            threading.Thread(target=server.run, daemon=True).start()
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.05)
            try:
                client = McpToolClient(f"http://127.0.0.1:{port}/mcp", TOKEN)
                token = JOB_CONTEXT.set({"job_id": "job-7", "trace_id": "a" * 32, "partner_keys": ["quarantine/u/7.pdf"]})
                try:
                    result = client.call("partner_document", {"key": "quarantine/u/7.pdf"})
                finally:
                    JOB_CONTEXT.reset(token)
                self.assertEqual(result["allowed"], ["quarantine/u/7.pdf"])
                self.assertEqual(client.call("partner_document", {"key": "quarantine/u/7.pdf"})["allowed"], [])  # no job
            finally:
                server.should_exit = True
        tagged = [call.kwargs for call in emitted.call_args_list if call.args[1] == "tool.partner_document"]
        self.assertEqual((tagged[0]["job_id"], tagged[0]["trace_id"]), ("job-7", "a" * 32))


class AgentLoopTests(unittest.TestCase):
    def test_long_tool_result_stays_valid_json_and_keeps_next_offset(self):
        text = fit_tool_result({"text": "x" * 50_000, "next_offset": 15000}, limit=20_000)
        data = json.loads(text)
        self.assertLessEqual(len(text), 20_000)
        self.assertEqual(data["next_offset"], 15000)
        self.assertTrue(data["text"].endswith("[truncated]"))
        self.assertEqual(json.loads(fit_tool_result({"rows": ["y"] * 50_000}, limit=1000))["error"], "ResultTooLarge")

    def test_budget_is_rechecked_as_tool_results_grow(self):
        memory = SimpleNamespace(policy=MemoryPolicy(700, 100), counter=conservative_token_count)
        prepared = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        transcript = prepared + [{"role": "tool", "tool_call_id": "1", "content": "z" * 1000},
                                 {"role": "tool", "tool_call_id": "2", "content": "ok"}]
        keep_in_budget(transcript, len(prepared), memory)
        self.assertEqual(transcript[2]["content"], DROPPED_TOOL_RESULT)  # oldest tool result dropped first
        self.assertEqual(transcript[3]["content"], "ok")
        too_big = prepared + [{"role": "user", "content": "w" * 2000}]
        with self.assertRaises(ContextBudgetExceeded):
            keep_in_budget(too_big, len(prepared), memory)


class CompactedHandoffTests(unittest.TestCase):
    def test_summary_keeps_every_finding_id_for_agent_3s_cross_check(self):
        from agents.hermes.agent_factory import create_agent
        agent = create_agent("agent_3_recommendation", provider=object())
        risk = {"agent_id": "agent_2_risk_scoring", "text": json.dumps({"findings": [{"finding_id": f"F{i}"} for i in range(3)]}),
                "padding": "p" * 5000}
        summarizer = SimpleNamespace(summarize=lambda text, target, budget: "SUMMARY-MARKER")
        contract = {"contract_id": "c", "contract_version": "1", "context": {}, "expected_chunk_count": 1}
        counter = lambda messages: 10 if "SUMMARY-MARKER" in json.dumps(messages, ensure_ascii=False) else 10**9
        request = handoff_request(agent, contract, "risk_scoring", risk, MemoryPolicy(1000, 100), summarizer, counter,
                                  "k", job_id="j")
        compacted = request.context["workflow"]["risk_scoring"]
        self.assertEqual((compacted["compacted"], compacted["ids"]), (True, ["F0", "F1", "F2"]))
        answer = {"recommendations": [], "overall_recommendation": None, "evidence_gaps": []}
        self.assertIn("thiếu đề xuất", agent.check_output(answer, request)[0])


class MonitoringTests(unittest.TestCase):
    def test_cache_and_reasoning_tokens_are_kept(self):
        observation = SafeObservation()
        observation.update(usage_details={"input": 10, "input_cache_read": 4, "output_reasoning": 7})
        self.assertEqual(observation.fields, {"input_tokens": 10, "cache_read_tokens": 4, "reasoning_tokens": 7})


if __name__ == "__main__":
    unittest.main()
