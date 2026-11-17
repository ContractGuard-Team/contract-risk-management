from agents.base import BaseAgent
from agents.shared.output import duplicates
from agents.agent_1_classification.context.context_builder import build_context
from agents.agent_1_classification.schemas.input import ClassificationInput
from agents.agent_1_classification.schemas.output import ClassificationOutput, ClassificationResult
from agents.agent_1_classification.tools.classify_clause import classify_clause
from agents.agent_1_classification.tools.extract_key_terms import extract_key_terms


class ClassificationAgent(BaseAgent):
    agent_id = "agent_1_classification"
    output_type = ClassificationOutput
    output_model = ClassificationResult
    tool_functions = {"classify_clause": classify_clause, "extract_key_terms": extract_key_terms}

    def build_context(self, request):
        return build_context(request)

    def check_output(self, data, request):
        chunk_id = (request.context.get("workflow") or {}).get("chunk_id")
        problems = [f"chunk_id phải là {chunk_id}"] if chunk_id and data["chunk_id"] != chunk_id else []
        ids = [clause["clause_id"] for clause in data["clauses"]]
        if duplicates(ids):
            problems.append(f"clause_id trùng trong cùng chunk: {duplicates(ids)}")
        problems += [f"{c['clause_id']}: page_start > page_end" for c in data["clauses"] if c["page_start"] > c["page_end"]]
        return problems

    def classify(self, request: ClassificationInput) -> ClassificationOutput:
        return self.run(request)
