from contextlib import contextmanager
import logging
from monitoring.config import LangfuseSettings, PROJECT_ENV
from monitoring.privacy import make_export_mask, redact

logger = logging.getLogger(__name__)


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

    def shutdown(self):
        pass


class SafeObservation:
    def __init__(self, observation, capture_content):
        self.observation = observation
        self.capture_content = capture_content

    def update(self, **kwargs):
        if not self.capture_content:
            kwargs.pop("input", None)
            kwargs.pop("output", None)
        try:
            self.observation.update(**redact(kwargs))
        except Exception as exc:
            logger.warning("Langfuse update failed: %s", type(exc).__name__)

    def summary(self, *, input=None, output=None):
        """Caller supplies safe structural summaries, never contract text."""
        values = {key: value for key, value in (("input", input), ("output", output)) if value is not None}
        try:
            self.observation.update(**redact(values))
        except Exception as exc:
            logger.warning("Langfuse summary failed: %s", type(exc).__name__)


class LangfuseObserver(NullObserver):
    def __init__(self, client, *, capture_content=False):
        self.client = client
        self.capture_content = capture_content

    @classmethod
    def from_env(cls, env_file=PROJECT_ENV):
        settings = LangfuseSettings.load(env_file)
        if not settings.enabled:
            return NullObserver()
        from langfuse import Langfuse
        return cls(Langfuse(public_key=settings.public_key, secret_key=settings.secret_key,
                           base_url=settings.base_url, environment=settings.environment,
                           release=settings.release, timeout=10,
                           mask_otel_spans=make_export_mask(settings.secret_key, settings.public_key)),
                   capture_content=settings.capture_content)

    @contextmanager
    def attributes(self, *, session_id=None, tags=None, trace_name=None):
        manager = None
        try:
            from langfuse import propagate_attributes
            manager = propagate_attributes(session_id=session_id, tags=tags, trace_name=trace_name)
            manager.__enter__()
        except Exception as exc:
            manager = None
            logger.warning("Langfuse propagation failed: %s", type(exc).__name__)
        try:
            yield
        finally:
            if manager is not None:
                try:
                    manager.__exit__(None, None, None)
                except Exception as exc:
                    logger.warning("Langfuse propagation end failed: %s", type(exc).__name__)

    @contextmanager
    def span(self, name, *, kind="span", metadata=None, trace_id=None, model=None):
        manager = None
        observation = NullObservation()
        try:
            args = {"name": name, "as_type": kind, "metadata": redact(metadata or {})}
            if model:
                args["model"] = model
            if trace_id:
                args["trace_context"] = {"trace_id": trace_id}
            manager = self.client.start_as_current_observation(**args)
            observation = SafeObservation(manager.__enter__(), self.capture_content)
        except Exception as exc:
            manager = None
            logger.warning("Langfuse start failed: %s", type(exc).__name__)
        try:
            yield observation
        except BaseException as exc:
            # Log error type, not exception text which may contain contract data.
            observation.update(level="ERROR", status_message=type(exc).__name__)
            raise
        finally:
            if manager is not None:
                try:
                    manager.__exit__(None, None, None)
                except Exception as exc:
                    logger.warning("Langfuse end failed: %s", type(exc).__name__)

    def flush(self):
        try:
            self.client.flush()
        except Exception as exc:
            logger.warning("Langfuse flush failed: %s", type(exc).__name__)

    def shutdown(self):
        try:
            self.client.shutdown()
        except Exception as exc:
            logger.warning("Langfuse shutdown failed: %s", type(exc).__name__)
