"""Business retry for agent stages: only transient model/service failures, with backoff.

Layers: ServiceClient retries HTTP 429/5xx/transport (3x, sub-second); agent JobStore requeues a
failed job only for RETRYABLE_JOB_ERRORS; this graph policy re-runs the node with longer backoff
(OpenAI rate limits need seconds, not milliseconds). Validation/config/model-output errors fail fast.
"""
from langgraph.types import RetryPolicy

from common.service_client import RemoteServiceError
from common.service_jobs import RETRYABLE_JOB_ERRORS


def is_transient(exc):
    if not isinstance(exc, RemoteServiceError):
        return False  # TimeoutError here is the overall job deadline: retrying cannot help
    message = str(exc)
    return message == "Remote service unavailable after retries" or any(
        message == "Remote job failed: " + name for name in RETRYABLE_JOB_ERRORS)


AGENT_STAGE_RETRY = RetryPolicy(initial_interval=5.0, backoff_factor=2.0, max_interval=60.0,
                                max_attempts=3, jitter=True, retry_on=is_transient)
