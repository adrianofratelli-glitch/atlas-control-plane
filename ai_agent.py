"""
ai_agent.py — Claude-powered MongoDB analysis + conversational chat
"""

import json
import os
import anthropic
from functools import lru_cache
from typing import Iterator

import observability

# Sonnet 5 = best cost/speed for the demo; override via .env (e.g. claude-opus-4-8)
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")


@lru_cache(maxsize=1)
def _get_client() -> anthropic.Anthropic:
    """Share one HTTP connection pool across analysis and chat requests."""
    return anthropic.Anthropic(
        api_key="dummy",
        base_url=os.getenv("ANTHROPIC_BASE_URL"),
        default_headers={"api-key": os.getenv("ANTHROPIC_API_KEY", "")},
    )


def _track_usage(usage) -> None:
    """Surfaces Claude token spend (incl. cache hits) at /api/metrics."""
    if usage is None:
        return
    observability.metrics.bump("anthropic_input_tokens", usage.input_tokens)
    observability.metrics.bump("anthropic_output_tokens", usage.output_tokens)
    observability.metrics.bump("anthropic_cache_read_tokens", getattr(usage, "cache_read_input_tokens", 0) or 0)
    observability.metrics.bump("anthropic_cache_write_tokens", getattr(usage, "cache_creation_input_tokens", 0) or 0)


# ══════════════════════════════════════════════════════════════════════════════
# ONE-SHOT ANALYSIS (original, kept)
# ══════════════════════════════════════════════════════════════════════════════

def _build_analysis_prompt(cluster: dict, pa_data: dict, slow_queries: dict,
                           measurements: dict = None, cpu24: dict = None) -> str:
    tier = region = "N/A"
    try:
        rc     = cluster["replicationSpecs"][0]["regionConfigs"][0]
        tier   = rc["electableSpecs"]["instanceSize"]
        region = rc["regionName"]
    except (KeyError, IndexError, TypeError):
        pass

    suggestions = pa_data.get("suggestedIndexes", [])
    slow_qs     = slow_queries.get("slowQueries", [])

    hw = ""
    if measurements and "error" not in measurements:
        hw = (
            "\n## Métricas de Hardware (últimos 5 min)\n"
            f"- CPU: **{measurements.get('cpu_pct', 0)}%** | "
            f"Memória: **{measurements.get('mem_pct', 0)}%** "
            f"({measurements.get('memory_used_gb', 0)}/{measurements.get('mem_total_gb', 0)} GB)\n"
            f"- Conexões: **{measurements.get('connections', 0)}** | "
            f"Disk IOPS R/W: **{measurements.get('disk_iops_read', 0)}/{measurements.get('disk_iops_write', 0)}** | "
            f"Storage: **{measurements.get('disk_pct', 0)}%**\n"
            f"- Ops/s — query: {measurements.get('ops_query', 0)}, insert: {measurements.get('ops_insert', 0)}, "
            f"update: {measurements.get('ops_update', 0)} | "
            f"Query targeting (scanned/returned): **{measurements.get('query_targeting', 0)}**\n"
        )
    if cpu24:
        hw += f"- CPU 24h — média: **{cpu24.get('avg')}%** | p95: **{cpu24.get('p95')}%**\n"

    return f"""Você é um DBA especialista em MongoDB com foco em performance para aplicações financeiras de alto volume no Brasil.

## Cluster analisado
- **Nome:** {cluster.get('name', 'N/A')}
- **Tier:** {tier}
- **Região:** {region}
- **Status:** {cluster.get('stateName', 'N/A')}
- **Versão:** {cluster.get('mongoDBVersion', 'N/A')}
{hw}
## Performance Advisor — {len(suggestions)} índice(s) sugerido(s)
```json
{json.dumps(suggestions[:5], indent=2)[:4000]}
```

## Slow Queries — {len(slow_qs)} registros
```json
{json.dumps(slow_qs[:10], indent=2)[:3000]}
```

---
Análise **técnica e direta**. Estruture assim:

### 🔴 Problemas Críticos
Os 3 de maior impacto, com latência estimada e operações afetadas.

### 🟡 Ações Recomendadas
Para cada problema: ação específica + comando MongoDB quando aplicável + impacto esperado.

### 🟢 Quick Wins
Ações executáveis em menos de 1 hora, sem risco de downtime.

### 📊 Veredicto de Scaling
O tier **{tier}** é adequado? Se não, qual tier recomendar e por quê (critério técnico).

Máximo 600 palavras. Foque em impacto de negócio."""


def analyze_cluster_stream(cluster: dict, pa_data: dict, slow_queries: dict,
                           measurements: dict = None, cpu24: dict = None) -> Iterator[str]:
    """Streams a one-shot performance analysis."""
    client = _get_client()
    with client.messages.stream(
        model=MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": _build_analysis_prompt(cluster, pa_data, slow_queries, measurements, cpu24)}],
    ) as stream:
        for text in stream.text_stream:
            yield text
        _track_usage(getattr(stream.get_final_message(), "usage", None))


# ══════════════════════════════════════════════════════════════════════════════
# CONVERSATIONAL CHAT
# ══════════════════════════════════════════════════════════════════════════════

# Static across every call in every session — the biggest win from prompt
# caching is marking exactly this block as `cache_control: ephemeral` so repeat
# chat turns (same session) and repeat sessions within the 5-min TTL don't
# re-bill the ~350-token instruction block as fresh input tokens every time.
_BASE_SYSTEM_PROMPT = (
    "Você é um especialista em **MongoDB Atlas** (banco de dados NoSQL) e Solutions "
    "Architect, focado em aplicações financeiras de alto volume no Brasil.\n"
    "Responda de forma técnica e objetiva. Use markdown para formatar.\n"
    "Quando relevante, forneça comandos do **MongoDB shell (mongosh)** prontos para execução.\n"
    "Foque em impacto de negócio ao explicar problemas técnicos.\n\n"
    "ESCOPO — LEIA COM ATENÇÃO:\n"
    "- O domínio é EXCLUSIVAMENTE MongoDB Atlas: clusters são **tiers do Atlas** "
    "(M10, M20, M30, M40, M80…), NÃO clusters de Kubernetes.\n"
    "- 'Scale up' significa subir o **tier do cluster Atlas** (ex: M20 → M40) ou fazer sharding — "
    "NUNCA é sobre adicionar nodes em Kubernetes/EKS/GKE.\n"
    "- NUNCA mencione Kubernetes, kubectl, pods, OOMKilled, EKS, GKE, AKS, Prometheus, "
    "Grafana ou autoscaler de nodes. Se a pergunta parecer ambígua, assuma SEMPRE MongoDB Atlas.\n"
    "- Indicadores de scale up no Atlas: **CPU normalizada alta** (>75%), **conexões próximas "
    "do limite do tier**, **IOPS de disco saturando**, **WiredTiger cache pressure / page faults**, "
    "**replication lag**, **query targeting alto** (scanned/returned), **latência p95/p99 subindo**.\n\n"
    "REGRAS CRÍTICAS:\n"
    "- Use APENAS os dados reais fornecidos neste contexto. NUNCA invente métricas, índices ou queries.\n"
    "- Se um dado não estiver neste contexto, diga explicitamente que ele não está disponível "
    "e o que seria necessário para obtê-lo — NUNCA preencha a lacuna com um valor estimado.\n"
    "- Se a pergunta estiver fora de MongoDB Atlas, não responda ao mérito nem diga apenas "
    "'não sei': reconheça o limite em uma frase e ofereça análise de cluster, métricas, "
    "Performance Advisor, Query Profiler, custo, health score ou sizing.\n"
    "- Se suggestedIndexes tiver 0 itens: informe que o PA não encontrou oportunidades e analise "
    "as slow queries para propor índices baseados nos padrões de acesso REAIS observados.\n"
    "- Distingua sempre: '**dados reais da API**' vs '**recomendação baseada em padrões**'.\n"
)


def build_chat_system_prompt(
    cluster_data: dict = None,
    pa_data: dict = None,
    slow_queries: dict = None,
    measurements: dict = None,
) -> str:
    base = _BASE_SYSTEM_PROMPT

    if not cluster_data:
        return base + "\nNenhum cluster selecionado. Oriente a selecionar um cluster no topo do Assistente para consultar dados reais."

    tier = region = "N/A"
    try:
        rc     = cluster_data["replicationSpecs"][0]["regionConfigs"][0]
        tier   = rc["electableSpecs"]["instanceSize"]
        region = rc["regionName"]
    except (KeyError, IndexError, TypeError):
        pass

    ctx = (
        f"\n\n## Contexto: Cluster `{cluster_data.get('name', 'N/A')}`\n"
        f"- Tier: **{tier}** | Região: {region}\n"
        f"- Status: {cluster_data.get('stateName', 'N/A')} | MongoDB: {cluster_data.get('mongoDBVersion', 'N/A')}\n"
    )

    # ── Hardware metrics (last 5 min) ──
    if measurements and "error" not in measurements:
        cpu   = measurements.get("cpu_pct", 0)
        m_use = measurements.get("memory_used_gb", 0)
        m_av  = measurements.get("memory_avail_gb", 0)
        conns = measurements.get("connections", 0)
        iops_r = measurements.get("disk_iops_read", 0)
        iops_w = measurements.get("disk_iops_write", 0)
        ops_q  = measurements.get("ops_query", 0)
        ops_i  = measurements.get("ops_insert", 0)
        ops_u  = measurements.get("ops_update", 0)
        net_in  = measurements.get("net_in_mb", 0)
        net_out = measurements.get("net_out_mb", 0)

        ctx += (
            f"\n### Métricas de Hardware (últimos 5 min)\n"
            f"| Métrica | Valor |\n|---|---|\n"
            f"| CPU (user+kernel) | **{cpu}%** |\n"
            f"| Memória usada | **{m_use} GB** |\n"
            f"| Memória disponível | **{m_av} GB** |\n"
            f"| Conexões ativas | **{conns}** |\n"
            f"| Disk IOPS Read | **{iops_r}** |\n"
            f"| Disk IOPS Write | **{iops_w}** |\n"
            f"| Queries/s | **{ops_q}** |\n"
            f"| Inserts/s | **{ops_i}** |\n"
            f"| Updates/s | **{ops_u}** |\n"
            f"| Network In | **{net_in} MB/s** |\n"
            f"| Network Out | **{net_out} MB/s** |\n"
        )
    else:
        ctx += "- Métricas de hardware: não disponíveis (verifique permissões da API Key)\n"

    if pa_data and "error" not in pa_data and "suggestedIndexes" in pa_data:
        suggestions = pa_data.get("suggestedIndexes", [])
        ctx += f"\n### Performance Advisor\n- **{len(suggestions)}** índice(s) sugerido(s)\n"
        if suggestions:
            ctx += f"```json\n{json.dumps(suggestions, indent=2)}\n```\n"

    else:
        ctx += "\n### Performance Advisor\nConsulta indisponível; não equivale a zero recomendações.\n"

    if slow_queries and "error" not in slow_queries:
        slow_qs = slow_queries.get("slowQueries", [])
        ctx += f"\n### Slow Queries\n- **{len(slow_qs)}** registrada(s)\n"
        if slow_qs:
            ctx += f"```json\n{json.dumps(slow_qs[:5], indent=2)[:1500]}\n```\n"

    return base + ctx


# Chat history grows unbounded on the client — cap what we actually send so a
# long-running session doesn't quietly bill more input tokens every turn.
MAX_HISTORY_MESSAGES = 16


def stream_chat(messages: list, system_prompt: str = "") -> Iterator[str]:
    """Streams a conversational Claude response, windowed history + cached system prompt."""
    client = _get_client()
    if len(messages) > MAX_HISTORY_MESSAGES:
        messages = messages[-MAX_HISTORY_MESSAGES:]
        # The window must start on a user turn — a leading assistant message
        # after slicing is rejected/mishandled by the API.
        while messages and messages[0].get("role") != "user":
            messages = messages[1:]
    kwargs = dict(model=MODEL, max_tokens=2000, messages=messages)
    if system_prompt:
        # Split the static instruction block (cacheable across turns/sessions)
        # from the per-cluster dynamic context that follows it.
        if system_prompt.startswith(_BASE_SYSTEM_PROMPT):
            dynamic = system_prompt[len(_BASE_SYSTEM_PROMPT):]
            blocks = [{"type": "text", "text": _BASE_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
            if dynamic:
                # The cluster context is now stable for ~2-3 min (api.py caches
                # the Atlas snapshot), so caching it pays off across chat turns.
                blocks.append({"type": "text", "text": dynamic,
                               "cache_control": {"type": "ephemeral"}})
            kwargs["system"] = blocks
        else:
            kwargs["system"] = system_prompt
    with client.messages.stream(**kwargs) as stream:
        for text in stream.text_stream:
            yield text
        _track_usage(getattr(stream.get_final_message(), "usage", None))


# ══════════════════════════════════════════════════════════════════════════════
# PDF EXPORT
# ══════════════════════════════════════════════════════════════════════════════

def _markdown_report(cluster_name, analysis_text, health_score, health_issues) -> bytes:
    """Fallback: plain Markdown report."""
    from datetime import datetime
    lines = [
        "# Torre — Atlas Report",
        f"**Cluster:** `{cluster_name}`  ",
        f"**Data:** {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        "",
    ]
    if health_score is not None:
        lines += [f"## Health Score: {health_score}/100", ""]
        if health_issues:
            for issue in health_issues:
                lines.append(f"- {issue}")
            lines.append("")
    lines += ["---", "", analysis_text]
    return "\n".join(lines).encode("utf-8")


def generate_pdf_report(
    cluster_name: str,
    analysis_text: str,
    health_score: int = None,
    health_issues: list = None,
) -> tuple:
    """
    Generates a real PDF report (via fpdf2) with MongoDB branding.
    Returns (bytes, mime, extension). Falls back to Markdown if fpdf2 fails.
    """
    from datetime import datetime
    try:
        from fpdf import FPDF
    except ImportError:
        return _markdown_report(cluster_name, analysis_text, health_score, health_issues), "text/markdown", "md"

    # Sanitize for fpdf2 core fonts: strip emoji/astral codepoints entirely
    # (instead of littering the PDF with "?"), then Latin-1 for the rest.
    import re as _re
    _EMOJI_RE = _re.compile(
        "["
        "\U00010000-\U0010FFFF"   # astral plane (most emojis)
        "←-⇿"           # arrows (⬆️/⬇️ base chars live nearby)
        "⌀-➿"           # misc technical / symbols / dingbats (✅⏳❌…)
        "⬀-⯿"           # misc symbols and arrows (⬆⬇)
        "︎️"            # variation selectors
        "‍"                  # zero-width joiner
        "]+"
    )

    def _safe(s: str) -> str:
        cleaned = _EMOJI_RE.sub("", s or "")
        return cleaned.encode("latin-1", "replace").decode("latin-1")

    try:
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=18)
        pdf.add_page()

        # ── MongoDB banner header ──
        pdf.set_fill_color(0, 30, 43)        # #001E2B
        pdf.rect(0, 0, 210, 28, "F")
        pdf.set_fill_color(0, 237, 100)      # #00ED64
        pdf.rect(0, 28, 210, 1.2, "F")
        pdf.set_xy(14, 9)
        pdf.set_text_color(0, 237, 100)
        pdf.set_font("Helvetica", "B", 18)
        pdf.cell(0, 8, "Torre", ln=False)
        pdf.set_text_color(227, 252, 247)
        pdf.set_font("Helvetica", "", 18)
        pdf.cell(0, 8, "  Atlas Control Plane", ln=True)

        # ── Metadata ──
        pdf.set_xy(14, 38)
        pdf.set_text_color(90, 110, 120)
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 6, _safe(f"Cluster: {cluster_name}"), ln=True)
        pdf.set_x(14)
        pdf.cell(0, 6, _safe(f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M')}"), ln=True)
        pdf.ln(4)

        # ── Health Score box (if present) ──
        if health_score is not None:
            color = (0, 237, 100) if health_score >= 75 else (250, 204, 21) if health_score >= 50 else (248, 113, 113)
            pdf.set_x(14)
            pdf.set_fill_color(*color)
            pdf.set_text_color(0, 30, 43)
            pdf.set_font("Helvetica", "B", 13)
            pdf.cell(60, 10, _safe(f"  Health Score: {health_score}/100"), ln=True, fill=True)
            pdf.ln(2)
            if health_issues:
                pdf.set_text_color(80, 80, 80)
                pdf.set_font("Helvetica", "", 9)
                for issue in health_issues:
                    clean = _safe(issue.replace("**", ""))
                    pdf.set_x(14)
                    pdf.multi_cell(180, 5, f"- {clean}")
                pdf.ln(2)

        # ── Analysis body ──
        pdf.set_text_color(40, 40, 40)
        pdf.set_font("Helvetica", "", 10)
        for raw_line in analysis_text.split("\n"):
            line = _safe(raw_line.replace("**", "").replace("`", ""))
            pdf.set_x(14)
            if raw_line.startswith("### "):
                pdf.ln(1); pdf.set_font("Helvetica", "B", 11)
                pdf.multi_cell(182, 6, line.replace("### ", ""))
                pdf.set_font("Helvetica", "", 10)
            elif raw_line.startswith("## "):
                pdf.ln(2); pdf.set_font("Helvetica", "B", 13)
                pdf.set_text_color(0, 120, 70)
                pdf.multi_cell(182, 7, line.replace("## ", ""))
                pdf.set_text_color(40, 40, 40); pdf.set_font("Helvetica", "", 10)
            elif line.strip():
                pdf.multi_cell(182, 5, line)
            else:
                pdf.ln(2)

        out = pdf.output(dest="S")
        pdf_bytes = bytes(out) if isinstance(out, (bytes, bytearray)) else out.encode("latin-1")
        return pdf_bytes, "application/pdf", "pdf"
    except Exception:
        return _markdown_report(cluster_name, analysis_text, health_score, health_issues), "text/markdown", "md"


def friendly_api_error(err: Exception) -> str:
    """Converts Anthropic API exceptions into friendly messages for the demo."""
    if isinstance(err, anthropic.AuthenticationError):
        return ("🔑 **API Key da Anthropic inválida ou expirada.** "
                "Verifique a ANTHROPIC_API_KEY no `.env` do servidor (deve começar com `sk-ant-`).")
    if isinstance(err, anthropic.PermissionDeniedError):
        msg = str(err).lower()
        if "credit" in msg or "billing" in msg:
            return ("💳 **Créditos da conta Anthropic esgotados.** "
                    "Verifique o billing em console.anthropic.com.")
        return ("🚫 **API Key sem permissão para este recurso.** "
                "Verifique o workspace e os limites em console.anthropic.com.")
    if isinstance(err, anthropic.RateLimitError):
        return ("⏳ **Limite de requisições atingido.** "
                "Aguarde alguns segundos e tente novamente.")
    if isinstance(err, anthropic.NotFoundError):
        return (f"❓ **Modelo `{MODEL}` não encontrado.** "
                "Verifique o CLAUDE_MODEL no `.env`.")
    if isinstance(err, anthropic.APIStatusError) and err.status_code >= 500:
        return ("⏳ **API da Anthropic temporariamente sobrecarregada.** "
                "Tente novamente em alguns instantes.")
    if isinstance(err, anthropic.APIConnectionError):
        return ("🌐 **Falha de conexão com a Anthropic.** "
                "Verifique sua internet e tente novamente.")
    return "❌ **Erro inesperado ao chamar Claude.** Consulte os logs pelo request-id e tente novamente."
