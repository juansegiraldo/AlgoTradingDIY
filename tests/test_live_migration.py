"""Tests for live-stage validation, Binance snapshots, and auto-pause behavior."""

from pathlib import Path
import uuid

from data import database
from execution import binance_executor
import pipeline


TEST_DB_DIR = Path(__file__).resolve().parents[1] / ".test-db"


class FakeExchange:
    def __init__(self):
        self._markets = {
            "BTC/USDT": {
                "symbol": "BTC/USDT",
                "limits": {
                    "amount": {"min": 0.001},
                    "cost": {"min": 100.0},
                    "leverage": {"max": 10},
                },
                "precision": {"amount": 3, "price": 2},
                "info": {"maxLeverage": "10", "filters": []},
            }
        }

    def load_markets(self):
        return self._markets

    def fetch_time(self):
        return 1710000000000

    def fetch_balance(self):
        return {
            "free": {"USDT": 50.0},
            "used": {"USDT": 10.0},
            "total": {"USDT": 60.0},
        }

    def amount_to_precision(self, pair, amount):
        return f"{amount:.3f}"

    def price_to_precision(self, pair, price):
        return f"{price:.2f}"


def _live_settings():
    return {
        "mode": "semi_auto",
        "live_stage": "stage_10",
        "initial_capital_gbp": 1000,
        "live_deployment": {
            "auto_pause_on_live_failures": True,
            "stage_profiles": {
                "stage_10": {
                    "label": "10 GBP",
                    "max_operable_capital_gbp": 10,
                    "risk_per_trade_pct": 0.25,
                    "max_simultaneous_positions": 1,
                    "leverage_max": 2,
                    "allow_partial_tp": False,
                    "allowed_order_types": ["market"],
                }
            },
        },
        "markets": {
            "crypto": {
                "use_testnet": False,
                "pairs": ["BTC/USDT"],
            }
        },
    }


def _reset_db(monkeypatch):
    TEST_DB_DIR.mkdir(exist_ok=True)
    monkeypatch.setattr(database, "DB_PATH", TEST_DB_DIR / f"{uuid.uuid4().hex}.db")
    database.init_db()


def _setup_live_exchange(monkeypatch):
    fake = FakeExchange()
    monkeypatch.setattr(binance_executor, "get_settings", _live_settings)
    monkeypatch.setattr(binance_executor, "get_live_stage_profile", lambda: _live_settings()["live_deployment"]["stage_profiles"]["stage_10"])
    monkeypatch.setattr(binance_executor, "get_exchange", lambda: fake)
    binance_executor._markets_cache = None
    return fake


def test_validate_live_order_rejects_below_min_qty(monkeypatch):
    _setup_live_exchange(monkeypatch)
    monkeypatch.setattr(binance_executor, "fetch_price", lambda pair: 70000.0)

    result = binance_executor.validate_live_order(
        pair="BTC/USDT",
        amount=0.0004,
        leverage=2,
        stop_loss_price=68000.0,
    )

    assert result["approved"] is False
    assert "minQty" in result["reason"]


def test_validate_live_order_rejects_below_min_notional(monkeypatch):
    _setup_live_exchange(monkeypatch)
    monkeypatch.setattr(binance_executor, "fetch_price", lambda pair: 70000.0)

    result = binance_executor.validate_live_order(
        pair="BTC/USDT",
        amount=0.001,
        leverage=2,
        stop_loss_price=68000.0,
    )

    assert result["approved"] is False
    assert "minNotional" in result["reason"]


def test_validate_live_order_rejects_insufficient_balance(monkeypatch):
    _setup_live_exchange(monkeypatch)
    monkeypatch.setattr(binance_executor, "fetch_price", lambda pair: 70000.0)

    result = binance_executor.validate_live_order(
        pair="BTC/USDT",
        amount=0.003,
        leverage=2,
        stop_loss_price=68000.0,
    )

    assert result["approved"] is False
    assert "Insufficient free balance" in result["reason"]


def test_validate_live_order_normalizes_price_and_amount(monkeypatch):
    _setup_live_exchange(monkeypatch)
    monkeypatch.setattr(binance_executor, "fetch_price", lambda pair: 100000.0)

    result = binance_executor.validate_live_order(
        pair="BTC/USDT",
        amount=0.0014,
        leverage=2,
        stop_loss_price=98765.4321,
        take_profit_1_price=101234.5678,
    )

    assert result["approved"] is True
    assert result["normalized_amount"] == 0.001
    assert result["normalized_stop_loss"] == 98765.43
    assert result["normalized_take_profit_1"] == 101234.57


def test_validate_live_order_rejects_stage_leverage(monkeypatch):
    _setup_live_exchange(monkeypatch)
    monkeypatch.setattr(binance_executor, "fetch_price", lambda pair: 100000.0)

    result = binance_executor.validate_live_order(
        pair="BTC/USDT",
        amount=0.001,
        leverage=3,
        stop_loss_price=98000.0,
    )

    assert result["approved"] is False
    assert "live stage max" in result["reason"]


def test_save_account_snapshot_persists_binance_source(monkeypatch):
    _reset_db(monkeypatch)
    _setup_live_exchange(monkeypatch)

    snapshot = binance_executor.save_account_snapshot()
    latest = database.get_latest_equity()

    assert snapshot["source"] == "binance"
    assert latest["source"] == "binance"
    assert latest["free_balance_gbp"] > 0
    assert latest["margin_used_gbp"] > 0


def test_execute_signal_auto_pauses_on_live_failure(monkeypatch):
    _reset_db(monkeypatch)
    settings = _live_settings()
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline, "open_trade", lambda **kwargs: 1)
    monkeypatch.setattr(pipeline, "_notify_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "_notify_execution", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "_is_system_paused", lambda: False)
    monkeypatch.setattr(
        pipeline,
        "_execute_on_broker",
        lambda signal, market: {"success": False, "error": "minNotional rejected"},
    )

    result = pipeline.execute_signal(
        {
            "pair": "BTC/USDT",
            "market": "crypto",
            "direction": "long",
            "entry_price": 100000.0,
            "stop_loss": 98000.0,
            "position_size": 0.001,
            "leverage": 2,
        }
    )

    assert result["success"] is False
    assert database.get_system_flag("paused") == "true"
