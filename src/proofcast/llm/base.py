"""Provider protocol, completions and the per-run token budget."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class LLMError(RuntimeError):
    """An LLM call failed. Messages never contain API keys."""


class BudgetExceededError(LLMError):
    """The run's token budget (``PROOFCAST_MAX_TOKENS``) would be exceeded."""


@dataclass(frozen=True)
class Completion:
    """The text a model returned plus token usage."""

    text: str
    input_tokens: int
    output_tokens: int
    model: str

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@runtime_checkable
class Provider(Protocol):
    """A text-completion backend. Agents get no tools, no network and no file access: they
    only receive the text our code puts in ``system`` and ``prompt`` (hard rule 6)."""

    name: str
    model: str

    def complete(
        self, system: str, prompt: str, *, max_tokens: int, temperature: float = 0.0
    ) -> Completion: ...


class TokenBudget:
    """Track tokens used in one run and refuse calls that could exceed the limit."""

    def __init__(self, limit: int | None = None) -> None:
        if limit is not None and limit <= 0:
            raise ValueError("token budget must be positive")
        self.limit = limit
        self.used = 0
        self.calls = 0

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.used)

    def check(self, prompt_chars: int, max_tokens: int) -> None:
        """Raise before a call whose worst case (rough prompt estimate + max output) is over."""
        if self.limit is None:
            return
        estimate = prompt_chars // 4 + max_tokens
        if self.used + estimate > self.limit:
            raise BudgetExceededError(
                f"token budget exhausted: used {self.used} of {self.limit}, next call needs up "
                f"to ~{estimate}"
            )

    def add(self, completion: Completion) -> None:
        self.used += completion.total_tokens
        self.calls += 1
