"""Passive file payload shared by components; importing it starts no processing."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RetrievedFile:
    data: bytes
    filename: str
    metadata: dict = field(default_factory=dict)
