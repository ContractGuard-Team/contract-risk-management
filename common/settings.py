from dataclasses import dataclass, field
import os

from common.exceptions import ConfigurationError


@dataclass(frozen=True)
class GeminiSettings:
    model: str = ""
    api_key: str = field(default="", repr=False)  # never logged, hashed or returned (see public())

    @classmethod
    def from_env(cls, prefix: str = ""):
        agent_model = os.getenv(f"{prefix}_MODEL", "").strip() if prefix else ""
        return cls(
            model=agent_model or os.getenv("GEMINI_MODEL", "").strip(),
            api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        )

    def validate(self):
        missing = [
            name
            for name, value in (
                ("GEMINI_MODEL", self.model),
                ("GEMINI_API_KEY", self.api_key),
            )
            if not isinstance(value, str) or not value.strip()
        ]
        if missing:
            raise ConfigurationError("TODO: configure Gemini " + ", ".join(missing))

    def public(self):
        return {"model": self.model}
