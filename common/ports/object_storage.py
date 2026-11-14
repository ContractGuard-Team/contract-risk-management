"""Bounded object download interface used by contract file retrieval."""
from typing import Protocol


class ObjectStorage(Protocol):
    def read(self, bucket: str, key: str, *, version_id: str | None = None,
             max_bytes: int) -> tuple[bytes, dict]:
        """Return bytes and service metadata; raise on errors or oversized objects."""
        ...
