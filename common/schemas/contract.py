from dataclasses import asdict, dataclass, field
import hashlib
import json


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


@dataclass(frozen=True)
class ContractChunk:
    chunk_id: str
    index: int
    text: str
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ContractInput:
    contract_id: str
    contract_version: str
    chunks: list[ContractChunk]
    expected_chunk_count: int
    context: dict = field(default_factory=dict)

    def validate(self):
        if not all(isinstance(v, str) and v.strip() for v in (self.contract_id, self.contract_version)):
            raise ValueError("contract_id and contract_version are required")
        if type(self.expected_chunk_count) is not int or self.expected_chunk_count < 1:
            raise ValueError("expected_chunk_count must be positive")
        if len(self.chunks) != self.expected_chunk_count:
            raise ValueError("Incomplete contract: chunk count does not match manifest")
        if not isinstance(self.context, dict):
            raise ValueError("context must be a dictionary")
        seen = set()
        for index, chunk in enumerate(self.chunks):
            if not isinstance(chunk, ContractChunk) or type(chunk.index) is not int or chunk.index != index:
                raise ValueError("Chunks must be in contiguous order starting at index 0")
            if not isinstance(chunk.chunk_id, str) or not chunk.chunk_id.strip() or chunk.chunk_id in seen:
                raise ValueError("Chunk IDs must be non-empty and unique")
            if not isinstance(chunk.text, str) or not chunk.text.strip():
                raise ValueError("Chunk text must not be empty")
            if not isinstance(chunk.metadata, dict):
                raise ValueError("Chunk metadata must be a dictionary")
            seen.add(chunk.chunk_id)
        canonical(asdict(self))

    def fingerprint(self, execution: dict) -> str:
        return hashlib.sha256(canonical({"input": asdict(self), "execution": execution}).encode()).hexdigest()
