"""Submit a prepared manifest to LangGraph; worker never orchestrates agent calls."""
from common.service_client import ServiceClient


class WorkflowServiceClient(ServiceClient):
    def execute(self, contract, identity, *, guard, on_progress):
        result = self.run_job({"identity": identity, "contract": contract}, guard=guard, on_progress=on_progress)
        if result.get("job_id") != identity["job_id"] or result.get("runtime_id") != identity["runtime_id"]:
            raise ValueError("Workflow result identity mismatch")
        if result.get("status") != "completed":
            raise ValueError("Workflow did not complete")
        # LangGraph built and schema-checked extraction.v2 from the three agents (orchestration/extraction.py).
        extraction = result.get("extraction")
        if not isinstance(extraction, dict) or extraction.get("schema_version") != "2.0.0" \
                or extraction.get("job_id") != identity["job_id"] or not extraction.get("clauses"):
            raise ValueError("Workflow returned no valid extraction.v2")
        return extraction
