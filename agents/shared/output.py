"""Agent business output: parse the model's JSON answer and validate it with the agent's Pydantic model
(agents/<agent>/schemas/output.py). Used by BaseAgent (re-ask the model) and orchestration/extraction.py."""
import json

from pydantic import ValidationError

from common.schemas.business.shared import CHECK_CODES  # noqa: F401  re-exported for agents

MAX_ERRORS = 10


def duplicates(values) -> list:
    return sorted({value for value in values if values.count(value) > 1})


def parse_json(text):
    """The model's answer as a JSON object, tolerating a ```json fence; None when it is not one object."""
    raw = text.strip() if isinstance(text, str) else ""
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def model_errors(model, data) -> list[str]:
    """Readable validation errors (at most MAX_ERRORS), [] when `data` matches `model`."""
    try:
        model.model_validate(data)
    except ValidationError as exc:
        return [f"{'/'.join(map(str, error['loc'])) or '(root)'}: {error['msg'][:300]}" for error in exc.errors()[:MAX_ERRORS]]
    return []


def output_models() -> dict:
    from agents.agent_1_classification.schemas.output import ClassificationResult
    from agents.agent_2_risk_scoring.schemas.output import RiskScoringResult
    from agents.agent_3_recommendation.schemas.output import RecommendationResult
    return {"agent_1_classification": ClassificationResult, "agent_2_risk_scoring": RiskScoringResult,
            "agent_3_recommendation": RecommendationResult}


def agent_output(agent_id, result) -> dict:
    """Validated business output of an agent result ({agent_id, text, ...}) as plain JSON data; ValueError if invalid."""
    data = parse_json(result.get("text") if isinstance(result, dict) else None)
    if data is None:
        raise ValueError(f"{agent_id} output is not a JSON object")
    try:
        return output_models()[agent_id].model_validate(data).model_dump(mode="json")
    except ValidationError as exc:
        raise ValueError(f"{agent_id} output does not match its business schema: {model_errors(output_models()[agent_id], data)}") from exc
