"""Follow one contract job (trace_id) across services as an ordered timeline."""


def timeline(events):
    if not events:
        return None
    events = sorted(events, key=lambda e: e["timestamp_ns"])
    # A span's event is written when it ends; its start is end - duration.
    started = min(e["timestamp_ns"] - int(e.get("duration_ms", 0) * 1_000_000) for e in events)
    ended = events[-1]["timestamp_ns"]
    steps = [{"service": e["service"], "stage": e["stage"], "status": e["status"],
              "at_ms": round((e["timestamp_ns"] - started) / 1_000_000, 1),
              **{k: e[k] for k in ("duration_ms", "progress", "error_type", "input_tokens", "output_tokens", "model") if k in e}}
             for e in events]
    failure = next((s for s in steps if s["status"] == "failed"), None)
    done = any(e["service"] == "worker" and e["stage"] == "completed" and e["status"] == "completed" for e in events)
    cancelled = any(e["status"] == "cancelled" for e in events)
    first = events[0]
    return {"trace_id": first.get("trace_id"), "job_id": first.get("job_id"), "contract_id": first.get("contract_id"),
            "status": "cancelled" if cancelled else "failed" if failure else "completed" if done else "running",
            "wall_ms": round((ended - started) / 1_000_000, 1),
            "services": list(dict.fromkeys(e["service"] for e in events)),
            "first_failure": failure, "steps": steps}
