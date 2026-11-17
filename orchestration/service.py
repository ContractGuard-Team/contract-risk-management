"""Durable workflow jobs; the existing LangGraph calls independent agent services."""
import hashlib
import json
import os
from pathlib import Path
from common.schemas.contract import ContractInput, ContractChunk
from common.service_jobs import job_app
from common.service_http import serve
from agents.hermes.context_budget import MemoryPolicy
from orchestration.cache import ContractCache
from orchestration.extraction import build_extraction
from orchestration.pipeline import ContractRiskPipeline, load_memory_policies
from orchestration.workflow import AGENT_IDS
from orchestration.remote_agent import RemoteAgent, RemoteSummarizer
from monitoring.service_observer import ServiceObserver


def contract_from_payload(payload):
    raw = payload["contract"]
    contract = ContractInput(raw["contract_id"], raw["contract_version"], [ContractChunk(**c) for c in raw["chunks"]],
                             raw["expected_chunk_count"], raw.get("context", {}))
    contract.validate()
    if contract.contract_id != payload["identity"]["contract_id"]:
        raise ValueError("Workflow contract identity mismatch")
    return contract


def workflow_trace(agents, pipeline_version):
    """extraction.v2 trace: models and prompt revisions of the agent services that produced this result."""
    specs = {key: agent.execution_spec for key, agent in agents.items()}
    revisions = json.dumps({key: spec["execution_revision"] for key, spec in specs.items()}, sort_keys=True)
    return {"model_version": ",".join(sorted({spec.get("model") or "unknown" for spec in specs.values()})),
            "prompt_version": hashlib.sha256(revisions.encode()).hexdigest()[:16], "pipeline_version": pipeline_version}


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
            # Runtime isolates the durable cache (retry keeps the same runtime); job_id goes to Agent 2/3.
            contract = contract_from_payload(payload)
            result = flow.run_contract(contract, job_id=payload["identity"]["job_id"],
                                       runtime_id=payload["identity"]["runtime_id"])
            # The business result every consumer reads (ContractGuard extraction.v2); agent outputs stay in results.
            result["extraction"], result["extraction_notes"] = build_extraction(
                contract, result["results"], job_id=payload["identity"]["job_id"],
                trace=workflow_trace(agents, flow.cache_revision))
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
