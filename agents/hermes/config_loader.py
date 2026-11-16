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


# Prompt types of every agent (plan.md is process documentation and is NOT sent to the model).
SOUL_PROMPT = "prompts/soul.md"          # vai trò/tính cách của agent; nạp đầu tiên
CORE_PROMPT = "prompts/core.md"          # xử lý trung tâm: how to process the contract into the right output
EXTERNAL_PROMPT = "prompts/external.md"  # xử lý ngoài luồng: when to use external services / supplied context
EXAMPLES_DIR = "prompts/examples"        # example_*.md: worked examples; optional, empty files are ignored
RULES_DIR = "prompts/rules"              # reference rule files (*.md, *.json) the agent must check; optional
OUTPUT_SCHEMA = "schemas/output.schema.json"  # output JSON schema written in source code; optional until designed
REQUIRED = (("soul", SOUL_PROMPT), ("core", CORE_PROMPT), ("external", EXTERNAL_PROMPT))


def _text(path):
    try:
        return re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.DOTALL).strip()
    except OSError as exc:
        raise ConfigurationError(f"TODO: provide {path}") from exc


def _listed(directory: Path, folder: str, patterns) -> list[str]:
    return sorted({p.relative_to(directory).as_posix() for pattern in patterns for p in (directory / folder).glob(pattern)})


def prompt_files(directory: Path) -> list[str]:
    """Relative paths of every prompt file (for execution revisions); examples/rules in name order."""
    optional = [OUTPUT_SCHEMA] if (directory / OUTPUT_SCHEMA).is_file() else []
    return [*(relative for _, relative in REQUIRED), *_listed(directory, EXAMPLES_DIR, ["*.md"]),
            *_listed(directory, RULES_DIR, ["*.md", "*.json"]), *optional]


def load_prompts(directory: Path) -> dict:
    """{"soul", "core", "external", "examples": [...], "rules": [(name, text)], "output_schema": str}.
    Soul, core and external must be filled before any model call."""
    prompts = {}
    for key, relative in REQUIRED:
        prompts[key] = _text(directory / relative)
        if not prompts[key]:
            raise ConfigurationError(f"TODO: fill {directory / relative}")
    prompts["examples"] = [text for relative in _listed(directory, EXAMPLES_DIR, ["*.md"])
                           if (text := _text(directory / relative))]
    prompts["rules"] = [(Path(relative).name, text) for relative in _listed(directory, RULES_DIR, ["*.md", "*.json"])
                        if (text := _text(directory / relative))]
    prompts["output_schema"] = _text(directory / OUTPUT_SCHEMA) if (directory / OUTPUT_SCHEMA).is_file() else ""
    return prompts
