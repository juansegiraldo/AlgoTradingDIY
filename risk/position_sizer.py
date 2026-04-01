"""
Position Sizer — calculates the correct position size for each trade
based on risk policies and available capital.

Core principle: Risk a fixed percentage of capital per trade (R1),
then derive position size from the distance to the stop-loss.

Formula:
    risk_amount = capital × max_risk_pct
    position_size = risk_amount / (entry - stop_loss)  [for longs]
    position_size = risk_amount / (stop_loss - entry)  [for shorts]

Then cap by:
    - max_position_size_pct of market allocation
    - max leverage for the market
"""

import logging
from typing import Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import get_risk_policies, get_settings
from data.database import calculate_current_equity, log_info

logger = logging.getLogger(__name__)


def _get_current_capital() -> float:
    """Get current total capital (initial + realized PnL)."""
    return calculate_current_equity()


def _get_market_capital(market: str) -> float:
    """Get allocated capital for a specific market."""
    capital = _get_current_capital()
    settings = get_settings()["markets"].get(market, {})
    alloc_pct = settings.get("capital_allocation_pct", 100)
    return capital * (alloc_pct / 100.0)


def _apply_size_scale(sizing: dict, size_scale: float) -> dict:
    """Scale a sizing result down for manual profiles such as /force."""
    scaled = dict(sizing)
    scale = max(0.0, min(float(size_scale), 1.0))

    for key in ("position_size", "position_size_value", "risk_amount", "risk_pct", "margin_required"):
        digits = 8 if key == "position_size" else 2
        scaled[key] = round(float(scaled.get(key, 0.0)) * scale, digits)

    scaled["reason"] = f"{scaled.get('reason', 'Position sized')} | scale={scale:.2f}"
    return scaled


def calculate_position(
    pair: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    market: str,
    leverage: int,
) -> dict:
    """
    Calculate position size based on risk parameters.

    Returns:
        {
            "position_size": float,       # units of base asset (e.g. BTC)
            "position_size_value": float,  # value in GBP
            "risk_amount": float,          # max loss in GBP
            "risk_pct": float,             # risk as % of total capital
            "leverage": int,
            "margin_required": float,      # GBP needed as margin
            "market_capital": float,       # allocated capital for market
            "approved": bool,
            "reason": str,
        }
    """
    policies = get_risk_policies().get("risk_policies", {})
    settings = get_settings()
    pm = settings.get("position_management", {})

    total_capital = _get_current_capital()
    market_capital = _get_market_capital(market)
    max_risk_pct = policies.get("max_loss_per_trade_pct", 5) / 100.0
    max_pos_pct = pm.get("max_position_size_pct", 20) / 100.0

    # 1. Calculate risk amount (max GBP we can lose on this trade)
    risk_amount = total_capital * max_risk_pct

    # 2. Calculate distance to stop-loss (as fraction of entry)
    if direction == "long":
        sl_distance = abs(entry_price - stop_loss)
    else:
        sl_distance = abs(stop_loss - entry_price)

    if sl_distance <= 0:
        return {
            "position_size": 0,
            "position_size_value": 0,
            "risk_amount": 0,
            "risk_pct": 0,
            "leverage": leverage,
            "margin_required": 0,
            "market_capital": market_capital,
            "approved": False,
            "reason": "Invalid SL distance (SL = entry price)",
        }

    sl_pct = sl_distance / entry_price

    # 3. Position value that risks exactly risk_amount at the SL
    #    If SL is 2% away: position_value = risk_amount / 0.02
    position_value_gbp = risk_amount / sl_pct

    # 4. Cap by max position size (% of market allocation)
    max_position_value = market_capital * max_pos_pct * leverage
    if position_value_gbp > max_position_value:
        position_value_gbp = max_position_value
        # Recalculate actual risk
        risk_amount = position_value_gbp * sl_pct

    # 5. Cap by available market capital (margin)
    margin_required = position_value_gbp / leverage
    if margin_required > market_capital:
        margin_required = market_capital
        position_value_gbp = margin_required * leverage
        risk_amount = position_value_gbp * sl_pct

    # 6. Convert to base asset units
    #    Approximate: assume entry_price is in USD, capital in GBP
    #    Use rough GBP/USD rate (will be refined when forex is connected)
    gbp_to_usd = 1.27  # approximate
    position_value_usd = position_value_gbp * gbp_to_usd
    position_size = position_value_usd / entry_price

    risk_pct = (risk_amount / total_capital) * 100

    result = {
        "position_size": round(position_size, 8),
        "position_size_value": round(position_value_gbp, 2),
        "risk_amount": round(risk_amount, 2),
        "risk_pct": round(risk_pct, 2),
        "leverage": leverage,
        "margin_required": round(margin_required, 2),
        "market_capital": round(market_capital, 2),
        "approved": True,
        "reason": "Position sized within risk limits",
    }

    log_info(
        "position_sizer",
        f"{pair} {direction.upper()}: size={result['position_size']:.6f} "
        f"value=GBP {result['position_size_value']:.2f} "
        f"risk=GBP {result['risk_amount']:.2f} ({result['risk_pct']:.1f}%) "
        f"margin=GBP {result['margin_required']:.2f} lev={leverage}x",
    )

    return result


def enrich_signal_with_sizing(signal: dict) -> dict:
    """
    Take a raw signal and add position sizing info.
    Returns the signal dict enriched with sizing fields.
    """
    sizing = calculate_position(
        pair=signal["pair"],
        direction=signal["direction"],
        entry_price=signal["entry_price"],
        stop_loss=signal["stop_loss"],
        market=signal.get("market", "crypto"),
        leverage=signal.get("leverage", 1),
    )

    size_scale = signal.get("size_scale")
    if size_scale is not None:
        sizing = _apply_size_scale(sizing, size_scale)

    signal["position_size"] = sizing["position_size"]
    signal["position_size_value"] = sizing["position_size_value"]
    signal["risk_gbp"] = sizing["risk_amount"]
    signal["risk_pct"] = sizing["risk_pct"]
    signal["margin_required"] = sizing["margin_required"]
    signal["sizing_approved"] = sizing["approved"]
    signal["sizing_reason"] = sizing["reason"]

    return signal
