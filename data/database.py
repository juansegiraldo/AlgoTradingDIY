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
    remaining_size REAL,
    tp1_hit_at TEXT,
    leverage REAL DEFAULT 1,
    entry_fee_gbp REAL DEFAULT 0,
    exit_fee_gbp REAL DEFAULT 0,
    total_fees_gbp REAL DEFAULT 0,
    realized_partial_pnl_gbp REAL DEFAULT 0,
    pnl_gross_gbp REAL,
    pnl_absolute REAL,
    pnl_percent REAL,
    fee_details_json TEXT,
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
    weekly_pnl REAL,
    mode TEXT DEFAULT 'paper',
    source TEXT DEFAULT 'internal',
    free_balance_gbp REAL,
    margin_used_gbp REAL,
    metadata_json TEXT
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

CREATE TABLE IF NOT EXISTS system_flags (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
        # Migrate existing DBs: add remaining_size and tp1_hit_at if missing
        cursor = conn.execute("PRAGMA table_info(trades)")
        columns = {row[1] for row in cursor.fetchall()}
        if "remaining_size" not in columns:
            conn.execute("ALTER TABLE trades ADD COLUMN remaining_size REAL")
        if "tp1_hit_at" not in columns:
            conn.execute("ALTER TABLE trades ADD COLUMN tp1_hit_at TEXT")
        if "entry_fee_gbp" not in columns:
            conn.execute("ALTER TABLE trades ADD COLUMN entry_fee_gbp REAL DEFAULT 0")
        if "exit_fee_gbp" not in columns:
            conn.execute("ALTER TABLE trades ADD COLUMN exit_fee_gbp REAL DEFAULT 0")
        if "total_fees_gbp" not in columns:
            conn.execute("ALTER TABLE trades ADD COLUMN total_fees_gbp REAL DEFAULT 0")
        if "realized_partial_pnl_gbp" not in columns:
            conn.execute("ALTER TABLE trades ADD COLUMN realized_partial_pnl_gbp REAL DEFAULT 0")
        if "pnl_gross_gbp" not in columns:
            conn.execute("ALTER TABLE trades ADD COLUMN pnl_gross_gbp REAL")
        if "fee_details_json" not in columns:
            conn.execute("ALTER TABLE trades ADD COLUMN fee_details_json TEXT")
        cursor = conn.execute("PRAGMA table_info(equity_snapshots)")
        eq_columns = {row[1] for row in cursor.fetchall()}
        if "mode" not in eq_columns:
            conn.execute("ALTER TABLE equity_snapshots ADD COLUMN mode TEXT DEFAULT 'paper'")
        if "source" not in eq_columns:
            conn.execute("ALTER TABLE equity_snapshots ADD COLUMN source TEXT DEFAULT 'internal'")
        if "free_balance_gbp" not in eq_columns:
            conn.execute("ALTER TABLE equity_snapshots ADD COLUMN free_balance_gbp REAL")
        if "margin_used_gbp" not in eq_columns:
            conn.execute("ALTER TABLE equity_snapshots ADD COLUMN margin_used_gbp REAL")
        if "metadata_json" not in eq_columns:
            conn.execute("ALTER TABLE equity_snapshots ADD COLUMN metadata_json TEXT")


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
    entry_fee_gbp: float = 0.0,
    fee_details: Optional[dict] = None,
) -> int:
    sql = """
    INSERT INTO trades
        (timestamp_open, market, pair, direction, entry_price, stop_loss,
         position_size, leverage, take_profit_1, take_profit_2,
         signals_triggered, mode, notes, entry_fee_gbp, total_fees_gbp,
         fee_details_json, status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
    """
    signals_json = json.dumps(signals_triggered) if signals_triggered else None
    fee_details_json = json.dumps(fee_details) if fee_details else None
    entry_fee = float(entry_fee_gbp or 0.0)
    with get_connection() as conn:
        cur = conn.execute(
            sql,
            (
                _now(), market, pair, direction, entry_price, stop_loss,
                position_size, leverage, take_profit_1, take_profit_2,
                signals_json, mode, notes, entry_fee, entry_fee,
                fee_details_json,
            ),
        )
        return cur.lastrowid


def close_trade(
    trade_id: int,
    exit_price: float,
    status: str,
    pnl_absolute: float,
    pnl_percent: float,
    *,
    pnl_gross_gbp: Optional[float] = None,
    exit_fee_gbp: float = 0.0,
    fee_details: Optional[dict] = None,
) -> None:
    sql = """
    UPDATE trades
    SET timestamp_close = ?, exit_price = ?, status = ?,
        pnl_gross_gbp = ?, pnl_absolute = ?, pnl_percent = ?,
        exit_fee_gbp = ?, total_fees_gbp = ?, fee_details_json = ?
    WHERE id = ?
    """
    with get_connection() as conn:
        existing = conn.execute(
            """
            SELECT entry_fee_gbp, exit_fee_gbp, realized_partial_pnl_gbp, fee_details_json
            FROM trades WHERE id = ?
            """,
            (trade_id,),
        ).fetchone()
        entry_fee = float((existing["entry_fee_gbp"] if existing else 0.0) or 0.0)
        previous_exit_fee = float((existing["exit_fee_gbp"] if existing else 0.0) or 0.0)
        partial_gross_pnl = float((existing["realized_partial_pnl_gbp"] if existing else 0.0) or 0.0)
        exit_fee_total = previous_exit_fee + float(exit_fee_gbp or 0.0)
        total_fees = entry_fee + exit_fee_total
        closing_gross_pnl = float(pnl_gross_gbp if pnl_gross_gbp is not None else pnl_absolute)
        gross_pnl = partial_gross_pnl + closing_gross_pnl
        net_pnl = gross_pnl - total_fees
        if fee_details is None and existing and existing["fee_details_json"]:
            fee_details_json = existing["fee_details_json"]
        else:
            fee_details_json = json.dumps(fee_details) if fee_details else None

        conn.execute(
            sql,
            (
                _now(), exit_price, status,
                round(gross_pnl, 6), round(net_pnl, 6), pnl_percent,
                round(exit_fee_total, 6), round(total_fees, 6), fee_details_json,
                trade_id,
            ),
        )


def add_trade_exit_fee(
    trade_id: int,
    exit_fee_gbp: float,
    realized_pnl_gbp: float = 0.0,
    fee_details: Optional[dict] = None,
) -> None:
    """Accumulate exit fees and realized gross PnL after a partial close."""
    with get_connection() as conn:
        existing = conn.execute(
            """
            SELECT entry_fee_gbp, exit_fee_gbp, realized_partial_pnl_gbp, fee_details_json
            FROM trades WHERE id = ?
            """,
            (trade_id,),
        ).fetchone()
        if existing is None:
            return
        entry_fee = float(existing["entry_fee_gbp"] or 0.0)
        exit_fee_total = float(existing["exit_fee_gbp"] or 0.0) + float(exit_fee_gbp or 0.0)
        partial_pnl = (
            float(existing["realized_partial_pnl_gbp"] or 0.0)
            + float(realized_pnl_gbp or 0.0)
        )
        total_fees = entry_fee + exit_fee_total
        if fee_details is None and existing["fee_details_json"]:
            fee_details_json = existing["fee_details_json"]
        else:
            fee_details_json = json.dumps(fee_details) if fee_details else existing["fee_details_json"]
        conn.execute(
            """
            UPDATE trades
            SET exit_fee_gbp = ?, total_fees_gbp = ?,
                realized_partial_pnl_gbp = ?, fee_details_json = ?
            WHERE id = ?
            """,
            (
                round(exit_fee_total, 6),
                round(total_fees, 6),
                round(partial_pnl, 6),
                fee_details_json,
                trade_id,
            ),
        )


def update_tp1_state(trade_id: int, remaining_size: float) -> None:
    """Record that TP1 was hit and update the remaining position size."""
    sql = """
    UPDATE trades
    SET remaining_size = ?, tp1_hit_at = ?
    WHERE id = ?
    """
    with get_connection() as conn:
        conn.execute(sql, (remaining_size, _now(), trade_id))


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
    mode: str = "paper",
    source: str = "internal",
    free_balance_gbp: Optional[float] = None,
    margin_used_gbp: Optional[float] = None,
    metadata: Optional[dict] = None,
) -> int:
    sql = """
    INSERT INTO equity_snapshots
        (timestamp, total_capital, crypto_capital, forex_capital,
         etf_capital, open_positions, daily_pnl, weekly_pnl,
         mode, source, free_balance_gbp, margin_used_gbp, metadata_json)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    metadata_json = json.dumps(metadata) if metadata else None
    with get_connection() as conn:
        cur = conn.execute(
            sql,
            (_now(), total_capital, crypto_capital, forex_capital,
             etf_capital, open_positions, daily_pnl, weekly_pnl,
             mode, source, free_balance_gbp, margin_used_gbp, metadata_json),
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


def get_system_flag(key: str) -> Optional[str]:
    """Read a runtime system flag (e.g. 'paused')."""
    sql = "SELECT value FROM system_flags WHERE key = ?"
    with get_connection() as conn:
        row = conn.execute(sql, (key,)).fetchone()
        return row[0] if row else None


def set_system_flag(key: str, value: str) -> None:
    """Set a runtime system flag (upsert)."""
    sql = """
    INSERT INTO system_flags (key, value, updated_at)
    VALUES (?, ?, ?)
    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
    """
    with get_connection() as conn:
        conn.execute(sql, (key, value, _now()))


def reset_runtime_state(
    *,
    total_capital: float,
    mode: str = "paper",
    source: str = "kraken_reset",
    crypto_capital: Optional[float] = None,
    forex_capital: float = 0.0,
    etf_capital: float = 0.0,
    free_balance_gbp: Optional[float] = None,
    margin_used_gbp: float = 0.0,
    note: str = "Runtime state reset",
) -> dict:
    """
    Clear runtime trading state while preserving configuration and secrets.

    This deletes trades, equity snapshots, logs, circuit breaker events, and
    flags from the SQLite runtime DB, then creates one clean equity snapshot.
    It does not touch config/secrets.yaml or any exchange-side account state.
    """
    init_db()
    tables = [
        "trades",
        "equity_snapshots",
        "system_logs",
        "circuit_breaker_events",
        "system_flags",
    ]
    now = _now()
    total = float(total_capital)
    crypto = float(total if crypto_capital is None else crypto_capital)
    free = total if free_balance_gbp is None else float(free_balance_gbp)

    with get_connection() as conn:
        before = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
        for table in tables:
            conn.execute(f"DELETE FROM {table}")
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name IN (?, ?, ?, ?)",
            ("trades", "equity_snapshots", "system_logs", "circuit_breaker_events"),
        )
        conn.execute(
            """
            INSERT INTO equity_snapshots
                (timestamp, total_capital, crypto_capital, forex_capital,
                 etf_capital, open_positions, daily_pnl, weekly_pnl,
                 mode, source, free_balance_gbp, margin_used_gbp, metadata_json)
            VALUES (?, ?, ?, ?, ?, 0, 0.0, 0.0, ?, ?, ?, ?, ?)
            """,
            (
                now,
                total,
                crypto,
                float(forex_capital),
                float(etf_capital),
                mode,
                source,
                free,
                float(margin_used_gbp),
                json.dumps({"reset": True, "note": note}),
            ),
        )
        conn.execute(
            """
            INSERT INTO system_flags (key, value, updated_at)
            VALUES ('paused', 'false', ?)
            """,
            (now,),
        )
        conn.execute(
            "INSERT INTO system_logs (timestamp, level, module, message) VALUES (?, ?, ?, ?)",
            (now, "INFO", "database", note),
        )
        after = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }

    return {
        "before": before,
        "after": after,
        "total_capital": total,
        "mode": mode,
        "source": source,
        "free_balance_gbp": free,
        "open_positions": 0,
    }


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


def get_total_fees() -> float:
    sql = "SELECT COALESCE(SUM(total_fees_gbp), 0.0) FROM trades WHERE status != 'open'"
    with get_connection() as conn:
        return conn.execute(sql).fetchone()[0]


def get_daily_fees(date_iso: str) -> float:
    sql = """
    SELECT COALESCE(SUM(total_fees_gbp), 0.0) FROM trades
    WHERE timestamp_close LIKE ? AND status != 'open'
    """
    with get_connection() as conn:
        return conn.execute(sql, (f"{date_iso}%",)).fetchone()[0]


def get_weekly_fees(week_start_iso: str) -> float:
    sql = """
    SELECT COALESCE(SUM(total_fees_gbp), 0.0) FROM trades
    WHERE timestamp_close >= ? AND status != 'open'
    """
    with get_connection() as conn:
        return conn.execute(sql, (week_start_iso,)).fetchone()[0]


def calculate_current_equity() -> float:
    """
    Calculate live equity: initial_capital + realized PnL from all closed trades.

    This is the authoritative source of capital — used by position sizer,
    risk manager, and circuit breakers instead of stale equity snapshots.
    """
    from config.loader import get_settings
    settings = get_settings()
    mode = settings.get("mode", "paper")
    latest_snapshot = get_latest_equity()
    if mode != "paper" and latest_snapshot and latest_snapshot.get("source") in {"binance", "kraken"}:
        total = latest_snapshot.get("total_capital")
        if total is not None:
            return float(total)

    initial = settings.get("initial_capital_gbp", 1000)
    realized_pnl = get_total_pnl()
    return initial + realized_pnl


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
