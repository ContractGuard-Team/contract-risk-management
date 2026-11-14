"""rag/: hybrid search, metadata/date filters, whole-article grouping, rerank and evidence on an in-memory Qdrant
indexed by the real data-pipeline writer (models are fakes)."""
import hashlib
import unittest
import warnings

from qdrant_client import QdrantClient

from data_pipeline.ingestion.qdrant_writer import QdrantWriter
from integrations.external.embedding_client import keyword_tokens, sparse_vector
from rag.retriever import LegalRetriever


def vector(text):
    return [b / 255 + 0.01 for b in hashlib.sha256(text.encode()).digest()[:8]]


class FakeEmbedder:
    def embed(self, texts):
        return [vector(t) for t in texts]

    def embed_query(self, text):
        return vector(text)


class PreferReranker:
    def __init__(self, word):
        self.word = word

    def scores(self, query, texts):
        return [1.0 if self.word in t else 0.0 for t in texts]


CIVIL = {"id": 1, "symbol": "91/2015/QH13", "doc_class": "code", "title": "Bộ luật Dân sự", "status": "in_force",
         "effective_on": "2017-01-01", "s3_prefix": "legal/QH13/2015/091-2015-QH13", "source_url": "https://x/?docid=1"}
INVEST = {"id": 2, "symbol": "61/2020/QH14", "doc_class": "law", "title": "Luật Đầu tư", "status": "in_force",
          "effective_on": "2021-01-01", "s3_prefix": "legal/QH14/2020/061-2020-QH14", "source_url": "https://x/?docid=2"}
OLD = {"id": 3, "symbol": "67/2014/QH13", "doc_class": "law", "title": "Luật Đầu tư", "status": "in_force",
       "effective_on": "2015-07-01", "s3_prefix": "legal/QH13/2014/067-2014-QH13", "source_url": "https://x/?docid=3"}


def article(no, heading, text, valid_from, **extra):
    return {"article_no": no, "heading": heading, "chapter": "Chương I", "text": f"Điều {no}. {heading}\n{text}",
            "valid_from": valid_from, "version": 1, "page_from": 1, "legal_references": [], **extra}


class RagTests(unittest.TestCase):
    def setUp(self):
        warnings.simplefilter("ignore", UserWarning)  # local Qdrant ignores payload indexes
        self.writer = QdrantWriter(client=QdrantClient(":memory:"), collection="legal_documents", dimensions=8)
        self.writer.ensure_indexes()
        self.index(CIVIL, [article("418", "Thỏa thuận phạt vi phạm", "Phạt vi phạm là sự thỏa thuận giữa các bên.", "2017-01-01"),
                           article("419", "Thiệt hại được bồi thường", "Thiệt hại được bồi thường do vi phạm hợp đồng.", "2017-01-01"),
                           article("420", "Điều chỉnh hợp đồng", "Hoàn cảnh thay đổi cơ bản.", "2017-01-01",
                                   has_pending_amendment=True)])
        # Điều 4 of the investment law: long (two parts), later amended on 2025-07-01.
        self.index(INVEST, [article("4", "Áp dụng Luật Đầu tư", "Khoản một cũ.", "2021-01-01")],
                   parts=["Điều 4. Áp dụng Luật Đầu tư\nKhoản một cũ.", "Khoản hai cũ về ưu đãi đầu tư."])
        self.writer.retire(INVEST["symbol"], "4", "2025-07-01")
        self.index(INVEST, [article("4", "Áp dụng Luật Đầu tư", "Khoản một mới.", "2025-07-01")],
                   amended_by="90/2025/QH15")
        self.index(OLD, [article("4", "Áp dụng Luật Đầu tư", "Bản 2014.", "2015-07-01")])
        self.writer.set_status(OLD["symbol"], "replaced", replaced_on="2021-01-01")
        self.rag = LegalRetriever(client=self.writer.client, collection="legal_documents", embedder=FakeEmbedder(),
                                  reranker=False)

    def index(self, document, rows, parts=None, amended_by=None):
        texts = parts or [rows[0]["text"]] if len(rows) == 1 else [r["text"] for r in rows]
        layout = [(0, p, t) for p, t in enumerate(texts)] if parts else [(i, 0, r["text"]) for i, r in enumerate(rows)]
        self.writer.upsert(document, rows, layout, FakeEmbedder().embed([t for *_, t in layout]), amended_by=amended_by)

    def test_keyword_side_of_hybrid_finds_the_article(self):
        result = self.rag.search("phạt vi phạm theo thỏa thuận", limit=1)
        self.assertEqual(result["evidence"][0]["citation"], "Điều 418 Bộ luật Dân sự số 91/2015/QH13")
        self.assertFalse(result["reranked"])

    def test_default_is_current_text_of_laws_in_force_one_item_per_article(self):
        found = self.rag.search("Áp dụng Luật Đầu tư", limit=10)["evidence"]
        investment = [e for e in found if e["article_no"] == "4"]
        self.assertEqual([(e["symbol"], e["valid_from"]) for e in investment], [("61/2020/QH14", "2025-07-01")])
        self.assertIn("Nội dung đã được sửa đổi bởi 90/2025/QH15.", investment[0]["warnings"])

    def test_as_of_returns_the_text_in_force_on_that_date(self):
        def versions(day):
            return sorted((e["symbol"], e["valid_from"]) for e in self.rag.search(
                "Áp dụng Luật Đầu tư", as_of=day, limit=10)["evidence"] if e["article_no"] == "4")
        self.assertEqual(versions("2018-05-01"), [("67/2014/QH13", "2015-07-01")])  # before the 2020 law
        old = self.rag.search("Áp dụng Luật Đầu tư", as_of="2023-01-01", limit=10)["evidence"]
        old = [e for e in old if e["article_no"] == "4"]
        self.assertEqual([(e["symbol"], e["valid_from"]) for e in old], [("61/2020/QH14", "2021-01-01")])
        self.assertEqual(old[0]["text"], "Điều 4. Áp dụng Luật Đầu tư\nKhoản một cũ.\nKhoản hai cũ về ưu đãi đầu tư.")
        self.assertTrue(any("Bản lịch sử" in w for w in old[0]["warnings"]))
        self.assertEqual(versions("2026-01-01"), [("61/2020/QH14", "2025-07-01")])

    def test_include_inactive_and_doc_class(self):
        everything = self.rag.search("Áp dụng Luật Đầu tư", include_inactive=True, limit=10)["evidence"]
        self.assertIn("67/2014/QH13", {e["symbol"] for e in everything})
        codes = self.rag.search("hợp đồng", doc_class="code", limit=10)["evidence"]
        self.assertEqual({e["doc_class"] for e in codes}, {"code"})

    def test_lookup_by_symbol_and_article_and_warnings(self):
        item = self.rag.search(symbol="91/2015/qh13", article_no="420")["evidence"][0]
        self.assertEqual((item["evidence_id"], item["s3_prefix"]), ("91/2015/QH13#420@2017-01-01", CIVIL["s3_prefix"]))
        self.assertTrue(item["has_pending_amendment"])
        self.assertTrue(any("chưa được áp dụng" in w for w in item["warnings"]))

    def test_reranker_orders_the_candidates(self):
        rag = LegalRetriever(client=self.writer.client, collection="legal_documents", embedder=FakeEmbedder(),
                             reranker=PreferReranker("Hoàn cảnh thay đổi"))
        result = rag.search("vi phạm hợp đồng", limit=2)
        self.assertTrue(result["reranked"])
        self.assertEqual(result["evidence"][0]["article_no"], "420")

    def test_validation(self):
        for kwargs in ({"query": ""}, {"query": "x", "as_of": "01/01/2024"}, {"query": "x", "doc_class": "decree"},
                       {"query": "x" * 1001}, {"query": "x", "symbol": "a b"}):
            with self.assertRaises(ValueError):
                self.rag.search(**kwargs)
        self.assertEqual(self.rag.search("không có trong kho", symbol="1/2000/QH10")["found"], 0)

    def test_keyword_vector(self):
        self.assertIn("91/2015/qh13", keyword_tokens("Bộ luật Dân sự số 91/2015/QH13"))
        self.assertIn("hợp đồng", keyword_tokens("Hợp đồng mua bán"))
        doc, query = sparse_vector("phạt phạt vi phạm"), sparse_vector("phạt vi phạm", query=True)
        self.assertEqual(set(query["values"]), {1.0})
        self.assertEqual(len(doc["indices"]), len(set(doc["indices"])))
        self.assertGreater(max(doc["values"]), 1.0)  # repeated term weighs more, saturating


if __name__ == "__main__":
    unittest.main()
