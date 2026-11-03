"""Choose the BYOK provider from flags and environment variables.

``PROOFCAST_MODEL`` (or ``--model``) is ``<provider>:<model id>``:

==============  ===========================================  =================================
provider        key (only this one is ever sent)             base URL
==============  ===========================================  =================================
``anthropic``   ``ANTHROPIC_API_KEY``                        ``https://api.anthropic.com``
``openai``      ``OPENAI_API_KEY``                           ``https://api.openai.com/v1``
``openrouter``  ``OPENROUTER_API_KEY``                       ``https://openrouter.ai/api/v1``
``ollama``      none                                         ``OLLAMA_BASE_URL`` or
                                                             ``http://localhost:11434/v1``
``compat``      ``PROOFCAST_COMPAT_API_KEY`` (optional)      ``PROOFCAST_COMPAT_BASE_URL``
==============  ===========================================  =================================

Examples: ``ollama:qwen2.5-coder:7b`` (everything after the first colon is the model id),
``anthropic:claude-opus-5-5``, ``openai:<id>``, ``compat:<id>``.

Nothing set means AI is off. Other settings: ``PROOFCAST_MAX_TOKENS`` (token budget per run),
``PROOFCAST_LLM_TIMEOUT`` (seconds; default 120, or 600 for localhost models).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from proofcast.llm.anthropic import DEFAULT_BASE_URL as ANTHROPIC_URL
from proofcast.llm.anthropic import AnthropicProvider
from proofcast.llm.base import LLMError, Provider, TokenBudget
from proofcast.llm.openai_compat import OpenAICompatProvider
from proofcast.llm.transport import is_localhost

MODEL_ENV = "PROOFCAST_MODEL"
BUDGET_ENV = "PROOFCAST_MAX_TOKENS"
TIMEOUT_ENV = "PROOFCAST_LLM_TIMEOUT"
REMOTE_TIMEOUT = 120.0
ANTHROPIC_TIMEOUT = 300.0
LOCAL_TIMEOUT = 600.0

PROVIDERS = ("anthropic", "openai", "openrouter", "ollama", "compat")


class ProviderConfigError(ValueError):
    """The AI configuration is invalid. Messages never contain keys."""


@dataclass(frozen=True)
class ProviderSettings:
    """Resolved AI settings, safe to print (the key itself is not stored here)."""

    provider: str
    model: str
    base_url: str
    key_env: str | None
    key_present: bool
    timeout: float
    budget: int | None


_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "ollama": None,
    "compat": "PROOFCAST_COMPAT_API_KEY",
}


def parse_model_spec(spec: str) -> tuple[str, str]:
    provider, sep, model = spec.strip().partition(":")
    provider = provider.lower()
    if not sep or not model.strip():
        raise ProviderConfigError(
            f"{MODEL_ENV} must look like '<provider>:<model>', e.g. 'ollama:qwen2.5-coder:7b'"
        )
    if provider not in PROVIDERS:
        raise ProviderConfigError(
            f"unknown provider {provider!r}; use one of: {', '.join(PROVIDERS)}"
        )
    return provider, model.strip()


def _base_url(provider: str, env: dict[str, str]) -> str:
    if provider == "anthropic":
        return ANTHROPIC_URL
    if provider == "openai":
        return "https://api.openai.com/v1"
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1"
    if provider == "ollama":
        return env.get("OLLAMA_BASE_URL", "").strip() or "http://localhost:11434/v1"
    url = env.get("PROOFCAST_COMPAT_BASE_URL", "").strip()
    if not url:
        raise ProviderConfigError("compat provider needs PROOFCAST_COMPAT_BASE_URL")
    return url


def resolve_settings(
    *, model: str | None = None, environ: dict[str, str] | None = None
) -> ProviderSettings | None:
    """Resolve settings from ``--model`` (wins) and the environment. None means AI off."""
    env = environ if environ is not None else dict(os.environ)
    spec = (model or env.get(MODEL_ENV, "")).strip()
    if not spec:
        return None
    provider, model_id = parse_model_spec(spec)
    base_url = _base_url(provider, env)
    key_env = _KEY_ENV[provider]
    key_present = bool(key_env and env.get(key_env, "").strip())
    default_timeout = (
        LOCAL_TIMEOUT
        if is_localhost(base_url)
        else ANTHROPIC_TIMEOUT
        if provider == "anthropic"
        else REMOTE_TIMEOUT
    )
    try:
        timeout = float(env.get(TIMEOUT_ENV, "") or default_timeout)
        raw_budget = env.get(BUDGET_ENV, "").strip()
        budget = int(raw_budget) if raw_budget else None
    except ValueError as exc:
        raise ProviderConfigError(f"{TIMEOUT_ENV} and {BUDGET_ENV} must be numbers") from exc
    if timeout <= 0 or (budget is not None and budget <= 0):
        raise ProviderConfigError(f"{TIMEOUT_ENV} and {BUDGET_ENV} must be positive")
    return ProviderSettings(provider, model_id, base_url, key_env, key_present, timeout, budget)


def build_provider(settings: ProviderSettings, environ: dict[str, str] | None = None) -> Provider:
    env = environ if environ is not None else dict(os.environ)
    key = env.get(settings.key_env, "").strip() if settings.key_env else None
    try:
        if settings.provider == "anthropic":
            if not key:
                raise ProviderConfigError("ANTHROPIC_API_KEY is not set")
            return AnthropicProvider(
                settings.model, key, base_url=settings.base_url, timeout=settings.timeout
            )
        if settings.provider in {"openai", "openrouter"} and not key:
            raise ProviderConfigError(f"{settings.key_env} is not set")
        extra = (
            {"X-Title": "Proofcast", "HTTP-Referer": "https://github.com/7suheel/proofcast"}
            if settings.provider == "openrouter"
            else None
        )
        return OpenAICompatProvider(
            settings.provider,
            settings.model,
            base_url=settings.base_url,
            api_key=key or None,
            timeout=settings.timeout,
            json_mode=settings.provider in {"openai", "ollama"},
            extra_headers=extra,
        )
    except LLMError as exc:
        raise ProviderConfigError(str(exc)) from exc


def provider_from_env(
    *, model: str | None = None, environ: dict[str, str] | None = None
) -> tuple[Provider | None, TokenBudget | None]:
    settings = resolve_settings(model=model, environ=environ)
    if settings is None:
        return None, None
    return build_provider(settings, environ), TokenBudget(settings.budget)
