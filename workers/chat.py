"""ContractGuard chatbot loop (ContractGuard docs/04 C6, docs/07 §11): chat-requests -> agent-3 -> analysis-results.

Runs in its own process next to intake, so a question never waits behind a long analysis. One message at a time:
read results/<job_id>/request.json (chat-request.v1), ask agent-3 (POST /v1/chat; it holds the model key), publish
chat_answered on the results queue, then delete the message. The backend gives up after 3 minutes, so a failure is
answered at once with `failed` and the message is deleted rather than retried after its visibility timeout.
"""
import json
import logging

from common.schemas.business.inbound import ChatInboundMessage, ChatRequest
from workers.partner import ERR_PROCESSING, check_outbound

log = logging.getLogger(__name__)

VISIBILITY_SECONDS = 240         # longer than one answer (the backend waits 180 s), below the queue's 300 s
MAX_REQUEST_BYTES = 2 * 1024 * 1024


class ChatWorker:
    def __init__(self, queue, results, storage, bucket, agent):
        self.queue, self.results, self.storage, self.bucket, self.agent = queue, results, storage, bucket, agent

    def serve(self, stop):
        while not stop.is_set():
            for message in self.queue.receive(visibility=VISIBILITY_SECONDS):
                self.handle(message)

    def handle(self, message):
        job_id = None
        try:
            body = json.loads(message["Body"])
            job_id = body.get("job_id") if isinstance(body, dict) else None
            asked = ChatInboundMessage.validate_python(body)
            data, _ = self.storage.read(self.bucket, asked.request_key, max_bytes=MAX_REQUEST_BYTES)
            request = ChatRequest.model_validate_json(data)
            if request.job_id != asked.job_id:
                raise ValueError("request.json belongs to another job_id")
            reply = self.agent.request("POST", "/v1/chat", json=json.loads(data), request_id=asked.job_id)
            clause_ids = {c.clause_id for c in request.clauses}
            cited = [c for c in reply.get("cited_clause_ids") or [] if c in clause_ids]
            self.publish({"message_version": "1", "event_type": "chat_answered", "job_id": asked.job_id,
                          "answer_text": reply.get("answer_text"), "cited_clause_ids": cited})
        except Exception as exc:  # never the question or contract text in logs or in the failure reason
            log.warning("Chat question %s failed: %s", job_id, type(exc).__name__)
            if isinstance(job_id, str) and job_id:
                try:
                    self.publish({"message_version": "1", "event_type": "failed", "job_id": job_id,
                                  "failure_reason": f"Chat answer failed: {type(exc).__name__}",
                                  "error_code": ERR_PROCESSING})
                except Exception as publish_error:
                    log.warning("Could not report chat failure %s: %s", job_id, type(publish_error).__name__)
        self.queue.delete(message["ReceiptHandle"])

    def publish(self, message):
        self.results.send(json.dumps(check_outbound(message), ensure_ascii=False))
