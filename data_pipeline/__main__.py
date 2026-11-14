"""python -m data_pipeline collector | processor | collect --mode … | process | refresh

collector : long-poll SQS contract-data-update-queue (filled by EventBridge Scheduler); each message = one crawl run
processor : drain ingest_jobs continuously and run the daily effective-date refresh (never reads SQS)
collect / process / refresh : one-shot runs for manual backfill and debugging
"""
import argparse
import asyncio
import json
import logging
import os
import signal
import threading

from data_pipeline import pipeline


def _collector(limit=None):
    """One crawl run: originals -> S3 (branch 1) and in-memory bytes -> processor (branch 2)."""
    from functools import partial
    from data_pipeline.collector import collect
    from data_pipeline.processor import process_files
    ctx = pipeline.processing_context()  # model + Qdrant loaded once per service, not per message
    ctx.qdrant.ensure_indexes()
    process = partial(process_files, ctx)
    return lambda mode: _collect(mode, process, limit)


def _collect(mode, process, limit=None):
    from data_pipeline.collector import collect

    async def run():
        source = pipeline.source()
        try:
            return await collect(mode, source=source, store=pipeline.store(), archiver=pipeline.archiver(),
                                 root=os.getenv("DATA_S3_PREFIX", "legal"), limit=limit, process=process)
        finally:
            await source.aclose()
    return asyncio.run(run())


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m data_pipeline")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("collector", help="SQS consumer: one crawl per EventBridge message")
    commands.add_parser("processor", help="ingest_jobs consumer + daily refresh")
    one_shot = commands.add_parser("collect", help="one crawl run now")
    one_shot.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    one_shot.add_argument("--limit", type=int, help="collect at most N new/changed documents (trial/backfill)")
    commands.add_parser("process", help="process pending ingest_jobs once")
    commands.add_parser("refresh", help="apply effective dates and due amendments once")
    args = parser.parse_args(argv)

    service = "data-" + ("collector" if args.command in ("collector", "collect") else "processor")
    os.environ["SERVICE_NAME"] = service
    from monitoring.logging import configure
    # Readable lines in a terminal, JSON under systemd; full error message + traceback (public legal data only).
    configure(service, error_details=True)
    log = logging.getLogger("data_pipeline")
    try:
        pipeline.load_environment()
        log.info("%s %s: env %s, monitoring %s, model %s", service, args.command, pipeline.env_file(),
                 os.getenv("MONITORING_URL") or "off (DATA_MONITORING_URL empty)", os.getenv("EMBEDDING_MODEL", "default"))
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())  # finish the current run/job, then exit

        if args.command == "collector":
            import boto3
            from data_pipeline.collector import serve
            serve(pipeline.required("DATA_SQS_QUEUE_URL"), _collector(),
                  sqs=boto3.client("sqs", region_name=pipeline.required("AWS_REGION")),
                  wait_seconds=int(os.getenv("DATA_SQS_WAIT_SECONDS", "20")),
                  visibility_seconds=int(os.getenv("DATA_SQS_VISIBILITY_SECONDS", "900")), stop=stop)
        elif args.command == "collect":
            print(json.dumps(_collector(args.limit)(args.mode), ensure_ascii=False, default=str))
        else:
            from data_pipeline import processor
            ctx = pipeline.processing_context()
            if args.command == "processor":
                processor.serve(ctx, interval_seconds=int(os.getenv("PROCESSOR_INTERVAL_SECONDS", "60")), stop=stop)
            elif args.command == "process":
                ctx.qdrant.ensure_indexes()
                print(json.dumps(processor.process_pending(ctx, limit=1000)))
            else:
                print(json.dumps(processor.refresh(ctx)))
    except Exception:  # startup/config/AWS errors: one readable line with where it failed + traceback
        log.exception("%s %s failed", service, args.command)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
