from pathlib import Path
import re

from common.exceptions import ConfigurationError, ModelResponseError
from agents.hermes.context_budget import ContextBudgetExceeded, conservative_token_count
from monitoring.observer import NullObserver
from monitoring.usage import model_usage


class ModelMemorySummarizer:
    """Bounded map/reduce through the same model provider; every source piece is visited."""

    def __init__(self, provider, prompt_path, observer=None, counter=conservative_token_count):
        """prompt_path: the owning agent's agents/<agent>/memory/short_term_summary.md (never shared)."""
        self.provider = provider
        self.observer = observer if observer is not None else NullObserver()
        self.prompt_path = Path(prompt_path)
        self.counter = counter

    def _messages(self, text, target):
        raw = self.prompt_path.read_text(encoding="utf-8") if self.prompt_path.is_file() else ""
        prompt = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL).strip()
        if not prompt:
            raise ConfigurationError(f"TODO: fill {self.prompt_path} before context compression")
        return [{"role": "system", "content": prompt},
                {"role": "user", "content": f"Output budget: {target} tokens\nSource memory:\n{text}"}]

    def summarize(self, text, target_tokens, input_budget):
        current = text
        while True:
            if self.counter(self._messages(current, target_tokens)) <= input_budget:
                return self._call(current, target_tokens)
            parts = []
            offset = 0
            while offset < len(current):
                low, high = 0, len(current) - offset
                while low < high:
                    middle = (low + high + 1) // 2
                    if self.counter(self._messages(current[offset:offset + middle], target_tokens)) <= input_budget:
                        low = middle
                    else:
                        high = middle - 1
                if not low:
                    raise ContextBudgetExceeded("Summary prompt leaves no room for source memory")
                parts.append(self._call(current[offset:offset + low], target_tokens))
                offset += low
            reduced = "\n".join(parts)
            if len(reduced.encode("utf-8")) >= len(current.encode("utf-8")):
                raise ContextBudgetExceeded("Hierarchical memory reduction made no progress")
            current = reduced

    def _call(self, text, target):
        messages = self._messages(text, target)
        settings = getattr(self.provider, "settings", None)
        with self.observer.span("memory.summarize", kind="generation",
                               model=getattr(settings, "model", None)) as observation:
            observation.summary(input={"source_characters": len(text), "output_token_budget": target})
            observation.update(input=messages, model_parameters={"max_completion_tokens": target})
            result = self.provider.generate(messages, inference_config={"max_completion_tokens": target})
            observation.summary(output={"summary_characters": len(result.text) if isinstance(result.text, str) else 0,
                                        "stop_reason": result.stop_reason})
            observation.update(usage_details=model_usage(result.usage),
                               output=result.text)
            if result.stop_reason != "stop" or not isinstance(result.text, str) or not result.text.strip():
                raise ModelResponseError("Memory summary is incomplete or empty")
            return result.text
