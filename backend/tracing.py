"""Langfuse tracing for the Torre assistant's tool-use loop (assistant_runtime.py).

PII e segredos são mascarados (`mask_for_trace`) ANTES de qualquer dado chegar ao
Langfuse: a trace nasce com o texto já mascarado.

Fail-open by design: sem LANGFUSE_PUBLIC_KEY/SECRET_KEY configurados, todo
método vira no-op — a demo nunca quebra por causa de observability. O client
é lazy-instanciado uma vez por processo. Com o Langfuse fora do ar: um aviso só
por queda (sem o "Unexpected error" do SDK a cada turno), turnos sem trace,
contadores `langfuse_*` em /api/metrics e nova tentativa silenciosa a cada
LANGFUSE_RETRY_SECONDS (padrão 300).
"""

import logging
import os
import re
import threading
import time

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

_client = None  # None = not built yet (or degraded, see _retry_at); False = disabled for good (tests)


def _configured() -> bool:
    """Read at call time: api.py runs load_dotenv() after some backend modules are imported."""
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))

# While Langfuse is unavailable, turns run without a trace and the probe is retried silently
# after this many seconds (the self-host may come back without restarting the PoV).
RETRY_SECONDS = float(os.getenv("LANGFUSE_RETRY_SECONDS", "300"))
PROBE_TIMEOUT_S = 2.0

_lock = threading.Lock()
_state = {"status": "pending", "reason": None, "warned": False}
_retry_at = 0.0
_connect_thread: threading.Thread | None = None  # the probe + auth_check never run inside a turn


def _host() -> str:
    return (os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com").rstrip("/")


def _bump(name: str) -> None:
    try:
        from backend import observability
        observability.metrics.bump(name)
    except Exception:  # noqa: BLE001 — metrics never break tracing
        pass


def _shutdown_quietly(client) -> None:
    """Stops a Langfuse client's consumer threads and HTTP pool off the caller's thread.

    Runs in a daemon thread: shutdown() flushes, and with the host down the flush can
    retry for a while; nobody waits for it (the caller may itself be an SDK thread).
    """
    if not client:
        return

    def run():
        try:
            client.shutdown()
        except Exception:  # noqa: BLE001
            logger.debug("shutdown do client Langfuse falhou", exc_info=True)

    threading.Thread(target=run, name="langfuse-shutdown", daemon=True).start()


def _degrade(status: str, reason: str) -> None:
    """Langfuse unavailable: ONE warning per outage, then silence (counters keep counting)."""
    global _client, _retry_at
    with _lock:
        old = _client if _client else None
        _client = None if _client is not False else False
        _retry_at = time.monotonic() + RETRY_SECONDS
        first = not _state["warned"]
        _state.update(status=status, reason=reason, warned=True)
    _shutdown_quietly(old)
    _bump("langfuse_unavailable")
    if first:
        # The host goes to the server log only; /api/metrics gets the hostless reason.
        logger.warning("Langfuse indisponível (%s; LANGFUSE_HOST=%s): turnos seguem sem trace; nova tentativa "
                       "silenciosa a cada %ds. Contadores em /api/metrics (langfuse_*).",
                       reason, _host(), int(RETRY_SECONDS))
    else:
        logger.debug("Langfuse segue indisponível (%s)", reason)


class _SdkLogSink(logging.Handler):
    """Swallows the Langfuse SDK's own log lines ("Unexpected error occurred...").

    Without this, a self-host that is down prints an SDK ERROR on every auth check and
    every background flush. Each swallowed WARNING+ line becomes a counter; while a client
    is live, the first one marks Langfuse degraded (one warning, via _degrade).
    """

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        _bump("langfuse_sdk_errors")
        if _client:  # a live client started failing (e.g. self-host went down mid-demo)
            _degrade("unreachable", "envio de eventos falhou")


def _install_sdk_log_sink() -> None:
    sdk = logging.getLogger("langfuse")
    if not any(isinstance(h, _SdkLogSink) for h in sdk.handlers):
        sdk.addHandler(_SdkLogSink())
        sdk.propagate = False


def _probe(host: str) -> tuple[str, str] | None:
    """Cheap health check before touching the SDK. Returns (status, hostless reason) on failure."""
    try:
        import httpx
        r = httpx.get(f"{host}/api/public/health", timeout=PROBE_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        return "unreachable", f"host não responde ({type(e).__name__})"
    if r.status_code != 200:
        return "unhealthy", f"health devolveu HTTP {r.status_code}"
    return None


def status() -> dict:
    """Tracing state for /api/metrics: off | pending | ok | unreachable | unhealthy | auth."""
    if not _configured() or _client is False:
        return {"status": "off", "reason": None}
    return {"status": _state["status"], "reason": _state["reason"]}


def _connect() -> None:
    """Probe + auth_check, in a background thread (blocking network: up to ~2 s + SDK timeout)."""
    global _client
    _install_sdk_log_sink()
    failure = _probe(_host())
    if failure:
        _degrade(*failure)
        return
    candidate = None
    try:
        from langfuse import Langfuse
        candidate = Langfuse()
        # auth_check runs once per (re)connection. Without it the SDK is fire-and-forget:
        # the UI would show a "Ver no Langfuse" link that 404s in the middle of a live demo.
        if not candidate.auth_check():
            raise RuntimeError("auth_check")
    except Exception:  # noqa: BLE001 — tracing never breaks the turn
        _shutdown_quietly(candidate)  # its consumer threads and HTTP pool would leak otherwise
        _degrade("auth", "credencial recusada ou SDK incompatível (auth_check falhou)")
        return
    with _lock:
        if _client is False:  # disabled meanwhile (tests)
            recovered = None
        else:
            recovered = _state["warned"]
            _client = candidate
            _state.update(status="ok", reason=None, warned=False)
    if recovered is None:
        _shutdown_quietly(candidate)
    elif recovered:
        logger.info("Langfuse disponível de novo; traces retomadas")


def warm_up() -> threading.Thread | None:
    """Starts the background connection if due. Never blocks; returns the thread (tests join it)."""
    global _connect_thread
    if not _configured() or _client is False or _client:
        return None
    with _lock:
        if (_connect_thread and _connect_thread.is_alive()) or time.monotonic() < _retry_at:
            return None
        _connect_thread = threading.Thread(target=_connect, name="langfuse-connect", daemon=True)
        _connect_thread.start()
        return _connect_thread


def _get_client():
    """Turn path: only READS the state (no network). While not connected, the turn runs
    untraced and a background connection is started if one is due."""
    if not _configured() or _client is False:
        return None
    if _client:
        return _client
    warm_up()
    _bump("langfuse_trace_skipped")
    return None


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
