# Báo cáo chấm điểm mẫu HD-002 — mức low
Nguồn: `risk/risk_rule_report.json` của gói ContractGuard 4.2–4.5. Đây là kết quả quét regex TẠM (`basis=rule_regex_provisional`), hit chưa có verdict: chỉ tham khảo cách đếm rule theo điều khoản, cộng điểm theo nhóm, rule thiếu section và cảnh báo. Không phải định dạng output và không phải kết luận pháp lý.

```json
{
 "contract_id": "HD-002",
 "document_kind": "agreement",
 "document_status": "needs_review",
 "language": "en",
 "risk_score": 13,
 "risk_level": "low",
 "risk_score_upper_bound": 13,
 "risk_level_upper_bound": "low",
 "group_scores": {
  "phap_ly": {
   "name": "Pháp lý",
   "points": 5.0,
   "level": "medium"
  },
  "tai_chinh": {
   "name": "Tài chính",
   "points": 0.0,
   "level": "low"
  },
  "van_hanh": {
   "name": "Vận hành",
   "points": 0.0,
   "level": "low"
  },
  "tuan_thu": {
   "name": "Tuân thủ",
   "points": 2.0,
   "level": "low"
  },
  "bao_mat": {
   "name": "Bảo mật & hạn chế cạnh tranh",
   "points": 2.0,
   "level": "low"
  },
  "ben_thu_ba": {
   "name": "Bên thứ ba",
   "points": 4.0,
   "level": "low"
  }
 },
 "basis": "rule_regex_provisional",
 "verification_gaps": [
  "other_clauses_unscanned",
  "missing_section_uncertain"
 ],
 "missing_section_rules": [],
 "missing_section_uncertain": [
  "M01",
  "M02"
 ],
 "missing_rules_skipped": null,
 "points_detail": [
  {
   "rule_id": "R03",
   "group": "phap_ly",
   "clauses": 1,
   "points": 5.0
  },
  {
   "rule_id": "R17",
   "group": "bao_mat",
   "clauses": 1,
   "points": 2.0
  },
  {
   "rule_id": "R18",
   "group": "ben_thu_ba",
   "clauses": 1,
   "points": 2.0
  },
  {
   "rule_id": "R19",
   "group": "tuan_thu",
   "clauses": 1,
   "points": 2.0
  },
  {
   "rule_id": "R20",
   "group": "ben_thu_ba",
   "clauses": 1,
   "points": 2.0
  }
 ],
 "pending_review": 5,
 "hit_summary": {
  "active": 5,
  "rejected": 0
 },
 "scan_coverage": {
  "clauses_total": 84,
  "clauses_scanned": 15,
  "clauses_other_unscanned": 69
 },
 "warnings": [
  "69 clause category 'other' chưa được quét rule (cần duyệt nhãn).",
  "Nhãn category không thấy section M01, M02 nhưng văn bản có nhắc nội dung liên quan: không tính điểm, cần người xem."
 ],
 "hits": [
  {
   "rule_id": "R19",
   "group": "tuan_thu",
   "clause_id": "HD-002-C0020",
   "category": "tax_and_compliance",
   "severity": "low",
   "matched_text": "COMPLIANCE WITH LAWS",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R03",
   "group": "phap_ly",
   "clause_id": "HD-002-C0080",
   "category": "general_and_boilerplate",
   "severity": "medium",
   "matched_text": "Indemnification",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R17",
   "group": "bao_mat",
   "clause_id": "HD-002-C0080",
   "category": "general_and_boilerplate",
   "severity": "low",
   "matched_text": "Confidential",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R20",
   "group": "ben_thu_ba",
   "clause_id": "HD-002-C0080",
   "category": "general_and_boilerplate",
   "severity": "low",
   "matched_text": "Third Parties",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R18",
   "group": "ben_thu_ba",
   "clause_id": "HD-002-C0081",
   "category": "general_and_boilerplate",
   "severity": "low",
   "matched_text": "may assign",
   "verdict": null,
   "source": "regex"
  }
 ]
}
```
