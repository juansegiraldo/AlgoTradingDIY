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
    get_daily_fees,
    get_daily_pnl,
    get_open_trades,
    get_total_fees,
    get_total_pnl,
    get_weekly_fees,
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
    daily_fees = get_daily_fees(today)
    realized_total = get_total_pnl()
    total_fees = get_total_fees()
    win_rate = get_win_rate()
    pf = get_profit_factor()
    risk = get_risk_status()
    open_count = count_open_trades()

    # Include unrealized PnL from open positions
    unrealized_pnl = 0.0
    try:
        from execution.position_manager import get_unrealized_pnl
        from data.database import get_open_trades as _get_open
        for t in _get_open():
            try:
                upnl = get_unrealized_pnl(t)
                unrealized_pnl += upnl["unrealized_pnl_gbp"]
            except Exception:
                pass
    except Exception:
        pass

    total_pnl = realized_total + unrealized_pnl
    daily_total = daily_pnl + unrealized_pnl

    wr_text = f"{win_rate:.1f}%" if win_rate is not None else "N/A"
    pf_text = f"{pf:.2f}" if pf is not None else "N/A"
    pnl_emoji = "\U0001f4b5" if daily_total >= 0 else "\U0001f4b8"

    # Count today's trades
    trades_today = [
        t for t in get_all_trades(200)
        if t.get("timestamp_open", "").startswith(today)
    ]

    unrealized_line = ""
    if open_count > 0 and unrealized_pnl != 0:
        unrealized_line = f"\U0001f4ca <b>Posiciones abiertas:</b> GBP {unrealized_pnl:+,.2f}\n"

    report = (
        f"\U0001f4ca <b>REPORTE DIARIO — {today}</b>\n"
        f"\U0001f3f7 Modo: {mode}\n"
        f"\n"
        f"{pnl_emoji} <b>PnL del dia:</b> GBP {daily_total:+,.2f}\n"
        f"\U0001f9fe <b>Fees del dia:</b> GBP -{daily_fees:,.2f}\n"
        f"{unrealized_line}"
        f"\U0001f4b0 <b>PnL total:</b> GBP {total_pnl:+,.2f}\n"
        f"\U0001f9fe <b>Fees total:</b> GBP -{total_fees:,.2f}\n"
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
    weekly_fees = get_weekly_fees(week_start)
    total_pnl = get_total_pnl()
    total_fees = get_total_fees()
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
        f"  PnL semanal neto: GBP {weekly_pnl:+,.2f}\n"
        f"  Fees semana: GBP -{weekly_fees:,.2f}\n"
        f"  PnL total neto: GBP {total_pnl:+,.2f}\n"
        f"  Fees total: GBP -{total_fees:,.2f}\n"
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
    """Generate a scheduled P&L summary (08:00, 12:00, 20:00 London time)."""
    import pytz
    now = datetime.now(timezone.utc)
    london_tz = pytz.timezone("Europe/London")
    now_london = now.astimezone(london_tz)
    today = now.strftime("%Y-%m-%d")
    daily_pnl = get_daily_pnl(today)
    daily_fees = get_daily_fees(today)
    open_count = count_open_trades()
    realized_pnl = get_total_pnl()

    # Include unrealized PnL from open positions
    unrealized_pnl = 0.0
    try:
        from execution.position_manager import get_unrealized_pnl
        from data.database import get_open_trades as _get_open
        for t in _get_open():
            try:
                upnl = get_unrealized_pnl(t)
                unrealized_pnl += upnl["unrealized_pnl_gbp"]
            except Exception:
                pass
    except Exception:
        pass

    total_pnl = realized_pnl + unrealized_pnl
    # daily_pnl = closed trades today; add unrealized for full day picture
    daily_total = daily_pnl + unrealized_pnl
    pnl_emoji = "\U0001f4b5" if daily_total >= 0 else "\U0001f4b8"

    text = f"{pnl_emoji} <b>Resumen {now_london.strftime('%H:%M')} (Londres)</b>\n"
    parts = []
    if open_count > 0 and unrealized_pnl != 0:
        parts.append(f"Abierto: GBP {unrealized_pnl:+,.2f}")
    if daily_pnl != 0:
        parts.append(f"Cerrado hoy: GBP {daily_pnl:+,.2f}")
    parts.append(f"PnL hoy: GBP {daily_total:+,.2f}")
    if daily_fees:
        parts.append(f"Fees hoy: GBP -{daily_fees:,.2f}")
    parts.append(f"Total: GBP {total_pnl:+,.2f}")
    parts.append(f"Abiertas: {open_count}")
    text += " | ".join(parts)
    return text


def generate_readiness_report() -> str:
    """Generate a compact operational readiness report for live trading."""
    settings = get_settings()
    mode = settings.get("mode", "paper").upper()
    stage = settings.get("live_stage", "stage_10")
    pairs = ", ".join(settings.get("markets", {}).get("crypto", {}).get("pairs", []))
    risk = get_risk_status()

    try:
        from execution.crypto_executor import (
            fetch_open_orders,
            fetch_positions,
            get_exchange_name,
            live_readiness_check,
        )
        readiness = live_readiness_check()
        snapshot = readiness.get("snapshot", {})
        positions = fetch_positions()
        orders = fetch_open_orders()
        ready = readiness.get("ready", False)
        status = "LISTO" if ready else "BLOQUEADO"
        free_gbp = snapshot.get("free_gbp", 0.0)
        total_gbp = snapshot.get("total_gbp", 0.0)
        exchange = snapshot.get("exchange") or get_exchange_name()
    except Exception as exc:
        ready = False
        status = "ERROR"
        positions = []
        orders = []
        free_gbp = 0.0
        total_gbp = 0.0
        exchange = get_settings().get("markets", {}).get("crypto", {}).get("exchange", "exchange")
        readiness = {"error": str(exc)}

    error_line = ""
    if readiness.get("error"):
        error_line = f"\n\u26a0 Error: {readiness['error']}"
    try:
        from execution.fees import estimate_round_trip_fee_gbp, get_taker_fee_pct
        fee_notional = min(float(free_gbp or 0.0), 10.0)
        fee_line = (
            f"\nFee est. ida/vuelta GBP {fee_notional:,.2f}: "
            f"GBP {estimate_round_trip_fee_gbp('BTC/GBP', fee_notional):,.2f} "
            f"({get_taker_fee_pct():.2f}% taker/lado)"
        )
    except Exception:
        fee_line = ""

    return (
        f"\U0001f6e1 <b>READY CHECK</b>\n"
        f"Modo: {mode}\n"
        f"Stage: {stage}\n"
        f"Estado: <b>{status}</b>\n"
        f"Saldo {str(exchange).title()}: GBP {total_gbp:,.2f}\n"
        f"Disponible: GBP {free_gbp:,.2f}\n"
        f"Posiciones abiertas: {len(positions)}\n"
        f"Ordenes abiertas: {len(orders)}\n"
        f"Circuit breaker: {'ACTIVO' if risk['circuit_breaker_active'] else 'OK'}\n"
        f"Pares habilitados: {pairs or 'N/A'}"
        f"{fee_line}"
        f"{error_line}"
    )


def generate_morning_report() -> str:
    """Generate the morning exchange balance and readiness report."""
    try:
        from execution.crypto_executor import save_account_snapshot
        snapshot = save_account_snapshot()
        exchange = snapshot.get("exchange", "exchange").title()
        header = (
            f"\U0001f305 <b>REPORTE MATINAL</b>\n"
            f"{exchange} total: GBP {snapshot['total_gbp']:,.2f}\n"
            f"Disponible: GBP {snapshot['free_gbp']:,.2f}\n"
            f"Reservado/usado: GBP {snapshot['used_gbp']:,.2f}\n\n"
        )
    except Exception as exc:
        header = (
            f"\U0001f305 <b>REPORTE MATINAL</b>\n"
            f"\u26a0 No se pudo guardar snapshot del exchange: {exc}\n\n"
        )
    return header + generate_readiness_report()


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


def send_morning_report() -> None:
    """Generate and send the dedicated morning readiness report."""
    from notifications.telegram_bot import send_text_sync
    report = generate_morning_report()
    send_text_sync(report)
    logger.info("Morning report sent")
