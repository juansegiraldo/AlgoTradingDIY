"""Tests for /force helpers and sizing profiles."""

import pytest

from notifications import telegram_bot
from risk import position_sizer


def test_normalize_force_pair_accepts_configured_aliases(monkeypatch):
    monkeypatch.setattr(
        telegram_bot,
        "get_settings",
        lambda: {"markets": {"crypto": {"pairs": ["BTC/USDT", "ETH/USDT", "SOL/USDT"]}}},
    )

    assert telegram_bot._normalize_force_pair(None) == "BTC/USDT"
    assert telegram_bot._normalize_force_pair("eth") == "ETH/USDT"
    assert telegram_bot._normalize_force_pair("solusdt") == "SOL/USDT"
    assert telegram_bot._normalize_force_pair("btc/usdt") == "BTC/USDT"


def test_normalize_force_pair_rejects_unknown_symbol(monkeypatch):
    monkeypatch.setattr(
        telegram_bot,
        "get_settings",
        lambda: {"markets": {"crypto": {"pairs": ["BTC/USDT", "ETH/USDT", "SOL/USDT"]}}},
    )

    with pytest.raises(ValueError, match="Activo no soportado"):
        telegram_bot._normalize_force_pair("xrp")


def test_normalize_force_pair_accepts_gbp_aliases(monkeypatch):
    monkeypatch.setattr(
        telegram_bot,
        "get_settings",
        lambda: {"markets": {"crypto": {"pairs": ["BTC/GBP", "ETH/GBP", "SOL/GBP"]}}},
    )

    assert telegram_bot._normalize_force_pair(None) == "BTC/GBP"
    assert telegram_bot._normalize_force_pair("eth") == "ETH/GBP"
    assert telegram_bot._normalize_force_pair("solgbp") == "SOL/GBP"
    assert telegram_bot._normalize_force_pair("btc/gbp") == "BTC/GBP"


def test_enrich_signal_with_sizing_respects_size_scale(monkeypatch):
    monkeypatch.setattr(
        position_sizer,
        "calculate_position",
        lambda **kwargs: {
            "position_size": 1.0,
            "position_size_value": 500.0,
            "risk_amount": 10.0,
            "risk_pct": 1.0,
            "leverage": kwargs["leverage"],
            "margin_required": 100.0,
            "market_capital": 500.0,
            "approved": True,
            "reason": "Position sized within risk limits",
        },
    )

    signal = position_sizer.enrich_signal_with_sizing({
        "pair": "BTC/USDT",
        "direction": "long",
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "market": "crypto",
        "leverage": 5,
        "size_scale": 0.4,
    })

    assert signal["position_size"] == pytest.approx(0.4)
    assert signal["position_size_value"] == pytest.approx(200.0)
    assert signal["risk_gbp"] == pytest.approx(4.0)
    assert signal["risk_pct"] == pytest.approx(0.4)
    assert signal["margin_required"] == pytest.approx(40.0)
