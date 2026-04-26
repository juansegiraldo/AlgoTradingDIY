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

    market_cfg = get_settings().get("markets", {}).get(market, {})
    if direction == "short" and market_cfg.get("allow_short", True) is False:
        logger.info(f"{pair} ({timeframe}): Short signal skipped because shorts are disabled")
        return None

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

    levels = calculate_exit_levels(
        pair=pair,
        timeframe=timeframe,
        market=market,
        direction=direction,
        price=price,
        analysis=analysis,
    )
    stop_loss = levels["stop_loss"]
    tp1 = levels["take_profit_1"]
    tp2 = levels["take_profit_2"]

    # Determine leverage from market settings
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
        "exit_model": levels["exit_model"],
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


def _timeframe_minutes(timeframe: str) -> int:
    raw = str(timeframe).strip().lower()
    if raw.endswith("m"):
        return int(raw[:-1])
    if raw.endswith("h"):
        return int(raw[:-1]) * 60
    if raw.endswith("d"):
        return int(raw[:-1]) * 60 * 24
    return 60


def _timeframe_floor_pct(timeframe: str, floors: dict, default: float) -> float:
    if timeframe in floors:
        return float(floors[timeframe])
    minutes = _timeframe_minutes(timeframe)
    if minutes <= 60:
        return float(floors.get("1h", default))
    if minutes <= 240:
        return float(floors.get("4h", default))
    return float(default)


def _use_paper_atr_exits(market: str) -> bool:
    settings = get_settings()
    if settings.get("mode", "paper") != "paper":
        return False
    if market != "crypto":
        return False
    profile = settings.get("position_management", {}).get("paper_crypto_atr", {})
    return bool(profile.get("enabled", False))


def calculate_exit_levels(
    pair: str,
    timeframe: str,
    market: str,
    direction: str,
    price: float,
    analysis: Optional[dict] = None,
) -> dict:
    """Calculate stop-loss and take-profit levels for a signal."""
    settings = get_settings()
    pm = settings.get("position_management", {})

    if _use_paper_atr_exits(market):
        profile = pm.get("paper_crypto_atr", {})
        volatility = (analysis or {}).get("volatility", {})
        atr_pct = volatility.get("atr_pct")
        try:
            atr_pct = float(atr_pct)
        except (TypeError, ValueError):
            atr_pct = 0.0

        fixed_floor = float(pm.get("stop_loss_pct", 2.0))
        floors = profile.get("min_stop_loss_pct_by_timeframe", {})
        min_stop_pct = _timeframe_floor_pct(timeframe, floors, fixed_floor)
        atr_mult = float(profile.get("atr_stop_multiplier", 2.5))
        max_stop_pct = float(profile.get("max_stop_loss_pct", 8.0))
        stop_pct = max(min_stop_pct, atr_pct * atr_mult)
        stop_pct = min(stop_pct, max_stop_pct)
        tp1_r = float(profile.get("tp1_r_multiple", 1.5))
        tp2_r = float(profile.get("tp2_r_multiple", 2.5))
        tp1_pct = stop_pct * tp1_r
        tp2_pct = stop_pct * tp2_r
        model = {
            "type": "paper_atr",
            "atr_pct": round(atr_pct, 4),
            "atr_stop_multiplier": atr_mult,
            "stop_loss_pct": round(stop_pct, 4),
            "tp1_r_multiple": tp1_r,
            "tp2_r_multiple": tp2_r,
            "take_profit_1_pct": round(tp1_pct, 4),
            "take_profit_2_pct": round(tp2_pct, 4),
        }
    else:
        stop_pct = float(pm.get("stop_loss_pct", 2.0))
        tp1_pct = float(pm.get("take_profit_1_pct", 3.0))
        tp2_pct = float(pm.get("take_profit_2_pct", 6.0))
        model = {
            "type": "fixed_pct",
            "stop_loss_pct": round(stop_pct, 4),
            "take_profit_1_pct": round(tp1_pct, 4),
            "take_profit_2_pct": round(tp2_pct, 4),
        }

    sl_fraction = stop_pct / 100.0
    tp1_fraction = tp1_pct / 100.0
    tp2_fraction = tp2_pct / 100.0

    if direction == "long":
        stop_loss = round(price * (1 - sl_fraction), 8)
        tp1 = round(price * (1 + tp1_fraction), 8)
        tp2 = round(price * (1 + tp2_fraction), 8)
    else:
        stop_loss = round(price * (1 + sl_fraction), 8)
        tp1 = round(price * (1 - tp1_fraction), 8)
        tp2 = round(price * (1 - tp2_fraction), 8)

    return {
        "pair": pair,
        "timeframe": timeframe,
        "stop_loss": stop_loss,
        "take_profit_1": tp1,
        "take_profit_2": tp2,
        "exit_model": model,
    }


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
        "exit_model": signal.get("exit_model", {}),
        "position_size": position_size,
        "risk_gbp": round(risk_gbp, 2),
        "risk_pct": round(risk_pct, 2),
        "leverage": signal["leverage"],
        "market": signal["market"],
        "signals_triggered": signal["signals_triggered"],
    }
