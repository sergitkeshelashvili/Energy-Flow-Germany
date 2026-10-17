"""SCADA control-room styling and status badge helpers for the Streamlit dashboard."""

import html
from typing import Literal

from dashboard.formatters import FreshnessStatus

SCADA_CSS = """
<style>
    :root {
        --streamlit-red: #FF4B4B;
        --streamlit-red-soft: rgba(255, 75, 75, 0.14);
        --surface: #151922;
        --surface-raised: #1B202B;
        --surface-border: rgba(198, 207, 222, 0.12);
        --text-primary: #F5F6FA;
        --text-muted: #A6AEBB;
    }
    .stApp {
        background:
            radial-gradient(ellipse at 9% 0%, rgba(255, 75, 75, 0.17), transparent 34%),
            radial-gradient(ellipse at 95% 13%, rgba(54, 207, 201, 0.08), transparent 27%),
            #0E1117;
        color: var(--text-primary);
    }
    [data-testid="stHeader"] {
        background: rgba(14, 17, 23, 0.72);
    }
    [data-testid="stAppViewContainer"] > .main .block-container {
        max-width: 1540px;
        padding-top: 1.55rem;
        padding-bottom: 2.5rem;
    }
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, rgba(255, 75, 75, 0.075), transparent 34%), #11151D;
        border-right: 1px solid var(--surface-border);
    }
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h3 {
        color: var(--streamlit-red);
        letter-spacing: 0.07em;
    }
    h1, h2, h3 {
        color: var(--text-primary);
        letter-spacing: -0.025em;
    }
    h2 { margin-top: 0.4rem; }
    [data-testid="stCaptionContainer"] { color: var(--text-muted); }
    [data-testid="stTabs"] [data-baseweb="tab-list"] {
        gap: 0.35rem;
        border-bottom: 1px solid var(--surface-border);
    }
    [data-testid="stTabs"] button[role="tab"] {
        color: var(--text-muted);
        border-radius: 10px 10px 0 0;
        padding: 0.7rem 1rem;
    }
    [data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
        color: var(--text-primary);
        border-bottom: 3px solid var(--streamlit-red);
        background: linear-gradient(180deg, var(--streamlit-red-soft), transparent);
    }
    [data-testid="stMetric"] {
        background: linear-gradient(145deg, rgba(255,255,255,0.045), rgba(255,255,255,0.015));
        border: 1px solid var(--surface-border);
        border-radius: 14px;
        padding: 0.85rem 1rem;
        min-height: 100%;
    }
    [data-testid="stMetricLabel"] p { color: var(--text-muted); font-size: 0.82rem; }
    [data-testid="stMetricValue"] { color: var(--text-primary); font-variant-numeric: tabular-nums; }
    [data-testid="stPlotlyChart"] {
        border: 1px solid var(--surface-border);
        border-radius: 16px;
        background: linear-gradient(145deg, rgba(255,255,255,0.025), rgba(255,255,255,0.005));
        padding: 0.35rem 0.35rem 0.1rem;
        overflow: hidden;
    }
    [data-testid="stDataFrame"] { border: 1px solid var(--surface-border); border-radius: 12px; overflow: hidden; }
    .stButton > button {
        border-radius: 10px;
        border: 1px solid rgba(255, 75, 75, 0.42);
        transition: transform 140ms ease, box-shadow 140ms ease, border-color 140ms ease;
    }
    .stButton > button:hover {
        border-color: var(--streamlit-red);
        box-shadow: 0 0 22px rgba(255, 75, 75, 0.18);
        transform: translateY(-1px);
    }
    [data-baseweb="select"] > div, [data-baseweb="input"] > div {
        background-color: var(--surface-raised);
        border-color: var(--surface-border);
        border-radius: 10px;
    }
    hr { border-color: var(--surface-border); }
    .scada-header {
        overflow: hidden;
        background: linear-gradient(110deg, rgba(255, 75, 75, 0.14), rgba(21, 25, 34, 0.96) 42%, rgba(27, 32, 43, 0.96));
        border: 1px solid rgba(255, 75, 75, 0.24);
        border-top: 3px solid var(--streamlit-red);
        border-radius: 18px;
        padding: 20px 24px;
        margin-bottom: 22px;
        box-shadow: 0 18px 52px rgba(0, 0, 0, 0.22);
    }
    .scada-title {
        font-size: 1.22rem;
        font-weight: 700;
        letter-spacing: 0.055em;
        color: var(--text-primary);
        margin: 0;
    }
    .scada-subtitle {
        font-size: 0.86rem;
        color: var(--text-muted);
        margin-top: 8px;
        font-family: Inter, ui-sans-serif, system-ui, sans-serif;
    }
    .scada-panel {
        background: linear-gradient(145deg, rgba(255, 255, 255, 0.05), rgba(255, 255, 255, 0.018)), var(--surface);
        border: 1px solid var(--surface-border);
        border-radius: 16px;
        padding: 18px;
        margin-bottom: 16px;
        box-shadow: 0 12px 30px rgba(0, 0, 0, 0.13);
    }
    .scada-panel-label {
        font-size: 0.72rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.105em;
        color: var(--text-muted);
        margin-bottom: 8px;
    }
    .scada-telemetry-value {
        font-family: Inter, ui-sans-serif, system-ui, sans-serif;
        font-variant-numeric: tabular-nums;
        font-size: clamp(1.7rem, 2vw, 2.3rem);
        font-weight: 700;
        color: var(--text-primary);
        line-height: 1.2;
        margin-bottom: 8px;
    }
    .scada-meta-row {
        font-family: Inter, ui-sans-serif, system-ui, sans-serif;
        font-size: 0.79rem;
        color: var(--text-muted);
        margin-top: 4px;
    }
    .scada-badge {
        display: inline-block;
        padding: 4px 10px;
        border-radius: 999px;
        font-family: Inter, ui-sans-serif, system-ui, sans-serif;
        font-size: 0.72rem;
        font-weight: 600;
        letter-spacing: 0.025em;
        border: 1px solid transparent;
    }
    .badge-ok { background: rgba(54, 207, 153, 0.13); color: #70E1B4; border-color: rgba(54, 207, 153, 0.25); }
    .badge-info { background: rgba(255, 75, 75, 0.13); color: #FF8C8C; border-color: rgba(255, 75, 75, 0.26); }
    .badge-warn { background: rgba(247, 201, 72, 0.13); color: #F7C948; border-color: rgba(247, 201, 72, 0.26); }
    .badge-err { background: rgba(255, 75, 75, 0.16); color: #FF8C8C; border-color: rgba(255, 75, 75, 0.3); }
    .scada-footer {
        background: linear-gradient(100deg, rgba(255, 75, 75, 0.07), rgba(21, 25, 34, 0.86));
        border: 1px solid var(--surface-border);
        border-radius: 14px;
        padding: 14px 18px;
        margin-top: 26px;
        font-size: 0.78rem;
        color: var(--text-muted);
    }
    @media (max-width: 700px) {
        [data-testid="stAppViewContainer"] > .main .block-container { padding: 1rem 1rem 2rem; }
        .scada-header { padding: 16px; border-radius: 14px; }
        .scada-title { font-size: 1rem; }
    }
</style>
"""

BadgeVariant = Literal["ok", "info", "warn", "err"]


def render_badge_html(label: str, variant: BadgeVariant = "info") -> str:
    """Return an HTML status pill string with escaped label text."""
    safe_label = html.escape(label)
    css_class = f"scada-badge badge-{variant}"
    return f'<span class="{css_class}">{safe_label}</span>'


def render_freshness_badge_html(freshness: FreshnessStatus) -> str:
    """Return an HTML badge representing data freshness and elapsed age."""
    variant_map: dict[str, BadgeVariant] = {
        "fresh": "ok",
        "forecast_horizon": "info",
        "stale": "warn",
        "missing": "err",
    }
    variant = variant_map.get(freshness.level, "info")
    text = f"{freshness.label} · {freshness.age_Text}"
    return render_badge_html(text, variant=variant)


def render_telemetry_card_html(
    *,
    title: str,
    primary_value: str,
    badge_html: str,
    meta_lines: list[str],
) -> str:
    """Render a clean SCADA telemetry readout card."""
    safe_title = html.escape(title)
    safe_value = html.escape(primary_value)
    meta_html = "".join(
        f'<div class="scada-meta-row">{html.escape(line)}</div>' for line in meta_lines
    )
    return (
        f'<div class="scada-panel">'
        f'  <div class="scada-panel-label">{safe_title}</div>'
        f'  <div class="scada-telemetry-value">{safe_value}</div>'
        f'  <div style="margin-bottom: 8px;">{badge_html}</div>'
        f"  {meta_html}"
        f"</div>"
    )
