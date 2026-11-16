from agents.shared.context import build_context as build_shared_context
from common.schemas.agent_messages import AgentRequest
from common.schemas.contract import ContractInput
from agents.agent_1_classification.schemas.input import ClassificationInput
from copy import deepcopy


def build_chunk_request(contract: ContractInput, index: int) -> ClassificationInput:
    """Build Agent 1's explicit context from a validated preprocessing manifest."""
    contract.validate()
    if type(index) is not int or not 0 <= index < contract.expected_chunk_count:
        raise ValueError("Invalid classification chunk index")
    if "workflow" in contract.context:
        raise ValueError("workflow is reserved for orchestration")
    chunk = contract.chunks[index]
    context = deepcopy(contract.context)
    context["workflow"] = {
        "contract_id": contract.contract_id, "contract_version": contract.contract_version,
        "chunk_id": chunk.chunk_id, "chunk_index": chunk.index,
        "chunk_count": contract.expected_chunk_count,
        "source_metadata": deepcopy(chunk.metadata),
    }
    return ClassificationInput(chunk.text, context)


def build_context(request: AgentRequest) -> dict:
    # Text remains user data; preprocessing metadata must not become system instructions.
    return build_shared_context(request)
