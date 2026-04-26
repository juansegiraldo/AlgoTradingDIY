"""Tests for configured crypto exchange routing."""

from execution import crypto_executor
import pipeline
from signals import scanner
from notifications import report_generator


def test_crypto_executor_selects_kraken_from_settings(monkeypatch):
    monkeypatch.setattr(
        crypto_executor,
        "get_settings",
        lambda: {"markets": {"crypto": {"exchange": "kraken", "pairs": ["BTC/GBP"]}}},
    )

    assert crypto_executor.get_exchange_name() == "kraken"
    assert crypto_executor.get_executor().__name__ == "execution.kraken_executor"


def test_usd_quote_conversion_uses_env_fx_rate(monkeypatch):
    monkeypatch.setenv("GBP_USD_RATE", "2.0")

    assert crypto_executor.quote_to_gbp("BTC/USDT", 200.0) == 100.0
    assert crypto_executor.gbp_to_quote("BTC/USDT", 100.0) == 200.0


def test_pipeline_routes_crypto_execution_to_crypto_executor(monkeypatch):
    called = {}

    def fake_execute_trade(**kwargs):
        called.update(kwargs)
        return {"success": True}

    monkeypatch.setattr(crypto_executor, "execute_trade", fake_execute_trade)

    result = pipeline._execute_on_broker(
        {
            "pair": "BTC/GBP",
            "direction": "long",
            "position_size": 0.01,
            "leverage": 1,
            "stop_loss": 49000.0,
            "take_profit_1": 51500.0,
            "take_profit_2": 53000.0,
        },
        "crypto",
    )

    assert result["success"] is True
    assert called["pair"] == "BTC/GBP"
    assert called["leverage"] == 1


def test_scanner_fetches_crypto_ohlcv_through_router(monkeypatch):
    monkeypatch.setattr(crypto_executor, "fetch_ohlcv", lambda pair, timeframe, limit: [pair, timeframe, limit])

    assert scanner._fetch_ohlcv("BTC/GBP", "1h", "crypto", 25) == ["BTC/GBP", "1h", 25]


def test_readiness_report_uses_configured_exchange(monkeypatch):
    monkeypatch.setattr(
        crypto_executor,
        "live_readiness_check",
        lambda: {
            "ready": True,
            "snapshot": {"exchange": "kraken", "free_gbp": 50.0, "total_gbp": 100.0},
        },
    )
    monkeypatch.setattr(crypto_executor, "fetch_positions", lambda: [])
    monkeypatch.setattr(crypto_executor, "fetch_open_orders", lambda: [])
    monkeypatch.setattr(crypto_executor, "get_exchange_name", lambda: "kraken")
    monkeypatch.setattr(
        report_generator,
        "get_risk_status",
        lambda: {"circuit_breaker_active": False},
    )
    monkeypatch.setattr(
        report_generator,
        "get_settings",
        lambda: {
            "mode": "paper",
            "live_stage": "stage_10",
            "markets": {"crypto": {"exchange": "kraken", "pairs": ["BTC/GBP"]}},
        },
    )

    report = report_generator.generate_readiness_report()

    assert "Saldo Kraken: GBP 100.00" in report
    assert "Pares habilitados: BTC/GBP" in report
