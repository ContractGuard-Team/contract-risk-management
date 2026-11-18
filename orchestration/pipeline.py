from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from langgraph.checkpoint.memory import InMemorySaver

from common.exceptions import ConfigurationError
from common.schemas.contract import ContractChunk, ContractInput, canonical
from agents.hermes.agent_factory import create_agents
from agents.hermes.config_loader import prompt_files
from agents.memory import LONG_TERM_PROMPT, SHORT_TERM_PROMPT
from agents.hermes.context_budget import MemoryPolicy, conservative_token_count
from agents.hermes.summarizer import ModelMemorySummarizer
from monitoring.observer import NullObserver
from orchestration.cache import ContractCache
from orchestration.checkpoint import forget, release_run, resume_chunk
from orchestration.workflow import AGENT_IDS, WorkflowSteps, build_workflow


def load_memory_policies(path=None):
    import yaml
    path = Path(path) if path else Path(__file__).resolve().parents[1] / "configs/memory.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigurationError(f"TODO: configure memory budgets in {path}")
    try:
        return {agent_id: MemoryPolicy(**raw[agent_id]) for agent_id in AGENT_IDS}
    except (KeyError, TypeError) as exc:
        raise ConfigurationError(f"TODO: configure each agent's memory policy in {path}") from exc


class ContractRiskPipeline:
    """LangGraph workflow; SQLite stores full results, Hermes manages active context."""
    agent_order = AGENT_IDS

    def __init__(self, agents=None, *, policies=None, cache=None, summarizers=None, observer=None,
                 checkpointer=None, counter=conservative_token_count, cache_revision="contract-workflow-v1"):
        self.agents = create_agents() if agents is None else agents
        self.policies = load_memory_policies() if policies is None else policies
        if set(self.policies) != set(AGENT_IDS) or set(self.agents) != set(AGENT_IDS):
            raise ConfigurationError("Exactly three agents and three memory policies are required")
        self.cache = cache if cache is not None else ContractCache(".cache/contracts.sqlite3")
        self.observer = observer if observer is not None else NullObserver()
        self.summarizers = summarizers if summarizers is not None else {
            agent_id: ModelMemorySummarizer(agent.provider, agent.directory / SHORT_TERM_PROMPT, self.observer,
                                            counter=counter)
            for agent_id, agent in self.agents.items()}
        self.counter, self.cache_revision = counter, cache_revision
        saver = checkpointer if checkpointer is not None else InMemorySaver()
        self.checkpointer = saver
        self.graph = build_workflow(WorkflowSteps(self.agents, self.cache, self.policies,
                                                 self.summarizers, self.observer, counter), saver)

    def _execution(self):
        specs = {"revision": self.cache_revision}
        for agent_id, agent in self.agents.items():
            if hasattr(agent, "execution_spec"):
                specs[agent_id] = {"remote": agent.execution_spec, "policy": asdict(self.policies[agent_id])}
                continue
            instructions = {}
            for name in (*prompt_files(agent.directory), "config.yaml", SHORT_TERM_PROMPT, LONG_TERM_PROMPT):
                path = agent.directory / name
                instructions[name] = path.read_text(encoding="utf-8") if path.is_file() else ""
            settings = getattr(agent.provider, "settings", None)
            # instructions already include this agent's own memory prompts (short_term_summary, long_term_reflection)
            model = getattr(agent, "output_model", None)
            instructions["output_schema"] = json.dumps(model.model_json_schema(), sort_keys=True) if model is not None else ""
            specs[agent_id] = {"instructions": instructions, "inference": agent.inference_config,
                              "model": getattr(settings, "model", ""),
                              "policy": asdict(self.policies[agent_id])}
        return specs

    def _learn(self, result):
        """Long-term memory: each agent reflects on its finished output and proposes lessons for review.
        Best effort: the contract result is already stored, so a reflection failure never fails the job."""
        contract = {key: result[key] for key in ("job_id", "contract_id", "contract_version")}
        for agent_id, agent in self.agents.items():
            output = result["results"].get(agent_id) or {}
            text = output.get("text") if isinstance(output.get("text"), str) else json.dumps(output, ensure_ascii=False)
            with self.observer.span(agent_id + ".reflect", kind="agent") as observation:
                try:
                    outcome = agent.reflect(contract, text)
                except Exception as exc:  # not ready (empty prompts), model/transport error...
                    outcome = {"skipped": type(exc).__name__}
                observation.summary(output={key: outcome[key] for key in ("proposed", "skipped") if key in outcome})

    def run_contract(self, contract: ContractInput, *, job_id: str, runtime_id: str | None = None, finalize=None):
        """job_id: the job (handed to Agent 2/3). runtime_id: optional attempt id; it only isolates the cache.
        finalize(result) -> result: builds/validates the final output BEFORE the run is cached as complete, so an
        invalid output never counts as a finished contract (and Agent 2/3 memory is kept for the retry)."""
        contract.validate()
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id is required for cache isolation/resume")
        if "workflow" in contract.context:
            raise ValueError("'workflow' is reserved for orchestration")
        run_key = f"{job_id}:{runtime_id}" if runtime_id else job_id
        key = hashlib.sha256(canonical([run_key, contract.contract_id, contract.contract_version]).encode()).hexdigest()
        try:
            with self.cache.job_lock(key):
                with self.observer.attributes(session_id=key, tags=["contract-risk-management"],
                                              trace_name="contract.workflow"), \
                     self.observer.span("contract.workflow", kind="chain", trace_id=uuid4().hex,
                                       metadata={"job_id": job_id, "contract_id": contract.contract_id,
                                                 "contract_version": contract.contract_version, "chunk_count": len(contract.chunks)}) as root:
                    root.summary(input={"chunk_count": len(contract.chunks),
                                        "total_characters": sum(len(c.text) for c in contract.chunks)})
                    self.cache.begin(key, contract.fingerprint(self._execution()))
                    completed = self.cache.get(key, "complete")
                    if completed is not None:
                        root.summary(output={"status": "completed", "cache_hit": True,
                                             "chunk_count": len(contract.chunks)})
                        return completed
                    raw = asdict(contract)
                    try:
                        state = self.graph.invoke(
                            {"cache_key": key, "job_id": job_id, "contract": raw,
                             "next_chunk": resume_chunk(self.cache, key, raw),
                             "aggregate": {}, "results": {}},
                            {"configurable": {"thread_id": key}, "recursion_limit": len(contract.chunks) + 10})
                    finally:
                        forget(self.checkpointer, key)  # durable progress is the cache, not RAM
                    result = {"job_id": job_id, "contract_id": contract.contract_id,
                              "contract_version": contract.contract_version, "cache_key": key, "status": "completed",
                              "results": state["results"]}
                    if finalize is not None:
                        result = finalize(result)
                    self.cache.put(key, "complete", result)
                    release_run(self.cache, key)  # final result stored: Agent 2/3 memory no longer needed
                    self._learn(result)
                    root.summary(output={"status": "completed", "cache_hit": False,
                                         "chunk_count": len(contract.chunks), "agent_count": len(state["results"])})
                    return result
        finally:
            self.observer.flush()

    def run(self, content: str, context: dict | None = None) -> dict:
        contract = ContractInput("inline", "1", [ContractChunk("chunk-0", 0, content)], 1,
                                 {} if context is None else context)
        return self.run_contract(contract, job_id=str(uuid4()))["results"]
