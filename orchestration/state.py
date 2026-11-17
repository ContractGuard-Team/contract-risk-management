from typing import TypedDict


class ContractState(TypedDict, total=False):
    cache_key: str
    job_id: str  # the job this run belongs to; handed to Agent 2/3 in context.workflow.job_id
    contract: dict
    next_chunk: int
    aggregate: dict
    results: dict
