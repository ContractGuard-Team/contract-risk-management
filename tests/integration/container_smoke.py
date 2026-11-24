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
           "WORKER_DYNAMODB_EVENTS_TABLE":"smoke-events", 
           "WORKER_OUTPUT_BUCKET":"smoke-output", "WORKER_INPUT_BUCKET":"smoke-input",
           "WORKER_SHUTDOWN_SECONDS":"5", "MONITORING_URL":"http://monitoring:8000",
           "WORKER_RESULTS_QUEUE_URL":""}  # internal message format; .env may enable ContractGuard mode
    process = None
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
        body = json.dumps({"job_id":job_id,"contract_id":"contract-"+run_id,"bucket":"smoke-input","source_file_key":filename,
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
            assert output["result"]["schema_version"] == "2.0.0" and output["result"]["job_id"] == job_id
            assert len(output["result"]["checks"]) == 20 and output["result"]["clauses"]
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
            # events table: one outcome log per job, nothing else.
            from boto3.dynamodb.conditions import Key
            def logs(job):
                return history.query(KeyConditionExpression=Key("job_id").eq(job))["Items"]
            [outcome] = logs(job_id)
            assert (outcome["event_id"], outcome["status"]) == ("outcome", "completed") and outcome["output"] == row["output"]
            assert (row["location"], row["status"]) == ("worker", "completed")
            print("PASS: jobs row completed at worker + one completed log with the same S3 output", flush=True)

            # A new job for the same contract resets only the current row.
            next_body = {**json.loads(body), "job_id":job_id+"-rerun"}
            sqs.send_message(QueueUrl=queue, MessageBody=json.dumps(next_body))
            def rerun_complete():
                current = table.get_item(Key=current_key).get("Item", {})
                return current if current.get("active_job_id") == next_body["job_id"] and current.get("status") == "completed" else None
            rerun = wait_for(rerun_complete)
            wait_for(empty_queue, 15)
            assert rerun["runtime_id"] != row["runtime_id"]
            assert logs(job_id) == [outcome]
            sqs.send_message(QueueUrl=queue, MessageBody=body)
            wait_for(empty_queue, 15)
            assert table.get_item(Key=current_key)["Item"]["active_job_id"] == next_body["job_id"]
            # Cancel: the worker answers, removes the message and never runs it.
            cancelled_body = {**json.loads(body), "job_id":job_id+"-cancel"}
            with httpx.Client(timeout=10) as client:
                answer = client.post(f"http://127.0.0.1:8000/v1/jobs/{cancelled_body['job_id']}/cancel", headers=headers)
                assert answer.status_code == 200 and answer.json()["status"] == "cancelled"
            sqs.send_message(QueueUrl=queue, MessageBody=json.dumps(cancelled_body))
            wait_for(empty_queue, 15)
            assert [l["status"] for l in logs(cancelled_body["job_id"])] == ["cancelled"]
            assert table.get_item(Key=current_key)["Item"]["active_job_id"] == next_body["job_id"]
            print("PASS: cancelled job logged, removed and not processed", flush=True)
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
        server.stop()


if __name__ == "__main__":
    main()
