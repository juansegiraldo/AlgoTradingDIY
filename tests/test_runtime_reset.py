"""Tests for local runtime state reset."""

from pathlib import Path
import uuid

from data import database


TEST_DB_DIR = Path(__file__).resolve().parents[1] / ".test-db"


def test_reset_runtime_state_clears_runtime_tables_and_seeds_snapshot(monkeypatch):
    TEST_DB_DIR.mkdir(exist_ok=True)
    monkeypatch.setattr(database, "DB_PATH", TEST_DB_DIR / f"{uuid.uuid4().hex}.db")
    database.init_db()

    database.open_trade(
        market="crypto",
        pair="BTC/GBP",
        direction="long",
        entry_price=50000.0,
        stop_loss=49000.0,
        position_size=0.01,
        leverage=1,
    )
    database.save_equity_snapshot(total_capital=900.0, open_positions=1)
    database.set_system_flag("paused", "true")
    database.record_circuit_breaker("test", "testing")
    database.log_info("test", "old log")

    summary = database.reset_runtime_state(
        total_capital=1000.0,
        mode="paper",
        source="kraken_reset",
        crypto_capital=1000.0,
        free_balance_gbp=1000.0,
    )

    assert summary["before"]["trades"] == 1
    assert summary["after"]["trades"] == 0
    assert summary["after"]["equity_snapshots"] == 1
    assert summary["after"]["system_flags"] == 1
    assert summary["after"]["system_logs"] == 1
    assert summary["after"]["circuit_breaker_events"] == 0
    assert database.count_open_trades() == 0
    assert database.get_system_flag("paused") == "false"

    latest = database.get_latest_equity()
    assert latest["total_capital"] == 1000.0
    assert latest["source"] == "kraken_reset"
    assert latest["open_positions"] == 0
