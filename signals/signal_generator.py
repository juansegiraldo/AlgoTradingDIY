"""
Signal generation logic.

Rule: If at least 3 of 4 indicators are aligned in the same direction,
generate a trading signal (long or short).

This module takes indicator results from indicators.py and decides
whether a signal is strong enough to act on.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Signal evaluation
# ---------------------------------------------------------------------------


def evaluate_signal(
    pair: str,
    timeframe: str,
    market: str,
    analysis: dict,
) -> Optional[dict]:
    """
    Evaluate indicator analysis and generate a signal if criteria are met.

    Args:
        pair: e.g. "BTC/USDT"
        timeframe: e.g. "1h"
        market: "crypto", "forex", or "etf"
        analysis: output from indicators.analyze()

    Returns:
        Signal dict if triggered, None otherwise.
    """
    cfg = get_settings().get("indicators", {})
    min_signals = cfg.get("min_signals_for_entry", 3)
    pm = get_settings().get("position_management", {})

    rsi = analysis["rsi"]
    ema = analysis["ema"]
    macd = analysis["macd"]
    volume = analysis["volume"]
    trend = analysis.get("trend", {})

    # Count directional signals
    long_count = 0
    short_count = 0
    signals_detail = {}

    # RSI
    if rsi["triggered"]:
        if rsi["signal"] == "long":
            long_count += 1
            signals_detail["rsi"] = True
        elif rsi["signal"] == "short":
            short_count += 1
            signals_detail["rsi"] = True
    else:
        signals_detail["rsi"] = False

    # EMA crossover
    if ema["triggered"]:
        if ema["signal"] == "long":
            long_count += 1
            signals_detail["ema"] = True
        elif ema["signal"] == "short":
            short_count += 1
            signals_detail["ema"] = True
    else:
        signals_detail["ema"] = False

    # MACD crossover
    if macd["triggered"]:
        if macd["signal"] == "long":
            long_count += 1
            signals_detail["macd"] = True
        elif macd["signal"] == "short":
            short_count += 1
            signals_detail["macd"] = True
    else:
        signals_detail["macd"] = False

    # Volume (direction-agnostic, confirms the dominant direction)
    if volume["triggered"]:
        signals_detail["volume"] = True
        # Add volume confirmation to whichever direction leads
        if long_count > short_count:
            long_count += 1
        elif short_count > long_count:
            short_count += 1
        else:
            # Tied — volume alone doesn't break the tie
            pass
    else:
        signals_detail["volume"] = False

    # Determine direction and strength
    direction = None
    signal_count = 0

    if long_count >= min_signals:
        direction = "long"
        signal_count = long_count
    elif short_count >= min_signals:
        direction = "short"
        signal_count = short_count

    if direction is None:
        logger.debug(
            f"{pair} ({timeframe}): No signal. "
            f"Long={long_count}, Short={short_count}, Need={min_signals}"
        )
        return None

    # Calculate entry price and levels
    price = trend.get("price") or _get_last_price(analysis)
    if price is None or price <= 0:
        logger.warning(f"{pair}: Cannot generate signal — no valid price")
        return None

    sl_pct = pm.get("stop_loss_pct", 2.0) / 100.0
    tp1_pct = pm.get("take_profit_1_pct", 3.0) / 100.0
    tp2_pct = pm.get("take_profit_2_pct", 6.0) / 100.0

    if direction == "long":
        stop_loss = round(price * (1 - sl_pct), 8)
        tp1 = round(price * (1 + tp1_pct), 8)
        tp2 = round(price * (1 + tp2_pct), 8)
    else:
        stop_loss = round(price * (1 + sl_pct), 8)
        tp1 = round(price * (1 - tp1_pct), 8)
        tp2 = round(price * (1 - tp2_pct), 8)

    # Determine leverage from market settings
    market_cfg = get_settings().get("markets", {}).get(market, {})
    leverage = market_cfg.get("leverage_default", 1)

    signal = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pair": pair,
        "timeframe": timeframe,
        "market": market,
        "direction": direction,
        "entry_price": price,
        "stop_loss": stop_loss,
        "take_profit_1": tp1,
        "take_profit_2": tp2,
        "leverage": leverage,
        "signal_count": signal_count,
        "min_required": min_signals,
        "signals_triggered": signals_detail,
        "indicators": {
            "rsi": rsi,
            "ema": ema,
            "macd": macd,
            "volume": volume,
        },
        "trend": trend.get("trend", "unknown"),
        "strength": _classify_strength(signal_count, min_signals),
    }

    logger.info(
        f"SIGNAL: {pair} ({timeframe}) {direction.upper()} "
        f"@ {price:,.2f} | {signal_count}/{4} indicators | "
        f"Strength: {signal['strength']}"
    )
    return signal


def _get_last_price(analysis: dict) -> Optional[float]:
    """Try to extract last price from indicator data."""
    ema_data = analysis.get("ema", {}).get("value")
    if isinstance(ema_data, dict):
        # Return first EMA value as proxy
        for v in ema_data.values():
            return v
    return None


def _classify_strength(signal_count: int, min_required: int) -> str:
    """Classify signal strength."""
    if signal_count >= 4:
        return "strong"
    elif signal_count >= min_required:
        return "moderate"
    else:
        return "weak"


# ---------------------------------------------------------------------------
# Signal formatting for Telegram
# ---------------------------------------------------------------------------


def format_signal_for_telegram(signal: dict, risk_gbp: float = 0, risk_pct: float = 0, position_size: str = "") -> dict:
    """
    Convert a signal dict into the format expected by telegram_bot.send_signal_alert().
    """
    return {
        "pair": signal["pair"],
        "direction": signal["direction"],
        "entry_price": signal["entry_price"],
        "stop_loss": signal["stop_loss"],
        "take_profit_1": signal["take_profit_1"],
        "take_profit_2": signal["take_profit_2"],
        "position_size": position_size,
        "risk_gbp": round(risk_gbp, 2),
        "risk_pct": round(risk_pct, 2),
        "leverage": signal["leverage"],
        "market": signal["market"],
        "signals_triggered": signal["signals_triggered"],
    }
