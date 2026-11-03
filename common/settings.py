from dataclasses import dataclass
import os

from common.exceptions import ConfigurationError


@dataclass(frozen=True)
class BedrockSettings:
    region: str = ""
    model_id: str = ""
    profile: str = ""

    @classmethod
    def from_env(cls, prefix: str = ""):
        def value(name, fallback):
            return (os.getenv(f"{prefix}_{name}", "") if prefix else "").strip() or os.getenv(fallback, "").strip()

        return cls(value("REGION", "AWS_REGION"),
                   value("MODEL_ID", "BEDROCK_MODEL_ID"),
                   value("PROFILE", "AWS_PROFILE"))

    def validate(self):
        missing = [name for name, value in
                   (("region", self.region), ("model_id", self.model_id))
                   if not isinstance(value, str) or not value.strip()]
        if missing:
            raise ConfigurationError("TODO: configure Bedrock " + ", ".join(missing))
        if not isinstance(self.profile, str):
            raise ConfigurationError("Bedrock profile must be a string")
