from common.exceptions import ConfigurationError, ModelResponseError
from common.schemas.agent_messages import ModelResult
from common.settings import BedrockSettings


class BedrockClient:
    """Create an AWS client only on the first call; credentials use the boto3 chain."""

    def __init__(self, settings: BedrockSettings, client=None):
        self.settings = settings
        self._client = client

    def converse(self, request: dict) -> ModelResult:
        self.settings.validate()
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise ConfigurationError("Install project dependencies to use Bedrock (boto3)") from exc
            session = boto3.Session(profile_name=self.settings.profile or None)
            self._client = session.client(
                "bedrock-runtime", region_name=self.settings.region,
                config=Config(connect_timeout=10, read_timeout=120,
                              retries={"mode": "standard", "total_max_attempts": 3}),
            )
        # SDK exceptions propagate to the caller; never fabricate a successful result.
        response = self._client.converse(**request)
        reason = response.get("stopReason", "")
        if reason not in ("end_turn", "stop_sequence"):
            raise ModelResponseError(f"Bedrock did not complete a text response: {reason}")
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        text = "\n".join(block["text"] for block in blocks if isinstance(block.get("text"), str))
        if not text.strip():
            raise ModelResponseError("Bedrock returned no text")
        return ModelResult(text, response.get("usage", {}), reason)
