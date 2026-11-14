"""Text preprocessing contracts; source offsets refer to the supplied text, not PDF bytes."""
from dataclasses import dataclass, field

from common.schemas.files import RetrievedFile


class JobError(ValueError):
    """Invalid preprocessing input or an unsatisfiable chunk budget."""


@dataclass(frozen=True)
class ChunkOptions:
    max_tokens: int = 512
    overlap_tokens: int = 64
    min_tokens: int = 80
    prepend_heading: bool = True

    def validate(self):
        for name in ("max_tokens", "overlap_tokens", "min_tokens"):
            if type(getattr(self, name)) is not int:
                raise JobError(f"options.{name} must be an integer")
        if self.max_tokens < 32:
            raise JobError("options.max_tokens must be >= 32")
        if not 0 <= self.overlap_tokens < self.max_tokens / 2:
            raise JobError("overlap_tokens must be >= 0 and < max_tokens/2")
        if not 0 <= self.min_tokens < self.max_tokens:
            raise JobError("min_tokens must be >= 0 and < max_tokens")
        if type(self.prepend_heading) is not bool:
            raise JobError("prepend_heading must be a boolean")


@dataclass(frozen=True)
class Job:
    contract_id: str
    text: str
    job_id: str
    version: str = "1"
    command: str = "preprocess"
    options: ChunkOptions = field(default_factory=ChunkOptions)
    source_metadata: dict = field(default_factory=dict)
    extraction: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessingLimits:
    """Host-owned resource limits, never overridden by job JSON."""
    max_file_bytes: int = 20 * 1024 * 1024
    max_pages: int = 200
    max_text_chars: int = 2_000_000
    max_zip_bytes: int = 100 * 1024 * 1024
    max_zip_entries: int = 10000
    max_image_pixels: int = 25_000_000
    ocr_timeout_seconds: int = 60
    ocr_dpi: int = 200

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise JobError(f"{name} must be a positive integer")


@dataclass
class ExtractedDocument:
    text: str
    segments: list[dict]
    metadata: dict
    warnings: list[str] = field(default_factory=list)


@dataclass
class CleanLine:
    text: str
    sources: list[dict]
    start: int = 0
    end: int = 0


@dataclass
class CleanedDocument:
    text: str
    lines: list[CleanLine]
    stats: dict


@dataclass
class Block:
    kind: str
    text: str
    start: int
    end: int


@dataclass
class Section:
    heading_path: list[str]
    labels: list[str]
    blocks: list[Block] = field(default_factory=list)
    heading: Block | None = None
