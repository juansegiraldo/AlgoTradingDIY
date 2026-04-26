"""
10X Trading System — Entry Point

Starts:
  1. SQLite database (auto-creates if needed)
  2. Telegram bot (listens for commands + GO/SKIP)
  3. APScheduler with all scheduled tasks:
     - Scanner every 5 min
     - Health check every 60 min
     - Summary at 08:00, 12:00, 20:00 London time (auto-adjusts BST/GMT)
     - Daily report at 22:00 UTC
     - Weekly report Friday 20:00 UTC

Usage:
  python main.py          # Start the system
  python main.py --scan   # Run one scan and exit (for testing)

To stop: Ctrl+C
"""

import asyncio
import argparse
import logging
import signal
import sys
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from config.loader import get_settings
from data.database import init_db, save_equity_snapshot, log_info
from pipeline import run_scan_cycle, on_go_callback
from notifications.telegram_bot import (
    build_app,
    set_go_callback,
    send_text_sync,
    start_bot,
    stop_bot,
)
from notifications.report_generator import (
    send_daily_report,
    send_weekly_report,
    send_partial_report,
    send_morning_report,
)
from execution.crypto_executor import get_exchange_name, health_check as crypto_health_check
from execution.position_manager import check_open_positions

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scheduled task wrappers
# ---------------------------------------------------------------------------


def scheduled_position_check():
    """Check open positions for SL/TP hits (called by APScheduler every 30s)."""
    try:
        check_open_positions()
    except Exception as e:
        logger.error(f"Position check error: {e}", exc_info=True)
        log_info("scheduler", f"Position check error: {e}")


def scheduled_scan():
    """Run the scanner (called by APScheduler)."""
    try:
        signals = run_scan_cycle()
        if signals:
            logger.info(f"Scan found {len(signals)} signal(s)")
    except Exception as e:
        logger.error(f"Scan error: {e}", exc_info=True)
        log_info("scheduler", f"Scan error: {e}")


def scheduled_health_check():
    """Check exchange connectivity."""
    try:
        exchange = get_exchange_name()
        hc = crypto_health_check()
        if hc["status"] != "ok":
            logger.warning(f"{exchange.title()} health check failed: {hc}")
            try:
                send_text_sync(
                    f"\u26a0 <b>Health Check FAILED</b>\n"
                    f"{exchange.title()}: {hc.get('error', 'unknown')}"
                )
            except Exception:
                pass
        else:
            logger.debug(f"Health check OK: {hc}")
    except Exception as e:
        logger.error(f"Health check error: {e}")


def scheduled_partial_report():
    """Send partial P&L update."""
    try:
        send_partial_report()
    except Exception as e:
        logger.error(f"Partial report error: {e}")


def scheduled_morning_report():
    """Send morning readiness and exchange balance snapshot."""
    try:
        send_morning_report()
    except Exception as e:
        logger.error(f"Morning report error: {e}")


def scheduled_daily_report():
    """Send nightly report."""
    try:
        send_daily_report()
    except Exception as e:
        logger.error(f"Daily report error: {e}")


def scheduled_weekly_report():
    """Send weekly report."""
    try:
        send_weekly_report()
    except Exception as e:
        logger.error(f"Weekly report error: {e}")


# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------


def print_banner(settings: dict):
    mode = settings.get("mode", "paper").upper()
    capital = settings.get("initial_capital_gbp", 1000)
    sched = settings.get("scheduler", {})

    print()
    print("=" * 60)
    print("  10X TRADING SYSTEM")
    print("=" * 60)
    print(f"  Mode:       {mode}")
    print(f"  Capital:    GBP {capital:,}")
    print(f"  Scanner:    every {sched.get('scanner_interval_minutes', 5)} min")
    exchange = settings.get("markets", {}).get("crypto", {}).get("exchange", "kraken").title()
    print(f"  Markets:    Crypto ({exchange})")
    print(f"  Pairs:      {settings['markets']['crypto']['pairs']}")
    print(f"  Timeframes: {settings['markets']['crypto']['timeframes']}")
    print(f"  Telegram:   @AlgoTradeJSG_bot")
    print("=" * 60)
    print(f"  Started:    {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Stop:       Ctrl+C")
    print("=" * 60)
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    parser = argparse.ArgumentParser(description="10X Trading System")
    parser.add_argument("--scan", action="store_true", help="Run one scan cycle and exit")
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Init database
    init_db()

    settings = get_settings()
    mode = settings.get("mode", "paper")

    # Save initial equity if none exists
    from data.database import get_latest_equity
    if not get_latest_equity():
        save_equity_snapshot(total_capital=settings.get("initial_capital_gbp", 1000))
        log_info("main", "Initial equity snapshot saved")

    # --scan mode: run once and exit
    if args.scan:
        print("Running single scan cycle...\n")
        signals = run_scan_cycle()
        print(f"\nSignals found: {len(signals)}")
        for s in signals:
            print(f"  {s['pair']} ({s['timeframe']}) {s['direction'].upper()} @ {s['entry_price']:,.2f}")
        return

    # Full mode: start everything
    print_banner(settings)

    # 1. Register GO callback
    set_go_callback(on_go_callback)

    # 2. Build and start Telegram bot
    app = build_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log_info("main", "Telegram bot started")

    # 3. Setup APScheduler
    scheduler = AsyncIOScheduler(timezone="UTC")
    sched_cfg = settings.get("scheduler", {})

    # Position monitor: every 30 seconds (checks SL/TP hits)
    scheduler.add_job(
        scheduled_position_check,
        IntervalTrigger(seconds=30),
        id="position_monitor",
        name="Position Monitor (SL/TP)",
        max_instances=1,
    )

    # Scanner: every N minutes
    scheduler.add_job(
        scheduled_scan,
        IntervalTrigger(minutes=sched_cfg.get("scanner_interval_minutes", 5)),
        id="scanner",
        name="Signal Scanner",
        max_instances=1,
    )

    # Health check: every 60 min
    scheduler.add_job(
        scheduled_health_check,
        IntervalTrigger(minutes=sched_cfg.get("health_check_interval_minutes", 60)),
        id="health_check",
        name="Health Check",
    )

    # Summaries at fixed London times (CronTrigger auto-adjusts BST/GMT)
    import pytz
    london_tz = pytz.timezone("Europe/London")

    morning_time = sched_cfg.get("morning_report_time", "08:00").split(":")
    scheduler.add_job(
        scheduled_morning_report,
        CronTrigger(hour=int(morning_time[0]), minute=int(morning_time[1]), timezone=london_tz),
        id="morning_report",
        name="Morning Report (08:00 London)",
    )

    midday_time = sched_cfg.get("midday_report_time", "12:00").split(":")
    scheduler.add_job(
        scheduled_partial_report,
        CronTrigger(hour=int(midday_time[0]), minute=int(midday_time[1]), timezone=london_tz),
        id="midday_report",
        name="Midday Report (12:00 London)",
    )

    evening_time = sched_cfg.get("evening_report_time", "20:00").split(":")
    scheduler.add_job(
        scheduled_partial_report,
        CronTrigger(hour=int(evening_time[0]), minute=int(evening_time[1]), timezone=london_tz),
        id="evening_report",
        name="Evening Report (20:00 London)",
    )

    # Daily report: at 22:00 UTC
    daily_time = sched_cfg.get("daily_report_time", "22:00").split(":")
    scheduler.add_job(
        scheduled_daily_report,
        CronTrigger(hour=int(daily_time[0]), minute=int(daily_time[1])),
        id="daily_report",
        name="Daily Report",
    )

    # Weekly report: Friday at 20:00 UTC
    weekly_time = sched_cfg.get("weekly_report_time", "20:00").split(":")
    scheduler.add_job(
        scheduled_weekly_report,
        CronTrigger(
            day_of_week="fri",
            hour=int(weekly_time[0]),
            minute=int(weekly_time[1]),
        ),
        id="weekly_report",
        name="Weekly Report",
    )

    scheduler.start()
    log_info("main", "Scheduler started with all jobs")

    # Print scheduled jobs
    print("Scheduled jobs:")
    for job in scheduler.get_jobs():
        print(f"  - {job.name}: {job.trigger}")
    print()

    # 4. Send startup notification to Telegram
    try:
        send_text_sync(
            f"\U0001f680 <b>10X Trading System ONLINE</b>\n\n"
            f"\U0001f3f7 Modo: <b>{mode.upper()}</b>\n"
            f"\U0001f4b0 Capital: GBP {settings.get('initial_capital_gbp', 1000):,}\n"
            f"\U0001f4e1 Scanner: cada {sched_cfg.get('scanner_interval_minutes', 5)} min\n"
            f"\U0001f552 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"<i>Usa /status, /positions, /pause, /help</i>"
        )
    except Exception as e:
        logger.warning(f"Could not send startup notification: {e}")

    # Run first scan immediately
    print("Running initial scan...")
    scheduled_scan()

    # 5. Keep running until Ctrl+C
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        print("\nShutting down...")
    finally:
        scheduler.shutdown(wait=False)
        log_info("main", "Scheduler stopped")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        log_info("main", "System stopped")
        print("System stopped cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBye!")
