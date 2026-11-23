from pathlib import Path

from agents.agent_1_classification.agent import ClassificationAgent
from agents.agent_2_risk_scoring.agent import RiskScoringAgent
from agents.agent_3_recommendation.agent import RecommendationAgent
from agents.hermes.config_loader import load_agent_config
from integrations.external.gemini_client import GeminiChatClient


AGENT_TYPES = {
    "agent_1_classification": (ClassificationAgent, "AGENT_1"),
    "agent_2_risk_scoring": (RiskScoringAgent, "AGENT_2"),
    "agent_3_recommendation": (RecommendationAgent, "AGENT_3"),
}


def create_agent(agent_id: str, *, settings=None, provider=None, tools=None, lessons=None):
    if agent_id not in AGENT_TYPES:
        raise ValueError(f"Unknown agent: {agent_id}")
    agent_type, env_prefix = AGENT_TYPES[agent_id]
    directory = Path(__file__).resolve().parents[1] / agent_id
    config = load_agent_config(directory, env_prefix, settings)
    model = provider if provider is not None else GeminiChatClient(config.model)
    if lessons is None:  # long-term memory: this agent's own SQLite file under LONG_TERM_MEMORY_DIR, unset = disabled
        from agents.memory.long_term import store_from_env
        lessons = store_from_env(agent_id)
    return agent_type(model, directory=directory, inference_config=config.inference_config, tools=tools,
                      lessons=lessons)


def create_agents(*, settings=None, provider=None):
    return {agent_id: create_agent(agent_id, settings=settings, provider=provider)
            for agent_id in AGENT_TYPES}
