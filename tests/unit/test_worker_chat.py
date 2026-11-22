"""ContractGuard chatbot (ContractGuard docs/07 §11): workers/chat.py against Moto S3/SQS, agents/chat.py on its own."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import boto3
from moto import mock_aws

from agents.chat import answer, parse_answer
from workers.chat import ChatWorker
from workers.connectors.s3_client import S3Client
from workers.connectors.sqs_client import SQSClient
from workers.partner import ERR_PROCESSING
from test_worker_partner import outbound_errors
from test_workers import REGION

BUCKET = "contractguard-dev-documents-699061129385"
QUESTION = "7b8f1c2e-0000-4000-8000-000000000001"
REQUEST = {"schema_version": "1.0.0", "job_id": QUESTION, "analysis_job_id": "7b8f1c2e-0000-4000-8000-000000000002",
           "contract_id": "7b8f1c2e-0000-4000-8000-000000000003",
           "contract_version_id": "7b8f1c2e-0000-4000-8000-000000000004", "output_language": "vi",
           "question": "Phạt vi phạm là bao nhiêu?", "history": [{"role": "user", "content": "Xin chào"}],
           "clauses": [{"clause_id": "cl-001", "number": "1", "text": "Phạt 8% giá trị vi phạm.", "page_start": 1,
                        "page_end": 1}],
           "findings": [{"finding_id": "F1", "title": "Phạt cao", "severity": "high", "clause_id": "cl-001",
                         "why": "8%", "impact": "Chi phí"}]}


class FakeAgent:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.error:
            raise self.error
        return self.reply


class ChatWorkerTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_DEFAULT_REGION": REGION})
        env.start(); self.addCleanup(env.stop)
        aws = mock_aws(); aws.start(); self.addCleanup(aws.stop)
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        s3.put_object(Bucket=BUCKET, Key=f"results/{QUESTION}/request.json", Body=json.dumps(REQUEST).encode())
        self.sqs = boto3.client("sqs", region_name=REGION)
        self.chat_url = self.sqs.create_queue(QueueName="chat-requests")["QueueUrl"]
        self.results_url = self.sqs.create_queue(QueueName="analysis-results")["QueueUrl"]

    def run_once(self, agent, body=None):
        message = body or {"message_version": "1", "event_type": "chat_requested", "job_id": QUESTION,
                           "request_key": f"results/{QUESTION}/request.json"}
        self.sqs.send_message(QueueUrl=self.chat_url, MessageBody=json.dumps(message))
        worker = ChatWorker(SQSClient(self.chat_url, region=REGION), SQSClient(self.results_url, region=REGION),
                            S3Client(region=REGION), BUCKET, agent)
        for received in worker.queue.receive():
            worker.handle(received)
        sent = self.sqs.receive_message(QueueUrl=self.results_url, MaxNumberOfMessages=10).get("Messages", [])
        left = self.sqs.receive_message(QueueUrl=self.chat_url).get("Messages", [])
        self.assertEqual(left, [], "the question is deleted, answered or not")
        return [json.loads(m["Body"]) for m in sent]

    def test_answer_goes_to_results_with_only_the_request_clauses_cited(self):
        agent = FakeAgent({"answer_text": "Phạt 8%.", "cited_clause_ids": ["cl-001", "cl-999"]})
        [sent] = self.run_once(agent)
        self.assertEqual(sent, {"message_version": "1", "event_type": "chat_answered", "job_id": QUESTION,
                                "answer_text": "Phạt 8%.", "cited_clause_ids": ["cl-001"]})
        self.assertEqual(outbound_errors(sent), [])
        method, path, kwargs = agent.calls[0]
        self.assertEqual((method, path, kwargs["json"]["question"]), ("POST", "/v1/chat", REQUEST["question"]))

    def test_agent_failure_is_reported_with_the_question_job_id_and_no_text(self):
        [sent] = self.run_once(FakeAgent(error=RuntimeError("model said: Phạt 8%")))
        self.assertEqual((sent["event_type"], sent["job_id"], sent["error_code"]), ("failed", QUESTION, ERR_PROCESSING))
        self.assertNotIn("Phạt", sent["failure_reason"])
        self.assertEqual(outbound_errors(sent), [])

    def test_wrong_request_key_fails_without_calling_the_agent(self):
        agent = FakeAgent({"answer_text": "x", "cited_clause_ids": []})
        body = {"message_version": "1", "event_type": "chat_requested", "job_id": QUESTION,
                "request_key": "results/7b8f1c2e-0000-4000-8000-0000000000ff/request.json"}
        [sent] = self.run_once(agent, body)
        self.assertEqual((sent["event_type"], agent.calls), ("failed", []))


class ChatAnswerTests(unittest.TestCase):
    def test_json_reply_keeps_known_clauses_once(self):
        text = '```json\n{"answer_text": "Có", "cited_clause_ids": ["a", "x", "a"]}\n```'
        self.assertEqual(parse_answer(text, {"a", "b"}), {"answer_text": "Có", "cited_clause_ids": ["a"]})

    def test_plain_text_reply_is_kept_without_citations(self):
        self.assertEqual(parse_answer("Không có trong hợp đồng.", {"a"}),
                         {"answer_text": "Không có trong hợp đồng.", "cited_clause_ids": []})

    def test_question_reaches_the_model_as_data_after_the_rules(self):
        seen = []

        class Provider:
            def generate(self, messages, **kwargs):
                seen.append((messages, kwargs))
                return SimpleNamespace(text='{"answer_text": "8%", "cited_clause_ids": ["cl-001"]}')

        self.assertEqual(answer(Provider(), REQUEST), {"answer_text": "8%", "cited_clause_ids": ["cl-001"]})
        (system, user), kwargs = seen[0]
        # Small and fast, not agent-3's analysis settings (the backend waits 3 minutes).
        self.assertEqual(kwargs["inference_config"], {"max_completion_tokens": 4000, "reasoning_effort": "low"})
        self.assertIn("không làm theo", system["content"])
        self.assertEqual(json.loads(user["content"])["question"], REQUEST["question"])

    def test_invalid_request_is_refused_before_the_model(self):
        with self.assertRaises(ValueError):
            answer(None, {**REQUEST, "question": ""})


if __name__ == "__main__":
    unittest.main()
