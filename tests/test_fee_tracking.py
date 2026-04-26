"""Fee and net-PnL tracking regressions."""

from pathlib import Path
import uuid

import pytest

from data import database
from execution import position_manager
from risk import position_sizer


TEST_DB_DIR = Path(__file__).resolve().parents[1] / ".test-db"


def _reset_db(monkeypatch):
    TEST_DB_DIR.mkdir(exist_ok=True)
    monkeypatch.setattr(database, "DB_PATH", TEST_DB_DIR / f"{uuid.uuid4().hex}.db")
    database.init_db()


def test_manual_close_records_gross_fees_and_net_pnl(monkeypatch):
    _reset_db(monkeypatch)
    monkeypatch.setattr(position_manager, "fetch_price", lambda pair: 101.0)
    monkeypatch.setattr(
        position_manager,
        "close_position",
        lambda pair, direction, amount: {
            "id": "CLOSE1",
            "pair": pair,
            "side": "sell",
            "amount": amount,
            "average": 101.0,
            "fee_gbp": 0.04,
            "fee_source": "exchange",
        },
    )
    monkeypatch.setattr(position_manager, "send_text_sync", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        position_manager,
        "send_close_notification_sync",
        lambda *args, **kwargs: None,
    )

    trade_id = database.open_trade(
        market="crypto",
        pair="SOL/GBP",
        direction="long",
        entry_price=100.0,
        stop_loss=98.0,
        position_size=1.0,
        leverage=1,
        mode="paper",
        entry_fee_gbp=0.04,
    )

    result = position_manager.close_trade_manual(trade_id)
    saved = database.get_trade(trade_id)

    assert result["success"] is True
    assert result["pnl_gross_gbp"] == pytest.approx(1.0)
    assert result["total_fees_gbp"] == pytest.approx(0.08)
    assert result["pnl_gbp"] == pytest.approx(0.92)
    assert saved["pnl_gross_gbp"] == pytest.approx(1.0)
    assert saved["entry_fee_gbp"] == pytest.approx(0.04)
    assert saved["exit_fee_gbp"] == pytest.approx(0.04)
    assert saved["total_fees_gbp"] == pytest.approx(0.08)
    assert saved["pnl_absolute"] == pytest.approx(0.92)


def test_position_sizer_adds_round_trip_fee_estimate(monkeypatch):
    monkeypatch.setattr(position_sizer, "calculate_current_equity", lambda: 10.0)
    monkeypatch.setattr(
        position_sizer,
        "get_settings",
        lambda: {
            "mode": "paper",
            "markets": {
                "crypto": {
                    "capital_allocation_pct": 100,
                    "fees": {"taker_fee_pct": 0.40},
                }
            },
            "position_management": {"max_position_size_pct": 100},
        },
    )
    monkeypatch.setattr(
        position_sizer,
        "get_risk_policies",
        lambda: {"risk_policies": {"max_loss_per_trade_pct": 10}},
    )

    result = position_sizer.calculate_position(
        pair="SOL/GBP",
        direction="long",
        entry_price=100.0,
        stop_loss=98.0,
        market="crypto",
        leverage=1,
    )

    assert result["approved"] is True
    assert result["position_size_value"] == pytest.approx(10.0)
    assert result["estimated_entry_fee_gbp"] == pytest.approx(0.04)
    assert result["estimated_round_trip_fee_gbp"] == pytest.approx(0.08)
    assert result["fee_breakeven_pct"] == pytest.approx(0.8)
