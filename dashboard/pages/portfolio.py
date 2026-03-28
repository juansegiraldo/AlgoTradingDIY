"""Portfolio overview: capital, P&L, equity curve."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st
import plotly.graph_objects as go
from datetime import datetime, timezone

from config.loader import get_settings
from data.database import (
    get_latest_equity,
    get_equity_history,
    get_total_pnl,
    get_daily_pnl,
    get_win_rate,
    get_profit_factor,
    count_open_trades,
)
from risk.circuit_breaker import get_risk_status


def render():
    st.title("\U0001f4b0 Portfolio Overview")

    settings = get_settings()
    initial = settings.get("initial_capital_gbp", 1000)
    risk = get_risk_status()
    current = risk["capital_current"]
    total_pnl = get_total_pnl()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = get_daily_pnl(today)
    win_rate = get_win_rate()
    pf = get_profit_factor()
    total_return = ((current - initial) / initial * 100) if initial > 0 else 0

    # Top metrics row
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Capital", f"GBP {current:,.2f}", f"{total_return:+.1f}%")
    col2.metric("PnL Total", f"GBP {total_pnl:+,.2f}")
    col3.metric("PnL Hoy", f"GBP {daily:+,.2f}")
    col4.metric("Posiciones", count_open_trades())

    # Second row
    col5, col6, col7, col8 = st.columns(4)
    col5.metric("Win Rate", f"{win_rate:.1f}%" if win_rate else "N/A")
    col6.metric("Profit Factor", f"{pf:.2f}" if pf else "N/A")
    col7.metric("Drawdown", f"{risk['drawdown_pct']:.1f}%")
    col8.metric("Modo", settings.get("mode", "paper").upper())

    # Equity curve
    st.markdown("---")
    st.subheader("\U0001f4c8 Curva de Equity")

    history = get_equity_history(limit=1000)
    if history and len(history) > 1:
        history.reverse()
        timestamps = [h["timestamp"] for h in history]
        capitals = [h["total_capital"] for h in history]

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=timestamps,
            y=capitals,
            mode="lines+markers",
            name="Capital",
            line=dict(color="#00d4aa", width=2),
            fill="tozeroy",
            fillcolor="rgba(0,212,170,0.1)",
        ))
        fig.add_hline(
            y=initial,
            line_dash="dash",
            line_color="gray",
            annotation_text=f"Capital inicial: GBP {initial:,}",
        )
        fig.update_layout(
            yaxis_title="GBP",
            xaxis_title="Fecha",
            height=400,
            margin=dict(l=0, r=0, t=10, b=0),
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("La curva de equity aparecera cuando haya snapshots (se guardan con los reportes diarios).")

    # Risk status
    st.markdown("---")
    st.subheader("\u26a0 Estado de Riesgo")

    col_r1, col_r2, col_r3 = st.columns(3)

    with col_r1:
        daily_used = abs(daily) if daily < 0 else 0
        daily_max = abs(risk["daily_max_loss"])
        daily_pct = (daily_used / daily_max * 100) if daily_max > 0 else 0
        st.markdown("**Limite diario**")
        st.progress(min(daily_pct / 100, 1.0))
        st.caption(f"GBP {daily_used:.2f} / {daily_max:.2f} ({daily_pct:.0f}%)")

    with col_r2:
        weekly_pnl = risk["weekly_pnl"]
        weekly_used = abs(weekly_pnl) if weekly_pnl < 0 else 0
        weekly_max = abs(risk["weekly_max_loss"])
        weekly_pct = (weekly_used / weekly_max * 100) if weekly_max > 0 else 0
        st.markdown("**Limite semanal**")
        st.progress(min(weekly_pct / 100, 1.0))
        st.caption(f"GBP {weekly_used:.2f} / {weekly_max:.2f} ({weekly_pct:.0f}%)")

    with col_r3:
        dd = risk["drawdown_pct"]
        dd_max = risk["drawdown_max_pct"]
        st.markdown("**Drawdown total**")
        st.progress(min(max(dd, 0) / dd_max, 1.0))
        st.caption(f"{dd:.1f}% / {dd_max}% max")

    if risk["circuit_breaker_active"]:
        st.error(
            f"\U0001f6a8 CIRCUIT BREAKER ACTIVO: "
            f"{risk['circuit_breaker']['rule_triggered']} — "
            f"{risk['circuit_breaker'].get('details', '')}"
        )
