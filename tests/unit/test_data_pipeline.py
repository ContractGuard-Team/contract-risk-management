import asyncio
import hashlib
import json
import os
from pathlib import Path
from functools import partial
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import boto3
import httpx
from moto import mock_aws
from qdrant_client import QdrantClient, models

from data_pipeline.collector import collect, message_mode, serve
from data_pipeline.ingestion.chunker import split_articles
from data_pipeline.ingestion.postgres_writer import Store
from data_pipeline.ingestion.qdrant_writer import QdrantWriter
from data_pipeline.ingestion.s3_archiver import S3Archiver, law_prefix, safe_name
from data_pipeline.processor import process_files, process_pending, refresh, retry_unresolved
from data_pipeline.router.data_router import apply_instruction, parse_amendment
from data_pipeline.router.source_classifier import classify
from data_pipeline.sources.legal_source import LegalSource, parse_detail, parse_list

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "data_pipeline"
LIST_URL = "https://vanban.chinhphu.vn/he-thong-van-ban?classid=1&mode=1&typegroupid=3"
BUCKET = "contract-risk-database-unique-333596350642-ap-southeast-1-an"


class MemoryStore(Store):
    """The four Store primitives over Python lists, matching PostgREST eq/order/limit semantics."""
    def __init__(self):
        self.tables, self.next_id = {}, 1

    def select(self, table, filters=None, *, order=None, limit=None):
        rows = [dict(r) for r in self.tables.get(table, []) if all(r.get(k) == v for k, v in (filters or {}).items())]
        if order:
            rows.sort(key=lambda r: (r.get(order[0]) is None, r.get(order[0])), reverse=order[1])
        return rows[:limit] if limit else rows

    def insert(self, table, rows):
        out = []
        for row in rows:
            stored = {"id": self.next_id, **row}
            self.next_id += 1
            self.tables.setdefault(table, []).append(stored)
            out.append(dict(stored))
        return out

    def update(self, table, filters, fields):
        for row in self.tables.get(table, []):
            if all(row.get(k) == v for k, v in filters.items()):
                row.update(fields)

    def upsert(self, table, row, on_conflict):
        keys = on_conflict.split(",")
        for existing in self.tables.get(table, []):
            if all(existing.get(k) == row.get(k) for k in keys):
                existing.update(row)
                return dict(existing)
        return self.insert(table, [row])[0]


def page_html(rows, *, current, has_next, total):
    body = "".join(
        f'<tr><td><a href="/?pageid=27160&amp;docid={d}"><span class="code">{s}</span></a></td>'
        f'<td><span class="issued-date">01/01/2025</span></td><td><a href="/?pageid=27160&amp;docid={d}">'
        f'<span class="substract">{t}</span></a><div class="bl-doc-files"><div class="bl-doc-file">'
        f'<a href="https://datafiles.chinhphu.vn/{d}.pdf">f</a></div></div></td></tr>' for d, s, t in rows)
    pager = f"<td><span>{current}</span></td>" + (
        f"<td><a href=\"javascript:__doPostBack('ctrl_1$grvDocument','Page${current + 1}')\">x</a></td>" if has_next else "")
    return (f'<form><input type="hidden" name="__VIEWSTATE" value="vs{current}"/><table id="ctrl_1_grvDocument">'
            f'<tr><th>a</th><th>b</th><th>c</th></tr>{body}<tr>{pager}</tr></table>'
            f'<div class="document_page_info">1 - 50 | {total}</div></form>')


class SourceTests(unittest.TestCase):
    def test_real_list_and_detail_pages(self):
        page = parse_list((FIXTURES / "list_page1.html").read_text(encoding="utf-8"), LIST_URL)
        self.assertEqual((len(page["rows"]), page["total"], page["next"], page["event_target"]),
                         (50, 865, "Page$2", "ctrl_191017_163$grvDocument"))
        self.assertEqual(page["rows"][0]["symbol"], "24/2026/QH16")
        self.assertIn("__VIEWSTATE", page["form"])
        detail = parse_detail((FIXTURES / "detail_page.html").read_text(encoding="utf-8"), LIST_URL)
        self.assertEqual((detail["doc_type"], detail["effective_on"], len(detail["files"])), ("Luật", "2027-03-01", 1))

    def test_postback_paging_deduplicates_unstable_order(self):
        page1 = [(str(i), f"{i}/2025/QH15", f"Luật số {i}") for i in range(1, 51)]
        page2 = page1[35:] + [(str(i), f"{i}/2025/QH15", f"Luật số {i}") for i in range(51, 86)]  # 15 repeated
        posted = []

        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, text=page_html(page1, current=1, has_next=True, total=85))
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            posted.append(form)
            return httpx.Response(200, text=page_html(page2, current=2, has_next=False, total=85))
        source = LegalSource(LIST_URL, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), delay=0)
        rows, report = asyncio.run(source.list_documents(sweep_years=False))
        self.assertEqual((len(rows), report["missing"], report["pages"]), (85, 0, 2))
        self.assertEqual((posted[0]["__EVENTARGUMENT"], posted[0]["__VIEWSTATE"]), ("Page%242", "vs1"))

    def test_type_filter(self):
        self.assertEqual(classify("Luật", "Luật sửa đổi, bổ sung một số điều của Luật Đầu tư", "24/2026/QH16")[0], "amendment")
        self.assertEqual(classify("Bộ luật", "Bộ luật Dân sự", "91/2015/QH13")[0], "code")
        self.assertEqual(classify("Hiến pháp", "Hiến pháp nước Cộng hòa xã hội chủ nghĩa Việt Nam", "")[0], "constitution")
        self.assertIsNone(classify("Pháp lệnh", "Pháp lệnh Chi phí tố tụng", "02/2024/UBTVQH15")[0])
        self.assertEqual(law_prefix("42/2024/QH15", "law", "2024-06-01", "1"), "legal/QH15/2024/042-2024-QH15")


BASE_LAW = """Luật số: 61/2020/QH14
LUẬT ĐẦU TƯ
Chương I
Điều 1. Phạm vi điều chỉnh
Luật này quy định về hoạt động đầu tư.
_ Điều 2. Đối tượng áp dụng
Luật này áp dụng đối với nhà đầu tư; xem “Điều 1 của Luật này
Điêu 3. Giải thích từ ngữ
1. Nhà đầu tư là tổ chức, cá nhân.
Điều 1 của Luật này được hiểu như sau.
Điều 4. Áp dụng
1. Khoản một.
2. Khoản hai.
3. Khoản ba.
Điều 5. Hiệu lực
Luật này có hiệu lực từ ngày 01 tháng 01 năm 2021."""

AMENDMENT = """Luật số: 90/2025/QH15
LUẬT SỬA ĐỔI, BỔ SUNG MỘT SỐ ĐIỀU CỦA LUẬT ĐẦU TƯ
Quốc hội ban hành Luật sửa đổi, bổ sung một số điều của Luật Đầu tư số 61/2020/QH14.
Điều 1. Sửa đối, bỗ sung một số điều của Luật Đầu tư
I. Bổ sung một số điểm, khoản của Điều 4 như sau:
a) Bồ sung điểm d vào sau điểm c khoản I như sau:
“d) Điểm mới.”;
b) Bồ sung khoản 2a vào sau khoản 2 như sau:
“2a. Khoản hai a.”.
2. Sửa đổi, bổ sung Điều 1 như sau:
“Điều 1. Phạm vi điều chỉnh
Luật này quy định về đầu tư kinh doanh.”
3. Bãi bỏ khoản 3 Điều 4.
4. Thay cụm từ “nhà đầu tư” bằng cụm từ “chủ đầu tư” tại Điều 2.
Điều 2. Hiệu lực thi hành
Luật này có hiệu lực từ ngày 01 tháng 7 năm 2025."""


def lines(text):
    return [(1, line) for line in text.splitlines()]


class TextTests(unittest.TestCase):
    def test_ocr_tolerant_article_split(self):
        _, articles = split_articles(lines(BASE_LAW))
        self.assertEqual([a["article_no"] for a in articles], ["1", "2", "3", "4", "5"])  # "_ Điều", "Điêu", cross-ref
        self.assertIn("Điều 1 của Luật này được hiểu", articles[2]["text"])  # backward reference stays in Điều 3
        self.assertEqual(articles[0]["chapter"], "Chương I")

    def test_amendment_instructions_and_application(self):
        preamble, articles = split_articles(lines(AMENDMENT), amendment=True)
        ops = parse_amendment(articles, "Luật sửa đổi, bổ sung một số điều của Luật Đầu tư", preamble)
        summary = [(o["operation"], o["article"], o["clause"], o["auto"]) for o in ops]
        self.assertEqual(summary, [("insert", "4", "1", False), ("insert", "4", "2a", True), ("replace", "1", None, True),
                                   ("repeal", "4", "3", True), ("rephrase", None, None, False)])
        self.assertEqual({o["target_symbol"] for o in ops}, {"61/2020/QH14"})
        _, base = split_articles(lines(BASE_LAW))
        current = {a["article_no"]: a for a in base}
        _, new = apply_instruction(current, ops[1])
        self.assertIn("2. Khoản hai.\n2a. Khoản hai a.\n3. Khoản ba.", new[0]["text"])
        _, new = apply_instruction(current, ops[3])
        self.assertNotIn("Khoản ba", new[0]["text"])


class FakeEmbedder:
    def embed(self, texts):
        return [[b / 255 + 0.01 for b in hashlib.sha256(t.encode()).digest()[:8]] for t in texts]


class FakeSource:
    def __init__(self, docs):
        self.docs = docs  # docid -> (row, detail, pdf bytes)

    async def list_documents(self, *, stop_when=None, sweep_years=True):
        rows = {d: v[0] for d, v in self.docs.items()}
        page = {"rows": list(rows.values())}
        stopped = stop_when is not None and stop_when(page)
        return rows, {"pages": 1, "seen": len(rows), "total_on_site": len(rows), "missing": None if stop_when else 0,
                      "stopped": stopped}

    async def detail(self, row):
        return self.docs[row["docid"]][1]

    async def download(self, url):
        return next(v[2] for v in self.docs.values() if url in v[1]["files"])


def doc(docid, symbol, title, doc_type, effective, body, issued="2020-06-17"):
    url = f"https://datafiles.chinhphu.vn/{docid}.pdf"
    row = {"docid": docid, "symbol": symbol, "issued_on": issued, "title": title,
           "detail_url": f"https://vanban.chinhphu.vn/?docid={docid}", "files": [url]}
    detail = {"symbol": symbol, "issued_on": issued, "effective_on": effective, "doc_type": doc_type,
              "issuer": "Quốc hội", "signer": "", "title": title, "files": [url]}
    return docid, (row, detail, b"%PDF-1.7\n" + body.encode())


@mock_aws
class PipelineTests(unittest.TestCase):
    def setUp(self):
        os.environ.update(AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing", AWS_DEFAULT_REGION="ap-southeast-1")
        self.s3 = boto3.client("s3", region_name="ap-southeast-1")
        self.s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-southeast-1"})
        self.store, self.archiver = MemoryStore(), S3Archiver(BUCKET, client=self.s3)
        qdrant = QdrantClient(":memory:")
        self.qdrant = QdrantWriter(client=qdrant, collection="legal_documents", dimensions=8)
        self.qdrant.ensure_indexes()  # creates the dense + sparse collection
        self.today = "2026-01-01"
        self.ctx = SimpleNamespace(store=self.store, embedder=FakeEmbedder(), qdrant=self.qdrant,
                                   today=lambda: self.today,
                                   extract=lambda files: {"pages": [{"page": 1, "method": "pdf_text",
                                                                     "text": files[0][1].decode()[9:]}],
                                                          "method": "text", "warnings": []})

    def collect(self, docs, mode, process=None):
        # Default = production wiring: the collector hands the downloaded bytes straight to the processor.
        return asyncio.run(collect(mode, source=FakeSource(dict(docs)), store=self.store, archiver=self.archiver,
                                   today=self.today, process=process or partial(process_files, self.ctx)))

    def job_states(self):
        return sorted(j["state"] for j in self.store.select("ingest_jobs", {}))

    def points(self, **must):
        flt = models.Filter(must=[models.FieldCondition(key=k, match=models.MatchValue(value=v)) for k, v in must.items()])
        return self.qdrant.client.scroll("legal_documents", scroll_filter=flt, limit=100)[0]

    def test_collect_archives_by_law_number_and_is_idempotent(self):
        base = doc("1", "61/2020/QH14", "Luật Đầu tư", "Luật", "2021-01-01", BASE_LAW)
        skip = doc("2", "02/2024/UBTVQH15", "Pháp lệnh Chi phí", "Pháp lệnh", "2025-01-01", "x")
        run = self.collect([base, skip], "full")
        self.assertEqual((run["new"], run["skipped"], run["status"]), (1, 1, "ok"))
        keys = [o["Key"] for o in self.s3.list_objects_v2(Bucket=BUCKET, Prefix="legal/QH14/")["Contents"]]
        self.assertTrue(any(k.startswith("legal/QH14/2020/061-2020-QH14/source/v1_") for k in keys), keys)
        self.assertEqual(self.job_states(), ["done"])  # processed from memory in the same run
        self.assertTrue(all(k.split("/")[-1].startswith("v1_") for k in keys))  # S3 holds originals only, no JSON
        self.assertEqual(self.collect([base], "incremental")["unchanged"], 1)  # known, same list row: no download
        changed = doc("1", "61/2020/QH14", "Luật Đầu tư", "Luật", "2021-01-01", BASE_LAW + "\nĐiều 6. Mới\nNội dung.")
        self.assertEqual(self.collect([changed], "full")["changed"], 1)
        versions = self.store.select("legal_document_versions", {}, order=("version", False))
        self.assertEqual([v["version"] for v in versions], [1, 2])
        sources = [o["Key"] for o in self.s3.list_objects_v2(Bucket=BUCKET, Prefix="legal/QH14/2020/061-2020-QH14/source/")["Contents"]]
        self.assertEqual(len(sources), 2)  # v1 original kept, never overwritten

    def test_process_rules_new_amendment_scheduled_and_out_of_order(self):
        amend = doc("9", "90/2025/QH15", "Luật sửa đổi, bổ sung một số điều của Luật Đầu tư", "Luật", "2025-07-01", AMENDMENT)
        base = doc("1", "61/2020/QH14", "Luật Đầu tư", "Luật", "2021-01-01", BASE_LAW)
        self.collect([amend], "full")  # newest first: the amendment arrives before its base law
        relations = self.store.relations("unresolved")
        self.assertEqual(len(relations), 5)
        self.collect([amend, base], "full")
        self.assertEqual(retry_unresolved(self.ctx), 5)
        statuses = sorted(r["status"] for r in self.store.select("legal_relations", {}))
        self.assertEqual(statuses, ["applied", "applied", "applied", "needs_review", "needs_review"])
        base_id = self.store.document_by_symbol("61/2020/QH14")["id"]
        article4 = [a for a in self.store.current_articles(base_id) if a["article_no"] == "4"]
        self.assertEqual(len(article4), 1)
        self.assertIn("2a. Khoản hai a.", article4[0]["text"])
        self.assertNotIn("Khoản ba", article4[0]["text"])
        self.assertTrue(article4[0]["has_pending_amendment"])  # unapplied point-level edit survives the new version
        current = self.points(symbol="61/2020/QH14", article_no="4", current=True)
        self.assertEqual(len(current), 1)
        self.assertTrue(current[0].payload["has_pending_amendment"])
        self.assertEqual(current[0].payload["amended_by"], "90/2025/QH15")
        self.assertEqual(len(self.points(symbol="61/2020/QH14", article_no="4", current=False)), 1)  # history kept
        self.assertEqual(self.points(symbol="61/2020/QH14", article_no="1", current=True)[0].payload["text"].splitlines()[1],
                         "Luật này quy định về đầu tư kinh doanh.")

    def test_future_amendment_waits_for_refresh_and_bad_text_is_held(self):
        future = AMENDMENT.replace("90/2025/QH15", "95/2026/QH15")
        base = doc("1", "61/2020/QH14", "Luật Đầu tư", "Luật", "2021-01-01", BASE_LAW)
        amend = doc("9", "95/2026/QH15", "Luật sửa đổi, bổ sung một số điều của Luật Đầu tư", "Luật", "2026-07-01", future)
        broken = doc("5", "50/2024/QH15", "Luật Hỏng", "Luật", "2024-01-01", "Luật số: 50/2024/QH15\nĐiều 1. A\nĐiều 3. C")
        self.collect([base, amend, broken], "full")
        self.assertEqual(self.job_states(), ["done", "done", "needs_review"])
        self.assertEqual(self.points(symbol="50/2024/QH15"), [])  # gap in article numbers: never indexed
        self.assertEqual(self.store.document_by_symbol("95/2026/QH15")["status"], "pending")
        self.assertEqual(len(self.store.relations("scheduled")), 3)
        self.today = "2026-07-01"
        result = refresh(self.ctx)
        self.assertEqual((result["in_force"], result["applied"]), (1, 3))
        self.assertEqual(self.points(symbol="95/2026/QH15")[0].payload["status"], "in_force")

    def test_failed_processing_is_retried_from_the_web_not_s3(self):
        base = doc("1", "61/2020/QH14", "Luật Đầu tư", "Luật", "2021-01-01", BASE_LAW)
        extract, calls = self.ctx.extract, []

        def flaky(files):
            calls.append(files)
            if len(calls) == 1:
                raise RuntimeError("OCR crashed")
            return extract(files)
        self.ctx.extract = flaky
        with patch("data_pipeline.processor.log"):
            self.collect([base], "full")
        self.assertEqual(self.job_states(), ["pending"])
        source = FakeSource(dict([base]))
        self.ctx.fetch = lambda urls: [(safe_name(u), asyncio.run(source.download(u))) for u in urls]
        with patch.object(self.s3, "get_object", side_effect=AssertionError("processor must not read S3")):
            self.assertEqual(process_pending(self.ctx), {"done": 1})
        self.assertEqual(calls[0], calls[1])  # same bytes as the collector handed over
        version = self.store.select("legal_document_versions", {})[0]
        self.assertEqual((version["state"], version["source_urls"]), ("processed", [base[1][1]["files"][0]]))
        self.assertTrue(self.points(symbol="61/2020/QH14"))

    def test_version_saved_without_its_job_gets_the_job_on_the_next_scan(self):
        base = doc("1", "61/2020/QH14", "Luật Đầu tư", "Luật", "2021-01-01", BASE_LAW)
        with patch.object(self.store, "add_job", side_effect=RuntimeError("crash after add_version")):
            run = asyncio.run(collect("full", source=FakeSource(dict([base])), store=self.store, archiver=self.archiver,
                                      today=self.today))
        self.assertEqual(run["failed"], 1)
        self.assertEqual((len(self.store.select("legal_document_versions", {})), self.job_states()), (1, []))
        run = asyncio.run(collect("full", source=FakeSource(dict([base])), store=self.store, archiver=self.archiver,
                                  today=self.today))
        self.assertEqual((run["unchanged"], self.job_states()), (1, ["pending"]))  # the processor service takes it
        run = asyncio.run(collect("full", source=FakeSource(dict([base])), store=self.store, archiver=self.archiver,
                                  today=self.today))
        self.assertEqual(self.job_states(), ["pending"])  # never a second job for the same version

    def test_retry_refuses_bytes_that_no_longer_match_the_stored_version(self):
        base = doc("1", "61/2020/QH14", "Luật Đầu tư", "Luật", "2021-01-01", BASE_LAW)
        self.collect([base], "full", process=lambda job, files: None)  # job queued, not processed
        self.store.update("ingest_jobs", {}, {"state": "pending"})
        self.ctx.fetch = lambda urls: [("changed.pdf", b"%PDF-1.7 another file")]
        with patch("data_pipeline.processor.process_files", side_effect=AssertionError("must not process")):
            self.assertEqual(process_pending(self.ctx), {"failed": 1})
        self.assertEqual(self.store.select("ingest_jobs", {})[0]["error_type"], "SourceChanged")

    def test_failed_amendment_keeps_the_current_article(self):
        base = doc("1", "61/2020/QH14", "Luật Đầu tư", "Luật", "2021-01-01", BASE_LAW)
        amend = doc("9", "90/2025/QH15", "Luật sửa đổi, bổ sung một số điều của Luật Đầu tư", "Luật", "2025-07-01", AMENDMENT)
        self.collect([base], "full")
        base_id = self.store.document_by_symbol("61/2020/QH14")["id"]
        before = {a["article_no"]: a["text"] for a in self.store.current_articles(base_id)}
        index = self.ctx.qdrant.upsert
        calls = []

        def failing(document, rows, *args, **kwargs):  # the base law indexes fine, the amendment's new versions do not
            if document["symbol"] == "61/2020/QH14" and any(r.get("amended_by") for r in rows):
                calls.append(1)
                index(document, rows, *args, **kwargs)  # partly written before the failure
                raise RuntimeError("Qdrant outage")
            return index(document, rows, *args, **kwargs)
        with patch.object(self.ctx.qdrant, "upsert", side_effect=failing), patch("data_pipeline.processor.log"):
            self.collect([amend, base], "full")
        self.assertTrue(calls)
        after = {a["article_no"]: a["text"] for a in self.store.current_articles(base_id)}
        self.assertEqual(after, before)  # every article still has exactly its old current version
        self.assertEqual(len(self.points(symbol="61/2020/QH14", article_no="4", current=True)), 1)
        self.assertFalse([p for p in self.points(symbol="61/2020/QH14") if p.payload.get("amended_by")])
        # Retry (job back to pending) applies the amendment cleanly from the same state.
        source = FakeSource(dict([amend, base]))
        self.ctx.fetch = lambda urls: [(safe_name(u), asyncio.run(source.download(u))) for u in urls]
        self.assertEqual(process_pending(self.ctx).get("done"), 1)
        article4 = [a for a in self.store.current_articles(base_id) if a["article_no"] == "4"]
        self.assertEqual(len(article4), 1)
        self.assertIn("2a. Khoản hai a.", article4[0]["text"])

    def test_collector_consumes_sqs_and_deletes_only_after_success(self):
        sqs = boto3.client("sqs", region_name="ap-southeast-1")
        queue = sqs.create_queue(QueueName="contract-data-update-queue")["QueueUrl"]
        sqs.send_message(QueueUrl=queue, MessageBody=json.dumps({"mode": "full"}))
        runs = []

        class Stop:
            def __init__(self):
                self.calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls > 1

        serve(queue, lambda mode: runs.append(mode) or {"status": "ok"}, sqs=sqs, wait_seconds=0, stop=Stop())
        self.assertEqual(runs, ["full"])
        self.assertEqual(sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["ApproximateNumberOfMessages"])
                         ["Attributes"]["ApproximateNumberOfMessages"], "0")
        sqs.send_message(QueueUrl=queue, MessageBody="{}")
        with patch("data_pipeline.collector.log"):
            serve(queue, lambda mode: 1 / 0, sqs=sqs, wait_seconds=0, visibility_seconds=1, stop=Stop())
        self.assertEqual(message_mode('{"detail-type": "Scheduled Event"}'), "incremental")
        self.assertEqual(len(sqs.receive_message(QueueUrl=queue, WaitTimeSeconds=2).get("Messages", [])), 1)  # kept


@mock_aws
class CoverageTests(PipelineTests):
    """Hiến pháp listing, newest constitution wins, citations, several listing URLs."""
    test_collect_archives_by_law_number_and_is_idempotent = None
    test_process_rules_new_amendment_scheduled_and_out_of_order = None
    test_future_amendment_waits_for_refresh_and_bad_text_is_held = None
    test_collector_consumes_sqs_and_deletes_only_after_success = None
    test_failed_processing_is_retried_from_the_web_not_s3 = None

    def test_constitutions_keyed_by_year_and_newest_replaces_older(self):
        old = doc("10", "68/LCT/HĐNN8", "Hiến pháp năm 1992", "Hiến pháp", "1992-04-18",
                  "HIẾN PHÁP\nĐiều 1. Nước\nNội dung.\nĐiều 2. Nhà nước\nNội dung.", issued="1992-04-18")
        new = doc("11", ".", "Hiến pháp năm 2013", "Hiến pháp", "2014-01-01",
                  "HIẾN PHÁP\nĐiều 1. Nước\nTheo Luật Đầu tư số 61/2020/QH14.\nĐiều 2. Nhà nước\nNội dung.", issued="2013-11-28")
        self.collect([old, new], "full")
        self.assertEqual(self.job_states(), ["done", "done"])
        self.assertEqual(self.store.document_by_symbol("HP-2013")["status"], "in_force")
        self.assertEqual(self.store.document_by_symbol("HP-1992")["status"], "replaced")
        self.assertEqual(self.points(symbol="HP-1992")[0].payload["status"], "replaced")
        keys = [o["Key"] for o in self.s3.list_objects_v2(Bucket=BUCKET, Prefix="legal/hien-phap/")["Contents"]]
        self.assertTrue(any(k.startswith("legal/hien-phap/2013/HP-2013/source/") for k in keys), keys)
        cited = self.points(symbol="HP-2013", article_no="1")[0].payload["legal_references"]
        self.assertEqual(cited, ["61/2020/QH14"])

    def test_every_listing_url_is_crawled(self):
        groups = {"1": [("100", ".", "Hiến pháp năm 2013")], "3": [("1", "1/2025/QH15", "Luật A")]}

        def handler(request):
            group = request.url.params.get("typegroupid")
            return httpx.Response(200, text=page_html(groups[group], current=1, has_next=False, total=1))
        urls = [LIST_URL.replace("typegroupid=3", "typegroupid=1"), LIST_URL]
        source = LegalSource(urls, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), delay=0)
        rows, report = asyncio.run(source.list_documents(sweep_years=False))
        self.assertEqual((sorted(rows), report["total_on_site"], report["missing"]), (["1", "100"], 2, 0))


class AttachmentFormatTests(unittest.TestCase):
    """Every attachment format is collected; non-native office files are converted before extraction."""

    def test_any_attachment_is_downloaded_but_error_pages_are_not(self):
        bodies = {"/a.doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1doc", "/b.rtf": b"{\\rtf1 x}", "/c.pdf": b"%PDF-1.7",
                  "/gone.doc": b"<!DOCTYPE html><html>404</html>"}
        source = LegalSource(LIST_URL, delay=0, client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=bodies[r.url.path]))))
        for path in ("/a.doc", "/b.rtf", "/c.pdf"):
            self.assertEqual(asyncio.run(source.download("https://datafiles.chinhphu.vn" + path)), bodies[path])
        with self.assertRaises(ValueError):
            asyncio.run(source.download("https://datafiles.chinhphu.vn/gone.doc"))
        html = ('<table><tr><td>Tài liệu đính kèm</td><td><a class="view-file" '
                'href="https://datafiles.chinhphu.vn/cpp/files/vbpq/2006/07/89146_hp68qh.doc">f</a></td></tr></table>')
        self.assertEqual(parse_detail(html, LIST_URL)["files"], ["https://datafiles.chinhphu.vn/cpp/files/vbpq/2006/07/89146_hp68qh.doc"])

    def test_doc_and_rtf_are_converted_then_extracted(self):
        from data_pipeline.processing.extractor import extract_pdfs
        from test_file_processing import native_pdf
        converted = []

        def convert(name, data):
            converted.append(name)
            return native_pdf([f"Dieu 1. {name}"])
        result = extract_pdfs([("v1_x_hp68qh.doc", b"\xd0\xcf\x11\xe0"), ("a.pdf", native_pdf(["Dieu 2. B"]))],
                              convert=convert)
        self.assertEqual(converted, ["v1_x_hp68qh.doc"])  # the PDF went straight to input_processing
        self.assertEqual([p["file"] for p in result["pages"]], ["v1_x_hp68qh.pdf", "a.pdf"])
        self.assertIn("Dieu 1. v1_x_hp68qh.doc", result["pages"][0]["text"])
        self.assertEqual(result["method"], "text")


if __name__ == "__main__":
    unittest.main()
