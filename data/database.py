"""SQLite schema and CRUD operations for the 10X Trading System."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent / "trades.db"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_open TEXT NOT NULL,
    timestamp_close TEXT,
    market TEXT NOT NULL,
    pair TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    stop_loss REAL NOT NULL,
    take_profit_1 REAL,
    take_profit_2 REAL,
    position_size REAL NOT NULL,
    leverage REAL DEFAULT 1,
    pnl_absolute REAL,
    pnl_percent REAL,
    status TEXT DEFAULT 'open',
    signals_triggered TEXT,
    mode TEXT DEFAULT 'paper',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    total_capital REAL NOT NULL,
    crypto_capital REAL,
    forex_capital REAL,
    etf_capital REAL,
    open_positions INTEGER,
    daily_pnl REAL,
    weekly_pnl REAL
);

CREATE TABLE IF NOT EXISTS system_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    level TEXT NOT NULL,
    module TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS circuit_breaker_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    rule_triggered TEXT NOT NULL,
    details TEXT,
    resume_after TEXT
);
"""

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA_SQL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Trades CRUD
# ---------------------------------------------------------------------------


def open_trade(
    market: str,
    pair: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    position_size: float,
    leverage: float = 1.0,
    take_profit_1: Optional[float] = None,
    take_profit_2: Optional[float] = None,
    signals_triggered: Optional[dict] = None,
    mode: str = "paper",
    notes: Optional[str] = None,
) -> int:
    sql = """
    INSERT INTO trades
        (timestamp_open, market, pair, direction, entry_price, stop_loss,
         position_size, leverage, take_profit_1, take_profit_2,
         signals_triggered, mode, notes, status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
    """
    signals_json = json.dumps(signals_triggered) if signals_triggered else None
    with get_connection() as conn:
        cur = conn.execute(
            sql,
            (
                _now(), market, pair, direction, entry_price, stop_loss,
                position_size, leverage, take_profit_1, take_profit_2,
                signals_json, mode, notes,
            ),
        )
        return cur.lastrowid


def close_trade(
    trade_id: int,
    exit_price: float,
    status: str,
    pnl_absolute: float,
    pnl_percent: float,
) -> None:
    sql = """
    UPDATE trades
    SET timestamp_close = ?, exit_price = ?, status = ?,
        pnl_absolute = ?, pnl_percent = ?
    WHERE id = ?
    """
    with get_connection() as conn:
        conn.execute(sql, (_now(), exit_price, status, pnl_absolute, pnl_percent, trade_id))


def get_open_trades() -> list[dict]:
    sql = "SELECT * FROM trades WHERE status = 'open' ORDER BY timestamp_open DESC"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(sql).fetchall()]


def get_trade(trade_id: int) -> Optional[dict]:
    sql = "SELECT * FROM trades WHERE id = ?"
    with get_connection() as conn:
        row = conn.execute(sql, (trade_id,)).fetchone()
        return dict(row) if row else None


def get_trades_by_status(status: str) -> list[dict]:
    sql = "SELECT * FROM trades WHERE status = ? ORDER BY timestamp_open DESC"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(sql, (status,)).fetchall()]


def get_all_trades(limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM trades ORDER BY timestamp_open DESC LIMIT ?"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(sql, (limit,)).fetchall()]


def get_trades_since(since_iso: str) -> list[dict]:
    sql = "SELECT * FROM trades WHERE timestamp_open >= ? ORDER BY timestamp_open"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(sql, (since_iso,)).fetchall()]


def count_open_trades() -> int:
    sql = "SELECT COUNT(*) FROM trades WHERE status = 'open'"
    with get_connection() as conn:
        return conn.execute(sql).fetchone()[0]


def get_open_pairs() -> list[str]:
    sql = "SELECT pair FROM trades WHERE status = 'open'"
    with get_connection() as conn:
        return [row[0] for row in conn.execute(sql).fetchall()]


# ---------------------------------------------------------------------------
# Equity Snapshots
# ---------------------------------------------------------------------------


def save_equity_snapshot(
    total_capital: float,
    crypto_capital: float = 0.0,
    forex_capital: float = 0.0,
    etf_capital: float = 0.0,
    open_positions: int = 0,
    daily_pnl: float = 0.0,
    weekly_pnl: float = 0.0,
) -> int:
    sql = """
    INSERT INTO equity_snapshots
        (timestamp, total_capital, crypto_capital, forex_capital,
         etf_capital, open_positions, daily_pnl, weekly_pnl)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    with get_connection() as conn:
        cur = conn.execute(
            sql,
            (_now(), total_capital, crypto_capital, forex_capital,
             etf_capital, open_positions, daily_pnl, weekly_pnl),
        )
        return cur.lastrowid


def get_equity_history(limit: int = 500) -> list[dict]:
    sql = "SELECT * FROM equity_snapshots ORDER BY timestamp DESC LIMIT ?"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(sql, (limit,)).fetchall()]


def get_latest_equity() -> Optional[dict]:
    sql = "SELECT * FROM equity_snapshots ORDER BY timestamp DESC LIMIT 1"
    with get_connection() as conn:
        row = conn.execute(sql).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# System Logs
# ---------------------------------------------------------------------------


def log(level: str, module: str, message: str) -> int:
    sql = "INSERT INTO system_logs (timestamp, level, module, message) VALUES (?, ?, ?, ?)"
    with get_connection() as conn:
        cur = conn.execute(sql, (_now(), level, module, message))
        return cur.lastrowid


def log_info(module: str, message: str) -> int:
    return log("INFO", module, message)


def log_warning(module: str, message: str) -> int:
    return log("WARNING", module, message)


def log_error(module: str, message: str) -> int:
    return log("ERROR", module, message)


def get_recent_logs(limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM system_logs ORDER BY timestamp DESC LIMIT ?"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(sql, (limit,)).fetchall()]


def get_logs_by_level(level: str, limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM system_logs WHERE level = ? ORDER BY timestamp DESC LIMIT ?"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(sql, (level, limit)).fetchall()]


# ---------------------------------------------------------------------------
# Circuit Breaker Events
# ---------------------------------------------------------------------------


def record_circuit_breaker(
    rule_triggered: str,
    details: Optional[str] = None,
    resume_after: Optional[str] = None,
) -> int:
    sql = """
    INSERT INTO circuit_breaker_events (timestamp, rule_triggered, details, resume_after)
    VALUES (?, ?, ?, ?)
    """
    with get_connection() as conn:
        cur = conn.execute(sql, (_now(), rule_triggered, details, resume_after))
        return cur.lastrowid


def get_active_circuit_breaker() -> Optional[dict]:
    sql = """
    SELECT * FROM circuit_breaker_events
    WHERE resume_after > ?
    ORDER BY timestamp DESC LIMIT 1
    """
    with get_connection() as conn:
        row = conn.execute(sql, (_now(),)).fetchone()
        return dict(row) if row else None


def get_circuit_breaker_history(limit: int = 20) -> list[dict]:
    sql = "SELECT * FROM circuit_breaker_events ORDER BY timestamp DESC LIMIT ?"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(sql, (limit,)).fetchall()]


# ---------------------------------------------------------------------------
# Aggregation helpers (for dashboard / reports)
# ---------------------------------------------------------------------------


def get_daily_pnl(date_iso: str) -> float:
    sql = """
    SELECT COALESCE(SUM(pnl_absolute), 0.0) FROM trades
    WHERE timestamp_close LIKE ? AND status != 'open'
    """
    with get_connection() as conn:
        return conn.execute(sql, (f"{date_iso}%",)).fetchone()[0]


def get_weekly_pnl(week_start_iso: str) -> float:
    sql = """
    SELECT COALESCE(SUM(pnl_absolute), 0.0) FROM trades
    WHERE timestamp_close >= ? AND status != 'open'
    """
    with get_connection() as conn:
        return conn.execute(sql, (week_start_iso,)).fetchone()[0]


def get_total_pnl() -> float:
    sql = "SELECT COALESCE(SUM(pnl_absolute), 0.0) FROM trades WHERE status != 'open'"
    with get_connection() as conn:
        return conn.execute(sql).fetchone()[0]


def get_win_rate() -> Optional[float]:
    sql = """
    SELECT
        COUNT(CASE WHEN pnl_absolute > 0 THEN 1 END) AS wins,
        COUNT(*) AS total
    FROM trades WHERE status != 'open'
    """
    with get_connection() as conn:
        row = conn.execute(sql).fetchone()
        if row["total"] == 0:
            return None
        return row["wins"] / row["total"] * 100


def get_profit_factor() -> Optional[float]:
    sql = """
    SELECT
        COALESCE(SUM(CASE WHEN pnl_absolute > 0 THEN pnl_absolute END), 0.0) AS gross_profit,
        COALESCE(SUM(CASE WHEN pnl_absolute < 0 THEN ABS(pnl_absolute) END), 0.0) AS gross_loss
    FROM trades WHERE status != 'open'
    """
    with get_connection() as conn:
        row = conn.execute(sql).fetchone()
        if row["gross_loss"] == 0:
            return None
        return row["gross_profit"] / row["gross_loss"]


# ---------------------------------------------------------------------------
# Init on import (creates DB + tables if they don't exist)
# ---------------------------------------------------------------------------

init_db()
