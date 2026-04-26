"""Tests for risk.risk_manager."""

from risk import risk_manager


def _base_signal(**overrides):
    signal = {
        "pair": "BTC/USDT",
        "direction": "long",
        "entry_price": 100.0,
        "stop_loss": 99.0,
        "position_size_value": 100.0,
        "market": "crypto",
    }
    signal.update(overrides)
    return signal


def _mock_env(monkeypatch, *, capital=1000.0, open_positions=0, open_pairs=None):
    monkeypatch.setattr(risk_manager, "_get_current_capital", lambda: capital)
    monkeypatch.setattr(risk_manager, "count_open_trades", lambda: open_positions)
    monkeypatch.setattr(risk_manager, "get_open_pairs", lambda: open_pairs or [])
    monkeypatch.setattr(risk_manager, "is_circuit_breaker_active", lambda: None)
    monkeypatch.setattr(
        risk_manager,
        "get_risk_policies",
        lambda: {
            "risk_policies": {
                "max_loss_per_trade_pct": 1,
                "max_simultaneous_positions": 3,
                "correlated_pairs": [["BTC/USDT", "ETH/USDT"]],
                "require_stop_loss": True,
            }
        },
    )


def test_validate_trade_passes_when_risk_within_limits(monkeypatch):
    _mock_env(monkeypatch, capital=1000.0, open_positions=0)
    result = risk_manager.validate_trade(_base_signal())

    assert result["approved"] is True
    assert not result["rejection_reasons"]


def test_validate_trade_rejects_when_risk_exceeds_policy(monkeypatch):
    _mock_env(monkeypatch, capital=1000.0, open_positions=0)
    risky_signal = _base_signal(position_size_value=1500.0)

    result = risk_manager.validate_trade(risky_signal)

    assert result["approved"] is False
    assert any("R1 FAIL" in reason for reason in result["rejection_reasons"])


def test_validate_trade_rejects_max_positions(monkeypatch):
    _mock_env(monkeypatch, capital=1000.0, open_positions=3)
    result = risk_manager.validate_trade(_base_signal())

    assert result["approved"] is False
    assert any("R5 FAIL" in reason for reason in result["rejection_reasons"])


def test_validate_trade_rejects_correlated_pair(monkeypatch):
    _mock_env(monkeypatch, capital=1000.0, open_positions=1, open_pairs=["ETH/USDT"])
    result = risk_manager.validate_trade(_base_signal(pair="BTC/USDT"))

    assert result["approved"] is False
    assert any("R6 FAIL" in reason for reason in result["rejection_reasons"])


def test_validate_trade_rejects_bad_stop_loss(monkeypatch):
    _mock_env(monkeypatch, capital=1000.0, open_positions=0)
    bad_sl = _base_signal(stop_loss=101.0)

    result = risk_manager.validate_trade(bad_sl)

    assert result["approved"] is False
    assert any("R7 FAIL" in reason for reason in result["rejection_reasons"])
