"""Submit a prepared manifest to LangGraph; worker never orchestrates agent calls."""
from common.service_client import ServiceClient


class WorkflowServiceClient(ServiceClient):
    def execute(self, contract, identity, *, guard, on_progress):
        result = self.run_job({"identity": identity, "contract": contract}, guard=guard, on_progress=on_progress)
        if result.get("job_id") != identity["job_id"] or result.get("runtime_id") != identity["runtime_id"]:
            raise ValueError("Workflow result identity mismatch")
        if result.get("status") != "completed":
            raise ValueError("Workflow did not complete")
        final = result.get("results", {}).get("agent_3_recommendation")
        if not isinstance(final, dict) or final.get("agent_id") != "agent_3_recommendation" or not isinstance(final.get("text"), str) or not final["text"].strip():
            raise ValueError("Workflow returned no valid Agent 3 output")
        return final
