"""Ask for JSON, validate it against a Pydantic model, repair once, else fall back.

The model's reply is untrusted too: it is size-capped, parsed with ``json.loads`` only, and
validated against a strict schema. Anything that does not validate after one repair attempt
yields ``value=None`` so the caller can use its safe fallback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from proofcast.llm.base import LLMError, Provider, TokenBudget

MAX_REPLY_CHARS = 60_000

REPAIR_PROMPT = (
    "Your previous reply could not be used: {error}\n"
    "Reply again with ONLY one JSON object that matches the required schema. "
    "No prose, no code fences."
)


@dataclass
class JsonResult[T: BaseModel]:
    value: T | None
    raw: str = ""
    repaired: bool = False
    notes: list[str] = field(default_factory=list)
    tokens: int = 0


def extract_json_object(text: str) -> object:
    """Parse the first JSON object in ``text`` (tolerates code fences and surrounding prose)."""
    text = text[:MAX_REPLY_CHARS].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                value, _ = decoder.raw_decode(text, i)
            except json.JSONDecodeError:
                continue
            return value
    raise ValueError("no JSON object found")


def _parse[T: BaseModel](text: str, schema: type[T]) -> T:
    data = extract_json_object(text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return schema.model_validate(data)


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = [
            f"{'.'.join(str(p) for p in e['loc']) or 'root'}: {e['msg']}" for e in exc.errors()[:5]
        ]
        return "; ".join(parts)
    return str(exc)[:300]


def complete_json[T: BaseModel](
    provider: Provider,
    system: str,
    prompt: str,
    schema: type[T],
    *,
    budget: TokenBudget | None = None,
    max_tokens: int = 1500,
) -> JsonResult[T]:
    """Call ``provider`` and return a validated ``schema`` instance, or ``value=None``."""
    budget = budget or TokenBudget()
    result: JsonResult[T] = JsonResult(value=None)
    conversation = prompt
    for attempt in range(2):
        budget.check(len(system) + len(conversation), max_tokens)
        try:
            completion = provider.complete(
                system, conversation, max_tokens=max_tokens, temperature=0.0
            )
        except LLMError as exc:
            result.notes.append(f"LLM call failed: {exc}")
            return result
        budget.add(completion)
        result.tokens += completion.total_tokens
        result.raw = completion.text[:MAX_REPLY_CHARS]
        try:
            result.value = _parse(completion.text, schema)
            result.repaired = attempt == 1
            return result
        except (ValueError, ValidationError) as exc:
            error = _short_error(exc)
            result.notes.append(f"invalid JSON reply (attempt {attempt + 1}): {error}")
            conversation = (
                f"{prompt}\n\n<previous_reply>\n{result.raw[:4000]}\n</previous_reply>\n\n"
                + REPAIR_PROMPT.format(error=error)
            )
    result.notes.append("falling back: no valid JSON after one repair attempt")
    return result
