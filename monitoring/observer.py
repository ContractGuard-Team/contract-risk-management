"""In-process observation interface for workflow progress; no external exporters."""
from contextlib import contextmanager


class NullObservation:
    def update(self, **kwargs):
        pass

    def summary(self, *, input=None, output=None):
        pass


class NullObserver:
    @contextmanager
    def attributes(self, *, session_id=None, tags=None, trace_name=None):
        yield

    @contextmanager
    def span(self, name, *, kind="span", metadata=None, trace_id=None, model=None):
        yield NullObservation()

    def flush(self):
        pass
