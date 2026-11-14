"""S3 holds the original documents only, ordered by law number: legal/QH15/2024/042-2024-QH15/source/v1_<sha8>_<file>

Originals are never overwritten (the key carries version + hash). Extracted text, metadata and run logs live in
Supabase; vectors in Qdrant. The processor never reads S3: it gets the bytes from the collector.
"""
import mimetypes
import re

SYMBOL = re.compile(r"^(\d+)/(\d{4})/(QH\d+)$")


def law_prefix(symbol, doc_class, issued_on, docid, root="legal"):
    match = SYMBOL.match((symbol or "").strip())
    if match:
        number, year, session = match.groups()
        return f"{root}/{session}/{year}/{int(number):03d}-{year}-{session}"
    year = (issued_on or "0000")[:4]
    if doc_class == "constitution":
        return f"{root}/hien-phap/{year}/{symbol if (symbol or '').startswith('HP-') else docid}"
    return f"{root}/khac/{year}/{docid}"


def safe_name(url):
    name = url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name)[:120] or "document.pdf"


class S3Archiver:
    def __init__(self, bucket, *, client=None, region=None):
        if client is None:
            import boto3
            client = boto3.client("s3", region_name=region)
        self.bucket, self.client = bucket, client

    def _exists(self, key):
        from botocore.exceptions import ClientError
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def put_source(self, prefix, version, sha256, filename, data):
        key = f"{prefix}/source/v{version}_{sha256[:8]}_{filename}"
        if not self._exists(key):  # never overwrite an original
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                                   ContentType=mimetypes.guess_type(filename)[0] or "application/octet-stream",
                                   Metadata={"sha256": sha256})
        return key
