"""Regression tests for paper position handling."""

from pathlib import Path
import uuid

import pytest

from data import database
from execution import binance_executor, position_manager


PAPER_SETTINGS = {
    "mode": "paper",
    "initial_capital_gbp": 1000,
    "markets": {
        "crypto": {
            "capital_allocation_pct": 50,
            "use_testnet": True,
        }
    },
}


TEST_DB_DIR = Path(__file__).resolve().parents[1] / ".test-db"


def _reset_test_state(monkeypatch):
    TEST_DB_DIR.mkdir(exist_ok=True)
    monkeypatch.setattr(database, "DB_PATH", TEST_DB_DIR / f"{uuid.uuid4().hex}.db")
    database.init_db()

    monkeypatch.setattr(binance_executor, "get_settings", lambda: PAPER_SETTINGS)
    monkeypatch.setattr(binance_executor, "fetch_price", lambda pair: 100.0)
    monkeypatch.setattr(position_manager, "fetch_price", lambda pair: 100.0)
    monkeypatch.setattr(
        position_manager,
        "close_position",
        lambda pair, direction, amount: {
            "id": "TEST_CLOSE",
            "pair": pair,
            "side": "sell" if direction == "long" else "buy",
            "amount": amount,
            "average": 100.0,
            "status": "closed",
        },
    )
    monkeypatch.setattr(position_manager, "send_text_sync", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        position_manager,
        "send_close_notification_sync",
        lambda *args, **kwargs: None,
    )

    binance_executor._paper_initialized = False
    binance_executor._paper_orders = []
    position_manager._tp1_hit_trades.clear()
    position_manager._remaining_sizes.clear()
    position_manager._tp1_state_loaded = False


def test_fetch_position_size_uses_db_state_in_paper_mode(monkeypatch):
    _reset_test_state(monkeypatch)

    first_id = database.open_trade(
        market="crypto",
        pair="BTC/USDT",
        direction="long",
        entry_price=100.0,
        stop_loss=95.0,
        position_size=1.5,
        leverage=10,
        take_profit_1=105.0,
        take_profit_2=110.0,
    )
    database.open_trade(
        market="crypto",
        pair="BTC/USDT",
        direction="long",
        entry_price=102.0,
        stop_loss=97.0,
        position_size=2.0,
        leverage=5,
        take_profit_1=107.0,
        take_profit_2=112.0,
    )
    database.update_tp1_state(first_id, remaining_size=0.5)

    positions = binance_executor.fetch_positions()
    btc_position = next(position for position in positions if position["pair"] == "BTC/USDT")

    assert btc_position["side"] == "long"
    assert btc_position["size"] == pytest.approx(2.5)
    assert binance_executor.fetch_position_size("BTC/USDT") == pytest.approx(2.5)


def test_manual_close_keeps_other_same_pair_trades_open(monkeypatch):
    _reset_test_state(monkeypatch)

    trade_to_close = database.open_trade(
        market="crypto",
        pair="BTC/USDT",
        direction="long",
        entry_price=100.0,
        stop_loss=95.0,
        position_size=1.0,
        leverage=10,
        take_profit_1=105.0,
        take_profit_2=110.0,
    )
    trade_to_keep = database.open_trade(
        market="crypto",
        pair="BTC/USDT",
        direction="long",
        entry_price=101.0,
        stop_loss=96.0,
        position_size=1.0,
        leverage=10,
        take_profit_1=106.0,
        take_profit_2=111.0,
    )

    result = position_manager.close_trade_manual(trade_to_close)

    assert result["success"] is True

    position_manager.check_open_positions()

    assert database.get_trade(trade_to_close)["status"] == "closed_manual"
    assert database.get_trade(trade_to_keep)["status"] == "open"
    assert database.count_open_trades() == 1
