import base64
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import boto3
import httpx
from fastapi.testclient import TestClient
from moto import mock_aws

from common.service_client import ServiceClient, call_key, RemoteServiceError
from common.service_jobs import JobStore, JobConflict
from common.service_http import identity
from config.secrets import SecretStore
from monitoring.service import EventStore, sanitize
from monitoring.langfuse_exporter import LangfuseExporter, otlp

TOKEN = "unit-test-service-token-" * 3
IDENTITY = {"job_id": "job", "runtime_id": "runtime", "contract_id": "contract", "trace_id": "a" * 32, "request_id": "request"}


class ServiceArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"INTERNAL_API_TOKEN": TOKEN, "APP_ENV": "local", "MODEL_PROVIDER": "fixture",
            **{key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ},
            "MONITORING_URL": "", "DATA_DIR": self.temp.name, "LANGFUSE_ENABLED": "false", "AWS_REGION": "us-east-1",
            "AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
            "CONFIG_SECRET_BINDINGS": "{}"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = StringIO()
        self.stdout = redirect_stdout(self.output)
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def test_durable_jobs_restart_replay_and_fencing(self):
        clock = [100]
        path = Path(self.temp.name) / "jobs.db"
        store = JobStore(path, clock=lambda: clock[0], lease_seconds=10)
        payload = {"identity": IDENTITY, "value": 1}
        key = call_key(payload)
        store.submit(key, payload)
        _, old_owner, _ = store.claim()
        store.close()
        recovered = JobStore(path, clock=lambda: clock[0], lease_seconds=10)
        self.addCleanup(recovered.close)
        self.assertIsNone(recovered.claim())
        clock[0] = 111
        _, owner, _ = recovered.claim()
        with self.assertRaises(RuntimeError):
            recovered.update(key, old_owner, stage="completed", progress=100, status="completed", result={})
        recovered.update(key, owner, stage="completed", progress=100, status="completed", result={"value": 2})
        self.assertEqual(recovered.submit(key, payload)["result"], {"value": 2})
        self.assertIsNone(recovered.claim())
        with self.assertRaises(JobConflict):
            recovered.submit(key, {"identity": IDENTITY, "value": 3})

    def test_job_retry_keeps_identity_and_increments_attempt(self):
        store = JobStore(Path(self.temp.name)/"retry.db")
        self.addCleanup(store.close)
        payload = {"identity": IDENTITY}
        key = call_key(payload)
        store.submit(key, payload)
        _, owner, _ = store.claim()
        store.update(key, owner, stage="failed", progress=10, status="failed", error_type="RemoteServiceError")
        self.assertEqual(store.submit(key, payload)["status"], "queued")
        store.claim()
        self.assertEqual(store.get(key)["attempts"], 2)

    def test_processing_http_auth_schema_and_source_provenance(self):
        from input_processing.service import create_app
        payload = {"identity": IDENTITY, "job": {"job_id":"job", "contract_id":"contract"},
                   "file": {"filename":"contract.txt", "data_base64":base64.b64encode(b"Article 1. Payment\nPay within thirty days.").decode(),
                            "metadata":{"type":"s3", "key":"contract.txt"}}}
        with TestClient(create_app()) as http:
            self.assertEqual(http.post("/v1/jobs", json=payload).status_code, 401)
            headers = {"Authorization":"Bearer "+TOKEN, "Idempotency-Key":call_key(payload)}
            response = http.post("/v1/jobs", json=payload, headers=headers)
            self.assertEqual(response.status_code, 202)
            for _ in range(100):
                row = http.get("/v1/jobs/"+call_key(payload), headers=headers).json()
                if row["status"] in ("completed", "failed"): break
                time.sleep(.02)
            self.assertEqual(row["status"], "completed", row)
            contract = row["result"]["contract"]
            self.assertTrue(contract["chunks"][0]["metadata"]["source_spans"])
            self.assertEqual(contract["context"]["preprocessing"]["document"]["key"], "contract.txt")
            changed = {**payload, "job": {"job_id":"other", "contract_id":"contract"}}
            self.assertEqual(http.post("/v1/jobs", json=changed, headers=headers).status_code, 400)
        self.assertNotIn("Pay within", self.output.getvalue())

    def test_agent_rpc_returns_memory_and_rejects_changed_revision(self):
        from agents.service import create_app
        with patch.dict(os.environ, {"AGENT_ID":"agent_1_classification", "SERVICE_NAME":"agent-1"}), TestClient(create_app()) as http:
            headers = {"Authorization":"Bearer "+TOKEN}
            desc = http.get("/v1/describe", headers=headers).json()
            payload = {"identity":IDENTITY,"execution_revision":desc["execution_revision"],"operation":"run",
                "request":{"content":"example clause","context":{"workflow":{"contract_id":"contract","contract_version":"1",
                    "chunk_id":"k0","chunk_index":0,"chunk_count":1,"source_metadata":{"pages":[2]}}}},"memory":{},
                "policy":{"max_input_tokens":10000,"summary_max_tokens":500}}
            headers["Idempotency-Key"] = call_key(payload)
            self.assertEqual(http.post("/v1/jobs", json=payload, headers=headers).status_code, 202)
            for _ in range(100):
                row = http.get("/v1/jobs/"+call_key(payload), headers=headers).json()
                if row["status"] in ("completed","failed"): break
                time.sleep(.02)
            self.assertEqual(row["status"], "completed", row)
            self.assertTrue(row["result"]["memory"]["turns"])
            answer = json.loads(row["result"]["result"]["text"])  # fixture answers with a valid ClassificationResult
            self.assertEqual((answer["chunk_id"], answer["clauses"][0]["text"], answer["clauses"][0]["page_start"]),
                             ("k0", "example clause", 2))
            payload["execution_revision"] = "changed"
            self.assertEqual(http.post("/v1/jobs", json=payload, headers=headers).status_code, 400)

    def test_fixture_cannot_run_in_production(self):
        from agents.service import AgentRuntime
        with patch.dict(os.environ, {"APP_ENV":"production"}):
            with self.assertRaises(ValueError):
                AgentRuntime("agent_1_classification")

    @mock_aws
    def test_secret_providers_cache_and_cross_principal_denial(self):
        sm = boto3.client("secretsmanager")
        sm.create_secret(Name="model", SecretString=json.dumps({"key":"never-log-this"}))
        ssm = boto3.client("ssm")
        ssm.put_parameter(Name="/worker/key", Type="SecureString", Value="ssm-value")
        policy = {"agent-1":{"model":{"provider":"secretsmanager","id_env":"MODEL_SECRET","json_key":"key"}},
                  "worker":{"parameter":{"provider":"ssm","id_env":"WORKER_PARAMETER"}}}
        with patch.dict(os.environ, {"MODEL_SECRET":"model", "WORKER_PARAMETER":"/worker/key"}):
            store = SecretStore(policy)
            self.assertEqual(store.get("agent-1","model"), "never-log-this")
            sm.delete_secret(SecretId="model", ForceDeleteWithoutRecovery=True)
            self.assertEqual(store.get("agent-1","model"), "never-log-this")
            self.assertEqual(store.get("worker","parameter"), "ssm-value")
            with self.assertRaises(PermissionError): store.get("worker","model")
        self.assertNotIn("never-log-this", self.output.getvalue())

    def test_config_api_requires_distinct_principal_tokens(self):
        from config.service import create_app
        path=Path(self.temp.name)/"policy.json"
        path.write_text(json.dumps({"worker":{}, "agent-1":{}}))
        tokens={"worker":"w"*40,"agent-1":"a"*40}
        with patch.dict(os.environ, {"CONFIG_POLICY_PATH":str(path),"CONFIG_CLIENT_TOKENS":json.dumps(tokens)}), TestClient(create_app()) as http:
            self.assertEqual(http.get("/v1/secrets/key").status_code,401)
            self.assertEqual(http.get("/v1/secrets/key",headers={"Authorization":"Bearer "+tokens["worker"]}).status_code,403)
        with patch.dict(os.environ, {"CONFIG_POLICY_PATH":str(path),"CONFIG_CLIENT_TOKENS":json.dumps({"worker":TOKEN,"agent-1":TOKEN})}):
            with self.assertRaises(ValueError): create_app()

    @mock_aws
    def test_authorized_secret_bootstrap_over_http_never_logs_value(self):
        from config.service import create_app
        from config.client import bootstrap
        value = "runtime-secret-value-not-for-logs"
        boto3.client("ssm").put_parameter(Name="/agent/model", Type="SecureString", Value=value)
        policy = {"worker": {}, "agent-1": {"model": {"provider":"ssm", "id_env":"MODEL_PARAMETER"}}}
        path = Path(self.temp.name)/"bootstrap-policy.json"
        path.write_text(json.dumps(policy))
        tokens = {"worker":"w"*40,"agent-1":"a"*40}
        env = {"MODEL_PARAMETER":"/agent/model", "CONFIG_POLICY_PATH":str(path),
               "CONFIG_CLIENT_TOKENS":json.dumps(tokens), "CONFIG_URL":"http://testserver", "CONFIG_TOKEN":tokens["agent-1"],
               "CONFIG_SECRET_BINDINGS":json.dumps({"OPENAI_MODEL":"model"})}
        with patch.dict(os.environ, env), TestClient(create_app()) as http:
            client = ServiceClient("http://testserver", token=tokens["agent-1"], client=http)
            with patch("config.client.ServiceClient", return_value=client):
                bootstrap("agent-1", load_env=False)
            self.assertEqual(os.environ["OPENAI_MODEL"], value)
        self.assertNotIn(value, self.output.getvalue())

    def test_telemetry_redacts_payloads_deduplicates_and_correlates(self):
        event={"event_id":"b"*32,"timestamp_ns":time.time_ns(),"service":"agent-1","stage":"model","status":"completed",
               **IDENTITY,"input_tokens":20,"output_tokens":10,"input":"private contract", "secret":"key"}
        store=EventStore(Path(self.temp.name)/"events.db")
        self.addCleanup(store.db.close)
        store.add(event);store.add(event)
        self.assertEqual(store.metrics()["events_by_service"],{"agent-1":1})
        saved=store.query(IDENTITY["trace_id"])[0]
        self.assertNotIn("input",saved);self.assertNotIn("secret",saved)
        exported=json.dumps(otlp(saved))
        self.assertIn("langfuse.observation.metadata.job_id",exported)
        self.assertNotIn("private contract",exported)

    def test_langfuse_optional_and_otlp_export(self):
        with patch.dict(os.environ, {"LANGFUSE_ENABLED":"true", "LANGFUSE_PUBLIC_KEY":"", "LANGFUSE_SECRET_KEY":""}):
            exporter=LangfuseExporter();self.addCleanup(exporter.close)
            self.assertFalse(exporter.active)
        requests=[]
        def handler(req):
            requests.append(req)
            return httpx.Response(200,json={})
        event={"event_id":"b"*32,"timestamp_ns":time.time_ns(),"service":"worker","stage":"completed","status":"completed",**IDENTITY}
        with patch.dict(os.environ,{"LANGFUSE_ENABLED":"true","LANGFUSE_PUBLIC_KEY":"pk","LANGFUSE_SECRET_KEY":"sk"}), httpx.Client(transport=httpx.MockTransport(handler)) as http:
            self.assertTrue(LangfuseExporter(client=http).export(event))
        self.assertEqual(requests[0].url.path,"/api/public/otel/v1/traces")
        self.assertEqual(requests[0].headers["x-langfuse-ingestion-version"],"4")
        with patch.dict(os.environ,{"APP_ENV":"local"}):
            span=otlp({**event,"service":"agent-1","stage":"agent_1_classification","model":"fixture","input_tokens":3})["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs={a["key"]:a["value"]["stringValue"] for a in span["attributes"]}
        self.assertEqual(span["name"],"agent-1.agent_1_classification")
        self.assertEqual((attrs["langfuse.environment"],attrs["langfuse.observation.model.name"],attrs["langfuse.observation.type"]),("local","fixture","generation"))

    def test_service_client_detects_wrong_job_identity(self):
        with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(202,json={"key":"wrong","identity":IDENTITY}))) as http:
            client=ServiceClient("http://processing",token=TOKEN,client=http)
            with self.assertRaises(RemoteServiceError): client.run_job({"identity":IDENTITY})

    def test_identity_rejects_unbound_or_invalid_trace(self):
        with self.assertRaises(ValueError): identity({**IDENTITY,"trace_id":"invalid"})
        with self.assertRaises(ValueError): identity({"job_id":"job"})


if __name__ == "__main__":
    unittest.main()
