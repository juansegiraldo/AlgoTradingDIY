"""Open positions view."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st
import pandas as pd

from data.database import get_open_trades


def render():
    st.title("\U0001f4cb Posiciones Abiertas")

    trades = get_open_trades()

    if not trades:
        st.info("\U0001f4ad No hay posiciones abiertas en este momento.")
        return

    # Try to get current prices for unrealized P&L
    for t in trades:
        try:
            if t["market"] == "crypto":
                from execution.binance_executor import fetch_price
                current = fetch_price(t["pair"])
                if t["direction"] == "long":
                    upnl = (current - t["entry_price"]) / t["entry_price"] * 100
                else:
                    upnl = (t["entry_price"] - current) / t["entry_price"] * 100
                t["current_price"] = current
                t["unrealized_pnl_pct"] = round(upnl, 2)
            else:
                t["current_price"] = None
                t["unrealized_pnl_pct"] = None
        except Exception:
            t["current_price"] = None
            t["unrealized_pnl_pct"] = None

    # Display as cards
    for t in trades:
        direction_icon = "\U0001f7e2" if t["direction"] == "long" else "\U0001f534"
        with st.container():
            col1, col2, col3, col4 = st.columns([2, 2, 2, 1])

            with col1:
                st.markdown(f"### {direction_icon} {t['pair']}")
                st.caption(f"{t['direction'].upper()} | {t['market']} | Lev: {t['leverage']}x")

            with col2:
                st.metric("Entrada", f"{t['entry_price']:,.2f}")
                if t.get("current_price"):
                    st.metric("Actual", f"{t['current_price']:,.2f}")

            with col3:
                st.metric("Stop-Loss", f"{t['stop_loss']:,.2f}")
                if t.get("take_profit_1"):
                    st.metric("TP1", f"{t['take_profit_1']:,.2f}")

            with col4:
                if t.get("unrealized_pnl_pct") is not None:
                    pnl = t["unrealized_pnl_pct"]
                    color = "green" if pnl >= 0 else "red"
                    st.markdown(
                        f"<h2 style='color:{color};text-align:center'>"
                        f"{pnl:+.2f}%</h2>",
                        unsafe_allow_html=True,
                    )

            st.markdown("---")

    # Also show as table
    st.subheader("Tabla resumen")
    df = pd.DataFrame([
        {
            "Par": t["pair"],
            "Dir": t["direction"].upper(),
            "Entrada": t["entry_price"],
            "SL": t["stop_loss"],
            "TP1": t.get("take_profit_1"),
            "Lev": f"{t['leverage']}x",
            "PnL%": t.get("unrealized_pnl_pct"),
            "Modo": t.get("mode", "paper"),
            "Abierto": t["timestamp_open"][:16],
        }
        for t in trades
    ])
    st.dataframe(df, use_container_width=True, hide_index=True)
