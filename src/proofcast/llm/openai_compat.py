"""OpenAI-compatible Chat Completions adapter (OpenAI, OpenRouter, Ollama, any base_url).

``POST {base_url}/chat/completions`` with ``Authorization: Bearer <key>`` when a key is set.
Sends ``temperature: 0``. ``json_mode`` adds ``response_format: {"type": "json_object"}``
(supported by OpenAI and Ollama; off by default for unknown servers).
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from proofcast.llm.base import Completion, LLMError
from proofcast.llm.transport import check_base_url, post_json


class OpenAICompatProvider:
    min_output_tokens = 0

    def __init__(
        self,
        name: str,
        model: str,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 120.0,
        json_mode: bool = False,
        extra_headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        check_base_url(base_url, has_key=bool(api_key))
        self.name = name
        self.model = model
        self._key = api_key
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._json_mode = json_mode
        self._extra = dict(extra_headers or {})
        self._sleep = sleep
        self._client = httpx.Client(timeout=httpx.Timeout(timeout), transport=transport)

    def complete(
        self, system: str, prompt: str, *, max_tokens: int, temperature: float = 0.0
    ) -> Completion:
        headers = {"content-type": "application/json", **self._extra}
        if self._key:
            headers["authorization"] = f"Bearer {self._key}"
        body: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if self._json_mode:
            body["response_format"] = {"type": "json_object"}
        data = post_json(
            self._client,
            self._url,
            headers=headers,
            body=body,
            secrets=[self._key],
            sleep=self._sleep,
            label=self.name,
        )
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LLMError(f"{self.name}: unexpected response shape") from None
        if choice.get("finish_reason") == "length":
            raise LLMError(f"{self.name}: reply was cut off at max_tokens")
        usage = data.get("usage") or {}
        return Completion(
            text=str(text),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            model=str(data.get("model") or self.model),
        )
