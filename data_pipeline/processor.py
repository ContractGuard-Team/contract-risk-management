"""Data Router + Processing: bytes from the collector -> extract/OCR -> clean -> split Điều -> validate -> route
-> Supabase + Qdrant. Never reads SQS or S3 and writes no files: the collector hands over the downloaded bytes;
a job that failed is retried by the processor service, which downloads the files again from the source web site.
Data rules:
- same document, new file version: the new version replaces the old one (old articles closed, old vectors removed);
- a law that declares an older one "hết hiệu lực": the old one becomes `replaced` once the new one is in force;
- an amending law: certain instructions are applied to the base law article by article, dated by the amending law's
  effective date (future dates wait as `scheduled`); uncertain ones are `needs_review` and the article is flagged.
"""
from datetime import date, datetime, timedelta, timezone
import json
import logging
import time

from data_pipeline.ingestion.chunker import embedding_parts, split_articles
from data_pipeline.processing.deduplicator import sha256
from data_pipeline.processing.normalizer import clean_pages
from data_pipeline.processing.validator import validate
from data_pipeline.telemetry import elapsed_ms, report, run_trace
from data_pipeline.router.data_router import Unresolvable, apply_instruction, legal_references, parse_amendment, replaced_laws

log = logging.getLogger("data_pipeline.processor")
ARTICLE_FIELDS = ("article_no", "heading", "chapter", "position", "page_from", "text")


def _today(ctx):
    return ctx.today() if getattr(ctx, "today", None) else date.today().isoformat()


def index(ctx, document, rows, amended_by=None):
    parts = [(i, p, text) for i, row in enumerate(rows) for p, text in enumerate(embedding_parts(row))]
    if not parts:
        return 0
    return ctx.qdrant.upsert(document, rows, parts, ctx.embedder.embed([text for *_, text in parts]), amended_by=amended_by)


def _target(store, op, exclude_id):
    if op.get("target_symbol"):
        found = store.document_by_symbol(op["target_symbol"])
        if found and found["id"] != exclude_id:
            return found
    if not op.get("target_title"):
        return None
    candidates = [d for d in store.documents_titled(op["target_title"])
                  if d["id"] != exclude_id and d["doc_class"] != "amendment" and d["status"] != "replaced"]
    return max(candidates, key=lambda d: d.get("issued_on") or "", default=None)


def _flag(ctx, target, article_no):
    for article in ctx.store.current_articles(target["id"]):
        if article["article_no"] == article_no:
            ctx.store.flag_article(article["id"], has_pending_amendment=True)
            ctx.qdrant.flag_pending(target["symbol"], article_no)


def apply_amendment(ctx, target, op, source, effective_on):
    current = {a["article_no"]: a for a in ctx.store.current_articles(target["id"])}
    closed, new = apply_instruction(current, op)  # raises Unresolvable before anything is written
    # A pending (unapplied) amendment of this article must stay visible on its new version.
    pending = {a["article_no"] for a in closed if a.get("has_pending_amendment")}
    retired, rows = [], []
    try:
        for article in closed:
            ctx.store.close_article(article["id"], effective_on)
            retired.append(article)
            ctx.qdrant.retire(target["symbol"], article["article_no"], effective_on)
        rows = ctx.store.add_articles([{**{k: n.get(k) for k in ARTICLE_FIELDS}, "document_id": target["id"],
                                        "version": target.get("current_version") or 1, "valid_from": effective_on,
                                        "is_current": True, "amended_by": source["id"],
                                        "has_pending_amendment": n["article_no"] in pending,
                                        "legal_references": legal_references(n["text"], target["symbol"])} for n in new])
        index(ctx, target, rows, amended_by=source["symbol"])
    except Exception:
        _undo_amendment(ctx, target, retired, rows)
        raise


def _undo_amendment(ctx, target, retired, written):
    """Supabase and Qdrant share no transaction: put back what one failed amendment changed, so the article keeps
    its current version and the retry starts from the same state (no "target article not found")."""
    try:
        for row in written:  # [valid_from, valid_from): a version that was never in force
            ctx.store.close_article(row["id"], row["valid_from"])
        ctx.qdrant.drop_versions(target["symbol"], written)
        for article in retired:
            ctx.store.reopen_article(article["id"])
        ctx.qdrant.reopen_versions(target["symbol"], retired)
    except Exception as exc:  # the caller re-raises the original error; a person must check this article
        log.error("could not undo the amendment of %s (%s): check its current articles", target["symbol"],
                  type(exc).__name__)


def _replace(ctx, new_document, old):
    ctx.store.update_document(old["id"], status="replaced", replaced_by=new_document["id"])
    # replaced_on lets rag/ answer "which text applied on date X" after the replacement.
    ctx.qdrant.set_status(old["symbol"], "replaced",
                          replaced_on=new_document.get("effective_on") or new_document.get("issued_on"))


def _settle(ctx, document, target, op, effective_on):
    """(status, reason) for one amending instruction; applies it when certain and due."""
    if target is None:
        return "unresolved", "base law not in the store"
    if not op["auto"]:
        if op["article"]:
            _flag(ctx, target, op["article"])
        return "needs_review", f"not automatically applicable: {op['operation']}"
    if effective_on > _today(ctx):
        return "scheduled", None
    try:
        apply_amendment(ctx, target, op, document, effective_on)
        return "applied", None
    except Unresolvable as exc:
        if op["article"]:
            _flag(ctx, target, op["article"])
        return "needs_review", str(exc)


def _op(relation):
    return {"operation": relation["operation"], "article": relation["target_article"], "clause": relation["target_clause"],
            "after_article": relation.get("after_article"), "content": relation.get("content") or "",
            "instruction": relation.get("instruction") or "", "auto": bool(relation.get("auto")),
            "target_symbol": relation.get("target_symbol"), "target_title": relation.get("target_title")}


def handle_amendment(ctx, document, op, effective_on):
    target = _target(ctx.store, op, document["id"])
    status, reason = _settle(ctx, document, target, op, effective_on)
    ctx.store.add_relation({
        "source_document_id": document["id"], "target_document_id": target["id"] if target else None,
        "target_symbol": op.get("target_symbol") or (target or {}).get("symbol"), "target_title": op.get("target_title"),
        "relation": "amends", "operation": op["operation"], "target_article": op["article"],
        "target_clause": op["clause"], "after_article": op.get("after_article"), "instruction": op["instruction"],
        "content": op["content"], "auto": op["auto"], "effective_on": effective_on, "status": status,
        "review_reason": reason})
    return status


def retry_unresolved(ctx):
    """Amendments are often collected before their base law (newest first): resolve them once it exists."""
    settled = 0
    for relation in ctx.store.relations("unresolved"):
        source = ctx.store._one("legal_documents", id=relation["source_document_id"])
        op = _op(relation)
        if relation["relation"] == "replaces":
            target = ctx.store.document_by_symbol(relation["target_symbol"])
            if target is None:
                continue
            status = "scheduled" if relation["effective_on"] > _today(ctx) else "applied"
            if status == "applied":
                _replace(ctx, source, target)
            reason = None
        else:
            target = _target(ctx.store, op, source["id"])
            if target is None:
                continue
            status, reason = _settle(ctx, source, target, op, relation["effective_on"])
        ctx.store.update_relation(relation["id"], target_document_id=target["id"], status=status, review_reason=reason)
        settled += 1
    return settled


def _latest_constitution_wins(ctx, document):
    """A newer Hiến pháp replaces every older one (the text does not phrase it as "… hết hiệu lực")."""
    constitutions = [d for d in ctx.store.select("legal_documents", {"doc_class": "constitution"}) if d.get("issued_on")]
    newest = max(constitutions, key=lambda d: d["issued_on"], default=document)
    for old in constitutions:
        if old["id"] != newest["id"] and old["status"] != "replaced":
            _replace(ctx, newest, old)


def handle_replacement(ctx, document, title, symbol, effective_on):
    old = ctx.store.document_by_symbol(symbol)
    if old and old["id"] == document["id"]:
        return None
    status = "unresolved" if old is None else ("scheduled" if effective_on > _today(ctx) else "applied")
    if status == "applied":
        _replace(ctx, document, old)
    ctx.store.add_relation({"source_document_id": document["id"], "target_document_id": old["id"] if old else None,
                            "target_symbol": symbol, "target_title": title, "relation": "replaces",
                            "operation": "replace_law", "instruction": f"{title} số {symbol} hết hiệu lực",
                            "effective_on": effective_on, "status": status})
    return status


def process_document(ctx, document, version, files):
    """files: [(filename, bytes)] handed over by the collector. Results go to Supabase and Qdrant only."""
    store, today = ctx.store, _today(ctx)
    extraction = ctx.extract(files)
    amendment = document["doc_class"] == "amendment"
    preamble, articles = split_articles(clean_pages(extraction["pages"]), amendment=amendment)
    errors = validate(document, preamble, articles)
    # Text before Điều 1, extraction warnings and validation result are kept on the version row (no file output).
    store.update_version(version["id"], extraction_method=extraction["method"], preamble=preamble,
                         warnings=extraction["warnings"], validation_errors=errors)
    if errors:  # broken OCR never reaches agents
        store.update_version(version["id"], state="needs_review")
        return "needs_review", errors
    valid_from = document.get("effective_on") or document.get("issued_on") or today
    keep = document["status"] in ("replaced", "expired")
    status = document["status"] if keep else ("in_force" if valid_from <= today else "pending")
    document = {**document, "status": status, "current_version": version["version"]}
    # The new version replaces the old text entirely, but only once it is stored AND indexed: until then the old
    # articles and vectors stay current, so a failure never leaves the law without text.
    previous = store.current_articles(document["id"])
    rows = []
    try:
        rows = store.add_articles([{**{k: a.get(k) for k in ARTICLE_FIELDS}, "document_id": document["id"],
                                    "version": version["version"], "valid_from": valid_from, "is_current": True,
                                    "legal_references": legal_references(a["text"], document["symbol"])} for a in articles])
        index(ctx, document, rows)
    except Exception:
        for row in rows:  # [valid_from, valid_from): never in force; the old text stays current until the retry
            store.close_article(row["id"], row["valid_from"])
        raise
    for old in previous:
        store.close_article(old["id"], today)
    ctx.qdrant.drop_versions(document["symbol"], previous, keep=rows)  # points the new version did not overwrite
    for done in store.select("legal_document_versions", {"document_id": document["id"], "state": "processed"}):
        store.update_version(done["id"], state="superseded")
    store.update_document(document["id"], status=status, current_version=version["version"])
    if document["doc_class"] == "constitution":
        _latest_constitution_wins(ctx, document)
    elif amendment:
        for op in parse_amendment(articles, document["title"], preamble):
            handle_amendment(ctx, document, op, valid_from)
    else:
        # "… hết hiệu lực kể từ ngày Luật này có hiệu lực" sits in the final articles.
        for title, symbol in replaced_laws("\n".join(a["text"] for a in articles[-5:])):
            handle_replacement(ctx, document, title, symbol, valid_from)
    store.update_version(version["id"], state="processed")
    return "done", []


def process_files(ctx, job, files, max_attempts=3):
    """Collector branch 2: process the in-memory files of one ingest job and record the outcome on the job."""
    attempts, start, name = (job.get("attempts") or 0) + 1, time.monotonic(), job["docid"]
    ctx.store.update_job(job["id"], state="processing", attempts=attempts)
    try:
        document = ctx.store.document_by_docid(job["docid"])
        name = document["symbol"]
        version = ctx.store.version_by_sha(job["sha256"])
        state, errors = process_document(ctx, document, version, files)
        ctx.store.update_job(job["id"], state=state, error_type=";".join(errors)[:500] or None)
        report("process", state, f"{name} v{version['version']}" + (f": {'; '.join(errors)}" if errors else
               " -> Supabase + Qdrant"), level=logging.WARNING if errors else logging.INFO, document=name,
               duration_ms=elapsed_ms(start))
    except Exception as exc:  # the crawl run continues; the processor service retries from the web
        state = "pending" if attempts < max_attempts else "failed"
        ctx.store.update_job(job["id"], state=state, error_type=type(exc).__name__)
        report("process", "retry" if state == "pending" else "failed",
               f"{name} attempt {attempts}/{max_attempts}: {exc}", level=logging.ERROR, exc_info=exc, document=name,
               duration_ms=elapsed_ms(start))
    return state


def process_job(ctx, job):
    """Retry path: fetch the files again from the source web site (never from S3) and process them."""
    version = ctx.store.version_by_sha(job["sha256"])
    files = ctx.fetch(version["source_urls"])
    if [sha256(data) for _, data in files] != version["file_hashes"]:
        # The site now serves other bytes: processing them under this version would make Supabase/Qdrant
        # disagree with the original kept on S3. The collector stores the new file as a new version.
        ctx.store.update_job(job["id"], state="failed", error_type="SourceChanged")
        report("process", "failed", f"{job['docid']}: source file changed since collection; the next crawl "
               "collects it as a new version", level=logging.WARNING)
        return "failed"
    return process_files(ctx, job, files)


def process_pending(ctx, limit=20):
    counts = {}
    for job in ctx.store.pending_jobs(limit):
        state = process_job(ctx, job)
        counts[state] = counts.get(state, 0) + 1
    return counts


def refresh(ctx):
    """Daily: documents reaching their effective date go in force; scheduled amendments/replacements fall due."""
    store, today, done = ctx.store, _today(ctx), {"in_force": 0, "applied": 0, "needs_review": 0}
    for document in store.select("legal_documents", {"status": "pending"}):
        if (document.get("effective_on") or document.get("issued_on") or "9999") <= today:
            store.update_document(document["id"], status="in_force")
            ctx.qdrant.set_status(document["symbol"], "in_force")
            done["in_force"] += 1
    for relation in store.relations("scheduled"):
        if (relation.get("effective_on") or "9999") > today:
            continue
        source = store._one("legal_documents", id=relation["source_document_id"])
        target = store._one("legal_documents", id=relation["target_document_id"])
        try:
            if relation["relation"] == "replaces":
                _replace(ctx, source, target)
            else:
                apply_amendment(ctx, target, _op(relation), source, relation["effective_on"])
            store.update_relation(relation["id"], status="applied")
            done["applied"] += 1
        except Unresolvable as exc:
            store.update_relation(relation["id"], status="needs_review", review_reason=str(exc))
            done["needs_review"] += 1
    done["resolved"] = retry_unresolved(ctx)
    return done


def requeue_stale(ctx, hours=3):
    """A job left in "processing" by a crash goes back to pending; live ones (collector working) are untouched."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    stale = [job for job in ctx.store.select("ingest_jobs", {"state": "processing"}) if (job.get("updated_at") or "") < cutoff]
    for job in stale:
        ctx.store.update_job(job["id"], state="pending")
    if stale:
        report("requeue", "completed", f"{len(stale)} job(s) stuck in processing > {hours} h set back to pending",
               level=logging.WARNING, count=len(stale))


def serve(ctx, *, interval_seconds=60, stop=None):
    """Retry jobs the collector could not finish (re-downloading from the web) and run refresh once per day."""
    import threading
    stop = stop or threading.Event()
    ctx.qdrant.ensure_indexes()
    report("service", "started", f"processor polling every {interval_seconds} s")
    refreshed_on = None
    while not stop.is_set():
        with run_trace():
            try:
                requeue_stale(ctx)
                if refreshed_on != _today(ctx):
                    done = refresh(ctx)
                    report("refresh", "completed", json.dumps(done), count=sum(done.values()))
                    refreshed_on = _today(ctx)
                counts = process_pending(ctx)
                if counts:
                    report("retry_cycle", "completed", f"processed {counts}, resolved relations "
                           f"{retry_unresolved(ctx)}", count=sum(counts.values()))
            except Exception as exc:  # Supabase/Qdrant outage: log, wait, try again; systemd restarts on crash
                report("cycle", "failed", str(exc), level=logging.ERROR, exc_info=exc)
                counts = None
        if not counts:
            stop.wait(interval_seconds)
    report("service", "stopped", "processor stopped")
