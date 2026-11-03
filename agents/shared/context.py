from copy import deepcopy

from common.schemas.agent_messages import AgentRequest


def build_context(request: AgentRequest) -> dict:
    request.validate()
    return deepcopy({"content": request.content, "context": request.context,
                     "previous_results": request.previous_results})
