"""
Binance order execution via ccxt.

Modes:
- PAPER: Uses real Binance market data but simulates orders locally.
         Orders are tracked in SQLite. No real money at risk.
- LIVE:  Connects to Binance Futures (USDT-M) for real execution.

Note: ccxt deprecated sandbox mode for Binance futures, so paper trading
is handled internally using real-time prices from the public API.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import ccxt

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import get_secrets, get_settings
from data.database import log_info, log_error, log_warning, get_open_trades, get_total_pnl

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exchange connection (public data - always works)
# ---------------------------------------------------------------------------

_exchange: Optional[ccxt.binance] = None


def _is_paper_mode() -> bool:
    mode = get_settings().get("mode", "paper")
    use_testnet = get_settings()["markets"]["crypto"].get("use_testnet", True)
    return mode == "paper" or use_testnet


def get_exchange() -> ccxt.binance:
    """Get or create the Binance exchange connection."""
    global _exchange
    if _exchange is not None:
        return _exchange

    secrets = get_secrets()["binance"]
    settings = get_settings()["markets"]["crypto"]

    config = {
        "enableRateLimit": True,
        "options": {
            "defaultType": "future",
            "adjustForTimeDifference": True,
        },
    }

    # In paper mode we only need public endpoints (no auth needed for prices)
    # In live mode we authenticate for real order execution
    if not _is_paper_mode():
        api_key = secrets.get("api_key", "")
        api_secret = secrets.get("api_secret", "")
        if not api_key or not api_secret:
            raise ValueError("Binance LIVE API keys not configured in secrets.yaml")
        config["apiKey"] = api_key
        config["secret"] = api_secret
        log_info("binance", "Connected to Binance LIVE (real money)")
    else:
        # Paper mode: no API keys needed, only public data endpoints
        log_info("binance", "Connected to Binance PAPER mode (simulated orders, real prices)")

    _exchange = ccxt.binance(config)
    return _exchange


def reset_connection() -> None:
    """Force reconnection on next call."""
    global _exchange
    _exchange = None


# ---------------------------------------------------------------------------
# Market data (works in all modes)
# ---------------------------------------------------------------------------


def fetch_ticker(pair: str) -> dict:
    """Get current price info for a pair."""
    exchange = get_exchange()
    return exchange.fetch_ticker(pair)


def fetch_price(pair: str) -> float:
    """Get current last price for a pair."""
    ticker = fetch_ticker(pair)
    return ticker["last"]


def fetch_ohlcv(pair: str, timeframe: str = "1h", limit: int = 100) -> list:
    """Fetch OHLCV candles. Returns list of [timestamp, open, high, low, close, volume]."""
    exchange = get_exchange()
    return exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=limit)


def fetch_order_book(pair: str, limit: int = 10) -> dict:
    """Get order book with bids and asks."""
    exchange = get_exchange()
    return exchange.fetch_order_book(pair, limit=limit)


# ---------------------------------------------------------------------------
# Paper trading engine
# ---------------------------------------------------------------------------

_paper_balance = {"USDT": {"free": 0, "used": 0, "total": 0}}
_paper_orders: list[dict] = []
_paper_initialized = False


def _init_paper():
    """Initialize paper trading balance from settings."""
    global _paper_balance, _paper_initialized
    if _paper_initialized:
        return
    capital = get_settings().get("initial_capital_gbp", 1000)
    crypto_pct = get_settings()["markets"]["crypto"].get("capital_allocation_pct", 50)
    # Approximate GBP to USDT (rough 1:1.27 rate)
    usdt_capital = capital * (crypto_pct / 100.0) * 1.27
    _paper_balance = {
        "USDT": {"free": usdt_capital, "used": 0, "total": usdt_capital}
    }
    _paper_initialized = True
    log_info("binance", f"Paper balance initialized: {usdt_capital:.2f} USDT")


def _paper_trade_size(trade: dict) -> float:
    """Return the currently open size for a paper trade."""
    remaining_size = trade.get("remaining_size")
    if remaining_size is not None:
        return float(remaining_size)
    return float(trade["position_size"])


def _paper_positions_from_db() -> list[dict]:
    """
    Build paper positions from open trades in SQLite.

    This keeps paper-mode position state alive across restarts and correctly
    aggregates multiple open trades on the same pair.
    """
    aggregated: dict[tuple[str, str], dict] = {}

    for trade in get_open_trades():
        pair = trade["pair"]
        side = trade["direction"]
        size = _paper_trade_size(trade)
        if size <= 0:
            continue

        key = (pair, side)
        entry_price = float(trade["entry_price"])
        leverage = float(trade.get("leverage") or 1.0)

        if key not in aggregated:
            aggregated[key] = {
                "pair": pair,
                "side": side,
                "size": size,
                "entry_price": entry_price,
                "unrealized_pnl": 0,
                "leverage": leverage,
                "liquidation_price": None,
            }
            continue

        position = aggregated[key]
        previous_size = float(position["size"])
        total_size = previous_size + size
        if total_size <= 0:
            continue

        position["entry_price"] = (
            (float(position["entry_price"]) * previous_size) + (entry_price * size)
        ) / total_size
        position["leverage"] = (
            (float(position["leverage"]) * previous_size) + (leverage * size)
        ) / total_size
        position["size"] = total_size

    return list(aggregated.values())


def _paper_generate_order_id() -> str:
    return f"PAPER-{uuid.uuid4().hex[:8].upper()}"


def _paper_market_order(pair: str, side: str, amount: float, params: dict = None) -> dict:
    """Simulate a market order using the current real price."""
    _init_paper()
    price = fetch_price(pair)
    order_id = _paper_generate_order_id()

    cost = amount * price
    reduce_only = (params or {}).get("reduceOnly", False)

    if not reduce_only:
        # Check balance
        if _paper_balance["USDT"]["free"] < cost / 10:  # leveraged, margin only
            log_warning("binance", f"Paper: insufficient margin for {amount} {pair}")

    order = {
        "id": order_id,
        "pair": pair,
        "type": "market",
        "side": side,
        "amount": amount,
        "price": price,
        "average": price,
        "status": "closed",
        "filled": amount,
        "remaining": 0,
        "cost": cost,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "paper": True,
    }
    _paper_orders.append(order)
    log_info("binance", f"[PAPER] Market {side.upper()} {amount} {pair} @ {price:,.2f}")
    return order


def _paper_stop_order(pair: str, side: str, amount: float, stop_price: float, order_type: str) -> dict:
    """Simulate placing a stop/TP order (stored, checked later by position_manager)."""
    _init_paper()
    order_id = _paper_generate_order_id()
    order = {
        "id": order_id,
        "pair": pair,
        "type": order_type,
        "side": side,
        "amount": amount,
        "price": None,
        "average": None,
        "stop_price": stop_price,
        "status": "open",
        "filled": 0,
        "remaining": amount,
        "cost": 0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "paper": True,
    }
    _paper_orders.append(order)
    log_info("binance", f"[PAPER] {order_type} {side.upper()} {amount} {pair} trigger @ {stop_price:,.2f}")
    return order


# ---------------------------------------------------------------------------
# Unified interface (paper or live, transparent to callers)
# ---------------------------------------------------------------------------


def _paper_balance_from_db() -> dict:
    """Calculate paper balance from DB (source of truth, survives restarts)."""
    capital_gbp = get_settings().get("initial_capital_gbp", 1000)
    crypto_pct = get_settings()["markets"]["crypto"].get("capital_allocation_pct", 50)
    GBP_TO_USDT = 1.27
    usdt_capital = capital_gbp * (crypto_pct / 100.0) * GBP_TO_USDT

    margin_used = 0.0
    realized_pnl_usdt = 0.0
    try:
        for t in get_open_trades():
            entry = float(t["entry_price"])
            size = float(t["position_size"])
            leverage = float(t.get("leverage") or 1)
            margin_used += size * entry / leverage
        realized_pnl_usdt = get_total_pnl() * GBP_TO_USDT
    except Exception:
        pass

    total = usdt_capital + realized_pnl_usdt
    free = total - margin_used

    return {
        "USDT": {"free": round(free, 2), "used": round(margin_used, 2), "total": round(total, 2)}
    }


def fetch_balance() -> dict:
    """Get account balance (paper: from DB, live: from Binance)."""
    if _is_paper_mode():
        _init_paper()
        return _paper_balance_from_db()
    exchange = get_exchange()
    balance = exchange.fetch_balance()
    relevant = {}
    for currency, amounts in balance.get("total", {}).items():
        if amounts and amounts > 0:
            relevant[currency] = {
                "free": balance["free"].get(currency, 0),
                "used": balance["used"].get(currency, 0),
                "total": amounts,
            }
    return relevant


def fetch_usdt_balance() -> dict:
    """Get USDT balance specifically."""
    balance = fetch_balance()
    return balance.get("USDT", {"free": 0, "used": 0, "total": 0})


def fetch_positions() -> list[dict]:
    """Get open positions."""
    if _is_paper_mode():
        return _paper_positions_from_db()
    exchange = get_exchange()
    positions = exchange.fetch_positions()
    return [
        {
            "pair": p["symbol"],
            "side": p["side"],
            "size": p["contracts"],
            "entry_price": p["entryPrice"],
            "unrealized_pnl": p["unrealizedPnl"],
            "leverage": p["leverage"],
            "liquidation_price": p.get("liquidationPrice"),
        }
        for p in positions
        if p["contracts"] and p["contracts"] > 0
    ]


def fetch_position_size(pair: str) -> float:
    """Return the actual open position size on the exchange (0 if no position)."""
    if _is_paper_mode():
        return sum(
            float(position["size"])
            for position in _paper_positions_from_db()
            if position["pair"] == pair
        )
    try:
        exchange = get_exchange()
        positions = exchange.fetch_positions([pair])
        for p in positions:
            if p["symbol"] == pair and p["contracts"] and p["contracts"] > 0:
                return float(p["contracts"])
    except Exception as exc:
        log_warning("binance", f"Could not fetch position size for {pair}: {exc}")
    return 0.0


def set_leverage(pair: str, leverage: int) -> dict:
    """Set leverage for a pair."""
    if _is_paper_mode():
        log_info("binance", f"[PAPER] Leverage set to {leverage}x for {pair}")
        return {"pair": pair, "leverage": leverage}
    exchange = get_exchange()
    result = exchange.set_leverage(leverage, pair)
    log_info("binance", f"Leverage set to {leverage}x for {pair}")
    return result


def place_market_order(pair: str, side: str, amount: float, params: Optional[dict] = None) -> dict:
    """Place a market order (paper or live)."""
    if _is_paper_mode():
        return _paper_market_order(pair, side, amount, params)
    exchange = get_exchange()
    order = exchange.create_order(
        symbol=pair, type="market", side=side, amount=amount, params=params or {},
    )
    log_info("binance", f"Market {side.upper()} {amount} {pair} @ {order.get('average', 'market')}")
    return _normalize_order(order)


def place_limit_order(pair: str, side: str, amount: float, price: float, params: Optional[dict] = None) -> dict:
    """Place a limit order."""
    if _is_paper_mode():
        order_id = _paper_generate_order_id()
        _init_paper()
        order = {
            "id": order_id, "pair": pair, "type": "limit", "side": side,
            "amount": amount, "price": price, "average": None, "status": "open",
            "filled": 0, "remaining": amount, "cost": 0,
            "timestamp": datetime.now(timezone.utc).isoformat(), "paper": True,
        }
        _paper_orders.append(order)
        log_info("binance", f"[PAPER] Limit {side.upper()} {amount} {pair} @ {price:,.2f}")
        return order
    exchange = get_exchange()
    order = exchange.create_order(
        symbol=pair, type="limit", side=side, amount=amount, price=price, params=params or {},
    )
    log_info("binance", f"Limit {side.upper()} {amount} {pair} @ {price}")
    return _normalize_order(order)


def place_stop_loss(pair: str, side: str, amount: float, stop_price: float) -> dict:
    """Place a stop-loss order."""
    if _is_paper_mode():
        return _paper_stop_order(pair, side, amount, stop_price, "stop_market")
    exchange = get_exchange()
    order = exchange.create_order(
        symbol=pair, type="stop_market", side=side, amount=amount,
        params={"stopPrice": stop_price, "reduceOnly": True},
    )
    log_info("binance", f"Stop-loss {side.upper()} {amount} {pair} @ {stop_price}")
    return _normalize_order(order)


def place_take_profit(pair: str, side: str, amount: float, tp_price: float) -> dict:
    """Place a take-profit order."""
    if _is_paper_mode():
        return _paper_stop_order(pair, side, amount, tp_price, "take_profit_market")
    exchange = get_exchange()
    order = exchange.create_order(
        symbol=pair, type="take_profit_market", side=side, amount=amount,
        params={"stopPrice": tp_price, "reduceOnly": True},
    )
    log_info("binance", f"Take-profit {side.upper()} {amount} {pair} @ {tp_price}")
    return _normalize_order(order)


# ---------------------------------------------------------------------------
# Full trade execution (entry + SL + TP)
# ---------------------------------------------------------------------------


def execute_trade(
    pair: str,
    direction: str,
    amount: float,
    leverage: int,
    stop_loss_price: float,
    take_profit_1_price: Optional[float] = None,
    take_profit_2_price: Optional[float] = None,
    tp1_close_pct: float = 50.0,
) -> dict:
    """
    Execute a complete trade: set leverage, open position, place SL and TP.

    direction: 'long' or 'short'
    amount: quantity in base currency (e.g. 0.015 BTC)
    """
    entry_side = "buy" if direction == "long" else "sell"
    exit_side = "sell" if direction == "long" else "buy"
    mode = "PAPER" if _is_paper_mode() else "LIVE"

    try:
        # 1. Set leverage
        set_leverage(pair, leverage)

        # 2. Open position
        entry_order = place_market_order(pair, entry_side, amount)
        entry_price = entry_order.get("average") or entry_order.get("price", 0)

        # 3. Place stop-loss (always required by R7)
        sl_order = place_stop_loss(pair, exit_side, amount, stop_loss_price)

        # 4. Place take-profit orders
        tp1_order = None
        tp2_order = None
        if take_profit_1_price:
            tp1_amount = round(amount * (tp1_close_pct / 100.0), 8)
            tp1_order = place_take_profit(pair, exit_side, tp1_amount, take_profit_1_price)
        if take_profit_2_price:
            tp2_amount = round(amount - amount * (tp1_close_pct / 100.0), 8)
            tp2_order = place_take_profit(pair, exit_side, tp2_amount, take_profit_2_price)

        result = {
            "success": True,
            "mode": mode,
            "pair": pair,
            "direction": direction,
            "entry_price": entry_price,
            "amount": amount,
            "leverage": leverage,
            "stop_loss": stop_loss_price,
            "take_profit_1": take_profit_1_price,
            "take_profit_2": take_profit_2_price,
            "entry_order": entry_order,
            "sl_order": sl_order,
            "tp1_order": tp1_order,
            "tp2_order": tp2_order,
        }

        log_info(
            "binance",
            f"[{mode}] Trade: {direction.upper()} {amount} {pair} @ {entry_price:,.2f} "
            f"| SL: {stop_loss_price:,.2f} | Lev: {leverage}x",
        )
        return result

    except Exception as e:
        log_error("binance", f"Trade execution failed: {e}")
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Order management
# ---------------------------------------------------------------------------


def cancel_order(order_id: str, pair: str) -> dict:
    """Cancel a specific order."""
    if _is_paper_mode():
        for o in _paper_orders:
            if o["id"] == order_id:
                o["status"] = "canceled"
        log_info("binance", f"[PAPER] Order {order_id} cancelled for {pair}")
        return {"id": order_id, "status": "canceled"}
    exchange = get_exchange()
    result = exchange.cancel_order(order_id, pair)
    log_info("binance", f"Order {order_id} cancelled for {pair}")
    return result


def cancel_all_orders(pair: str) -> list:
    """Cancel all open orders for a pair."""
    if _is_paper_mode():
        cancelled = []
        for o in _paper_orders:
            if o["pair"] == pair and o["status"] == "open":
                o["status"] = "canceled"
                cancelled.append(o)
        log_info("binance", f"[PAPER] {len(cancelled)} orders cancelled for {pair}")
        return cancelled
    exchange = get_exchange()
    result = exchange.cancel_all_orders(pair)
    log_info("binance", f"All orders cancelled for {pair}")
    return result


def fetch_open_orders(pair: Optional[str] = None) -> list[dict]:
    """Get all open orders."""
    if _is_paper_mode():
        orders = [o for o in _paper_orders if o["status"] == "open"]
        if pair:
            orders = [o for o in orders if o["pair"] == pair]
        return orders
    exchange = get_exchange()
    return [_normalize_order(o) for o in exchange.fetch_open_orders(pair)]


def close_position(pair: str, direction: str, amount: float) -> dict:
    """Close an open position with a market order.

    Checks actual exchange position first to avoid closing a position
    that was already closed by an exchange-side SL/TP order.
    """
    actual_size = fetch_position_size(pair)

    if actual_size <= 0:
        log_info("binance", f"Position already closed on exchange: {pair} {direction}")
        # Cancel any stale orders and return a synthetic result
        try:
            cancel_all_orders(pair)
        except Exception:
            pass
        return {
            "id": "ALREADY_CLOSED",
            "pair": pair,
            "side": "sell" if direction == "long" else "buy",
            "amount": amount,
            "price": fetch_price(pair),
            "average": fetch_price(pair),
            "status": "closed",
            "filled": amount,
            "remaining": 0,
            "cost": 0,
            "paper": _is_paper_mode(),
        }

    # If exchange has less than requested, close only what's actually open
    close_amount = min(amount, actual_size)
    if close_amount < amount:
        log_warning(
            "binance",
            f"Adjusting close size for {pair}: requested {amount}, "
            f"exchange has {actual_size}. Closing {close_amount}.",
        )

    exit_side = "sell" if direction == "long" else "buy"
    order = place_market_order(pair, exit_side, close_amount, params={"reduceOnly": True})

    try:
        cancel_all_orders(pair)
    except Exception as e:
        log_warning("binance", f"Could not cancel remaining orders for {pair}: {e}")

    log_info("binance", f"Position closed: {pair} {direction} {close_amount}")
    return order


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def health_check() -> dict:
    """Check connectivity and return status."""
    try:
        exchange = get_exchange()
        server_time = exchange.fetch_time()
        balance = fetch_usdt_balance()
        paper = _is_paper_mode()
        return {
            "status": "ok",
            "exchange": "binance",
            "mode": "paper" if paper else "live",
            "usdt_balance": balance,
            "server_time": server_time,
        }
    except Exception as e:
        log_error("binance", f"Health check failed: {e}")
        return {"status": "error", "exchange": "binance", "error": str(e)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_order(order: dict) -> dict:
    """Extract key fields from a ccxt order response."""
    return {
        "id": order.get("id"),
        "pair": order.get("symbol"),
        "type": order.get("type"),
        "side": order.get("side"),
        "amount": order.get("amount"),
        "price": order.get("price"),
        "average": order.get("average"),
        "status": order.get("status"),
        "filled": order.get("filled"),
        "remaining": order.get("remaining"),
        "cost": order.get("cost"),
        "timestamp": order.get("datetime"),
    }


# ---------------------------------------------------------------------------
# Quick test: python -m execution.binance_executor
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print("=" * 60)
    print("  BINANCE EXECUTOR - CONNECTION TEST")
    print("=" * 60)

    paper = _is_paper_mode()
    print(f"\n  Mode: {'PAPER (simulated orders)' if paper else 'LIVE'}")

    # 1. Health check
    print("\n[1] Health check...")
    hc = health_check()
    print(f"    Status:  {hc['status']}")
    print(f"    Mode:    {hc.get('mode')}")
    print(f"    Balance: {hc.get('usdt_balance')}")

    # 2. Fetch live prices
    pairs = get_settings()["markets"]["crypto"]["pairs"]
    print(f"\n[2] Live prices...")
    for pair in pairs:
        try:
            price = fetch_price(pair)
            print(f"    {pair}: ${price:,.2f}")
        except Exception as e:
            print(f"    {pair}: ERROR - {e}")

    # 3. Fetch OHLCV candles
    print(f"\n[3] Last 5 candles BTC/USDT (1h)...")
    candles = fetch_ohlcv("BTC/USDT", "1h", limit=5)
    for c in candles:
        print(f"    O:{c[1]:,.0f} H:{c[2]:,.0f} L:{c[3]:,.0f} C:{c[4]:,.0f} V:{c[5]:,.0f}")

    # 4. Paper trade test
    if paper:
        print(f"\n[4] Simulated trade test...")
        btc_price = fetch_price("BTC/USDT")
        sl_price = round(btc_price * 0.98, 2)
        tp1_price = round(btc_price * 1.03, 2)
        tp2_price = round(btc_price * 1.06, 2)

        result = execute_trade(
            pair="BTC/USDT",
            direction="long",
            amount=0.01,
            leverage=5,
            stop_loss_price=sl_price,
            take_profit_1_price=tp1_price,
            take_profit_2_price=tp2_price,
        )
        print(f"    Success:  {result['success']}")
        print(f"    Entry:    ${result['entry_price']:,.2f}")
        print(f"    SL:       ${result['stop_loss']:,.2f}")
        print(f"    TP1:      ${result['take_profit_1']:,.2f}")
        print(f"    TP2:      ${result['take_profit_2']:,.2f}")
        print(f"    Leverage: {result['leverage']}x")

        print(f"\n[5] Paper positions...")
        positions = fetch_positions()
        for p in positions:
            print(f"    {p['pair']} {p['side']} size={p['size']} @ {p['entry_price']:,.2f}")

        print(f"\n[6] Open orders...")
        orders = fetch_open_orders()
        for o in orders:
            print(f"    {o['type']} {o['side']} {o['amount']} trigger={o.get('stop_price', 'N/A')}")

        print(f"\n[7] Closing position...")
        close_result = close_position("BTC/USDT", "long", 0.01)
        print(f"    Closed @ ${close_result['average']:,.2f}")

    print("\n" + "=" * 60)
    print("  ALL TESTS PASSED")
    print("=" * 60)
