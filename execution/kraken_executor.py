"""Kraken spot order execution via ccxt.

Modes:
- PAPER: Uses real Kraken market data but simulates spot orders locally.
- LIVE:  Connects to Kraken Spot for real buy/sell execution.

This executor is spot-only: no margin, no leverage, and no shorts.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import ccxt

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import get_gbp_usd_rate, get_live_stage_profile, get_secrets, get_settings
from execution.fees import estimate_fee_gbp, estimate_round_trip_fee_gbp, extract_order_fee_gbp, get_taker_fee_pct
from data.database import (
    count_open_trades,
    get_open_trades,
    get_total_pnl,
    log_error,
    log_info,
    log_warning,
    save_equity_snapshot,
)

logger = logging.getLogger(__name__)
MODULE = "kraken"
DEFAULT_QUOTE = "GBP"

_exchange: Optional[ccxt.kraken] = None
_markets_cache: Optional[dict] = None
_paper_orders: list[dict] = []
_paper_initialized = False


def _crypto_settings() -> dict:
    return get_settings().get("markets", {}).get("crypto", {})


def _is_paper_mode() -> bool:
    mode = get_settings().get("mode", "paper")
    return mode == "paper" or bool(_crypto_settings().get("use_testnet", False))


def _is_live_mode() -> bool:
    return not _is_paper_mode()


def _quote_currency(pair: Optional[str] = None) -> str:
    if pair and "/" in pair:
        return pair.split("/", 1)[1].upper()
    pairs = _crypto_settings().get("pairs") or []
    if pairs and "/" in pairs[0]:
        return pairs[0].split("/", 1)[1].upper()
    return str(_crypto_settings().get("quote_currency", DEFAULT_QUOTE)).upper()


def _base_currency(pair: str) -> str:
    return pair.split("/", 1)[0].upper()


def _quote_to_gbp_value(quote: str, amount: float) -> float:
    quote = quote.upper()
    if quote == "GBP":
        return float(amount)
    if quote in {"USD", "USDT", "USDC"}:
        return float(amount) / get_gbp_usd_rate()
    return float(amount)


def quote_to_gbp(pair: Optional[str], amount: float) -> float:
    return _quote_to_gbp_value(_quote_currency(pair), amount)


def gbp_to_quote(pair: Optional[str], amount: float) -> float:
    quote = _quote_currency(pair)
    if quote == "GBP":
        return float(amount)
    if quote in {"USD", "USDT", "USDC"}:
        return float(amount) * get_gbp_usd_rate()
    return float(amount)


def get_exchange() -> ccxt.kraken:
    """Get or create the Kraken Spot exchange connection."""
    global _exchange
    if _exchange is not None:
        return _exchange

    config = {
        "enableRateLimit": True,
    }

    if _is_live_mode():
        secrets = get_secrets().get("kraken", {})
        api_key = secrets.get("api_key", "")
        api_secret = secrets.get("api_secret", "")
        if not api_key or not api_secret:
            raise ValueError("Kraken LIVE API keys not configured")
        config["apiKey"] = api_key
        config["secret"] = api_secret
        log_info(MODULE, "Connected to Kraken Spot LIVE (real money)")
    else:
        log_info(MODULE, "Connected to Kraken PAPER mode (simulated orders, real prices)")

    _exchange = ccxt.kraken(config)
    return _exchange


def get_markets(force_reload: bool = False) -> dict:
    """Load and cache exchange markets for precision/limit validation."""
    global _markets_cache
    if _markets_cache is not None and not force_reload:
        return _markets_cache
    exchange = get_exchange()
    _markets_cache = exchange.load_markets()
    return _markets_cache


def get_market(pair: str) -> dict:
    markets = get_markets()
    if pair not in markets:
        raise ValueError(f"Symbol {pair} is not available on Kraken")
    return markets[pair]


def reset_connection() -> None:
    """Force reconnection on next call."""
    global _exchange, _markets_cache
    _exchange = None
    _markets_cache = None


def fetch_ticker(pair: str) -> dict:
    return get_exchange().fetch_ticker(pair)


def fetch_price(pair: str) -> float:
    ticker = fetch_ticker(pair)
    return float(ticker["last"])


def fetch_ohlcv(pair: str, timeframe: str = "1h", limit: int = 100) -> list:
    return get_exchange().fetch_ohlcv(pair, timeframe=timeframe, limit=limit)


def fetch_order_book(pair: str, limit: int = 10) -> dict:
    return get_exchange().fetch_order_book(pair, limit=limit)


def _normalize_amount(pair: str, amount: float) -> float:
    return float(get_exchange().amount_to_precision(pair, amount))


def _normalize_price(pair: str, price: Optional[float]) -> Optional[float]:
    if price is None:
        return None
    return float(get_exchange().price_to_precision(pair, price))


def _min_notional(market: dict) -> float:
    limits = market.get("limits", {})
    cost_min = ((limits.get("cost") or {}).get("min"))
    if cost_min is not None:
        return float(cost_min)
    return 0.0


def _min_amount(market: dict) -> float:
    return float(((market.get("limits", {}).get("amount") or {}).get("min")) or 0.0)


def _normalize_balance(balance: dict) -> dict:
    relevant = {}
    totals = balance.get("total", {})
    for currency, total in totals.items():
        if total and float(total) > 0:
            relevant[currency] = {
                "free": float(balance.get("free", {}).get(currency, 0) or 0),
                "used": float(balance.get("used", {}).get(currency, 0) or 0),
                "total": float(total),
            }
    return relevant


def _paper_trade_size(trade: dict) -> float:
    remaining_size = trade.get("remaining_size")
    if remaining_size is not None:
        return float(remaining_size)
    return float(trade["position_size"])


def _paper_balance_from_db() -> dict:
    capital_gbp = float(get_settings().get("initial_capital_gbp", 1000))
    crypto_pct = float(_crypto_settings().get("capital_allocation_pct", 100))
    quote = _quote_currency()
    quote_capital = gbp_to_quote(None, capital_gbp * (crypto_pct / 100.0))

    open_cost = 0.0
    for trade in get_open_trades():
        if trade.get("market") != "crypto":
            continue
        try:
            open_cost += _paper_trade_size(trade) * float(trade["entry_price"])
        except (TypeError, ValueError):
            continue

    realized_quote = gbp_to_quote(None, get_total_pnl())
    total = quote_capital + realized_quote
    free = total - open_cost
    return {
        quote: {
            "free": round(free, 2),
            "used": round(open_cost, 2),
            "total": round(total, 2),
        }
    }


def fetch_balance() -> dict:
    """Get account balance (paper: from DB, live: from Kraken)."""
    if _is_paper_mode():
        _init_paper()
        return _paper_balance_from_db()
    return _normalize_balance(get_exchange().fetch_balance())


def fetch_quote_balance(pair: Optional[str] = None) -> dict:
    quote = _quote_currency(pair)
    balance = fetch_balance()
    amounts = balance.get(quote, {"free": 0, "used": 0, "total": 0})
    return {
        "currency": quote,
        "free": float(amounts.get("free", 0) or 0),
        "used": float(amounts.get("used", 0) or 0),
        "total": float(amounts.get("total", 0) or 0),
    }


def fetch_gbp_balance() -> dict:
    return fetch_quote_balance("BTC/GBP")


def fetch_account_snapshot() -> dict:
    """Fetch current Kraken quote-currency balance and convert to GBP for reporting."""
    balance = fetch_quote_balance()
    quote = balance["currency"]
    total_quote = float(balance.get("total", 0) or 0)
    free_quote = float(balance.get("free", 0) or 0)
    used_quote = float(balance.get("used", 0) or 0)
    settings = get_settings()
    return {
        "exchange": "kraken",
        "mode": "paper" if _is_paper_mode() else settings.get("mode", "paper"),
        "source": "internal" if _is_paper_mode() else "kraken",
        "quote_currency": quote,
        "total_quote": round(total_quote, 4),
        "free_quote": round(free_quote, 4),
        "used_quote": round(used_quote, 4),
        "total_gbp": round(_quote_to_gbp_value(quote, total_quote), 2),
        "free_gbp": round(_quote_to_gbp_value(quote, free_quote), 2),
        "used_gbp": round(_quote_to_gbp_value(quote, used_quote), 2),
        "live_stage": settings.get("live_stage", "stage_10"),
    }


def save_account_snapshot() -> dict:
    snapshot = fetch_account_snapshot()
    save_equity_snapshot(
        total_capital=snapshot["total_gbp"],
        crypto_capital=snapshot["total_gbp"],
        open_positions=count_open_trades(),
        mode=str(snapshot["mode"]),
        source=str(snapshot["source"]),
        free_balance_gbp=snapshot["free_gbp"],
        margin_used_gbp=snapshot["used_gbp"],
        metadata={
            "exchange": snapshot["exchange"],
            "quote_currency": snapshot["quote_currency"],
            "total_quote": snapshot["total_quote"],
            "free_quote": snapshot["free_quote"],
            "used_quote": snapshot["used_quote"],
            "live_stage": snapshot["live_stage"],
        },
    )
    return snapshot


def live_readiness_check() -> dict:
    """Check whether the system is ready to place a real Kraken spot order."""
    settings = get_settings()
    mode = settings.get("mode", "paper")
    if _is_paper_mode():
        snapshot = fetch_account_snapshot()
        return {
            "ready": True,
            "mode": "paper",
            "checks": {
                "mode": "paper",
                "exchange": "kraken",
                "free_gbp": snapshot["free_gbp"],
            },
            "snapshot": snapshot,
        }

    if mode != "semi_auto":
        return {
            "ready": False,
            "mode": mode,
            "error": "Live trading only allowed in semi_auto mode",
            "checks": {"mode": mode},
        }

    secrets = get_secrets().get("kraken", {})
    if not secrets.get("api_key") or not secrets.get("api_secret"):
        return {
            "ready": False,
            "mode": mode,
            "error": "Kraken API key/secret missing",
            "checks": {"credentials": "missing"},
        }

    try:
        exchange = get_exchange()
        server_time = exchange.fetch_time()
        markets = get_markets()
        snapshot = fetch_account_snapshot()
    except Exception as exc:
        return {"ready": False, "mode": mode, "error": str(exc), "checks": {"exchange": "unreachable"}}

    configured_pairs = _crypto_settings().get("pairs", [])
    missing_pairs = [pair for pair in configured_pairs if pair not in markets]
    if missing_pairs:
        return {
            "ready": False,
            "mode": mode,
            "error": f"Configured pair(s) not available on Kraken: {', '.join(missing_pairs)}",
            "checks": {"markets": "missing"},
            "snapshot": snapshot,
        }

    free_gbp = snapshot["free_gbp"]
    return {
        "ready": free_gbp > 0,
        "mode": mode,
        "checks": {
            "exchange": "ok",
            "server_time": server_time,
            "free_gbp": free_gbp,
            "live_stage": settings.get("live_stage", "stage_10"),
        },
        "snapshot": snapshot,
        "error": None if free_gbp > 0 else "No free GBP balance available on Kraken",
    }


def validate_live_order(
    pair: str,
    amount: float,
    leverage: int,
    stop_loss_price: float,
    take_profit_1_price: Optional[float] = None,
    take_profit_2_price: Optional[float] = None,
) -> dict:
    """Validate a Kraken spot order against exchange limits and account balance."""
    readiness = live_readiness_check()
    if not readiness["ready"]:
        return {"approved": False, "reason": readiness.get("error", "Live readiness check failed")}

    if int(leverage) != 1:
        return {"approved": False, "reason": "Kraken spot only supports 1x leverage"}

    try:
        market = get_market(pair)
    except ValueError as exc:
        return {"approved": False, "reason": str(exc)}
    normalized_amount = _normalize_amount(pair, amount)
    if normalized_amount <= 0:
        return {
            "approved": False,
            "reason": f"Normalized amount for {pair} is 0; requested size is below exchange precision",
        }

    min_qty = _min_amount(market)
    if min_qty and normalized_amount < min_qty:
        return {
            "approved": False,
            "reason": f"Amount {normalized_amount} below minQty {min_qty} for {pair}",
        }

    entry_price = fetch_price(pair)
    notional_quote = normalized_amount * entry_price
    min_notional = _min_notional(market)
    if min_notional and notional_quote < min_notional:
        return {
            "approved": False,
            "reason": f"Notional {notional_quote:.4f} below minNotional {min_notional:.4f} for {pair}",
        }

    snapshot = readiness["snapshot"]
    free_quote = float(snapshot["free_quote"])
    estimated_entry_fee_gbp = estimate_fee_gbp(pair, notional_quote)
    estimated_entry_fee_quote = gbp_to_quote(pair, estimated_entry_fee_gbp)
    estimated_total_cost_quote = notional_quote + estimated_entry_fee_quote
    if estimated_total_cost_quote > free_quote:
        quote = snapshot["quote_currency"]
        return {
            "approved": False,
            "reason": (
                f"Insufficient free balance including estimated fee: "
                f"need {estimated_total_cost_quote:.4f} {quote}, "
                f"have {free_quote:.4f} {quote}"
            ),
        }

    return {
        "approved": True,
        "entry_price": entry_price,
        "normalized_amount": normalized_amount,
        "normalized_stop_loss": _normalize_price(pair, stop_loss_price),
        "normalized_take_profit_1": _normalize_price(pair, take_profit_1_price),
        "normalized_take_profit_2": _normalize_price(pair, take_profit_2_price),
        "notional_quote": round(notional_quote, 4),
        "notional_gbp": round(quote_to_gbp(pair, notional_quote), 2),
        "estimated_entry_fee_gbp": round(estimated_entry_fee_gbp, 4),
        "estimated_round_trip_fee_gbp": round(estimate_round_trip_fee_gbp(pair, notional_quote), 4),
        "estimated_total_cost_quote": round(estimated_total_cost_quote, 6),
        "fee_rate_pct": get_taker_fee_pct(),
        "snapshot": snapshot,
    }


def _init_paper() -> None:
    global _paper_initialized
    if _paper_initialized:
        return
    balance = _paper_balance_from_db()
    quote = _quote_currency()
    log_info(MODULE, f"Paper balance initialized: {balance[quote]['total']:.2f} {quote}")
    _paper_initialized = True


def _paper_positions_from_db() -> list[dict]:
    aggregated: dict[tuple[str, str], dict] = {}

    for trade in get_open_trades():
        if trade.get("market") != "crypto":
            continue
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
        position["size"] = total_size

    return list(aggregated.values())


def _paper_generate_order_id() -> str:
    return f"KRAKEN-PAPER-{uuid.uuid4().hex[:8].upper()}"


def _paper_market_order(pair: str, side: str, amount: float, params: Optional[dict] = None) -> dict:
    _init_paper()
    price = fetch_price(pair)
    cost = amount * price
    fee_gbp = estimate_fee_gbp(pair, cost)
    fee_quote = gbp_to_quote(pair, fee_gbp)
    quote = _quote_currency(pair)
    order = {
        "id": _paper_generate_order_id(),
        "pair": pair,
        "symbol": pair,
        "type": "market",
        "side": side,
        "amount": amount,
        "price": price,
        "average": price,
        "status": "closed",
        "filled": amount,
        "remaining": 0,
        "cost": cost,
        "fee": {
            "cost": round(fee_quote, 8),
            "currency": quote,
            "rate": get_taker_fee_pct() / 100.0,
        },
        "fee_gbp": fee_gbp,
        "fee_source": "estimated_paper",
        "fee_currency": quote,
        "fee_rate_pct": get_taker_fee_pct(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "paper": True,
    }
    _paper_orders.append(order)
    log_info(MODULE, f"[PAPER] Market {side.upper()} {amount} {pair} @ {price:,.2f}")
    return order


def _paper_stop_order(pair: str, side: str, amount: float, stop_price: float, order_type: str) -> dict:
    _init_paper()
    order = {
        "id": _paper_generate_order_id(),
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
    log_info(MODULE, f"[PAPER] {order_type} {side.upper()} {amount} {pair} trigger @ {stop_price:,.2f}")
    return order


def fetch_positions() -> list[dict]:
    if _is_paper_mode():
        return _paper_positions_from_db()

    balance = fetch_balance()
    positions = []
    for pair in _crypto_settings().get("pairs", []):
        base = _base_currency(pair)
        amounts = balance.get(base, {})
        size = float(amounts.get("total", 0) or 0)
        if size > 0:
            positions.append(
                {
                    "pair": pair,
                    "side": "long",
                    "size": size,
                    "entry_price": None,
                    "unrealized_pnl": 0,
                    "leverage": 1,
                    "liquidation_price": None,
                }
            )
    return positions


def fetch_position_size(pair: str) -> float:
    if _is_paper_mode():
        return sum(
            float(position["size"])
            for position in _paper_positions_from_db()
            if position["pair"] == pair
        )

    base = _base_currency(pair)
    balance = fetch_balance()
    return float((balance.get(base) or {}).get("total", 0) or 0)


def set_leverage(pair: str, leverage: int) -> dict:
    if int(leverage) != 1:
        raise ValueError("Kraken spot only supports 1x leverage")
    log_info(MODULE, f"Spot leverage fixed at 1x for {pair}")
    return {"pair": pair, "leverage": 1, "spot": True}


def place_market_order(pair: str, side: str, amount: float, params: Optional[dict] = None) -> dict:
    if _is_paper_mode():
        return _paper_market_order(pair, side, amount, params)
    order = get_exchange().create_order(symbol=pair, type="market", side=side, amount=amount)
    log_info(MODULE, f"Market {side.upper()} {amount} {pair} @ {order.get('average', 'market')}")
    return _normalize_order(order)


def place_limit_order(
    pair: str,
    side: str,
    amount: float,
    price: float,
    params: Optional[dict] = None,
) -> dict:
    if _is_paper_mode():
        order = {
            "id": _paper_generate_order_id(),
            "pair": pair,
            "type": "limit",
            "side": side,
            "amount": amount,
            "price": price,
            "average": None,
            "status": "open",
            "filled": 0,
            "remaining": amount,
            "cost": 0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "paper": True,
        }
        _paper_orders.append(order)
        log_info(MODULE, f"[PAPER] Limit {side.upper()} {amount} {pair} @ {price:,.2f}")
        return order
    order = get_exchange().create_order(
        symbol=pair,
        type="limit",
        side=side,
        amount=amount,
        price=price,
        params=params or {},
    )
    log_info(MODULE, f"Limit {side.upper()} {amount} {pair} @ {price}")
    return _normalize_order(order)


def place_stop_loss(pair: str, side: str, amount: float, stop_price: float) -> dict:
    if _is_paper_mode():
        return _paper_stop_order(pair, side, amount, stop_price, "stop-loss")
    order = get_exchange().create_order(
        symbol=pair,
        type="stop-loss",
        side=side,
        amount=amount,
        price=stop_price,
    )
    log_info(MODULE, f"Stop-loss {side.upper()} {amount} {pair} @ {stop_price}")
    return _normalize_order(order)


def place_take_profit(pair: str, side: str, amount: float, tp_price: float) -> dict:
    if _is_paper_mode():
        return _paper_stop_order(pair, side, amount, tp_price, "take-profit")
    order = get_exchange().create_order(
        symbol=pair,
        type="take-profit",
        side=side,
        amount=amount,
        price=tp_price,
    )
    log_info(MODULE, f"Take-profit {side.upper()} {amount} {pair} @ {tp_price}")
    return _normalize_order(order)


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
    """Execute a complete Kraken spot trade."""
    if direction != "long":
        return {"success": False, "error": "Kraken spot does not support shorts"}
    if int(leverage) != 1:
        return {"success": False, "error": "Kraken spot only supports 1x leverage"}

    entry_side = "buy"
    exit_side = "sell"
    mode = "PAPER" if _is_paper_mode() else "LIVE"

    try:
        if _is_live_mode():
            validation = validate_live_order(
                pair=pair,
                amount=amount,
                leverage=leverage,
                stop_loss_price=stop_loss_price,
                take_profit_1_price=take_profit_1_price,
                take_profit_2_price=take_profit_2_price,
            )
            if not validation.get("approved"):
                reason = validation.get("reason", "Live order validation failed")
                log_error(MODULE, f"Live order rejected for {pair}: {reason}")
                return {"success": False, "error": reason}
            amount = float(validation["normalized_amount"])
            stop_loss_price = float(validation["normalized_stop_loss"])
            take_profit_1_price = validation.get("normalized_take_profit_1")
            take_profit_2_price = validation.get("normalized_take_profit_2")

        set_leverage(pair, leverage)
        entry_order = place_market_order(pair, entry_side, amount)
        entry_price = entry_order.get("average") or entry_order.get("price", 0)
        entry_fee = extract_order_fee_gbp(
            pair,
            entry_order,
            price=entry_price,
            fallback_notional_quote=float(amount) * float(entry_price or 0),
        )
        sl_order = place_stop_loss(pair, exit_side, amount, stop_loss_price)

        tp1_order = None
        tp2_order = None
        if _is_paper_mode():
            if take_profit_1_price:
                tp1_amount = round(amount * (tp1_close_pct / 100.0), 8)
                tp1_order = place_take_profit(pair, exit_side, tp1_amount, take_profit_1_price)
            if take_profit_2_price:
                tp2_amount = round(amount - amount * (tp1_close_pct / 100.0), 8)
                tp2_order = place_take_profit(pair, exit_side, tp2_amount, take_profit_2_price)
        elif take_profit_1_price or take_profit_2_price:
            log_info(MODULE, "Kraken Spot take-profit exits are managed by the bot position monitor")

        result = {
            "success": True,
            "mode": mode,
            "pair": pair,
            "direction": direction,
            "entry_price": entry_price,
            "amount": amount,
            "leverage": 1,
            "stop_loss": stop_loss_price,
            "take_profit_1": take_profit_1_price,
            "take_profit_2": take_profit_2_price,
            "entry_fee_gbp": entry_fee["fee_gbp"],
            "entry_fee_source": entry_fee["fee_source"],
            "entry_fee_currency": entry_fee["fee_currency"],
            "fee_rate_pct": entry_fee.get("fee_rate_pct") or get_taker_fee_pct(),
            "estimated_round_trip_fee_gbp": round(
                entry_fee["fee_gbp"]
                + estimate_fee_gbp(pair, float(amount) * float(entry_price or 0)),
                4,
            ),
            "entry_order": entry_order,
            "sl_order": sl_order,
            "tp1_order": tp1_order,
            "tp2_order": tp2_order,
        }
        log_info(
            MODULE,
            f"[{mode}] Trade: LONG {amount} {pair} @ {entry_price:,.2f} | "
            f"SL: {stop_loss_price:,.2f} | Lev: 1x",
        )
        return result
    except Exception as exc:
        log_error(MODULE, f"Trade execution failed: {exc}")
        return {"success": False, "error": str(exc)}


def cancel_order(order_id: str, pair: str) -> dict:
    if _is_paper_mode():
        for order in _paper_orders:
            if order["id"] == order_id:
                order["status"] = "canceled"
        log_info(MODULE, f"[PAPER] Order {order_id} cancelled for {pair}")
        return {"id": order_id, "status": "canceled"}
    result = get_exchange().cancel_order(order_id, pair)
    log_info(MODULE, f"Order {order_id} cancelled for {pair}")
    return result


def cancel_all_orders(pair: str) -> list:
    if _is_paper_mode():
        cancelled = []
        for order in _paper_orders:
            if order["pair"] == pair and order["status"] == "open":
                order["status"] = "canceled"
                cancelled.append(order)
        log_info(MODULE, f"[PAPER] {len(cancelled)} orders cancelled for {pair}")
        return cancelled

    exchange = get_exchange()
    try:
        result = exchange.cancel_all_orders(pair)
        log_info(MODULE, f"All orders cancelled for {pair}")
        return result
    except Exception:
        cancelled = []
        for order in exchange.fetch_open_orders(pair):
            cancelled.append(exchange.cancel_order(order["id"], pair))
        log_info(MODULE, f"{len(cancelled)} open orders cancelled for {pair}")
        return cancelled


def fetch_open_orders(pair: Optional[str] = None) -> list[dict]:
    if _is_paper_mode():
        orders = [order for order in _paper_orders if order["status"] == "open"]
        if pair:
            orders = [order for order in orders if order["pair"] == pair]
        return orders
    return [_normalize_order(order) for order in get_exchange().fetch_open_orders(pair)]


def close_position(pair: str, direction: str, amount: float) -> dict:
    if direction != "long":
        raise ValueError("Kraken spot can only close long spot positions")

    actual_size = fetch_position_size(pair)
    if actual_size <= 0:
        log_info(MODULE, f"Position already closed on exchange: {pair} {direction}")
        try:
            cancel_all_orders(pair)
        except Exception:
            pass
        price = fetch_price(pair)
        return {
            "id": "ALREADY_CLOSED",
            "pair": pair,
            "side": "sell",
            "amount": amount,
            "price": price,
            "average": price,
            "status": "closed",
            "filled": amount,
            "remaining": 0,
            "cost": 0,
            "paper": _is_paper_mode(),
        }

    close_amount = min(amount, actual_size)
    if close_amount < amount:
        log_warning(
            MODULE,
            f"Adjusting close size for {pair}: requested {amount}, "
            f"exchange has {actual_size}. Closing {close_amount}.",
        )

    order = place_market_order(pair, "sell", close_amount)
    try:
        cancel_all_orders(pair)
    except Exception as exc:
        log_warning(MODULE, f"Could not cancel remaining orders for {pair}: {exc}")
    log_info(MODULE, f"Position closed: {pair} long {close_amount}")
    return order


def health_check() -> dict:
    try:
        exchange = get_exchange()
        server_time = exchange.fetch_time()
        balance = fetch_quote_balance()
        paper = _is_paper_mode()
        return {
            "status": "ok",
            "exchange": "kraken",
            "mode": "paper" if paper else "live",
            "quote_balance": balance,
            "server_time": server_time,
            "readiness": live_readiness_check(),
        }
    except Exception as exc:
        log_error(MODULE, f"Health check failed: {exc}")
        return {"status": "error", "exchange": "kraken", "error": str(exc)}


def _normalize_order(order: dict) -> dict:
    pair = order.get("symbol")
    price = order.get("average") or order.get("price")
    fallback_notional = order.get("cost")
    if fallback_notional is None and order.get("amount") and price:
        fallback_notional = float(order["amount"]) * float(price)
    fee = extract_order_fee_gbp(
        pair,
        order,
        price=price,
        fallback_notional_quote=fallback_notional,
    )
    normalized = {
        "id": order.get("id"),
        "pair": pair,
        "symbol": pair,
        "type": order.get("type"),
        "side": order.get("side"),
        "amount": order.get("amount"),
        "price": order.get("price"),
        "average": order.get("average"),
        "status": order.get("status"),
        "filled": order.get("filled"),
        "remaining": order.get("remaining"),
        "cost": order.get("cost"),
        "fee": order.get("fee"),
        "fees": order.get("fees"),
        "timestamp": order.get("datetime"),
    }
    normalized.update(fee)
    return normalized


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print("=" * 60)
    print("  KRAKEN EXECUTOR - CONNECTION TEST")
    print("=" * 60)
    print(f"\n  Mode: {'PAPER (simulated orders)' if _is_paper_mode() else 'LIVE'}")

    hc = health_check()
    print(f"\n[1] Health check: {hc['status']}")
    print(f"    Mode:    {hc.get('mode')}")
    print(f"    Balance: {hc.get('quote_balance')}")

    pairs = _crypto_settings().get("pairs", ["BTC/GBP"])
    print("\n[2] Live prices...")
    for pair in pairs:
        try:
            print(f"    {pair}: {fetch_price(pair):,.2f}")
        except Exception as exc:
            print(f"    {pair}: ERROR - {exc}")

    print("\n" + "=" * 60)
    print("  TEST COMPLETE")
    print("=" * 60)
