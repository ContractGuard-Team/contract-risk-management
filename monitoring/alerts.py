"""Alert rules over sanitized events; deduplicated, stored, printed and optionally sent to a webhook.

SQS backlog/age is measured by AWS, not by this collector: use a CloudWatch alarm on
ApproximateAgeOfOldestMessage / ApproximateNumberOfMessagesVisible (see docs/monitoring.md).
"""
import json
import os
from urllib.parse import urlsplit


def thresholds():
    return {"slow_stage_ms": float(os.getenv("ALERT_SLOW_STAGE_MS", "900000")),
            "throttle_count": int(os.getenv("ALERT_THROTTLE_COUNT", "3")),
            "throttle_window_ns": int(float(os.getenv("ALERT_THROTTLE_WINDOW_MINUTES", "10")) * 60 * 1_000_000_000)}


def evaluate(event, *, count_recent_throttles, limits=None):
    """Return alerts {key, rule, severity, summary, event fields} raised by one event."""
    limits = limits or thresholds()
    facts = {k: event[k] for k in ("service", "stage", "status", "job_id", "contract_id", "trace_id", "document",
                                   "error_type", "duration_ms", "timestamp_ns") if k in event}
    alerts = []
    if event["status"] == "failed" and event.get("job_id") and event["stage"] in ("job", "pipeline"):
        # One failed contract fails agent -> langgraph -> worker; the first report wins.
        alerts.append({"key": "job_failed:" + event["job_id"], "rule": "job_failed", "severity": "error",
                       "summary": f"Job {event['job_id']} failed in {event['service']} ({event.get('error_type', 'unknown')})"})
    if event.get("error_type") == "TransientModelError":
        window = event["timestamp_ns"] // limits["throttle_window_ns"]
        if count_recent_throttles(event["timestamp_ns"] - limits["throttle_window_ns"]) >= limits["throttle_count"]:
            alerts.append({"key": f"model_throttled:{window}", "rule": "model_throttled", "severity": "warning",
                           "summary": f"Model API (Gemini) throttled/unavailable >= {limits['throttle_count']} times in the window"})
    if event["service"].startswith("data-"):
        # Legal data pipeline: a crawl run that did not finish cleanly, or a document given up after retries.
        if event["stage"] == "run" and event["status"] in ("failed", "incomplete"):
            alerts.append({"key": "data_run:" + event.get("trace_id", event["event_id"]), "rule": "data_run_" + event["status"],
                           "severity": "error" if event["status"] == "failed" else "warning",
                           "summary": f"Data pipeline run {event['status']} ({event.get('error_type', 'see logs')})"})
        if event["stage"] == "process" and event["status"] == "failed":
            alerts.append({"key": "data_document_failed:" + event.get("document", event["event_id"]),
                           "rule": "data_document_failed", "severity": "error",
                           "summary": f"Data pipeline gave up on {event.get('document', 'a document')} ({event.get('error_type', 'unknown')})"})
        if event["stage"] == "sqs_message" and event["status"] == "kept":
            alerts.append({"key": "data_sqs_kept:" + event["event_id"], "rule": "data_sqs_retry", "severity": "warning",
                           "summary": "Data collector run failed; the SQS message stays for redelivery"})
    long_by_design = event["service"].startswith("data-") and event["stage"] != "process"  # a full crawl takes hours
    if event["status"] == "completed" and event.get("duration_ms", 0) >= limits["slow_stage_ms"] and not long_by_design:
        alerts.append({"key": f"slow_stage:{event['service']}:{event['stage']}:{event.get('job_id') or event['event_id']}",
                       "rule": "slow_stage", "severity": "warning",
                       "summary": f"{event['service']}.{event['stage']} took {round(event['duration_ms'] / 1000)} s"})
    return [{**alert, "event": facts} for alert in alerts]


class WebhookNotifier:
    """POST {"text", "alert"} (Slack/Teams-compatible text) to ALERT_WEBHOOK_URL when configured."""
    def __init__(self, url=None, client=None):
        self.url = os.getenv("ALERT_WEBHOOK_URL", "") if url is None else url
        if self.url:
            parts = urlsplit(self.url)
            if parts.scheme != "https" or not parts.hostname:
                raise ValueError("ALERT_WEBHOOK_URL must be an https URL")
        self.active = bool(self.url)
        self.client = client
        if self.active and client is None:
            import httpx
            self.client = httpx.Client(timeout=10, follow_redirects=False)

    def send(self, alert):
        response = self.client.post(self.url, json={"text": f"[{alert['severity'].upper()}] {alert['summary']}",
                                                    "alert": alert})
        response.raise_for_status()

    def close(self):
        if self.client is not None:
            self.client.close()


def log_line(alert):
    return json.dumps({"service": "monitoring", "alert": alert["rule"], "severity": alert["severity"],
                       "summary": alert["summary"], "key": alert["key"]}, ensure_ascii=False)
