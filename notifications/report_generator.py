"""
Report generator — daily and weekly summaries sent via Telegram.
"""

import logging
from datetime import datetime, timedelta, timezone

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import get_settings
from data.database import (
    get_all_trades,
    get_daily_pnl,
    get_latest_equity,
    get_open_trades,
    get_total_pnl,
    get_weekly_pnl,
    get_win_rate,
    get_profit_factor,
    count_open_trades,
    save_equity_snapshot,
)
from risk.circuit_breaker import get_risk_status

logger = logging.getLogger(__name__)


def generate_daily_report() -> str:
    """Generate the nightly report (22:00 GMT)."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    mode = get_settings().get("mode", "paper").upper()

    daily_pnl = get_daily_pnl(today)
    total_pnl = get_total_pnl()
    win_rate = get_win_rate()
    pf = get_profit_factor()
    risk = get_risk_status()
    open_count = count_open_trades()

    wr_text = f"{win_rate:.1f}%" if win_rate is not None else "N/A"
    pf_text = f"{pf:.2f}" if pf is not None else "N/A"
    pnl_emoji = "\U0001f4b5" if daily_pnl >= 0 else "\U0001f4b8"

    # Count today's trades
    trades_today = [
        t for t in get_all_trades(200)
        if t.get("timestamp_open", "").startswith(today)
    ]

    report = (
        f"\U0001f4ca <b>REPORTE DIARIO — {today}</b>\n"
        f"\U0001f3f7 Modo: {mode}\n"
        f"\n"
        f"{pnl_emoji} <b>PnL del dia:</b> GBP {daily_pnl:+,.2f}\n"
        f"\U0001f4b0 <b>PnL total:</b> GBP {total_pnl:+,.2f}\n"
        f"\U0001f4b0 <b>Capital:</b> GBP {risk['capital_current']:,.2f}\n"
        f"\n"
        f"\U0001f4c8 <b>Metricas:</b>\n"
        f"  Win Rate: {wr_text}\n"
        f"  Profit Factor: {pf_text}\n"
        f"  Drawdown: {risk['drawdown_pct']:.1f}%\n"
        f"\n"
        f"\U0001f4cb Trades hoy: {len(trades_today)}\n"
        f"\U0001f4cb Posiciones abiertas: {open_count}\n"
        f"\n"
        f"\u26a0 <b>Riesgo:</b>\n"
        f"  Daily restante: GBP {risk['daily_remaining']:,.2f}\n"
        f"  Weekly restante: GBP {risk['weekly_remaining']:,.2f}\n"
        f"  Circuit breaker: {'ACTIVO' if risk['circuit_breaker_active'] else 'OK'}\n"
        f"\n"
        f"\U0001f552 {now.strftime('%H:%M UTC')}"
    )
    return report


def generate_weekly_report() -> str:
    """Generate the weekly report (Friday 20:00 GMT)."""
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    week_start = monday.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    mode = get_settings().get("mode", "paper").upper()

    weekly_pnl = get_weekly_pnl(week_start)
    total_pnl = get_total_pnl()
    win_rate = get_win_rate()
    pf = get_profit_factor()
    risk = get_risk_status()

    initial = risk["capital_initial"]
    current = risk["capital_current"]
    total_return = ((current - initial) / initial * 100) if initial > 0 else 0

    wr_text = f"{win_rate:.1f}%" if win_rate is not None else "N/A"
    pf_text = f"{pf:.2f}" if pf is not None else "N/A"

    # Count week's trades
    week_trades = [
        t for t in get_all_trades(500)
        if t.get("timestamp_open", "") >= week_start
    ]
    closed_trades = [t for t in week_trades if t["status"] != "open"]

    report = (
        f"\U0001f4ca <b>REPORTE SEMANAL</b>\n"
        f"\U0001f4c5 Semana del {monday.strftime('%d/%m')} al {now.strftime('%d/%m/%Y')}\n"
        f"\U0001f3f7 Modo: {mode}\n"
        f"\n"
        f"\U0001f4b0 <b>Resumen financiero:</b>\n"
        f"  PnL semanal: GBP {weekly_pnl:+,.2f}\n"
        f"  PnL total: GBP {total_pnl:+,.2f}\n"
        f"  Capital: GBP {current:,.2f}\n"
        f"  Retorno total: {total_return:+.1f}%\n"
        f"\n"
        f"\U0001f4c8 <b>Metricas:</b>\n"
        f"  Win Rate: {wr_text}\n"
        f"  Profit Factor: {pf_text}\n"
        f"  Max Drawdown: {risk['drawdown_pct']:.1f}%\n"
        f"\n"
        f"\U0001f4cb <b>Actividad:</b>\n"
        f"  Trades esta semana: {len(week_trades)}\n"
        f"  Cerrados: {len(closed_trades)}\n"
        f"  Abiertos: {count_open_trades()}\n"
        f"\n"
        f"\u26a0 <b>Riesgo semanal:</b>\n"
        f"  Restante: GBP {risk['weekly_remaining']:,.2f}\n"
        f"  Circuit breaker: {'ACTIVO' if risk['circuit_breaker_active'] else 'OK'}\n"
    )

    # Add milestone check
    if current >= 5000:
        report += f"\n\U0001f3c6 <b>MILESTONE: 5x alcanzado! Retira GBP 1,000 de proteccion.</b>"
    elif current >= 2000:
        report += f"\n\U0001f389 <b>Capital duplicado! Sigue asi.</b>"
    elif current <= 500:
        report += f"\n\U0001f6a8 <b>ALERTA: Capital al 50%. Revisa la estrategia.</b>"

    return report


def generate_partial_report() -> str:
    """Generate a partial P&L update (every 4 hours)."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    daily_pnl = get_daily_pnl(today)
    open_count = count_open_trades()
    total_pnl = get_total_pnl()

    pnl_emoji = "\U0001f4b5" if daily_pnl >= 0 else "\U0001f4b8"

    return (
        f"{pnl_emoji} <b>Update {now.strftime('%H:%M UTC')}</b>\n"
        f"PnL hoy: GBP {daily_pnl:+,.2f} | Total: GBP {total_pnl:+,.2f} | "
        f"Abiertas: {open_count}"
    )


def send_daily_report() -> None:
    """Generate and send the daily report."""
    from notifications.telegram_bot import send_report_sync
    report = generate_daily_report()
    send_report_sync("REPORTE DIARIO", report)
    # Save equity snapshot
    risk = get_risk_status()
    save_equity_snapshot(
        total_capital=risk["capital_current"],
        open_positions=count_open_trades(),
        daily_pnl=get_daily_pnl(datetime.now(timezone.utc).strftime("%Y-%m-%d")),
    )
    logger.info("Daily report sent")


def send_weekly_report() -> None:
    """Generate and send the weekly report."""
    from notifications.telegram_bot import send_report_sync
    report = generate_weekly_report()
    send_report_sync("REPORTE SEMANAL", report)
    logger.info("Weekly report sent")


def send_partial_report() -> None:
    """Generate and send a partial update."""
    from notifications.telegram_bot import send_text_sync
    report = generate_partial_report()
    send_text_sync(report)
    logger.info("Partial report sent")
