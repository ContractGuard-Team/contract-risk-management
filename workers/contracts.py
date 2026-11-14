"""Worker-owned wire data and errors; no imports from other application packages."""
from dataclasses import dataclass, field
import json


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


class SourceError(ValueError):
    pass


class ConnectionConfigurationError(ValueError):
    pass


class RemoteJobFailed(RuntimeError):
    pass


class TransientRemoteError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadedFile:
    data: bytes
    filename: str
    metadata: dict = field(default_factory=dict)
