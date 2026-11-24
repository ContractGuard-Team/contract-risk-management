"""Launch two worker processes under a small supervisor: python -m workers."""
import multiprocessing
import signal
import time

from workers.connectors.dynamodb_client import DynamoDBClient
from workers.connectors.s3_client import S3Client
from workers.connectors.sqs_client import SQSClient
from workers.runtime.job_store import JobStore
from workers.runtime.progress_worker import ProgressChannel, ProgressWorker
from workers.settings import WorkerSettings
from workers.connectors.processing_service import ProcessingServiceClient
from workers.connectors.workflow_service import WorkflowServiceClient


def make_store(settings):
    if settings.events_table_name:
        from workers.connectors.dynamodb_tables import DynamoTables
        from workers.runtime.contract_job_store import ContractJobStore
        return ContractJobStore(DynamoTables(settings.table_name, settings.events_table_name, settings.region),
                                lease_seconds=settings.lease_seconds)
    return JobStore(DynamoDBClient(settings.table_name, region=settings.region))


def _initialize_child():
    # Parent owns graceful shutdown; children finish the in-flight job within its grace period.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    from monitoring.logging import configure
    configure("worker")


def progress_process(settings, requests, responses, stop):
    _initialize_child()
    ProgressWorker(make_store(settings), S3Client(region=settings.region), settings.output_bucket,
                   settings.output_prefix).serve(requests, responses, stop)


def intake_process(settings, requests, responses, stop):
    _initialize_child()
    from workers.runtime.intake_worker import IntakeWorker

    channel = ProgressChannel(requests, responses, settings.ack_timeout)
    processor = ProcessingServiceClient(settings.input_processing_url, token=settings.service_token, timeout=settings.http_timeout)
    workflow = WorkflowServiceClient(settings.langgraph_url, token=settings.service_token, timeout=settings.http_timeout)
    storage = S3Client(region=settings.region)
    partner = None
    if settings.results_queue_url:  # ContractGuard protocol (requests/results queues, keys from request.json)
        from workers.partner import PartnerProtocol
        partner = PartnerProtocol(SQSClient(settings.results_queue_url, region=settings.region), storage,
                                  settings.input_bucket)
    worker = IntakeWorker(SQSClient(settings.queue_url, region=settings.region),
                          make_store(settings), storage, channel.send, settings,
                          processor=processor, workflow=workflow, partner=partner)
    try:
        worker.serve(stop)
    finally:
        processor.close()
        workflow.close()


def chat_process(settings, stop):
    _initialize_child()
    from common.service_client import ServiceClient
    from workers.chat import ChatWorker
    # The model answers within the backend's 3-minute wait; agent-3 holds the model key, not the worker.
    agent = ServiceClient(settings.chat_url, token=settings.service_token, timeout=150)
    ChatWorker(SQSClient(settings.chat_queue_url, region=settings.region),
               SQSClient(settings.results_queue_url, region=settings.region),
               S3Client(region=settings.region), settings.input_bucket, agent).serve(stop)


def run(settings):
    settings.validate_connections()
    if not settings.langgraph_url:
        raise ValueError("WORKER_LANGGRAPH_URL is required by the container entrypoint")
    store = make_store(settings)
    if settings.events_table_name:
        store.tables.validate_schema()
    from threading import Event, Thread
    import uvicorn
    from common.service_http import base_app
    context = multiprocessing.get_context("spawn")
    requests, responses = context.Queue(maxsize=2), context.Queue(maxsize=2)
    intake_stop, progress_stop, chat_stop = context.Event(), context.Event(), context.Event()
    # A signal can interrupt Event.wait while its non-reentrant lock is held.
    # Calling Event.set in that handler can deadlock PID 1 during docker stop.
    # The handler only flips a local flag; normal code handles IPC and cleanup.
    shutdown_requested = False

    def request_shutdown(*_):
        nonlocal shutdown_requested
        shutdown_requested = True

    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, request_shutdown)
    progress = context.Process(target=progress_process, name="progress-worker",
                               args=(settings, requests, responses, progress_stop))
    intake = context.Process(target=intake_process, name="intake-worker",
                             args=(settings, requests, responses, intake_stop))
    # ContractGuard chatbot: its own process, so a question never waits behind an analysis (workers/chat.py).
    chat = (context.Process(target=chat_process, name="chat-worker", args=(settings, chat_stop))
            if settings.chat_queue_url else None)
    processes = [p for p in (progress, intake, chat) if p]
    children = []
    failed = False
    def readiness():
        if len(children) != len(processes) or not all(p.is_alive() for p in children):
            raise RuntimeError("Worker child process unavailable")
        return {"mode": "sqs", "processes": [p.name for p in children]}
    app = base_app("worker", ready=readiness)
    cancel_stop, cancel_thread = Event(), None
    if settings.events_table_name:
        from workers import cancel
        cancel.attach(app, store)
        if settings.cancel_queue_url:
            cancel_thread = Thread(target=cancel.serve_queue, name="cancel-queue", daemon=True, args=(
                SQSClient(settings.cancel_queue_url, region=settings.region), store, cancel_stop))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8000, access_log=False, log_config=None))
    server_thread = Thread(target=server.run, daemon=True)
    try:
        for process in processes:
            process.start()
            children.append(process)
        server_thread.start()
        if cancel_thread:
            cancel_thread.start()
        while not shutdown_requested:
            if not all(process.is_alive() for process in children):
                failed = True
                break
            time.sleep(0.5)
    finally:
        server.should_exit = True
        cancel_stop.set()
        intake_stop.set()
        chat_stop.set()
        deadline = time.monotonic() + settings.shutdown_seconds
        if intake in children:
            while intake.is_alive() and time.monotonic() < deadline and progress.is_alive():
                intake.join(timeout=0.5)
            if intake.is_alive():
                intake.terminate()
            intake.join()
        if chat in children:  # its long poll returns within 20 s; an answer in flight is cut at the deadline
            chat.join(timeout=max(0, deadline - time.monotonic()))
            if chat.is_alive():
                chat.terminate()
            chat.join()
        # Keep the writer alive until intake finishes; stopping both together can lose the last ack.
        progress_stop.set()
        if progress in children:
            progress.join(timeout=max(0, deadline - time.monotonic()))
            if progress.is_alive():
                progress.terminate()
            progress.join()
        for queue in (requests, responses):
            queue.close()
            queue.cancel_join_thread()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if server_thread.is_alive():
            server_thread.join(timeout=5)
    return 1 if failed else 0


def main(argv=None):
    import argparse
    import os
    from dotenv import dotenv_values
    from pathlib import Path
    parser = argparse.ArgumentParser(description="Two-process SQS / processing / LangGraph / DynamoDB worker")
    parser.add_argument("--check-config", action="store_true", help="Validate configuration without contacting services")
    args = parser.parse_args(argv)
    env_file = Path(__file__).resolve().parents[1] / ".env"
    # Blank template entries must not select an empty AWS profile or credential.
    for name, value in dotenv_values(env_file).items():
        if value and value.strip():
            os.environ.setdefault(name, value)
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION",
                 "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        if name in os.environ and not os.environ[name].strip():
            del os.environ[name]
    from config.client import bootstrap
    from common.service_http import flag, base_app, serve
    from monitoring.logging import configure
    configure("worker")
    bootstrap("worker", load_env=False)
    if not flag("WORKER_ENABLED", "true"):
        if args.check_config:
            print("Worker disabled explicitly; configuration valid for idle mode.")
            return 0
        def idle_app():
            from monitoring.client import emit
            emit("worker", "service", "idle")
            return base_app("worker", ready=lambda: {"mode": "idle", "sqs_polling": False})
        serve(idle_app, "worker")
        return 0
    settings = WorkerSettings.from_env()
    if args.check_config:
        settings.validate_connections()
        print("Worker configuration is valid; no network requests made.")
        return 0
    return run(settings)


if __name__ == "__main__":
    raise SystemExit(main())
