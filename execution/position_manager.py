"""
Position management: trailing stops, partial take profits.

Checks all open trades on every poll cycle and:
- Fires stop-loss closes when price moves against the position.
- Fires partial TP1 closes (50%) when TP1 is reached.
- Fires full TP2 closes on the remaining size when TP2 is reached.
- Exposes manual-close helpers for the dashboard and Telegram /close command.

PnL formula
-----------
Quote: long  = (exit - entry) * size
       short = (entry - exit) * size
GBP  : quote PnL converted according to the active pair quote currency.

pnl_percent is always calculated against the *notional* value at entry
(entry_price * original_position_size).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.database import (
    calculate_current_equity,
    count_open_trades,
    get_open_trades,
    get_trade,
    add_trade_exit_fee,
    close_trade,
    get_connection,
    log_info,
    log_warning,
    log_error,
    save_equity_snapshot,
    update_tp1_state,
    set_system_flag,
)
from config.loader import get_settings
from execution.crypto_executor import close_position, fetch_position_size, fetch_price, quote_to_gbp
from execution.fees import estimate_fee_gbp, extract_order_fee_gbp
from notifications.telegram_bot import send_text_sync, send_close_notification_sync

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Trades that already had TP1 hit — avoids re-triggering a second partial close.
_tp1_hit_trades: set[int] = set()

# Tracks the remaining position size after a TP1 partial close.
# Key: trade_id, Value: remaining size in base currency.
_remaining_sizes: dict[int, float] = {}

# Whether we've already reconstructed TP1 state from DB this session.
_tp1_state_loaded: bool = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _calc_pnl(
    direction: str,
    entry_price: float,
    exit_price: float,
    position_size: float,
    leverage: float,
) -> tuple[float, float]:
    """
    Return (pnl_quote, pnl_percent).

    position_size is the actual quantity traded (e.g. 0.019 BTC).
    The position sizer already accounts for leverage when calculating
    position_size (via the max_position_value cap), so we do NOT
    multiply by leverage here — that would double-count it.

    pnl_percent is relative to the notional value at entry.
    """
    if direction == "long":
        pnl_quote = (exit_price - entry_price) * position_size
    else:
        pnl_quote = (entry_price - exit_price) * position_size

    notional = entry_price * position_size
    pnl_pct = (pnl_quote / notional * 100.0) if notional > 0 else 0.0
    return pnl_quote, pnl_pct


def _quote_to_gbp(pair: str, pnl_quote: float) -> float:
    return quote_to_gbp(pair, pnl_quote)


def _entry_fee_gbp(trade: dict) -> float:
    return float(trade.get("entry_fee_gbp") or 0.0)


def _estimate_exit_fee_gbp(pair: str, exit_price: float, close_size: float) -> float:
    return estimate_fee_gbp(pair, float(exit_price) * float(close_size))


def _extract_exit_fee_gbp(
    pair: str,
    close_order: Optional[dict],
    exit_price: float,
    close_size: float,
) -> dict:
    return extract_order_fee_gbp(
        pair,
        close_order or {},
        price=exit_price,
        fallback_notional_quote=float(exit_price) * float(close_size),
    )


def _net_pnl_pct(net_pnl_gbp: float, pair: str, entry_price: float, size: float) -> float:
    notional_gbp = _quote_to_gbp(pair, float(entry_price) * float(size))
    return (net_pnl_gbp / notional_gbp * 100.0) if notional_gbp > 0 else 0.0


def _update_trade_notes(trade_id: int, notes: str) -> None:
    """Overwrite the notes field of an existing trade row."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE trades SET notes = ? WHERE id = ?",
            (notes, trade_id),
        )


def _load_tp1_state_from_db() -> None:
    """Reconstruct TP1 partial-close state from DB on first run after restart."""
    global _tp1_state_loaded
    if _tp1_state_loaded:
        return
    _tp1_state_loaded = True

    try:
        trades = get_open_trades()
        for t in trades:
            if t.get("tp1_hit_at") and t.get("remaining_size") is not None:
                tid = t["id"]
                _tp1_hit_trades.add(tid)
                _remaining_sizes[tid] = float(t["remaining_size"])
                log_info(
                    "position_manager",
                    f"Restored TP1 state for trade #{tid}: "
                    f"remaining_size={t['remaining_size']}",
                )
    except Exception as exc:
        log_error("position_manager", f"Failed to restore TP1 state: {exc}")


def _snapshot_equity_after_close() -> None:
    """Save an equity snapshot after a trade close so reports stay accurate."""
    try:
        equity = calculate_current_equity()
        open_count = count_open_trades()
        save_equity_snapshot(total_capital=equity, open_positions=open_count)
    except Exception as exc:
        log_warning("position_manager", f"Could not save post-close equity snapshot: {exc}")


def _auto_pause_live(reason: str, trade: Optional[dict] = None) -> None:
    """Pause live trading after critical execution-management failures."""
    settings = get_settings()
    if settings.get("mode", "paper") == "paper":
        return
    if not settings.get("live_deployment", {}).get("auto_pause_on_live_failures", True):
        return

    set_system_flag("paused", "true")
    pair = trade.get("pair") if trade else "N/A"
    message = f"{reason} | pair={pair}"
    log_error("position_manager", f"AUTO-PAUSE: {message}")
    try:
        send_text_sync(
            f"\u26d4 <b>SISTEMA PAUSADO AUTOMATICAMENTE</b>\n\n"
            f"{message}\n\n"
            f"<i>Revisa cierres, ordenes abiertas y el exchange antes de reanudar.</i>"
        )
    except Exception:
        pass


def _get_effective_size(trade: dict) -> float:
    """
    Return the effective (remaining) position size for a trade.
    After a TP1 partial close the original position_size is halved;
    this function returns what is actually still open.
    """
    trade_id: int = trade["id"]
    if trade_id in _remaining_sizes:
        return _remaining_sizes[trade_id]
    return float(trade["position_size"])


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------


def _reconcile_exchange_positions(trades: list[dict]) -> list[dict]:
    """
    Check each open DB trade against the exchange. If a position no longer
    exists on the exchange (e.g. an exchange-side SL/TP order filled),
    close it in the DB so we don't try to manage a ghost position.

    Returns the list of trades that are still actually open.
    """
    still_open = []

    for trade in trades:
        trade_id: int = trade["id"]
        pair: str = trade["pair"]
        direction: str = trade["direction"]
        entry_price: float = float(trade["entry_price"])
        leverage: float = float(trade.get("leverage") or 1.0)

        try:
            exchange_size = fetch_position_size(pair)
        except Exception:
            # If we can't check, assume it's still open (safer)
            still_open.append(trade)
            continue

        if exchange_size > 0:
            still_open.append(trade)
            continue

        # Position gone from exchange — close in DB
        try:
            current_price = fetch_price(pair)
        except Exception:
            current_price = entry_price

        close_size = _get_effective_size(trade)
        pnl_usdt, _gross_pnl_pct = _calc_pnl(direction, entry_price, current_price, close_size, leverage)
        gross_pnl_gbp = _quote_to_gbp(pair, pnl_usdt)
        partial_gross_pnl = float(trade.get("realized_partial_pnl_gbp") or 0.0)
        entry_fee = _entry_fee_gbp(trade)
        prior_exit_fees = float(trade.get("exit_fee_gbp") or 0.0)
        exit_fee_gbp = _estimate_exit_fee_gbp(pair, current_price, close_size)
        total_gross_pnl_gbp = partial_gross_pnl + gross_pnl_gbp
        total_fees_gbp = entry_fee + prior_exit_fees + exit_fee_gbp
        net_pnl_gbp = total_gross_pnl_gbp - total_fees_gbp
        net_pnl_pct = _net_pnl_pct(
            net_pnl_gbp,
            pair,
            entry_price,
            float(trade.get("position_size") or close_size),
        )

        log_warning(
            "position_manager",
            f"Reconciliation: trade #{trade_id} {pair} no longer on exchange. "
            f"Closing in DB @ {current_price:,.4f} | Gross {total_gross_pnl_gbp:+.2f} GBP "
            f"| Fees {total_fees_gbp:.2f} GBP | Net {net_pnl_gbp:+.2f} GBP",
        )

        try:
            close_trade(
                trade_id=trade_id,
                exit_price=current_price,
                status="closed_exchange",
                pnl_absolute=round(net_pnl_gbp, 4),
                pnl_percent=round(net_pnl_pct, 4),
                pnl_gross_gbp=round(gross_pnl_gbp, 4),
                exit_fee_gbp=exit_fee_gbp,
                fee_details={
                    "entry_fee_gbp": entry_fee,
                    "prior_exit_fees_gbp": prior_exit_fees,
                    "exit_fee_gbp": exit_fee_gbp,
                    "exit_fee_source": "estimated_reconcile",
                },
            )
            _snapshot_equity_after_close()
            _tp1_hit_trades.discard(trade_id)
            _remaining_sizes.pop(trade_id, None)

            closed_trade = _build_closed_trade_dict(
                trade,
                current_price,
                "closed_exchange",
                net_pnl_gbp,
                net_pnl_pct,
                gross_pnl_gbp=total_gross_pnl_gbp,
                exit_fee_gbp=exit_fee_gbp,
            )
            _notify_close(closed_trade)
        except Exception as exc:
            log_error("position_manager", f"Reconciliation DB close failed #{trade_id}: {exc}")
            _auto_pause_live(f"Reconciliation DB close failed for trade #{trade_id}: {exc}", trade)
            still_open.append(trade)

    return still_open


def check_open_positions() -> None:
    """
    Evaluate every open trade against its stop-loss and take-profit levels.

    Called by APScheduler every 30 s (or whatever interval is configured in main.py).
    Errors on individual trades are caught and logged so a single bad symbol
    cannot break the whole loop.
    """
    _load_tp1_state_from_db()

    try:
        trades = get_open_trades()
    except Exception as exc:
        log_error("position_manager", f"Failed to fetch open trades: {exc}")
        logger.error("Failed to fetch open trades", exc_info=True)
        return

    if not trades:
        return

    # Reconcile: close DB trades whose exchange position is already gone
    trades = _reconcile_exchange_positions(trades)
    if not trades:
        return

    log_info("position_manager", f"Checking {len(trades)} open position(s)...")

    for trade in trades:
        trade_id: int = trade["id"]
        pair: str = trade["pair"]
        direction: str = trade["direction"]
        entry_price: float = float(trade["entry_price"])
        stop_loss: float = float(trade["stop_loss"])
        leverage: float = float(trade.get("leverage") or 1.0)
        original_size: float = float(trade["position_size"])

        take_profit_1: Optional[float] = (
            float(trade["take_profit_1"]) if trade.get("take_profit_1") else None
        )
        take_profit_2: Optional[float] = (
            float(trade["take_profit_2"]) if trade.get("take_profit_2") else None
        )

        # ------------------------------------------------------------------
        # Fetch live price — skip this trade if unavailable
        # ------------------------------------------------------------------
        try:
            current_price = fetch_price(pair)
        except Exception as exc:
            log_warning(
                "position_manager",
                f"Could not fetch price for {pair} (trade #{trade_id}): {exc}",
            )
            logger.warning("Price fetch failed for %s: %s", pair, exc)
            continue

        # ------------------------------------------------------------------
        # Determine trigger conditions
        # ------------------------------------------------------------------
        sl_hit = (
            current_price <= stop_loss
            if direction == "long"
            else current_price >= stop_loss
        )
        tp1_hit = (
            (current_price >= take_profit_1)
            if (take_profit_1 is not None and direction == "long")
            else (current_price <= take_profit_1)
            if (take_profit_1 is not None and direction == "short")
            else False
        )
        tp2_hit = (
            (current_price >= take_profit_2)
            if (take_profit_2 is not None and direction == "long")
            else (current_price <= take_profit_2)
            if (take_profit_2 is not None and direction == "short")
            else False
        )

        # ------------------------------------------------------------------
        # Priority: SL > TP2 > TP1
        # ------------------------------------------------------------------

        if sl_hit:
            _handle_stop_loss(trade, current_price, original_size, leverage)

        elif tp2_hit and trade_id in _tp1_hit_trades:
            # TP1 was already partially closed; close remaining size at TP2.
            remaining = _get_effective_size(trade)
            _handle_tp2(trade, current_price, remaining, leverage)

        elif tp2_hit and trade_id not in _tp1_hit_trades:
            # TP2 hit before TP1 was processed — close the full position.
            _handle_tp2(trade, current_price, original_size, leverage)

        elif tp1_hit and trade_id not in _tp1_hit_trades:
            _handle_tp1(trade, current_price, original_size, leverage)

        else:
            logger.debug(
                "Trade #%d %s %s — price %.4f | SL %.4f | TP1 %s | TP2 %s — no trigger",
                trade_id,
                pair,
                direction.upper(),
                current_price,
                stop_loss,
                take_profit_1,
                take_profit_2,
            )


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


def _handle_stop_loss(
    trade: dict,
    exit_price: float,
    close_size: float,
    leverage: float,
) -> None:
    """Close the full position at stop-loss."""
    trade_id: int = trade["id"]
    pair: str = trade["pair"]
    direction: str = trade["direction"]
    entry_price: float = float(trade["entry_price"])

    pnl_usdt, _gross_pnl_pct = _calc_pnl(direction, entry_price, exit_price, close_size, leverage)
    closing_gross_pnl_gbp = _quote_to_gbp(pair, pnl_usdt)
    partial_gross_pnl = float(trade.get("realized_partial_pnl_gbp") or 0.0)
    entry_fee = _entry_fee_gbp(trade)
    prior_exit_fees = float(trade.get("exit_fee_gbp") or 0.0)
    close_order = None
    exit_fee_info = {
        "fee_gbp": _estimate_exit_fee_gbp(pair, exit_price, close_size),
        "fee_source": "estimated",
    }

    log_info(
        "position_manager",
        f"STOP-LOSS hit: trade #{trade_id} {pair} {direction.upper()} "
        f"@ {exit_price:,.4f}",
    )

    try:
        close_order = close_position(pair, direction, close_size)
        exit_fee_info = _extract_exit_fee_gbp(pair, close_order, exit_price, close_size)
    except Exception as exc:
        log_error(
            "position_manager",
            f"Broker close failed for SL trade #{trade_id}: {exc}",
        )
        logger.error("Broker close_position failed for %s #%d: %s", pair, trade_id, exc)
        _auto_pause_live(f"Broker close failed for stop-loss trade #{trade_id}: {exc}", trade)
        # Fall through and still close in DB so the trade is not stuck.

    exit_fee_gbp = float(exit_fee_info["fee_gbp"])
    total_gross_pnl_gbp = partial_gross_pnl + closing_gross_pnl_gbp
    total_fees_gbp = entry_fee + prior_exit_fees + exit_fee_gbp
    net_pnl_gbp = total_gross_pnl_gbp - total_fees_gbp
    net_pnl_pct = _net_pnl_pct(
        net_pnl_gbp,
        pair,
        entry_price,
        float(trade.get("position_size") or close_size),
    )
    log_info(
        "position_manager",
        f"STOP-LOSS net: trade #{trade_id} gross {total_gross_pnl_gbp:+.2f} GBP "
        f"| fees {total_fees_gbp:.2f} GBP | net {net_pnl_gbp:+.2f} GBP",
    )

    try:
        close_trade(
            trade_id=trade_id,
            exit_price=exit_price,
            status="closed_sl",
            pnl_absolute=round(net_pnl_gbp, 4),
            pnl_percent=round(net_pnl_pct, 4),
            pnl_gross_gbp=round(closing_gross_pnl_gbp, 4),
            exit_fee_gbp=exit_fee_gbp,
            fee_details={
                "entry_fee_gbp": entry_fee,
                "prior_exit_fees_gbp": prior_exit_fees,
                "exit_fee_gbp": exit_fee_gbp,
                "exit_fee_source": exit_fee_info["fee_source"],
                "close_order_id": (close_order or {}).get("id"),
            },
        )
    except Exception as exc:
        log_error("position_manager", f"DB close failed for SL trade #{trade_id}: {exc}")
        _auto_pause_live(f"DB close failed for stop-loss trade #{trade_id}: {exc}", trade)
        return

    _snapshot_equity_after_close()

    # Clean up module state
    _tp1_hit_trades.discard(trade_id)
    _remaining_sizes.pop(trade_id, None)

    # Notification
    closed_trade = _build_closed_trade_dict(
        trade,
        exit_price,
        "closed_sl",
        net_pnl_gbp,
        net_pnl_pct,
        gross_pnl_gbp=total_gross_pnl_gbp,
        exit_fee_gbp=exit_fee_gbp,
    )
    _notify_close(closed_trade)


def _handle_tp1(
    trade: dict,
    exit_price: float,
    full_size: float,
    leverage: float,
) -> None:
    """Partially close 50% of the position at TP1."""
    trade_id: int = trade["id"]
    pair: str = trade["pair"]
    direction: str = trade["direction"]
    entry_price: float = float(trade["entry_price"])

    close_size = round(full_size * 0.5, 8)
    remaining_size = round(full_size - close_size, 8)

    pnl_usdt, _gross_pnl_pct = _calc_pnl(direction, entry_price, exit_price, close_size, leverage)
    gross_pnl_gbp = _quote_to_gbp(pair, pnl_usdt)
    entry_fee_alloc = _entry_fee_gbp(trade) * (close_size / full_size if full_size > 0 else 0.0)
    close_order = None
    exit_fee_info = {
        "fee_gbp": _estimate_exit_fee_gbp(pair, exit_price, close_size),
        "fee_source": "estimated",
    }
    net_pnl_gbp = gross_pnl_gbp - entry_fee_alloc - exit_fee_info["fee_gbp"]
    net_pnl_pct = _net_pnl_pct(net_pnl_gbp, pair, entry_price, close_size)

    log_info(
        "position_manager",
        f"TP1 hit: trade #{trade_id} {pair} {direction.upper()} "
        f"@ {exit_price:,.4f} | Partial close {close_size} ({50}%) "
        f"| Remaining {remaining_size}",
    )

    # Mark TP1 reached in memory and DB before broker call to prevent race conditions
    _tp1_hit_trades.add(trade_id)
    _remaining_sizes[trade_id] = remaining_size

    try:
        update_tp1_state(trade_id, remaining_size)
    except Exception as exc:
        log_error("position_manager", f"Could not persist TP1 state for trade #{trade_id}: {exc}")

    try:
        close_order = close_position(pair, direction, close_size)
        exit_fee_info = _extract_exit_fee_gbp(pair, close_order, exit_price, close_size)
        net_pnl_gbp = gross_pnl_gbp - entry_fee_alloc - float(exit_fee_info["fee_gbp"])
        net_pnl_pct = _net_pnl_pct(net_pnl_gbp, pair, entry_price, close_size)
    except Exception as exc:
        log_error(
            "position_manager",
            f"Broker partial close failed for TP1 trade #{trade_id}: {exc}",
        )
        logger.error("Broker partial close failed for %s #%d: %s", pair, trade_id, exc)
        _auto_pause_live(f"Broker partial close failed for TP1 trade #{trade_id}: {exc}", trade)

    exit_fee_gbp = float(exit_fee_info["fee_gbp"])
    try:
        add_trade_exit_fee(
            trade_id,
            exit_fee_gbp,
            realized_pnl_gbp=round(gross_pnl_gbp, 4),
            fee_details={
                "tp1_entry_fee_alloc_gbp": round(entry_fee_alloc, 6),
                "tp1_exit_fee_gbp": exit_fee_gbp,
                "tp1_exit_fee_source": exit_fee_info["fee_source"],
                "tp1_close_order_id": (close_order or {}).get("id"),
            },
        )
    except Exception as exc:
        log_error("position_manager", f"Could not persist TP1 fees for trade #{trade_id}: {exc}")

    # Update notes in DB to record the partial close
    existing_notes: str = trade.get("notes") or ""
    tp1_note = (
        f"[TP1] Partial close {close_size} @ {exit_price:,.4f} "
        f"| Gross {gross_pnl_gbp:+.2f} GBP | Fees {entry_fee_alloc + exit_fee_gbp:.2f} GBP "
        f"| Net {net_pnl_gbp:+.2f} GBP | Remaining {remaining_size}"
    )
    new_notes = f"{existing_notes} | {tp1_note}" if existing_notes else tp1_note

    try:
        _update_trade_notes(trade_id, new_notes)
    except Exception as exc:
        log_error("position_manager", f"Could not update notes for TP1 trade #{trade_id}: {exc}")

    # Telegram notification (trade is still open for TP2)
    partial_dict = _build_closed_trade_dict(
        trade,
        exit_price,
        "closed_tp1",
        net_pnl_gbp,
        net_pnl_pct,
        gross_pnl_gbp=gross_pnl_gbp,
        exit_fee_gbp=exit_fee_gbp,
    )
    partial_dict["_partial"] = True
    partial_dict["remaining_size"] = remaining_size

    try:
        send_text_sync(
            f"\U0001f3af <b>TP1 ALCANZADO (50% cerrado)</b>\n\n"
            f"\U0001f4c4 <b>{pair} {direction.upper()}</b>\n"
            f"\U0001f4b0 Entrada: {entry_price:,.4f}\n"
            f"\U0001f3c1 Salida TP1: {exit_price:,.4f}\n"
            f"\U0001f4ca Cerrado: {close_size} | Restante: {remaining_size}\n"
            f"Gross: {gross_pnl_gbp:+.2f} GBP\n"
            f"Fees: -{entry_fee_alloc + exit_fee_gbp:.2f} GBP\n"
            f"Net: {net_pnl_gbp:+.2f} GBP\n\n"
            f"<i>Posicion sigue abierta para TP2.</i>"
        )
    except Exception as exc:
        log_warning("position_manager", f"Telegram TP1 notification failed: {exc}")


def _handle_tp2(
    trade: dict,
    exit_price: float,
    close_size: float,
    leverage: float,
) -> None:
    """Close the remaining position at TP2."""
    trade_id: int = trade["id"]
    pair: str = trade["pair"]
    direction: str = trade["direction"]
    entry_price: float = float(trade["entry_price"])

    pnl_usdt, _gross_pnl_pct = _calc_pnl(direction, entry_price, exit_price, close_size, leverage)
    closing_gross_pnl_gbp = _quote_to_gbp(pair, pnl_usdt)
    partial_gross_pnl = float(trade.get("realized_partial_pnl_gbp") or 0.0)
    prior_exit_fees = float(trade.get("exit_fee_gbp") or 0.0)
    entry_fee = _entry_fee_gbp(trade)
    close_order = None
    exit_fee_info = {
        "fee_gbp": _estimate_exit_fee_gbp(pair, exit_price, close_size),
        "fee_source": "estimated",
    }

    log_info(
        "position_manager",
        f"TP2 hit: trade #{trade_id} {pair} {direction.upper()} "
        f"@ {exit_price:,.4f} | Close size {close_size}",
    )

    try:
        close_order = close_position(pair, direction, close_size)
        exit_fee_info = _extract_exit_fee_gbp(pair, close_order, exit_price, close_size)
    except Exception as exc:
        log_error(
            "position_manager",
            f"Broker close failed for TP2 trade #{trade_id}: {exc}",
        )
        logger.error("Broker TP2 close_position failed for %s #%d: %s", pair, trade_id, exc)
        _auto_pause_live(f"Broker close failed for TP2 trade #{trade_id}: {exc}", trade)

    exit_fee_gbp = float(exit_fee_info["fee_gbp"])
    total_gross_pnl_gbp = partial_gross_pnl + closing_gross_pnl_gbp
    total_fees_gbp = entry_fee + prior_exit_fees + exit_fee_gbp
    net_pnl_gbp = total_gross_pnl_gbp - total_fees_gbp
    net_pnl_pct = _net_pnl_pct(
        net_pnl_gbp,
        pair,
        entry_price,
        float(trade.get("position_size") or close_size),
    )
    log_info(
        "position_manager",
        f"TP2 net: trade #{trade_id} gross {total_gross_pnl_gbp:+.2f} GBP "
        f"| fees {total_fees_gbp:.2f} GBP | net {net_pnl_gbp:+.2f} GBP",
    )

    try:
        close_trade(
            trade_id=trade_id,
            exit_price=exit_price,
            status="closed_tp2",
            pnl_absolute=round(net_pnl_gbp, 4),
            pnl_percent=round(net_pnl_pct, 4),
            pnl_gross_gbp=round(closing_gross_pnl_gbp, 4),
            exit_fee_gbp=exit_fee_gbp,
            fee_details={
                "entry_fee_gbp": entry_fee,
                "prior_exit_fees_gbp": prior_exit_fees,
                "exit_fee_gbp": exit_fee_gbp,
                "exit_fee_source": exit_fee_info["fee_source"],
                "close_order_id": (close_order or {}).get("id"),
            },
        )
    except Exception as exc:
        log_error("position_manager", f"DB close failed for TP2 trade #{trade_id}: {exc}")
        _auto_pause_live(f"DB close failed for TP2 trade #{trade_id}: {exc}", trade)
        return

    _snapshot_equity_after_close()

    # Clean up module state
    _tp1_hit_trades.discard(trade_id)
    _remaining_sizes.pop(trade_id, None)

    closed_trade = _build_closed_trade_dict(
        trade,
        exit_price,
        "closed_tp2",
        net_pnl_gbp,
        net_pnl_pct,
        gross_pnl_gbp=total_gross_pnl_gbp,
        exit_fee_gbp=exit_fee_gbp,
    )
    _notify_close(closed_trade)


# ---------------------------------------------------------------------------
# Manual close helpers
# ---------------------------------------------------------------------------


def close_trade_manual(trade_id: int) -> dict:
    """
    Manually close an open trade at the current market price.

    Returns a result dict with keys:
        trade_id, pair, direction, entry_price, exit_price,
        position_size, leverage, pnl_usdt, pnl_gbp, pnl_pct,
        status, success, error (only on failure)
    """
    trade = get_trade(trade_id)
    if trade is None:
        msg = f"Trade #{trade_id} not found in database"
        log_warning("position_manager", msg)
        return {"trade_id": trade_id, "success": False, "error": msg}

    if trade.get("status") != "open":
        msg = f"Trade #{trade_id} is not open (status={trade['status']})"
        log_warning("position_manager", msg)
        return {"trade_id": trade_id, "success": False, "error": msg}

    pair: str = trade["pair"]
    direction: str = trade["direction"]
    entry_price: float = float(trade["entry_price"])
    leverage: float = float(trade.get("leverage") or 1.0)
    close_size: float = _get_effective_size(trade)

    # ------------------------------------------------------------------
    # Fetch price
    # ------------------------------------------------------------------
    try:
        exit_price = fetch_price(pair)
    except Exception as exc:
        msg = f"Could not fetch price for {pair}: {exc}"
        log_error("position_manager", f"Manual close #{trade_id}: {msg}")
        return {"trade_id": trade_id, "success": False, "error": msg}

    pnl_usdt, _gross_pnl_pct = _calc_pnl(direction, entry_price, exit_price, close_size, leverage)
    closing_gross_pnl_gbp = _quote_to_gbp(pair, pnl_usdt)
    partial_gross_pnl = float(trade.get("realized_partial_pnl_gbp") or 0.0)
    prior_exit_fees = float(trade.get("exit_fee_gbp") or 0.0)
    entry_fee = _entry_fee_gbp(trade)
    close_order = None
    exit_fee_info = {
        "fee_gbp": _estimate_exit_fee_gbp(pair, exit_price, close_size),
        "fee_source": "estimated",
    }

    # ------------------------------------------------------------------
    # Close on broker
    # ------------------------------------------------------------------
    try:
        close_order = close_position(pair, direction, close_size)
        exit_fee_info = _extract_exit_fee_gbp(pair, close_order, exit_price, close_size)
    except Exception as exc:
        log_error(
            "position_manager",
            f"Broker close failed for manual trade #{trade_id}: {exc}",
        )
        _auto_pause_live(f"Broker close failed for manual trade #{trade_id}: {exc}", trade)
        # Still attempt DB close so the position is not stuck.

    exit_fee_gbp = float(exit_fee_info["fee_gbp"])
    total_gross_pnl_gbp = partial_gross_pnl + closing_gross_pnl_gbp
    total_fees_gbp = entry_fee + prior_exit_fees + exit_fee_gbp
    net_pnl_gbp = total_gross_pnl_gbp - total_fees_gbp
    net_pnl_pct = _net_pnl_pct(
        net_pnl_gbp,
        pair,
        entry_price,
        float(trade.get("position_size") or close_size),
    )

    # ------------------------------------------------------------------
    # Close in DB
    # ------------------------------------------------------------------
    try:
        close_trade(
            trade_id=trade_id,
            exit_price=exit_price,
            status="closed_manual",
            pnl_absolute=round(net_pnl_gbp, 4),
            pnl_percent=round(net_pnl_pct, 4),
            pnl_gross_gbp=round(closing_gross_pnl_gbp, 4),
            exit_fee_gbp=exit_fee_gbp,
            fee_details={
                "entry_fee_gbp": entry_fee,
                "prior_exit_fees_gbp": prior_exit_fees,
                "exit_fee_gbp": exit_fee_gbp,
                "exit_fee_source": exit_fee_info["fee_source"],
                "close_order_id": (close_order or {}).get("id"),
            },
        )
    except Exception as exc:
        msg = f"DB close failed for manual trade #{trade_id}: {exc}"
        log_error("position_manager", msg)
        _auto_pause_live(msg, trade)
        return {"trade_id": trade_id, "success": False, "error": msg}

    _snapshot_equity_after_close()

    # Clean up module state
    _tp1_hit_trades.discard(trade_id)
    _remaining_sizes.pop(trade_id, None)

    log_info(
        "position_manager",
        f"Manual close: trade #{trade_id} {pair} {direction.upper()} "
        f"@ {exit_price:,.4f} | Gross {total_gross_pnl_gbp:+.2f} GBP "
        f"| Fees {total_fees_gbp:.2f} GBP | Net {net_pnl_gbp:+.2f} GBP",
    )

    result: dict = {
        "trade_id": trade_id,
        "pair": pair,
        "direction": direction,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "position_size": close_size,
        "leverage": leverage,
        "pnl_quote": round(pnl_usdt, 4),
        "pnl_usdt": round(pnl_usdt, 4),
        "pnl_gross_gbp": round(total_gross_pnl_gbp, 4),
        "entry_fee_gbp": round(entry_fee, 6),
        "exit_fee_gbp": round(prior_exit_fees + exit_fee_gbp, 6),
        "total_fees_gbp": round(total_fees_gbp, 6),
        "pnl_gbp": round(net_pnl_gbp, 4),
        "pnl_pct": round(net_pnl_pct, 4),
        "status": "closed_manual",
        "success": True,
    }

    # Notification
    closed_trade = _build_closed_trade_dict(
        trade,
        exit_price,
        "closed_manual",
        net_pnl_gbp,
        net_pnl_pct,
        gross_pnl_gbp=total_gross_pnl_gbp,
        exit_fee_gbp=exit_fee_gbp,
    )
    _notify_close(closed_trade)

    return result


def close_all_trades() -> list[dict]:
    """
    Manually close every open trade at current market prices.

    Returns a list of result dicts, one per trade (see ``close_trade_manual``).
    """
    try:
        trades = get_open_trades()
    except Exception as exc:
        log_error("position_manager", f"close_all_trades: failed to fetch open trades: {exc}")
        return []

    if not trades:
        log_info("position_manager", "close_all_trades: no open trades to close")
        return []

    log_info("position_manager", f"close_all_trades: closing {len(trades)} trade(s)...")
    results: list[dict] = []

    for trade in trades:
        result = close_trade_manual(trade["id"])
        results.append(result)

    successes = sum(1 for r in results if r.get("success"))
    log_info(
        "position_manager",
        f"close_all_trades: {successes}/{len(results)} trades closed successfully",
    )
    return results


# ---------------------------------------------------------------------------
# Unrealized PnL
# ---------------------------------------------------------------------------


def get_unrealized_pnl(trade: dict) -> dict:
    """
    Return unrealized PnL for an open trade at the current market price.

    Parameters
    ----------
    trade : dict
        A trade row as returned by ``get_trade()`` or ``get_open_trades()``.

    Returns
    -------
    dict with keys:
        current_price       : float
        unrealized_pnl_quote: float
        unrealized_pnl_gbp  : float
        unrealized_pnl_pct  : float  (% of notional)
    """
    pair: str = trade["pair"]
    direction: str = trade["direction"]
    entry_price: float = float(trade["entry_price"])
    leverage: float = float(trade.get("leverage") or 1.0)
    size: float = _get_effective_size(trade)

    try:
        current_price = fetch_price(pair)
    except Exception as exc:
        log_warning(
            "position_manager",
            f"get_unrealized_pnl: price fetch failed for {pair}: {exc}",
        )
        raise

    pnl_usdt, gross_pnl_pct = _calc_pnl(direction, entry_price, current_price, size, leverage)
    gross_pnl_gbp = _quote_to_gbp(pair, pnl_usdt)
    partial_gross_pnl = float(trade.get("realized_partial_pnl_gbp") or 0.0)
    entry_fee = _entry_fee_gbp(trade)
    exit_fees_paid = float(trade.get("exit_fee_gbp") or 0.0)
    estimated_exit_fee = _estimate_exit_fee_gbp(pair, current_price, size)
    total_estimated_fees = entry_fee + exit_fees_paid + estimated_exit_fee
    net_pnl_gbp = partial_gross_pnl + gross_pnl_gbp - total_estimated_fees
    net_pnl_pct = _net_pnl_pct(
        net_pnl_gbp,
        pair,
        entry_price,
        float(trade.get("position_size") or size),
    )

    return {
        "current_price": current_price,
        "unrealized_pnl_quote": round(pnl_usdt, 4),
        "unrealized_pnl_usdt": round(pnl_usdt, 4),
        "unrealized_pnl_gross_gbp": round(partial_gross_pnl + gross_pnl_gbp, 4),
        "entry_fee_gbp": round(entry_fee, 6),
        "exit_fees_paid_gbp": round(exit_fees_paid, 6),
        "estimated_exit_fee_gbp": round(estimated_exit_fee, 6),
        "estimated_total_fees_gbp": round(total_estimated_fees, 6),
        "unrealized_pnl_gbp": round(net_pnl_gbp, 4),
        "unrealized_pnl_pct": round(net_pnl_pct, 4),
        "unrealized_pnl_gross_pct": round(gross_pnl_pct, 4),
    }


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------


def _build_closed_trade_dict(
    trade: dict,
    exit_price: float,
    status: str,
    pnl_gbp: float,
    pnl_pct: float,
    *,
    gross_pnl_gbp: Optional[float] = None,
    exit_fee_gbp: float = 0.0,
) -> dict:
    """Build a minimal trade dict compatible with ``format_close_notification``."""
    entry_fee = _entry_fee_gbp(trade)
    prior_exit_fees = float(trade.get("exit_fee_gbp") or 0.0)
    total_fees = entry_fee + prior_exit_fees + float(exit_fee_gbp or 0.0)
    return {
        "id": trade["id"],
        "pair": trade["pair"],
        "direction": trade["direction"],
        "entry_price": float(trade["entry_price"]),
        "exit_price": exit_price,
        "position_size": trade["position_size"],
        "leverage": trade.get("leverage", 1),
        "status": status,
        "pnl_gross_gbp": round(
            float(gross_pnl_gbp if gross_pnl_gbp is not None else pnl_gbp),
            4,
        ),
        "entry_fee_gbp": round(entry_fee, 6),
        "exit_fee_gbp": round(prior_exit_fees + float(exit_fee_gbp or 0.0), 6),
        "total_fees_gbp": round(total_fees, 6),
        "pnl_absolute": round(pnl_gbp, 4),
        "pnl_percent": round(pnl_pct, 4),
    }


def _notify_close(closed_trade: dict) -> None:
    """Send a Telegram close notification, swallowing any delivery errors."""
    try:
        send_close_notification_sync(closed_trade)
    except Exception as exc:
        log_warning(
            "position_manager",
            f"Telegram close notification failed for trade #{closed_trade.get('id')}: {exc}",
        )


# ---------------------------------------------------------------------------
# Quick test: python -m execution.position_manager
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import logging as _logging
    from data.database import open_trade as _open_trade, init_db, get_connection as _gc

    _logging.basicConfig(level=_logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")

    init_db()
    print("=" * 60)
    print("  POSITION MANAGER - SELF TEST")
    print("=" * 60)

    # Seed a test trade
    from execution.crypto_executor import fetch_price as _fp
    pair = get_settings().get("markets", {}).get("crypto", {}).get("pairs", ["BTC/GBP"])[0]
    price = _fp(pair)
    tid = _open_trade(
        market="crypto",
        pair=pair,
        direction="long",
        entry_price=price,
        stop_loss=round(price * 0.98, 2),
        position_size=0.001,
        leverage=5.0,
        take_profit_1=round(price * 1.03, 2),
        take_profit_2=round(price * 1.06, 2),
        mode="paper",
        notes="position_manager self-test",
    )
    print(f"\n[1] Test trade opened: #{tid} @ {price:,.2f}")

    # Test unrealized PnL
    upnl = get_unrealized_pnl(get_trade(tid))
    print(
        f"\n[2] Unrealized PnL: {upnl['unrealized_pnl_gbp']:+.4f} GBP "
        f"({upnl['unrealized_pnl_pct']:+.4f}%) @ {upnl['current_price']:,.2f}"
    )

    # Test manual close
    result = close_trade_manual(tid)
    print(
        f"\n[3] Manual close result: success={result['success']} "
        f"PnL={result.get('pnl_gbp', 'N/A'):+.4f} GBP"
        if result.get("success")
        else f"\n[3] Manual close result: {result}"
    )

    # Cleanup
    with _gc() as conn:
        conn.execute("DELETE FROM trades WHERE notes = 'position_manager self-test'")
        conn.execute("DELETE FROM system_logs WHERE module = 'position_manager'")

    print("\n" + "=" * 60)
    print("  SELF TEST COMPLETE")
    print("=" * 60)
