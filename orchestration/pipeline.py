from copy import deepcopy
from dataclasses import asdict

from common.schemas.agent_messages import AgentRequest
from hermes_integration.agent_factory import create_agents


class ContractRiskPipeline:
    """Minimal same-process chain; not a distributed/container workflow."""

    agent_order = ("agent_1_classification", "agent_2_risk_scoring", "agent_3_recommendation")

    def __init__(self, agents=None):
        self.agents = create_agents() if agents is None else agents

    def run(self, content: str, context: dict | None = None) -> dict:
        context = {} if context is None else context
        AgentRequest(content, context).validate()
        results = {}
        for agent_id in self.agent_order:
            request = AgentRequest(content, deepcopy(context), deepcopy(results))
            results[agent_id] = asdict(self.agents[agent_id].run(request))
        return results
