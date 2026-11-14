"""Lazy S3 download client. Credentials follow the normal boto3 credential chain."""


class ObjectTooLarge(ValueError):
    pass


class S3Client:
    def __init__(self, *, client=None, region=None, profile=None):
        self._client, self.region, self.profile = client, region, profile

    def read(self, bucket, key, *, version_id=None, max_bytes):
        self._connect()
        request = {"Bucket": bucket, "Key": key}
        if version_id is not None:
            request["VersionId"] = version_id
        response = self._client.get_object(**request)
        return self._read_response(response, max_bytes)

    def _connect(self):
        if self._client is None:
            import boto3
            from botocore.config import Config
            self._client = boto3.Session(profile_name=self.profile).client(
                "s3", region_name=self.region,
                config=Config(connect_timeout=10, read_timeout=60,
                              retries={"mode": "standard", "total_max_attempts": 3}))

    @staticmethod
    def _read_response(response, max_bytes):
        body = response["Body"]
        try:
            if response.get("ContentLength", 0) > max_bytes:
                raise ObjectTooLarge("S3 object exceeds max_file_bytes")
            data = bytearray()
            while len(data) <= max_bytes:
                part = body.read(min(1024 * 1024, max_bytes + 1 - len(data)))
                if not part:
                    break
                data.extend(part)
            if len(data) > max_bytes:
                raise ObjectTooLarge("S3 object exceeds max_file_bytes")
            if "ContentLength" in response and len(data) != response["ContentLength"]:
                raise IOError("Incomplete S3 object download")
            return bytes(data), {k: response[k] for k in ("ETag", "VersionId", "ContentType") if k in response}
        finally:
            body.close()

    def write_json(self, bucket, key, value):
        from workers.contracts import canonical
        import hashlib
        self._connect()
        data = canonical(value).encode("utf-8")
        response = self._client.put_object(Bucket=bucket, Key=key, Body=data,
                                          ContentType="application/json; charset=utf-8")
        return {"bucket": bucket, "key": key, "sha256": hashlib.sha256(data).hexdigest(),
                **{k: response[k] for k in ("VersionId", "ETag") if k in response}}
