"""
Circuit Breaker — freezes all operations when risk limits are violated.

Monitors:
  R2: Daily loss > 10% → freeze 24 hours
  R3: Weekly loss > 20% → freeze until next Monday
  R4: Total drawdown > 50% → full stop, manual reset required

Runs periodically (after each trade close and on scheduled checks).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import get_risk_policies, get_settings
from data.database import (
    calculate_current_equity,
    get_active_circuit_breaker,
    get_daily_pnl,
    get_weekly_pnl,
    log_info,
    log_warning,
    log_error,
    record_circuit_breaker,
)

logger = logging.getLogger(__name__)


def _get_policies() -> dict:
    return get_risk_policies().get("risk_policies", {})


def _get_initial_capital() -> float:
    return get_settings().get("initial_capital_gbp", 1000)


def _get_current_capital() -> float:
    """Get current total capital (initial + realized PnL)."""
    return calculate_current_equity()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Check individual rules
# ---------------------------------------------------------------------------


def check_r2_daily_loss() -> Optional[dict]:
    """R2: Daily loss exceeds max_loss_daily_pct."""
    policies = _get_policies()
    max_pct = policies.get("max_loss_daily_pct", 10)
    capital = _get_current_capital()
    max_loss = capital * (max_pct / 100.0)

    today = _now().strftime("%Y-%m-%d")
    daily_pnl = get_daily_pnl(today)

    if daily_pnl < -max_loss:
        cooldown = policies.get("circuit_breaker_cooldown_hours", 24)
        resume = (_now() + timedelta(hours=cooldown)).isoformat()
        return {
            "rule": "R2",
            "details": (
                f"Daily loss GBP {daily_pnl:.2f} exceeds max "
                f"GBP {-max_loss:.2f} ({max_pct}% of {capital:.2f}). "
                f"Frozen for {cooldown}h."
            ),
            "resume_after": resume,
        }
    return None


def check_r3_weekly_loss() -> Optional[dict]:
    """R3: Weekly loss exceeds max_loss_weekly_pct."""
    policies = _get_policies()
    max_pct = policies.get("max_loss_weekly_pct", 20)
    capital = _get_current_capital()
    max_loss = capital * (max_pct / 100.0)

    # Find Monday of current week
    now = _now()
    monday = now - timedelta(days=now.weekday())
    week_start = monday.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    weekly_pnl = get_weekly_pnl(week_start)

    if weekly_pnl < -max_loss:
        # Freeze until next Monday
        next_monday = monday + timedelta(weeks=1)
        resume = next_monday.replace(hour=6, minute=0).isoformat()
        return {
            "rule": "R3",
            "details": (
                f"Weekly loss GBP {weekly_pnl:.2f} exceeds max "
                f"GBP {-max_loss:.2f} ({max_pct}% of {capital:.2f}). "
                f"Frozen until Monday."
            ),
            "resume_after": resume,
        }
    return None


def check_r4_total_drawdown() -> Optional[dict]:
    """R4: Total drawdown exceeds max_drawdown_total_pct from initial capital."""
    policies = _get_policies()
    max_pct = policies.get("max_drawdown_total_pct", 50)
    initial = _get_initial_capital()
    current = _get_current_capital()

    drawdown_pct = ((initial - current) / initial) * 100

    if drawdown_pct >= max_pct:
        # Permanent stop — resume set far in the future to require manual reset
        return {
            "rule": "R4",
            "details": (
                f"Total drawdown {drawdown_pct:.1f}% (capital GBP {current:.2f} "
                f"from initial GBP {initial:.2f}). SYSTEM STOPPED. "
                f"Manual reset required."
            ),
            "resume_after": "9999-12-31T23:59:59",  # requires manual reset
        }
    return None


# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------


def run_checks() -> Optional[dict]:
    """
    Run all circuit breaker checks. If any triggers, record it and return details.
    Returns None if all clear.
    """
    # Check in order of severity
    for check_fn in [check_r4_total_drawdown, check_r3_weekly_loss, check_r2_daily_loss]:
        result = check_fn()
        if result:
            # Record to database
            record_circuit_breaker(
                rule_triggered=result["rule"],
                details=result["details"],
                resume_after=result["resume_after"],
            )
            log_error(
                "circuit_breaker",
                f"{result['rule']} TRIGGERED: {result['details']}",
            )
            logger.critical(f"CIRCUIT BREAKER {result['rule']}: {result['details']}")
            return result

    return None


def is_circuit_breaker_active() -> Optional[dict]:
    """Check if there's an active circuit breaker event."""
    return get_active_circuit_breaker()


def get_risk_status() -> dict:
    """
    Get current risk status without triggering any breakers.
    Used for dashboards and reports.
    """
    policies = _get_policies()
    initial = _get_initial_capital()
    current = _get_current_capital()
    now = _now()

    today = now.strftime("%Y-%m-%d")
    daily_pnl = get_daily_pnl(today)

    monday = now - timedelta(days=now.weekday())
    week_start = monday.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    weekly_pnl = get_weekly_pnl(week_start)

    drawdown_pct = ((initial - current) / initial) * 100 if initial > 0 else 0

    daily_max = current * (policies.get("max_loss_daily_pct", 10) / 100.0)
    weekly_max = current * (policies.get("max_loss_weekly_pct", 20) / 100.0)
    dd_max = policies.get("max_drawdown_total_pct", 50)

    cb = is_circuit_breaker_active()

    return {
        "capital_initial": initial,
        "capital_current": current,
        "drawdown_pct": round(drawdown_pct, 2),
        "drawdown_max_pct": dd_max,
        "daily_pnl": round(daily_pnl, 2),
        "daily_max_loss": round(-daily_max, 2),
        "daily_remaining": round(daily_max + daily_pnl, 2),
        "weekly_pnl": round(weekly_pnl, 2),
        "weekly_max_loss": round(-weekly_max, 2),
        "weekly_remaining": round(weekly_max + weekly_pnl, 2),
        "circuit_breaker_active": cb is not None,
        "circuit_breaker": cb,
    }
