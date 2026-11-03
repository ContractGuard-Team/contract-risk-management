from common.settings import BedrockSettings
from hermes_integration.model_adapter import HermesModelAdapter
from integrations.aws.bedrock_client import BedrockClient


class HermesBedrockProvider:
    def __init__(self, settings: BedrockSettings, client=None, adapter=None):
        self.settings = settings
        self.client = BedrockClient(settings, client)
        self.adapter = adapter if adapter is not None else HermesModelAdapter()

    def generate(self, messages, *, inference_config=None):
        self.settings.validate()
        request = self.adapter.build_request(messages, self.settings.model_id, inference_config)
        return self.client.converse(request)
