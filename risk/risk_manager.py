"""
Risk Manager — validates every trade against risk policies R1-R8.

Each rule returns a (pass, reason) tuple. A trade is only approved
if ALL rules pass. If any rule fails, the trade is rejected with
a clear explanation.

Rules:
  R1: Max loss per trade (5% of capital)
  R2: Max daily loss (10%) — delegated to circuit_breaker
  R3: Max weekly loss (20%) — delegated to circuit_breaker
  R4: Max total drawdown (50%) — delegated to circuit_breaker
  R5: Max simultaneous positions (3)
  R6: No correlated positions
  R7: Stop-loss required
  R8: Forex no-trade hours
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import get_live_stage_profile, get_risk_policies, get_settings
from data.database import (
    calculate_current_equity,
    count_open_trades,
    get_open_pairs,
    log_info,
    log_warning,
)
from risk.circuit_breaker import is_circuit_breaker_active

logger = logging.getLogger(__name__)


def _get_policies() -> dict:
    return get_risk_policies().get("risk_policies", {})


def _get_current_capital() -> float:
    """Get current total capital (initial + realized PnL)."""
    capital = calculate_current_equity()
    settings = get_settings()
    if settings.get("mode", "paper") == "paper":
        return capital
    stage_cap = float(get_live_stage_profile().get("max_operable_capital_gbp", capital) or capital)
    return min(capital, stage_cap)


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------


def check_r1_max_loss_per_trade(signal: dict) -> tuple[bool, str]:
    """R1: Trade risk must not exceed max_loss_per_trade_pct of total capital."""
    policies = _get_policies()
    max_pct = policies.get("max_loss_per_trade_pct", 5)
    capital = _get_current_capital()
    max_loss = capital * (max_pct / 100.0)

    # Calculate potential loss for this trade
    entry = signal.get("entry_price", 0)
    sl = signal.get("stop_loss", 0)
    position_size_value = signal.get("position_size_value", 0)  # in GBP

    if not entry or not sl or entry <= 0 or sl <= 0:
        return False, "R1 FAIL: Invalid entry/SL prices"

    if signal["direction"] == "long":
        loss_pct_on_trade = abs(entry - sl) / entry
    else:
        loss_pct_on_trade = abs(sl - entry) / entry

    potential_loss = position_size_value * loss_pct_on_trade

    if potential_loss > max_loss + 0.005:
        return (
            False,
            f"R1 FAIL: Potential loss GBP {potential_loss:.2f} exceeds max "
            f"GBP {max_loss:.2f} ({max_pct}% of {capital:.2f})",
        )

    return True, f"R1 OK: Risk GBP {potential_loss:.2f} / {max_loss:.2f} max"


def check_r5_max_positions() -> tuple[bool, str]:
    """R5: Do not exceed max simultaneous positions."""
    policies = _get_policies()
    max_pos = policies.get("max_simultaneous_positions", 3)
    settings = get_settings()
    if settings.get("mode", "paper") != "paper":
        stage_max = get_live_stage_profile().get("max_simultaneous_positions")
        if stage_max is not None:
            max_pos = min(max_pos, int(stage_max))
    current = count_open_trades()

    if current >= max_pos:
        return (
            False,
            f"R5 FAIL: {current} positions open (max {max_pos}). "
            f"Close a position before opening a new one.",
        )
    return True, f"R5 OK: {current}/{max_pos} positions"


def check_r6_correlation(pair: str) -> tuple[bool, str]:
    """R6: No correlated positions open."""
    policies = _get_policies()
    correlated_groups = policies.get("correlated_pairs", [])
    open_pairs = get_open_pairs()

    if not open_pairs:
        return True, "R6 OK: No open positions"

    for group in correlated_groups:
        if pair in group:
            for open_pair in open_pairs:
                if open_pair in group and open_pair != pair:
                    return (
                        False,
                        f"R6 FAIL: {pair} is correlated with open position {open_pair}. "
                        f"Correlated group: {group}",
                    )

    return True, f"R6 OK: No correlated conflicts for {pair}"


def check_r7_stop_loss(signal: dict) -> tuple[bool, str]:
    """R7: Every trade MUST have a stop-loss."""
    policies = _get_policies()
    if not policies.get("require_stop_loss", True):
        return True, "R7 OK: SL requirement disabled"

    sl = signal.get("stop_loss")
    if sl is None or sl <= 0:
        return False, "R7 FAIL: No stop-loss defined. Every trade requires a SL."

    entry = signal.get("entry_price", 0)
    direction = signal.get("direction", "")

    # Validate SL is on the correct side
    if direction == "long" and sl >= entry:
        return False, f"R7 FAIL: SL ({sl}) must be below entry ({entry}) for LONG"
    if direction == "short" and sl <= entry:
        return False, f"R7 FAIL: SL ({sl}) must be above entry ({entry}) for SHORT"

    return True, "R7 OK: Stop-loss is set correctly"


def check_r8_forex_hours(signal: dict) -> tuple[bool, str]:
    """R8: No forex trading outside allowed hours."""
    if signal.get("market") != "forex":
        return True, "R8 OK: Not a forex trade"

    policies = _get_policies()
    no_trade = policies.get("forex_no_trade_hours", {})
    start_str = no_trade.get("start", "22:00")
    end_str = no_trade.get("end", "06:00")

    now = datetime.now(timezone.utc)
    current_time = now.strftime("%H:%M")

    # No-trade window wraps midnight: 22:00 -> 06:00
    if start_str > end_str:
        # e.g. 22:00-06:00: blocked if time >= 22:00 OR time < 06:00
        in_no_trade = current_time >= start_str or current_time < end_str
    else:
        in_no_trade = start_str <= current_time < end_str

    if in_no_trade:
        return (
            False,
            f"R8 FAIL: Forex trading blocked {start_str}-{end_str} GMT. "
            f"Current time: {current_time} GMT.",
        )

    return True, f"R8 OK: Forex allowed at {current_time} GMT"


def check_live_stage_constraints(signal: dict) -> tuple[bool, str]:
    """Additional live-only validation for stage caps and leverage."""
    settings = get_settings()
    if settings.get("mode", "paper") == "paper":
        return True, "LIVE_STAGE OK: Paper mode"

    profile = get_live_stage_profile()
    stage_name = settings.get("live_stage", "stage_10")
    leverage = float(signal.get("leverage") or 1)
    leverage_max = profile.get("leverage_max")
    if leverage_max is not None and leverage > float(leverage_max):
        return False, (
            f"LIVE_STAGE FAIL: leverage {leverage}x exceeds {leverage_max}x "
            f"for {stage_name}"
        )

    stage_cap = profile.get("max_operable_capital_gbp")
    position_margin = float(signal.get("margin_required") or 0.0)
    if stage_cap is not None and position_margin > float(stage_cap):
        return False, (
            f"LIVE_STAGE FAIL: margin GBP {position_margin:.2f} exceeds stage cap "
            f"GBP {float(stage_cap):.2f}"
        )

    return True, f"LIVE_STAGE OK: {stage_name}"


def check_circuit_breaker() -> tuple[bool, str]:
    """R2/R3/R4: Check if any circuit breaker is active."""
    cb = is_circuit_breaker_active()
    if cb:
        return (
            False,
            f"CIRCUIT BREAKER ACTIVE: {cb['rule_triggered']} — {cb.get('details', '')}. "
            f"Resumes after {cb.get('resume_after', 'manual reset')}",
        )
    return True, "R2/R3/R4 OK: No circuit breaker active"


# ---------------------------------------------------------------------------
# Full validation
# ---------------------------------------------------------------------------


def validate_trade(signal: dict) -> dict:
    """
    Validate a signal against ALL risk policies.

    Args:
        signal: dict with keys: pair, direction, entry_price, stop_loss,
                market, position_size_value (GBP), etc.

    Returns:
        {
            "approved": bool,
            "rules": {rule_id: {"passed": bool, "reason": str}},
            "rejection_reasons": [str],  # only if not approved
        }
    """
    rules = {}
    rejections = []

    # R1: Max loss per trade
    passed, reason = check_r1_max_loss_per_trade(signal)
    rules["R1"] = {"passed": passed, "reason": reason}
    if not passed:
        rejections.append(reason)

    # R2/R3/R4: Circuit breaker
    passed, reason = check_circuit_breaker()
    rules["R2_R3_R4"] = {"passed": passed, "reason": reason}
    if not passed:
        rejections.append(reason)

    # R5: Max positions
    passed, reason = check_r5_max_positions()
    rules["R5"] = {"passed": passed, "reason": reason}
    if not passed:
        rejections.append(reason)

    # R6: Correlation
    passed, reason = check_r6_correlation(signal.get("pair", ""))
    rules["R6"] = {"passed": passed, "reason": reason}
    if not passed:
        rejections.append(reason)

    # R7: Stop-loss
    passed, reason = check_r7_stop_loss(signal)
    rules["R7"] = {"passed": passed, "reason": reason}
    if not passed:
        rejections.append(reason)

    # R8: Forex hours
    passed, reason = check_r8_forex_hours(signal)
    rules["R8"] = {"passed": passed, "reason": reason}
    if not passed:
        rejections.append(reason)

    # Live stage constraints
    passed, reason = check_live_stage_constraints(signal)
    rules["LIVE_STAGE"] = {"passed": passed, "reason": reason}
    if not passed:
        rejections.append(reason)

    approved = len(rejections) == 0

    if approved:
        log_info("risk_manager", f"Trade APPROVED: {signal.get('pair')} {signal.get('direction')}")
    else:
        log_warning(
            "risk_manager",
            f"Trade REJECTED: {signal.get('pair')} {signal.get('direction')} — "
            + "; ".join(rejections),
        )

    return {
        "approved": approved,
        "rules": rules,
        "rejection_reasons": rejections,
    }
