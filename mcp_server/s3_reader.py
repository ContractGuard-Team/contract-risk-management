"""Read-only S3 access for Agent 2/3: list and read objects in allow-listed buckets only.

Two locks: IAM should grant only s3:ListBucket + s3:GetObject (infrastructure/aws/mcp-s3-readonly-policy.json),
and this client refuses every other S3 operation even if the credentials could do more.
"""
import codecs
import os

from common.exceptions import IntegrationNotConfigured

READ_ONLY_OPERATIONS = {"ListObjectsV2", "GetObject", "HeadObject"}


def _refuse_writes(model, **_):
    if model.name not in READ_ONLY_OPERATIONS:
        raise PermissionError(f"S3 {model.name} refused: MCP S3 access is read-only")


def _valid_key(value, name):
    if not isinstance(value, str) or value.startswith("/") or len(value) > 1024:
        raise ValueError(f"Invalid S3 {name}")
    return value


class S3ReadOnly:
    def __init__(self, buckets=None, *, client=None, max_bytes=1_000_000):
        raw = os.getenv("MCP_S3_BUCKETS", "") if buckets is None else ",".join(buckets)
        self.buckets = [b.strip() for b in raw.split(",") if b.strip()]
        if not self.buckets:
            raise IntegrationNotConfigured("TODO: set MCP_S3_BUCKETS to the reference buckets agents may read")
        if client is None:
            import boto3
            client = boto3.client("s3", region_name=os.getenv("AWS_REGION") or None)
        client.meta.events.register("before-call.s3", _refuse_writes)
        self.client, self.max_bytes = client, max_bytes

    def _bucket(self, bucket):
        if not bucket and len(self.buckets) == 1:
            return self.buckets[0]  # one configured bucket: agents need not know its name
        if bucket not in self.buckets:
            raise PermissionError("Bucket is not in MCP_S3_BUCKETS")
        return bucket

    def list_objects(self, bucket="", prefix="", max_keys=100, continuation_token=None):
        bucket = self._bucket(bucket)
        request = {"Bucket": bucket, "Prefix": _valid_key(prefix, "prefix"), "MaxKeys": max(1, min(int(max_keys), 1000))}
        if continuation_token:
            request["ContinuationToken"] = continuation_token
        page = self.client.list_objects_v2(**request)
        return {"bucket": bucket, "prefix": prefix,
                "objects": [{"key": o["Key"], "size": o["Size"], "last_modified": o["LastModified"].isoformat()}
                            for o in page.get("Contents", [])],
                "truncated": page.get("IsTruncated", False), "next_token": page.get("NextContinuationToken")}

    def get_text(self, bucket, key):
        bucket = self._bucket(bucket)
        if not _valid_key(key, "key"):
            raise ValueError("Invalid S3 key")
        obj = self.client.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{self.max_bytes}")
        data = obj["Body"].read(self.max_bytes + 1)
        truncated = len(data) > self.max_bytes
        result = {"bucket": bucket, "key": key, "content_type": obj.get("ContentType", ""), "truncated": truncated}
        try:
            # final=False when truncated: a multi-byte character cut at the limit is not an error.
            result["text"] = codecs.getincrementaldecoder("utf-8")().decode(data[:self.max_bytes], final=not truncated)
        except UnicodeDecodeError:
            result.update(text=None, binary=True)  # e.g. PDF/DOCX: needs the document pipeline, not raw bytes
        return result
