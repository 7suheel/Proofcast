"""``proofcast doctor``: check the setup without printing secrets.

Only the *presence* of keys is reported, never their values or lengths.
"""

from __future__ import annotations

import os
import platform
import re
from pathlib import Path

from proofcast import __version__
from proofcast.config import Config, find_config
from proofcast.llm.base import LLMError
from proofcast.llm.factory import ProviderConfigError, build_provider, resolve_settings
from proofcast.llm.jsonout import extract_json_object
from proofcast.storage import DataStore

OK, WARN, FAIL = "ok", "warn", "FAIL"
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


def _present(env: dict[str, str], name: str) -> bool:
    return bool(env.get(name, "").strip())


def run_doctor(
    cfg: Config,
    *,
    config_path: Path | None,
    environ: dict[str, str] | None = None,
    model: str | None = None,
    live: bool = False,
) -> tuple[list[tuple[str, str, str]], bool]:
    """Return (rows of (status, check, detail), all_ok)."""
    env = environ if environ is not None else dict(os.environ)
    rows: list[tuple[str, str, str]] = []
    rows.append((OK, "proofcast", f"{__version__} on Python {platform.python_version()}"))
    found = find_config(config_path)
    rows.append(
        (OK, "config", str(found))
        if found
        else (WARN, "config", "no proofcast.toml found; using defaults")
    )
    rows.append((OK, "universe", f"{len(cfg.assets)} assets, {len(cfg.macro)} macro series"))

    store = DataStore(cfg.data_dir)
    with_prices = [a.symbol for a in cfg.assets if store.prices_path(a.symbol).exists()]
    status = OK if with_prices else WARN
    rows.append((status, "price data", f"{len(with_prices)}/{len(cfg.assets)} assets have bars"))

    ua = env.get("PROOFCAST_SEC_USER_AGENT", "")
    if not ua.strip():
        rows.append((WARN, "SEC filings", "PROOFCAST_SEC_USER_AGENT not set: filings skipped"))
    elif not _EMAIL.search(ua):
        rows.append((FAIL, "SEC filings", "PROOFCAST_SEC_USER_AGENT has no contact email"))
    else:
        rows.append((OK, "SEC filings", "PROOFCAST_SEC_USER_AGENT set"))
    rows.append(
        (OK, "FRED", "FRED_API_KEY set (real release dates)")
        if _present(env, "FRED_API_KEY")
        else (WARN, "FRED", "FRED_API_KEY not set: CSV download, estimated release times")
    )
    rows.append(
        (OK, "CoinGecko", "demo key set" if _present(env, "COINGECKO_DEMO_API_KEY") else "public")
    )
    stock = cfg.sources.stocks
    rows.append(
        (
            OK,
            "stock prices",
            f"providers {stock.providers}, unofficial sources "
            f"{'allowed' if stock.allow_unofficial else 'not allowed'}",
        )
    )

    try:
        settings = resolve_settings(model=model, environ=env)
    except ProviderConfigError as exc:
        rows.append((FAIL, "AI", str(exc)))
        return rows, False
    if settings is None:
        rows.append((OK, "AI", "off (no PROOFCAST_MODEL): baselines only"))
    else:
        key = (
            f"{settings.key_env} {'set' if settings.key_present else 'NOT set'}"
            if settings.key_env
            else "no key needed"
        )
        ok = settings.key_present or settings.key_env in {None, "PROOFCAST_COMPAT_API_KEY"}
        budget = settings.budget if settings.budget is not None else "unlimited"
        rows.append(
            (
                OK if ok else FAIL,
                "AI",
                f"{settings.provider}:{settings.model} at {settings.base_url}; {key}; "
                f"timeout {settings.timeout:g}s; token budget {budget}",
            )
        )
        if live and ok:
            rows.append(_live_check(settings, env))
    return rows, all(r[0] != FAIL for r in rows)


def _live_check(settings: object, env: dict[str, str]) -> tuple[str, str, str]:
    from proofcast.llm.factory import ProviderSettings

    assert isinstance(settings, ProviderSettings)
    try:
        provider = build_provider(settings, env)
        reply = provider.complete(
            'Reply with only this JSON object: {"ok": true}', "Health check.", max_tokens=50
        )
        data = extract_json_object(reply.text)
    except (LLMError, ProviderConfigError, ValueError) as exc:
        return (FAIL, "AI live call", str(exc))
    if isinstance(data, dict) and data.get("ok") is True:
        return (OK, "AI live call", f"valid JSON reply, {reply.total_tokens} tokens")
    return (WARN, "AI live call", "model replied but not with the expected JSON")
