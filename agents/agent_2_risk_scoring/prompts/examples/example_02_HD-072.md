# Báo cáo chấm điểm mẫu HD-072 — mức medium
Nguồn: `risk/risk_rule_report.json` của gói ContractGuard 4.2–4.5. Đây là kết quả quét regex TẠM (`basis=rule_regex_provisional`), hit chưa có verdict: chỉ tham khảo cách đếm rule theo điều khoản, cộng điểm theo nhóm, rule thiếu section và cảnh báo. Không phải định dạng output và không phải kết luận pháp lý (hiển thị 10/11 hit).

```json
{
 "contract_id": "HD-072",
 "document_kind": "agreement",
 "document_status": "needs_review",
 "language": "en",
 "risk_score": 34,
 "risk_level": "medium",
 "risk_score_upper_bound": 34,
 "risk_level_upper_bound": "medium",
 "group_scores": {
  "phap_ly": {
   "name": "Pháp lý",
   "points": 5.0,
   "level": "medium"
  },
  "tai_chinh": {
   "name": "Tài chính",
   "points": 2.0,
   "level": "low"
  },
  "van_hanh": {
   "name": "Vận hành",
   "points": 14.0,
   "level": "high"
  },
  "tuan_thu": {
   "name": "Tuân thủ",
   "points": 0.0,
   "level": "low"
  },
  "bao_mat": {
   "name": "Bảo mật & hạn chế cạnh tranh",
   "points": 9.0,
   "level": "medium"
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
  "M01"
 ],
 "missing_rules_skipped": null,
 "points_detail": [
  {
   "rule_id": "R01",
   "group": "bao_mat",
   "clauses": 1,
   "points": 5.0
  },
  {
   "rule_id": "R02",
   "group": "bao_mat",
   "clauses": 1,
   "points": 2.0
  },
  {
   "rule_id": "R03",
   "group": "phap_ly",
   "clauses": 1,
   "points": 5.0
  },
  {
   "rule_id": "R06",
   "group": "van_hanh",
   "clauses": 1,
   "points": 5.0
  },
  {
   "rule_id": "R07",
   "group": "van_hanh",
   "clauses": 1,
   "points": 5.0
  },
  {
   "rule_id": "R08",
   "group": "van_hanh",
   "clauses": 1,
   "points": 2.0
  },
  {
   "rule_id": "R11",
   "group": "van_hanh",
   "clauses": 1,
   "points": 2.0
  },
  {
   "rule_id": "R12",
   "group": "tai_chinh",
   "clauses": 1,
   "points": 2.0
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
   "rule_id": "R20",
   "group": "ben_thu_ba",
   "clauses": 1,
   "points": 2.0
  }
 ],
 "pending_review": 11,
 "hit_summary": {
  "active": 11,
  "rejected": 0
 },
 "scan_coverage": {
  "clauses_total": 17,
  "clauses_scanned": 11,
  "clauses_other_unscanned": 6
 },
 "warnings": [
  "6 clause category 'other' chưa được quét rule (cần duyệt nhãn).",
  "Nhãn category không thấy section M01 nhưng văn bản có nhắc nội dung liên quan: không tính điểm, cần người xem."
 ],
 "hits": [
  {
   "rule_id": "R07",
   "group": "van_hanh",
   "clause_id": "HD-072-C0002",
   "category": "term_and_termination",
   "severity": "medium",
   "matched_text": "automatically extend",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R08",
   "group": "van_hanh",
   "clause_id": "HD-072-C0004",
   "category": "compensation_and_payment",
   "severity": "low",
   "matched_text": "sole discretion",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R12",
   "group": "tai_chinh",
   "clause_id": "HD-072-C0009",
   "category": "term_and_termination",
   "severity": "low",
   "matched_text": "Severance",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R06",
   "group": "van_hanh",
   "clause_id": "HD-072-C0009",
   "category": "term_and_termination",
   "severity": "medium",
   "matched_text": "may be terminated by either party at any time and for any reason",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R11",
   "group": "van_hanh",
   "clause_id": "HD-072-C0009",
   "category": "term_and_termination",
   "severity": "low",
   "matched_text": "Change in Control",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R01",
   "group": "bao_mat",
   "clause_id": "HD-072-C0010",
   "category": "confidentiality_and_ip",
   "severity": "medium",
   "matched_text": "Non-Competition",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R02",
   "group": "bao_mat",
   "clause_id": "HD-072-C0010",
   "category": "confidentiality_and_ip",
   "severity": "low",
   "matched_text": "will not, directly or indirectly, solicit",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R17",
   "group": "bao_mat",
   "clause_id": "HD-072-C0011",
   "category": "confidentiality_and_ip",
   "severity": "low",
   "matched_text": "Confidentiality",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R03",
   "group": "phap_ly",
   "clause_id": "HD-072-C0013",
   "category": "general_and_boilerplate",
   "severity": "medium",
   "matched_text": "Indemnification",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R18",
   "group": "ben_thu_ba",
   "clause_id": "HD-072-C0013",
   "category": "general_and_boilerplate",
   "severity": "low",
   "matched_text": "may be assigned",
   "verdict": null,
   "source": "regex"
  }
 ]
}
```
