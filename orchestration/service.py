"""Durable workflow jobs; the existing LangGraph calls independent agent services."""
import os
from pathlib import Path
from common.schemas.contract import ContractInput, ContractChunk
from common.service_jobs import job_app
from common.service_http import serve
from agents.hermes.context_budget import MemoryPolicy
from orchestration.cache import ContractCache
from orchestration.pipeline import ContractRiskPipeline, load_memory_policies
from orchestration.workflow import AGENT_IDS
from orchestration.remote_agent import RemoteAgent, RemoteSummarizer
from monitoring.service_observer import ServiceObserver


def contract_from_payload(payload):
    raw = payload["contract"]
    contract = ContractInput(raw["contract_id"], raw["version"], [ContractChunk(**c) for c in raw["chunks"]],
                             raw["expected_chunk_count"], raw.get("context", {}))
    contract.validate()
    if contract.contract_id != payload["identity"]["contract_id"]:
        raise ValueError("Workflow contract identity mismatch")
    return contract


def policies():
    if os.getenv("MODEL_PROVIDER") == "fixture":
        if os.getenv("APP_ENV") != "local":
            raise ValueError("Fixture policies require local mode")
        return {key: MemoryPolicy(200000, 1000, 1) for key in AGENT_IDS}
    return load_memory_policies()


def create_app():
    cache = ContractCache(Path(os.getenv("DATA_DIR", "/data")) / "workflow-cache.sqlite3")
    urls = {key: os.environ[f"AGENT_{index}_URL"] for index, key in enumerate(AGENT_IDS, 1)}
    def ready():
        policies()
        return {"agent_transport": "http"}
    def handle(payload, progress, guard):
        agents = {}
        try:
            for key in AGENT_IDS:
                agents[key] = RemoteAgent(key, urls[key], payload["identity"], guard=guard)
            observer = ServiceObserver("langgraph", payload["identity"], progress)
            flow = ContractRiskPipeline(agents, policies=policies(), cache=cache,
                summarizers={key: RemoteSummarizer(agent) for key, agent in agents.items()}, observer=observer,
                cache_revision="remote-services-v1")
            # Include runtime in the durable cache isolation key; retry keeps the same runtime.
            run_key = payload["identity"]["job_id"] + ":" + payload["identity"]["runtime_id"]
            result = flow.run_contract(contract_from_payload(payload), job_id=run_key)
            result["job_id"] = payload["identity"]["job_id"]
            result["runtime_id"] = payload["identity"]["runtime_id"]
            guard()
            return result
        finally:
            for agent in agents.values():
                agent.close()
    return job_app("langgraph", handle, validate=contract_from_payload, ready=ready)


if __name__ == "__main__":
    serve(create_app, "langgraph")
