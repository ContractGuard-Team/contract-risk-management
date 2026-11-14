class ConfigurationError(ValueError):
    """A required setting or instruction has not been configured."""


class IntegrationNotConfigured(NotImplementedError):
    """A business rule or external integration still needs an implementation."""


class TransientModelError(RuntimeError):
    """The model service was throttled/unavailable after SDK retries; the call may succeed later."""


class ModelResponseError(RuntimeError):
    """The model did not return a complete, usable text response."""


class SourceError(ValueError):
    """Invalid source descriptor or an unreadable/oversized source file."""
