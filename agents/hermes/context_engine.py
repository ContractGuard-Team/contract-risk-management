"""Job-scoped implementation of the ContextEngine extension interface in the vendored Hermes."""
from copy import deepcopy
import json

from agents.hermes.context_budget import ContextBudgetExceeded, conservative_token_count
from agents.hermes.runtime import load_hermes_module

ContextEngine = load_hermes_module("agent.context_engine").ContextEngine


class ContractContextEngine(ContextEngine):
    name = "contract-memory"

    def __init__(self, policy, summarizer, snapshot=None, counter=conservative_token_count):
        self.policy = policy
        self.summarizer = summarizer
        self.counter = counter
        saved = deepcopy(snapshot or {})
        self.summary = saved.get("summary", "")
        self.turns = saved.get("turns", [])
        self.compression_count = saved.get("compression_count", 0)
        self.context_length = policy.max_input_tokens
        self.threshold_tokens = policy.max_input_tokens
        self.last_prompt_tokens = saved.get("last_prompt_tokens", 0)
        self.last_completion_tokens = saved.get("last_completion_tokens", 0)
        self.last_total_tokens = self.last_prompt_tokens + self.last_completion_tokens

    def snapshot(self):
        return deepcopy({"summary": self.summary, "turns": self.turns,
                         "compression_count": self.compression_count,
                         "last_prompt_tokens": self.last_prompt_tokens,
                         "last_completion_tokens": self.last_completion_tokens})

    def update_from_response(self, usage):
        self.last_prompt_tokens = usage.get("prompt_tokens", 0)
        self.last_completion_tokens = usage.get("completion_tokens", 0)
        self.last_total_tokens = self.last_prompt_tokens + self.last_completion_tokens

    def should_compress(self, prompt_tokens=None):
        return (self.last_prompt_tokens if prompt_tokens is None else prompt_tokens) > self.threshold_tokens

    def _assemble(self, messages):
        system = [m for m in messages if m["role"] == "system"]
        current = [m for m in messages if m["role"] != "system"]
        history = []
        if self.summary:
            history.append({"role": "user", "content": json.dumps(
                {"prior_contract_memory": self.summary}, ensure_ascii=False)})
        for turn in self.turns:
            history.extend(turn)
        return deepcopy(system + history + current)

    def prepare(self, messages):
        # The current chunk and system instructions are never truncated.
        if self.counter(messages) > self.threshold_tokens:
            raise ContextBudgetExceeded("Current input alone exceeds budget; split the chunk or compact its handoff")
        while self.counter(self._assemble(messages)) > self.threshold_tokens:
            before = self.counter(self._assemble(messages))
            if self.turns:
                count = max(1, len(self.turns) - self.policy.keep_recent_turns)
                old = self.turns[:count]
                self.summary = self.summarizer.summarize(
                    json.dumps({"summary": self.summary, "turns": old}, ensure_ascii=False),
                    self.policy.summary_max_tokens, self.policy.max_input_tokens)
                self.turns = self.turns[count:]
            elif self.summary:
                target = min(self.policy.summary_max_tokens, max(1, self.counter(
                    [{"role": "user", "content": self.summary}]) // 2))
                self.summary = self.summarizer.summarize(
                    self.summary, target, self.policy.max_input_tokens)
            else:
                raise ContextBudgetExceeded("Input does not fit memory budget")
            self.compression_count += 1
            if self.counter(self._assemble(messages)) >= before:
                raise ContextBudgetExceeded("Memory summary did not shrink enough; adjust summarizer/budgets")
        return self._assemble(messages)

    def compress(self, messages, current_tokens=None, focus_topic=None, force=False, memory_context=""):
        return self.prepare(messages)

    def record(self, current_messages, result):
        # Store the current input only; never recursively persist the replayed history.
        turn = [deepcopy(m) for m in current_messages if m["role"] != "system"]
        turn.append({"role": "assistant", "content": result.text})
        self.turns.append(turn)
        self.update_from_response(result.usage)
