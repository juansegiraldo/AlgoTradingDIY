"""
Technical indicators: RSI, EMA crossover, MACD crossover, Volume spike.

Each function takes a pandas DataFrame with OHLCV columns and returns
a dict with the indicator value + a boolean signal (bullish/bearish).

DataFrame expected columns: open, high, low, close, volume
"""

import pandas as pd
import ta

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import get_settings


def _get_indicator_settings() -> dict:
    return get_settings().get("indicators", {})


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------


def compute_rsi(df: pd.DataFrame, period: int = None) -> pd.Series:
    """Compute RSI series."""
    cfg = _get_indicator_settings()
    period = period or cfg.get("rsi_period", 14)
    return ta.momentum.RSIIndicator(close=df["close"], window=period).rsi()


def check_rsi(df: pd.DataFrame) -> dict:
    """
    RSI signal:
    - LONG:  RSI crosses above 50 from below
    - SHORT: RSI crosses below 50 from above
    """
    rsi = compute_rsi(df)
    if len(rsi) < 3:
        return {"name": "rsi", "value": None, "signal": None, "triggered": False}

    current = rsi.iloc[-1]
    previous = rsi.iloc[-2]

    signal = None
    triggered = False

    if previous < 50 and current >= 50:
        signal = "long"
        triggered = True
    elif previous > 50 and current <= 50:
        signal = "short"
        triggered = True

    return {
        "name": "rsi",
        "value": round(current, 2),
        "previous": round(previous, 2),
        "signal": signal,
        "triggered": triggered,
    }


# ---------------------------------------------------------------------------
# EMA Crossover (fast/slow)
# ---------------------------------------------------------------------------


def compute_ema(df: pd.DataFrame, period: int) -> pd.Series:
    """Compute EMA series for a given period."""
    return ta.trend.EMAIndicator(close=df["close"], window=period).ema_indicator()


def check_ema_crossover(df: pd.DataFrame) -> dict:
    """
    EMA crossover signal:
    - LONG:  EMA fast crosses above EMA slow
    - SHORT: EMA fast crosses below EMA slow
    """
    cfg = _get_indicator_settings()
    fast_period = cfg.get("ema_fast", 9)
    slow_period = cfg.get("ema_slow", 21)

    ema_fast = compute_ema(df, fast_period)
    ema_slow = compute_ema(df, slow_period)

    if len(ema_fast) < 3 or len(ema_slow) < 3:
        return {"name": "ema", "value": None, "signal": None, "triggered": False}

    # Current and previous differences
    diff_now = ema_fast.iloc[-1] - ema_slow.iloc[-1]
    diff_prev = ema_fast.iloc[-2] - ema_slow.iloc[-2]

    signal = None
    triggered = False

    if diff_prev <= 0 and diff_now > 0:
        signal = "long"
        triggered = True
    elif diff_prev >= 0 and diff_now < 0:
        signal = "short"
        triggered = True

    return {
        "name": "ema",
        "value": {
            f"ema_{fast_period}": round(ema_fast.iloc[-1], 4),
            f"ema_{slow_period}": round(ema_slow.iloc[-1], 4),
        },
        "diff": round(diff_now, 4),
        "signal": signal,
        "triggered": triggered,
    }


# ---------------------------------------------------------------------------
# MACD Crossover
# ---------------------------------------------------------------------------


def compute_macd(df: pd.DataFrame) -> dict:
    """Compute MACD line, signal line, and histogram."""
    cfg = _get_indicator_settings()
    macd_obj = ta.trend.MACD(
        close=df["close"],
        window_slow=cfg.get("macd_slow", 26),
        window_fast=cfg.get("macd_fast", 12),
        window_sign=cfg.get("macd_signal", 9),
    )
    return {
        "macd": macd_obj.macd(),
        "signal": macd_obj.macd_signal(),
        "histogram": macd_obj.macd_diff(),
    }


def check_macd(df: pd.DataFrame) -> dict:
    """
    MACD crossover signal:
    - LONG:  MACD line crosses above Signal line
    - SHORT: MACD line crosses below Signal line
    """
    macd_data = compute_macd(df)
    macd_line = macd_data["macd"]
    signal_line = macd_data["signal"]

    if len(macd_line) < 3 or len(signal_line) < 3:
        return {"name": "macd", "value": None, "signal": None, "triggered": False}

    diff_now = macd_line.iloc[-1] - signal_line.iloc[-1]
    diff_prev = macd_line.iloc[-2] - signal_line.iloc[-2]

    signal = None
    triggered = False

    if diff_prev <= 0 and diff_now > 0:
        signal = "long"
        triggered = True
    elif diff_prev >= 0 and diff_now < 0:
        signal = "short"
        triggered = True

    return {
        "name": "macd",
        "value": {
            "macd": round(macd_line.iloc[-1], 4),
            "signal": round(signal_line.iloc[-1], 4),
            "histogram": round(macd_data["histogram"].iloc[-1], 4),
        },
        "signal": signal,
        "triggered": triggered,
    }


# ---------------------------------------------------------------------------
# Volume Spike
# ---------------------------------------------------------------------------


def check_volume(df: pd.DataFrame) -> dict:
    """
    Volume spike signal:
    - Triggered when current volume > multiplier × average volume (lookback)
    - Direction-agnostic: confirms momentum in either direction.
    """
    cfg = _get_indicator_settings()
    multiplier = cfg.get("volume_multiplier", 1.5)
    lookback = cfg.get("volume_lookback", 20)

    if len(df) < lookback + 1:
        return {"name": "volume", "value": None, "signal": None, "triggered": False}

    current_vol = df["volume"].iloc[-1]
    avg_vol = df["volume"].iloc[-(lookback + 1):-1].mean()

    triggered = current_vol > (multiplier * avg_vol)

    # Volume is direction-agnostic, so signal is 'confirm' meaning
    # it confirms whichever direction the other indicators point to
    return {
        "name": "volume",
        "value": {
            "current": round(current_vol, 2),
            "average": round(avg_vol, 2),
            "ratio": round(current_vol / avg_vol, 2) if avg_vol > 0 else 0,
        },
        "threshold": multiplier,
        "signal": "confirm" if triggered else None,
        "triggered": triggered,
    }


# ---------------------------------------------------------------------------
# EMA Trend (50/200 for forex swing trading context)
# ---------------------------------------------------------------------------


def check_trend(df: pd.DataFrame) -> dict:
    """
    Trend context using EMA 50 and EMA 200:
    - Bullish: price > EMA 50 > EMA 200
    - Bearish: price < EMA 50 < EMA 200
    - Mixed: anything else

    This is NOT a signal generator, it's contextual info for filtering.
    """
    cfg = _get_indicator_settings()
    ema_50 = compute_ema(df, cfg.get("ema_trend", 50))
    ema_200 = compute_ema(df, cfg.get("ema_trend_long", 200))

    if len(ema_50) < 1 or len(ema_200) < 1:
        return {"trend": "unknown", "ema_50": None, "ema_200": None}

    price = df["close"].iloc[-1]
    e50 = ema_50.iloc[-1]
    e200 = ema_200.iloc[-1]

    if price > e50 > e200:
        trend = "bullish"
    elif price < e50 < e200:
        trend = "bearish"
    else:
        trend = "mixed"

    return {
        "trend": trend,
        "price": round(price, 4),
        "ema_50": round(e50, 4),
        "ema_200": round(e200, 4),
    }


# ---------------------------------------------------------------------------
# Run all 4 indicators at once
# ---------------------------------------------------------------------------


def analyze(df: pd.DataFrame) -> dict:
    """
    Run all 4 indicators and return a complete analysis.

    Returns:
        {
            "rsi": {...},
            "ema": {...},
            "macd": {...},
            "volume": {...},
            "trend": {...},
        }
    """
    return {
        "rsi": check_rsi(df),
        "ema": check_ema_crossover(df),
        "macd": check_macd(df),
        "volume": check_volume(df),
        "trend": check_trend(df),
    }


# ---------------------------------------------------------------------------
# Helper: convert OHLCV list (from ccxt) to DataFrame
# ---------------------------------------------------------------------------


def ohlcv_to_dataframe(ohlcv: list) -> pd.DataFrame:
    """Convert ccxt OHLCV list to a pandas DataFrame."""
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df
