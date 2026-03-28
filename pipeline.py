"""
Pipeline — connects the full trading flow:

  Scanner → Position Sizer → Risk Manager → Telegram Alert → [GO/SKIP] → Executor → Database

This is the central orchestration module. It is called by:
  - APScheduler (every 5 min scan)
  - Telegram GO callback (execute a confirmed signal)

Flow:
  1. scanner.scan_all() detects signals
  2. position_sizer.enrich_signal_with_sizing() calculates size
  3. risk_manager.validate_trade() checks R1-R8
  4. telegram_bot.send_signal_alert() sends alert with GO/SKIP
  5. User taps GO → execute_signal() runs
  6. Executor places orders (paper or live)
  7. Database records the trade
  8. Telegram confirms execution
"""

import json
import logging
import uuid
from typing import Optional

from config.loader import get_settings
from data.database import (
    log_info,
    log_warning,
    log_error,
    open_trade,
    save_equity_snapshot,
    count_open_trades,
)
from signals.scanner import scan_all
from signals.signal_generator import format_signal_for_telegram
from risk.position_sizer import enrich_signal_with_sizing
from risk.risk_manager import validate_trade
from risk.circuit_breaker import run_checks as run_circuit_breaker_checks

logger = logging.getLogger(__name__)

# Store approved signals waiting for user confirmation
_approved_signals: dict[str, dict] = {}  # signal_id -> enriched signal


# ---------------------------------------------------------------------------
# Step 1-4: Scan → Size → Validate → Alert
# ---------------------------------------------------------------------------


def run_scan_cycle() -> list[dict]:
    """
    Run one complete scan cycle:
      1. Scan all pairs/timeframes
      2. Size each signal
      3. Validate against risk policies
      4. Send approved signals to Telegram

    Returns list of approved signals (for testing/logging).
    """
    settings = get_settings()
    mode = settings.get("mode", "paper")

    if mode == "pause":
        log_info("pipeline", "System PAUSED — skipping scan cycle")
        return []

    # Check circuit breaker before scanning
    cb = run_circuit_breaker_checks()
    if cb:
        _notify_circuit_breaker(cb)
        return []

    # 1. Scan
    log_info("pipeline", "Scan cycle starting...")
    raw_signals = scan_all()

    if not raw_signals:
        logger.debug("No signals detected this cycle")
        return []

    approved = []

    for signal in raw_signals:
        # 2. Position sizing
        signal = enrich_signal_with_sizing(signal)
        if not signal.get("sizing_approved", False):
            log_warning(
                "pipeline",
                f"Sizing rejected for {signal['pair']}: {signal.get('sizing_reason')}",
            )
            continue

        # 3. Risk validation
        validation = validate_trade(signal)
        if not validation["approved"]:
            log_warning(
                "pipeline",
                f"Risk rejected {signal['pair']} {signal['direction']}: "
                + "; ".join(validation["rejection_reasons"]),
            )
            # Notify user of rejection
            _notify_rejection(signal, validation)
            continue

        # 4. Approved — store and send alert
        signal_id = f"sig_{uuid.uuid4().hex[:8]}"
        _approved_signals[signal_id] = signal

        log_info(
            "pipeline",
            f"Signal APPROVED: {signal['pair']} {signal['direction'].upper()} "
            f"@ {signal['entry_price']:,.2f} | Risk: GBP {signal['risk_gbp']:.2f}",
        )

        # Send to Telegram based on mode
        if mode == "full_auto":
            # Auto-execute without waiting for confirmation
            log_info("pipeline", f"FULL_AUTO: Executing {signal_id} immediately")
            execute_signal(signal)
        else:
            # semi_auto or paper: send alert and wait for GO/SKIP
            _send_alert(signal, signal_id)

        approved.append(signal)

    log_info("pipeline", f"Scan cycle complete: {len(approved)} approved signals")
    return approved


# ---------------------------------------------------------------------------
# Step 5-8: Execute (called when user taps GO)
# ---------------------------------------------------------------------------


def execute_signal(signal: dict) -> dict:
    """
    Execute a confirmed signal:
      1. Place orders via the appropriate executor
      2. Record trade in database
      3. Send confirmation to Telegram

    Returns the trade result dict.
    """
    pair = signal["pair"]
    market = signal.get("market", "crypto")
    mode = get_settings().get("mode", "paper")

    log_info("pipeline", f"Executing: {pair} {signal['direction'].upper()} ({mode})")

    try:
        # Execute via the appropriate broker
        result = _execute_on_broker(signal, market)

        if not result.get("success"):
            error_msg = result.get("error", "Unknown error")
            log_error("pipeline", f"Execution failed for {pair}: {error_msg}")
            _notify_error(signal, error_msg)
            return result

        # Record in database
        trade_id = open_trade(
            market=market,
            pair=pair,
            direction=signal["direction"],
            entry_price=result.get("entry_price", signal["entry_price"]),
            stop_loss=signal["stop_loss"],
            position_size=signal["position_size"],
            leverage=signal.get("leverage", 1),
            take_profit_1=signal.get("take_profit_1"),
            take_profit_2=signal.get("take_profit_2"),
            signals_triggered=signal.get("signals_triggered"),
            mode=mode,
            notes=f"Strength: {signal.get('strength', 'N/A')} | "
                  f"Timeframe: {signal.get('timeframe', 'N/A')} | "
                  f"Trend: {signal.get('trend', 'N/A')}",
        )

        result["trade_id"] = trade_id
        log_info("pipeline", f"Trade #{trade_id} recorded: {pair} {signal['direction']}")

        # Send confirmation to Telegram
        _notify_execution(signal, result, trade_id)

        return result

    except Exception as e:
        log_error("pipeline", f"Execution error for {pair}: {e}")
        logger.error(f"Execution error: {e}", exc_info=True)
        _notify_error(signal, str(e))
        return {"success": False, "error": str(e)}


def _execute_on_broker(signal: dict, market: str) -> dict:
    """Route execution to the correct broker."""
    if market == "crypto":
        from execution.binance_executor import execute_trade
        return execute_trade(
            pair=signal["pair"],
            direction=signal["direction"],
            amount=signal["position_size"],
            leverage=signal.get("leverage", 1),
            stop_loss_price=signal["stop_loss"],
            take_profit_1_price=signal.get("take_profit_1"),
            take_profit_2_price=signal.get("take_profit_2"),
        )
    elif market == "forex":
        # Will be connected in step 11
        log_warning("pipeline", "Forex execution not implemented yet")
        return {"success": False, "error": "Forex executor not implemented"}
    elif market == "etf":
        # Will be connected in step 12
        log_warning("pipeline", "ETF execution not implemented yet")
        return {"success": False, "error": "ETF executor not implemented"}
    else:
        return {"success": False, "error": f"Unknown market: {market}"}


# ---------------------------------------------------------------------------
# GO callback handler (registered with Telegram bot)
# ---------------------------------------------------------------------------


def on_go_callback(signal: dict) -> None:
    """Called when user taps GO on a Telegram alert."""
    log_info("pipeline", f"User confirmed GO: {signal.get('pair')} {signal.get('direction')}")
    execute_signal(signal)


# ---------------------------------------------------------------------------
# Telegram notifications (sync wrappers)
# ---------------------------------------------------------------------------


def _send_alert(signal: dict, signal_id: str) -> None:
    """Send a trade alert with GO/SKIP buttons."""
    try:
        from notifications.telegram_bot import send_signal_alert_sync
        telegram_signal = format_signal_for_telegram(
            signal,
            risk_gbp=signal.get("risk_gbp", 0),
            risk_pct=signal.get("risk_pct", 0),
            position_size=f"{signal.get('position_size', 0):.6f}",
        )
        send_signal_alert_sync(telegram_signal, signal_id)
    except Exception as e:
        logger.error(f"Failed to send Telegram alert: {e}")


def _notify_execution(signal: dict, result: dict, trade_id: int) -> None:
    """Send execution confirmation to Telegram."""
    try:
        from notifications.telegram_bot import send_execution_confirmation_sync
        trade_data = {
            "pair": signal["pair"],
            "direction": signal["direction"],
            "entry_price": result.get("entry_price", signal["entry_price"]),
            "stop_loss": signal["stop_loss"],
            "position_size": f"{signal.get('position_size', 0):.6f}",
            "leverage": signal.get("leverage", 1),
            "mode": get_settings().get("mode", "paper"),
        }
        send_execution_confirmation_sync(trade_data)
    except Exception as e:
        logger.error(f"Failed to send execution confirmation: {e}")


def _notify_rejection(signal: dict, validation: dict) -> None:
    """Notify user of a rejected trade."""
    try:
        from notifications.telegram_bot import send_text_sync
        reasons = "\n".join(f"  - {r}" for r in validation["rejection_reasons"])
        text = (
            f"\u26d4 <b>SIGNAL REJECTED</b>\n\n"
            f"{signal['pair']} {signal['direction'].upper()} "
            f"@ {signal['entry_price']:,.2f}\n\n"
            f"<b>Razones:</b>\n{reasons}"
        )
        send_text_sync(text)
    except Exception as e:
        logger.error(f"Failed to send rejection notification: {e}")


def _notify_circuit_breaker(cb: dict) -> None:
    """Notify user of a circuit breaker trigger."""
    try:
        from notifications.telegram_bot import send_text_sync
        text = (
            f"\U0001f6a8 <b>CIRCUIT BREAKER ACTIVADO</b>\n\n"
            f"\u26a0 Regla: <b>{cb['rule']}</b>\n"
            f"\U0001f4cb {cb['details']}\n\n"
            f"\U0001f552 Reanuda: {cb.get('resume_after', 'reset manual')}"
        )
        send_text_sync(text)
    except Exception as e:
        logger.error(f"Failed to send circuit breaker notification: {e}")


def _notify_error(signal: dict, error: str) -> None:
    """Notify user of an execution error."""
    try:
        from notifications.telegram_bot import send_text_sync
        text = (
            f"\u274c <b>ERROR DE EJECUCION</b>\n\n"
            f"{signal['pair']} {signal['direction'].upper()}\n"
            f"Error: {error}"
        )
        send_text_sync(text)
    except Exception as e:
        logger.error(f"Failed to send error notification: {e}")


# ---------------------------------------------------------------------------
# Quick test: python -m pipeline
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    from data.database import init_db
    init_db()

    print("=" * 60)
    print("  PIPELINE END-TO-END TEST")
    print("=" * 60)

    print("\n[1] Running scan cycle (real market data)...")
    approved = run_scan_cycle()
    print(f"    Approved signals: {len(approved)}")

    if approved:
        for s in approved:
            print(f"    - {s['pair']} ({s['timeframe']}) {s['direction'].upper()}")
            print(f"      Entry: {s['entry_price']:,.2f} | SL: {s['stop_loss']:,.2f}")
            print(f"      Size: {s['position_size']:.6f} | Risk: GBP {s['risk_gbp']:.2f}")
    else:
        print("    (No signals in current market conditions — this is normal)")

    # Simulate a forced signal to test execution
    print("\n[2] Simulated signal -> execution test...")
    from execution.binance_executor import fetch_price
    btc_price = fetch_price("BTC/USDT")

    test_signal = {
        "pair": "BTC/USDT",
        "timeframe": "1h",
        "market": "crypto",
        "direction": "long",
        "entry_price": btc_price,
        "stop_loss": round(btc_price * 0.98, 2),
        "take_profit_1": round(btc_price * 1.03, 2),
        "take_profit_2": round(btc_price * 1.06, 2),
        "leverage": 5,
        "signal_count": 3,
        "min_required": 3,
        "signals_triggered": {"rsi": True, "ema": True, "macd": False, "volume": True},
        "trend": "bullish",
        "strength": "moderate",
    }

    # Size it
    test_signal = enrich_signal_with_sizing(test_signal)
    print(f"    Position: {test_signal['position_size']:.6f} BTC")
    print(f"    Risk:     GBP {test_signal['risk_gbp']:.2f} ({test_signal['risk_pct']:.1f}%)")

    # Validate
    validation = validate_trade(test_signal)
    print(f"    Risk OK:  {validation['approved']}")

    if validation["approved"]:
        # Execute
        result = execute_signal(test_signal)
        print(f"    Executed: {result.get('success')}")
        print(f"    Trade ID: {result.get('trade_id')}")
        print(f"    Entry:    ${result.get('entry_price', 0):,.2f}")

        # Check DB
        from data.database import get_open_trades
        open_t = get_open_trades()
        print(f"    DB open:  {len(open_t)} trade(s)")
    else:
        print(f"    Rejected: {validation['rejection_reasons']}")

    # Clean up
    from data.database import get_connection
    with get_connection() as conn:
        conn.execute("DELETE FROM trades")
        conn.execute("DELETE FROM equity_snapshots")
        conn.execute("DELETE FROM system_logs")

    print("\n" + "=" * 60)
    print("  PIPELINE TEST COMPLETE")
    print("=" * 60)
