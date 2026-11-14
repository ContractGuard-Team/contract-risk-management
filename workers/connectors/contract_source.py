"""Retrieve only explicit local paths or S3 keys; never follow arbitrary remote URLs."""
import hashlib
from pathlib import Path, PurePosixPath

from workers.schemas import validate_source
from workers.contracts import SourceError
from workers.contracts import DownloadedFile
from workers.connectors.s3_client import ObjectTooLarge, S3Client


class ContractSource:
    def __init__(self, *, storage=None, local_root=None, max_file_bytes=20 * 1024 * 1024):
        self.storage = storage
        self.local_root = Path(local_root).resolve() if local_root is not None else None
        if type(max_file_bytes) is not int or max_file_bytes <= 0:
            raise SourceError("max_file_bytes must be a positive integer")
        self.max_file_bytes = max_file_bytes

    def load(self, source: dict) -> DownloadedFile:
        validate_source(source)
        maximum = self.max_file_bytes
        if source["type"] == "local":
            path = Path(source["path"])
            if self.local_root is not None and not path.is_absolute():
                path = self.local_root / path
            path = path.resolve()
            if self.local_root is not None and not path.is_relative_to(self.local_root):
                raise SourceError("Local source is outside the permitted upload directory")
            try:
                if not path.is_file():
                    raise SourceError("Local source is not a regular file")
                with path.open("rb") as stream:
                    data = stream.read(maximum + 1)
            except OSError as exc:
                raise SourceError("Unable to read local source file") from exc
            filename = path.name
            metadata = {"type": "local", "filename": filename}
        else:
            storage = self.storage if self.storage is not None else S3Client()
            try:
                data, service = storage.read(source["bucket"], source["key"],
                                             version_id=source.get("version_id"), max_bytes=maximum)
            except ObjectTooLarge as exc:
                raise SourceError(str(exc)) from exc
            filename = PurePosixPath(source["key"]).name
            metadata = {"type": "s3", "bucket": source["bucket"], "key": source["key"],
                        "requested_version_id": source.get("version_id"), "service": service}
        if not isinstance(data, bytes) or not data:
            raise SourceError("Source file is empty or invalid")
        if len(data) > maximum:
            raise SourceError("File exceeds max_file_bytes")
        metadata.update(filename=filename, file_size=len(data),
                        file_sha256=hashlib.sha256(data).hexdigest())
        return DownloadedFile(data, filename, metadata)
