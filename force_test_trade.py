"""
Force a test trade through the full pipeline.
Sends a real alert to Telegram with GO/SKIP buttons.
If you tap GO, it executes a paper trade and records it in the database.

Usage: python force_test_trade.py
"""

import asyncio
import json
import logging
import uuid
import urllib.request
import urllib.parse
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

from config.loader import get_secrets, get_settings
from data.database import init_db, save_equity_snapshot, get_latest_equity, get_open_trades
from execution.binance_executor import fetch_price
from risk.position_sizer import enrich_signal_with_sizing
from risk.risk_manager import validate_trade
from pipeline import execute_signal
from notifications.telegram_bot import format_signal_alert

# Init
init_db()
if not get_latest_equity():
    save_equity_snapshot(total_capital=get_settings().get("initial_capital_gbp", 1000))

secrets = get_secrets()
TOKEN = secrets["telegram"]["bot_token"]
CHAT_ID = secrets["telegram"]["chat_id"]

def send_telegram(text, reply_markup=None):
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data)
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())

def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates?timeout=30"
    if offset:
        url += f"&offset={offset}"
    resp = urllib.request.urlopen(url, timeout=35)
    return json.loads(resp.read())

print("=" * 50)
print("  FORCE TEST TRADE")
print("=" * 50)

# 1. Get live price
print("\nObteniendo precio de BTC...")
btc_price = fetch_price("BTC/USDT")
print(f"BTC/USDT: ${btc_price:,.2f}")

# 2. Build signal
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
    "strength": "moderate",
}

# 3. Size it
signal = enrich_signal_with_sizing(signal)
print(f"Position: {signal['position_size']:.6f} BTC")
print(f"Risk: GBP {signal['risk_gbp']:.2f} ({signal['risk_pct']:.1f}%)")

# 4. Validate risk
validation = validate_trade(signal)
print(f"Risk check: {'APROBADO' if validation['approved'] else 'RECHAZADO'}")
if not validation["approved"]:
    for r in validation["rejection_reasons"]:
        print(f"  -> {r}")
    print("\nTrade rechazado por las reglas de riesgo.")
    exit(1)

# 5. Send alert to Telegram
signal_id = f"test_{uuid.uuid4().hex[:6]}"
alert_text = format_signal_alert({
    "pair": signal["pair"],
    "direction": signal["direction"],
    "entry_price": signal["entry_price"],
    "stop_loss": signal["stop_loss"],
    "take_profit_1": signal["take_profit_1"],
    "take_profit_2": signal["take_profit_2"],
    "position_size": f"{signal['position_size']:.6f} BTC",
    "risk_gbp": signal["risk_gbp"],
    "risk_pct": signal["risk_pct"],
    "leverage": signal["leverage"],
    "market": "crypto",
    "signals_triggered": signal["signals_triggered"],
})

keyboard = {
    "inline_keyboard": [[
        {"text": "\u2705 GO", "callback_data": f"go:{signal_id}"},
        {"text": "\u23ed SKIP", "callback_data": f"skip:{signal_id}"},
    ]]
}

print("\nEnviando alerta a Telegram...")
send_telegram(alert_text, reply_markup=keyboard)
print("Alerta enviada! Revisa Telegram.")
print("\nEsperando tu respuesta (GO o SKIP)...")

# 6. Wait for callback
last_update_id = None
while True:
    updates = get_updates(offset=last_update_id)
    for update in updates.get("result", []):
        last_update_id = update["update_id"] + 1
        callback = update.get("callback_query")
        if callback and signal_id in callback.get("data", ""):
            action = callback["data"].split(":")[0]

            # Answer the callback
            urllib.request.urlopen(
                f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery"
                f"?callback_query_id={callback['id']}"
            )

            if action == "go":
                print("\n>> GO recibido! Ejecutando trade...")
                result = execute_signal(signal)
                print(f"\nTrade ejecutado:")
                print(f"  Pair:    {result.get('pair', signal['pair'])}")
                print(f"  Entry:   ${result.get('entry_price', 0):,.2f}")
                print(f"  SL:      ${signal['stop_loss']:,.2f}")
                print(f"  TP1:     ${signal['take_profit_1']:,.2f}")
                print(f"  TP2:     ${signal['take_profit_2']:,.2f}")
                print(f"  TradeID: #{result.get('trade_id')}")

                # Send confirmation
                send_telegram(
                    f"\u2705 <b>TRADE EJECUTADO (PAPER)</b>\n\n"
                    f"BTC/USDT LONG @ ${result.get('entry_price', 0):,.2f}\n"
                    f"SL: ${signal['stop_loss']:,.2f} | TP1: ${signal['take_profit_1']:,.2f}\n"
                    f"Size: {signal['position_size']:.6f} BTC | Lev: {signal['leverage']}x\n\n"
                    f"Trade #{result.get('trade_id')} registrado en la base de datos."
                )

                # Show open positions
                open_trades = get_open_trades()
                print(f"\nPosiciones abiertas: {len(open_trades)}")
                for t in open_trades:
                    print(f"  #{t['id']} {t['pair']} {t['direction']} @ {t['entry_price']:,.2f}")

            elif action == "skip":
                print("\n>> SKIP recibido. Trade ignorado.")
                send_telegram("\u23ed Trade ignorado.")

            print("\nDone!")
            exit(0)
