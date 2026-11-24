"""Observe existing workflow/model spans without exporting their content."""
from contextlib import contextmanager
import time
from monitoring.observer import NullObserver, NullObservation
from monitoring.client import emit
from common.exceptions import JobCancelled


class SafeObservation(NullObservation):
    def __init__(self):
        self.fields = {}

    def update(self, **kwargs):
        usage = kwargs.get("usage_details", {})
        for source, target in (("input", "input_tokens"), ("output", "output_tokens"), ("total", "total_tokens"),
                               ("input_cache_read", "cache_read_tokens"), ("output_reasoning", "reasoning_tokens")):
            value = usage.get(source)
            if type(value) in (int, float) and value >= 0:
                self.fields[target] = value
        # input/output/model_parameters are deliberately never forwarded.


class ServiceObserver(NullObserver):
    def __init__(self, service, identity, progress=None):
        self.service, self.identity, self.progress = service, identity, progress

    @contextmanager
    def span(self, name, *, kind="span", metadata=None, trace_id=None, model=None):
        stage, percentage = None, None
        if name.startswith("agent_1"):
            stage, percentage = "agent_1", 30
        elif name.startswith("agent_2"):
            stage, percentage = "agent_2", 60
        elif name.startswith("agent_3"):
            stage, percentage = "agent_3", 85
        # <agent>.reflect runs after Agent 3 (long-term memory); reporting it would move the stage backwards.
        if self.progress and stage and not name.endswith(".reflect"):
            self.progress(stage, percentage)
        observation, start = SafeObservation(), time.monotonic()
        if model:
            observation.fields["model"] = str(model)
        try:
            yield observation
        except Exception as exc:
            emit(self.service, name, "cancelled" if isinstance(exc, JobCancelled) else "failed", **self.identity, **observation.fields,
                 error_type=type(exc).__name__, duration_ms=(time.monotonic()-start)*1000)
            raise
        else:
            emit(self.service, name, "completed", **self.identity, **observation.fields,
                 duration_ms=(time.monotonic()-start)*1000)
