"""Build the AnalysisResult (common/schemas/business/outbound.py) ContractGuard receives, from the agents' outputs.

Runs in the LangGraph service after the workflow; the worker only writes the result to S3. Each agent output is
first validated with its business model (agents/<agent>/schemas/output.py); job facts come from the whole
ContractGuard request in context["request"]. Deterministic steps happen here, not in a model:
- Agent 1 chunks -> one clause list (continuations merged, corrections applied, order 1..n), metadata/facts merged;
- every evidence quote must be a contiguous substring of its clause (normalised as risk_policy `matching`) and its
  page inside the clause pages: wrong pages are set to the clause's first page, unmatched quotes are dropped and a
  problematic_clause finding left without evidence is dropped (counted in notes) — the backend rejects the WHOLE
  file for one bad quote;
- primary_domain from the fixed mapping (orchestration/scoring.py), recommendation from Agent 3 by finding_id;
- scoring = 4.5b in code.
"""
from agents.shared.output import agent_output
from common.schemas.business.outbound import AnalysisResult
from orchestration.scoring import domain_of, load_policy, normalize, score_contract

FILE_TYPES = {"pdf": "pdf", "docx": "docx", "png": "png", "jpg": "jpg", "jpeg": "jpg"}  # result document.file_type
CHECK_DOMAIN = {"CY": "cybersecurity", "PR": "data_privacy", "TP": "third_party"}
AGENT_1, AGENT_2, AGENT_3 = "agent_1_classification", "agent_2_risk_scoring", "agent_3_recommendation"
METADATA_KEYS = ("contract_type", "contract_value", "currency", "effective_date", "expiry_date", "governing_law")


def _merge_chunks(aggregate):
    clauses, languages, warnings, corrections = {}, set(), [], []
    metadata, facts = {"parties": [], **{key: None for key in METADATA_KEYS}}, {}
    last = None  # clause_id of the last clause seen (document order)
    for chunk in aggregate["chunks"]:
        out = agent_output(AGENT_1, chunk["result"])
        if out["chunk_id"] != chunk["chunk_id"]:
            raise ValueError(f"Agent 1 answered chunk {out['chunk_id']} for chunk {chunk['chunk_id']}")
        languages.add(out["language"])
        # Pages come from preprocessing, not from the model: keep each clause inside its chunk's pages
        # (TXT/DOCX have no physical pages -> page 1).
        pages = [p for p in (chunk.get("metadata") or {}).get("pages") or [] if isinstance(p, int) and p > 0] or [1]
        for position, clause in enumerate(out["clauses"]):
            clause = {**clause, "page_start": min(max(clause["page_start"], min(pages)), max(pages)),
                      "page_end": min(max(clause["page_end"], min(pages)), max(pages))}
            current = clauses.get(clause["clause_id"])
            if clause["continuation"] and (position or current is None or clause["clause_id"] != last
                                           or current["complete"]):
                # Only the first clause of a chunk may continue, and only the previous chunk's cut last clause.
                raise ValueError(f"Agent 1 continuation of {clause['clause_id']} does not follow a cut clause")
            last = clause["clause_id"]
            if current is None:
                clauses[clause["clause_id"]] = {**clause, "facts": list(clause["facts"])}
                continue
            if not clause["continuation"]:
                raise ValueError(f"Agent 1 used clause_id {clause['clause_id']} for two different clauses")
            # The clause continues across a chunk boundary: one clause, text in document order.
            current["text"] += "\n" + clause["text"]
            current["page_end"] = max(current["page_end"], clause["page_end"])
            current["complete"] = clause["complete"]
            current["facts"] += clause["facts"]
        for party in out["metadata"]["parties"]:
            if party not in metadata["parties"]:
                metadata["parties"].append(party)
        for key in METADATA_KEYS:
            if metadata[key] is None:
                metadata[key] = out["metadata"][key]
        for key, fact in out["fact_values"].items():
            known = facts.get(key)
            if known is None or (known["status"] != "FOUND" and fact["status"] == "FOUND"):
                facts[key] = {**fact, "evidence": list(fact["evidence"]) + (known["evidence"] if known else [])}
            else:
                known["evidence"] += fact["evidence"]
        warnings += out["injection_warnings"]
        corrections += out["corrections"]
    for fix in corrections:  # Agent 1 corrected an earlier chunk: last word wins
        if fix["clause_id"] in clauses:
            clauses[fix["clause_id"]].update(category=fix["category"], clause_type=fix["clause_type"])
    return clauses, languages, metadata, facts, warnings


def _evidence(items, clauses, notes):
    kept, seen = [], set()
    for item in items:
        clause = clauses.get(item["clause_id"])
        if clause is None or normalize(item["quote"]) not in normalize(clause["text"]):
            notes["dropped_evidence"] += 1
            continue
        page = item["page"] if clause["page_start"] <= item["page"] <= clause["page_end"] else clause["page_start"]
        key = (item["clause_id"], page, item["quote"])
        if key not in seen:
            seen.add(key)
            kept.append({"clause_id": item["clause_id"], "page": page, "quote": item["quote"]})
    return kept


def _references(*groups):
    seen, out = set(), []
    for group in groups:
        for ref in group:
            key = (ref["source"], ref["reference_number"])
            if key not in seen:
                seen.add(key)
                out.append({"source": ref["source"], "reference_number": ref["reference_number"]})
    return out


def build_extraction(contract, results, *, job_id, trace, policy=None):
    """contract: ContractInput; results: workflow results by agent id. Returns (extraction.v2, notes)."""
    policy = policy or load_policy()
    names = {rule["id"]: rule["name"] for rule in [*policy["rules"], *policy["missing_section_rules"]]}
    notes = {"dropped_evidence": 0, "dropped_findings": []}
    clauses, languages, metadata, facts, injection = _merge_chunks(results[AGENT_1])
    if not clauses:
        raise ValueError("Agent 1 returned no clauses: extraction.v2 needs at least one")
    risk = agent_output(AGENT_2, results[AGENT_2])
    advice = agent_output(AGENT_3, results[AGENT_3])
    recommendations = {item["finding_id"]: item for item in advice["recommendations"]}

    findings = []
    for finding in risk["findings"]:
        recommendation = recommendations.get(finding["finding_id"])
        if recommendation is None:
            raise ValueError(f"Agent 3 gave no recommendation for finding {finding['finding_id']}")
        evidence = _evidence(finding["evidence"], clauses, notes)
        problematic = finding["finding_kind"] == "problematic_clause"
        if problematic and (finding["clause_id"] not in clauses or not evidence):
            notes["dropped_findings"].append(finding["finding_id"])
            continue
        rule_id = finding["rule_id"]
        findings.append({
            "finding_id": finding["finding_id"], "finding_kind": finding["finding_kind"], "title": finding["title"],
            "primary_domain": domain_of(rule_id, policy) if rule_id else CHECK_DOMAIN[finding["check_code"][:2]],
            "category": names.get(rule_id), "severity": finding["severity"],
            "clause_id": finding["clause_id"] if problematic else None, "evidence": evidence,
            "missing_description": None if problematic else finding["missing_description"],
            "why": finding["why"], "impact": finding["impact"], "recommendation": recommendation["recommendation"],
            "references": _references(finding["references"], recommendation["references"]),
            "check_code": finding["check_code"]})
    kept = {finding["finding_id"] for finding in findings}
    checks = [{**check, "clause_ids": [c for c in check["clause_ids"] if c in clauses],
               "finding_id": check["finding_id"] if check["finding_id"] in kept else None}
              for check in risk["checks"]]

    for fact in facts.values():
        fact["evidence"] = _evidence(fact["evidence"], clauses, notes)
        if fact["status"] == "FOUND" and not fact["evidence"]:
            fact["status"] = "UNKNOWN"  # a value we cannot point to in the contract is not FOUND
    # Only verdicts whose quote is really in their clause may raise the score (same check as finding evidence).
    def quoted(item, clause_id):
        ok = clause_id in clauses and normalize(item["evidence_quote"]) in normalize(clauses[clause_id]["text"])
        notes["dropped_verdicts"] += not ok
        return ok
    notes["dropped_verdicts"] = 0
    verdicts = [{**item, "verdicts": [v for v in item["verdicts"] if quoted(v, item["clause_id"])],
                 "additional": [a for a in item["additional"] if quoted(a, item["clause_id"])]}
                for item in risk["clause_verdicts"]]
    scored = score_contract([{"clause_id": cid, "category": c["category"], "text": c["text"]} for cid, c in clauses.items()],
                            verdicts, policy)
    notes["scoring_detail"] = scored["detail"]

    context = contract.context
    preprocessing = context.get("preprocessing") or {}
    document_info = preprocessing.get("document") or {}
    file_format = str(document_info.get("format") or "").lower()
    document = {"file_type": FILE_TYPES.get(file_format, file_format or "unknown"),
                # shortcut: TXT/DOCX have no physical pages (clauses are on page 1) until the partner defines it.
                "page_count": max(1, document_info.get("page_count") or 1, max(c["page_end"] for c in clauses.values())),
                "language": languages.pop() if len(languages) == 1 else "mixed"}
    request = context.get("request") or {}  # every field ContractGuard sent (absent for internal jobs)
    if request.get("clean_file_key"):
        document["clean_file_key"] = request["clean_file_key"]
    extraction = {
        "schema_version": "2.0.0", "job_id": job_id,
        "contract_id": contract.contract_id, "contract_version": contract.contract_version,
        "document_complete": all(c["complete"] for c in clauses.values()), "document": document,
        "clauses": [{"clause_id": cid, "order": order, "number": c["number"], "heading": c["heading"], "text": c["text"],
                     "clause_type": c["clause_type"],
                     "location": {"page_start": c["page_start"], "page_end": c["page_end"], "regions": []}}
                    for order, (cid, c) in enumerate(clauses.items(), 1)],
        "metadata": metadata, "findings": findings, "checks": checks, "facts": facts,
        "injection_warnings": [{"page": max(1, w["page"]), "excerpt": w["excerpt"]} for w in injection],
        "overall_recommendation": advice["overall_recommendation"], "scoring": scored["scoring"], "trace": trace,
    }
    # TXT exists only for internal jobs (ContractGuard has no txt file_type); everything else must be a valid
    # AnalysisResult — pydantic.ValidationError is a ValueError, so an invalid result fails the job, never ships.
    if document["file_type"] in FILE_TYPES.values():
        extraction = AnalysisResult.model_validate(extraction).to_json()
    return extraction, notes
