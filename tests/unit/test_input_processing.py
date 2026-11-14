from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from input_processing.pipeline import process_job
from input_processing.schemas.processed_contract import JobError
from scripts.preprocess_contract import main


def job(text, **options):
    return {"contract_id": "HD-01", "job_id": "preprocess-01", "version": "2", "text": text,
            "options": {"max_tokens": 128, "overlap_tokens": 0, "min_tokens": 0, **options}}


class PreprocessingTests(unittest.TestCase):
    def process(self, text, **options):
        result = process_job(job(text, **options), token_counter=len)
        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(all(0 < chunk["token_count"] <= options.get("max_tokens", 128)
                            for chunk in result["chunks"]))
        return result

    def test_invalid_jobs_and_option_types_return_error(self):
        invalid = [None, [], {}, job(""), {**job("text"), "contract_id": []},
                   {**job("text"), "options": []}, {**job("text"), "options": "x"},
                   {**job("text"), "command": "delete"}, {**job("text"), "version": None},
                   {**job("text"), "job_id": ""}, {**job("text"), "source_metadata": []},
                   job("text", max_tokens="512"), job("text", max_tokens=True),
                   job("text", overlap_tokens=-1), job("text", overlap_tokens=64),
                   job("text", min_tokens=128), job("text", prepend_heading="false"),
                   job("text", unknown=True)]
        for data in invalid:
            with self.subTest(data=data):
                result = process_job(data)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["chunks"], [])

    def test_smaller_budget_uses_valid_defaults(self):
        result = process_job({"contract_id": "a", "text": "nội dung", "options": {"max_tokens": 32}})
        self.assertEqual(result["status"], "ok")
        self.assertIn("estimate", result["warnings"][0])

    def test_page_noise_does_not_delete_distinct_articles_or_amounts(self):
        text = "\f".join(f"Điều {i}. Thanh toán\n1000\nNội dung riêng {i}.\nTrang {i}/3" for i in range(1, 4))
        result = self.process(text)
        self.assertEqual(result["stats"]["articles_detected"], 3)
        self.assertEqual(result["stats"]["page_noise_lines_removed"], 3)
        self.assertEqual(result["cleaned_text"].count("1000"), 3)
        self.assertEqual({p for c in result["chunks"] for p in c["pages"]}, {1, 2, 3})

    def test_hyphen_and_decomposed_vietnamese_preserve_meaning(self):
        result = self.process("Điều 1. Quy định\r\nhợp-\r\nđồng đã ký.")
        self.assertIn("Điều 1", result["cleaned_text"])
        self.assertIn("hợp-\nđồng", result["cleaned_text"])
        self.assertNotIn("hợpđồng", result["cleaned_text"])

    def test_empty_table_cells_and_single_oversized_row_are_not_lost(self):
        text = "\tGiá\t\n| " + "x" * 400 + " |"
        result = self.process(text, max_tokens=64)
        self.assertIn("|  | Giá |  |", result["cleaned_text"])
        output = "".join(c["text"] for c in result["chunks"])
        self.assertEqual(output.count("x"), 400)
        self.assertTrue(result["warnings"])

    def test_long_unbroken_word_and_heading_stay_bounded(self):
        result = self.process("Điều 1. " + "T" * 100 + "\n" + "a" * 501, max_tokens=32)
        output = "".join(c["text"] for c in result["chunks"])
        self.assertEqual(output.count("a"), 501)
        self.assertEqual(output.count("T"), 100)

    def test_markdown_table_repeats_header_without_losing_rows(self):
        header = "| Mục | Giá |\n| --- | --- |"
        rows = [f"| Mục{i} | {1000+i} |" for i in range(8)]
        result = self.process(header + "\n" + "\n".join(rows), max_tokens=64)
        output = "\n".join(c["text"] for c in result["chunks"])
        for row in rows:
            self.assertEqual(output.count(row), 1)
        self.assertGreater(output.count(header), 1)

    def test_appendix_scope_and_article_identity_do_not_merge(self):
        result = self.process("Điều 1. Chính\nMột.\nPHỤ LỤC A\nĐiều 1. Phụ\nHai.", min_tokens=80)
        labels = [c for c in result["chunks"] if c["articles"]]
        self.assertEqual(len(labels), 2)
        self.assertEqual(labels[-1]["heading_path"], ["PHỤ LỤC A", "Điều 1. Phụ"])

    def test_source_spans_reference_raw_text_and_cleaned_text(self):
        raw = "\ufeffĐiều 1. Giá\r\nBên A thanh toán\r\ntrong 30 ngày.\fĐiều 2. Thời hạn\nMột năm."
        result = self.process(raw)
        for chunk in result["chunks"]:
            self.assertTrue(chunk["source_spans"])
            for span in chunk["source_spans"]:
                self.assertTrue(raw[span["raw_start"]:span["raw_end"]])
                self.assertTrue(result["cleaned_text"][span["cleaned_start"]:span["cleaned_end"]])

    def test_deterministic_chunks_and_no_mutation(self):
        data = job("Điều 1. Giá\nThanh toán trong ba ngày.")
        before = deepcopy(data)
        self.assertEqual(process_job(data), process_job(data))
        self.assertEqual(data, before)
        self.assertEqual(process_job(data)["chunks"][0]["pages"], [])

    def test_manifest_chunks_preserve_identity_and_counts(self):
        result = self.process("Điều 1. Một\nA.\nĐiều 2. Hai\nB.")
        self.assertEqual(result["stats"]["chunk_count"], len(result["chunks"]))
        for index, chunk in enumerate(result["chunks"]):
            self.assertEqual(chunk["chunk_index"], index)
            self.assertEqual(chunk["total_chunks"], len(result["chunks"]))
            self.assertEqual(chunk["version"], result["version"])
            self.assertEqual(chunk["contract_id"], result["contract_id"])

    def test_counter_failures_are_explicit(self):
        for count in (lambda s: -1, lambda s: True, lambda s: 0, lambda s: 1.2):
            self.assertEqual(process_job(job("text"), count)["status"], "error")
        def broken(text):
            raise RuntimeError("counter unavailable")
        with self.assertRaises(RuntimeError):
            process_job(job("text"), broken)
        self.assertEqual(process_job(job("x", max_tokens=32), lambda s: 100 if s else 0)["status"], "error")

    def test_overlap_reuses_source_spans_within_budget(self):
        result = self.process("Điều 1. Giá\n" + "\n".join(f"{i}. " + "x" * 12 for i in range(12)),
                              max_tokens=64, overlap_tokens=25)
        spans = [span["raw_start"] for c in result["chunks"] for span in c["source_spans"]]
        self.assertGreater(len(spans), len(set(spans)))

    def test_cli_json_stdin_remains_open_and_jsonl_failure_preserves_output(self):
        stream = StringIO(json.dumps(job("text")))
        with patch("sys.stdin", stream), redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["-"]), 0)
        self.assertFalse(stream.closed)
        self.assertEqual(json.loads(output.getvalue())["status"], "ok")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "out.jsonl"
            target.write_text("previous output", encoding="utf-8")
            with patch("sys.stdin", StringIO('{}')), redirect_stderr(StringIO()):
                self.assertEqual(main(["-", "--jsonl", "-o", str(target)]), 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "previous output")

    def test_cli_invalid_json_options_and_zero_limit(self):
        for payload, extra in (("not-json", []), ('[]', []), (json.dumps(job("text")), ["--max-tokens", "0"])):
            with patch("sys.stdin", StringIO(payload)), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(main(["-", *extra]), 1)


if __name__ == "__main__":
    unittest.main()
