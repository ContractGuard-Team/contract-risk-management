"""Keep the queue receipt invisible and fence ownership with a DynamoDB lease."""
from threading import Event, Thread
import time


class Heartbeat:
    def __init__(self, queue, store, receipt, job_id, runtime_id, owner, settings):
        self.queue, self.store, self.receipt = queue, store, receipt
        self.job_id, self.runtime_id, self.owner, self.settings = job_id, runtime_id, owner, settings
        self.done = Event()
        self.error = None
        self.started = time.monotonic()
        self.thread = Thread(target=self._run, name="job-lease", daemon=True)

    def check(self):
        if self.error is not None:
            raise RuntimeError("Job heartbeat failed") from self.error
        if time.monotonic() - self.started >= self.settings.max_job_seconds:
            raise TimeoutError("Job exceeded its execution window")

    def tick(self):
        self.check()
        self.store.renew(self.job_id, self.runtime_id, self.owner, self.settings.lease_seconds)
        self.queue.extend(self.receipt, self.settings.visibility_seconds)

    def _run(self):
        while not self.done.wait(self.settings.heartbeat_seconds):
            try:
                self.tick()
            except Exception as exc:
                self.error = exc
                return

    def __enter__(self):
        self.tick()
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.done.set()
        self.thread.join()
