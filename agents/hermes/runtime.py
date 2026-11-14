from importlib import import_module
from pathlib import Path
import sys

from common.exceptions import ConfigurationError


def load_hermes_module(module_name):
    """Load a Hermes extension module without starting its CLI or tool runtime."""
    if module_name == "agent.context_engine" and sys.version_info < (3, 11):
        raise ConfigurationError("Hermes ContextEngine requires Python >=3.11; use the project .venv312 environment")
    source = Path(__file__).resolve().parent / "hermes_agent"
    if not (source / "agent" / "context_engine.py").is_file():
        raise ConfigurationError("Provide agents/hermes/hermes_agent (Hermes source) in the checkout/image")
    for name in ("agent", "pm"):
        existing = sys.modules.get(name)
        if existing is not None:
            origin = getattr(existing, "__file__", None)
            if not origin or not Path(origin).resolve().is_relative_to(source.resolve()):
                raise ConfigurationError(f"Another '{name}' package shadows the Hermes source")
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    try:
        return import_module(module_name)
    except ImportError as exc:
        raise ConfigurationError(f"Cannot import Hermes module {module_name}; install its required dependencies") from exc

