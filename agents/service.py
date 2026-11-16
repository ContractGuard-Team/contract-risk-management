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
    """Opt-in transport test fixture. Never selected as a fallback for OpenAI errors."""
    settings = OpenAISettings("transport-fixture")

    def generate(self, messages, *, inference_config=None, tools=None, tool_choice=None):
        if not all(isinstance(m, dict) and m.get("role") in ("system", "user", "assistant")
                   and isinstance(m.get("content"), str) for m in messages):
            raise ValueError("Messages are not in OpenAI chat format")
        text = json.dumps({"fixture": True, "input_sha256": hashlib.sha256(messages[-1]["content"].encode()).hexdigest()})
        return ModelResult(text, {"prompt_tokens": 20, "completion_tokens": 10}, "stop")


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
        self.agent = create_agent(agent_id, provider=FixtureProvider() if self.mode == "fixture" else None)
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
        values = {"files": files, "model": self.agent.provider.settings.public(), "mode": self.mode,
                  "inference": self.agent.inference_config}
        revision = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {"agent_id": self.agent.agent_id, "execution_revision": revision, "mode": self.mode}

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
        result = self.agent.run(AgentRequest(**payload["request"]), memory=memory, observer=observer)
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
