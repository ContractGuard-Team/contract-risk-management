from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agents.agent_1_classification.context.chunk_memory import ChunkMemory
from agents.agent_1_classification.context.context_compactor import compact_context
from agents.agent_2_risk_scoring.context.evidence_manager import EvidenceManager
from agents.agent_3_recommendation.context.recommendation_memory import RecommendationMemory
from common.exceptions import ConfigurationError, IntegrationNotConfigured, ModelResponseError
from common.schemas.agent_messages import AgentRequest, ModelResult
from common.settings import OpenAISettings
from agents.hermes.agent_factory import create_agent, create_agents
from agents.hermes.config_loader import load_agent_config
from agents.memory.short_term import memory_context
from agents.hermes.tool_registry import ToolRegistry
from integrations.external.openai_client import OpenAIChatClient
from orchestration.pipeline import ContractRiskPipeline
from orchestration.cache import ContractCache
from agents.hermes.context_budget import MemoryPolicy
from monitoring.observer import NullObserver


SETTINGS = OpenAISettings("test-model", "sk-test")


def test_pipeline(agents):
    return ContractRiskPipeline(agents, cache=ContractCache(), observer=NullObserver(),
        policies={key: MemoryPolicy(100000, 1000) for key in agents})


class FakeUsage:
    def __init__(self, data):
        self.data = data

    def model_dump(self, exclude_none=True):
        return dict(self.data)


def completion(text="model result", reason="stop"):
    """Shape of openai ChatCompletion used by OpenAIChatClient."""
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=reason, message=SimpleNamespace(content=text))],
                           usage=FakeUsage({"prompt_tokens": 2, "completion_tokens": 3}))


class FakeClient:
    """Stands in for openai.OpenAI(): client.chat.completions.create(**kwargs)."""
    def __init__(self, response=None):
        self.requests = []
        self.response = response
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return self.response if self.response is not None else completion()


def first_user(request):
    return json.loads(next(m["content"] for m in request["messages"] if m["role"] == "user"))


class FakeProvider:
    def __init__(self):
        self.calls = []

    def generate(self, messages, *, inference_config=None):
        self.calls.append(deepcopy(messages))
        return ModelResult("result", {"prompt_tokens": 1}, "stop")


PROMPTS = ("prompts/soul.md", "prompts/core.md", "prompts/external.md")


@contextmanager
def configured_agents(provider):
    agents = create_agents(provider=provider)
    with TemporaryDirectory() as directory:
        for agent in agents.values():
            agent.directory = Path(directory) / agent.agent_id
            for relative in PROMPTS:
                path = agent.directory / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"Test instructions for {relative}", encoding="utf-8")
        yield agents


def tool_completion(*calls):
    """ChatCompletion asking for tools: calls = (id, name, arguments JSON)."""
    message = SimpleNamespace(content=None, tool_calls=[
        SimpleNamespace(id=i, function=SimpleNamespace(name=n, arguments=a)) for i, n, a in calls])
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls", message=message)],
                           usage=FakeUsage({"prompt_tokens": 5, "completion_tokens": 1}))


class ScriptedClient(FakeClient):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return self.responses.pop(0)


def bind_tool(agent, name, handler):
    agent.tools.register(name, handler)
    agent.tools.spec_loader = lambda: {name: {"type": "function", "function": {
        "name": name, "description": "test", "parameters": {"type": "object"}}}}


class ToolLoopTests(unittest.TestCase):
    def test_model_calls_tool_then_answers_with_its_evidence(self):
        searched = []
        client = ScriptedClient([tool_completion(("c1", "legal_search", '{"query": "phạt vi phạm"}')),
                                 completion('{"verdict": "confirmed", "evidence_gaps": []}')])
        with configured_agents(OpenAIChatClient(SETTINGS, client)) as agents:
            agent = agents["agent_2_risk_scoring"]
            bind_tool(agent, "legal_search", lambda p: searched.append(p) or {"evidence": [{"citation": "Điều 418"}]})
            result = agent.run(AgentRequest("contract"))
        self.assertEqual(searched, [{"query": "phạt vi phạm"}])
        self.assertEqual(client.requests[0]["tools"][0]["function"]["name"], "legal_search")
        tool_message = client.requests[1]["messages"][-1]
        self.assertEqual((tool_message["role"], tool_message["tool_call_id"]), ("tool", "c1"))
        self.assertIn("Điều 418", tool_message["content"])
        self.assertEqual(client.requests[1]["messages"][-2]["tool_calls"][0]["function"]["name"], "legal_search")
        self.assertEqual(json.loads(result.text)["verdict"], "confirmed")
        self.assertEqual(result.usage["prompt_tokens"], 7)  # both model calls billed

    def test_evidence_gaps_trigger_new_search_and_reevaluation(self):
        client = ScriptedClient([completion('{"evidence_gaps": ["Điều 3: chưa có căn cứ phạt"]}'),
                                 tool_completion(("c2", "web_search", '{"query": "mức phạt vi phạm hợp đồng"}')),
                                 completion('{"evidence_gaps": []}')])
        with configured_agents(OpenAIChatClient(SETTINGS, client)) as agents:
            agent = agents["agent_2_risk_scoring"]
            bind_tool(agent, "web_search", lambda p: {"results": [{"url": "https://vbpl.vn/x"}]})
            result = agent.run(AgentRequest("contract"))
        self.assertEqual(json.loads(result.text), {"evidence_gaps": []})
        retry = client.requests[1]["messages"][-1]
        self.assertEqual(retry["role"], "user")
        self.assertIn("Điều 3: chưa có căn cứ phạt", retry["content"])

    def test_gaps_stay_after_reevaluation_budget_and_tool_errors_reach_the_model(self):
        gap = completion('{"evidence_gaps": ["thiếu căn cứ"]}')
        client = ScriptedClient([tool_completion(("c1", "legal_search", "{}")), gap, gap, gap])
        def broken(_):
            raise IntegrationNotConfigured("TODO: set QDRANT_URL")
        with configured_agents(OpenAIChatClient(SETTINGS, client)) as agents:
            agent = agents["agent_3_recommendation"]
            bind_tool(agent, "legal_search", broken)
            agent.tool_functions = {**agent.tool_functions, "legal_search": agent.tool_functions["qdrant_search"]}
            result = agent.run(AgentRequest("contract"))
        self.assertIn("IntegrationNotConfigured", client.requests[1]["messages"][-1]["content"])
        self.assertEqual(len(client.requests), 4)  # first pass + 2 re-evaluations, then the honest gap is kept
        self.assertEqual(json.loads(result.text)["evidence_gaps"], ["thiếu căn cứ"])

    def test_tool_budget_forces_final_answer(self):
        loop = [tool_completion((f"c{i}", "legal_search", "{}")) for i in range(6)]
        client = ScriptedClient([*loop, completion('{"evidence_gaps": []}')])
        with configured_agents(OpenAIChatClient(SETTINGS, client)) as agents:
            agent = agents["agent_2_risk_scoring"]
            bind_tool(agent, "legal_search", lambda p: {"evidence": []})
            agent.run(AgentRequest("contract"))
        self.assertEqual(client.requests[-1]["tool_choice"], "none")
        self.assertNotIn("tool_choice", client.requests[0])


class AgentBaseTests(unittest.TestCase):
    def test_creation_does_not_require_openai_sdk_or_key(self):
        with patch.dict(os.environ, {}, clear=True), patch.dict("sys.modules", {"openai": None}):
            agents = create_agents()
            self.assertEqual(len(agents), 3)
            self.assertTrue(all(agent.provider._client is None for agent in agents.values()))

    def test_source_prompts_are_filled_and_loaded_in_order(self):
        from agents.hermes.config_loader import load_prompts
        for agent in create_agents(provider=FakeProvider()).values():
            with self.subTest(agent=agent.agent_id):
                prompts = load_prompts(agent.directory)
                self.assertTrue(all(prompts[key] for key in ("soul", "core", "external")))
                system = [m["content"] for m in agent.build_messages(AgentRequest("contract")) if m["role"] == "system"]
                self.assertEqual(system[:3], [prompts["soul"], prompts["core"], prompts["external"]])
                self.assertIn("schema output được thiết kế trong source code", prompts["core"])
        agents = create_agents(provider=FakeProvider())
        self.assertGreaterEqual(len(load_prompts(agents["agent_1_classification"].directory)["examples"]), 10)
        rules = load_prompts(agents["agent_2_risk_scoring"].directory)["rules"]
        self.assertEqual([name for name, _ in rules], ["risk_policy.json"])
        self.assertEqual(json.loads(rules[0][1])["policy_version"], "1.2.2")
        self.assertIn("partner_document", load_prompts(agents["agent_3_recommendation"].directory)["external"])

    def test_rules_and_output_schema_become_system_messages(self):
        with configured_agents(FakeProvider()) as agents:
            agent = agents["agent_2_risk_scoring"]
            (agent.directory / "prompts/rules").mkdir(parents=True)
            (agent.directory / "prompts/rules/policy.json").write_text('{"rules": []}', encoding="utf-8")
            (agent.directory / "schemas").mkdir()
            (agent.directory / "schemas/output.schema.json").write_text('{"type": "object"}', encoding="utf-8")
            system = [m["content"] for m in agent.build_messages(AgentRequest("contract")) if m["role"] == "system"]
        self.assertEqual(system[3], '# Rules: policy.json\n{"rules": []}')
        self.assertTrue(system[4].startswith("# Output schema") and system[4].endswith('{"type": "object"}'))

    def test_each_instruction_file_is_required(self):
        provider = FakeProvider()
        with configured_agents(provider) as agents:
            agent = agents["agent_1_classification"]
            for relative in PROMPTS:
                path = agent.directory / relative
                original = path.read_text(encoding="utf-8")
                path.write_text("\n<!-- TODO: fill later -->\n", encoding="utf-8")
                with self.subTest(file=relative), self.assertRaises(ConfigurationError):
                    agent.run(AgentRequest("contract"))
                path.write_text(original, encoding="utf-8")
        self.assertEqual(provider.calls, [])

    def test_three_agents_openai_chat_requests_fake_client(self):
        client = FakeClient()
        provider = OpenAIChatClient(SETTINGS, client)
        with configured_agents(provider) as agents:
            agents["agent_1_classification"].inference_config = {"max_completion_tokens": 42}
            result = test_pipeline(agents).run("Hợp đồng thử", {"case": ["one"]})
        self.assertEqual(list(result), list(agents))
        self.assertEqual(len(client.requests), 3)
        self.assertEqual(client.requests[0]["max_completion_tokens"], 42)
        self.assertNotIn("max_completion_tokens", client.requests[1])
        for request in client.requests:
            self.assertEqual(request["model"], "test-model")
            soul, core, external = request["messages"][:3]
            self.assertEqual((soul["role"], core["role"], external["role"]), ("system", "system", "system"))
            self.assertNotIn("tools", request)  # no MCP bound: plain single call
            self.assertEqual(soul["content"], "Test instructions for prompts/soul.md")
            self.assertEqual(core["content"], "Test instructions for prompts/core.md")
            self.assertEqual(external["content"], "Test instructions for prompts/external.md")
        body = first_user(client.requests[2])
        self.assertEqual(body["context"]["case"], ["one"])
        self.assertEqual(body["context"]["workflow"]["risk_scoring"]["text"], "model result")
        self.assertEqual(first_user(client.requests[1])["context"]["workflow"]["classification"]["chunk_count"], 1)
        self.assertEqual(result["agent_3_recommendation"]["usage"]["completion_tokens"], 3)

    def test_named_agent_methods_return_specific_output(self):
        from agents.agent_1_classification.schemas.input import ClassificationInput
        from agents.agent_1_classification.schemas.output import ClassificationOutput
        from agents.agent_2_risk_scoring.schemas.input import RiskScoringInput
        from agents.agent_2_risk_scoring.schemas.output import RiskScoringOutput
        from agents.agent_3_recommendation.schemas.input import RecommendationInput
        from agents.agent_3_recommendation.schemas.output import RecommendationOutput
        cases = [("classify", ClassificationInput, ClassificationOutput),
                 ("evaluate", RiskScoringInput, RiskScoringOutput),
                 ("recommend", RecommendationInput, RecommendationOutput)]
        with configured_agents(FakeProvider()) as agents:
            for agent, (method, input_type, output_type) in zip(agents.values(), cases):
                result = getattr(agent, method)(input_type("contract"))
                self.assertIsInstance(result, output_type)
                self.assertEqual(result.agent_id, agent.agent_id)

    def test_invalid_inputs_do_not_call_model(self):
        provider = FakeProvider()
        with configured_agents(provider) as agents:
            agent = next(iter(agents.values()))
            requests = [AgentRequest(" "), AgentRequest(None), AgentRequest("x", []),
                        AgentRequest("x", {"bad": object()}), AgentRequest("x", previous_results=[]),
                        AgentRequest("x", {"bad": float("nan")})]
            for request in requests:
                with self.assertRaises(ValueError):
                    agent.run(request)
        self.assertEqual(provider.calls, [])

    def test_context_is_copied_and_no_cross_run_history(self):
        provider = FakeProvider()
        context = {"nested": ["original"]}
        with configured_agents(provider) as agents:
            agent = next(iter(agents.values()))
            built = agent.build_context(AgentRequest("x", context))
            built["context"]["nested"].append("changed")
            pipeline = test_pipeline(agents)
            pipeline.run("first", context)
            pipeline.run("second")
        self.assertEqual(context, {"nested": ["original"]})
        second_run = json.loads(provider.calls[3][-1]["content"])  # last message = this request
        self.assertEqual(second_run["previous_results"], {})
        self.assertNotIn("nested", second_run["context"])

    def test_pipeline_stops_on_failure(self):
        provider = FakeProvider()
        with configured_agents(provider) as agents:
            failing = agents["agent_2_risk_scoring"]
            with patch.object(failing, "run", side_effect=RuntimeError("failed")):
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    test_pipeline(agents).run("contract")
        self.assertEqual(len(provider.calls), 1)

    def test_tools_require_binding_and_use_explicit_handler(self):
        for agent in create_agents(provider=FakeProvider()).values():
            for name in agent.tool_functions:
                with self.subTest(agent=agent.agent_id, tool=name):
                    with self.assertRaises(IntegrationNotConfigured):
                        agent.run_tool(name, {})
                    def handler(payload):
                        payload["items"].append("processed")
                        return payload
                    agent.tools.register(name, handler)
                    payload = {"items": []}
                    self.assertEqual(agent.run_tool(name, payload), {"items": ["processed"]})
                    self.assertEqual(payload, {"items": []})
                    with self.assertRaises(ValueError):
                        agent.run_tool(name, "invalid")
            with self.assertRaises(ValueError):
                agent.run_tool("unknown_tool", {})

    def test_tool_registry_rejects_duplicate_or_invalid_handler(self):
        registry = ToolRegistry()
        with self.assertRaises(ValueError):
            registry.register("risk_calculator", None)
        registry.register("risk_calculator", lambda payload: "not a dict")
        with self.assertRaises(ValueError):
            registry.register("risk_calculator", lambda payload: {})
        agent = create_agent("agent_2_risk_scoring", provider=FakeProvider(), tools=registry)
        with self.assertRaises(ValueError):
            agent.run_tool("risk_calculator", {})

    def test_job_memory_copies_values_and_remains_explicit(self):
        for memory_type in (ChunkMemory, EvidenceManager, RecommendationMemory):
            memory = memory_type()
            value = {"items": ["one"]}
            memory.put("1", value)
            value["items"].append("two")
            snapshot = memory_context(memory)
            snapshot["memory"]["1"]["items"].append("three")
            self.assertEqual(memory.snapshot(), {"1": {"items": ["one"]}})
            self.assertEqual(memory_type().snapshot(), {})
            memory.clear()
            self.assertEqual(memory.snapshot(), {})
        with self.assertRaises(IntegrationNotConfigured):
            compact_context({})
        with self.assertRaises(IntegrationNotConfigured):
            EvidenceManager().validate()
        self.assertEqual(compact_context({"x": 1}, compactor=lambda data: data), {"x": 1})

    def test_missing_openai_config_fails_before_client_call(self):
        client = FakeClient()
        for settings in (OpenAISettings(), OpenAISettings("gpt-model"), OpenAISettings("", "sk-test")):
            with self.subTest(settings=settings), self.assertRaisesRegex(ConfigurationError, "OPENAI"):
                OpenAIChatClient(settings, client).generate([{"role": "user", "content": "hello"}])
        self.assertEqual(client.requests, [])
        self.assertNotIn("sk-test", repr(SETTINGS))
        self.assertEqual(SETTINGS.public(), {"model": "test-model"})

    def test_incomplete_or_empty_openai_response_is_rejected(self):
        for reason in ("length", "content_filter", "tool_calls", ""):
            with self.subTest(reason=reason), self.assertRaises(ModelResponseError):
                OpenAIChatClient(SETTINGS, FakeClient(completion(reason=reason))).generate([])
        with self.assertRaises(ModelResponseError):
            OpenAIChatClient(SETTINGS, FakeClient(completion(text=" "))).generate([])

    def test_sdk_failure_is_propagated(self):
        client = FakeClient()
        client.chat.completions.create = lambda **_: (_ for _ in ()).throw(RuntimeError("OpenAI unavailable"))
        with self.assertRaisesRegex(RuntimeError, "OpenAI unavailable"):
            OpenAIChatClient(SETTINGS, client).generate([])

    def test_config_precedence_and_unset_inference(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory)
            (path / "config.yaml").write_text(
                "model: yaml-model\ninference_config:\n  max_completion_tokens:\n",
                encoding="utf-8")
            self.assertEqual(load_agent_config(path, "AGENT_1").model.model, "yaml-model")
            os.environ.update(OPENAI_MODEL="global-model", AGENT_1_MODEL="agent-model", OPENAI_API_KEY="sk-env")
            self.assertEqual(load_agent_config(path, "AGENT_1").model.model, "agent-model")
            self.assertEqual(load_agent_config(path, "AGENT_2").model.model, "global-model")
            self.assertEqual(load_agent_config(path, "AGENT_2").model.api_key, "sk-env")
            config = load_agent_config(path, "AGENT_1", SETTINGS)
            self.assertEqual(config.model, SETTINGS)
            self.assertEqual(config.inference_config, {})

    def test_factory_uses_independent_agent_settings(self):
        with patch.dict(os.environ, {"OPENAI_MODEL": "global-model", "AGENT_2_MODEL": "second-model"}, clear=True):
            agents = create_agents()
        self.assertEqual(agents["agent_1_classification"].provider.settings.model, "global-model")
        self.assertEqual(agents["agent_2_risk_scoring"].provider.settings.model, "second-model")
        self.assertIsNot(agents["agent_1_classification"].provider, agents["agent_2_risk_scoring"].provider)

    def test_malformed_config_fails_clearly(self):
        for text in ("[]", "false", "[not, mapping]", "model: 123", "region: x", "inference_config: [invalid]",
                     "inference_config: []", "inference_config: false",
                     "inference_config:\n  unknown: 1", "typo: value", "broken: ["):
            with TemporaryDirectory() as directory:
                path = Path(directory)
                (path / "config.yaml").write_text(text, encoding="utf-8")
                with self.subTest(text=text), self.assertRaises(ConfigurationError):
                    load_agent_config(path, "AGENT_1")

    def test_main_entrypoints_construct_the_right_agent(self):
        from agents.agent_1_classification.main import create_agent as first
        from agents.agent_2_risk_scoring.main import create_agent as second
        from agents.agent_3_recommendation.main import create_agent as third
        self.assertEqual(first(provider=FakeProvider()).agent_id, "agent_1_classification")
        self.assertEqual(second(provider=FakeProvider()).agent_id, "agent_2_risk_scoring")
        self.assertEqual(third(provider=FakeProvider()).agent_id, "agent_3_recommendation")
        with self.assertRaises(ValueError):
            create_agent("unknown")


if __name__ == "__main__":
    unittest.main()
