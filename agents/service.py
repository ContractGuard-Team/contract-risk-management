"""Agent RPC adapter preserving BaseAgent and Hermes memory semantics."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from fastapi import Request, HTTPException
from common.schemas.agent_messages import AgentRequest, ModelResult
from common.settings import OpenAISettings
from common.service_http import serve, read_json
from common.service_jobs import job_app
from agents.hermes.agent_factory import create_agent, AGENT_TYPES
from agents.memory import LONG_TERM_PROMPT, SHORT_TERM_PROMPT
from agents.hermes.config_loader import REQUIRED, load_prompts, prompt_files
from agents.hermes.context_budget import MemoryPolicy
from agents.memory.short_term import create_contract_memory
from agents.hermes.summarizer import ModelMemorySummarizer


class FixtureProvider:
    """Opt-in transport test fixture. Never selected as a fallback for OpenAI errors.
    Answers with the smallest output that matches the agent's schema (no business analysis), so the whole
    pipeline up to extraction.v2 runs without a model."""
    settings = OpenAISettings("transport-fixture")

    def __init__(self, agent_id=""):
        self.agent_id = agent_id

    def generate(self, messages, *, inference_config=None, tools=None, tool_choice=None):
        if not all(isinstance(m, dict) and m.get("role") in ("system", "user", "assistant")
                   and isinstance(m.get("content"), str) for m in messages):
            raise ValueError("Messages are not in OpenAI chat format")
        try:
            request = json.loads(messages[-1]["content"])
        except ValueError:
            request = {}
        answer = fixture_output(self.agent_id, request if isinstance(request, dict) else {})
        return ModelResult(json.dumps(answer, ensure_ascii=False), {"prompt_tokens": 20, "completion_tokens": 10}, "stop")


def fixture_output(agent_id, request):
    """Schema-shaped fixture answer; Agent 1 echoes its chunk verbatim as one clause (audits check the handoff)."""
    from agents.shared.output import CHECK_CODES
    workflow = (request.get("context") or {}).get("workflow") or {}
    if agent_id == "agent_1_classification" and workflow.get("chunk_id"):
        pages = [p for p in (workflow.get("source_metadata") or {}).get("pages") or [] if isinstance(p, int) and p > 0] or [1]
        return {"chunk_id": workflow["chunk_id"], "language": "vi", "clauses": [{
                    "clause_id": f"{workflow.get('contract_id', 'contract')}-C{workflow.get('chunk_index', 0) + 1:04d}"[-64:],
                    "continuation": False, "complete": True, "number": "", "heading": None, "text": request["content"],
                    "category": "other", "clause_type": "C99", "page_start": min(pages), "page_end": max(pages),
                    "facts": []}],
                "metadata": {"parties": [], "contract_type": None, "contract_value": None, "currency": None,
                             "effective_date": None, "expiry_date": None, "governing_law": None},
                "fact_values": {}, "injection_warnings": [], "corrections": []}
    if agent_id == "agent_2_risk_scoring":
        return {"clause_verdicts": [], "findings": [], "evidence_gaps": [],
                "checks": [{"check_code": code, "check_status": "not_applicable", "clause_ids": [], "finding_id": None,
                            "note": "fixture"} for code in CHECK_CODES]}
    if agent_id == "agent_3_recommendation":
        return {"recommendations": [], "overall_recommendation": None, "evidence_gaps": []}
    return {"fixture": True}


class FixtureSummarizer:
    def summarize(self, text, target_tokens, input_budget):
        return "fixture-summary:" + hashlib.sha256(text.encode()).hexdigest()


class AgentRuntime:
    def __init__(self, agent_id):
        if agent_id not in AGENT_TYPES:
            raise ValueError("Unknown AGENT_ID")
        self.mode = os.getenv("MODEL_PROVIDER", "openai")
        if self.mode not in ("openai", "fixture"):
            raise ValueError("MODEL_PROVIDER must be openai or fixture")
        if self.mode == "fixture" and os.getenv("APP_ENV") != "local":
            raise ValueError("Fixture mode requires APP_ENV=local")
        self.agent = create_agent(agent_id, provider=FixtureProvider(agent_id) if self.mode == "fixture" else None)
        from integrations.external.mcp_client import bind_mcp_tools
        self.mcp_tools = bind_mcp_tools(self.agent)  # Agent 2/3 only: Agent 1 lists no MCP tools
        self.fixture_directory = None
        if self.mode == "fixture":
            # Test instructions live only in /tmp. Source prompts/plans are untouched.
            self.fixture_directory = TemporaryDirectory(prefix=agent_id)
            directory = Path(self.fixture_directory.name)
            for _, relative in REQUIRED:
                path = directory / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("Local transport fixture; no business analysis.", encoding="utf-8")
            self.agent.directory = directory

    def ready(self):
        if self.mode == "openai":
            self.agent.provider.settings.validate()
        load_prompts(self.agent.directory)
        return {"model_provider": self.mode, "mcp_tools": self.mcp_tools,
                "long_term_memory": self.agent.lessons is not None}

    def describe(self):
        files = {}
        for name in (*prompt_files(self.agent.directory), "config.yaml", SHORT_TERM_PROMPT, LONG_TERM_PROMPT):
            path = self.agent.directory / name
            files[name] = path.read_text(encoding="utf-8") if path.is_file() else ""
        # Approved long-term lessons are NOT part of the revision: approving one must not fail running jobs.
        model = self.agent.output_model
        values = {"files": files, "model": self.agent.provider.settings.public(), "mode": self.mode,
                  "inference": self.agent.inference_config,
                  "output_schema": model.model_json_schema() if model is not None else None}
        revision = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {"agent_id": self.agent.agent_id, "execution_revision": revision, "mode": self.mode,
                "model": self.agent.provider.settings.model}  # extraction.v2 trace.model_version (never the key)

    def summarizer(self, observer=None):
        return FixtureSummarizer() if self.mode == "fixture" else ModelMemorySummarizer(
            self.agent.provider, self.agent.directory / SHORT_TERM_PROMPT, observer)

    def validate(self, payload):
        if payload.get("execution_revision") != self.describe()["execution_revision"]:
            raise ValueError("Agent configuration changed; submit a new workflow")
        operation = payload.get("operation", "run")
        if operation == "run":
            AgentRequest(**payload["request"]).validate()
            MemoryPolicy(**payload["policy"])
            if not isinstance(payload.get("memory", {}), dict):
                raise ValueError("Invalid memory snapshot")
        elif operation == "reflect":
            if not isinstance(payload.get("contract"), dict) or not isinstance(payload.get("output"), str) or not payload["output"]:
                raise ValueError("Reflection needs contract metadata and the agent output")
        elif operation == "summarize":
            if not isinstance(payload.get("text"), str) or not payload["text"]:
                raise ValueError("Missing summary text")
            if not 0 < payload["target_tokens"] < payload["input_budget"]:
                raise ValueError("Invalid summary budget")
        else:
            raise ValueError("Unknown agent operation")

    def handle(self, payload, progress, guard):
        from monitoring.service_observer import ServiceObserver
        self.ready()
        self.validate(payload)
        observer = ServiceObserver(os.environ.get("SERVICE_NAME", self.agent.agent_id), payload["identity"])
        progress(self.agent.agent_id, 20)
        if payload.get("operation") == "reflect":
            return self.agent.reflect(payload["contract"], payload["output"])
        if payload.get("operation") == "summarize":
            text = self.summarizer(observer).summarize(payload["text"], payload["target_tokens"], payload["input_budget"])
            guard()
            return {"text": text}
        memory = create_contract_memory(MemoryPolicy(**payload["policy"]), self.summarizer(observer), payload.get("memory"))
        from agents.shared.tools import JOB_CONTEXT
        token = JOB_CONTEXT.set(dict(payload["identity"]))  # MCP resolves this job's documents itself
        try:
            result = self.agent.run(AgentRequest(**payload["request"]), memory=memory, observer=observer)
        finally:
            JOB_CONTEXT.reset(token)
        guard()
        return {"result": asdict(result), "memory": memory.snapshot(), "execution_revision": payload["execution_revision"]}


def create_app():
    runtime = AgentRuntime(os.environ["AGENT_ID"])
    service = os.getenv("SERVICE_NAME", runtime.agent.agent_id)
    app = job_app(service, runtime.handle, validate=runtime.validate, ready=runtime.ready, describe=runtime.describe)
    @app.post("/v1/messages")
    async def messages(request: Request):
        payload = await read_json(request)
        if payload.get("execution_revision") != runtime.describe()["execution_revision"]:
            raise HTTPException(409, "Agent configuration changed")
        runtime.ready()
        return {"messages": runtime.agent.build_messages(AgentRequest(**payload["request"]))}
    return app


if __name__ == "__main__":
    serve(create_app, os.environ.get("SERVICE_NAME", "agent"))
