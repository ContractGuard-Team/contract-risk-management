"""Aggregate sanitized events: latency, errors, tokens, throughput and failed jobs per time window."""
from collections import Counter
import math


def _percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def summarize(events, *, now_ns, window_minutes=60):
    cutoff = now_ns - int(window_minutes * 60 * 1_000_000_000)
    # Rows written before sanitize() existed may lack fields; they are history, not metrics.
    rows = [e for e in events if e.get("timestamp_ns", 0) >= cutoff and {"service", "stage", "status"} <= e.keys()]
    stages, tokens, errors, failed_jobs = {}, {}, Counter(), []
    for e in rows:
        stat = stages.setdefault(e["service"] + "." + e["stage"], {"count": 0, "completed": 0, "failed": 0, "durations": []})
        stat["count"] += 1
        if e["status"] in ("completed", "failed"):
            stat[e["status"]] += 1
        if "duration_ms" in e:
            stat["durations"].append(e["duration_ms"])
        if "input_tokens" in e or "output_tokens" in e:
            usage = tokens.setdefault(e["service"] + ("/" + e["model"] if e.get("model") else ""),
                                      {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
            usage["calls"] += 1
            for key in ("input_tokens", "output_tokens"):
                usage[key] += e.get(key, 0)
            usage["total_tokens"] += e.get("total_tokens", e.get("input_tokens", 0) + e.get("output_tokens", 0))
        if e["status"] == "failed":
            errors[e.get("error_type", "unknown")] += 1
            if e.get("job_id") and e["stage"] in ("job", "pipeline"):
                failed_jobs.append({k: e[k] for k in ("job_id", "contract_id", "service", "error_type", "timestamp_ns") if k in e})
    for stat in stages.values():
        durations = stat.pop("durations")
        finished = stat["completed"] + stat["failed"]
        stat["error_rate"] = round(stat["failed"] / finished, 4) if finished else 0.0
        if durations:
            stat.update(avg_ms=round(sum(durations) / len(durations), 1), p95_ms=round(_percentile(durations, 0.95), 1),
                        max_ms=round(max(durations), 1))
    worker = Counter((e["stage"], e["status"]) for e in rows if e["service"] == "worker")
    return {"window_minutes": window_minutes, "event_count": len(rows),
            "jobs": {"completed": worker[("completed", "completed")], "failed": worker[("pipeline", "failed")]},
            "stages": dict(sorted(stages.items())), "tokens": tokens, "errors_by_type": dict(errors),
            "failed_jobs": sorted(failed_jobs, key=lambda j: j["timestamp_ns"], reverse=True)[:50]}
