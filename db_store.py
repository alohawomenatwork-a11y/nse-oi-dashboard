"""
db_store.py
===========
Lightweight SQLite persistence layer for NSE OI, 5-minute TradingView candles,
ORB trade executions, and trade/system exceptions.

Designed for zero-config local + GitHub Actions usage and future rule backtesting.
"""

import os
import sqlite3
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(ROOT_DIR, "oi_trading.db")


def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Create tables and indexes if they do not already exist."""
    with get_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS daily_oi (
                trade_date     TEXT NOT NULL,
                symbol         TEXT NOT NULL,
                prev_close     REAL,
                open           REAL,
                high           REAL,
                low            REAL,
                close          REAL,
                price_chg_pct  REAL,
                oi             INTEGER,
                oi_chg         INTEGER,
                oi_chg_pct     REAL,
                deliv_pct      REAL,
                bucket         TEXT,
                PRIMARY KEY (trade_date, symbol)
            );

            CREATE TABLE IF NOT EXISTS intraday_candles_5m (
                trade_date  TEXT NOT NULL,
                symbol      TEXT NOT NULL,
                bar_time    TEXT NOT NULL,
                open        REAL NOT NULL,
                high        REAL NOT NULL,
                low         REAL NOT NULL,
                close       REAL NOT NULL,
                volume      REAL DEFAULT 0.0,
                PRIMARY KEY (trade_date, symbol, bar_time)
            );

            CREATE TABLE IF NOT EXISTS trade_executions (
                trade_date         TEXT NOT NULL,
                symbol             TEXT NOT NULL,
                rule_version       TEXT NOT NULL DEFAULT 'ORB_5M_V1',
                side               TEXT NOT NULL,
                triggered          INTEGER NOT NULL DEFAULT 0,
                trigger_time       TEXT,
                entry_price        REAL DEFAULT 0.0,
                sl_price           REAL DEFAULT 0.0,
                exit_price         REAL DEFAULT 0.0,
                exit_time          TEXT,
                exit_reason        TEXT,
                qty                INTEGER DEFAULT 0,
                margin_used        REAL DEFAULT 0.0,
                risk_rupees        REAL DEFAULT 0.0,
                risk_pct           REAL DEFAULT 0.0,
                max_profit_rupees  REAL DEFAULT 0.0,
                max_profit_pct     REAL DEFAULT 0.0,
                pnl                REAL DEFAULT 0.0,
                pnl_pct            REAL DEFAULT 0.0,
                target_1to1        REAL DEFAULT 0.0,
                target_1to1_hit    INTEGER DEFAULT 0,
                target_1to1_time   TEXT,
                pnl_1to1           REAL DEFAULT 0.0,
                pnl_1to1_pct       REAL DEFAULT 0.0,
                gap_filtered       INTEGER DEFAULT 0,
                erratic_filtered   INTEGER DEFAULT 0,
                PRIMARY KEY (trade_date, symbol, rule_version)
            );

            CREATE TABLE IF NOT EXISTS trade_exceptions (
                trade_date      TEXT NOT NULL,
                symbol          TEXT NOT NULL,
                stage           TEXT NOT NULL,
                exception_type  TEXT NOT NULL,
                details         TEXT,
                created_at      TEXT NOT NULL,
                PRIMARY KEY (trade_date, symbol, stage, exception_type)
            );

            CREATE INDEX IF NOT EXISTS idx_candles_sym_dt
                ON intraday_candles_5m (symbol, trade_date);
            CREATE INDEX IF NOT EXISTS idx_oi_dt
                ON daily_oi (trade_date);
            CREATE INDEX IF NOT EXISTS idx_exec_dt
                ON trade_executions (trade_date);
            """
        )


def _classify_bucket(price_chg: float, oi_chg: int) -> str:
    if price_chg > 0 and oi_chg > 0:
        return "fresh_longs"
    if price_chg < 0 and oi_chg > 0:
        return "fresh_shorts"
    if price_chg > 0 and oi_chg < 0:
        return "short_covering"
    if price_chg < 0 and oi_chg < 0:
        return "long_unwinding"
    return "neutral"


def save_daily_oi(trade_date_str: str, rows: list, db_path: str = DEFAULT_DB_PATH) -> int:
    """Persist all daily F&O rows (full universe, not only Top 10)."""
    if not rows:
        return 0
    init_db(db_path)
    records = [
        (
            trade_date_str,
            r["symbol"],
            r.get("last_close"),
            r.get("open"),
            r.get("high"),
            r.get("low"),
            r.get("close"),
            r.get("price_chg"),
            int(r.get("oi") or 0),
            int(r.get("oi_chg") or 0),
            r.get("oi_chg_pct"),
            r.get("deliv_pct"),
            _classify_bucket(float(r.get("price_chg") or 0.0), int(r.get("oi_chg") or 0)),
        )
        for r in rows
    ]
    with get_connection(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO daily_oi (
                trade_date, symbol, prev_close, open, high, low, close,
                price_chg_pct, oi, oi_chg, oi_chg_pct, deliv_pct, bucket
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, symbol) DO UPDATE SET
                prev_close=excluded.prev_close,
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                price_chg_pct=excluded.price_chg_pct,
                oi=excluded.oi,
                oi_chg=excluded.oi_chg,
                oi_chg_pct=excluded.oi_chg_pct,
                deliv_pct=excluded.deliv_pct,
                bucket=excluded.bucket
            """,
            records,
        )
    return len(records)


def save_intraday_candles(trade_date_str: str, symbol: str, bar_data: dict,
                          db_path: str = DEFAULT_DB_PATH) -> int:
    """Persist 5-minute OHLCV candles for a (trade_date, symbol) session."""
    if not bar_data or not bar_data.get("valid"):
        return 0
    opens = bar_data.get("open", [])
    highs = bar_data.get("high", [])
    lows = bar_data.get("low", [])
    closes = bar_data.get("close", [])
    times = bar_data.get("times", [])
    vols = bar_data.get("volume", [0.0] * len(opens))
    n = min(len(opens), len(highs), len(lows), len(closes), len(times))
    if n == 0:
        return 0

    init_db(db_path)
    records = [
        (
            trade_date_str,
            symbol,
            times[i],
            float(opens[i]),
            float(highs[i]),
            float(lows[i]),
            float(closes[i]),
            float(vols[i]) if i < len(vols) else 0.0,
        )
        for i in range(n)
    ]
    with get_connection(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO intraday_candles_5m (
                trade_date, symbol, bar_time, open, high, low, close, volume
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, symbol, bar_time) DO UPDATE SET
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume
            """,
            records,
        )
    return len(records)


def save_trade_executions(trade_date_str: str, trades: list,
                          rule_version: str = "ORB_5M_V1",
                          prune_stale: bool = True,
                          db_path: str = DEFAULT_DB_PATH) -> int:
    """Persist ORB trade executions for a session."""
    if not trades:
        return 0
    init_db(db_path)
    records = [
        (
            trade_date_str,
            t["symbol"],
            rule_version,
            t["side"],
            1 if t.get("triggered") else 0,
            t.get("trigger_time", "-"),
            float(t.get("entry_price") or 0.0),
            float(t.get("sl_price") or 0.0),
            float(t.get("exit_price") or 0.0),
            t.get("exit_time", "15:10"),
            t.get("exit_reason", "-"),
            int(t.get("qty") or 0),
            float(t.get("margin_used") or 0.0),
            float(t.get("risk_rupees") or 0.0),
            float(t.get("risk_pct") or 0.0),
            float(t.get("max_profit_rupees") or 0.0),
            float(t.get("max_profit_pct") or 0.0),
            float(t.get("pnl") or 0.0),
            float(t.get("pnl_pct") or 0.0),
            float(t.get("target_1to1") or 0.0),
            1 if t.get("target_1to1_hit") else 0,
            t.get("target_1to1_time", "-"),
            float(t.get("pnl_1to1") or 0.0),
            float(t.get("pnl_1to1_pct") or 0.0),
            1 if t.get("gap_filtered") else 0,
            1 if t.get("erratic_filtered") else 0,
        )
        for t in trades
    ]
    with get_connection(db_path) as conn:
        if prune_stale:
            syms = [t["symbol"] for t in trades if t.get("symbol")]
            if syms:
                placeholders = ",".join("?" for _ in syms)
                conn.execute(
                    f"DELETE FROM trade_executions WHERE trade_date=? AND rule_version=? AND symbol NOT IN ({placeholders})",
                    [trade_date_str, rule_version, *syms],
                )
        conn.executemany(
            """
            INSERT INTO trade_executions (
                trade_date, symbol, rule_version, side, triggered, trigger_time,
                entry_price, sl_price, exit_price, exit_time, exit_reason, qty,
                margin_used, risk_rupees, risk_pct, max_profit_rupees, max_profit_pct,
                pnl, pnl_pct, target_1to1, target_1to1_hit, target_1to1_time,
                pnl_1to1, pnl_1to1_pct, gap_filtered, erratic_filtered
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, symbol, rule_version) DO UPDATE SET
                side=excluded.side,
                triggered=excluded.triggered,
                trigger_time=excluded.trigger_time,
                entry_price=excluded.entry_price,
                sl_price=excluded.sl_price,
                exit_price=excluded.exit_price,
                exit_time=excluded.exit_time,
                exit_reason=excluded.exit_reason,
                qty=excluded.qty,
                margin_used=excluded.margin_used,
                risk_rupees=excluded.risk_rupees,
                risk_pct=excluded.risk_pct,
                max_profit_rupees=excluded.max_profit_rupees,
                max_profit_pct=excluded.max_profit_pct,
                pnl=excluded.pnl,
                pnl_pct=excluded.pnl_pct,
                target_1to1=excluded.target_1to1,
                target_1to1_hit=excluded.target_1to1_hit,
                target_1to1_time=excluded.target_1to1_time,
                pnl_1to1=excluded.pnl_1to1,
                pnl_1to1_pct=excluded.pnl_1to1_pct,
                gap_filtered=excluded.gap_filtered,
                erratic_filtered=excluded.erratic_filtered
            """,
            records,
        )
    return len(records)


def log_trade_exception(trade_date_str: str, symbol: str, stage: str,
                        exception_type: str, details: str = "",
                        db_path: str = DEFAULT_DB_PATH) -> None:
    """Record a trade filter skip, corporate action exclusion, or fetch error."""
    init_db(db_path)
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO trade_exceptions (
                trade_date, symbol, stage, exception_type, details, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, symbol, stage, exception_type) DO UPDATE SET
                details=excluded.details,
                created_at=excluded.created_at
            """,
            (trade_date_str, symbol, stage, exception_type, details, now_iso),
        )


def get_db_stats(db_path: str = DEFAULT_DB_PATH) -> dict:
    """Return row counts for all tables."""
    init_db(db_path)
    tables = ["daily_oi", "intraday_candles_5m", "trade_executions", "trade_exceptions"]
    stats = {}
    with get_connection(db_path) as conn:
        for tbl in tables:
            row = conn.execute(f"SELECT COUNT(*) AS cnt FROM {tbl}").fetchone()
            stats[tbl] = int(row["cnt"]) if row else 0
    return stats

