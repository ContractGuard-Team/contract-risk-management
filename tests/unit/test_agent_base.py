from contextlib import contextmanager
from copy import deepcopy
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
from common.settings import BedrockSettings
from hermes_integration.agent_factory import create_agent, create_agents
from hermes_integration.bedrock_provider import HermesBedrockProvider
from hermes_integration.config_loader import load_agent_config
from hermes_integration.memory_adapter import memory_context
from hermes_integration.tool_registry import ToolRegistry
from integrations.aws.bedrock_client import BedrockClient
from orchestration.pipeline import ContractRiskPipeline
from orchestration.cache import ContractCache
from hermes_integration.context_budget import MemoryPolicy
from monitoring.langfuse import NullObserver


SETTINGS = BedrockSettings("test-region", "test-model")


def test_pipeline(agents):
    return ContractRiskPipeline(agents, cache=ContractCache(), observer=NullObserver(),
        policies={key: MemoryPolicy(100000, 1000) for key in agents})


class FakeClient:
    def __init__(self, response=None):
        self.requests = []
        self.response = response

    def converse(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        if self.response is not None:
            return self.response
        return {"output": {"message": {"content": [{"text": "model result"}]}},
                "usage": {"inputTokens": 2, "outputTokens": 3}, "stopReason": "end_turn"}


class FakeProvider:
    def __init__(self):
        self.calls = []

    def generate(self, messages, *, inference_config=None):
        self.calls.append(deepcopy(messages))
        return ModelResult("result", {"inputTokens": 1}, "end_turn")


@contextmanager
def configured_agents(provider):
    agents = create_agents(provider=provider)
    with TemporaryDirectory() as directory:
        for agent in agents.values():
            agent.directory = Path(directory) / agent.agent_id
            for relative in ("prompts/system.md", "soul.md", "plan.md",
                             "constraints.md", "context/instructions.md", agent.task_prompt):
                path = agent.directory / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"Test instructions for {relative}", encoding="utf-8")
        yield agents


class AgentBaseTests(unittest.TestCase):
    def test_creation_does_not_require_boto3_or_aws_settings(self):
        with patch.dict(os.environ, {}, clear=True), patch.dict("sys.modules", {"boto3": None}):
            agents = create_agents()
            self.assertEqual(len(agents), 3)
            self.assertTrue(all(agent.provider.client._client is None for agent in agents.values()))

    def test_placeholder_instructions_stop_all_agents_before_model_call(self):
        provider = FakeProvider()
        for agent in create_agents(provider=provider).values():
            with self.subTest(agent=agent.agent_id), self.assertRaises(ConfigurationError):
                agent.run(AgentRequest("contract"))
        self.assertEqual(provider.calls, [])

    def test_each_instruction_file_is_required(self):
        provider = FakeProvider()
        with configured_agents(provider) as agents:
            agent = agents["agent_1_classification"]
            for relative in ("prompts/system.md", "soul.md", "plan.md",
                             "constraints.md", "context/instructions.md", agent.task_prompt):
                path = agent.directory / relative
                original = path.read_text(encoding="utf-8")
                path.write_text("\n<!-- TODO: fill later -->\n", encoding="utf-8")
                with self.subTest(file=relative), self.assertRaises(ConfigurationError):
                    agent.run(AgentRequest("contract"))
                path.write_text(original, encoding="utf-8")
        self.assertEqual(provider.calls, [])

    def test_three_agents_real_hermes_conversion_fake_bedrock(self):
        client = FakeClient()
        provider = HermesBedrockProvider(SETTINGS, client)
        with configured_agents(provider) as agents:
            agents["agent_1_classification"].inference_config = {"maxTokens": 42}
            result = test_pipeline(agents).run("Hợp đồng thử", {"case": ["one"]})
        self.assertEqual(list(result), list(agents))
        self.assertEqual(len(client.requests), 3)
        self.assertEqual(client.requests[0]["inferenceConfig"], {"maxTokens": 42})
        self.assertNotIn("inferenceConfig", client.requests[1])
        for request in client.requests:
            self.assertEqual(request["modelId"], "test-model")
            self.assertIn("Test instructions for plan.md", request["system"][0]["text"])
            self.assertIn("Test instructions for constraints.md", request["system"][0]["text"])
        body = json.loads(client.requests[2]["messages"][0]["content"][0]["text"])
        self.assertEqual(body["context"]["case"], ["one"])
        self.assertEqual(body["context"]["workflow"]["risk_scoring"]["text"], "model result")
        second_body = json.loads(client.requests[1]["messages"][0]["content"][0]["text"])
        self.assertEqual(second_body["context"]["workflow"]["classification"]["chunk_count"], 1)
        self.assertEqual(result["agent_3_recommendation"]["usage"]["outputTokens"], 3)

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
        second_run = json.loads(provider.calls[3][1]["content"])
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

    def test_missing_bedrock_config_fails_before_client_call(self):
        client = FakeClient()
        provider = HermesBedrockProvider(BedrockSettings(), client)
        with self.assertRaises(ConfigurationError):
            provider.generate([{"role": "user", "content": "hello"}])
        self.assertEqual(client.requests, [])

    def test_incomplete_or_empty_bedrock_response_is_rejected(self):
        for reason in ("max_tokens", "tool_use", "guardrail_intervened", "content_filtered", ""):
            with self.subTest(reason=reason), self.assertRaises(ModelResponseError):
                client = FakeClient({"stopReason": reason})
                BedrockClient(SETTINGS, client).converse({})
        with self.assertRaises(ModelResponseError):
            BedrockClient(SETTINGS, FakeClient({"stopReason": "end_turn"})).converse({})

    def test_sdk_failure_is_propagated(self):
        client = FakeClient()
        with patch.object(client, "converse", side_effect=RuntimeError("AWS unavailable")):
            with self.assertRaisesRegex(RuntimeError, "AWS unavailable"):
                BedrockClient(SETTINGS, client).converse({})

    def test_config_precedence_and_unset_inference(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory)
            (path / "config.yaml").write_text(
                "region: yaml-region\nmodel_id: yaml-model\ninference_config:\n  maxTokens:\n",
                encoding="utf-8")
            self.assertEqual(load_agent_config(path, "AGENT_1").bedrock.model_id, "yaml-model")
            os.environ["BEDROCK_MODEL_ID"] = "global-model"
            os.environ["AGENT_1_MODEL_ID"] = "agent-model"
            self.assertEqual(load_agent_config(path, "AGENT_1").bedrock.model_id, "agent-model")
            self.assertEqual(load_agent_config(path, "AGENT_2").bedrock.model_id, "global-model")
            config = load_agent_config(path, "AGENT_1", SETTINGS)
            self.assertEqual(config.bedrock, SETTINGS)
            self.assertEqual(config.inference_config, {})

    def test_factory_uses_independent_agent_settings(self):
        with patch.dict(os.environ, {"AWS_REGION": "global-region", "BEDROCK_MODEL_ID": "global-model",
                                     "AGENT_2_MODEL_ID": "second-model"}, clear=True):
            agents = create_agents()
        self.assertEqual(agents["agent_1_classification"].provider.settings.model_id, "global-model")
        self.assertEqual(agents["agent_2_risk_scoring"].provider.settings.model_id, "second-model")
        self.assertIsNot(agents["agent_1_classification"].provider, agents["agent_2_risk_scoring"].provider)

    def test_malformed_config_fails_clearly(self):
        for text in ("[]", "false", "[not, mapping]", "model_id: 123", "inference_config: [invalid]",
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
