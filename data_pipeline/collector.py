"""Data Recollector: the only SQS consumer. EventBridge Scheduler -> SQS contract-data-update-queue -> one crawl run.

For every new or changed document the downloaded bytes go two ways at once:
  1. web -> collector -> S3: the original files only (by law number, never overwritten);
  2. web -> collector -> processor: the same bytes in memory -> text -> Supabase + Qdrant. Nothing is written locally.
The SQS message is deleted only after the run is recorded; a crash leaves it for redelivery (DLQ via redrive policy).
"""
import asyncio
from collections import Counter
from datetime import date, datetime, timezone
import json
import logging
import threading
import time
from uuid import uuid4

from data_pipeline.ingestion.s3_archiver import law_prefix, safe_name
from data_pipeline.processing.deduplicator import decide, row_fingerprint, sha256, version_hash
from data_pipeline.router.source_classifier import classify, document_symbol, maybe_kept
from data_pipeline.telemetry import elapsed_ms, report, run_trace

log = logging.getLogger("data_pipeline.collector")
MODES = ("incremental", "full")


async def _collect_one(row, known, *, source, store, archiver, root, today, process, processing):
    start = time.monotonic()
    detail = await source.detail(row)
    symbol, title = detail["symbol"] or row["symbol"], detail["title"] or row["title"]
    doc_class, reason = classify(detail["doc_type"], title, symbol)
    if not doc_class:
        log.debug("skip %s (%s)", row["docid"], reason)
        return "skipped"
    symbol = document_symbol(symbol, doc_class, title, detail["issued_on"] or row["issued_on"])
    urls = detail["files"] or row["files"]  # every attachment format (.pdf, .doc, .rtf, .docx, …)
    if not urls:
        report("document", "no_file", f"docid {row['docid']} has no attachment: {title[:80]}", level=logging.WARNING,
               document=symbol)
        return "no_file"
    datas = [await source.download(url) for url in urls]
    hashes = [sha256(data) for data in datas]
    content_hash = version_hash(hashes)
    effective = detail["effective_on"] or detail["issued_on"] or row["issued_on"]
    document = {"docid": row["docid"], "symbol": symbol, "doc_class": doc_class, "doc_type": detail["doc_type"],
                "title": title, "issuer": detail["issuer"], "signer": detail["signer"],
                "issued_on": detail["issued_on"] or row["issued_on"], "effective_on": detail["effective_on"],
                "source_url": row["detail_url"], "list_fingerprint": row_fingerprint(row), "missing_since": None,
                "s3_prefix": law_prefix(symbol, doc_class, detail["issued_on"] or row["issued_on"], row["docid"], root)}
    same = store.version_by_sha(content_hash)
    if same:
        if known and same["document_id"] == known["id"]:
            store.save_document({**document, "status": known["status"]})  # metadata/fingerprint refresh only
            # A run that stopped between add_version and add_job left this version without a job: queue it now
            # (add_job keeps an existing job as it is, so a processed version is never reprocessed).
            store.add_job({"docid": row["docid"], "sha256": content_hash, "version": same["version"],
                           "payload": {"document_id": known["id"], "version_id": same["id"]}})
            return "unchanged"
        return "duplicate"  # identical file already stored under another docid
    keep = known and known["status"] in ("replaced", "expired")  # history decided by newer laws stays
    document["status"] = known["status"] if keep else ("in_force" if effective and effective <= today else "pending")
    saved = store.save_document(document)
    latest = store.latest_version(saved["id"])
    number = (latest["version"] if latest else 0) + 1
    names = [safe_name(url) for url in urls]
    # Branch 1: originals to S3.
    keys = [archiver.put_source(document["s3_prefix"], number, h, name, data)
            for name, h, data in zip(names, hashes, datas)]
    report("document", "collected", f"{symbol} v{number} ({'changed' if known else 'new'}): {len(keys)} file -> S3 "
           f"{document['s3_prefix']}", document=symbol, count=len(keys), duration_ms=elapsed_ms(start))
    version = store.add_version({"document_id": saved["id"], "version": number, "sha256": content_hash,
                                 "s3_keys": keys, "file_hashes": hashes, "source_urls": urls, "state": "collected"})
    # Created as "processing" when processed right here, so the processor service never picks it up twice.
    job = store.add_job({"docid": row["docid"], "sha256": content_hash, "version": number,
                         "payload": {"document_id": saved["id"], "version_id": version["id"]}},
                        state="processing" if process else "pending")
    # Branch 2: the same bytes, in memory, to the processor. OCR is CPU-bound: one document at a time.
    if process is not None:
        async with processing:
            await asyncio.to_thread(process, job, list(zip(names, datas)))
    return "changed" if known else "new"


async def collect(mode, *, source, store, archiver, root="legal", today=None, limit=None, process=None):
    """process(job, [(filename, bytes)]) -> processing state; None only queues the job (processor retries it)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    with run_trace():
        start = time.monotonic()
        report("run", "started", f"mode={mode}" + (f" limit={limit}" if limit is not None else ""))
        try:
            run = await _collect(mode, source=source, store=store, archiver=archiver, root=root, today=today,
                                 limit=limit, process=process)
        except Exception as exc:
            report("run", "failed", f"crawl run stopped: {exc}", level=logging.ERROR, exc_info=exc,
                   duration_ms=elapsed_ms(start))
            raise
        report("run", "completed" if run["status"] == "ok" else "incomplete",
               "new={new} changed={changed} unchanged={unchanged} skipped={skipped} failed={failed} missing={missing}"
               .format(**run), level=logging.INFO if run["status"] == "ok" else logging.WARNING,
               count=run["new"] + run["changed"], duration_ms=elapsed_ms(start))
        return run


async def _collect(mode, *, source, store, archiver, root, today, limit, process):
    today = today or date.today().isoformat()
    started = datetime.now(timezone.utc)
    full = mode == "full"
    processing = asyncio.Semaphore(1)

    def settled(row):
        return not maybe_kept(row["title"]) or decide(store.document_by_docid(row["docid"]), row, full=False) == "unchanged"
    # Incremental runs stop at the first page whose rows are all known and unchanged (newest documents come first).
    listed = time.monotonic()
    rows, listing = await source.list_documents(stop_when=None if full else (lambda page: all(map(settled, page["rows"]))),
                                                sweep_years=full)
    report("listing", "completed", f"{listing['seen']} documents on {listing['pages']} pages "
           f"(site total {listing['total_on_site']}, missing {listing['missing']})", count=listing["seen"],
           duration_ms=elapsed_ms(listed))
    counts, work, targets = Counter(), [], []
    for row in rows.values():
        if not maybe_kept(row["title"]):
            counts["skipped"] += 1
            continue
        known = store.document_by_docid(row["docid"])
        if decide(known, row, full=full) == "unchanged":
            counts["unchanged"] += 1
        elif limit is not None and len(work) >= limit:  # backfill cap: the rest waits for the next run
            counts["deferred"] += 1
        else:
            work.append(_collect_one(row, known, source=source, store=store, archiver=archiver, root=root, today=today,
                                     process=process, processing=processing))
            targets.append(row)
    log.info("to collect: %d new/changed, %d unchanged, %d skipped by title, %d deferred (limit)",
             len(work), counts["unchanged"], counts["skipped"], counts["deferred"])
    for row, result in zip(targets, await asyncio.gather(*work, return_exceptions=True)):
        if isinstance(result, Exception):
            report("document", "failed", f"docid {row['docid']} {row['symbol']}: {result}", level=logging.ERROR,
                   exc_info=result, document=row["symbol"] or row["docid"])
        counts[result if isinstance(result, str) else "failed"] += 1
    if full and listing["missing"] == 0:  # complete listing: documents gone from the site are flagged, never deleted
        for document in store.select("legal_documents", {}):
            if document["docid"] not in rows and not document.get("missing_since"):
                store.update_document(document["id"], missing_since=started.isoformat())
    run = {"run_id": f"{started:%Y%m%dT%H%M%SZ}-{uuid4().hex[:6]}", "mode": mode, "started_at": started.isoformat(),
           "finished_at": datetime.now(timezone.utc).isoformat(), "pages": listing["pages"], "seen": listing["seen"],
           "total_on_site": listing["total_on_site"], "missing": listing["missing"],
           **{k: counts[k] for k in ("new", "changed", "unchanged", "skipped", "failed")},
           "status": "incomplete" if counts["failed"] or listing["missing"] else "ok"}
    store.save_run(run)  # run log lives in Supabase crawl_runs, not on S3
    return run


def message_mode(body):
    """EventBridge Scheduler input {"mode": "..."}; a bare scheduled event means an incremental run."""
    try:
        value = json.loads(body or "{}")
    except json.JSONDecodeError:
        raise ValueError("SQS message body is not JSON") from None
    mode = (value.get("mode") or (value.get("detail") or {}).get("mode") or "incremental") if isinstance(value, dict) else None
    if mode not in MODES:
        raise ValueError(f"Unsupported mode: {mode!r}")
    return mode


def serve(queue_url, run, *, sqs, wait_seconds=20, visibility_seconds=900, stop=None):
    """Long-poll SQS; keep the message invisible while a run lasts; delete it only after run(mode) returns."""
    stop = stop or threading.Event()
    while not stop.is_set():
        messages = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=wait_seconds,
                                       VisibilityTimeout=visibility_seconds).get("Messages", [])
        for message in messages:
            receipt, done = message["ReceiptHandle"], threading.Event()

            def heartbeat():
                while not done.wait(visibility_seconds / 3):
                    sqs.change_message_visibility(QueueUrl=queue_url, ReceiptHandle=receipt,
                                                  VisibilityTimeout=visibility_seconds)
            keeper = threading.Thread(target=heartbeat, daemon=True)
            keeper.start()
            try:
                mode = message_mode(message.get("Body"))
                report("sqs_message", "received", f"mode={mode} message={message.get('MessageId', '?')}")
                result = run(mode)
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
                report("sqs_message", "deleted", f"run {result.get('run_id')} recorded: {json.dumps(result, default=str)}")
            except Exception as exc:  # message stays; SQS redelivers, then moves it to the DLQ
                report("sqs_message", "kept", f"run failed, message kept for redelivery: {exc}", level=logging.ERROR,
                       exc_info=exc)
            finally:
                done.set()
                keeper.join(timeout=5)
