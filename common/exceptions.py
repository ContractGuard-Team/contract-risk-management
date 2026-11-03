class ConfigurationError(ValueError):
    """A required setting or instruction has not been configured."""


class IntegrationNotConfigured(NotImplementedError):
    """A business rule or external integration still needs an implementation."""


class ModelResponseError(RuntimeError):
    """The model did not return a complete, usable text response."""
