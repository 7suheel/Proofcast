"""Anthropic Messages API adapter (raw httpx, no SDK: hard rule 7).

* Endpoint ``POST {base_url}/v1/messages`` with ``x-api-key`` and
  ``anthropic-version: 2026-10-21``.
* No ``temperature``: since API version 2026-10-21 the Messages API rejects sampling
  parameters on every model. Determinism comes from our strict JSON schema and validation
  instead; the ``temperature`` argument of :meth:`complete` is accepted and ignored.
* Current models think before answering and return ``thinking`` blocks; only ``text`` blocks
  are read. Thinking counts toward ``max_tokens``, so a floor of
  :data:`MIN_OUTPUT_TOKENS` is applied.
* ``stop_reason == "refusal"`` and ``"max_tokens"`` are errors (callers fall back safely).
* For models that support it, ``fallbacks: "default"`` (beta
  ``server-side-fallback-2026-07-01``) lets the API re-run a declined request on Anthropic's
  recommended fallback model.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from proofcast.llm.base import Completion, LLMError
from proofcast.llm.transport import check_base_url, post_json

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2026-10-21"
SUGGESTED_MODEL = "claude-opus-5-5"
MIN_OUTPUT_TOKENS = 8000
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = frozenset(
    {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
)


class AnthropicProvider:
    name = "anthropic"
    min_output_tokens = MIN_OUTPUT_TOKENS

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 300.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        check_base_url(base_url, has_key=True)
        self.model = model
        self._key = api_key
        self._url = base_url.rstrip("/") + "/v1/messages"
        self._sleep = sleep
        self._client = httpx.Client(timeout=httpx.Timeout(timeout), transport=transport)

    def complete(
        self, system: str, prompt: str, *, max_tokens: int, temperature: float = 0.0
    ) -> Completion:
        headers = {
            "x-api-key": self._key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        body: dict[str, object] = {
            "model": self.model,
            "max_tokens": max(max_tokens, MIN_OUTPUT_TOKENS),
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.model in FALLBACK_MODELS:
            headers["anthropic-beta"] = FALLBACK_BETA
            body["fallbacks"] = "default"
        data = post_json(
            self._client,
            self._url,
            headers=headers,
            body=body,
            secrets=[self._key],
            sleep=self._sleep,
            label="anthropic",
        )
        stop = data.get("stop_reason")
        if stop == "refusal":
            details = data.get("stop_details") or {}
            category = details.get("category") if isinstance(details, dict) else None
            raise LLMError(f"anthropic: the model declined the request (category: {category})")
        if stop == "max_tokens":
            raise LLMError("anthropic: reply was cut off at max_tokens")
        blocks = data.get("content") or []
        text = "".join(
            b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )
        usage = data.get("usage") or {}
        return Completion(
            text=text,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            model=str(data.get("model") or self.model),
        )
