"""Choose the BYOK provider from flags and environment variables.

Nothing configured means AI is off: every command still works with rules and baselines.
Real providers are added in the BYOK LLM layer; until then any ``PROOFCAST_MODEL`` is
rejected with a clear message.
"""

from __future__ import annotations

import os

from proofcast.llm.base import Provider, TokenBudget

MODEL_ENV = "PROOFCAST_MODEL"


class ProviderConfigError(ValueError):
    """The AI configuration is invalid. Messages never contain keys."""


def provider_from_env(
    *, model: str | None = None, environ: dict[str, str] | None = None
) -> tuple[Provider | None, TokenBudget | None]:
    env = environ if environ is not None else dict(os.environ)
    spec = (model or env.get(MODEL_ENV, "")).strip()
    if not spec:
        return None, None
    raise ProviderConfigError(f"unsupported model spec {spec.split(':', 1)[0]!r}")
