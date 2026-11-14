"""Push job observations to the worker; collector SQLite is the durable outbox."""
import os
from common.service_client import ServiceClient


def job_scoped(event):
    return all(event.get(key) for key in ("job_id", "runtime_id", "contract_id", "trace_id", "request_id"))


class WorkerExporter:
    def __init__(self, client=None):
        self.active = os.getenv("MONITORING_WORKER_ENABLED", "false").lower() == "true"
        self.client = client
        if self.active and client is None:
            self.client = ServiceClient(os.environ["MONITORING_WORKER_URL"], timeout=5)

    def export(self, event):
        response = self.client.request("POST", "/v1/monitoring-events", json=event, request_id=event["request_id"])
        if response.get("accepted") is not True or response.get("event_id") != event["event_id"]:
            raise ValueError("Worker did not acknowledge the monitoring event")

    def close(self):
        if self.client is not None:
            self.client.close()
