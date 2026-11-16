"""Lazy SQS transport with long polling and explicit acknowledgement."""
class SQSClient:
    def __init__(self, queue_url, *, client=None, region=None):
        self.queue_url, self._client, self.region = queue_url, client, region

    def _connect(self):
        if self._client is None:
            import boto3
            from botocore.config import Config
            self._client = boto3.client("sqs", region_name=self.region,
                config=Config(connect_timeout=10, read_timeout=30,
                              retries={"mode": "standard", "total_max_attempts": 3}))
        return self._client

    def receive(self, visibility=120):
        return self._connect().receive_message(QueueUrl=self.queue_url, MaxNumberOfMessages=1,
            WaitTimeSeconds=20, VisibilityTimeout=visibility,
            MessageSystemAttributeNames=["ApproximateReceiveCount"]).get("Messages", [])

    def extend(self, receipt, seconds):
        self._connect().change_message_visibility(QueueUrl=self.queue_url,
            ReceiptHandle=receipt, VisibilityTimeout=seconds)

    def send(self, body):
        self._connect().send_message(QueueUrl=self.queue_url, MessageBody=body)

    def delete(self, receipt):
        self._connect().delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt)
