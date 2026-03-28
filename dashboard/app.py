"""
10X Trading Dashboard — Streamlit main app.

Run with:  streamlit run dashboard/app.py
Opens at:  http://localhost:8501
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st
from data.database import init_db

# Ensure DB exists
init_db()

# Page config
st.set_page_config(
    page_title="10X Trading System",
    page_icon="\U0001f4c8",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Sidebar navigation
st.sidebar.title("\U0001f4c8 10X Trading")

page = st.sidebar.radio(
    "Navegacion",
    [
        "\U0001f4b0 Portfolio",
        "\U0001f4cb Posiciones",
        "\U0001f4d6 Historial",
        "\U0001f4c9 Riesgo",
        "\U0001f4ca Indicadores",
        "\U0001f5c3 Logs",
    ],
)

# Sidebar info
from config.loader import get_settings
settings = get_settings()
mode = settings.get("mode", "paper").upper()
st.sidebar.markdown("---")
st.sidebar.markdown(f"**Modo:** `{mode}`")
st.sidebar.markdown(f"**Capital inicial:** GBP {settings.get('initial_capital_gbp', 1000):,}")

from data.database import get_latest_equity
eq = get_latest_equity()
if eq:
    st.sidebar.metric("Capital actual", f"GBP {eq['total_capital']:,.2f}")

st.sidebar.markdown("---")
st.sidebar.caption("Auto-refresh: 30s")

# Route to pages
if "\U0001f4b0 Portfolio" in page:
    from dashboard.pages.portfolio import render
    render()
elif "\U0001f4cb Posiciones" in page:
    from dashboard.pages.positions import render
    render()
elif "\U0001f4d6 Historial" in page:
    from dashboard.pages.history import render
    render()
elif "\U0001f4c9 Riesgo" in page:
    from dashboard.pages.risk_metrics import render
    render()
elif "\U0001f4ca Indicadores" in page:
    from dashboard.pages.live_indicators import render
    render()
elif "\U0001f5c3 Logs" in page:
    from dashboard.pages.logs import render
    render()

# Auto-refresh every 30 seconds
st.markdown(
    """
    <meta http-equiv="refresh" content="30">
    """,
    unsafe_allow_html=True,
)
