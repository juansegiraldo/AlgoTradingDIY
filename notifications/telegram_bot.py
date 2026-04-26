"""
Telegram bot for the 10X Trading System.

Features:
- Sends formatted trade alerts with GO/SKIP inline buttons
- Receives user confirmations and routes them to execution
- Sends system notifications (SL/TP hits, errors, reports)
- /start command to register chat_id
- /status command for quick portfolio summary
- /positions command to list open positions
- /pause and /resume commands for system control
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import get_secrets, get_settings
from data.database import (
    count_open_trades,
    get_latest_equity,
    get_open_trades,
    get_total_fees,
    get_total_pnl,
    get_win_rate,
    log_info,
    log_warning,
)
from execution.crypto_executor import (
    fetch_quote_balance,
    format_price,
    get_exchange_name,
    get_quote_currency,
    quote_to_gbp,
)
# NOTE: execution.position_manager is imported lazily inside cmd_close/cmd_closeall/callback_handler
# to avoid circular imports (position_manager imports telegram_bot for notifications)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_app: Optional[Application] = None
_chat_id: Optional[str] = None
_on_go_callback: Optional[Callable] = None  # Called when user taps GO
_pending_signals: dict = {}  # signal_id -> signal_data


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def _get_token() -> str:
    token = get_secrets().get("telegram", {}).get("bot_token", "")
    if not token:
        raise ValueError("Telegram bot_token not configured in config/secrets.yaml")
    return token


def _get_chat_id() -> Optional[str]:
    global _chat_id
    if _chat_id:
        return _chat_id
    cid = get_secrets().get("telegram", {}).get("chat_id", "")
    if cid:
        _chat_id = str(cid)
    return _chat_id


def _get_force_pairs() -> list[str]:
    pairs = get_settings().get("markets", {}).get("crypto", {}).get("pairs", [])
    return pairs or ["BTC/GBP"]


def _normalize_force_pair(raw_pair: Optional[str]) -> str:
    """Normalize /force input into a configured crypto pair."""
    pairs = _get_force_pairs()
    if not raw_pair:
        return pairs[0]

    normalized = raw_pair.strip().upper().replace("-", "/")
    if normalized in pairs:
        return normalized

    compact = normalized.replace("/", "")
    base_symbol = normalized.split("/", 1)[0]
    for pair in pairs:
        pair_base, pair_quote = pair.split("/", 1)
        if base_symbol == pair_base or compact == f"{pair_base}{pair_quote}":
            return pair

    supported = ", ".join(pair.split("/", 1)[0] for pair in pairs)
    raise ValueError(f"Activo no soportado: {raw_pair}. Usa uno de: {supported}")


def _notional_gbp(pair: str, amount: float) -> float:
    return quote_to_gbp(pair, amount)


def _position_value_gbp(pair: str, size: float, entry_price: float) -> float:
    return _notional_gbp(pair, size * entry_price)


def _active_exchange_label() -> str:
    try:
        return get_exchange_name().upper()
    except Exception:
        return str(get_settings().get("markets", {}).get("crypto", {}).get("exchange", "kraken")).upper()


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def format_signal_alert(signal: dict) -> str:
    direction_emoji = "\U0001f7e2" if signal["direction"] == "long" else "\U0001f534"
    exit_model = signal.get("exit_model") or {}
    exit_line = ""
    if exit_model.get("type") == "paper_atr":
        exit_line = (
            f"\U0001f4d0 <b>Salida paper ATR:</b> "
            f"SL {float(exit_model.get('stop_loss_pct', 0)):.2f}% | "
            f"TP1 {float(exit_model.get('take_profit_1_pct', 0)):.2f}% | "
            f"TP2 {float(exit_model.get('take_profit_2_pct', 0)):.2f}%\n"
        )
    return (
        f"{direction_emoji} <b>{signal['pair']} {signal['direction'].upper()}</b>\n"
        f"\n"
        f"\U0001f4b0 <b>Entrada:</b> {signal['entry_price']:,.2f}\n"
        f"\U0001f6d1 <b>Stop-Loss:</b> {signal['stop_loss']:,.2f}\n"
        f"\U0001f3af <b>TP1:</b> {float(signal['take_profit_1']):,.2f}\n"
        f"\U0001f3af <b>TP2:</b> {float(signal['take_profit_2']):,.2f}\n"
        f"{exit_line}"
        f"\n"
        f"\U0001f4ca <b>Tamano:</b> {float(signal['position_size']):.6f}\n"
        f"\U000026a0 <b>Riesgo:</b> GBP {float(signal.get('risk_gbp', 0)):.1f} ({float(signal.get('risk_pct', 0)):.1f}%)\n"
        f"\U0001f9fe <b>Fees est. ida/vuelta:</b> GBP {float(signal.get('estimated_round_trip_fee_gbp', 0)):.2f} "
        f"(break-even {float(signal.get('fee_breakeven_pct', 0)):.2f}%)\n"
        f"\U0001f4aa <b>Apalancamiento:</b> {signal.get('leverage', 1)}x\n"
        f"\U0001f4c8 <b>Mercado:</b> {signal.get('market', 'N/A')}\n"
        f"\n"
        f"\U0001f527 <b>Senales:</b> {_format_signals_triggered(signal.get('signals_triggered', {}))}\n"
        f"\n"
        f"<i>Responde GO para ejecutar, SKIP para ignorar</i>"
    )


def _format_signals_triggered(signals: dict) -> str:
    parts = []
    for name, active in signals.items():
        icon = "\u2705" if active else "\u274c"
        parts.append(f"{icon} {name.upper()}")
    return " | ".join(parts) if parts else "N/A"


def format_execution_confirmation(trade: dict) -> str:
    pair = trade["pair"]
    entry = float(trade["entry_price"])
    size = float(trade["position_size"])
    leverage = float(trade.get("leverage", 1))
    notional_quote = size * entry
    margin_gbp = _notional_gbp(pair, notional_quote / leverage)
    position_gbp = _notional_gbp(pair, notional_quote)
    entry_fee = float(trade.get("entry_fee_gbp") or 0.0)
    round_trip_fee = float(trade.get("estimated_round_trip_fee_gbp") or entry_fee * 2.0)
    fee_breakeven = (
        float(trade.get("fee_breakeven_pct") or (round_trip_fee / position_gbp * 100.0))
        if position_gbp > 0
        else 0.0
    )
    return (
        f"\u2705 <b>ORDEN EJECUTADA</b>\n"
        f"\n"
        f"\U0001f4c4 <b>{trade['pair']} {trade['direction'].upper()}</b>\n"
        f"\U0001f4b0 Entrada: {format_price(pair, entry)}\n"
        f"\U0001f6d1 SL: {format_price(pair, float(trade['stop_loss']))}\n"
        f"\U0001f4aa Leverage: {leverage:.0f}x\n"
        f"\n"
        f"\U0001f4b0 <b>Tu pones:</b> GBP {margin_gbp:,.2f}\n"
        f"\U0001f4ca <b>Posicion total:</b> GBP {position_gbp:,.2f}\n"
        f"\U0001f9fe <b>Fee entrada:</b> GBP {entry_fee:,.4f}\n"
        f"\U0001f9fe <b>Fees ida/vuelta est.:</b> GBP {round_trip_fee:,.4f} "
        f"(break-even {fee_breakeven:.2f}%)\n"
        f"\U0001f3f7 Modo: {trade.get('mode', 'paper').upper()}"
    )


def format_close_notification(trade: dict) -> str:
    pnl = trade.get("pnl_absolute", 0)
    gross = float(trade.get("pnl_gross_gbp") if trade.get("pnl_gross_gbp") is not None else pnl)
    fees = float(trade.get("total_fees_gbp") or 0.0)
    pnl_emoji = "\U0001f4b5" if pnl >= 0 else "\U0001f4b8"
    status_map = {
        "closed_tp1": "\U0001f3af TP1 alcanzado",
        "closed_tp2": "\U0001f3af\U0001f3af TP2 alcanzado",
        "closed_sl": "\U0001f6d1 Stop-Loss ejecutado",
        "closed_manual": "\U0001f91a Cierre manual",
    }
    status_text = status_map.get(trade.get("status", ""), trade.get("status", ""))

    return (
        f"{pnl_emoji} <b>POSICION CERRADA</b>\n"
        f"\n"
        f"\U0001f4c4 <b>{trade['pair']} {trade['direction'].upper()}</b>\n"
        f"\U0001f3f7 {status_text}\n"
        f"\U0001f4b0 Entrada: {trade['entry_price']:,.2f}\n"
        f"\U0001f3c1 Salida: {trade.get('exit_price', 'N/A')}\n"
        f"\n"
        f"Gross: {gross:+.2f} GBP\n"
        f"Fees: -{fees:.2f} GBP\n"
        f"{'Net: +' if pnl >= 0 else 'Net: '}{pnl:.2f} GBP ({trade.get('pnl_percent', 0):.1f}%)"
    )


def format_portfolio_status() -> str:
    settings = get_settings()
    equity = get_latest_equity()
    open_count = count_open_trades()
    realized_pnl = get_total_pnl()
    total_fees = get_total_fees()
    win_rate = get_win_rate()
    initial_capital = float(settings.get("initial_capital_gbp", 1000))
    equity_source = "internal"
    free_balance_gbp = None
    margin_snapshot_gbp = None
    if equity:
        current_capital = float(equity["total_capital"])
        equity_source = str(equity.get("source") or "internal")
        if equity.get("free_balance_gbp") is not None:
            free_balance_gbp = float(equity["free_balance_gbp"])
        if equity.get("margin_used_gbp") is not None:
            margin_snapshot_gbp = float(equity["margin_used_gbp"])
    else:
        current_capital = initial_capital + realized_pnl

    crypto_cfg = settings.get("markets", {}).get("crypto", {})
    crypto_pct = float(crypto_cfg.get("capital_allocation_pct", 50))
    crypto_alloc_gbp = current_capital * (crypto_pct / 100.0)
    other_alloc_gbp = max(current_capital - crypto_alloc_gbp, 0.0)
    other_pct = max(0.0, 100.0 - crypto_pct)
    exchange_label = _active_exchange_label()
    crypto_pairs = ", ".join(crypto_cfg.get("pairs", [])) or "N/A"
    quote_currency = get_quote_currency()

    wr_text = f"{win_rate:.1f}%" if win_rate is not None else "N/A"

    # Fetch balance from the configured exchange or DB-derived paper balance.
    try:
        quote_bal = fetch_quote_balance()
        quote = quote_bal.get("currency") or get_quote_currency()
        pair_for_quote = f"BTC/{quote}"
        exchange_free_gbp = quote_to_gbp(pair_for_quote, quote_bal["free"])
        exchange_used_gbp = quote_to_gbp(pair_for_quote, quote_bal["used"])
    except Exception:
        exchange_free_gbp = None
        exchange_used_gbp = None

    # Calculate unrealized PnL from open positions
    unrealized_pnl = 0.0
    total_margin = 0.0
    trades = get_open_trades()
    try:
        from execution.position_manager import get_unrealized_pnl
        for t in trades:
            # Margin in use
            entry = float(t["entry_price"])
            size = float(t["position_size"])
            leverage = float(t.get("leverage") or 1)
            total_margin += _notional_gbp(t["pair"], size * entry / leverage)
            # Unrealized PnL
            try:
                upnl = get_unrealized_pnl(t)
                unrealized_pnl += upnl["unrealized_pnl_gbp"]
            except Exception:
                pass
    except Exception:
        pass

    total_pnl = realized_pnl + unrealized_pnl

    # Use exchange balance as primary, fallback to internal calc
    if exchange_free_gbp is not None:
        available = exchange_free_gbp
        margin_display = exchange_used_gbp
    else:
        available = current_capital - total_margin
        margin_display = total_margin
    if free_balance_gbp is not None:
        available = free_balance_gbp
    if margin_snapshot_gbp is not None:
        margin_display = margin_snapshot_gbp

    # Build text
    text = (
        f"\U0001f4ca <b>MI PORTAFOLIO</b>\n"
        f"\n"
        f"\U0001f3e6 <b>Capital inicial:</b> GBP {initial_capital:,.2f}\n"
    )

    if abs(current_capital - initial_capital) >= 0.005 or open_count > 0:
        text += f"\U0001f4bc <b>Capital actual:</b> GBP {current_capital:,.2f}\n"
    text += (
        f"\U0001f4e1 <b>Fuente equity:</b> {equity_source.upper()}\n"
        f"\U0001f3e6 <b>Exchange crypto:</b> {exchange_label} Spot | Quote: {quote_currency}\n"
        f"\U0001f4cc <b>Pares:</b> {crypto_pairs}\n"
        f"\U0001f3f7 <b>Modo:</b> {settings.get('mode', 'paper').upper()} | "
        f"Stage: {settings.get('live_stage', 'stage_10')}\n"
    )

    # Límites dinámicos (solo en live mode)
    if settings.get("mode", "paper") != "paper":
        try:
            from config.loader import get_dynamic_limits, get_live_stage_profile
            dyn = get_dynamic_limits(current_capital)
            stage_profile = get_live_stage_profile()
            risk_pct = float(stage_profile.get("risk_per_trade_pct", 5.0))
            max_risk_gbp = current_capital * (risk_pct / 100.0)
            max_pos_size_gbp = (
                current_capital
                * (crypto_pct / 100.0)
                * (dyn["max_position_size_pct"] / 100.0)
            )
            stage_leverage = float(stage_profile.get("leverage_max", 1))
            leveraged_notional = max_pos_size_gbp * stage_leverage
            notional_label = "spot" if stage_leverage <= 1 else "con apalancamiento"
            text += (
                f"\n<b>\U0001f4d0 L\u00edmites ({dyn['tier_label']}):</b>\n"
                f"   \U0001f3af Riesgo/trade: {risk_pct:.0f}% = GBP {max_risk_gbp:.2f}\n"
                f"   \U0001f4cf Max posici\u00f3n: {dyn['max_position_size_pct']:.0f}% alloc"
                f" = GBP {max_pos_size_gbp:.2f}"
                f" (~GBP {leveraged_notional:.0f} {notional_label})\n"
                f"   \U0001f522 Posiciones m\u00e1x: {dyn['max_simultaneous_positions']}\n"
            )
        except Exception:
            pass

    text += (
        f"\n<b>\U0001f4b8 Asignacion:</b>\n"
        f"   \U0001f4b0 Cripto ({crypto_pct:.0f}%): GBP {crypto_alloc_gbp:,.2f}\n"
        f"   \U0001f4b5 Otros ({other_pct:.0f}%): GBP {other_alloc_gbp:,.2f}\n"
    )

    if open_count > 0:
        text += (
            f"\n<b>\U0001f4bc Posiciones ({open_count}):</b>\n"
            f"   \U0001f512 Margen en uso: GBP {margin_display:,.2f}\n"
            f"   \U0001f4b0 Disponible: GBP {available:,.2f}\n"
        )

    text += f"\n<b>\U0001f4b5 Rendimiento:</b>\n"

    if open_count > 0:
        unr_emoji = "\U0001f7e2" if unrealized_pnl >= 0 else "\U0001f534"
        sign = "+" if unrealized_pnl >= 0 else ""
        text += f"   {unr_emoji} Posiciones abiertas: {sign}{unrealized_pnl:,.2f} GBP\n"

    if realized_pnl != 0:
        real_emoji = "\U0001f7e2" if realized_pnl >= 0 else "\U0001f534"
        real_sign = "+" if realized_pnl >= 0 else ""
        text += f"   {real_emoji} Trades cerrados neto: {real_sign}{realized_pnl:,.2f} GBP\n"
    if total_fees:
        text += f"   \U0001f9fe Fees cerrados: -{total_fees:,.2f} GBP\n"

    total_emoji = "\U0001f7e2" if total_pnl >= 0 else "\U0001f534"
    total_sign = "+" if total_pnl >= 0 else ""
    total_pct = (total_pnl / initial_capital * 100) if initial_capital > 0 else 0
    text += f"   {total_emoji} <b>Total: {total_sign}{total_pnl:,.2f} GBP ({total_sign}{total_pct:.1f}%)</b>\n"

    if win_rate is not None:
        text += f"\n\U0001f3af Win Rate: {wr_text}\n"

    text += f"\n\U0001f552 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"

    return text


def format_open_positions() -> str:
    exchange_label = _active_exchange_label()
    trades = get_open_trades()
    if not trades:
        return f"\U0001f4ad <b>Sin posiciones abiertas en {exchange_label}</b>"

    lines = [f"\U0001f4cb <b>POSICIONES {exchange_label} ({len(trades)})</b>"]
    lines.append("<i>Fuente operativa: exchange activo + DB local reconciliada.</i>")
    total_invested = 0.0
    total_pnl = 0.0

    for i, t in enumerate(trades, 1):
        direction_emoji = "\U0001f7e2" if t["direction"] == "long" else "\U0001f534"
        entry = float(t["entry_price"])
        size = float(t["position_size"])
        leverage = float(t.get("leverage") or 1)
        sl = float(t["stop_loss"])
        tp1 = float(t["take_profit_1"]) if t.get("take_profit_1") else None

        # Cash/margin committed, expressed in GBP.
        margin_gbp = _position_value_gbp(t["pair"], size, entry) / leverage
        total_invested += margin_gbp

        # Unrealized PnL
        pnl_line = ""
        try:
            from execution.position_manager import get_unrealized_pnl
            upnl = get_unrealized_pnl(t)
            current_price = upnl["current_price"]
            gbp = upnl["unrealized_pnl_gbp"]
            total_pnl += gbp
            pnl_emoji = "\U0001f7e2" if gbp >= 0 else "\U0001f534"
            sign = "+" if gbp >= 0 else ""
            gross = upnl.get("unrealized_pnl_gross_gbp", gbp)
            fees = upnl.get("estimated_total_fees_gbp", 0.0)
            pnl_line = (
                f"   {pnl_emoji} <b>Net {sign}{gbp:.2f} GBP</b> | "
                f"Gross {gross:+.2f} | Fees -{fees:.2f} | "
                f"Ahora: {format_price(t['pair'], current_price)}\n"
            )
        except Exception:
            pass

        # Distance to SL and TP1
        if t["direction"] == "long":
            sl_dist = ((sl - entry) / entry) * 100
            tp1_dist = (((tp1 - entry) / entry) * 100) if tp1 else None
        else:
            sl_dist = ((entry - sl) / entry) * -100
            tp1_dist = (((entry - tp1) / entry) * 100) if tp1 else None

        tp_text = f" | \U0001f3af TP1: {tp1_dist:+.1f}%" if tp1_dist is not None else ""

        lines.append(
            f"\n{i}. {direction_emoji} <b>{t['pair']} {t['direction'].upper()}</b> ({leverage:.0f}x)\n"
            f"   \U0001f4b0 Invertido: GBP {margin_gbp:,.2f}\n"
            f"{pnl_line}"
            f"   \U0001f6d1 SL: {sl_dist:+.1f}%{tp_text}"
        )

    # Summary
    total_emoji = "\U0001f7e2" if total_pnl >= 0 else "\U0001f534"
    sign = "+" if total_pnl >= 0 else ""
    lines.append(
        f"\n{'=' * 24}\n"
        f"\U0001f4b0 Total invertido: GBP {total_invested:,.2f}\n"
        f"{total_emoji} PnL neto total: <b>{sign}{total_pnl:.2f} GBP</b>"
    )
    return "\n".join(lines)


def format_go_live_checklist() -> str:
    settings = get_settings()
    crypto_cfg = settings.get("markets", {}).get("crypto", {})
    exchange = _active_exchange_label()
    pairs = ", ".join(crypto_cfg.get("pairs", [])) or "N/A"
    mode = str(settings.get("mode", "paper")).upper()
    stage = str(settings.get("live_stage", "stage_10"))
    leverage_max = crypto_cfg.get("leverage_max", 1)
    allow_short = bool(crypto_cfg.get("allow_short", False))

    return (
        "\U0001f6e1 <b>GO-LIVE CHECKLIST</b>\n\n"
        f"Exchange activo: <b>{exchange}</b>\n"
        f"Modo actual: <b>{mode}</b>\n"
        f"Stage: <b>{stage}</b>\n"
        f"Pares: {pairs}\n"
        f"Leverage max crypto: {leverage_max}x\n"
        f"Shorts: {'SI' if allow_short else 'NO'}\n\n"
        "<b>Antes de aceptar GO:</b>\n"
        "1. GBP disponible en Kraken.\n"
        "2. Ejecutar /ready y exigir Estado LISTO.\n"
        "3. Confirmar circuit breaker OK.\n"
        "4. Confirmar 0 posiciones/ordenes inesperadas.\n"
        "5. Hacer paper smoke antes de semi_auto.\n"
        "6. Empezar real solo en stage_10.\n"
        "7. Confirmar cada trade con GO/SKIP.\n\n"
        "<b>No hacer:</b>\n"
        "- No full_auto.\n"
        "- No margin, futures, withdrawals ni shorts.\n"
        "- No subir etapa por PnL; solo por estabilidad.\n\n"
        "<i>Checklist completo: GO_LIVE_CHECKLIST.md y GO_LIVE_RUNBOOK.md</i>"
    )


def format_party_blurb() -> str:
    """Short plain-language explanation for social situations."""
    settings = get_settings()
    crypto_cfg = settings.get("markets", {}).get("crypto", {})
    exchange = _active_exchange_label()
    pairs = ", ".join(crypto_cfg.get("pairs", [])) or "BTC/GBP, ETH/GBP, SOL/GBP"
    mode = str(settings.get("mode", "paper")).upper()

    return (
        "\U0001f37b <b>Version fiesta del bot</b>\n\n"
        "Es un bot educativo de trading crypto. Mira precios reales en "
        f"<b>{exchange}</b> para <b>{pairs}</b>, busca senales con RSI, EMAs, MACD y volumen, "
        "y cuando ve algo interesante manda una alerta con <b>GO/SKIP</b>.\n\n"
        f"Ahora esta en <b>{mode}</b>: en paper simula operaciones; si algun dia pasa a real, "
        "solo opera spot GBP, sin margen, sin shorts, sin withdrawals y con confirmacion manual. "
        "Tambien calcula fees, PnL neto y usa stops/targets dinamicos por volatilidad."
    )


def _indicator_mark(indicator: dict) -> str:
    if not indicator.get("triggered"):
        return "-"
    signal = indicator.get("signal")
    if signal == "long":
        return "L"
    if signal == "short":
        return "S"
    return "ok"


def format_scan_report(details: list[dict], approved_signals: list[dict]) -> str:
    """Format a compact verbose scan report for Telegram."""
    approved = approved_signals or []
    lines = [
        "\U0001f50d <b>SCAN MANUAL</b>",
        f"Alertas GO/SKIP nuevas: <b>{len(approved)}</b>",
    ]
    if approved:
        lines.append("")
        for signal in approved:
            lines.append(
                f"\U0001f4e1 <b>{signal['pair']} {signal['timeframe']}</b> "
                f"{signal['direction'].upper()} | {signal.get('signal_count', '?')}/4"
            )

    lines.append("")
    lines.append("<b>Detalle bruto:</b>")

    for item in details:
        pair = item.get("pair", "?")
        tf = item.get("timeframe", "?")
        status = item.get("status", "unknown")
        reason = item.get("reason", "")
        if status == "error":
            lines.append(f"\n\u274c <b>{pair} {tf}</b> - {reason}")
            continue

        price = item.get("price")
        price_text = f"{float(price):,.2f}" if price else "N/A"
        trend = str(item.get("trend", "unknown")).upper()
        vol = item.get("volatility", {})
        atr = vol.get("atr_pct")
        atr_text = f"{float(atr):.2f}%" if atr is not None else "N/A"

        rsi = item.get("rsi", {})
        ema = item.get("ema", {})
        macd = item.get("macd", {})
        volume = item.get("volume", {})
        vol_value = volume.get("value") if isinstance(volume.get("value"), dict) else {}
        vol_ratio = vol_value.get("ratio")
        vol_text = f"{float(vol_ratio):.2f}x" if vol_ratio is not None else "N/A"

        icon = "\U0001f7e2" if status == "signal" else "\u26d4" if status == "blocked" else "\u26aa"
        lines.append(
            f"\n{icon} <b>{pair} {tf}</b> @ {price_text} | Trend {trend} | ATR {atr_text}"
        )
        lines.append(
            "   "
            f"RSI {_indicator_mark(rsi)} ({rsi.get('value', 'N/A')}) | "
            f"EMA {_indicator_mark(ema)} | "
            f"MACD {_indicator_mark(macd)} | "
            f"Vol {_indicator_mark(volume)} ({vol_text})"
        )
        lines.append(f"   Resultado: {reason}")

    lines.append("\n<i>L=long, S=short, -=sin trigger. Una alerta puede no enviarse si es duplicada o falla riesgo.</i>")
    text = "\n".join(lines)
    if len(text) > 3900:
        return text[:3850] + "\n\n<i>Reporte recortado por limite de Telegram.</i>"
    return text


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _chat_id
    _chat_id = str(update.effective_chat.id)
    log_info("telegram", f"Chat ID registered: {_chat_id}")
    await update.message.reply_text(
        "\U0001f680 <b>10X Trading Bot activado</b>\n\n"
        f"Chat ID: <code>{_chat_id}</code>\n\n"
        "Comandos disponibles:\n"
        "/status - Resumen del portfolio\n"
        "/positions - Posiciones abiertas\n"
        "/ready - Readiness Kraken antes de operar\n"
        "/golive - Checklist go-live Kraken\n"
        "/party - Explicacion rapida del bot\n"
        "/pause - Pausar el sistema\n"
        "/resume - Reanudar el sistema\n"
        "/help - Ayuda\n\n"
        "<i>Guarda este chat ID en config/secrets.yaml</i>",
        parse_mode="HTML",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(format_portfolio_status(), parse_mode="HTML")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(format_open_positions(), parse_mode="HTML")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from data.database import set_system_flag
    set_system_flag("paused", "true")
    log_warning("telegram", "System PAUSED by user command")
    await update.message.reply_text(
        "\u23f8 <b>Sistema PAUSADO</b>\n\n"
        "No se generaran ni ejecutaran senales.\n"
        "Usa /resume para reanudar.",
        parse_mode="HTML",
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from data.database import set_system_flag
    set_system_flag("paused", "false")
    log_info("telegram", "System RESUMED by user command")
    await update.message.reply_text(
        "\u25b6 <b>Sistema REANUDADO</b>\n\n"
        "El scanner vuelve a estar activo.",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "\U0001f4d6 <b>10X Trading Bot - Ayuda</b>\n\n"
        "/status - Resumen del portfolio\n"
        "/positions - Posiciones del exchange activo + DB local\n"
        "/ready - Readiness, saldo Kraken, posiciones y ordenes\n"
        "/golive - Checklist operativo antes de pasar a semi_auto\n"
        "/party - Blurb rapido para explicar el bot\n"
        "/force - Compra manual controlada (ej: /force btc, /force eth)\n"
        "/test - Trade de prueba con GO/SKIP\n"
        "/scan - Escanear mercados ahora\n"
        "/close - Ver posiciones con boton Cerrar por trade\n"
        "/close 5 - Cerrar trade #5 directamente\n"
        "/closeall - Cerrar TODAS las posiciones (pide confirmacion)\n"
        "/pause - Pausar todo el sistema\n"
        "/resume - Reanudar el sistema\n"
        "/help - Este mensaje\n\n"
        "<b>Alertas de trading:</b>\n"
        "Cuando se detecte una senal, recibiras un mensaje con botones "
        "<b>GO</b> (ejecutar) o <b>SKIP</b> (ignorar).\n\n"
        "<b>Proteccion automatica:</b>\n"
        "El bot revisa cada 30s si alguna posicion toco su SL o TP.\n"
        "Si tocas /close puedes cerrar manualmente en cualquier momento.\n\n"
        "<b>Ruta activa UK:</b>\n"
        "Kraken Spot GBP es la ruta crypto principal. Binance queda solo como codigo legado/fallback, no como ruta activa.\n\n"
        "<i>Modo actual: {mode}</i>".format(
            mode=get_settings().get("mode", "paper").upper()
        ),
        parse_mode="HTML",
    )


async def cmd_party(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(format_party_blurb(), parse_mode="HTML")


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force a test trade with real BTC price and GO/SKIP buttons."""
    await update.message.reply_text(
        "\U0001f9ea <b>Preparando trade de prueba...</b>",
        parse_mode="HTML",
    )

    try:
        from execution.crypto_executor import fetch_ohlcv, fetch_price
        from risk.position_sizer import enrich_signal_with_sizing
        from risk.risk_manager import validate_trade
        from signals.indicators import analyze, ohlcv_to_dataframe
        from signals.signal_generator import calculate_exit_levels
        from signals.signal_generator import format_signal_for_telegram
        from data.database import get_latest_equity, save_equity_snapshot
        import uuid

        # Ensure equity exists
        if not get_latest_equity():
            save_equity_snapshot(total_capital=get_settings().get("initial_capital_gbp", 1000))

        # Get live price
        pair = _normalize_force_pair("BTC")
        btc_price = fetch_price(pair)
        ohlcv = fetch_ohlcv(pair, "1h", limit=100)
        test_analysis = analyze(ohlcv_to_dataframe(ohlcv))

        # Build signal
        signal = {
            "pair": pair,
            "timeframe": "1h",
            "market": "crypto",
            "direction": "long",
            "entry_price": btc_price,
            "leverage": get_settings().get("markets", {}).get("crypto", {}).get("leverage_default", 1),
            "signal_count": 3,
            "min_required": 3,
            "signals_triggered": {"rsi": True, "ema": True, "macd": False, "volume": True},
            "trend": "bullish",
            "strength": "moderate (TEST)",
        }
        levels = calculate_exit_levels(pair, "1h", "crypto", "long", btc_price, test_analysis)
        signal.update({
            "stop_loss": levels["stop_loss"],
            "take_profit_1": levels["take_profit_1"],
            "take_profit_2": levels["take_profit_2"],
            "exit_model": levels["exit_model"],
        })

        # Size and validate
        signal = enrich_signal_with_sizing(signal)
        validation = validate_trade(signal)

        if not validation["approved"]:
            reasons = "\n".join(f"  - {r}" for r in validation["rejection_reasons"])
            await update.message.reply_text(
                f"\u26d4 <b>Trade rechazado por reglas de riesgo:</b>\n{reasons}",
                parse_mode="HTML",
            )
            return

        # Send alert with GO/SKIP
        signal_id = f"test_{uuid.uuid4().hex[:6]}"
        _pending_signals[signal_id] = signal

        tg_signal = format_signal_for_telegram(
            signal,
            risk_gbp=signal.get("risk_gbp", 0),
            risk_pct=signal.get("risk_pct", 0),
            position_size=signal.get("position_size", 0),
        )
        text = format_signal_alert(tg_signal)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("\u2705 GO", callback_data=f"go:{signal_id}"),
                InlineKeyboardButton("\u23ed SKIP", callback_data=f"skip:{signal_id}"),
            ]
        ])
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        await update.message.reply_text(
            f"\u274c <b>Error:</b> {e}",
            parse_mode="HTML",
        )


async def cmd_force(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show asset info, suggest sizes, let user pick with buttons."""
    direction = "long"
    pair_arg = None
    for arg in (context.args or []):
        low = arg.lower()
        if low in ("short", "sell", "s"):
            direction = "short"
            continue
        if low in ("long", "buy", "l"):
            direction = "long"
            continue
        if pair_arg is None:
            pair_arg = arg

    try:
        pair = _normalize_force_pair(pair_arg)
    except ValueError as exc:
        await update.message.reply_text(
            f"\u274c <b>Error:</b> {exc}",
            parse_mode="HTML",
        )
        return

    crypto_cfg = get_settings().get("markets", {}).get("crypto", {})
    if direction == "short" and crypto_cfg.get("allow_short", True) is False:
        await update.message.reply_text(
            "\u274c <b>Short no disponible:</b> Kraken Spot solo permite compras/ventas spot sin margen.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        f"\U0001f50e <b>Analizando {pair}...</b>",
        parse_mode="HTML",
    )

    try:
        from execution.crypto_executor import fetch_ohlcv, fetch_price
        from signals.indicators import ohlcv_to_dataframe, analyze
        from signals.signal_generator import calculate_exit_levels
        from risk.position_sizer import enrich_signal_with_sizing
        from data.database import calculate_current_equity
        import uuid

        price = fetch_price(pair)

        # Get indicators
        ohlcv = fetch_ohlcv(pair, "1h", limit=100)
        df = ohlcv_to_dataframe(ohlcv)
        analysis = analyze(df)
        rsi = analysis["rsi"]
        ema = analysis["ema"]
        macd = analysis["macd"]
        vol = analysis["volume"]
        trend = analysis["trend"]

        # 24h change
        ohlcv_24h = fetch_ohlcv(pair, "1d", limit=2)
        if ohlcv_24h and len(ohlcv_24h) >= 2:
            prev_close = ohlcv_24h[-2][4]
            change_24h = ((price - prev_close) / prev_close) * 100
        else:
            change_24h = 0

        # Indicator icons
        rsi_icon = "\u2705" if rsi["triggered"] else "\u26aa"
        ema_icon = "\u2705" if ema["triggered"] else "\u26aa"
        macd_icon = "\u2705" if macd["triggered"] else "\u26aa"
        vol_icon = "\u2705" if vol["triggered"] else "\u26aa"
        vol_ratio = vol.get("value", {}).get("ratio", 0)
        trend_icon = {
            "bullish": "\U0001f7e2 ALCISTA",
            "bearish": "\U0001f534 BAJISTA",
            "mixed": "\U0001f7e1 MIXTA",
        }.get(trend.get("trend"), "\u26aa DESCONOCIDA")
        change_icon = "\U0001f4c8" if change_24h >= 0 else "\U0001f4c9"

        levels = calculate_exit_levels(pair, "1h", "crypto", direction, price, analysis)
        sl = levels["stop_loss"]
        tp1 = levels["take_profit_1"]
        tp2 = levels["take_profit_2"]

        capital = calculate_current_equity()
        dir_emoji = "\U0001f7e2" if direction == "long" else "\U0001f534"

        base_signal = {
            "pair": pair,
            "timeframe": "manual",
            "market": "crypto",
            "direction": direction,
            "entry_price": price,
            "stop_loss": sl,
            "take_profit_1": tp1,
            "take_profit_2": tp2,
            "exit_model": levels["exit_model"],
            "signal_count": 4,
            "min_required": 3,
            "signals_triggered": {"manual": True},
            "trend": trend.get("trend", "unknown"),
            "strength": "manual",
        }

        max_leverage = max(1, int(crypto_cfg.get("leverage_max", crypto_cfg.get("leverage_default", 1)) or 1))
        default_leverage = max(1, min(int(crypto_cfg.get("leverage_default", 1) or 1), max_leverage))
        profiles = [
            {
                "label": "Spot" if max_leverage == 1 else "Seguro",
                "emoji": "\u2705",
                "leverage": default_leverage,
                "scale": 1.0 if max_leverage == 1 else 0.4,
                "profile": "spot" if max_leverage == 1 else "safe",
            }
        ]
        if max_leverage > default_leverage:
            profiles.append({
                "label": "Medio",
                "emoji": "\U0001f525",
                "leverage": max_leverage,
                "scale": 1.0,
                "profile": "medium",
            })

        sized_options = []
        for profile in profiles:
            sized = enrich_signal_with_sizing({
                **base_signal,
                "leverage": profile["leverage"],
                "size_scale": profile["scale"],
                "size_profile": profile["profile"],
            })
            if sized.get("sizing_approved", False):
                sized_options.append((profile, sized))

        if not sized_options:
            await update.message.reply_text(
                "\u26d4 <b>No se pudo calcular un tamano valido para este activo.</b>",
                parse_mode="HTML",
            )
            return

        option_lines = []
        keyboard_rows = []
        skip_id = None
        for profile, signal in sized_options:
            signal_id = f"force_{profile['profile']}_{uuid.uuid4().hex[:6]}"
            if skip_id is None:
                skip_id = signal_id
            _pending_signals[signal_id] = signal
            margin = signal["margin_required"]
            value = signal["position_size_value"]
            round_trip_fee = float(signal.get("estimated_round_trip_fee_gbp", 0.0))
            breakeven = float(signal.get("fee_breakeven_pct", 0.0))
            option_lines.append(
                f"{profile['emoji']} <b>{profile['label']}</b> ({profile['leverage']}x) - "
                f"Pones <b>GBP {margin:,.0f}</b> -> Posicion GBP {value:,.0f} | "
                f"Fees est. GBP {round_trip_fee:,.2f} | "
                f"Break-even {breakeven:.2f}% | "
                f"Pierdes max GBP {signal['risk_gbp']:,.0f}"
            )
            keyboard_rows.append([
                InlineKeyboardButton(
                    f"{profile['emoji']} {profile['label']} ({profile['leverage']}x) - GBP {margin:,.0f}",
                    callback_data=f"go:{signal_id}",
                )
            ])
        keyboard_rows.append([InlineKeyboardButton("\u23ed No comprar", callback_data=f"skip:{skip_id}")])
        exit_model = levels.get("exit_model", {})
        exit_desc = ""
        if exit_model.get("type") == "paper_atr":
            exit_desc = (
                f"\U0001f4d0 <b>Salida paper ATR:</b> "
                f"SL {float(exit_model.get('stop_loss_pct', 0)):.2f}% | "
                f"TP1 {float(exit_model.get('take_profit_1_pct', 0)):.2f}% | "
                f"TP2 {float(exit_model.get('take_profit_2_pct', 0)):.2f}%\n"
            )

        info_text = (
            f"{dir_emoji} <b>{pair} — {direction.upper()}</b>\n"
            f"\n"
            f"\U0001f4b0 <b>Precio:</b> {format_price(pair, price)}\n"
            f"{change_icon} <b>24h:</b> {change_24h:+.2f}%\n"
            f"\U0001f4c8 <b>Tendencia:</b> {trend_icon}\n"
            f"\n"
            f"<b>Indicadores (1h):</b>\n"
            f"  {rsi_icon} RSI: {rsi.get('value', 'N/A')}\n"
            f"  {ema_icon} EMA 9/21: {'cruce ' + (ema.get('signal') or 'sin cruce').upper() if ema.get('signal') else 'sin cruce'}\n"
            f"  {macd_icon} MACD: {'cruce ' + (macd.get('signal') or 'sin cruce').upper() if macd.get('signal') else 'sin cruce'}\n"
            f"  {vol_icon} Volumen: {vol_ratio}x vs promedio\n"
            f"\n"
            f"\U0001f6d1 <b>Stop-Loss:</b> {format_price(pair, sl)}\n"
            f"\U0001f3af <b>TP1:</b> {format_price(pair, tp1)} | <b>TP2:</b> {format_price(pair, tp2)}\n"
            f"{exit_desc}"
            f"\n"
            f"\U0001f4b7 <b>Capital actual:</b> GBP {capital:,.0f}\n"
            f"\n"
            f"<b>Elige tamano:</b>\n\n"
            + "\n".join(option_lines)
        )

        keyboard = InlineKeyboardMarkup(keyboard_rows)

        await update.message.reply_text(info_text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        await update.message.reply_text(
            f"\u274c <b>Error:</b> {e}",
            parse_mode="HTML",
        )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run a manual scan right now."""
    await update.message.reply_text(
        "\U0001f50d <b>Escaneando mercados...</b>",
        parse_mode="HTML",
    )

    try:
        from pipeline import run_scan_cycle
        from signals.scanner import scan_diagnostics
        details = scan_diagnostics()
        signals = run_scan_cycle()
        await update.message.reply_text(format_scan_report(details, signals), parse_mode="HTML")

    except Exception as e:
        await update.message.reply_text(
            f"\u274c <b>Error:</b> {e}",
            parse_mode="HTML",
        )


async def cmd_ready(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show operational readiness and current exchange balance snapshot."""
    try:
        from notifications.report_generator import generate_readiness_report
        import concurrent.futures
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            report = await loop.run_in_executor(pool, generate_readiness_report)
        await update.message.reply_text(report, parse_mode="HTML")
    except Exception as e:
        logger.error(f"cmd_ready error: {e}", exc_info=True)
        await update.message.reply_text(
            f"\u274c <b>Error:</b> {e}",
            parse_mode="HTML",
        )


async def cmd_golive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the short go-live checklist for Kraken Spot."""
    await update.message.reply_text(format_go_live_checklist(), parse_mode="HTML")


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Close a specific trade by ID, or show open positions with Cerrar buttons."""
    from execution.position_manager import close_trade_manual, get_unrealized_pnl

    args = context.args or []

    if args:
        # Close a specific trade by ID
        try:
            trade_id = int(args[0])
        except ValueError:
            await update.message.reply_text(
                "\u274c <b>ID invalido.</b> Usa <code>/close 5</code> con el numero de trade.",
                parse_mode="HTML",
            )
            return

        await update.message.reply_text(
            f"\u23f3 <b>Cerrando trade #{trade_id}...</b>",
            parse_mode="HTML",
        )
        try:
            result = close_trade_manual(trade_id)
            if not result or not result.get("success"):
                error = result.get("error", "Trade no encontrado o ya cerrado") if result else "Error desconocido"
                await update.message.reply_text(
                    f"\u274c <b>#{trade_id}:</b> {error}",
                    parse_mode="HTML",
                )
                return

            pnl = result.get("pnl_gbp", 0)
            pnl_pct = result.get("pnl_pct", 0)
            gross = result.get("pnl_gross_gbp", pnl)
            fees = result.get("total_fees_gbp", 0.0)
            pnl_emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"
            sign = "+" if pnl >= 0 else ""
            direction_emoji = "\U0001f7e2" if result.get("direction") == "long" else "\U0001f534"

            await update.message.reply_text(
                f"{pnl_emoji} <b>TRADE CERRADO</b>\n"
                f"\n"
                f"{direction_emoji} <b>{result['pair']} {result.get('direction', '').upper()}</b>\n"
                f"\U0001f4b0 Entrada: {result.get('entry_price', 0):,.2f}\n"
                f"\U0001f3c1 Salida: {result.get('exit_price', 0):,.2f}\n"
                f"\n"
                f"Gross: {gross:+.2f} GBP\n"
                f"Fees: -{fees:.2f} GBP\n"
                f"Net: <b>{sign}{pnl:.2f} GBP ({sign}{pnl_pct:.1f}%)</b>",
                parse_mode="HTML",
            )
            log_info("telegram", f"Trade #{trade_id} closed manually via /close command")
        except Exception as e:
            logger.error(f"Error closing trade #{trade_id}: {e}")
            await update.message.reply_text(
                f"\u274c <b>Error al cerrar trade #{trade_id}:</b> {e}",
                parse_mode="HTML",
            )
        return

    # No ID given — show all open positions with Cerrar buttons
    trades = get_open_trades()
    if not trades:
        await update.message.reply_text(
            "\U0001f4ad <b>Sin posiciones abiertas.</b>",
            parse_mode="HTML",
        )
        return

    exchange_label = _active_exchange_label()
    lines = [f"\U0001f4cb <b>POSICIONES {exchange_label} ABIERTAS</b>\n"]
    keyboard_rows = []

    for i, t in enumerate(trades, start=1):
        trade_id = t["id"]
        direction_emoji = "\U0001f7e2" if t["direction"] == "long" else "\U0001f534"

        try:
            upnl = get_unrealized_pnl(t)
            upnl_gbp = upnl["unrealized_pnl_gbp"]
            upnl_emoji = "\U0001f7e2" if upnl_gbp >= 0 else "\U0001f534"
            sign = "+" if upnl_gbp >= 0 else ""
            upnl_text = f" | PnL: {upnl_emoji} {sign}{upnl_gbp:.2f} GBP"
        except Exception:
            upnl_text = ""

        lines.append(
            f"{i}. {direction_emoji} <b>{t['pair']}</b> {t['direction'].upper()} "
            f"@ {t['entry_price']:,.2f} | SL: {t['stop_loss']:,.2f} | "
            f"Lev: {t.get('leverage', 1)}x{upnl_text}"
        )
        keyboard_rows.append(
            [InlineKeyboardButton(
                f"\U0001f6aa Cerrar #{trade_id} {t['pair']}",
                callback_data=f"close:{trade_id}",
            )]
        )

    keyboard_rows.append(
        [InlineKeyboardButton("\u26d4 Cerrar TODAS", callback_data="closeall:confirm_pre")]
    )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard_rows),
    )


async def cmd_closeall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask for confirmation before closing all open trades."""
    from execution.position_manager import close_all_trades  # noqa: F811
    trades = get_open_trades()
    if not trades:
        await update.message.reply_text(
            "\U0001f4ad <b>Sin posiciones abiertas.</b>",
            parse_mode="HTML",
        )
        return

    count = len(trades)
    pairs = ", ".join(t["pair"] for t in trades)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\u2705 Si, cerrar todo", callback_data="closeall:confirm"),
            InlineKeyboardButton("\u274c No, cancelar", callback_data="closeall:cancel"),
        ]
    ])
    await update.message.reply_text(
        f"\u26a0 <b>Confirmar cierre total</b>\n"
        f"\n"
        f"Vas a cerrar <b>{count} posicion(es)</b>:\n"
        f"<code>{pairs}</code>\n"
        f"\n"
        f"\u26a0 Esta accion no se puede deshacer.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Callback (GO / SKIP / CLOSE buttons)
# ---------------------------------------------------------------------------


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data  # e.g. "go:signal_123", "close:42", "closeall:confirm"
    action, payload = data.split(":", 1)

    # Lazy imports to avoid circular dependency with position_manager
    from execution.position_manager import close_trade_manual, close_all_trades

    # ------------------------------------------------------------------
    # close:{trade_id} — close a single trade from the /close keyboard
    # ------------------------------------------------------------------
    if action == "close":
        try:
            trade_id = int(payload)
        except ValueError:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                "\u274c ID de trade invalido.", parse_mode="HTML"
            )
            return

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"\u23f3 <b>Cerrando trade #{trade_id}...</b>",
            parse_mode="HTML",
        )
        try:
            result = close_trade_manual(trade_id)
            if not result or not result.get("success"):
                error = result.get("error", "Trade no encontrado o ya cerrado") if result else "Error desconocido"
                await query.message.reply_text(
                    f"\u274c <b>#{trade_id}:</b> {error}",
                    parse_mode="HTML",
                )
                return

            pnl = result.get("pnl_gbp", 0)
            pnl_pct = result.get("pnl_pct", 0)
            gross = result.get("pnl_gross_gbp", pnl)
            fees = result.get("total_fees_gbp", 0.0)
            pnl_emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"
            sign = "+" if pnl >= 0 else ""
            direction_emoji = "\U0001f7e2" if result.get("direction") == "long" else "\U0001f534"

            await query.message.reply_text(
                f"{pnl_emoji} <b>TRADE CERRADO</b>\n"
                f"\n"
                f"{direction_emoji} <b>{result['pair']} {result.get('direction', '').upper()}</b>\n"
                f"\U0001f4b0 Entrada: {result.get('entry_price', 0):,.2f}\n"
                f"\U0001f3c1 Salida: {result.get('exit_price', 0):,.2f}\n"
                f"\n"
                f"Gross: {gross:+.2f} GBP\n"
                f"Fees: -{fees:.2f} GBP\n"
                f"Net: <b>{sign}{pnl:.2f} GBP ({sign}{pnl_pct:.1f}%)</b>",
                parse_mode="HTML",
            )
            log_info("telegram", f"Trade #{trade_id} closed manually via inline button")
        except Exception as e:
            logger.error(f"Error closing trade #{trade_id} via callback: {e}")
            await query.message.reply_text(
                f"\u274c <b>Error al cerrar trade #{trade_id}:</b> {e}",
                parse_mode="HTML",
            )
        return

    # ------------------------------------------------------------------
    # closeall:confirm_pre — from /close keyboard, redirect to confirmation
    # ------------------------------------------------------------------
    if action == "closeall" and payload == "confirm_pre":
        trades = get_open_trades()
        count = len(trades)
        pairs = ", ".join(t["pair"] for t in trades) if trades else "ninguna"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("\u2705 Si, cerrar todo", callback_data="closeall:confirm"),
                InlineKeyboardButton("\u274c No, cancelar", callback_data="closeall:cancel"),
            ]
        ])
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"\u26a0 <b>Confirmar cierre total</b>\n"
            f"\n"
            f"Vas a cerrar <b>{count} posicion(es)</b>:\n"
            f"<code>{pairs}</code>\n"
            f"\n"
            f"\u26a0 Esta accion no se puede deshacer.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    # ------------------------------------------------------------------
    # closeall:confirm / closeall:cancel
    # ------------------------------------------------------------------
    if action == "closeall":
        if payload == "cancel":
            await query.edit_message_text(
                text=query.message.text_html + "\n\n\u274c <b>Cancelado.</b>",
                parse_mode="HTML",
            )
            return

        if payload == "confirm":
            await query.edit_message_text(
                text=query.message.text_html + "\n\n\u23f3 <b>Cerrando todas las posiciones...</b>",
                parse_mode="HTML",
            )
            try:
                results = close_all_trades()
                if not results:
                    await query.message.reply_text(
                        "\U0001f4ad <b>Sin posiciones abiertas para cerrar.</b>",
                        parse_mode="HTML",
                    )
                    return

                total_pnl = sum(r.get("pnl_gbp", 0) for r in results if r.get("success"))
                total_fees = sum(r.get("total_fees_gbp", 0) for r in results if r.get("success"))
                total_emoji = "\U0001f7e2" if total_pnl >= 0 else "\U0001f534"
                sign = "+" if total_pnl >= 0 else ""

                lines = [f"\u26d4 <b>CIERRE TOTAL — {len(results)} posicion(es)</b>\n"]
                for r in results:
                    if not r.get("success"):
                        lines.append(f"\u274c #{r.get('trade_id')} {r.get('pair', '?')} - Error: {r.get('error', '?')}")
                        continue
                    pnl = r.get("pnl_gbp", 0)
                    pnl_pct = r.get("pnl_pct", 0)
                    fees = r.get("total_fees_gbp", 0.0)
                    pnl_emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"
                    r_sign = "+" if pnl >= 0 else ""
                    direction_emoji = "\U0001f7e2" if r.get("direction") == "long" else "\U0001f534"
                    lines.append(
                        f"{direction_emoji} <b>{r['pair']}</b> {r.get('direction', '').upper()} "
                        f"@ {r.get('entry_price', 0):,.2f} -> {r.get('exit_price', 0):,.2f} | "
                        f"Fees -{fees:.2f} | Net {pnl_emoji} {r_sign}{pnl:.2f} GBP ({r_sign}{pnl_pct:.1f}%)"
                    )

                lines.append(
                    f"\n\U0001f9fe Fees total: -{total_fees:.2f} GBP\n"
                    f"{total_emoji} <b>PnL neto total: {sign}{total_pnl:.2f} GBP</b>"
                )
                await query.message.reply_text(
                    "\n".join(lines),
                    parse_mode="HTML",
                )
                log_info("telegram", f"All trades closed manually via Telegram. Count: {len(results)}")
            except Exception as e:
                logger.error(f"Error closing all trades via callback: {e}")
                await query.message.reply_text(
                    f"\u274c <b>Error al cerrar todas las posiciones:</b> {e}",
                    parse_mode="HTML",
                )
        return

    # ------------------------------------------------------------------
    # go / skip — existing signal confirmation logic
    # ------------------------------------------------------------------
    signal_id = payload
    signal = _pending_signals.pop(signal_id, None)
    if signal is None:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "\u26a0 Senal expirada o ya procesada.", parse_mode="HTML"
        )
        return

    if action == "go":
        log_info("telegram", f"User confirmed GO for {signal_id}")
        await query.edit_message_text(
            text=query.message.text_html + "\n\n\u2705 <b>CONFIRMADO - Ejecutando...</b>",
            parse_mode="HTML",
        )
        if _on_go_callback:
            try:
                _on_go_callback(signal)
            except Exception as e:
                logger.error(f"Error executing GO callback: {e}")
                await query.message.reply_text(
                    f"\u274c Error al ejecutar: {e}", parse_mode="HTML"
                )
    elif action == "skip":
        log_info("telegram", f"User SKIPPED signal {signal_id}")
        await query.edit_message_text(
            text=query.message.text_html + "\n\n\u23ed <b>SKIP - Senal ignorada</b>",
            parse_mode="HTML",
        )


# ---------------------------------------------------------------------------
# Public API - send messages
# ---------------------------------------------------------------------------


async def _send_message(text: str, reply_markup=None) -> None:
    chat_id = _get_chat_id()
    if not chat_id:
        logger.warning("Cannot send message: chat_id not configured. Send /start to the bot first.")
        return
    if _app is None:
        logger.warning("Bot not initialized. Call start_bot() first.")
        return
    await _app.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def send_signal_alert(signal: dict, signal_id: str) -> None:
    _pending_signals[signal_id] = signal
    text = format_signal_alert(signal)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\u2705 GO", callback_data=f"go:{signal_id}"),
            InlineKeyboardButton("\u23ed SKIP", callback_data=f"skip:{signal_id}"),
        ]
    ])
    await _send_message(text, reply_markup=keyboard)


async def send_execution_confirmation(trade: dict) -> None:
    await _send_message(format_execution_confirmation(trade))


async def send_close_notification(trade: dict) -> None:
    await _send_message(format_close_notification(trade))


async def send_text(text: str) -> None:
    await _send_message(text)


async def send_report(title: str, body: str) -> None:
    message = f"\U0001f4ca <b>{title}</b>\n\n{body}"
    await _send_message(message)


# ---------------------------------------------------------------------------
# Sync wrappers (for calling from non-async code like APScheduler)
# ---------------------------------------------------------------------------


def send_signal_alert_sync(signal: dict, signal_id: str) -> None:
    _run_async(send_signal_alert(signal, signal_id))


def send_execution_confirmation_sync(trade: dict) -> None:
    _run_async(send_execution_confirmation(trade))


def send_close_notification_sync(trade: dict) -> None:
    _run_async(send_close_notification(trade))


def send_text_sync(text: str) -> None:
    _run_async(send_text(text))


def send_report_sync(title: str, body: str) -> None:
    _run_async(send_report(title, body))


def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        asyncio.run(coro)


# ---------------------------------------------------------------------------
# Bot lifecycle
# ---------------------------------------------------------------------------


def set_go_callback(callback: Callable) -> None:
    global _on_go_callback
    _on_go_callback = callback


def build_app() -> Application:
    global _app
    token = _get_token()
    _app = (
        Application.builder()
        .token(token)
        .build()
    )
    _app.add_handler(CommandHandler("start", cmd_start))
    _app.add_handler(CommandHandler("status", cmd_status))
    _app.add_handler(CommandHandler("positions", cmd_positions))
    _app.add_handler(CommandHandler("test", cmd_test))
    _app.add_handler(CommandHandler("force", cmd_force))
    _app.add_handler(CommandHandler("scan", cmd_scan))
    _app.add_handler(CommandHandler("ready", cmd_ready))
    _app.add_handler(CommandHandler("golive", cmd_golive))
    _app.add_handler(CommandHandler("checklist", cmd_golive))
    _app.add_handler(CommandHandler("party", cmd_party))
    _app.add_handler(CommandHandler("pause", cmd_pause))
    _app.add_handler(CommandHandler("resume", cmd_resume))
    _app.add_handler(CommandHandler("close", cmd_close))
    _app.add_handler(CommandHandler("closeall", cmd_closeall))
    _app.add_handler(CommandHandler("help", cmd_help))
    _app.add_handler(CallbackQueryHandler(callback_handler))
    return _app


async def start_bot() -> None:
    app = build_app()
    log_info("telegram", "Telegram bot starting...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log_info("telegram", "Telegram bot running")


async def stop_bot() -> None:
    if _app:
        log_info("telegram", "Telegram bot stopping...")
        await _app.updater.stop()
        await _app.stop()
        await _app.shutdown()
        log_info("telegram", "Telegram bot stopped")


# ---------------------------------------------------------------------------
# Quick test: python -m notifications.telegram_bot
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    async def test():
        app = build_app()
        await app.initialize()

        chat_id = _get_chat_id()
        if not chat_id:
            print(
                "\n\u26a0  chat_id no configurado."
                "\n   1. Abre Telegram y busca tu bot: @AlgoTradeJSG_bot"
                "\n   2. Envia /start"
                "\n   3. Copia el chat_id que te devuelve"
                "\n   4. Pegalo en config/secrets.yaml > telegram > chat_id"
                "\n   5. Vuelve a ejecutar este script"
                "\n"
            )
            # Start polling to capture the /start command
            print("Esperando que envies /start al bot...\n")
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            try:
                # Run until interrupted
                while True:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                pass
            finally:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
            return

        # If chat_id exists, send a test message
        print(f"Enviando mensaje de prueba a chat_id={chat_id}...")
        await app.bot.send_message(
            chat_id=chat_id,
            text=(
                "\U0001f680 <b>10X Trading Bot - Test</b>\n\n"
                "\u2705 Conexion exitosa\n"
                f"\U0001f552 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                "<i>El bot esta listo para enviar alertas de trading.</i>"
            ),
            parse_mode="HTML",
        )
        print("\u2705 Mensaje enviado!")

        # Send a sample signal alert
        print("Enviando alerta de ejemplo...")
        sample_signal = {
            "pair": _normalize_force_pair("BTC"),
            "direction": "long",
            "entry_price": 67450.00,
            "stop_loss": 66100.00,
            "take_profit_1": 69470.00,
            "take_profit_2": 71500.00,
            "position_size": "0.015 BTC",
            "risk_gbp": 42,
            "risk_pct": 4.2,
            "leverage": 8,
            "market": "crypto",
            "signals_triggered": {
                "rsi": True,
                "ema": True,
                "macd": False,
                "volume": True,
            },
        }
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("\u2705 GO", callback_data="go:test_001"),
                InlineKeyboardButton("\u23ed SKIP", callback_data="skip:test_001"),
            ]
        ])
        _pending_signals["test_001"] = sample_signal
        await app.bot.send_message(
            chat_id=chat_id,
            text=format_signal_alert(sample_signal),
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        print("\u2705 Alerta de ejemplo enviada!")
        print("\nIniciando polling para recibir GO/SKIP...")
        print("(Presiona Ctrl+C para salir)\n")

        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()

    asyncio.run(test())
