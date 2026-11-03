"""BYOK provider adapters, tested with httpx.MockTransport only (never a real LLM)."""

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from conftest import REPO_ROOT
from proofcast.cli import app
from proofcast.config import load_config
from proofcast.doctor import run_doctor
from proofcast.llm.anthropic import API_VERSION, MIN_OUTPUT_TOKENS, AnthropicProvider
from proofcast.llm.base import LLMError
from proofcast.llm.factory import (
    ProviderConfigError,
    build_provider,
    parse_model_spec,
    provider_from_env,
    resolve_settings,
)
from proofcast.llm.jsonout import complete_json
from proofcast.llm.openai_compat import OpenAICompatProvider

KEY = "sk-test-SECRET-1234567890"


class Capture:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def body(self, i: int = 0) -> dict:  # type: ignore[type-arg]
        return json.loads(self.requests[i].content)


def anthropic_reply(text: str, stop: str = "end_turn", **extra: object) -> httpx.Response:
    payload = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": text}],
        "stop_reason": stop,
        "usage": {"input_tokens": 120, "output_tokens": 30},
        **extra,
    }
    return httpx.Response(200, json=payload)


def openai_reply(text: str, finish: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "m",
            "choices": [
                {"message": {"role": "assistant", "content": text}, "finish_reason": finish}
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        },
    )


def _anthropic(cap: Capture, model: str = "claude-opus-5-5") -> AnthropicProvider:
    return AnthropicProvider(model, KEY, transport=httpx.MockTransport(cap), sleep=lambda s: None)


# -- Anthropic ------------------------------------------------------------------------


def test_anthropic_request_shape() -> None:
    cap = Capture(anthropic_reply('{"ok": true}'))
    c = _anthropic(cap).complete("sys", "hello", max_tokens=100, temperature=0.0)
    req = cap.requests[0]
    assert req.url == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == KEY
    assert req.headers["anthropic-version"] == API_VERSION
    assert req.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    body = cap.body()
    assert body["system"] == "sys"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["max_tokens"] == MIN_OUTPUT_TOKENS
    assert body["fallbacks"] == "default"
    # API version 2026-10-21 rejects sampling parameters.
    assert "temperature" not in body and "top_p" not in body
    assert c.text == '{"ok": true}'  # thinking blocks ignored
    assert (c.input_tokens, c.output_tokens) == (120, 30)


def test_anthropic_no_fallback_for_other_models() -> None:
    cap = Capture(anthropic_reply("x"))
    _anthropic(cap, model="claude-haiku-5-5").complete("s", "p", max_tokens=10)
    assert "anthropic-beta" not in cap.requests[0].headers
    assert "fallbacks" not in cap.body()


def test_anthropic_refusal_and_truncation_are_errors() -> None:
    refusal = anthropic_reply(
        "", stop="refusal", stop_details={"type": "refusal", "category": "cyber"}
    )
    with pytest.raises(LLMError, match="declined"):
        _anthropic(Capture(refusal)).complete("s", "p", max_tokens=10)
    with pytest.raises(LLMError, match="cut off"):
        _anthropic(Capture(anthropic_reply("{", stop="max_tokens"))).complete(
            "s", "p", max_tokens=10
        )


def test_anthropic_error_redacts_key() -> None:
    body = {"type": "error", "error": {"type": "authentication_error", "message": f"bad key {KEY}"}}
    cap = Capture(httpx.Response(401, json=body))
    with pytest.raises(LLMError) as info:
        _anthropic(cap).complete("s", "p", max_tokens=10)
    assert KEY not in str(info.value)
    assert "authentication_error" in str(info.value)


def test_anthropic_retries_overload() -> None:
    cap = Capture(httpx.Response(529, json={}), anthropic_reply("ok"))
    assert _anthropic(cap).complete("s", "p", max_tokens=10).text == "ok"
    assert len(cap.requests) == 2


def test_anthropic_requires_key_and_https() -> None:
    with pytest.raises(LLMError):
        AnthropicProvider("m", "")
    with pytest.raises(LLMError, match="plain http"):
        AnthropicProvider("m", KEY, base_url="http://evil.example.com")


def test_complete_json_respects_min_output_tokens() -> None:
    from pydantic import BaseModel

    class R(BaseModel):
        ok: bool

    cap = Capture(anthropic_reply('{"ok": true}'))
    res = complete_json(_anthropic(cap), "s", "p", R, max_tokens=500)
    assert res.value == R(ok=True)
    assert cap.body()["max_tokens"] == MIN_OUTPUT_TOKENS


# -- OpenAI-compatible ---------------------------------------------------------------


def _compat(cap: Capture, **kw: object) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        kw.pop("name", "openai"),  # type: ignore[arg-type]
        "gpt-test",
        base_url=kw.pop("base_url", "https://api.openai.com/v1"),  # type: ignore[arg-type]
        transport=httpx.MockTransport(cap),
        sleep=lambda s: None,
        **kw,  # type: ignore[arg-type]
    )


def test_openai_request_shape() -> None:
    cap = Capture(openai_reply('{"a": 1}'))
    c = _compat(cap, api_key=KEY, json_mode=True).complete("sys", "hi", max_tokens=200)
    req = cap.requests[0]
    assert req.url == "https://api.openai.com/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    body = cap.body()
    assert body["temperature"] == 0.0
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["response_format"] == {"type": "json_object"}
    assert c.text == '{"a": 1}' and c.total_tokens == 60


def test_ollama_localhost_without_key() -> None:
    cap = Capture(openai_reply("ok"))
    p = _compat(cap, name="ollama", base_url="http://localhost:11434/v1")
    p.complete("s", "p", max_tokens=10)
    assert "authorization" not in cap.requests[0].headers


def test_compat_refuses_key_over_plain_http_remote() -> None:
    with pytest.raises(LLMError, match="plain http"):
        _compat(Capture(openai_reply("x")), base_url="http://10.0.0.5:8000/v1", api_key=KEY)
    # Without a key, a LAN server over http is allowed.
    _compat(Capture(openai_reply("x")), base_url="http://10.0.0.5:8000/v1")


def test_compat_length_and_bad_shape() -> None:
    with pytest.raises(LLMError, match="cut off"):
        _compat(Capture(openai_reply("x", finish="length"))).complete("s", "p", max_tokens=5)
    with pytest.raises(LLMError, match="unexpected"):
        _compat(Capture(httpx.Response(200, json={"choices": []}))).complete("s", "p", max_tokens=5)


def test_compat_error_redacted() -> None:
    cap = Capture(httpx.Response(400, json={"error": {"message": f"key {KEY} invalid"}}))
    with pytest.raises(LLMError) as info:
        _compat(cap, api_key=KEY).complete("s", "p", max_tokens=5)
    assert KEY not in str(info.value)


# -- factory -------------------------------------------------------------------------


def test_parse_model_spec() -> None:
    assert parse_model_spec("ollama:qwen2.5-coder:7b") == ("ollama", "qwen2.5-coder:7b")
    assert parse_model_spec("anthropic:claude-opus-5-5") == ("anthropic", "claude-opus-5-5")
    for bad in ["", "ollama", "ollama:", "gemini:x"]:
        with pytest.raises(ProviderConfigError):
            parse_model_spec(bad)


def test_nothing_set_means_ai_off() -> None:
    assert provider_from_env(environ={}) == (None, None)


def test_flag_overrides_env_and_settings() -> None:
    env = {
        "PROOFCAST_MODEL": "openai:gpt-x",
        "ANTHROPIC_API_KEY": KEY,
        "PROOFCAST_MAX_TOKENS": "5000",
    }
    s = resolve_settings(model="anthropic:claude-opus-5-5", environ=env)
    assert s is not None
    assert (s.provider, s.model, s.key_present, s.budget) == (
        "anthropic",
        "claude-opus-5-5",
        True,
        5000,
    )
    provider, budget = provider_from_env(model="anthropic:claude-opus-5-5", environ=env)
    assert isinstance(provider, AnthropicProvider) and budget is not None and budget.limit == 5000


def test_each_provider_uses_only_its_own_key() -> None:
    env = {"PROOFCAST_MODEL": "openai:gpt-x", "ANTHROPIC_API_KEY": KEY}
    with pytest.raises(ProviderConfigError, match="OPENAI_API_KEY"):
        provider_from_env(environ=env)


def test_timeouts_default_longer_for_localhost() -> None:
    local = resolve_settings(environ={"PROOFCAST_MODEL": "ollama:llama3"})
    remote = resolve_settings(
        environ={"PROOFCAST_MODEL": "openrouter:x", "OPENROUTER_API_KEY": "k"}
    )
    assert local is not None and remote is not None
    assert local.timeout > remote.timeout
    custom = resolve_settings(
        environ={"PROOFCAST_MODEL": "ollama:x", "PROOFCAST_LLM_TIMEOUT": "42"}
    )
    assert custom is not None and custom.timeout == 42
    with pytest.raises(ProviderConfigError):
        resolve_settings(environ={"PROOFCAST_MODEL": "ollama:x", "PROOFCAST_MAX_TOKENS": "lots"})


def test_compat_needs_base_url() -> None:
    with pytest.raises(ProviderConfigError, match="BASE_URL"):
        resolve_settings(environ={"PROOFCAST_MODEL": "compat:x"})
    s = resolve_settings(
        environ={
            "PROOFCAST_MODEL": "compat:x",
            "PROOFCAST_COMPAT_BASE_URL": "https://llm.example.org/v1",
        }
    )
    assert s is not None
    assert isinstance(build_provider(s, {}), OpenAICompatProvider)


# -- doctor ----------------------------------------------------------------------------


def test_doctor_never_prints_keys(tmp_path: Path) -> None:
    cfg = load_config(REPO_ROOT / "proofcast.toml")
    env = {
        "PROOFCAST_MODEL": "anthropic:claude-opus-5-5",
        "ANTHROPIC_API_KEY": KEY,
        "FRED_API_KEY": "fredsecret123",
        "PROOFCAST_SEC_USER_AGENT": "Proofcast t admin@example.org",
    }
    rows, ok = run_doctor(cfg, config_path=REPO_ROOT / "proofcast.toml", environ=env)
    text = "\n".join(" ".join(r) for r in rows)
    assert ok
    assert KEY not in text and "fredsecret123" not in text
    assert "ANTHROPIC_API_KEY set" in text


def test_doctor_reports_missing_key() -> None:
    cfg = load_config(REPO_ROOT / "proofcast.toml")
    rows, ok = run_doctor(cfg, config_path=None, environ={"PROOFCAST_MODEL": "openai:gpt-x"})
    assert not ok
    assert any("OPENAI_API_KEY NOT set" in r[2] for r in rows)


def test_doctor_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PROOFCAST_MODEL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    r = CliRunner().invoke(app, ["doctor", "--config", str(REPO_ROOT / "proofcast.toml")])
    assert r.exit_code == 0, r.output
    assert "AI: off" in r.output
    r = CliRunner().invoke(
        app, ["doctor", "--model", "nope:x", "--config", str(REPO_ROOT / "proofcast.toml")]
    )
    assert r.exit_code == 1
