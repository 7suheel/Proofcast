"""Route requests to fixture files through httpx.MockTransport (no network)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from proofcast.net.http import HttpClient, RateLimiter

FIXTURES = Path(__file__).parent / "fixtures"

Handler = Callable[[httpx.Request], httpx.Response]


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@dataclass
class Router:
    """Map (host, path prefix) to a response; record every request."""

    routes: list[tuple[str, str, Handler]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def add(self, host: str, path: str, handler: Handler) -> Router:
        self.routes.append((host, path, handler))
        return self

    def text(self, host: str, path: str, body: str, status: int = 200) -> Router:
        return self.add(host, path, lambda r: httpx.Response(status, text=body))

    def file(self, host: str, path: str, name: str, status: int = 200) -> Router:
        return self.text(host, path, fixture_text(name), status)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for host, path, handler in self.routes:
            if request.url.host == host and request.url.path.startswith(path):
                return handler(request)
        return httpx.Response(404, text=f"no route for {request.url}")

    def client(self, **kwargs: object) -> HttpClient:
        sleeps: list[float] = []
        limiter = RateLimiter(clock=lambda: 0.0, sleep=sleeps.append)
        return HttpClient(
            transport=httpx.MockTransport(self),
            rate_limiter=limiter,
            sleep=lambda s: None,
            **kwargs,  # type: ignore[arg-type]
        )
