"""System logs view."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st
import pandas as pd

from data.database import get_recent_logs, get_logs_by_level, get_circuit_breaker_history


def render():
    st.title("\U0001f5c3 Logs del Sistema")

    # Filters
    col1, col2 = st.columns(2)
    level = col1.selectbox("Nivel", ["ALL", "INFO", "WARNING", "ERROR", "CIRCUIT_BREAKER"])
    limit = col2.slider("Cantidad", 10, 200, 50)

    # Fetch logs
    if level == "ALL":
        logs = get_recent_logs(limit)
    else:
        logs = get_logs_by_level(level, limit)

    if not logs:
        st.info("No hay logs todavia.")
        return

    # Color coding
    level_colors = {
        "INFO": "\U0001f7e2",
        "WARNING": "\U0001f7e1",
        "ERROR": "\U0001f534",
        "CIRCUIT_BREAKER": "\U0001f6a8",
    }

    # Display as table
    df = pd.DataFrame([
        {
            "": level_colors.get(l["level"], "\u26aa"),
            "Hora": l["timestamp"][11:19] if len(l["timestamp"]) > 19 else l["timestamp"],
            "Fecha": l["timestamp"][:10],
            "Nivel": l["level"],
            "Modulo": l["module"],
            "Mensaje": l["message"],
        }
        for l in logs
    ])
    st.dataframe(df, use_container_width=True, hide_index=True, height=500)

    # Circuit breaker history
    st.markdown("---")
    st.subheader("\U0001f6a8 Historial Circuit Breaker")

    cb_history = get_circuit_breaker_history(20)
    if cb_history:
        df_cb = pd.DataFrame([
            {
                "Fecha": cb["timestamp"][:19],
                "Regla": cb["rule_triggered"],
                "Detalle": cb.get("details", ""),
                "Reanuda": cb.get("resume_after", "")[:19],
            }
            for cb in cb_history
        ])
        st.dataframe(df_cb, use_container_width=True, hide_index=True)
    else:
        st.success("\u2705 Sin eventos de circuit breaker")
