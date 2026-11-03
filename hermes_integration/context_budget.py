from dataclasses import dataclass
import json

from common.exceptions import ConfigurationError


class ContextBudgetExceeded(ValueError):
    """The request cannot fit without losing content."""


def conservative_token_count(messages: list[dict]) -> int:
    """Conservative UTF-8 estimate + envelope allowance, NOT a model tokenizer."""
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 32 * len(messages)


@dataclass(frozen=True)
class MemoryPolicy:
    max_input_tokens: int
    summary_max_tokens: int
    keep_recent_turns: int = 1

    def __post_init__(self):
        if (type(self.max_input_tokens) is not int or self.max_input_tokens <= 0
                or type(self.summary_max_tokens) is not int or self.summary_max_tokens <= 0
                or self.summary_max_tokens >= self.max_input_tokens
                or type(self.keep_recent_turns) is not int or self.keep_recent_turns < 0):
            raise ConfigurationError("Set positive input/summary budgets (summary < input) and non-negative recent turns")
