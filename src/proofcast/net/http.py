"""A small, polite HTTP client for data sources (hard rule 8).

* descriptive User-Agent on every request,
* timeouts on every request,
* retries with exponential backoff on 429, 5xx and transport errors (``Retry-After`` honoured,
  capped),
* a per-host rate limiter (minimum interval between requests),
* an optional on-disk cache with a time-to-live,
* secrets (API keys in query strings or headers) never appear in errors or cache metadata.

TLS verification is always on; there is deliberately no option to turn it off.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from proofcast import __version__

DEFAULT_USER_AGENT = f"proofcast/{__version__} (+https://github.com/7suheel/proofcast)"

SECRET_PARAM_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "key",
        "token",
        "access_token",
        "x_cg_demo_api_key",
        "x_cg_pro_api_key",
    }
)
SECRET_HEADER_NAMES = frozenset(
    {
        "authorization",
        "x-api-key",
        "x-cg-demo-api-key",
        "x-cg-pro-api-key",
        "api-key",
    }
)
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 60.0
MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class HttpError(RuntimeError):
    """A request failed. The message never contains secrets."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SourceBlockedError(HttpError):
    """The source answered, but not with data (for example a bot-check page)."""


def redact_url(url: str) -> str:
    """Return ``url`` with secret query parameters replaced by ``REDACTED``."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    query = [
        (k, "REDACTED" if k.lower() in SECRET_PARAM_NAMES else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _full_url(url: str, params: Mapping[str, str | int | float] | None) -> str:
    if not params:
        return url
    sep = "&" if urlsplit(url).query else "?"
    return url + sep + urlencode({k: str(v) for k, v in params.items()})


class RateLimiter:
    """Enforce a minimum interval between requests to the same host."""

    def __init__(
        self,
        default_interval: float = 0.0,
        per_host: Mapping[str, float] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.default_interval = default_interval
        self.per_host = dict(per_host or {})
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}

    def set_interval(self, host: str, seconds: float) -> None:
        self.per_host[host] = seconds

    def wait(self, host: str) -> None:
        interval = self.per_host.get(host, self.default_interval)
        last = self._last.get(host)
        now = self._clock()
        if last is not None and interval > 0:
            remaining = interval - (now - last)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last[host] = now


@dataclass(frozen=True)
class CachedResponse:
    status: int
    text: str
    fetched_at: float


class DiskCache:
    """Cache response bodies on disk, keyed by a hash of the full request."""

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.root = root
        self._clock = clock

    def _paths(self, key: str) -> tuple[Path, Path]:
        base = self.root / key[:2] / key
        return base.with_suffix(".body"), base.with_suffix(".meta.json")

    @staticmethod
    def key(method: str, full_url: str, headers: Mapping[str, str]) -> str:
        relevant = sorted((k.lower(), v) for k, v in headers.items() if k.lower() == "accept")
        raw = json.dumps([method, full_url, relevant])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str, ttl: float) -> CachedResponse | None:
        body_path, meta_path = self._paths(key)
        if not (body_path.exists() and meta_path.exists()):
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fetched_at = float(meta["fetched_at"])
            status = int(meta["status"])
        except (ValueError, KeyError, json.JSONDecodeError):
            return None
        if self._clock() - fetched_at > ttl:
            return None
        return CachedResponse(status, body_path.read_text(encoding="utf-8"), fetched_at)

    def put(self, key: str, redacted_url: str, status: int, text: str) -> None:
        body_path, meta_path = self._paths(key)
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_text(text, encoding="utf-8")
        meta = {"url": redacted_url, "status": status, "fetched_at": self._clock()}
        meta_path.write_text(json.dumps(meta), encoding="utf-8")


class HttpClient:
    """GET-only HTTP client used by every adapter.

    ``transport`` lets tests plug in :class:`httpx.MockTransport`; production code leaves it None.
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 1.0,
        cache_dir: Path | None = None,
        rate_limiter: RateLimiter | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self.user_agent = user_agent
        self.retries = retries
        self.backoff = backoff
        self.cache = DiskCache(cache_dir) if cache_dir is not None else None
        self.rate_limiter = rate_limiter or RateLimiter()
        self._sleep = sleep
        self.max_bytes = max_bytes
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout),
            transport=transport,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_text(
        self,
        url: str,
        *,
        params: Mapping[str, str | int | float] | None = None,
        headers: Mapping[str, str] | None = None,
        cache_ttl: float = 0.0,
        user_agent: str | None = None,
    ) -> str:
        """GET ``url`` and return the body as text. Raises :class:`HttpError` on failure."""
        headers = dict(headers or {})
        if user_agent is not None:
            headers["User-Agent"] = user_agent
        full = _full_url(url, params)
        safe = redact_url(full)
        if urlsplit(full).scheme != "https" and not _is_localhost(full):
            raise HttpError(f"refusing non-HTTPS request to {safe}")
        key = DiskCache.key("GET", full, headers) if self.cache else ""
        if self.cache and cache_ttl > 0:
            hit = self.cache.get(key, cache_ttl)
            if hit is not None:
                return hit.text
        text = self._get_with_retries(full, safe, headers)
        if self.cache and cache_ttl > 0:
            self.cache.put(key, safe, 200, text)
        return text

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str | int | float] | None = None,
        headers: Mapping[str, str] | None = None,
        cache_ttl: float = 0.0,
        user_agent: str | None = None,
    ) -> Any:
        """GET ``url`` and parse JSON. Non-JSON bodies raise :class:`SourceBlockedError`."""
        text = self.get_text(
            url, params=params, headers=headers, cache_ttl=cache_ttl, user_agent=user_agent
        )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            snippet = " ".join(text[:120].split())
            raise SourceBlockedError(
                f"expected JSON from {redact_url(_full_url(url, params))}, got: {snippet!r}"
            ) from exc

    def _get_with_retries(self, full: str, safe: str, headers: dict[str, str]) -> str:
        host = urlsplit(full).hostname or ""
        last_error: HttpError | None = None
        for attempt in range(self.retries + 1):
            self.rate_limiter.wait(host)
            try:
                with self._client.stream("GET", full, headers=headers) as response:
                    status = response.status_code
                    retry_after = response.headers.get("Retry-After")
                    body = self._read_capped(response, safe)
                    encoding = response.encoding or "utf-8"
            except httpx.HTTPError as exc:
                last_error = HttpError(f"{type(exc).__name__} for {safe}")
                self._backoff(attempt, None, host)
                continue
            if status in RETRY_STATUS and attempt < self.retries:
                last_error = HttpError(f"HTTP {status} for {safe}", status=status)
                self._backoff(attempt, retry_after, host)
                continue
            text = body.decode(encoding, errors="replace")
            if status >= 400:
                detail = error_snippet(text, full)
                suffix = f": {detail}" if detail else ""
                raise HttpError(f"HTTP {status} for {safe}{suffix}", status=status)
            return text
        assert last_error is not None
        raise last_error

    def _read_capped(self, response: httpx.Response, safe: str) -> bytes:
        """Read the body, stopping as soon as it exceeds ``max_bytes`` (never buffer more)."""
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > self.max_bytes:
                raise HttpError(f"response from {safe} larger than {self.max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    def _backoff(self, attempt: int, retry_after: str | None, host: str) -> None:
        if attempt >= self.retries:
            return
        # Never retry faster than the host's own minimum interval (e.g. GDELT: 5 s).
        delay = max(self.backoff * (2**attempt), 2 * self.rate_limiter.per_host.get(host, 0.0))
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = max(delay, min(float(retry_after), MAX_RETRY_AFTER_SECONDS))
        self._sleep(delay)


def _is_localhost(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")


def error_snippet(text: str, full_url: str, limit: int = 200) -> str:
    """A short, single-line excerpt of an error body, with secret query values removed.

    Some APIs explain a 4xx in the body (FRED: "Bad Request. ..."); showing it makes failures
    debuggable from CI logs. Values of secret query parameters are replaced in case the server
    echoes the request back.
    """
    snippet = " ".join(text.split())[: limit * 4]
    if snippet.lstrip().startswith("<"):
        return ""
    for k, v in parse_qsl(urlsplit(full_url).query, keep_blank_values=True):
        if k.lower() in SECRET_PARAM_NAMES and len(v) >= 4:
            snippet = snippet.replace(v, "REDACTED")
    return snippet[:limit]


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Copy of ``headers`` with secret values replaced by ``REDACTED``."""
    return {k: ("REDACTED" if k.lower() in SECRET_HEADER_NAMES else v) for k, v in headers.items()}
