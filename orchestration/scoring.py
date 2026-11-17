"""Deterministic 4.5b risk scoring (ContractGuard policy 1.2.2) computed in code, never by the model.

Input: Agent 1 clauses (clause_id, category, text) + Agent 2 clause_verdicts. Policy: the same file Agent 2 reads
(agents/agent_2_risk_scoring/prompts/rules/risk_policy.json). Output: extraction.v2 `scoring`
(rule_version, overall_score, risk_level, 7 domains) plus the 4.5b detail kept in the internal result.

Formula (Risk_Agent/02 §E): per rule r with n confirmed clauses P_r = w_r * (1 + bonus * (min(n, cap) - 1));
each effective missing-section rule adds w once; S_g = sum per group; T = sum S_g;
risk_score = min(100, floor(T + 0.5)); levels from overall_levels / group_levels; upper bound uses n + pending.
Domain mapping (fixed, agreed 2026-11-17): group -> domain, except the bao_mat rules R17 -> data_privacy and
R01/R02 -> legal. extraction.v2 domain score = min(100, floor(S_d + 0.5)) with group_levels; a domain without any
4.5b rule (cybersecurity) is not applicable.
"""
from collections import defaultdict
import json
import math
from pathlib import Path
import re
import unicodedata

POLICY_PATH = Path(__file__).resolve().parents[1] / "agents/agent_2_risk_scoring/prompts/rules/risk_policy.json"
DOMAINS = ("legal", "financial", "operational", "compliance", "cybersecurity", "data_privacy", "third_party")
DOMAIN_OF_GROUP = {"phap_ly": "legal", "tai_chinh": "financial", "van_hanh": "operational", "tuan_thu": "compliance",
                   "ben_thu_ba": "third_party"}
DOMAIN_OF_RULE = {"R17": "data_privacy", "R01": "legal", "R02": "legal"}  # group bao_mat is split by meaning


def load_policy(path=POLICY_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def normalize(text) -> str:
    """Policy `matching`: NFKC, straight quotes, whitespace/newlines collapsed (case handled by the caller)."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "‚": "'"}))
    return " ".join(text.split())


def domain_of(rule_id, policy) -> str:
    if rule_id in DOMAIN_OF_RULE:
        return DOMAIN_OF_RULE[rule_id]
    rules = {r["id"]: r for r in [*policy["rules"], *policy["missing_section_rules"]]}
    return DOMAIN_OF_GROUP[rules[rule_id]["group"]]


def half_up(value) -> int:
    return math.floor(value + 0.5)


def _level(points, levels) -> str:
    for step in sorted(levels, key=lambda item: -item["min_points"]):
        if points >= step["min_points"]:
            return step["level"]
    return levels[-1]["level"]


def score_contract(clauses, clause_verdicts, policy=None) -> dict:
    """clauses: [{clause_id, category, text}]; clause_verdicts: Agent 2 output. Returns
    {"scoring": extraction.v2 scoring, "detail": 4.5b counts/points for the internal result}."""
    policy = policy or load_policy()
    rules = {r["id"]: r for r in policy["rules"]}
    weights, repeat = policy["severity_points"], policy["repeat"]
    category = {c["clause_id"]: c["category"] for c in clauses}
    excluded = set(policy.get("scan_excluded_categories", []))
    confirmed, pending = defaultdict(set), defaultdict(set)
    for item in clause_verdicts:
        cat = category.get(item["clause_id"])
        if cat is None or cat in excluded:
            continue  # unknown clause or category "other": never confirmed (Risk_Agent/02 §B)
        marks = [(v["rule_id"], v["verdict"]) for v in item["verdicts"]] + [(a["rule_id"], "confirmed") for a in item["additional"]]
        for rule_id, verdict in marks:
            if rule_id not in rules or cat not in rules[rule_id]["cats"]:
                continue  # rule not defined for this category: not counted
            if verdict == "confirmed":
                confirmed[rule_id].add(item["clause_id"])
            elif verdict == "needs_review":
                pending[rule_id].add(item["clause_id"])
    for rule_id in list(pending):
        pending[rule_id] -= confirmed.get(rule_id, set())
    present = {c["category"] for c in clauses}
    text = normalize(" ".join(c["text"] for c in clauses)).lower()
    missing, uncertain = [], []
    for rule in policy["missing_section_rules"]:
        if rule["cat"] in present:
            continue
        # Category absent but the wording is there: not scored, a person must look (Risk_Agent/01 B.2).
        (uncertain if re.search(rule["fallback"], text, re.IGNORECASE) else missing).append(rule)

    def totals(counts):
        groups, domains, points = defaultdict(float), defaultdict(float), []
        for rule_id in sorted(r for r in counts if counts[r]):
            n = len(counts[rule_id])
            value = weights[rules[rule_id]["sev"]] * (1 + repeat["bonus"] * (min(n, repeat["cap"]) - 1))
            groups[rules[rule_id]["group"]] += value
            domains[domain_of(rule_id, policy)] += value
            points.append({"rule_id": rule_id, "group": rules[rule_id]["group"], "clauses": n, "points": value})
        for rule in missing:
            value = weights[rule["sev"]]
            groups[rule["group"]] += value
            domains[domain_of(rule["id"], policy)] += value
            points.append({"rule_id": rule["id"], "group": rule["group"], "clauses": 0, "points": value})
        return groups, domains, points

    groups, domains, rule_points = totals(confirmed)
    upper_counts = {r: confirmed.get(r, set()) | pending.get(r, set()) for r in set(confirmed) | set(pending)}
    upper_total = sum(totals(upper_counts)[0].values())
    total = sum(groups.values())
    scored_domains = {domain_of(r, policy) for r in [*rules, *(m["id"] for m in policy["missing_section_rules"])]}
    domain_rows = []
    for domain in DOMAINS:
        if domain not in scored_domains:
            domain_rows.append({"domain": domain, "applicable": False, "score": None, "risk_level": None,
                                "na_reason": f"Chính sách 4.5b {policy['policy_version']} chưa có rule cho domain này."})
            continue
        points = domains.get(domain, 0.0)
        domain_rows.append({"domain": domain, "applicable": True, "score": min(100, half_up(points)),
                            "risk_level": _level(points, policy["group_levels"]), "na_reason": None})
    pending_review = sum(len(v) for v in pending.values())
    return {
        "scoring": {"rule_version": f"4.5b-{policy['policy_version']}", "overall_score": min(100, half_up(total)),
                    "risk_level": _level(total, policy["overall_levels"]), "domains": domain_rows},
        "detail": {"risk_score_upper_bound": min(100, half_up(upper_total)),
                   "risk_level_upper_bound": _level(upper_total, policy["overall_levels"]),
                   "group_scores": {g["id"]: {"points": round(groups.get(g["id"], 0.0), 1),
                                              "level": _level(groups.get(g["id"], 0.0), policy["group_levels"])}
                                    for g in policy["groups"]},
                   "rule_points": rule_points, "pending_review": pending_review,
                   "confirmed_rule_counts": {r: len(v) for r, v in confirmed.items() if v},
                   "pending_rule_counts": {r: len(v) for r, v in pending.items() if v},
                   "missing_section_rules": [m["id"] for m in missing],
                   "missing_section_uncertain": [m["id"] for m in uncertain],
                   "basis": "partial_pending_review" if pending_review else
                            ("verified_with_gaps" if uncertain else "verified")},
    }
