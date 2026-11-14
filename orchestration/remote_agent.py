"""Agent facades for the existing graph. Execution happens in agent containers."""
from dataclasses import asdict
from common.schemas.agent_messages import AgentResult
from common.service_client import ServiceClient


class RemoteAgent:
    def __init__(self, agent_id, url, identity, *, guard=lambda: None):
        self.agent_id, self.identity, self.guard = agent_id, identity, guard
        self.client = ServiceClient(url)
        self.execution_spec = self.client.request("GET", "/v1/describe", guard=guard)
        if self.execution_spec.get("agent_id") != agent_id:
            raise ValueError("Remote agent identity mismatch")

    def _payload(self, **fields):
        return {"identity": self.identity, "execution_revision": self.execution_spec["execution_revision"], **fields}

    def build_messages(self, request):
        result = self.client.request("POST", "/v1/messages", json=self._payload(request=asdict(request)),
                                     guard=self.guard, request_id=self.identity["request_id"])
        return result["messages"]

    def run(self, request, *, memory, observer=None, trace_metadata=None):
        result = self.client.run_job(self._payload(operation="run", request=asdict(request),
                    memory=memory.snapshot(), policy=asdict(memory.policy)), guard=self.guard)
        parsed = AgentResult(**result["result"])
        if parsed.agent_id != self.agent_id or not parsed.text.strip() or parsed.stop_reason != "stop":
            raise ValueError("Invalid remote AgentResult")
        if result["execution_revision"] != self.execution_spec["execution_revision"]:
            raise ValueError("Agent configuration changed during execution")
        memory.__init__(memory.policy, memory.summarizer, result["memory"], memory.counter)
        return parsed

    def reflect(self, contract, output):
        """Long-term memory: the agent container proposes lessons into its own store (pending review)."""
        return self.client.run_job(self._payload(operation="reflect", contract=contract, output=output), guard=self.guard)

    def close(self):
        self.client.close()


class RemoteSummarizer:
    def __init__(self, agent):
        self.agent = agent

    def summarize(self, text, target_tokens, input_budget):
        response = self.agent.client.run_job(self.agent._payload(operation="summarize", text=text,
            target_tokens=target_tokens, input_budget=input_budget), guard=self.agent.guard)
        if not isinstance(response.get("text"), str) or not response["text"].strip():
            raise ValueError("Invalid remote summary")
        return response["text"]
