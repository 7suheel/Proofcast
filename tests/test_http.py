from pathlib import Path

import httpx
import pytest

from mock_http import Router
from proofcast.net.http import (
    DiskCache,
    HttpClient,
    HttpError,
    RateLimiter,
    SourceBlockedError,
    redact_headers,
    redact_url,
)


def test_user_agent_sent() -> None:
    router = Router().text("api.example.com", "/", "ok")
    with router.client(user_agent="proofcast-test/1.0") as c:
        c.get_text("https://api.example.com/x")
    assert router.requests[0].headers["User-Agent"] == "proofcast-test/1.0"


def test_per_request_user_agent_override() -> None:
    router = Router().text("data.sec.gov", "/", "{}")
    with router.client() as c:
        c.get_text("https://data.sec.gov/x", user_agent="Me me@example.org")
    assert router.requests[0].headers["User-Agent"] == "Me me@example.org"


def test_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text="ok")

    sleeps: list[float] = []
    client = HttpClient(
        transport=httpx.MockTransport(flaky), retries=3, backoff=1.0, sleep=sleeps.append
    )
    assert client.get_text("https://api.example.com/") == "ok"
    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]


def test_retry_after_honoured_and_capped() -> None:
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(429, headers={"Retry-After": "100000"}),
            httpx.Response(200, text="ok"),
        ]
    )
    sleeps: list[float] = []
    client = HttpClient(
        transport=httpx.MockTransport(lambda r: next(responses)), retries=3, sleep=sleeps.append
    )
    assert client.get_text("https://api.example.com/") == "ok"
    assert sleeps == [7.0, 60.0]


def test_gives_up_after_retries() -> None:
    client = HttpClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        retries=2,
        sleep=lambda s: None,
    )
    with pytest.raises(HttpError) as info:
        client.get_text("https://api.example.com/")
    assert info.value.status == 500


def test_transport_errors_retried() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    client = HttpClient(transport=httpx.MockTransport(boom), retries=1, sleep=lambda s: None)
    with pytest.raises(HttpError, match="ConnectTimeout"):
        client.get_text("https://api.example.com/")


def test_4xx_not_retried() -> None:
    router = Router().text("api.example.com", "/", "nope", status=403)
    with router.client(retries=3) as c, pytest.raises(HttpError, match="403"):
        c.get_text("https://api.example.com/x")
    assert len(router.requests) == 1


def test_errors_never_contain_secrets() -> None:
    router = Router().text("api.stlouisfed.org", "/", "bad", status=400)
    with router.client() as c, pytest.raises(HttpError) as info:
        c.get_text("https://api.stlouisfed.org/x", params={"api_key": "SECRET123", "id": "A"})
    assert "SECRET123" not in str(info.value)
    assert "REDACTED" in str(info.value)


def test_non_https_refused() -> None:
    router = Router()
    with router.client() as c, pytest.raises(HttpError, match="non-HTTPS"):
        c.get_text("http://api.example.com/x")
    assert router.requests == []


def test_json_error_on_html() -> None:
    router = Router().text("api.example.com", "/", "<html>blocked</html>")
    with router.client() as c, pytest.raises(SourceBlockedError, match="expected JSON"):
        c.get_json("https://api.example.com/x?key=SECRET")


def test_response_size_cap() -> None:
    router = Router().text("api.example.com", "/", "x" * 2000)
    with router.client(max_bytes=1000) as c, pytest.raises(HttpError, match="larger"):
        c.get_text("https://api.example.com/x")


def test_cache_hit_and_expiry(tmp_path: Path) -> None:
    router = Router().text("api.example.com", "/", "fresh")
    with router.client(cache_dir=tmp_path) as c:
        assert c.get_text("https://api.example.com/x", cache_ttl=60) == "fresh"
        assert c.get_text("https://api.example.com/x", cache_ttl=60) == "fresh"
        assert len(router.requests) == 1
        c.get_text("https://api.example.com/x")  # ttl 0: never cached
        assert len(router.requests) == 2


def test_cache_metadata_redacted(tmp_path: Path) -> None:
    router = Router().text("api.example.com", "/", "body")
    with router.client(cache_dir=tmp_path) as c:
        c.get_text("https://api.example.com/x", params={"api_key": "TOPSECRET"}, cache_ttl=60)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert "TOPSECRET" not in path.read_text(encoding="utf-8")


def test_disk_cache_expires(tmp_path: Path) -> None:
    now = {"t": 1000.0}
    cache = DiskCache(tmp_path, clock=lambda: now["t"])
    key = DiskCache.key("GET", "https://e.com", {})
    cache.put(key, "https://e.com", 200, "hello")
    assert cache.get(key, ttl=10) is not None
    now["t"] += 11
    assert cache.get(key, ttl=10) is None


def test_rate_limiter_waits_per_host() -> None:
    clock = {"t": 0.0}
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        clock["t"] += s

    limiter = RateLimiter(per_host={"a.com": 5.0}, clock=lambda: clock["t"], sleep=sleep)
    limiter.wait("a.com")
    clock["t"] += 1.0
    limiter.wait("a.com")
    limiter.wait("b.com")
    limiter.wait("b.com")
    assert sleeps == [4.0]


def test_redact_helpers() -> None:
    assert "abc" not in redact_url("https://x.com/p?api_key=abc&q=1")
    assert redact_url("https://x.com/p") == "https://x.com/p"
    headers = redact_headers({"Authorization": "Bearer s", "x-api-key": "k", "Accept": "a"})
    assert headers == {"Authorization": "REDACTED", "x-api-key": "REDACTED", "Accept": "a"}


def test_backoff_respects_host_interval() -> None:
    responses = iter([httpx.Response(429), httpx.Response(200, text="ok")])
    sleeps: list[float] = []
    limiter = RateLimiter(
        per_host={"api.gdeltproject.org": 5.5}, clock=lambda: 0.0, sleep=lambda s: None
    )
    client = HttpClient(
        transport=httpx.MockTransport(lambda r: next(responses)),
        rate_limiter=limiter,
        sleep=sleeps.append,
    )
    assert client.get_text("https://api.gdeltproject.org/x") == "ok"
    assert sleeps == [11.0]
