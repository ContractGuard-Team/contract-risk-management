"""Send a synthetic contract workflow trace. Never calls AWS or reads real contracts."""
from contextlib import contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from common.schemas.contract import ContractChunk, ContractInput
from common.settings import BedrockSettings
from hermes_integration.agent_factory import create_agents
from hermes_integration.bedrock_provider import HermesBedrockProvider
from hermes_integration.context_budget import MemoryPolicy
from hermes_integration.summarizer import ModelMemorySummarizer
from monitoring.langfuse import LangfuseObserver
from orchestration.cache import ContractCache
from orchestration.pipeline import ContractRiskPipeline


class SyntheticBedrock:
    def converse(self, **request):
        return {"output": {"message": {"content": [{"text": "Synthetic result; no real contract or model call."}]}},
                "usage": {"inputTokens": 10, "outputTokens": 8}, "stopReason": "end_turn"}


class AuditObserver(LangfuseObserver):
    trace_id = None

    @contextmanager
    def span(self, name, **kwargs):
        if name == "contract.workflow":
            self.trace_id = kwargs["trace_id"]
            kwargs["metadata"] = {**kwargs.get("metadata", {}), "synthetic": True,
                                  "usage_is_simulated": True}
        with super().span(name, **kwargs) as observation:
            yield observation


def main():
    configured = LangfuseObserver.from_env()
    if not isinstance(configured, LangfuseObserver):
        raise RuntimeError("Langfuse must be enabled to verify delivery")
    # Full capture is limited to this explicitly synthetic run, not saved to application config.
    observer = AuditObserver(configured.client, capture_content=True)
    cache = ContractCache()
    try:
        provider = HermesBedrockProvider(BedrockSettings("synthetic", "synthetic-bedrock-no-network"),
                                         SyntheticBedrock())
        agents = create_agents(provider=provider)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for agent in agents.values():
                agent.directory = root / agent.agent_id
                for relative in ("prompts/system.md", agent.task_prompt, "soul.md", "plan.md",
                                 "constraints.md", "context/instructions.md"):
                    path = agent.directory / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("Synthetic tracing verification only.", encoding="utf-8")
            summary_prompt = root / "summary.md"
            summary_prompt.write_text("Summarize this synthetic fixture.", encoding="utf-8")
            flow = ContractRiskPipeline(
                agents, cache=cache, observer=observer,
                policies={key: MemoryPolicy(1400, 150) for key in agents},
                summarizers={key: ModelMemorySummarizer(agent.provider, observer, summary_prompt)
                             for key, agent in agents.items()},
                cache_revision="synthetic-langfuse-verification-v1")
            contract = ContractInput("synthetic-tracing-check", "1",
                [ContractChunk(f"synthetic-{i}", i, f"Synthetic clause {i}. " * 6) for i in range(4)], 4)
            job_id = "langfuse-smoke-" + uuid4().hex
            with observer.attributes(tags=["smoke-test", "synthetic"]):
                result = flow.run_contract(contract, job_id=job_id)
        report = {"trace_id": observer.trace_id, "trace_url": observer.client.get_trace_url(trace_id=observer.trace_id),
                  "job_id": job_id, "status": result["status"], "synthetic": True}
        output = Path(".cache/langfuse-smoke-result.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
    finally:
        cache.close()
        observer.shutdown()


if __name__ == "__main__":
    main()
