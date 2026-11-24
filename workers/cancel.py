"""Cancel a contract job: the user pressed cancel.

ContractJobStore.cancel() writes the job's `cancelled` log. The intake process checks it every second, stops, tells the
services to drop the job, answers the results queue and takes the next contract. Two ways in, same function:
- HTTP  POST worker:8000/v1/jobs/{job_id}/cancel (Bearer INTERNAL_API_TOKEN, internal network only);
- SQS   WORKER_CANCEL_QUEUE_URL, message CancelRequested (common/schemas/business/inbound.py).
"""
import logging

from pydantic import ValidationError

from common.schemas.business.inbound import CancelRequested

log = logging.getLogger(__name__)


def attach(app, store):
    from fastapi import HTTPException

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        try:
            return {"job_id": job_id, "status": store.cancel(job_id)}
        except ValueError:
            raise HTTPException(400, "Invalid job_id") from None


def serve_queue(queue, store, stop):
    while not stop.is_set():
        try:
            for message in queue.receive(visibility=30):
                handle(queue, store, message)
        except Exception as exc:  # SQS/DynamoDB unavailable: the message comes back after its visibility timeout
            log.error("Cancel request not handled (%s); SQS will redeliver", type(exc).__name__)
            stop.wait(1)


def handle(queue, store, message):
    try:
        job_id = CancelRequested.model_validate_json(message["Body"]).job_id
    except ValidationError:
        log.warning("Invalid cancel message dropped")
    else:
        log.info("Cancel %s: %s", job_id, store.cancel(job_id))
    queue.delete(message["ReceiptHandle"])
