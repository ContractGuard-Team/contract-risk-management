from importlib import import_module
from pathlib import Path
import sys

from common.exceptions import ConfigurationError


def load_bedrock_adapter():
    """Load only Hermes's converter, without starting its CLI, tools or AIAgent loop."""
    source = Path(__file__).resolve().parents[1] / "third_party" / "hermes-agent"
    if not (source / "agent" / "bedrock_adapter.py").is_file():
        raise ConfigurationError("Provide third_party/hermes-agent in the source checkout")
    for name in ("agent", "pm"):
        existing = sys.modules.get(name)
        if existing is not None:
            origin = getattr(existing, "__file__", None)
            if not origin or not Path(origin).resolve().is_relative_to(source.resolve()):
                raise ConfigurationError(f"Another '{name}' package shadows the Hermes source")
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    try:
        return import_module("agent.bedrock_adapter")
    except ImportError as exc:
        raise ConfigurationError("Cannot import Hermes Bedrock adapter; install its required dependencies") from exc
