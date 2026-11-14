from common.schemas.agent_messages import AgentRequest
from agents.hermes.agent_factory import create_agent as _create_agent


def create_agent(*, settings=None, provider=None, tools=None):
    return _create_agent("agent_3_recommendation", settings=settings, provider=provider, tools=tools)


def run(request: AgentRequest, *, settings=None, provider=None, tools=None):
    """Python entrypoint; starts no HTTP service and requires no AWS access on import."""
    return create_agent(settings=settings, provider=provider, tools=tools).run(request)
