"""Configured crypto execution router.

Callers should import this module instead of importing a concrete exchange
executor directly. The active exchange is selected by:
settings.yaml -> markets.crypto.exchange
"""

from importlib import import_module
from types import ModuleType
from typing import Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import get_gbp_usd_rate, get_settings


SUPPORTED_EXCHANGES = {
    "binance": "execution.binance_executor",
    "kraken": "execution.kraken_executor",
}

USD_QUOTES = {"USD", "USDT", "USDC"}


def get_exchange_name() -> str:
    crypto_cfg = get_settings().get("markets", {}).get("crypto", {})
    return str(crypto_cfg.get("exchange", "kraken")).strip().lower()


def get_executor() -> ModuleType:
    exchange = get_exchange_name()
    module_name = SUPPORTED_EXCHANGES.get(exchange)
    if not module_name:
        supported = ", ".join(sorted(SUPPORTED_EXCHANGES))
        raise ValueError(f"Unsupported crypto exchange '{exchange}'. Supported: {supported}")
    return import_module(module_name)


def get_quote_currency(pair: Optional[str] = None) -> str:
    if pair and "/" in pair:
        return pair.split("/", 1)[1].upper()

    crypto_cfg = get_settings().get("markets", {}).get("crypto", {})
    pairs = crypto_cfg.get("pairs") or []
    if pairs and "/" in pairs[0]:
        return str(pairs[0]).split("/", 1)[1].upper()
    return str(crypto_cfg.get("quote_currency", "GBP")).upper()


def quote_to_gbp(pair: Optional[str], amount: float) -> float:
    quote = get_quote_currency(pair)
    if quote == "GBP":
        return float(amount)
    if quote in USD_QUOTES:
        return float(amount) / get_gbp_usd_rate()
    return float(amount)


def gbp_to_quote(pair: Optional[str], amount: float) -> float:
    quote = get_quote_currency(pair)
    if quote == "GBP":
        return float(amount)
    if quote in USD_QUOTES:
        return float(amount) * get_gbp_usd_rate()
    return float(amount)


def format_price(pair: Optional[str], price: float) -> str:
    quote = get_quote_currency(pair)
    if quote == "GBP":
        return f"GBP {float(price):,.2f}"
    if quote in USD_QUOTES:
        return f"${float(price):,.2f}"
    return f"{float(price):,.2f} {quote}"


def fetch_quote_balance(pair: Optional[str] = None) -> dict:
    executor = get_executor()
    if hasattr(executor, "fetch_quote_balance"):
        return executor.fetch_quote_balance(pair)

    quote = get_quote_currency(pair)
    balance = executor.fetch_balance()
    amounts = balance.get(quote, {"free": 0, "used": 0, "total": 0})
    return {
        "currency": quote,
        "free": float(amounts.get("free", 0) or 0),
        "used": float(amounts.get("used", 0) or 0),
        "total": float(amounts.get("total", 0) or 0),
    }


def reset_connection() -> None:
    executor = get_executor()
    if hasattr(executor, "reset_connection"):
        executor.reset_connection()


def fetch_ticker(pair: str) -> dict:
    return get_executor().fetch_ticker(pair)


def fetch_price(pair: str) -> float:
    return get_executor().fetch_price(pair)


def fetch_ohlcv(pair: str, timeframe: str = "1h", limit: int = 100) -> list:
    return get_executor().fetch_ohlcv(pair, timeframe=timeframe, limit=limit)


def fetch_order_book(pair: str, limit: int = 10) -> dict:
    return get_executor().fetch_order_book(pair, limit=limit)


def fetch_balance() -> dict:
    return get_executor().fetch_balance()


def fetch_positions() -> list[dict]:
    return get_executor().fetch_positions()


def fetch_position_size(pair: str) -> float:
    return get_executor().fetch_position_size(pair)


def fetch_open_orders(pair: Optional[str] = None) -> list[dict]:
    return get_executor().fetch_open_orders(pair)


def fetch_account_snapshot() -> dict:
    return get_executor().fetch_account_snapshot()


def save_account_snapshot() -> dict:
    return get_executor().save_account_snapshot()


def live_readiness_check() -> dict:
    return get_executor().live_readiness_check()


def validate_live_order(*args, **kwargs) -> dict:
    return get_executor().validate_live_order(*args, **kwargs)


def set_leverage(pair: str, leverage: int) -> dict:
    return get_executor().set_leverage(pair, leverage)


def place_market_order(pair: str, side: str, amount: float, params: Optional[dict] = None) -> dict:
    return get_executor().place_market_order(pair, side, amount, params=params)


def place_limit_order(
    pair: str,
    side: str,
    amount: float,
    price: float,
    params: Optional[dict] = None,
) -> dict:
    return get_executor().place_limit_order(pair, side, amount, price, params=params)


def cancel_order(order_id: str, pair: str) -> dict:
    return get_executor().cancel_order(order_id, pair)


def cancel_all_orders(pair: str) -> list:
    return get_executor().cancel_all_orders(pair)


def close_position(pair: str, direction: str, amount: float) -> dict:
    return get_executor().close_position(pair, direction, amount)


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
    return get_executor().execute_trade(
        pair=pair,
        direction=direction,
        amount=amount,
        leverage=leverage,
        stop_loss_price=stop_loss_price,
        take_profit_1_price=take_profit_1_price,
        take_profit_2_price=take_profit_2_price,
        tp1_close_pct=tp1_close_pct,
    )


def health_check() -> dict:
    return get_executor().health_check()
