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
        "/start - Registrar chat y obtener ID\n"
        "/status - Resumen del portfolio\n"
        "/positions - Posiciones abiertas\n"
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
