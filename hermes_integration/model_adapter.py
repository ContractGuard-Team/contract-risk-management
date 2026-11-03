from copy import deepcopy

from hermes_integration.runtime import load_bedrock_adapter


class HermesModelAdapter:
    """Adapt project messages to the Converse format using the existing Hermes source."""

    def __init__(self, converter=None):
        self._converter = converter

    def build_request(self, messages, model_id, inference_config=None):
        converter = self._converter or load_bedrock_adapter()
        system, converted = converter.convert_messages_to_converse(deepcopy(messages), model=model_id)
        request = {"modelId": model_id, "messages": converted}
        if system:
            request["system"] = system
        if inference_config:
            request["inferenceConfig"] = deepcopy(inference_config)
        return request
