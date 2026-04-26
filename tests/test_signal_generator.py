"""Tests for signals.signal_generator."""

from signals import signal_generator

evaluate_signal = signal_generator.evaluate_signal


def _analysis(price: float, direction: str):
    # Build a minimal indicator snapshot that still passes validation
    base = {
        "rsi": {"triggered": True, "signal": direction},
        "ema": {"triggered": True, "signal": direction},
        "macd": {"triggered": False, "signal": None},
        "volume": {"triggered": True, "signal": None},
        "trend": {"price": price, "trend": "up" if direction == "long" else "down"},
    }
    return base


def _fixed_settings():
    return {
        "mode": "semi_auto",
        "indicators": {"min_signals_for_entry": 3},
        "position_management": {
            "stop_loss_pct": 2.0,
            "take_profit_1_pct": 3.0,
            "take_profit_2_pct": 6.0,
        },
        "markets": {"crypto": {"leverage_default": 1, "allow_short": True}},
    }


def test_long_signal_levels_use_entry_percentage(monkeypatch):
    monkeypatch.setattr(signal_generator, "get_settings", _fixed_settings)
    analysis = _analysis(price=100.0, direction="long")
    signal = evaluate_signal("BTC/USDT", "1h", "crypto", analysis)

    assert signal is not None
    # stop_loss_pct=2%, take_profit_1_pct=3%, take_profit_2_pct=6% over entry price
    assert signal["stop_loss"] == 98.0
    assert signal["take_profit_1"] == 103.0
    assert signal["take_profit_2"] == 106.0


def _settings_allowing_shorts():
    settings = _fixed_settings()
    settings["markets"]["crypto"]["leverage_default"] = 5
    return settings


def test_short_signal_levels_use_entry_percentage(monkeypatch):
    monkeypatch.setattr(signal_generator, "get_settings", _settings_allowing_shorts)
    analysis = _analysis(price=50.0, direction="short")
    signal = evaluate_signal("ETH/USDT", "4h", "crypto", analysis)

    assert signal is not None
    assert signal["stop_loss"] == 51.0  # 2% por encima de la entrada para shorts
    assert signal["take_profit_1"] == 48.5  # 3% por debajo
    assert signal["take_profit_2"] == 47.0  # 6% por debajo


def test_signal_requires_minimum_confirmations():
    analysis = {
        "rsi": {"triggered": True, "signal": "long"},
        "ema": {"triggered": False, "signal": None},
        "macd": {"triggered": False, "signal": None},
        "volume": {"triggered": False, "signal": None},
        "trend": {"price": 100.0, "trend": "flat"},
    }
    assert evaluate_signal("BTC/USDT", "1h", "crypto", analysis) is None


def test_signal_uses_ema_price_when_trend_missing(monkeypatch):
    monkeypatch.setattr(signal_generator, "get_settings", _settings_allowing_shorts)
    analysis = {
        "rsi": {"triggered": True, "signal": "short"},
        "ema": {"triggered": True, "signal": "short", "value": {"ema_fast": 80.0}},
        "macd": {"triggered": True, "signal": "short"},
        "volume": {"triggered": True, "signal": None},
        "trend": {"price": None, "trend": "down"},
    }
    signal = evaluate_signal("SOL/USDT", "1h", "crypto", analysis)
    assert signal is not None
    assert signal["entry_price"] == 80.0


def test_paper_crypto_uses_atr_exit_profile(monkeypatch):
    monkeypatch.setattr(
        signal_generator,
        "get_settings",
        lambda: {
            "mode": "paper",
            "indicators": {"min_signals_for_entry": 3},
            "position_management": {
                "stop_loss_pct": 2.0,
                "take_profit_1_pct": 3.0,
                "take_profit_2_pct": 6.0,
                "paper_crypto_atr": {
                    "enabled": True,
                    "atr_stop_multiplier": 2.5,
                    "min_stop_loss_pct_by_timeframe": {"1h": 3.0, "4h": 4.0},
                    "max_stop_loss_pct": 8.0,
                    "tp1_r_multiple": 1.5,
                    "tp2_r_multiple": 2.5,
                },
            },
            "markets": {"crypto": {"leverage_default": 1, "allow_short": True}},
        },
    )
    analysis = _analysis(price=100.0, direction="long")
    analysis["volatility"] = {"atr_pct": 2.0}

    signal = evaluate_signal("BTC/GBP", "1h", "crypto", analysis)

    assert signal is not None
    assert signal["exit_model"]["type"] == "paper_atr"
    assert signal["exit_model"]["stop_loss_pct"] == 5.0
    assert signal["stop_loss"] == 95.0
    assert signal["take_profit_1"] == 107.5
    assert signal["take_profit_2"] == 112.5
