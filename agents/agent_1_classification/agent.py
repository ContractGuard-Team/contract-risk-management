from agents.base import BaseAgent
from agents.agent_1_classification.context.context_builder import build_context
from agents.agent_1_classification.schemas.input import ClassificationInput
from agents.agent_1_classification.schemas.output import ClassificationOutput
from agents.agent_1_classification.tools.classify_clause import classify_clause
from agents.agent_1_classification.tools.extract_key_terms import extract_key_terms


class ClassificationAgent(BaseAgent):
    agent_id = "agent_1_classification"
    output_type = ClassificationOutput
    tool_functions = {"classify_clause": classify_clause, "extract_key_terms": extract_key_terms}

    def build_context(self, request):
        return build_context(request)

    def classify(self, request: ClassificationInput) -> ClassificationOutput:
        return self.run(request)
