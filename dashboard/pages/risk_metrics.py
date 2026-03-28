"""Risk metrics: drawdown, win rate, Sharpe ratio, profit factor."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st
import plotly.graph_objects as go

from data.database import (
    get_all_trades,
    get_win_rate,
    get_profit_factor,
    get_total_pnl,
    get_equity_history,
)
from risk.circuit_breaker import get_risk_status


def render():
    st.title("\U0001f4c9 Metricas de Riesgo")

    risk = get_risk_status()
    win_rate = get_win_rate()
    pf = get_profit_factor()
    trades = [t for t in get_all_trades(500) if t["status"] != "open"]

    # Key metrics
    col1, col2, col3, col4 = st.columns(4)
    col1.metric(
        "Win Rate",
        f"{win_rate:.1f}%" if win_rate else "N/A",
        help="Objetivo: >45%. Alarma: <35%",
    )
    col2.metric(
        "Profit Factor",
        f"{pf:.2f}" if pf else "N/A",
        help="Objetivo: >1.5. Alarma: <1.0",
    )
    col3.metric(
        "Drawdown",
        f"{risk['drawdown_pct']:.1f}%",
        help=f"Max permitido: {risk['drawdown_max_pct']}%",
    )
    col4.metric(
        "Trades totales",
        len(trades),
    )

    st.markdown("---")

    # Risk gauges
    st.subheader("\u26a0 Limites de Riesgo")

    col_a, col_b, col_c = st.columns(3)

    with col_a:
        st.markdown("**Perdida diaria**")
        daily_used = abs(risk["daily_pnl"]) if risk["daily_pnl"] < 0 else 0
        daily_max = abs(risk["daily_max_loss"])
        pct = daily_used / daily_max if daily_max > 0 else 0
        st.progress(min(pct, 1.0))
        st.caption(f"GBP {daily_used:.2f} / {daily_max:.2f}")
        if pct > 0.8:
            st.warning("Cerca del limite diario")

    with col_b:
        st.markdown("**Perdida semanal**")
        weekly_used = abs(risk["weekly_pnl"]) if risk["weekly_pnl"] < 0 else 0
        weekly_max = abs(risk["weekly_max_loss"])
        pct = weekly_used / weekly_max if weekly_max > 0 else 0
        st.progress(min(pct, 1.0))
        st.caption(f"GBP {weekly_used:.2f} / {weekly_max:.2f}")

    with col_c:
        st.markdown("**Drawdown total**")
        dd = max(risk["drawdown_pct"], 0)
        dd_max = risk["drawdown_max_pct"]
        st.progress(min(dd / dd_max, 1.0))
        st.caption(f"{dd:.1f}% / {dd_max}%")
        if dd > 30:
            st.error("Drawdown critico")

    # Circuit breaker
    if risk["circuit_breaker_active"]:
        st.error(
            f"\U0001f6a8 **CIRCUIT BREAKER ACTIVO**\n\n"
            f"Regla: {risk['circuit_breaker']['rule_triggered']}\n\n"
            f"{risk['circuit_breaker'].get('details', '')}"
        )
    else:
        st.success("\u2705 Circuit breaker: OK (sin activar)")

    # Performance table
    if trades:
        st.markdown("---")
        st.subheader("\U0001f4ca Resumen de Performance")

        pnls = [(t.get("pnl_absolute") or 0) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        col1, col2 = st.columns(2)

        with col1:
            st.markdown("**Ganancias**")
            st.write(f"- Trades ganados: {len(wins)}")
            st.write(f"- Ganancia promedio: GBP {(sum(wins)/len(wins)):,.2f}" if wins else "- N/A")
            st.write(f"- Mayor ganancia: GBP {max(wins):,.2f}" if wins else "- N/A")
            st.write(f"- Total ganancias: GBP {sum(wins):,.2f}" if wins else "- N/A")

        with col2:
            st.markdown("**Perdidas**")
            st.write(f"- Trades perdidos: {len(losses)}")
            st.write(f"- Perdida promedio: GBP {(sum(losses)/len(losses)):,.2f}" if losses else "- N/A")
            st.write(f"- Mayor perdida: GBP {min(losses):,.2f}" if losses else "- N/A")
            st.write(f"- Total perdidas: GBP {sum(losses):,.2f}" if losses else "- N/A")

        # Avg R:R
        if wins and losses:
            avg_win = sum(wins) / len(wins)
            avg_loss = abs(sum(losses) / len(losses))
            rr = avg_win / avg_loss if avg_loss > 0 else 0
            st.metric("Ratio Riesgo/Recompensa", f"1:{rr:.2f}", help="Objetivo: >1:2")
