"""Fee estimation and extraction helpers for crypto spot trades."""

from __future__ import annotations

from typing import Optional

from config.loader import get_gbp_usd_rate, get_settings


DEFAULT_TAKER_FEE_PCT = 0.40
USD_QUOTES = {"USD", "USDT", "USDC"}


def get_taker_fee_pct() -> float:
    """Return the configured taker fee percentage used for estimates."""
    crypto_cfg = get_settings().get("markets", {}).get("crypto", {})
    fees_cfg = crypto_cfg.get("fees", {})
    raw = fees_cfg.get("taker_fee_pct", DEFAULT_TAKER_FEE_PCT)
    try:
        fee_pct = float(raw)
    except (TypeError, ValueError):
        fee_pct = DEFAULT_TAKER_FEE_PCT
    return max(fee_pct, 0.0)


def _quote_currency(pair: Optional[str]) -> str:
    if pair and "/" in pair:
        return pair.split("/", 1)[1].upper()
    crypto_cfg = get_settings().get("markets", {}).get("crypto", {})
    return str(crypto_cfg.get("quote_currency", "GBP")).upper()


def _base_currency(pair: Optional[str]) -> str:
    if pair and "/" in pair:
        return pair.split("/", 1)[0].upper()
    return ""


def quote_to_gbp_value(pair: Optional[str], amount: float) -> float:
    """Convert pair quote currency amount to GBP for fee reporting."""
    quote = _quote_currency(pair)
    amount = float(amount or 0.0)
    if quote == "GBP":
        return amount
    if quote in USD_QUOTES:
        try:
            return amount / get_gbp_usd_rate()
        except ValueError:
            return amount
    return amount


def currency_to_gbp_value(
    pair: Optional[str],
    currency: Optional[str],
    amount: float,
    *,
    price: Optional[float] = None,
) -> Optional[float]:
    """Convert an exchange-reported fee amount to GBP when possible."""
    if amount is None:
        return None

    currency = str(currency or "").upper()
    amount = float(amount or 0.0)
    if amount == 0:
        return 0.0
    if currency == "GBP":
        return amount
    if currency == _quote_currency(pair):
        return quote_to_gbp_value(pair, amount)
    if currency in USD_QUOTES:
        try:
            return amount / get_gbp_usd_rate()
        except ValueError:
            return amount
    if currency == _base_currency(pair) and price:
        return quote_to_gbp_value(pair, amount * float(price))
    return None


def estimate_fee_gbp(
    pair: Optional[str],
    notional_quote: float,
    *,
    fee_pct: Optional[float] = None,
) -> float:
    """Estimate one side of a spot trade fee in GBP."""
    pct = get_taker_fee_pct() if fee_pct is None else float(fee_pct)
    fee_quote = float(notional_quote or 0.0) * (pct / 100.0)
    return round(quote_to_gbp_value(pair, fee_quote), 6)


def estimate_round_trip_fee_gbp(
    pair: Optional[str],
    notional_quote: float,
    *,
    fee_pct: Optional[float] = None,
) -> float:
    """Estimate entry + exit fees at the same notional value."""
    return round(estimate_fee_gbp(pair, notional_quote, fee_pct=fee_pct) * 2.0, 6)


def extract_order_fee_gbp(
    pair: Optional[str],
    order: Optional[dict],
    *,
    price: Optional[float] = None,
    fallback_notional_quote: Optional[float] = None,
) -> dict:
    """
    Extract an exchange-reported order fee in GBP, or estimate it if absent.

    Returns keys:
      fee_gbp, fee_source, fee_currency, fee_rate_pct
    """
    order = order or {}
    if order.get("fee_gbp") is not None:
        return {
            "fee_gbp": round(float(order.get("fee_gbp") or 0.0), 6),
            "fee_source": order.get("fee_source") or "exchange",
            "fee_currency": order.get("fee_currency") or "GBP",
            "fee_rate_pct": order.get("fee_rate_pct"),
        }

    fee_items = []
    if isinstance(order.get("fee"), dict):
        fee_items.append(order["fee"])
    fee_items.extend(item for item in (order.get("fees") or []) if isinstance(item, dict))

    total_gbp = 0.0
    currencies = set()
    converted_any = False
    for fee in fee_items:
        gbp_value = currency_to_gbp_value(
            pair,
            fee.get("currency"),
            fee.get("cost") or 0.0,
            price=price,
        )
        if gbp_value is None:
            continue
        total_gbp += gbp_value
        currencies.add(str(fee.get("currency") or "unknown").upper())
        converted_any = True

    if converted_any:
        return {
            "fee_gbp": round(total_gbp, 6),
            "fee_source": "exchange",
            "fee_currency": ",".join(sorted(currencies)) or "unknown",
            "fee_rate_pct": None,
        }

    if fallback_notional_quote is None:
        amount = order.get("amount") or order.get("filled")
        average = price or order.get("average") or order.get("price")
        if amount and average:
            fallback_notional_quote = float(amount) * float(average)

    if fallback_notional_quote is not None:
        return {
            "fee_gbp": estimate_fee_gbp(pair, float(fallback_notional_quote)),
            "fee_source": "estimated",
            "fee_currency": "GBP",
            "fee_rate_pct": get_taker_fee_pct(),
        }

    return {
        "fee_gbp": 0.0,
        "fee_source": "unknown",
        "fee_currency": "unknown",
        "fee_rate_pct": None,
    }
