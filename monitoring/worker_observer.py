"""Send workflow progress directly to the durable progress worker over local IPC."""
from contextlib import contextmanager
from monitoring.observer import NullObservation, NullObserver


STAGES = {
    "processing": ("processing", 10, 25),
    "agent_1_classification.chunk": ("agent_1", 30, 60),
    "aggregate_classification": ("agent_1_aggregate", 60, 65),
    "agent_2_risk_scoring.stage": ("agent_2", 65, 80),
    "agent_3_recommendation.stage": ("agent_3", 80, 95),
}


class ProgressObservation(NullObservation):
    def __init__(self):
        self.counts = {}

    def summary(self, *, input=None, output=None):
        for summary in (input, output):
            if isinstance(summary, dict):
                for key in ("chunk_index", "chunk_count", "cache_hit"):
                    if key in summary:
                        self.counts[key] = summary[key]


class WorkerObserver(NullObserver):
    """Progress uses synchronous IPC without a telemetry SDK or cloud callback.

    Errors writing progress propagate so SQS can replay the job.
    """
    def __init__(self, events):
        self.events = events

    @contextmanager
    def span(self, name, *, metadata=None, **kwargs):
        observation = ProgressObservation()
        stage = STAGES.get(name)
        if stage:
            self.events.emit(stage[0], progress=stage[1], details={"state": "started"})
        yield observation
        if stage:
            progress = stage[2]
            if name == "agent_1_classification.chunk":
                index = observation.counts.get("chunk_index", 0)
                count = observation.counts.get("chunk_count", 1)
                progress = 30 + int(30 * (index + 1) / max(count, 1))
            self.events.emit(stage[0], progress=progress,
                             details={"state": "completed", **observation.counts})
