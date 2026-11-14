from dataclasses import dataclass, field
from pathlib import Path
import re

from common.exceptions import ConfigurationError
from common.settings import OpenAISettings
from integrations.external.openai_client import INFERENCE_KEYS


@dataclass(frozen=True)
class AgentConfig:
    model: OpenAISettings
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
    unknown = set(raw) - {"model", "inference_config"}
    if unknown:
        raise ConfigurationError(f"Unknown agent config keys: {list(unknown)}")
    env = OpenAISettings.from_env(env_prefix)
    model = raw.get("model")
    if model is not None and not isinstance(model, str):
        raise ConfigurationError("model must be a string or null")
    inference = raw.get("inference_config")
    if inference is None:
        inference = {}
    if not isinstance(inference, dict):
        raise ConfigurationError("inference_config must be a mapping")
    unknown = set(inference) - set(INFERENCE_KEYS)
    if unknown:
        raise ConfigurationError(f"Unknown inference_config keys: {list(unknown)}")
    # Keep model-specific choices unset. The selected model may impose additional limits.
    configured = {key: value for key, value in inference.items() if value is not None}
    if settings is None:
        settings = OpenAISettings(env.model or (model or "").strip(), env.api_key)
    return AgentConfig(settings, configured)


# The three prompt types of every agent (plan.md is process documentation and is NOT sent to the model).
CORE_PROMPT = "prompts/core.md"          # xử lý trung tâm: how to process the contract into the right output
EXTERNAL_PROMPT = "prompts/external.md"  # xử lý ngoài luồng: when to use external services / supplied context
EXAMPLES_DIR = "prompts/examples"        # example_*.md: worked examples; optional, empty files are ignored


def _text(path):
    try:
        return re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.DOTALL).strip()
    except OSError as exc:
        raise ConfigurationError(f"TODO: provide {path}") from exc


def prompt_files(directory: Path) -> list[str]:
    """Relative paths of every prompt file (for execution revisions); examples in name order."""
    examples = sorted(p.relative_to(directory).as_posix() for p in (directory / EXAMPLES_DIR).glob("*.md"))
    return [CORE_PROMPT, EXTERNAL_PROMPT, *examples]


def load_prompts(directory: Path) -> dict:
    """{"core", "external", "examples": [...]}. Core and external must be filled before any model call."""
    prompts = {}
    for key, relative in (("core", CORE_PROMPT), ("external", EXTERNAL_PROMPT)):
        prompts[key] = _text(directory / relative)
        if not prompts[key]:
            raise ConfigurationError(f"TODO: fill {directory / relative}")
    prompts["examples"] = [text for relative in prompt_files(directory)[2:] if (text := _text(directory / relative))]
    return prompts
