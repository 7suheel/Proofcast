"""Bring-your-own-key LLM layer: small httpx adapters, no gateway libraries (hard rule 7)."""

from proofcast.llm.base import BudgetExceededError, Completion, LLMError, Provider, TokenBudget
from proofcast.llm.fake import FakeProvider
from proofcast.llm.jsonout import JsonResult, complete_json

__all__ = [
    "BudgetExceededError",
    "Completion",
    "FakeProvider",
    "JsonResult",
    "LLMError",
    "Provider",
    "TokenBudget",
    "complete_json",
]
