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
    get_total_pnl,
    get_win_rate,
    log_info,
    log_warning,
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
    return pairs or ["BTC/USDT"]


def _normalize_force_pair(raw_pair: Optional[str]) -> str:
    """Normalize /force input into a configured crypto pair."""
    pairs = _get_force_pairs()
    if not raw_pair:
        return pairs[0]

    normalized = raw_pair.strip().upper().replace("-", "/")
    if normalized in pairs:
        return normalized

    if normalized.endswith("USDT") and "/" not in normalized:
        normalized = f"{normalized[:-4]}/USDT"
    elif "/" not in normalized:
        normalized = f"{normalized}/USDT"

    if normalized in pairs:
        return normalized

    base_symbol = normalized.split("/", 1)[0]
    for pair in pairs:
        if pair.split("/", 1)[0] == base_symbol:
            return pair

    supported = ", ".join(pair.split("/", 1)[0] for pair in pairs)
    raise ValueError(f"Activo no soportado: {raw_pair}. Usa uno de: {supported}")


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def format_signal_alert(signal: dict) -> str:
    direction_emoji = "\U0001f7e2" if signal["direction"] == "long" else "\U0001f534"
    return (
        f"{direction_emoji} <b>{signal['pair']} {signal['direction'].upper()}</b>\n"
        f"\n"
        f"\U0001f4b0 <b>Entrada:</b> {signal['entry_price']:,.2f}\n"
        f"\U0001f6d1 <b>Stop-Loss:</b> {signal['stop_loss']:,.2f}\n"
        f"\U0001f3af <b>TP1:</b> {float(signal['take_profit_1']):,.2f}\n"
        f"\U0001f3af <b>TP2:</b> {float(signal['take_profit_2']):,.2f}\n"
        f"\n"
        f"\U0001f4ca <b>Tamano:</b> {float(signal['position_size']):.6f}\n"
        f"\U000026a0 <b>Riesgo:</b> GBP {float(signal.get('risk_gbp', 0)):.1f} ({float(signal.get('risk_pct', 0)):.1f}%)\n"
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
    GBP_PER_USDT = 1.0 / 1.27
    entry = float(trade["entry_price"])
    size = float(trade["position_size"])
    leverage = float(trade.get("leverage", 1))
    notional_usd = size * entry
    margin_gbp = (notional_usd / leverage) * GBP_PER_USDT
    position_gbp = notional_usd * GBP_PER_USDT
    return (
        f"\u2705 <b>ORDEN EJECUTADA</b>\n"
        f"\n"
        f"\U0001f4c4 <b>{trade['pair']} {trade['direction'].upper()}</b>\n"
        f"\U0001f4b0 Entrada: ${entry:,.2f}\n"
        f"\U0001f6d1 SL: ${float(trade['stop_loss']):,.2f}\n"
        f"\U0001f4aa Leverage: {leverage:.0f}x\n"
        f"\n"
        f"\U0001f4b0 <b>Tu pones:</b> GBP {margin_gbp:,.2f}\n"
        f"\U0001f4ca <b>Posicion total:</b> GBP {position_gbp:,.2f}\n"
        f"\U0001f3f7 Modo: {trade.get('mode', 'paper').upper()}"
    )


def format_close_notification(trade: dict) -> str:
    pnl = trade.get("pnl_absolute", 0)
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
        f"{'PnL: +' if pnl >= 0 else 'PnL: '}{pnl:.2f} GBP ({trade.get('pnl_percent', 0):.1f}%)"
    )


def format_portfolio_status() -> str:
    GBP_PER_USDT = 1.0 / 1.27
    settings = get_settings()
    equity = get_latest_equity()
    open_count = count_open_trades()
    realized_pnl = get_total_pnl()
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

    wr_text = f"{win_rate:.1f}%" if win_rate is not None else "N/A"

    # Fetch balance from Binance (live) or DB (paper) — source of truth
    try:
        from execution.binance_executor import fetch_usdt_balance
        usdt_bal = fetch_usdt_balance()
        exchange_free_gbp = usdt_bal["free"] * GBP_PER_USDT
        exchange_used_gbp = usdt_bal["used"] * GBP_PER_USDT
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
            total_margin += (size * entry / leverage) * GBP_PER_USDT
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
        f"\U0001f3f7 <b>Modo:</b> {settings.get('mode', 'paper').upper()} | "
        f"Stage: {settings.get('live_stage', 'stage_10')}\n"
    )

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
        text += f"   {real_emoji} Trades cerrados: {real_sign}{realized_pnl:,.2f} GBP\n"

    total_emoji = "\U0001f7e2" if total_pnl >= 0 else "\U0001f534"
    total_sign = "+" if total_pnl >= 0 else ""
    total_pct = (total_pnl / initial_capital * 100) if initial_capital > 0 else 0
    text += f"   {total_emoji} <b>Total: {total_sign}{total_pnl:,.2f} GBP ({total_sign}{total_pct:.1f}%)</b>\n"

    if win_rate is not None:
        text += f"\n\U0001f3af Win Rate: {wr_text}\n"

    text += f"\n\U0001f552 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"

    return text


def format_open_positions() -> str:
    trades = get_open_trades()
    if not trades:
        return "\U0001f4ad <b>Sin posiciones abiertas</b>"

    GBP_PER_USDT = 1.0 / 1.27
    lines = [f"\U0001f4cb <b>POSICIONES ABIERTAS ({len(trades)})</b>"]
    total_invested = 0.0
    total_pnl = 0.0

    for i, t in enumerate(trades, 1):
        direction_emoji = "\U0001f7e2" if t["direction"] == "long" else "\U0001f534"
        entry = float(t["entry_price"])
        size = float(t["position_size"])
        leverage = float(t.get("leverage") or 1)
        sl = float(t["stop_loss"])
        tp1 = float(t["take_profit_1"]) if t.get("take_profit_1") else None

        # Margin = what you actually "put in"
        notional_usd = size * entry
        margin_gbp = (notional_usd / leverage) * GBP_PER_USDT
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
            pnl_line = f"   {pnl_emoji} <b>{sign}{gbp:.2f} GBP</b> | Ahora: ${current_price:,.2f}\n"
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
        f"{total_emoji} PnL total: <b>{sign}{total_pnl:.2f} GBP</b>"
    )
    return "\n".join(lines)


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
        "/positions - Posiciones abiertas\n"
        "/force - Comprar YA (ej: /force btc, /force eth, /force sol short)\n"
        "/test - Trade de prueba con GO/SKIP\n"
        "/scan - Escanear mercados ahora\n"
        "/ready - Chequeo de readiness y saldo Binance\n"
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
        "<i>Modo actual: {mode}</i>".format(
            mode=get_settings().get("mode", "paper").upper()
        ),
        parse_mode="HTML",
    )


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force a test trade with real BTC price and GO/SKIP buttons."""
    await update.message.reply_text(
        "\U0001f9ea <b>Preparando trade de prueba...</b>",
        parse_mode="HTML",
    )

    try:
        from execution.binance_executor import fetch_price
        from risk.position_sizer import enrich_signal_with_sizing
        from risk.risk_manager import validate_trade
        from signals.signal_generator import format_signal_for_telegram
        from data.database import get_latest_equity, save_equity_snapshot
        import uuid

        # Ensure equity exists
        if not get_latest_equity():
            save_equity_snapshot(total_capital=get_settings().get("initial_capital_gbp", 1000))

        # Get live price
        btc_price = fetch_price("BTC/USDT")

        # Build signal
        signal = {
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
            "strength": "moderate (TEST)",
        }

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

    await update.message.reply_text(
        f"\U0001f50e <b>Analizando {pair}...</b>",
        parse_mode="HTML",
    )

    try:
        from execution.binance_executor import fetch_price, fetch_ohlcv
        from signals.indicators import ohlcv_to_dataframe, analyze
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

        # Calculate 3 position sizes
        if direction == "long":
            sl = round(price * 0.98, 2)
            tp1 = round(price * 1.03, 2)
            tp2 = round(price * 1.06, 2)
        else:
            sl = round(price * 1.02, 2)
            tp1 = round(price * 0.97, 2)
            tp2 = round(price * 0.94, 2)

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
            "signal_count": 4,
            "min_required": 3,
            "signals_triggered": {"manual": True},
            "trend": trend.get("trend", "unknown"),
            "strength": "manual",
        }

        sig_safe = enrich_signal_with_sizing({
            **base_signal,
            "leverage": 5,
            "size_scale": 0.4,
            "size_profile": "safe",
        })
        sig_med = enrich_signal_with_sizing({
            **base_signal,
            "leverage": 10,
            "size_scale": 1.0,
            "size_profile": "medium",
        })

        if not sig_safe.get("sizing_approved", False) and not sig_med.get("sizing_approved", False):
            await update.message.reply_text(
                "\u26d4 <b>No se pudo calcular un tamano valido para este activo.</b>",
                parse_mode="HTML",
            )
            return

        margin_safe = sig_safe["margin_required"]
        margin_med = sig_med["margin_required"]
        value_safe = sig_safe["position_size_value"]
        value_med = sig_med["position_size_value"]

        info_text = (
            f"{dir_emoji} <b>{pair} — {direction.upper()}</b>\n"
            f"\n"
            f"\U0001f4b0 <b>Precio:</b> ${price:,.2f}\n"
            f"{change_icon} <b>24h:</b> {change_24h:+.2f}%\n"
            f"\U0001f4c8 <b>Tendencia:</b> {trend_icon}\n"
            f"\n"
            f"<b>Indicadores (1h):</b>\n"
            f"  {rsi_icon} RSI: {rsi.get('value', 'N/A')}\n"
            f"  {ema_icon} EMA 9/21: {'cruce ' + (ema.get('signal') or 'sin cruce').upper() if ema.get('signal') else 'sin cruce'}\n"
            f"  {macd_icon} MACD: {'cruce ' + (macd.get('signal') or 'sin cruce').upper() if macd.get('signal') else 'sin cruce'}\n"
            f"  {vol_icon} Volumen: {vol_ratio}x vs promedio\n"
            f"\n"
            f"\U0001f6d1 <b>Stop-Loss:</b> ${sl:,.2f} (-2%)\n"
            f"\U0001f3af <b>TP1:</b> ${tp1:,.2f} (+3%) | <b>TP2:</b> ${tp2:,.2f} (+6%)\n"
            f"\n"
            f"\U0001f4b7 <b>Capital actual:</b> GBP {capital:,.0f}\n"
            f"\n"
            f"<b>Elige tamano:</b>\n\n"
            f"\u2705 <b>Seguro</b> (5x) - Pones <b>GBP {margin_safe:,.0f}</b> -> Posicion GBP {value_safe:,.0f} | Pierdes max GBP {sig_safe['risk_gbp']:,.0f}\n"
            f"\U0001f525 <b>Medio</b> (10x) - Pones <b>GBP {margin_med:,.0f}</b> -> Posicion GBP {value_med:,.0f} | Pierdes max GBP {sig_med['risk_gbp']:,.0f}"
        )

        # Store signals for each button (risk validation happens again on GO)
        id_safe = f"force_safe_{uuid.uuid4().hex[:6]}"
        id_med = f"force_med_{uuid.uuid4().hex[:6]}"

        _pending_signals[id_safe] = sig_safe
        _pending_signals[id_med] = sig_med

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"\u2705 Seguro (5x) \u2014 GBP {margin_safe:,.0f}",
                    callback_data=f"go:{id_safe}",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"\U0001f525 Medio (10x) \u2014 GBP {margin_med:,.0f}",
                    callback_data=f"go:{id_med}",
                ),
            ],
            [
                InlineKeyboardButton("\u23ed No comprar", callback_data=f"skip:{id_safe}"),
            ],
        ])

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
        signals = run_scan_cycle()

        if signals:
            text = f"\U0001f4e1 <b>{len(signals)} senal(es) detectada(s)!</b>\n\n"
            for s in signals:
                text += f"  - {s['pair']} ({s['timeframe']}) {s['direction'].upper()}\n"
            text += "\n<i>Las alertas con GO/SKIP se enviaron arriba.</i>"
        else:
            text = (
                "\U0001f4ad <b>Sin senales en este momento.</b>\n\n"
                "Los 3 pares (BTC, ETH, SOL) fueron analizados en 1h y 4h.\n"
                "Ninguno tiene 3/4 indicadores alineados ahora.\n\n"
                "<i>El scanner automatico revisa cada 5 minutos.</i>"
            )
        await update.message.reply_text(text, parse_mode="HTML")

    except Exception as e:
        await update.message.reply_text(
            f"\u274c <b>Error:</b> {e}",
            parse_mode="HTML",
        )


async def cmd_ready(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show operational readiness and current Binance balance snapshot."""
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
                f"PnL: <b>{sign}{pnl:.2f} GBP ({sign}{pnl_pct:.1f}%)</b>",
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

    lines = ["\U0001f4cb <b>POSICIONES ABIERTAS</b>\n"]
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
                f"PnL: <b>{sign}{pnl:.2f} GBP ({sign}{pnl_pct:.1f}%)</b>",
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
                total_emoji = "\U0001f7e2" if total_pnl >= 0 else "\U0001f534"
                sign = "+" if total_pnl >= 0 else ""

                lines = [f"\u26d4 <b>CIERRE TOTAL — {len(results)} posicion(es)</b>\n"]
                for r in results:
                    if not r.get("success"):
                        lines.append(f"\u274c #{r.get('trade_id')} {r.get('pair', '?')} - Error: {r.get('error', '?')}")
                        continue
                    pnl = r.get("pnl_gbp", 0)
                    pnl_pct = r.get("pnl_pct", 0)
                    pnl_emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"
                    r_sign = "+" if pnl >= 0 else ""
                    direction_emoji = "\U0001f7e2" if r.get("direction") == "long" else "\U0001f534"
                    lines.append(
                        f"{direction_emoji} <b>{r['pair']}</b> {r.get('direction', '').upper()} "
                        f"@ {r.get('entry_price', 0):,.2f} -> {r.get('exit_price', 0):,.2f} | "
                        f"{pnl_emoji} {r_sign}{pnl:.2f} GBP ({r_sign}{pnl_pct:.1f}%)"
                    )

                lines.append(
                    f"\n{total_emoji} <b>PnL total: {sign}{total_pnl:.2f} GBP</b>"
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
            "pair": "BTC/USDT",
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
