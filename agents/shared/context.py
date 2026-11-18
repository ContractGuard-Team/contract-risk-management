from copy import deepcopy

from common.schemas.agent_messages import AgentRequest


def agent_context(context: dict) -> dict:
    """The contract context an agent request carries. preprocessing.extraction (one segment per page/element, read
    only by the result builder from the full contract context) is left out: it would repeat in every chunk request
    and in every memory turn."""
    context = deepcopy(context)
    preprocessing = context.get("preprocessing")
    if isinstance(preprocessing, dict) and "extraction" in preprocessing:
        context["preprocessing"] = {k: v for k, v in preprocessing.items() if k != "extraction"}
    return context


def build_context(request: AgentRequest) -> dict:
    request.validate()
    return deepcopy({"content": request.content, "context": request.context,
                     "previous_results": request.previous_results})
