"""Live indicators: RSI, EMA, MACD for monitored pairs."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from config.loader import get_settings
from signals.indicators import (
    ohlcv_to_dataframe,
    compute_rsi,
    compute_ema,
    compute_macd,
    check_rsi,
    check_ema_crossover,
    check_macd,
    check_volume,
    analyze,
)


def render():
    st.title("\U0001f4ca Indicadores en Vivo")

    settings = get_settings()
    crypto = settings["markets"]["crypto"]
    pairs = crypto["pairs"]
    timeframes = crypto["timeframes"]

    # Selector
    col1, col2 = st.columns(2)
    pair = col1.selectbox("Par", pairs)
    tf = col2.selectbox("Temporalidad", timeframes)

    # Fetch data
    try:
        from execution.binance_executor import fetch_ohlcv
        ohlcv = fetch_ohlcv(pair, tf, limit=100)
        df = ohlcv_to_dataframe(ohlcv)
    except Exception as e:
        st.error(f"Error obteniendo datos: {e}")
        return

    # Run analysis
    analysis = analyze(df)
    rsi = analysis["rsi"]
    ema = analysis["ema"]
    macd = analysis["macd"]
    vol = analysis["volume"]
    trend = analysis["trend"]

    # Signal summary
    st.markdown("---")
    st.subheader(f"\U0001f50d {pair} ({tf})")

    col_a, col_b, col_c, col_d, col_e = st.columns(5)

    with col_a:
        icon = "\u2705" if rsi["triggered"] else "\u26aa"
        st.metric(f"{icon} RSI", f"{rsi.get('value', 'N/A')}")
        if rsi["signal"]:
            st.caption(f"Signal: {rsi['signal'].upper()}")

    with col_b:
        icon = "\u2705" if ema["triggered"] else "\u26aa"
        st.metric(f"{icon} EMA Cross", f"{ema.get('diff', 'N/A')}")
        if ema["signal"]:
            st.caption(f"Signal: {ema['signal'].upper()}")

    with col_c:
        icon = "\u2705" if macd["triggered"] else "\u26aa"
        macd_val = macd.get("value", {})
        st.metric(f"{icon} MACD", f"{macd_val.get('histogram', 'N/A')}")
        if macd["signal"]:
            st.caption(f"Signal: {macd['signal'].upper()}")

    with col_d:
        icon = "\u2705" if vol["triggered"] else "\u26aa"
        vol_val = vol.get("value", {})
        st.metric(f"{icon} Volume", f"{vol_val.get('ratio', 'N/A')}x")

    with col_e:
        trend_icon = {
            "bullish": "\U0001f7e2",
            "bearish": "\U0001f534",
            "mixed": "\U0001f7e1",
        }.get(trend.get("trend"), "\u26aa")
        st.metric(f"{trend_icon} Trend", trend.get("trend", "N/A").upper())

    # Candlestick + indicators chart
    st.markdown("---")
    st.subheader("\U0001f4c8 Grafico")

    ind_cfg = settings.get("indicators", {})
    ema_fast_s = compute_ema(df, ind_cfg.get("ema_fast", 9))
    ema_slow_s = compute_ema(df, ind_cfg.get("ema_slow", 21))
    rsi_s = compute_rsi(df)
    macd_data = compute_macd(df)

    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.5, 0.25, 0.25],
        subplot_titles=["Precio + EMAs", "RSI", "MACD"],
    )

    # Candlestick
    fig.add_trace(go.Candlestick(
        x=df.index, open=df["open"], high=df["high"],
        low=df["low"], close=df["close"], name="Price",
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=df.index, y=ema_fast_s, name=f"EMA {ind_cfg.get('ema_fast', 9)}",
        line=dict(color="orange", width=1),
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=df.index, y=ema_slow_s, name=f"EMA {ind_cfg.get('ema_slow', 21)}",
        line=dict(color="blue", width=1),
    ), row=1, col=1)

    # RSI
    fig.add_trace(go.Scatter(
        x=df.index, y=rsi_s, name="RSI",
        line=dict(color="purple", width=1),
    ), row=2, col=1)
    fig.add_hline(y=50, line_dash="dash", line_color="gray", row=2, col=1)
    fig.add_hline(y=70, line_dash="dot", line_color="red", row=2, col=1)
    fig.add_hline(y=30, line_dash="dot", line_color="green", row=2, col=1)

    # MACD
    fig.add_trace(go.Scatter(
        x=df.index, y=macd_data["macd"], name="MACD",
        line=dict(color="blue", width=1),
    ), row=3, col=1)
    fig.add_trace(go.Scatter(
        x=df.index, y=macd_data["signal"], name="Signal",
        line=dict(color="orange", width=1),
    ), row=3, col=1)
    hist = macd_data["histogram"]
    colors = ["#00d4aa" if h >= 0 else "#ff4b4b" for h in hist]
    fig.add_trace(go.Bar(
        x=df.index, y=hist, name="Histogram",
        marker_color=colors,
    ), row=3, col=1)

    fig.update_layout(
        height=700,
        showlegend=False,
        xaxis_rangeslider_visible=False,
        margin=dict(l=0, r=0, t=30, b=0),
    )

    st.plotly_chart(fig, use_container_width=True)
