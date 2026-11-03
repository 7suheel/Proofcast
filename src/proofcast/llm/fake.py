"""FakeProvider: a scripted provider for tests (hard rule 9: tests never call real LLMs)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from proofcast.llm.base import Completion, LLMError

Responder = Callable[[str, str], str]


@dataclass
class FakeCall:
    system: str
    prompt: str
    max_tokens: int
    temperature: float


class FakeProvider:
    """Returns scripted replies, in order, or computes them with a function.

    ``responses`` may be a list of strings (consumed in order; the last one repeats) or a
    callable ``(system, prompt) -> str``. Every call is recorded in :attr:`calls`.
    """

    name = "fake"

    def __init__(
        self,
        responses: Sequence[str] | Responder = (),
        *,
        model: str = "fake-model",
        fail: bool = False,
    ) -> None:
        self.model = model
        self._responses = responses
        self._fail = fail
        self.calls: list[FakeCall] = []

    def complete(
        self, system: str, prompt: str, *, max_tokens: int, temperature: float = 0.0
    ) -> Completion:
        self.calls.append(FakeCall(system, prompt, max_tokens, temperature))
        if self._fail:
            raise LLMError("fake provider failure")
        if callable(self._responses):
            text = self._responses(system, prompt)
        elif self._responses:
            index = min(len(self.calls) - 1, len(self._responses) - 1)
            text = self._responses[index]
        else:
            text = "{}"
        return Completion(
            text=text,
            input_tokens=(len(system) + len(prompt)) // 4,
            output_tokens=len(text) // 4,
            model=self.model,
        )
