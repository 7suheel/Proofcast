"""HTTP plumbing shared by the provider adapters: POST JSON, retry, redact secrets."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from proofcast.llm.base import LLMError

RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
MAX_ERROR_CHARS = 300
MAX_REPLY_BYTES = 5 * 1024 * 1024


def is_localhost(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")


def check_base_url(base_url: str, *, has_key: bool) -> None:
    """Refuse to send a key over plain HTTP to anything but localhost."""
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise LLMError(f"invalid base URL {base_url!r}")
    if parts.scheme == "http" and has_key and not is_localhost(base_url):
        raise LLMError(
            "refusing to send an API key over plain http to a non-localhost host; use https"
        )


def redact(text: str, secrets: Iterable[str | None]) -> str:
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "REDACTED")
    return text


def _error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        return response.text[:MAX_ERROR_CHARS]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        kind = err.get("type") or err.get("code") or ""
        msg = err.get("message") or ""
        return f"{kind}: {msg}"[:MAX_ERROR_CHARS]
    if isinstance(err, str):
        return err[:MAX_ERROR_CHARS]
    return str(data)[:MAX_ERROR_CHARS]


def post_json(
    client: httpx.Client,
    url: str,
    *,
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    secrets: Iterable[str | None],
    retries: int = 2,
    backoff: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    label: str = "LLM",
) -> dict[str, Any]:
    """POST ``body`` as JSON and return the parsed object. Errors never contain secrets."""
    secret_list = list(secrets)
    last = ""
    for attempt in range(retries + 1):
        try:
            response = client.post(url, headers=dict(headers), json=dict(body))
        except httpx.HTTPError as exc:
            last = f"{label} request failed: {type(exc).__name__}"
            if attempt < retries:
                sleep(backoff * 2**attempt)
                continue
            raise LLMError(redact(last, secret_list)) from None
        if response.status_code in RETRY_STATUS and attempt < retries:
            last = f"{label} HTTP {response.status_code}"
            delay = backoff * 2**attempt
            retry_after = response.headers.get("retry-after")
            if retry_after and retry_after.replace(".", "", 1).isdigit():
                delay = max(delay, min(float(retry_after), 60.0))
            sleep(delay)
            continue
        if response.status_code >= 400:
            message = f"{label} HTTP {response.status_code}: {_error_message(response)}"
            raise LLMError(redact(message, secret_list))
        if len(response.content) > MAX_REPLY_BYTES:
            raise LLMError(f"{label} reply larger than {MAX_REPLY_BYTES} bytes")
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError):
            raise LLMError(f"{label} returned non-JSON") from None
        if not isinstance(data, dict):
            raise LLMError(f"{label} returned unexpected JSON")
        return data
    raise LLMError(redact(last or f"{label} request failed", secret_list))
