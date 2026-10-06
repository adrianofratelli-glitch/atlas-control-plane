"""Single entry point for every LLM call in Torre: the Grove gateway via pov-shared.

Every model call goes through `_shared/grove_client` (pov-shared >= 0.1.5), which adds
retry/backoff on 429/5xx/timeout, a per-model circuit breaker, key failover and
locked destination checks by default. Torre never builds an `anthropic` client itself.

Fail closed: without `GROVE_BASE_URL` and `GROVE_API_KEY` the assistant refuses to call
any model. There is deliberately no fallback to `ANTHROPIC_API_KEY` or to the provider's
public endpoint.

Install: `uv pip install --python venv/bin/python -e "../_shared[llm]"`.
"""
from __future__ import annotations

import os

# The gateway does not serve haiku; any haiku request is mapped to the served sonnet.
DEFAULT_MODEL = "claude-sonnet-5"
HAIKU_REPLACEMENT = "claude-sonnet-5-5"


class GatewayNotConfigured(RuntimeError):
    """Raised before any network call when the Grove gateway is not configured."""

    public_message = ("Modelo indisponível: o gateway LLM (Grove) não está configurado no servidor. "
                      "Defina GROVE_BASE_URL e GROVE_API_KEY no .env e reinicie o backend.")


def gateway_configured() -> bool:
    return bool(os.getenv("GROVE_BASE_URL", "").strip() and os.getenv("GROVE_API_KEY", "").strip())


def require_gateway() -> None:
    if not gateway_configured():
        raise GatewayNotConfigured(GatewayNotConfigured.public_message)


def resolve_model(requested: str | None = None) -> str:
    model = (requested or os.getenv("CLAUDE_MODEL") or DEFAULT_MODEL).strip()
    return HAIKU_REPLACEMENT if "haiku" in model.lower() else model


def async_client():
    """Drop-in `AsyncAnthropic` replacement (messages.create / messages.stream) with resilience."""
    require_gateway()
    from grove_client import AsyncGroveClient  # imported lazily: keeps pure-logic tests SDK-free
    return AsyncGroveClient()
