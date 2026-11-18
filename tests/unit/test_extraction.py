"""Business schemas (common/schemas/business, agents/*/schemas) -> 4.5b scoring in code -> AnalysisResult."""
import json
import unittest

from agents.service import fixture_output
from agents.shared.output import CHECK_CODES, model_errors, output_models
from common.schemas.business.inbound import AnalysisRequest, InboundMessage
from common.schemas.business.outbound import AnalysisResult
from common.schemas.contract import ContractChunk, ContractInput
from orchestration.extraction import build_extraction
from orchestration.scoring import score_contract

AGENTS = ("agent_1_classification", "agent_2_risk_scoring", "agent_3_recommendation")
TRACE = {"model_version": "test-model", "prompt_version": "abc", "pipeline_version": "test"}
CID = "c0ffee00-0000-4000-8000-000000000009"
JOB = "5f0c2c1e-0000-4000-8000-000000000001"
REQUEST = {"schema_version": "1.0.0", "job_id": JOB, "tenant_id": "org-1", "contract_id": CID, "contract_version": "2",
           "source_file_key": "quarantine/u/1.pdf", "clean_file_key": f"clean/org/{CID}/v2/source.pdf",
           "result_key": f"results/{JOB}/extraction.json", "declared_file_type": "pdf", "output_language": "vi",
           "context": {"own_party": "buyer", "contract_type": "service_agreement", "has_personal_data": True},
           "limits": {"max_pages": 100, "max_bytes": 20971520}}


def result(agent_id, data):
    return {"agent_id": agent_id, "text": json.dumps(data, ensure_ascii=False), "usage": {}, "stop_reason": "stop"}


def clause(number, text, category, page=1, **changes):
    return {"clause_id": f"{CID}-C{number:04d}", "continuation": False, "complete": True, "number": str(number),
            "heading": None, "text": text, "category": category, "clause_type": "C99", "page_start": page,
            "page_end": page, "facts": [], **changes}


def agent_1(chunk_id, clauses, **changes):
    return {"chunk_id": chunk_id, "language": "vi", "clauses": clauses,
            "metadata": {"parties": [], "contract_type": None, "contract_value": None, "currency": None,
                         "effective_date": None, "expiry_date": None, "governing_law": None},
            "fact_values": {}, "injection_warnings": [], "corrections": [], **changes}


def checks(**overrides):
    return [{"check_code": code, "check_status": "not_applicable", "clause_ids": [], "finding_id": None, "note": None,
             **overrides.get(code, {})} for code in CHECK_CODES]


def contract(file_format="pdf"):
    return ContractInput(CID, "2", [ContractChunk("k0", 0, "x"), ContractChunk("k1", 1, "y")], 2, {
        "preprocessing": {"document": {"format": file_format, "page_count": 3}, "extraction": {"ocr_segments": 0}},
        "request": REQUEST})


class BusinessSchemaTests(unittest.TestCase):
    def test_inbound_keeps_every_field_the_partner_sends(self):
        request = AnalysisRequest.model_validate({**REQUEST, "new_partner_field": {"a": 1},
                                                  "context": {**REQUEST["context"], "counterparty_name": "B", "extra": 2}})
        dumped = request.model_dump(mode="json")
        self.assertEqual(dumped["new_partner_field"], {"a": 1})  # unknown fields are received, not dropped
        self.assertEqual((dumped["context"]["counterparty_name"], dumped["context"]["extra"]), ("B", 2))
        message = InboundMessage.validate_python({"message_version": "1", "event_type": "analysis_requested",
                                                  "job_id": JOB, "request_key": f"results/{JOB}/request.json"})
        self.assertEqual(type(message).__name__, "AnalysisRequested")

    def test_inbound_rejects_what_the_partner_contract_forbids(self):
        for bad in ({"declared_file_type": "txt"}, {"output_language": "fr"}, {"contract_version": "0"},
                    {"source_file_key": "uploads/a.pdf"}, {"result_key": "results/other/extraction.json"},
                    {"limits": {"max_pages": 0, "max_bytes": 1}}, {"context": {"own_party": "buyer"}}):
            with self.subTest(bad=bad):
                self.assertTrue(model_errors(AnalysisRequest, {**REQUEST, **bad}))
        missing = {k: v for k, v in REQUEST.items() if k != "tenant_id"}
        self.assertTrue(model_errors(AnalysisRequest, missing))

    def test_every_business_model_publishes_a_json_schema(self):
        for agent, model in output_models().items():
            with self.subTest(agent=agent):
                schema = model.model_json_schema()
                self.assertEqual(schema["type"], "object")
                self.assertFalse(schema.get("additionalProperties", True))  # agents may not invent keys
        self.assertIn("checks", AnalysisResult.model_json_schema()["properties"])

    def test_fixture_answers_match_the_agent_models(self):
        request = {"content": "Điều 1. Giá", "context": {"workflow": {
            "chunk_id": "k0", "chunk_index": 0, "contract_id": CID, "source_metadata": {"pages": [2]}}}}
        for agent, model in output_models().items():
            with self.subTest(agent=agent):
                self.assertEqual(model_errors(model, fixture_output(agent, request)), [])


class ScoringTests(unittest.TestCase):
    def test_hd_001_matches_the_partner_report(self):
        # risk_rule_report.json HD-001: R01x1, R06x2, R11x2, R17x1, M01 missing, M03 uncertain -> 23 medium.
        clauses = [{"clause_id": "a", "category": "preamble_and_scope", "text": "Agreement"},
                   {"clause_id": "t1", "category": "term_and_termination", "text": "terminate without cause"},
                   {"clause_id": "t2", "category": "term_and_termination", "text": "change of control"},
                   {"clause_id": "c1", "category": "confidentiality_and_ip", "text": "non-compete, confidential"},
                   {"clause_id": "g", "category": "general_and_boilerplate", "text": "breach of this agreement"}]
        verdict = lambda rid: {"rule_id": rid, "verdict": "confirmed", "evidence_quote": "x", "rationale": "y"}
        verdicts = [{"clause_id": "t1", "verdicts": [verdict("R06"), verdict("R11")], "additional": []},
                    {"clause_id": "t2", "verdicts": [verdict("R06"), verdict("R11")], "additional": []},
                    {"clause_id": "c1", "verdicts": [verdict("R01"), verdict("R17")], "additional": []}]
        scored = score_contract(clauses, verdicts)
        self.assertEqual((scored["scoring"]["overall_score"], scored["scoring"]["risk_level"]), (23, "medium"))
        self.assertEqual(scored["scoring"]["rule_version"], "4.5b-1.2.2")
        detail = scored["detail"]
        self.assertEqual((detail["missing_section_rules"], detail["missing_section_uncertain"]), (["M01"], ["M03"]))
        self.assertEqual({g: v["points"] for g, v in detail["group_scores"].items() if v["points"]},
                         {"phap_ly": 5.0, "van_hanh": 10.5, "bao_mat": 7.0})
        domains = {d["domain"]: d for d in scored["scoring"]["domains"]}
        self.assertEqual(len(domains), 7)
        self.assertEqual((domains["legal"]["score"], domains["legal"]["risk_level"]), (10, "medium"))  # R01 + M01
        self.assertEqual((domains["operational"]["score"], domains["data_privacy"]["score"]), (11, 2))
        self.assertFalse(domains["cybersecurity"]["applicable"])
        self.assertTrue(domains["cybersecurity"]["na_reason"])

    def test_pending_only_raises_the_upper_bound_and_other_is_never_scored(self):
        # Risk_Agent example 3 (HD-003): R10 pending, M03 missing -> score 2, upper bound 7.
        clauses = [{"clause_id": "p", "category": "compensation_and_payment", "text": "penalty"},
                   {"clause_id": "d", "category": "dispute_and_governing_law", "text": "x"},
                   {"clause_id": "t", "category": "term_and_termination", "text": "x"},
                   {"clause_id": "o", "category": "other", "text": "x"}]
        verdicts = [{"clause_id": "p", "verdicts": [{"rule_id": "R10", "verdict": "needs_review", "evidence_quote": "x",
                                                    "rationale": "y"}], "additional": []},
                    {"clause_id": "o", "verdicts": [], "additional": [{"rule_id": "R08", "evidence_quote": "x",
                                                                       "rationale": "y"}]}]
        scored = score_contract(clauses, verdicts)
        self.assertEqual((scored["scoring"]["overall_score"], scored["detail"]["risk_score_upper_bound"]), (2, 7))
        self.assertEqual(scored["detail"]["basis"], "partial_pending_review")


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        quote = "phạt 8% giá trị phần nghĩa vụ vi phạm"
        self.results = {
            "agent_1_classification": {"chunks": [
                {"chunk_id": "k0", "metadata": {"pages": [1]}, "result": result("agent_1_classification", agent_1("k0", [
                    clause(1, "Điều 1. Giá trị hợp đồng là 100 triệu đồng.", "compensation_and_payment"),
                    clause(2, "Điều 2. Bên B chịu", "breach_and_liabilities", complete=False)],
                    fact_values={"payment_term_days": {"status": "FOUND", "value": 30, "evidence": [
                        {"clause_id": f"{CID}-C0001", "page": 1, "quote": "không có trong văn bản"}]}}))},
                {"chunk_id": "k1", "metadata": {"pages": [2, 3]}, "result": result("agent_1_classification", agent_1("k1", [
                    clause(2, f"{quote}.", "breach_and_liabilities", page=2, continuation=True),
                    clause(3, "Điều 3. Tranh chấp giải quyết tại tòa án.", "general_and_boilerplate", page=3)],
                    corrections=[{"clause_id": f"{CID}-C0003", "category": "dispute_and_governing_law",
                                  "clause_type": "C99", "reason": "điều khoản tranh chấp"}],
                    metadata={"parties": ["Công ty A"], "contract_type": "service_agreement", "contract_value": 100000000,
                              "currency": "VND", "effective_date": "2026-01-01", "expiry_date": None,
                              "governing_law": "Việt Nam"}))}]},
            "agent_2_risk_scoring": result("agent_2_risk_scoring", {
                "clause_verdicts": [{"clause_id": f"{CID}-C0002", "verdicts": [
                    {"rule_id": "R10", "verdict": "confirmed", "evidence_quote": quote, "rationale": "Có điều khoản phạt."}],
                    "additional": []}],
                "findings": [
                    {"finding_id": "F1", "finding_kind": "problematic_clause", "title": "Phạt vi phạm", "rule_id": "R10",
                     "check_code": None, "severity": "medium", "clause_id": f"{CID}-C0002", "missing_description": None,
                     "evidence": [{"clause_id": f"{CID}-C0002", "page": 9, "quote": quote},
                                  {"clause_id": f"{CID}-C0002", "page": 2, "quote": "câu không tồn tại"}],
                     "why": "Mức phạt cố định.", "impact": "Chi phí khi vi phạm.",
                     "references": [{"source": "legal_search", "reference_number": "91/2015/QH13 Điều 418"}]},
                    {"finding_id": "F2", "finding_kind": "problematic_clause", "title": "Trích sai", "rule_id": "R03",
                     "check_code": None, "severity": "medium", "clause_id": f"{CID}-C0001", "missing_description": None,
                     "evidence": [{"clause_id": f"{CID}-C0001", "page": 1, "quote": "bồi hoàn mọi thiệt hại"}],
                     "why": "x", "impact": "y", "references": []},
                    {"finding_id": "F3", "finding_kind": "missing_clause", "title": "Thiếu bảo mật dữ liệu",
                     "rule_id": None, "check_code": "PR-01", "severity": "high", "clause_id": None, "evidence": [],
                     "missing_description": "Không thấy điều khoản bảo vệ dữ liệu cá nhân.", "why": "x", "impact": "y",
                     "references": []}],
                "checks": checks(**{"PR-01": {"check_status": "missing", "finding_id": "F3"},
                                    "CY-01": {"check_status": "partial", "finding_id": "F2",
                                              "clause_ids": [f"{CID}-C0001", "unknown"]}}),
                "evidence_gaps": []}),
            "agent_3_recommendation": result("agent_3_recommendation", {
                "recommendations": [
                    {"finding_id": fid, "recommendation": f"Sửa {fid}", "proposed_text": None, "priority": "high",
                     "evidence": [], "references": [{"source": "legal_search", "reference_number": "91/2015/QH13 Điều 418"}]}
                    for fid in ("F1", "F2", "F3")],
                "overall_recommendation": "Đàm phán lại điều khoản phạt.", "evidence_gaps": []}),
        }

    def test_builds_a_schema_valid_extraction_v2(self):
        extraction, notes = build_extraction(contract(), self.results, job_id=JOB, trace=TRACE)
        self.assertEqual(model_errors(AnalysisResult, extraction), [])
        self.assertEqual([c["order"] for c in extraction["clauses"]], [1, 2, 3])
        merged = extraction["clauses"][1]
        self.assertEqual(merged["text"], "Điều 2. Bên B chịu\nphạt 8% giá trị phần nghĩa vụ vi phạm.")  # continuation
        self.assertEqual(merged["location"], {"page_start": 1, "page_end": 2, "regions": []})
        self.assertTrue(extraction["document_complete"])
        findings = {f["finding_id"]: f for f in extraction["findings"]}
        self.assertEqual(sorted(findings), ["F1", "F3"])  # F2 had no quote inside its clause
        self.assertEqual(notes["dropped_findings"], ["F2"])
        self.assertEqual(findings["F1"]["evidence"], [{"clause_id": f"{CID}-C0002", "page": 1,
                                                       "quote": "phạt 8% giá trị phần nghĩa vụ vi phạm"}])
        self.assertEqual((findings["F1"]["primary_domain"], findings["F1"]["category"]), ("financial", "penalty_or_forfeiture"))
        self.assertEqual(len(findings["F1"]["references"]), 1)  # Agent 2 + Agent 3 references de-duplicated
        self.assertEqual((findings["F3"]["primary_domain"], findings["F3"]["recommendation"]), ("data_privacy", "Sửa F3"))
        by_code = {c["check_code"]: c for c in extraction["checks"]}
        self.assertEqual((by_code["CY-01"].get("finding_id"), by_code["CY-01"]["clause_ids"]), (None, [f"{CID}-C0001"]))
        self.assertEqual(extraction["facts"]["payment_term_days"]["status"], "UNKNOWN")  # its quote is not in the text
        self.assertEqual(extraction["metadata"]["parties"], ["Công ty A"])
        self.assertEqual(extraction["document"], {"file_type": "pdf", "page_count": 3, "language": "vi",
                                                  "is_scanned": False, "clean_file_key": f"clean/org/{CID}/v2/source.pdf"})
        # Correction moved C0003 to dispute_and_governing_law: no M01; M02 (no term clause) is missing.
        self.assertEqual(notes["scoring_detail"]["missing_section_rules"], ["M02"])
        self.assertEqual(extraction["scoring"]["overall_score"], 10)  # R10 confirmed (5) + M02 (5)
        self.assertEqual((extraction["tenant_id"], extraction["output_language"], extraction["trace"]), ("org-1", "vi", TRACE))

    def test_verdict_with_a_quote_outside_its_clause_never_scores(self):
        risk = json.loads(self.results["agent_2_risk_scoring"]["text"])
        risk["clause_verdicts"][0]["verdicts"].append(
            {"rule_id": "R03", "verdict": "confirmed", "evidence_quote": "bồi hoàn mọi thiệt hại", "rationale": "x"})
        self.results["agent_2_risk_scoring"] = result("agent_2_risk_scoring", risk)
        extraction, notes = build_extraction(contract(), self.results, job_id=JOB, trace=TRACE)
        self.assertEqual((extraction["scoring"]["overall_score"], notes["dropped_verdicts"]), (10, 1))  # not 15

    def test_reused_clause_id_without_continuation_is_an_error(self):
        chunks = self.results["agent_1_classification"]["chunks"]
        second = json.loads(chunks[1]["result"]["text"])
        second["clauses"][0]["continuation"] = False
        chunks[1]["result"] = result("agent_1_classification", second)
        with self.assertRaisesRegex(ValueError, "two different clauses"):
            build_extraction(contract(), self.results, job_id=JOB, trace=TRACE)

    def test_continuation_and_chunk_ids_are_checked(self):
        chunks = self.results["agent_1_classification"]["chunks"]
        first, second = (json.loads(c["result"]["text"]) for c in chunks)
        for broken_first, broken_second, message in (
                ({**first, "chunk_id": "k9"}, second, "answered chunk k9"),
                ({**first, "clauses": [first["clauses"][0], {**first["clauses"][1], "complete": True}]}, second,
                 "does not follow a cut clause"),          # continues a clause that was not cut
                (first, {**second, "clauses": second["clauses"][::-1]}, "does not follow a cut clause")):  # not first
            with self.subTest(message=message):
                chunks[0]["result"] = result("agent_1_classification", broken_first)
                chunks[1]["result"] = result("agent_1_classification", broken_second)
                with self.assertRaisesRegex(ValueError, message):
                    build_extraction(contract(), self.results, job_id=JOB, trace=TRACE)

    def test_clause_pages_stay_inside_their_chunk(self):
        chunks = self.results["agent_1_classification"]["chunks"]
        second = json.loads(chunks[1]["result"]["text"])
        second["clauses"][1].update(page_start=99, page_end=99)
        chunks[1]["result"] = result("agent_1_classification", second)
        extraction, _ = build_extraction(contract(), self.results, job_id=JOB, trace=TRACE)
        self.assertEqual(extraction["clauses"][2]["location"]["page_start"], 3)
        self.assertEqual(extraction["document"]["page_count"], 3)

    def test_every_finding_needs_a_recommendation(self):
        advice = json.loads(self.results["agent_3_recommendation"]["text"])
        advice["recommendations"] = advice["recommendations"][:1]
        self.results["agent_3_recommendation"] = result("agent_3_recommendation", advice)
        with self.assertRaisesRegex(ValueError, "no recommendation"):
            build_extraction(contract(), self.results, job_id="j", trace=TRACE)

    def test_invalid_agent_output_is_rejected_before_building(self):
        self.results["agent_2_risk_scoring"] = result("agent_2_risk_scoring", {"findings": []})
        with self.assertRaisesRegex(ValueError, "does not match its business schema"):
            build_extraction(contract(), self.results, job_id="j", trace=TRACE)

    def test_internal_txt_jobs_skip_only_the_partner_file_type(self):
        extraction, _ = build_extraction(contract("txt"), self.results, job_id="j", trace=TRACE)
        self.assertEqual(extraction["document"]["file_type"], "txt")
        errors = model_errors(AnalysisResult, extraction)
        self.assertTrue(errors and all(e.startswith("document/file_type") for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
