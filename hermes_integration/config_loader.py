from dataclasses import dataclass, field
from pathlib import Path
import re

from common.exceptions import ConfigurationError
from common.settings import BedrockSettings


@dataclass(frozen=True)
class AgentConfig:
    bedrock: BedrockSettings
    inference_config: dict = field(default_factory=dict)


def load_agent_config(directory: Path, env_prefix: str, settings=None) -> AgentConfig:
    import yaml

    path = directory / "config.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Cannot load agent config: {path}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"Agent config must be a mapping: {path}")
    unknown = set(raw) - {"region", "model_id", "profile", "inference_config"}
    if unknown:
        raise ConfigurationError(f"Unknown agent config keys: {list(unknown)}")
    env = BedrockSettings.from_env(env_prefix)
    values = {}
    for key in ("region", "model_id", "profile"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            raise ConfigurationError(f"{key} must be a string or null")
        values[key] = getattr(env, key) or (value or "").strip()
    inference = raw.get("inference_config")
    if inference is None:
        inference = {}
    if not isinstance(inference, dict):
        raise ConfigurationError("inference_config must be a mapping")
    unknown = set(inference) - {"maxTokens", "temperature", "topP", "stopSequences"}
    if unknown:
        raise ConfigurationError(f"Unknown inference_config keys: {list(unknown)}")
    # Keep model-specific choices unset. The selected model may impose additional limits.
    configured = {key: value for key, value in inference.items() if value is not None}
    return AgentConfig(settings if settings is not None else BedrockSettings(**values), configured)


def load_instructions(directory: Path, task_prompt: str) -> str:
    sections = []
    paths = ("prompts/system.md", "soul.md", "plan.md",
             "context/instructions.md", "constraints.md", task_prompt)
    for relative in paths:
        path = directory / relative
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigurationError(f"TODO: provide {path}") from exc
        text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()
        if not text:
            raise ConfigurationError(f"TODO: fill {path}")
        sections.append(text)
    return "\n\n".join(sections)
