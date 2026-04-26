"""Tests for risk.position_sizer."""

import pytest

from risk import position_sizer


def test_calculate_position_uses_capital_and_sl_distance(monkeypatch):
    monkeypatch.setattr(position_sizer, "calculate_current_equity", lambda: 1000.0)
    # Use original settings for allocations
    signal = position_sizer.calculate_position(
        pair="BTC/USDT",
        direction="long",
        entry_price=100.0,
        stop_loss=98.0,
        market="crypto",
        leverage=5,
    )

    assert signal["approved"] is True
    # Risk amount = 10% of capital (risk_policies.yaml), but capped by max_position_size_pct
    # market_capital=1000, max_pos=1000*20%*5=1000 GBP, position_value=100/0.02=5000 → cap to 1000
    # risk recalculates to 1000 * 0.02 = 20
    assert signal["risk_amount"] == pytest.approx(20.0, rel=1e-3)
    assert signal["position_size_value"] == pytest.approx(1000.0, rel=1e-2)


def test_calculate_position_rejects_invalid_sl(monkeypatch):
    monkeypatch.setattr(position_sizer, "calculate_current_equity", lambda: 1000.0)
    result = position_sizer.calculate_position(
        pair="BTC/USDT",
        direction="short",
        entry_price=50.0,
        stop_loss=50.0,
        market="crypto",
        leverage=3,
    )

    assert result["approved"] is False
    assert "Invalid SL distance" in result["reason"]


def test_calculate_position_caps_by_max_position_pct(monkeypatch):
    monkeypatch.setattr(position_sizer, "calculate_current_equity", lambda: 1000.0)
    result = position_sizer.calculate_position(
        pair="BTC/USDT",
        direction="long",
        entry_price=100.0,
        stop_loss=99.5,  # 0.5% distance => huge nominal size
        market="crypto",
        leverage=10,
    )

    assert result["approved"] is True
    # Max position value = market_capital(1000) * 20% * leverage(10) = 2000
    assert result["position_size_value"] == pytest.approx(2000.0, rel=1e-3)
    # Risk amount recalculates to size * sl_pct = 2000 * 0.005 = 10
    assert result["risk_amount"] == pytest.approx(10.0, rel=1e-3)


def test_calculate_position_caps_by_available_margin(monkeypatch):
    monkeypatch.setattr(position_sizer, "calculate_current_equity", lambda: 1000.0)
    result = position_sizer.calculate_position(
        pair="ETH/USDT",
        direction="short",
        entry_price=200.0,
        stop_loss=198.0,
        market="crypto",
        leverage=1,
    )

    assert result["approved"] is True
    # Margin required should not exceed market capital (1000 GBP)
    assert result["margin_required"] <= result["market_capital"]
