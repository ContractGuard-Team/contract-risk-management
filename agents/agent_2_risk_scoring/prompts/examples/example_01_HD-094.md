# Báo cáo chấm điểm mẫu HD-094 — mức high
Nguồn: `risk/risk_rule_report.json` của gói ContractGuard 4.2–4.5. Đây là kết quả quét regex TẠM (`basis=rule_regex_provisional`), hit chưa có verdict: chỉ tham khảo cách đếm rule theo điều khoản, cộng điểm theo nhóm, rule thiếu section và cảnh báo. Không phải định dạng output và không phải kết luận pháp lý (hiển thị 10/16 hit).

```json
{
 "contract_id": "HD-094",
 "document_kind": "agreement",
 "document_status": "needs_review",
 "language": "en",
 "risk_score": 51,
 "risk_level": "high",
 "risk_score_upper_bound": 51,
 "risk_level_upper_bound": "high",
 "group_scores": {
  "phap_ly": {
   "name": "Pháp lý",
   "points": 7.5,
   "level": "medium"
  },
  "tai_chinh": {
   "name": "Tài chính",
   "points": 9.5,
   "level": "medium"
  },
  "van_hanh": {
   "name": "Vận hành",
   "points": 15.0,
   "level": "high"
  },
  "tuan_thu": {
   "name": "Tuân thủ",
   "points": 9.5,
   "level": "medium"
  },
  "bao_mat": {
   "name": "Bảo mật & hạn chế cạnh tranh",
   "points": 9.0,
   "level": "medium"
  },
  "ben_thu_ba": {
   "name": "Bên thứ ba",
   "points": 0.0,
   "level": "low"
  }
 },
 "basis": "rule_regex_provisional",
 "verification_gaps": [
  "other_clauses_unscanned"
 ],
 "missing_section_rules": [],
 "missing_section_uncertain": [],
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
   "clauses": 2,
   "points": 7.5
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
   "rule_id": "R10",
   "group": "tai_chinh",
   "clauses": 2,
   "points": 7.5
  },
  {
   "rule_id": "R11",
   "group": "van_hanh",
   "clauses": 2,
   "points": 3.0
  },
  {
   "rule_id": "R12",
   "group": "tai_chinh",
   "clauses": 1,
   "points": 2.0
  },
  {
   "rule_id": "R13",
   "group": "tuan_thu",
   "clauses": 2,
   "points": 7.5
  },
  {
   "rule_id": "R17",
   "group": "bao_mat",
   "clauses": 1,
   "points": 2.0
  },
  {
   "rule_id": "R19",
   "group": "tuan_thu",
   "clauses": 1,
   "points": 2.0
  }
 ],
 "pending_review": 16,
 "hit_summary": {
  "active": 16,
  "rejected": 0
 },
 "scan_coverage": {
  "clauses_total": 38,
  "clauses_scanned": 28,
  "clauses_other_unscanned": 10
 },
 "warnings": [
  "10 clause category 'other' chưa được quét rule (cần duyệt nhãn)."
 ],
 "hits": [
  {
   "rule_id": "R07",
   "group": "van_hanh",
   "clause_id": "HD-094-C0003",
   "category": "term_and_termination",
   "severity": "medium",
   "matched_text": "successive one-year renewal periods",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R10",
   "group": "tai_chinh",
   "clause_id": "HD-094-C0004",
   "category": "compensation_and_payment",
   "severity": "medium",
   "matched_text": "forfeitable",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R08",
   "group": "van_hanh",
   "clause_id": "HD-094-C0004",
   "category": "compensation_and_payment",
   "severity": "low",
   "matched_text": "sole discretion",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R11",
   "group": "van_hanh",
   "clause_id": "HD-094-C0004",
   "category": "compensation_and_payment",
   "severity": "low",
   "matched_text": "Change of Control",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R12",
   "group": "tai_chinh",
   "clause_id": "HD-094-C0006",
   "category": "term_and_termination",
   "severity": "low",
   "matched_text": "severance",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R06",
   "group": "van_hanh",
   "clause_id": "HD-094-C0006",
   "category": "term_and_termination",
   "severity": "medium",
   "matched_text": "shall have the right to terminate his employment without Good Reason (as hereinafter defined) at any time",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R11",
   "group": "van_hanh",
   "clause_id": "HD-094-C0006",
   "category": "term_and_termination",
   "severity": "low",
   "matched_text": "Change of Control",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R01",
   "group": "bao_mat",
   "clause_id": "HD-094-C0007",
   "category": "confidentiality_and_ip",
   "severity": "medium",
   "matched_text": "Non-Competition",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R02",
   "group": "bao_mat",
   "clause_id": "HD-094-C0007",
   "category": "confidentiality_and_ip",
   "severity": "low",
   "matched_text": "Non-Solicitation",
   "verdict": null,
   "source": "regex"
  },
  {
   "rule_id": "R17",
   "group": "bao_mat",
   "clause_id": "HD-094-C0008",
   "category": "confidentiality_and_ip",
   "severity": "low",
   "matched_text": "Confidentiality",
   "verdict": null,
   "source": "regex"
  }
 ]
}
```
