from typing import TypedDict


class ContractState(TypedDict, total=False):
    cache_key: str
    contract: dict
    next_chunk: int
    aggregate: dict
    results: dict
