from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

from common.exceptions import ConfigurationError

PROJECT_ENV = Path(__file__).resolve().parents[1] / ".env"


@dataclass(frozen=True)
class LangfuseSettings:
    enabled: bool = False
    public_key: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    base_url: str = ""
    capture_content: bool = False
    environment: str = "development"
    release: str | None = None

    @classmethod
    def load(cls, env_file=PROJECT_ENV):
        # Read only the explicitly selected file; never search parent folders or mutate os.environ.
        values = dict(dotenv_values(env_file, interpolate=False)) if env_file is not None else {}
        values.update({k: v for k, v in os.environ.items() if k.startswith("LANGFUSE_")})
        def text(name, default=""):
            return (values.get(name) or default).strip()
        settings = cls(
            enabled=text("LANGFUSE_ENABLED").lower() in ("true", "1"),
            public_key=text("LANGFUSE_PUBLIC_KEY"), secret_key=text("LANGFUSE_SECRET_KEY"),
            base_url=text("LANGFUSE_BASE_URL"),
            capture_content=text("LANGFUSE_CAPTURE_CONTENT").lower() in ("true", "1"),
            environment=text("LANGFUSE_ENVIRONMENT", "development"),
            release=text("LANGFUSE_RELEASE") or None)
        if settings.enabled:
            if not all((settings.public_key, settings.secret_key, settings.base_url)):
                raise ConfigurationError("Configure Langfuse public key, secret key and base URL")
            url = urlparse(settings.base_url)
            if url.scheme not in ("https", "http") or not url.netloc or url.username or url.password:
                raise ConfigurationError("LANGFUSE_BASE_URL must be an HTTP(S) URL without credentials")
        return settings
