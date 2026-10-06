"""
ai_agent.py — shared model settings, token accounting and PDF export.

Every Claude call goes through the Grove gateway (`llm_gateway.async_client`, backed by
`_shared/grove_client`). The legacy synchronous chat/analysis helpers that built their
own `anthropic.Anthropic` client were removed in v3.2.0: the UI only uses the MCP
assistant runtime (`assistant_runtime.py`).
"""

import observability
from llm_gateway import resolve_model

# Sonnet 5 = best cost/speed for the demo; override via CLAUDE_MODEL. Haiku is mapped to
# claude-sonnet-5-5 because the gateway does not serve it.
MODEL = resolve_model()

def _track_usage(usage) -> None:
    """Surfaces Claude token spend (incl. cache hits) at /api/metrics."""
    if usage is None:
        return
    observability.metrics.bump("anthropic_input_tokens", usage.input_tokens)
    observability.metrics.bump("anthropic_output_tokens", usage.output_tokens)
    observability.metrics.bump("anthropic_cache_read_tokens", getattr(usage, "cache_read_input_tokens", 0) or 0)
    observability.metrics.bump("anthropic_cache_write_tokens", getattr(usage, "cache_creation_input_tokens", 0) or 0)



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
        for old, new in {"—": " - ", "–": "-", "“": '"', "”": '"', "‘": "'", "’": "'", "…": "...", "→": "->", "≈": "~", "≥": ">=", "≤": "<="}.items():
            cleaned = cleaned.replace(old, new)
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
        pdf.cell(28, 8, "Torre", ln=False)
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
            if raw_line.startswith("# "):
                pdf.ln(2); pdf.set_font("Helvetica", "B", 14)
                pdf.multi_cell(182, 7, line[2:])
                pdf.set_font("Helvetica", "", 10)
            elif raw_line.startswith("### "):
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
