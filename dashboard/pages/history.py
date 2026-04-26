"""Trade history view."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st
import pandas as pd
import plotly.graph_objects as go

from data.database import get_all_trades


def render():
    st.title("\U0001f4d6 Historial de Trades")

    trades = get_all_trades(limit=500)
    closed = [t for t in trades if t["status"] != "open"]

    if not closed:
        st.info("No hay trades cerrados todavia. Apareceraan cuando el sistema ejecute y cierre posiciones.")
        return

    # Summary metrics
    wins = [t for t in closed if (t.get("pnl_absolute") or 0) > 0]
    losses = [t for t in closed if (t.get("pnl_absolute") or 0) < 0]
    total_pnl = sum(t.get("pnl_absolute", 0) or 0 for t in closed)
    total_fees = sum(t.get("total_fees_gbp", 0) or 0 for t in closed)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total trades", len(closed))
    col2.metric("Ganados", len(wins))
    col3.metric("Perdidos", len(losses))
    col4.metric("PnL neto", f"GBP {total_pnl:+,.2f}", delta=f"Fees -GBP {total_fees:,.2f}")

    # PnL per trade chart
    st.subheader("\U0001f4ca PnL por trade")
    pnls = [t.get("pnl_absolute", 0) or 0 for t in reversed(closed)]
    colors = ["#00d4aa" if p >= 0 else "#ff4b4b" for p in pnls]
    pairs = [t["pair"] for t in reversed(closed)]

    fig = go.Figure(data=[
        go.Bar(x=list(range(1, len(pnls) + 1)), y=pnls, marker_color=colors,
               text=[f"{p:+.2f}" for p in pnls], textposition="auto",
               hovertext=pairs, hoverinfo="text+y")
    ])
    fig.update_layout(
        xaxis_title="Trade #",
        yaxis_title="PnL (GBP)",
        height=350,
        margin=dict(l=0, r=0, t=10, b=0),
    )
    st.plotly_chart(fig, use_container_width=True)

    # Cumulative PnL
    st.subheader("\U0001f4c8 PnL acumulado")
    cumulative = []
    running = 0
    for p in pnls:
        running += p
        cumulative.append(running)

    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(
        x=list(range(1, len(cumulative) + 1)),
        y=cumulative,
        mode="lines+markers",
        line=dict(color="#00d4aa", width=2),
        fill="tozeroy",
        fillcolor="rgba(0,212,170,0.1)",
    ))
    fig2.add_hline(y=0, line_dash="dash", line_color="gray")
    fig2.update_layout(
        xaxis_title="Trade #",
        yaxis_title="PnL acumulado (GBP)",
        height=300,
        margin=dict(l=0, r=0, t=10, b=0),
    )
    st.plotly_chart(fig2, use_container_width=True)

    # Full table
    st.subheader("\U0001f4cb Detalle")
    df = pd.DataFrame([
        {
            "ID": t["id"],
            "Par": t["pair"],
            "Dir": t["direction"].upper(),
            "Entrada": t["entry_price"],
            "Salida": t.get("exit_price"),
            "Gross": f"GBP {(t.get('pnl_gross_gbp') or t.get('pnl_absolute') or 0):+.2f}",
            "Fees": f"GBP -{(t.get('total_fees_gbp') or 0):.2f}",
            "Net": f"GBP {(t.get('pnl_absolute') or 0):+.2f}",
            "PnL%": f"{(t.get('pnl_percent') or 0):+.1f}%",
            "Status": t["status"],
            "Modo": t.get("mode", "paper"),
            "Abierto": t["timestamp_open"][:16],
            "Cerrado": (t.get("timestamp_close") or "")[:16],
        }
        for t in closed
    ])
    st.dataframe(df, use_container_width=True, hide_index=True)
