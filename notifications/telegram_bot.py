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
        f"\U0001f3af <b>TP1:</b> {signal.get('take_profit_1', 'N/A')}\n"
        f"\U0001f3af <b>TP2:</b> {signal.get('take_profit_2', 'N/A')}\n"
        f"\n"
        f"\U0001f4ca <b>Tamano:</b> {signal['position_size']}\n"
        f"\U000026a0 <b>Riesgo:</b> GBP {signal.get('risk_gbp', '?')} ({signal.get('risk_pct', '?')}%)\n"
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
    return (
        f"\u2705 <b>ORDEN EJECUTADA</b>\n"
        f"\n"
        f"\U0001f4c4 <b>{trade['pair']} {trade['direction'].upper()}</b>\n"
        f"\U0001f4b0 Entrada: {trade['entry_price']:,.2f}\n"
        f"\U0001f6d1 SL: {trade['stop_loss']:,.2f}\n"
        f"\U0001f4ca Tamano: {trade['position_size']}\n"
        f"\U0001f4aa Leverage: {trade.get('leverage', 1)}x\n"
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
    equity = get_latest_equity()
    open_count = count_open_trades()
    total_pnl = get_total_pnl()
    win_rate = get_win_rate()

    if equity:
        capital = equity["total_capital"]
    else:
        capital = get_settings().get("initial_capital_gbp", 1000)

    wr_text = f"{win_rate:.1f}%" if win_rate is not None else "N/A"

    return (
        f"\U0001f4ca <b>PORTFOLIO STATUS</b>\n"
        f"\n"
        f"\U0001f4b0 Capital: GBP {capital:,.2f}\n"
        f"\U0001f4c8 PnL Total: GBP {total_pnl:+,.2f}\n"
        f"\U0001f3af Win Rate: {wr_text}\n"
        f"\U0001f4cb Posiciones abiertas: {open_count}\n"
        f"\n"
        f"\U0001f552 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )


def format_open_positions() -> str:
    trades = get_open_trades()
    if not trades:
        return "\U0001f4ad <b>Sin posiciones abiertas</b>"

    lines = ["\U0001f4cb <b>POSICIONES ABIERTAS</b>\n"]
    for t in trades:
        direction_emoji = "\U0001f7e2" if t["direction"] == "long" else "\U0001f534"
        lines.append(
            f"{direction_emoji} <b>{t['pair']}</b> {t['direction'].upper()} "
            f"@ {t['entry_price']:,.2f} | SL: {t['stop_loss']:,.2f} | "
            f"Lev: {t.get('leverage', 1)}x"
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
    log_warning("telegram", "System PAUSED by user command")
    await update.message.reply_text(
        "\u23f8 <b>Sistema PAUSADO</b>\n\n"
        "No se generaran ni ejecutaran senales.\n"
        "Usa /resume para reanudar.",
        parse_mode="HTML",
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        "/pause - Pausar todo el sistema\n"
        "/resume - Reanudar el sistema\n"
        "/help - Este mensaje\n\n"
        "<b>Alertas de trading:</b>\n"
        "Cuando se detecte una senal, recibiras un mensaje con botones "
        "<b>GO</b> (ejecutar) o <b>SKIP</b> (ignorar).\n\n"
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
            position_size=f"{signal.get('position_size', 0):.6f} BTC",
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
    """Force buy immediately — no rules, no confirmation. Just buy."""
    args = (context.args or [])
    pair = args[0].upper() if args else "BTC/USDT"
    # Normalize: "btc" -> "BTC/USDT"
    if "/" not in pair:
        pair = pair + "/USDT"

    direction = "long"
    if len(args) > 1 and args[1].lower() in ("short", "sell"):
        direction = "short"

    await update.message.reply_text(
        f"\U0001f525 <b>FORCE {direction.upper()} {pair}...</b>",
        parse_mode="HTML",
    )

    try:
        from execution.binance_executor import fetch_price
        from risk.position_sizer import enrich_signal_with_sizing
        from pipeline import execute_signal
        from data.database import get_latest_equity, save_equity_snapshot

        if not get_latest_equity():
            save_equity_snapshot(total_capital=get_settings().get("initial_capital_gbp", 1000))

        price = fetch_price(pair)

        if direction == "long":
            sl = round(price * 0.98, 2)
            tp1 = round(price * 1.03, 2)
            tp2 = round(price * 1.06, 2)
        else:
            sl = round(price * 1.02, 2)
            tp1 = round(price * 0.97, 2)
            tp2 = round(price * 0.94, 2)

        signal = {
            "pair": pair,
            "timeframe": "manual",
            "market": "crypto",
            "direction": direction,
            "entry_price": price,
            "stop_loss": sl,
            "take_profit_1": tp1,
            "take_profit_2": tp2,
            "leverage": 5,
            "signal_count": 4,
            "min_required": 3,
            "signals_triggered": {"manual": True},
            "trend": "forced",
            "strength": "manual",
        }

        signal = enrich_signal_with_sizing(signal)
        result = execute_signal(signal)

        if result.get("success"):
            entry = result.get("entry_price", price)
            mode = get_settings().get("mode", "paper").upper()
            await update.message.reply_text(
                f"\u2705 <b>COMPRADO ({mode})</b>\n\n"
                f"\U0001f4c4 {pair} {direction.upper()}\n"
                f"\U0001f4b0 Entrada: ${entry:,.2f}\n"
                f"\U0001f6d1 SL: ${sl:,.2f}\n"
                f"\U0001f3af TP1: ${tp1:,.2f} | TP2: ${tp2:,.2f}\n"
                f"\U0001f4ca Size: {signal['position_size']:.6f}\n"
                f"\U0001f4aa Leverage: 5x\n"
                f"\U0001f3f7 Trade #{result.get('trade_id')}",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"\u274c <b>Error:</b> {result.get('error')}",
                parse_mode="HTML",
            )

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


# ---------------------------------------------------------------------------
# Callback (GO / SKIP buttons)
# ---------------------------------------------------------------------------


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data  # e.g. "go:signal_123" or "skip:signal_123"
    action, signal_id = data.split(":", 1)

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
    _app.add_handler(CommandHandler("pause", cmd_pause))
    _app.add_handler(CommandHandler("resume", cmd_resume))
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
