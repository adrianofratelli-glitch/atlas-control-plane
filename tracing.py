"""Langfuse tracing for the Torre assistant's tool-use loop (assistant_runtime.py).

PII e segredos são mascarados (`mask_for_trace`) ANTES de qualquer dado chegar ao
Langfuse: a trace nasce com o texto já mascarado.

Fail-open by design: sem LANGFUSE_PUBLIC_KEY/SECRET_KEY configurados, todo
método vira no-op — a demo nunca quebra por causa de observability. O client
é lazy-instanciado uma vez por processo.
"""

import logging
import os
import re

logger = logging.getLogger("poc.tracing")

try:  # pov-shared (../_shared): CPF/CNPJ/e-mail/telefone/cartão com dígito verificador
    from guardrails import mask_pii as _shared_mask_pii
except Exception:  # noqa: BLE001 — sem pov-shared, usa a máscara local abaixo
    _shared_mask_pii = None

# Cartão: o mask_pii do _shared cobre desde a v0.2.x; este Luhn local fica como defesa em profundidade
# (e cobre o fallback sem pov-shared). Só mascara número que passa no Luhn,
# para não apagar timestamps e métricas numéricas dos spans.
_CARD = re.compile(r"(?<![\d.])\d(?:[ -]?\d){12,18}(?![\d.])")
# Fallback local só quando pov-shared não está instalado (sem dígito verificador).
_LOCAL_PII = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("CNPJ", re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")),
    ("CPF", re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")),
    ("TELEFONE", re.compile(r"(?:\+?55\s?)?\(?\b\d{2}\)?\s?9?\d{4}[-\s]?\d{4}\b")),
]
# Connection strings/credenciais nunca vão para o trace, mesmo que o usuário cole uma.
_SECRETS = re.compile(r"mongodb(?:\+srv)?://[^\s\"']+|\b(?:sk-[A-Za-z0-9_-]{16,}|pk-lf-[\w-]+|sk-lf-[\w-]+)")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total, alt = total + d, not alt
    return total % 10 == 0


def _mask_card(match: re.Match) -> str:
    digits = re.sub(r"\D", "", match.group())
    return "<CARTAO>" if 13 <= len(digits) <= 19 and _luhn(digits) else match.group()


def mask_text(text: str) -> str:
    """Remove PII e segredos de um texto antes de qualquer envio ao Langfuse."""
    if not isinstance(text, str) or not text:
        return text
    text = _SECRETS.sub("<SEGREDO>", text)
    text = _CARD.sub(_mask_card, text)
    masked = False
    if _shared_mask_pii is not None:
        try:
            text, masked = _shared_mask_pii(text).text, True
        except Exception:  # noqa: BLE001
            logger.warning("mask_pii do _shared falhou; aplicando máscara local")
    if not masked:
        for label, rx in _LOCAL_PII:
            text = rx.sub(f"<{label}>", text)
    return text


def mask_for_trace(value, _depth: int = 0):
    """Aplica `mask_text` a strings em qualquer estrutura JSON (dict/list) antes do trace."""
    if _depth > 20:
        return "<omitido>"
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {k: mask_for_trace(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_for_trace(v, _depth + 1) for v in value]
    return value

_client = None
_enabled = bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def _get_client():
    global _client
    if not _enabled:
        return None
    if _client is None:
        try:
            from langfuse import Langfuse
            candidate = Langfuse()
            # auth_check é síncrono e rápido (uma chamada), mas SÓ roda uma vez por
            # processo (client é cacheado). Sem isso, um Langfuse fora do ar não
            # impede a criação da trace (o SDK é fire-and-forget) — o turno segue
            # normal, mas a UI mostraria um link "Ver no Langfuse" que dá 404 no
            # meio de uma demo ao vivo. Aqui a feature inteira fica invisível em
            # vez de visível-e-quebrada.
            if not candidate.auth_check():
                raise RuntimeError("Langfuse auth_check falhou")
            _client = candidate
        except Exception:  # noqa: BLE001 — tracing nunca derruba o turno
            logger.warning("Langfuse indisponível/mal configurado; tracing desligado para este processo")
            _client = False
    return _client or None


def start_trace(*, name: str, user_id: str, session_id: str, input_text: str,
                metadata: dict | None = None):
    """Uma trace por turno do agente. Retorna None se Langfuse não configurado."""
    client = _get_client()
    if client is None:
        return None
    try:
        return client.trace(name=name, user_id=user_id, session_id=session_id,
                            input=mask_for_trace(input_text), metadata=mask_for_trace(metadata or {}))
    except Exception:  # noqa: BLE001
        logger.exception("falha ao criar trace Langfuse")
        return None


def log_generation(trace, *, name: str, model: str, input_text: str,
                   output_text: str, usage: dict, latency_ms: int):
    """Uma chamada ao LLM (reasoning step do loop) como generation."""
    if trace is None:
        return
    try:
        trace.generation(
            name=name, model=model,
            input=mask_for_trace(input_text), output=mask_for_trace(output_text),
            usage={
                "input": usage.get("input_tokens", 0),
                "output": usage.get("output_tokens", 0),
                "unit": "TOKENS",
            },
            metadata={
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                "latency_ms": latency_ms,
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception("falha ao logar generation no Langfuse")


def log_span(trace, *, name: str, input_data=None, output_data=None,
            metadata: dict | None = None):
    """Uma tool call (MongoDB MCP) ou passo não-LLM do turno como span."""
    if trace is None:
        return
    try:
        trace.span(name=name, input=mask_for_trace(input_data), output=mask_for_trace(output_data),
                  metadata=mask_for_trace(metadata or {}))
    except Exception:  # noqa: BLE001
        logger.exception("falha ao logar span no Langfuse")


def finish_trace(trace, *, output_text: str | None = None):
    if trace is None:
        return
    try:
        if output_text is not None:
            trace.update(output=mask_for_trace(output_text))
    except Exception:  # noqa: BLE001
        logger.exception("falha ao finalizar trace Langfuse")


def trace_url(trace) -> str | None:
    if trace is None:
        return None
    try:
        return trace.get_trace_url()
    except Exception:  # noqa: BLE001
        return None
