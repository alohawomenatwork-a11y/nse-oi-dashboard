"""
backfill_db.py
==============
One-time backfill of oi_trading.db from:
1. All cached NSE F&O + EQ bhavcopies in .bhavcopy_cache/
2. All verified trade records in trade_history.json
3. 750-bar 5-minute TradingView candles for every symbol in trade_history.json + NIFTY
"""

import os
import re
import json
import datetime
import pandas as pd

import db_store
from fetch_oi_data import parse_fo_oi, parse_eq_prices, TRACK_SYMBOLS
from tv_candles import fetch_tv_candles

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(ROOT, ".bhavcopy_cache")
HIST_FILE = os.path.join(ROOT, "trade_history.json")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

TV_SYMBOL_MAP = {
    "BAJAJ-AUTO": "BAJAJ_AUTO",
    "M&M":        "M_M",
    "M&MFIN":     "M_MFIN",
    "L&TFH":      "L_TFH",
}


def tv_variants(sym: str) -> list:
    if sym in TV_SYMBOL_MAP:
        return [TV_SYMBOL_MAP[sym], sym]
    return [sym]


def backfill_daily_oi():
    print("[1/3] Backfilling daily_oi from .bhavcopy_cache/ ...")
    fo_dates = sorted(
        m.group(1)
        for fn in os.listdir(CACHE_DIR)
        if (m := re.match(r"^fo_(\d{8})\.csv$", fn))
    )
    prev_oi_map = {}
    total_rows = 0
    for dt_ymd in fo_dates:
        dt_str = f"{dt_ymd[:4]}-{dt_ymd[4:6]}-{dt_ymd[6:8]}"
        fo_path = os.path.join(CACHE_DIR, f"fo_{dt_ymd}.csv")
        eq_path = os.path.join(CACHE_DIR, f"eq_{dt_ymd}.csv")
        oi_today = parse_fo_oi(pd.read_csv(fo_path))
        if not os.path.exists(eq_path):
            prev_oi_map = oi_today
            continue
        prices = parse_eq_prices(pd.read_csv(eq_path, skipinitialspace=True))
        if not prev_oi_map:
            prev_oi_map = oi_today
            continue

        rows = []
        for sym in (TRACK_SYMBOLS | set(oi_today.keys())):
            p = prices.get(sym)
            if p is None:
                continue
            oi_t = oi_today.get(sym, 0)
            oi_p = prev_oi_map.get(sym, 0)
            oi_chg = oi_t - oi_p
            oi_chg_pct = round(oi_chg / max(oi_p, 1) * 100, 2) if oi_p else 0.0
            prev_close = p.get("prev_close", p["close"])
            price_chg_pct = round((p["close"] - prev_close) / prev_close * 100, 2) if prev_close else 0.0
            rows.append({
                "symbol": sym,
                "last_close": round(prev_close, 2),
                "open": round(p["open"], 2),
                "high": round(p["high"], 2),
                "low": round(p["low"], 2),
                "close": round(p["close"], 2),
                "price_chg": price_chg_pct,
                "oi": oi_t,
                "oi_chg": oi_chg,
                "oi_chg_pct": oi_chg_pct,
                "deliv_pct": p.get("deliv_pct"),
            })
        cnt = db_store.save_daily_oi(dt_str, rows)
        total_rows += cnt
        print(f"    {dt_str}: saved {cnt} rows")
        prev_oi_map = oi_today
    print(f"    Total daily_oi saved: {total_rows}")


def backfill_trades_and_exceptions():
    print("\n[2/3] Backfilling trade_executions & trade_exceptions from trade_history.json ...")
    with open(HIST_FILE, "r", encoding="utf-8") as f:
        trades = json.load(f)

    by_date = {}
    for t in trades:
        by_date.setdefault(t["date"], []).append(t)

    for dt_str, day_trades in sorted(by_date.items()):
        cnt = db_store.save_trade_executions(dt_str, day_trades, prune_stale=True)
        for t in day_trades:
            if t.get("gap_filtered"):
                db_store.log_trade_exception(
                    dt_str, t["symbol"], "ORB_FILTER", "GAP_FILTER",
                    f"side={t.get('side')} backfilled"
                )
            elif t.get("erratic_filtered"):
                db_store.log_trade_exception(
                    dt_str, t["symbol"], "ORB_FILTER", "ERRATIC_2PCT",
                    f"side={t.get('side')} backfilled"
                )
        print(f"    {dt_str}: saved {cnt} trade executions")

    # Record known NSE holiday
    db_store.log_trade_exception(
        "2026-11-10", "NSE", "MARKET_SESSION", "TRADING_HOLIDAY",
        "NSE closed on 2026-11-10 (no F&O bhavcopy, 0 TradingView bars)"
    )


def backfill_5m_candles():
    print("\n[3/3] Backfilling 5m TradingView candles (750 bars/symbol) ...")
    with open(HIST_FILE, "r", encoding="utf-8") as f:
        trades = json.load(f)

    symbols = sorted({t["symbol"] for t in trades} | {"NIFTY"})
    total_bars = 0
    for idx, sym in enumerate(symbols, 1):
        bars = []
        for s_try in tv_variants(sym):
            bars = fetch_tv_candles(s_try, exchange="NSE", interval="5", n_bars=750)
            if bars:
                break
        if not bars:
            print(f"    [{idx}/{len(symbols)}] {sym}: 0 bars")
            continue

        by_day = {}
        for b in bars:
            dt_obj = datetime.datetime.fromtimestamp(b["time"], tz=IST)
            dt_str = dt_obj.strftime("%Y-%m-%d")
            hm_str = dt_obj.strftime("%H:%M")
            d = by_day.setdefault(dt_str, {"valid": True, "open": [], "high": [], "low": [], "close": [], "volume": [], "times": []})
            d["open"].append(b["open"])
            d["high"].append(b["high"])
            d["low"].append(b["low"])
            d["close"].append(b["close"])
            d["volume"].append(b.get("volume", 0.0))
            d["times"].append(hm_str)

        sym_saved = 0
        for dt_str, b_data in by_day.items():
            sym_saved += db_store.save_intraday_candles(dt_str, sym, b_data)
        total_bars += sym_saved
        print(f"    [{idx}/{len(symbols)}] {sym}: {sym_saved} bars across {len(by_day)} sessions")

    print(f"    Total 5m candles saved: {total_bars}")


if __name__ == "__main__":
    db_store.init_db()
    backfill_daily_oi()
    backfill_trades_and_exceptions()
    backfill_5m_candles()
    print("\n[DONE] SQLite DB stats:", db_store.get_db_stats())

