from pathlib import Path

from agents.agent_1_classification.agent import ClassificationAgent
from agents.agent_2_risk_scoring.agent import RiskScoringAgent
from agents.agent_3_recommendation.agent import RecommendationAgent
from hermes_integration.bedrock_provider import HermesBedrockProvider
from hermes_integration.config_loader import load_agent_config


AGENT_TYPES = {
    "agent_1_classification": (ClassificationAgent, "AGENT_1"),
    "agent_2_risk_scoring": (RiskScoringAgent, "AGENT_2"),
    "agent_3_recommendation": (RecommendationAgent, "AGENT_3"),
}


def create_agent(agent_id: str, *, settings=None, provider=None, tools=None):
    if agent_id not in AGENT_TYPES:
        raise ValueError(f"Unknown agent: {agent_id}")
    agent_type, env_prefix = AGENT_TYPES[agent_id]
    directory = Path(__file__).resolve().parents[1] / "agents" / agent_id
    config = load_agent_config(directory, env_prefix, settings)
    model = provider if provider is not None else HermesBedrockProvider(config.bedrock)
    return agent_type(model, directory=directory, inference_config=config.inference_config, tools=tools)


def create_agents(*, settings=None, provider=None):
    return {agent_id: create_agent(agent_id, settings=settings, provider=provider)
            for agent_id in AGENT_TYPES}
