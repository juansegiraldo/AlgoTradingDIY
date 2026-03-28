"""
Main scanner loop — checks all configured pairs for trading signals.

Designed to run every 5 minutes via APScheduler. For each pair and
timeframe, it:
1. Fetches OHLCV data from the exchange
2. Runs all 4 indicators
3. Generates a signal if 3/4 indicators align
4. Returns a list of signals for further processing (risk check, alert, etc.)
"""

import logging
from typing import Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import get_settings
from data.database import log_info, log_error, log_warning
from signals.indicators import analyze, ohlcv_to_dataframe
from signals.signal_generator import evaluate_signal

logger = logging.getLogger(__name__)

# Track last signals to avoid duplicate alerts
_last_signals: dict[str, str] = {}  # "pair:timeframe" -> "direction:timestamp_hour"


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


def _fetch_ohlcv(pair: str, timeframe: str, market: str, limit: int = 200):
    """Fetch OHLCV data from the appropriate exchange."""
    if market == "crypto":
        from execution.binance_executor import fetch_ohlcv
        return fetch_ohlcv(pair, timeframe, limit)
    # forex and etf fetchers will be added in steps 11 and 12
    elif market == "forex":
        log_warning("scanner", f"Forex OHLCV not implemented yet: {pair}")
        return None
    elif market == "etf":
        log_warning("scanner", f"ETF OHLCV not implemented yet: {pair}")
        return None
    return None


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def _is_duplicate(pair: str, timeframe: str, direction: str) -> bool:
    """Check if we already generated the same signal recently."""
    from datetime import datetime, timezone
    key = f"{pair}:{timeframe}"
    current_hour = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")
    value = f"{direction}:{current_hour}"
    if _last_signals.get(key) == value:
        return True
    _last_signals[key] = value
    return False


# ---------------------------------------------------------------------------
# Scan a single pair
# ---------------------------------------------------------------------------


def scan_pair(pair: str, timeframe: str, market: str) -> Optional[dict]:
    """
    Scan a single pair/timeframe for a signal.

    Returns a signal dict if triggered, None otherwise.
    """
    try:
        ohlcv = _fetch_ohlcv(pair, timeframe, market)
        if ohlcv is None or len(ohlcv) < 30:
            logger.debug(f"{pair} ({timeframe}): Not enough data ({len(ohlcv) if ohlcv else 0} candles)")
            return None

        df = ohlcv_to_dataframe(ohlcv)
        analysis = analyze(df)
        signal = evaluate_signal(pair, timeframe, market, analysis)

        if signal is None:
            return None

        # Check for duplicate
        if _is_duplicate(pair, timeframe, signal["direction"]):
            logger.debug(f"{pair} ({timeframe}): Duplicate signal, skipping")
            return None

        log_info(
            "scanner",
            f"Signal: {signal['direction'].upper()} {pair} ({timeframe}) "
            f"| {signal['signal_count']}/4 indicators | {signal['strength']}",
        )
        return signal

    except Exception as e:
        log_error("scanner", f"Error scanning {pair} ({timeframe}): {e}")
        logger.error(f"Error scanning {pair} ({timeframe}): {e}", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Full scan (all pairs, all timeframes)
# ---------------------------------------------------------------------------


def scan_all() -> list[dict]:
    """
    Scan all configured pairs and timeframes.

    Returns a list of triggered signals.
    """
    settings = get_settings()
    mode = settings.get("mode", "paper")

    if mode == "pause":
        log_info("scanner", "System is PAUSED — skipping scan")
        return []

    signals = []

    # Crypto
    crypto = settings["markets"].get("crypto", {})
    for pair in crypto.get("pairs", []):
        for tf in crypto.get("timeframes", []):
            signal = scan_pair(pair, tf, "crypto")
            if signal:
                signals.append(signal)

    # Forex (will work once oanda_executor is built in step 11)
    forex = settings["markets"].get("forex", {})
    for pair in forex.get("pairs", []):
        for tf in forex.get("timeframes", []):
            signal = scan_pair(pair, tf, "forex")
            if signal:
                signals.append(signal)

    # ETFs (will work once ibkr_executor is built in step 12)
    etfs = settings["markets"].get("etfs", {})
    for symbol in etfs.get("symbols", []):
        tf = etfs.get("timeframe", "D")
        signal = scan_pair(symbol, tf, "etf")
        if signal:
            signals.append(signal)

    log_info("scanner", f"Scan complete: {len(signals)} signal(s) from all markets")
    return signals


# ---------------------------------------------------------------------------
# Quick test: python -m signals.scanner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    print("=" * 60)
    print("  SCANNER TEST — Crypto pairs")
    print("=" * 60)

    settings = get_settings()
    crypto = settings["markets"]["crypto"]

    for pair in crypto["pairs"]:
        for tf in crypto["timeframes"]:
            print(f"\n--- {pair} ({tf}) ---")

            # Fetch data
            ohlcv = _fetch_ohlcv(pair, tf, "crypto")
            if not ohlcv:
                print("  No data")
                continue

            df = ohlcv_to_dataframe(ohlcv)
            print(f"  Candles: {len(df)}")
            print(f"  Last close: {df['close'].iloc[-1]:,.2f}")

            # Run indicators
            analysis = analyze(df)

            rsi = analysis["rsi"]
            ema = analysis["ema"]
            macd = analysis["macd"]
            vol = analysis["volume"]
            trend = analysis["trend"]

            print(f"  RSI:    {rsi['value']} | signal={rsi['signal']} | triggered={rsi['triggered']}")
            print(f"  EMA:    diff={ema.get('diff')} | signal={ema['signal']} | triggered={ema['triggered']}")
            print(f"  MACD:   {macd.get('value', {})} | signal={macd['signal']} | triggered={macd['triggered']}")
            vol_val = vol.get("value", {})
            print(f"  Volume: ratio={vol_val.get('ratio', 'N/A')} | triggered={vol['triggered']}")
            print(f"  Trend:  {trend.get('trend', 'N/A')}")

            # Generate signal
            signal = evaluate_signal(pair, tf, "crypto", analysis)
            if signal:
                print(f"\n  >>> SIGNAL: {signal['direction'].upper()} @ {signal['entry_price']:,.2f}")
                print(f"      SL: {signal['stop_loss']:,.2f} | TP1: {signal['take_profit_1']:,.2f} | TP2: {signal['take_profit_2']:,.2f}")
                print(f"      Strength: {signal['strength']} ({signal['signal_count']}/4)")
            else:
                print(f"  >>> No signal")

    print("\n" + "=" * 60)
    print("  FULL SCAN")
    print("=" * 60)
    all_signals = scan_all()
    print(f"\n  Total signals: {len(all_signals)}")
    for s in all_signals:
        print(f"  - {s['pair']} ({s['timeframe']}) {s['direction'].upper()} @ {s['entry_price']:,.2f}")

    print("\n  DONE")
