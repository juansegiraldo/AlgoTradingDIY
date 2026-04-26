"""Tests for Kraken spot paper execution and live validation."""

from pathlib import Path
import uuid

import pytest

from data import database
from execution import kraken_executor


TEST_DB_DIR = Path(__file__).resolve().parents[1] / ".test-db"


PAPER_SETTINGS = {
    "mode": "paper",
    "live_stage": "stage_10",
    "initial_capital_gbp": 1000,
    "markets": {
        "crypto": {
            "exchange": "kraken",
            "pairs": ["BTC/GBP"],
            "quote_currency": "GBP",
            "capital_allocation_pct": 100,
            "use_testnet": False,
            "leverage_default": 1,
            "leverage_max": 1,
            "allow_short": False,
        }
    },
}


class FakeKrakenExchange:
    def __init__(self, free_gbp=100.0):
        self.free_gbp = free_gbp
        self._markets = {
            "BTC/GBP": {
                "symbol": "BTC/GBP",
                "limits": {
                    "amount": {"min": 0.001},
                    "cost": {"min": 10.0},
                },
                "precision": {"amount": 4, "price": 2},
            }
        }

    def load_markets(self):
        return self._markets

    def fetch_time(self):
        return 1710000000000

    def fetch_balance(self):
        return {
            "free": {"GBP": self.free_gbp},
            "used": {"GBP": 0.0},
            "total": {"GBP": self.free_gbp},
        }

    def amount_to_precision(self, pair, amount):
        return f"{amount:.4f}"

    def price_to_precision(self, pair, price):
        return f"{price:.2f}"


def _reset_db(monkeypatch):
    TEST_DB_DIR.mkdir(exist_ok=True)
    monkeypatch.setattr(database, "DB_PATH", TEST_DB_DIR / f"{uuid.uuid4().hex}.db")
    database.init_db()


def _setup_paper(monkeypatch):
    _reset_db(monkeypatch)
    monkeypatch.setattr(kraken_executor, "get_settings", lambda: PAPER_SETTINGS)
    monkeypatch.setattr(kraken_executor, "fetch_price", lambda pair: 50000.0)
    kraken_executor._paper_initialized = False
    kraken_executor._paper_orders = []


def _live_settings():
    settings = dict(PAPER_SETTINGS)
    settings["mode"] = "semi_auto"
    settings["live_deployment"] = {
        "stage_profiles": {
            "stage_10": {
                "label": "10 GBP",
                "max_operable_capital_gbp": 10,
                "risk_per_trade_pct": 0.25,
                "max_simultaneous_positions": 1,
                "leverage_max": 1,
                "allow_partial_tp": False,
                "allowed_order_types": ["market"],
            }
        }
    }
    return settings


def _setup_live(monkeypatch, free_gbp=100.0):
    fake = FakeKrakenExchange(free_gbp=free_gbp)
    settings = _live_settings()
    monkeypatch.setattr(kraken_executor, "get_settings", lambda: settings)
    monkeypatch.setattr(kraken_executor, "get_secrets", lambda: {"kraken": {"api_key": "k", "api_secret": "s"}})
    monkeypatch.setattr(kraken_executor, "get_live_stage_profile", lambda: settings["live_deployment"]["stage_profiles"]["stage_10"])
    monkeypatch.setattr(kraken_executor, "get_exchange", lambda: fake)
    monkeypatch.setattr(kraken_executor, "fetch_price", lambda pair: 50000.0)
    kraken_executor._markets_cache = None
    return fake


def test_kraken_paper_balance_and_positions_use_gbp(monkeypatch):
    _setup_paper(monkeypatch)
    database.open_trade(
        market="crypto",
        pair="BTC/GBP",
        direction="long",
        entry_price=50000.0,
        stop_loss=49000.0,
        position_size=0.01,
        leverage=1,
    )

    balance = kraken_executor.fetch_quote_balance()
    positions = kraken_executor.fetch_positions()

    assert balance["currency"] == "GBP"
    assert balance["total"] == pytest.approx(1000.0)
    assert balance["used"] == pytest.approx(500.0)
    assert balance["free"] == pytest.approx(500.0)
    assert positions[0]["pair"] == "BTC/GBP"
    assert positions[0]["size"] == pytest.approx(0.01)


def test_kraken_paper_readiness_includes_paper_snapshot(monkeypatch):
    _setup_paper(monkeypatch)

    readiness = kraken_executor.live_readiness_check()

    assert readiness["ready"] is True
    assert readiness["mode"] == "paper"
    assert readiness["snapshot"]["exchange"] == "kraken"
    assert readiness["snapshot"]["free_gbp"] == pytest.approx(1000.0)


def test_kraken_paper_execute_trade_creates_simulated_orders(monkeypatch):
    _setup_paper(monkeypatch)

    result = kraken_executor.execute_trade(
        pair="BTC/GBP",
        direction="long",
        amount=0.01,
        leverage=1,
        stop_loss_price=49000.0,
        take_profit_1_price=51500.0,
        take_profit_2_price=53000.0,
    )

    assert result["success"] is True
    assert result["entry_order"]["paper"] is True
    assert result["sl_order"]["type"] == "stop-loss"
    assert len(kraken_executor.fetch_open_orders("BTC/GBP")) == 3


def test_kraken_rejects_short_and_leverage(monkeypatch):
    _setup_paper(monkeypatch)

    short_result = kraken_executor.execute_trade("BTC/GBP", "short", 0.01, 1, 51000.0)
    leveraged_result = kraken_executor.execute_trade("BTC/GBP", "long", 0.01, 2, 49000.0)

    assert short_result["success"] is False
    assert "shorts" in short_result["error"]
    assert leveraged_result["success"] is False
    assert "1x" in leveraged_result["error"]


def test_kraken_live_validation_rejects_unsupported_pair(monkeypatch):
    _setup_live(monkeypatch)

    result = kraken_executor.validate_live_order("ETH/GBP", 0.01, 1, 1900.0)

    assert result["approved"] is False
    assert "not available on Kraken" in result["reason"]


def test_kraken_live_validation_rejects_below_min_qty(monkeypatch):
    _setup_live(monkeypatch)

    result = kraken_executor.validate_live_order("BTC/GBP", 0.0004, 1, 49000.0)

    assert result["approved"] is False
    assert "minQty" in result["reason"]


def test_kraken_live_validation_rejects_insufficient_gbp(monkeypatch):
    _setup_live(monkeypatch, free_gbp=20.0)

    result = kraken_executor.validate_live_order("BTC/GBP", 0.001, 1, 49000.0)

    assert result["approved"] is False
    assert "Insufficient free balance" in result["reason"]


def test_kraken_live_validation_approves_and_normalizes(monkeypatch):
    _setup_live(monkeypatch, free_gbp=100.0)

    result = kraken_executor.validate_live_order(
        pair="BTC/GBP",
        amount=0.00149,
        leverage=1,
        stop_loss_price=48999.123,
        take_profit_1_price=51555.987,
    )

    assert result["approved"] is True
    assert result["normalized_amount"] == pytest.approx(0.0015)
    assert result["normalized_stop_loss"] == pytest.approx(48999.12)
    assert result["normalized_take_profit_1"] == pytest.approx(51555.99)
    assert result["notional_gbp"] == pytest.approx(75.0)
