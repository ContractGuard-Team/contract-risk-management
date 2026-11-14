"""Run in the test image on the Compose network. No AWS or model credentials needed.

Real HTTP across input-processing, LangGraph and three agents; real spawned worker
processes call a local Moto HTTP server for SQS/S3/DynamoDB. Langfuse is optional telemetry.
"""
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4
import boto3
import httpx
from moto.server import ThreadedMotoServer

# Run as a script, sys.path[0] is tests/integration; in-process probes import workers/ from the app root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def input_fixture(kind):
    if kind == "txt":
        text = ("Điều 1. Thanh toán\nBên A thanh toán trong ba mươi ngày.\n\n"
                "Điều 2. Bảo mật\nCác bên bảo vệ thông tin hợp đồng.\n\n"
                "Điều 3. Chấm dứt\nMỗi bên thông báo trước mười ngày.")
        return "contract.txt", text.encode(), text
    if kind == "png":
        from io import BytesIO
        from PIL import Image, ImageDraw, ImageFont
        text = "PAYMENT TERM 30 DAYS\nCONFIDENTIAL CONTRACT\nNOTICE PERIOD 10 DAYS"
        image = Image.new("RGB", (1100, 320), "white")
        ImageDraw.Draw(image).multiline_text((40, 40), text, fill="black",
            font=ImageFont.load_default(size=38), spacing=24)
        output = BytesIO()
        image.save(output, format="PNG")
        return "contract.png", output.getvalue(), text
    raise ValueError("SMOKE_INPUT_KIND must be txt or png")


def wait_for(check, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.25)
    raise TimeoutError("Container smoke deadline exceeded")


def main():
    if os.getenv("MODEL_PROVIDER") != "fixture" or os.getenv("APP_ENV") != "local":
        raise RuntimeError("Smoke test requires explicit local fixture mode")
    token = os.environ["INTERNAL_API_TOKEN"]
    headers = {"Authorization": "Bearer " + token}
    with httpx.Client(timeout=10) as client:
        for name in ("worker", "input-processing", "agent-1", "agent-2", "agent-3", "langgraph", "monitoring", "config"):
            response = client.get(f"http://{name}:8000/readyz")
            assert response.status_code == 200, (name, response.status_code)
        # Langfuse is optional telemetry: the job must pass whether export is on or off.
        assert isinstance(client.get("http://monitoring:8000/readyz").json()["langfuse_active"], bool)
        assert client.get("http://monitoring:8000/v1/metrics").status_code == 401
        # Audit a denied config read without ever returning any secret.
        assert client.get("http://config:8000/v1/secrets/not-authorized",
                          headers={"Authorization":"Bearer "+os.environ["CONFIG_TOKEN_WORKER"]}).status_code == 403
    print("PASS: eight services ready, private APIs authenticated, Langfuse optional", flush=True)

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=5000, verbose=False)
    server.start()
    run_id = uuid4().hex
    env = {**os.environ, "AWS_ACCESS_KEY_ID":"testing", "AWS_SECRET_ACCESS_KEY":"testing",
           "AWS_SESSION_TOKEN":"", "AWS_REGION":"ap-southeast-1", "AWS_DEFAULT_REGION":"ap-southeast-1",
           "AWS_EC2_METADATA_DISABLED":"true", "AWS_ENDPOINT_URL":"http://127.0.0.1:5000",
           "WORKER_ENABLED":"true", "WORKER_INPUT_PROCESSING_URL":"http://input-processing:8000",
           "WORKER_LANGGRAPH_URL":"http://langgraph:8000", "WORKER_DYNAMODB_TABLE":"smoke-jobs",
           "WORKER_DYNAMODB_EVENTS_TABLE":"smoke-events", "DATA_DIR":"/tmp/worker-smoke-data",
           "WORKER_OUTPUT_BUCKET":"smoke-output", "WORKER_INPUT_BUCKET":"smoke-input",
           "WORKER_SHUTDOWN_SECONDS":"5", "MONITORING_URL":"http://monitoring:8000"}
    process = None
    monitor_process = None
    log_path = Path("/tmp/worker-smoke.log")
    try:
        session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="ap-southeast-1")
        sqs = session.client("sqs", endpoint_url=env["AWS_ENDPOINT_URL"])
        s3 = session.client("s3", endpoint_url=env["AWS_ENDPOINT_URL"])
        db = session.resource("dynamodb", endpoint_url=env["AWS_ENDPOINT_URL"])
        table = db.create_table(TableName="smoke-jobs", KeySchema=[{"AttributeName":"job_id","KeyType":"HASH"}],
            AttributeDefinitions=[{"AttributeName":"job_id","AttributeType":"S"}], BillingMode="PAY_PER_REQUEST")
        history = db.create_table(TableName="smoke-events", KeySchema=[{"AttributeName":"job_id","KeyType":"HASH"},{"AttributeName":"event_id","KeyType":"RANGE"}],
            AttributeDefinitions=[{"AttributeName":"job_id","AttributeType":"S"},{"AttributeName":"event_id","AttributeType":"S"}], BillingMode="PAY_PER_REQUEST")
        for bucket in ("smoke-input", "smoke-output"):
            s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint":"ap-southeast-1"})
        filename, file_bytes, text = input_fixture(os.getenv("SMOKE_INPUT_KIND", "txt"))
        s3.put_object(Bucket="smoke-input", Key=filename, Body=file_bytes)
        queue = sqs.create_queue(QueueName="smoke-contracts")["QueueUrl"]
        env["WORKER_SQS_QUEUE_URL"] = queue
        job_id = "smoke-" + run_id
        current_key = {"job_id":"contract:contract-"+run_id}
        body = json.dumps({"job_id":job_id,"contract_id":"contract-"+run_id,"s3_bucket":"smoke-input","s3_key":filename,
                          "options":{"max_tokens":120,"min_tokens":20,"overlap_tokens":0},
                          "extraction":{"ocr":"auto","languages":"vie+eng"},
                          "context":{"handoff_probe":run_id}})
        sqs.send_message(QueueUrl=queue, MessageBody=body)
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-m", "workers"], env=env, stdout=log, stderr=subprocess.STDOUT)
            def complete():
                if process.poll() is not None:
                    raise RuntimeError("Worker exited before completing the job")
                row = table.get_item(Key=current_key).get("Item", {})
                if row.get("status") == "failed":
                    raise RuntimeError("Worker failed: " + str(row.get("details", {})))
                return row if row.get("status") == "completed" else None
            row = wait_for(complete)
            assert row["progress"] == 100 and len(row["runtime_id"]) == 32
            output = json.loads(s3.get_object(Bucket=row["output"]["bucket"], Key=row["output"]["key"])["Body"].read())
            assert output["result"]["agent_id"] == "agent_3_recommendation"
            assert json.loads(output["result"]["text"])["fixture"] is True
            if audit_root := os.getenv("SMOKE_AUDIT_DIR"):
                from handoff_audit import verify_handoffs
                identity = {"job_id":job_id,"runtime_id":row["runtime_id"],"contract_id":"contract-"+run_id,
                            "trace_id":row["trace_id"],"request_id":row["trace_id"]}
                verify_handoffs(audit_root, identity, file_bytes, filename, output, run_id)
            def empty_queue():
                values = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
                return all(int(v) == 0 for v in values.values())
            wait_for(empty_queue, 15)
            sqs.send_message(QueueUrl=queue, MessageBody=body)
            wait_for(empty_queue, 15)
            duplicate = table.get_item(Key=current_key)["Item"]
            assert duplicate["runtime_id"] == row["runtime_id"] and duplicate["output"] == row["output"]
            with httpx.Client(timeout=10) as client:
                events = client.get("http://monitoring:8000/v1/events", params={"trace_id":row["trace_id"]}, headers=headers).json()["events"]
                assert {"worker","input-processing","langgraph","agent-1","agent-2","agent-3"} <= {e["service"] for e in events}
                assert all(e["job_id"] == job_id and e["request_id"] == row["trace_id"] for e in events)
                assert any("input_tokens" in e for e in events)
                assert text not in json.dumps(events)
                metrics = client.get("http://monitoring:8000/v1/metrics", headers=headers).json()
                assert {"worker","input-processing","langgraph","agent-1","agent-2","agent-3","monitoring","config"} <= set(metrics["events_by_service"])
            assert history.get_item(Key={"job_id":job_id,"event_id":"completed"}).get("Item")
            # Agent 3 output -> progress writer -> both tables, written in one transaction per event.
            from boto3.dynamodb.conditions import Key
            logs = history.query(KeyConditionExpression=Key("job_id").eq(job_id))["Items"]
            phases = sorted((l for l in logs if l["record_type"] == "worker_progress"), key=lambda l: int(l["sequence"]))
            stages = [l["stage"] for l in phases]
            for stage in ("downloading", "dispatching", "processing", "saving_result", "completed"):
                assert stage in stages, (stage, stages)
            # Agent stages come from polling LangGraph: a fast stage can fall between two polls.
            assert any(s.startswith("agent_") for s in stages), stages
            assert [int(l["progress"]) for l in phases] == sorted(int(l["progress"]) for l in phases)
            receipt = history.get_item(Key={"job_id":job_id,"event_id":"completed"})["Item"]
            assert receipt["output"] == row["output"] == phases[-1]["output"]
            assert {l["runtime_id"] for l in logs} == {row["runtime_id"]}
            print(f"PASS: Agent 3 output -> DynamoDB jobs row completed + events history {stages} + completion receipt, same S3 output", flush=True)

            # Exercise the real collector outbox -> worker HTTP -> progress process.
            # Isolated collector prevents redirecting any existing local/cloud telemetry.
            monitor_env = {**env, "PORT":"8001", "DATA_DIR":"/tmp/monitoring-smoke-data", "MONITORING_URL":"", "LANGFUSE_ENABLED":"false",
                           "MONITORING_WORKER_ENABLED":"true", "MONITORING_WORKER_URL":"http://127.0.0.1:8000"}
            with Path("/tmp/monitoring-smoke.log").open("w") as monitor_log:
                monitor_process = subprocess.Popen([sys.executable,"-m","monitoring.service"], env=monitor_env,
                    stdout=monitor_log, stderr=subprocess.STDOUT)
                with httpx.Client(timeout=10) as client:
                    def monitor_ready():
                        if monitor_process.poll() is not None:
                            raise RuntimeError("Isolated collector exited")
                        try:
                            return client.get("http://127.0.0.1:8001/readyz").status_code == 200
                        except httpx.TransportError:
                            return False
                    wait_for(monitor_ready, 20)
                    for event in events:
                        response = client.post("http://127.0.0.1:8001/v1/events", json=event, headers=headers)
                        assert response.status_code == 200
                    wait_for(lambda: all(history.get_item(Key={"job_id":job_id,"event_id":"monitoring:"+e["event_id"]}).get("Item") for e in events))
                    assert table.get_item(Key=current_key)["Item"]["status"] == "completed"

                    # An active run receives projected progress, not premature completion.
                    from workers.connectors.dynamodb_tables import DynamoTables
                    from workers.runtime.contract_job_store import ContractJobStore
                    from workers.schemas import QueueJob
                    store = ContractJobStore(DynamoTables("smoke-jobs","smoke-events","ap-southeast-1",
                        client=session.client("dynamodb", endpoint_url=env["AWS_ENDPOINT_URL"])))
                    probe_job = QueueJob.from_body({"job_id":"probe-"+run_id,"contract_id":"probe-contract-"+run_id,
                        "s3_bucket":"smoke-input","s3_key":filename})
                    probe = store.reserve(probe_job)
                    store.claim(probe_job.job_id, probe["runtime_id"], "probe-owner", 180)
                    store.bind_input(probe_job.job_id,"probe-owner","a"*64)
                    store.bind_trace(probe_job.job_id,"probe-owner","c"*32)
                    probe_event = {"event_id":uuid4().hex,"timestamp_ns":time.time_ns(),"service":"agent-2",
                        "stage":"agent_2_risk_scoring","status":"completed","job_id":probe_job.job_id,
                        "contract_id":probe_job.contract_id,"runtime_id":probe["runtime_id"],"trace_id":"c"*32,"request_id":"c"*32}
                    assert client.post("http://127.0.0.1:8001/v1/events", json=probe_event, headers=headers).status_code == 200
                    wait_for(lambda: store.client.get(probe_job.job_id)["progress"] == 75)
                    assert store.client.get(probe_job.job_id)["status"] == "running"
                    assert history.get_item(Key={"job_id":probe_job.job_id,"event_id":"monitoring:"+probe_event["event_id"]}).get("Item")
                    store.release(probe_job.job_id,"probe-owner")
                monitor_process.terminate()
                monitor_code = monitor_process.wait(timeout=25)
                monitor_tail = Path("/tmp/monitoring-smoke.log").read_text()[-3000:]
                # uvicorn>=0.29 re-raises SIGTERM after a graceful shutdown, so -15 is a clean stop too.
                assert monitor_code in (0, -15) and "Application shutdown complete" in monitor_tail, (monitor_code, monitor_tail)

            # A new job for the same contract resets only the current row.
            old_receipt = history.get_item(Key={"job_id":job_id,"event_id":"completed"})["Item"]
            next_body = {**json.loads(body), "job_id":job_id+"-rerun"}
            sqs.send_message(QueueUrl=queue, MessageBody=json.dumps(next_body))
            def rerun_complete():
                current = table.get_item(Key=current_key).get("Item", {})
                return current if current.get("active_job_id") == next_body["job_id"] and current.get("status") == "completed" else None
            rerun = wait_for(rerun_complete)
            wait_for(empty_queue, 15)
            assert rerun["runtime_id"] != row["runtime_id"]
            assert history.get_item(Key={"job_id":job_id,"event_id":"completed"})["Item"] == old_receipt
            sqs.send_message(QueueUrl=queue, MessageBody=body)
            wait_for(empty_queue, 15)
            assert table.get_item(Key=current_key)["Item"]["active_job_id"] == next_body["job_id"]
            print("PASS: monitoring outbox -> worker HTTP/inbox -> two-table history and live contract progress", flush=True)
            print("PASS: same-contract new job resets current state; old event history and duplicate protection retained", flush=True)
            print("PASS: SQS -> S3 -> processing -> LangGraph -> three agent services -> S3/DynamoDB -> SQS ACK", flush=True)
            print("PASS: runtime generated, duplicate reused result, trace correlation and token usage observed", flush=True)
            process.terminate()
            code = process.wait(timeout=15)
            assert code == 0, code
            print("PASS: two-process worker supervisor stopped cleanly", flush=True)
    except Exception:
        if log_path.exists():
            # Worker logger emits only error classes/status; no contract body or credentials.
            print(log_path.read_text(encoding="utf-8")[-8000:], flush=True)
        raise
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if monitor_process is not None and monitor_process.poll() is None:
            monitor_process.kill()
            monitor_process.wait(timeout=5)
        server.stop()


if __name__ == "__main__":
    main()
