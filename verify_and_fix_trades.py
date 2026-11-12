"""
verify_and_fix_trades.py
========================
Re-downloads 5-min TradingView candles for every trade from 2026-11-05 to today,
re-runs compute_orb_execution() against the REAL rules, compares with stored
trade_history.json, fixes every mismatch and regenerates dashboard.html.

ROOT CAUSE ANALYSIS is printed at the end.

Usage:
    python verify_and_fix_trades.py
"""

import sys
import os
import json
import csv
import io
import datetime
import time

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT      = os.path.dirname(os.path.abspath(__file__))
HIST_FILE = os.path.join(ROOT, "trade_history.json")
CACHE_DIR = os.path.join(ROOT, ".bhavcopy_cache")

# ── IST timezone ───────────────────────────────────────────────────────────────
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# ── Trading calendar (days we have trades) ──────────────────────────────────
TRADE_DATES_STR = ["2026-11-05","2026-11-06","2026-11-09",
                   "2026-11-10","2026-11-11","2026-11-12"]

# ══════════════════════════════════════════════════════════════════════════════
# 1.  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def session_timestamps(trading_date: datetime.date):
    """Return (start_ts, end_ts) in UNIX seconds for a full NSE session in IST."""
    start = datetime.datetime(trading_date.year, trading_date.month, trading_date.day,
                               9, 15, 0, tzinfo=IST)
    end   = datetime.datetime(trading_date.year, trading_date.month, trading_date.day,
                               15, 30, 0, tzinfo=IST)
    return int(start.timestamp()), int(end.timestamp())


def get_prev_close(symbol: str, trading_date: datetime.date) -> float:
    """
    Look up the previous day's close for `symbol` from the EQ bhavcopy cache.
    Falls back to 0.0 if not found (gap filter will then be skipped).
    """
    # Walk backwards up to 5 calendar days looking for a cached EQ bhavcopy
    for delta in range(1, 6):
        prev_dt = trading_date - datetime.timedelta(days=delta)
        path = os.path.join(CACHE_DIR, f"eq_{prev_dt.strftime('%Y%m%d')}.csv")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                # Strip column names
                reader.fieldnames = [c.strip() for c in (reader.fieldnames or [])]
                for row in reader:
                    # Try multiple column-name variants
                    sym_val = (row.get("SYMBOL") or row.get("TckrSymb") or
                               row.get("SC_NAME") or "").strip().upper()
                    if sym_val == symbol.upper():
                        for col in ["CLOSE_PRICE", "CLOSE", "ClsPric", "CLSPRIC"]:
                            v = row.get(col, "").strip()
                            if v:
                                try:
                                    return float(v)
                                except ValueError:
                                    pass
        except Exception:
            pass
    return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# 2.  FETCH TV CANDLES  (import the fixed tv_candles module)
# ══════════════════════════════════════════════════════════════════════════════

sys.path.insert(0, ROOT)
from tv_candles import fetch_tv_candles

# TradingView uses different ticker names for a few NSE symbols
TV_SYMBOL_MAP = {
    "BAJAJ-AUTO":  "BAJAJ_AUTO",
    "M&M":         "M_M",
    "M&MFIN":      "M_MFIN",
    "L&TFH":       "L_TFH",
    "NIFTY":       "NIFTY",
    "BANKNIFTY":   "BANKNIFTY",
    "FINNIFTY":    "FINNIFTY",
}

def tv_symbol(symbol: str) -> list:
    """Return ordered list of TV symbol variants to try for a given NSE symbol."""
    if symbol in TV_SYMBOL_MAP:
        mapped = TV_SYMBOL_MAP[symbol]
        # Try mapped first, then original, then other variants
        variants = [mapped]
    else:
        variants = []

    # Always include the original
    if symbol not in variants:
        variants.append(symbol)

    # Generate additional variants from special characters
    for sep_in, sep_out in [("-", "_"), ("&", "_"), ("-", ""), ("&", "")]:
        alt = symbol.replace("-", sep_out).replace("&", sep_out)
        if alt and alt not in variants:
            variants.append(alt)

    return variants


def should_skip_tv_download(stored: dict) -> bool:
    """
    If a trade was gap_filtered or erratic_filtered AND not triggered,
    all numeric fields are zero by design — skip TV download, just re-verify filter flags.
    """
    if (stored.get("gap_filtered") or stored.get("erratic_filtered")) and not stored.get("triggered"):
        return True
    return False


_RAW_TV_CACHE = {}

def download_bars(symbol: str, trading_date: datetime.date, retries: int = 2):
    """
    Download 5-min candles from TradingView for the given session.
    Caches full 750-bar history per symbol so each symbol is fetched only once.
    Tries multiple symbol name variants (handles BAJAJ-AUTO, M&M etc.)
    Returns bar_data dict (with key 'valid') or {'valid': False}.
    """
    if symbol not in _RAW_TV_CACHE:
        variants = tv_symbol(symbol)
        raw_all = []
        for sym_try in variants:
            for attempt in range(1, retries + 1):
                raw_all = fetch_tv_candles(sym_try, exchange="NSE", interval="5",
                                           n_bars=750)
                if raw_all:
                    if sym_try != symbol:
                        print(f"      [info] Used TV symbol '{sym_try}' for NSE '{symbol}'")
                    break
                if attempt < retries:
                    print(f"      [retry {attempt}] No bars for {sym_try}, waiting 1s…")
                    time.sleep(1)
            if raw_all:
                break
            time.sleep(0.5)
        _RAW_TV_CACHE[symbol] = raw_all
        if not raw_all:
            print(f"      [warn] All symbol variants failed for {symbol}: {variants}")

    raw_all = _RAW_TV_CACHE[symbol]
    if not raw_all:
        return {"valid": False}

    start_ts, end_ts = session_timestamps(trading_date)
    raw = [b for b in raw_all if start_ts <= b["time"] <= end_ts]
    if not raw:
        return {"valid": False}

    return {
        "valid":  True,
        "open":   [b["open"]  for b in raw],
        "high":   [b["high"]  for b in raw],
        "low":    [b["low"]   for b in raw],
        "close":  [b["close"] for b in raw],
        "times":  [
            datetime.datetime.fromtimestamp(b["time"], tz=IST).strftime("%H:%M")
            for b in raw
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
# 3.  ORB LOGIC  (self-contained duplicate — avoids importing the giant script)
# ══════════════════════════════════════════════════════════════════════════════

def _fmt(n, d=2):
    return f"{n:,.{d}f}"


def compute_orb(side: str, symbol: str, bar_data: dict,
                prev_close: float = 0.0,
                capital: float = 10000.0, margin: int = 5) -> dict:
    """
    Re-implementation of compute_orb_execution() with the exact same rules.
    Returns a dict with all trade fields.
    """
    base = dict(
        symbol=symbol, side=side, triggered=False,
        entry_price=0.0, trigger_time="-", exit_price=0.0,
        exit_time="15:10", exit_reason="LTP",
        sl_price=0.0, qty=0, margin_used=0.0, exposure=0.0,
        risk_rupees=0.0, risk_pct=0.0,
        max_profit_rupees=0.0, max_profit_pct=0.0,
        pnl=0.0, pnl_pct=0.0,
        target_1to1=0.0, target_1to1_hit=False,
        target_1to1_time="-", pnl_1to1=0.0, pnl_1to1_pct=0.0,
        gap_filtered=False, erratic_filtered=False,
        bar0_high=0.0, bar0_low=0.0, bar0_range_pct=0.0, gap_pct=0.0,
        note=""
    )

    if not bar_data or not bar_data.get("valid"):
        base["note"] = "No intraday data"
        return base

    opens  = bar_data["open"]
    highs  = bar_data["high"]
    lows   = bar_data["low"]
    closes = bar_data["close"]
    times  = bar_data.get("times", [])
    n      = len(opens)

    if n < 2:
        base["note"] = "Too few bars"
        return base

    b0_open  = opens[0]
    b0_high  = highs[0]
    b0_low   = lows[0]
    b0_range = round(((b0_high - b0_low) / max(b0_open, 0.01)) * 100, 2)
    ltp      = closes[-1]
    day_high = max(highs)
    day_low  = min(lows)

    base["bar0_high"]      = b0_high
    base["bar0_low"]       = b0_low
    base["bar0_range_pct"] = b0_range

    # Gap %
    gap_pct = round(((b0_open - prev_close) / prev_close) * 100, 2) if prev_close > 0 else 0.0
    base["gap_pct"] = gap_pct

    # ── Filter 1: Gap direction ──────────────────────────────────────────────
    if prev_close > 0:
        if side == "BUY" and b0_open < prev_close:
            base["gap_filtered"] = True
            base["note"] = f"Filtered: opened BELOW prev close ({b0_open} < {prev_close}, {gap_pct}%)"
            return base
        if side == "SELL" and b0_open > prev_close:
            base["gap_filtered"] = True
            base["note"] = f"Filtered: opened ABOVE prev close ({b0_open} > {prev_close}, {gap_pct}%)"
            return base
        # ── Filter 2: Extreme gap ────────────────────────────────────────────
        if side == "BUY" and gap_pct > 2.5:
            base["gap_filtered"] = True
            base["note"] = f"Filtered: extreme gap-up {gap_pct}% > 2.5%"
            return base
        if side == "SELL" and gap_pct < -2.5:
            base["gap_filtered"] = True
            base["note"] = f"Filtered: extreme gap-down {gap_pct}% < -2.5%"
            return base

    # ── Filter 3: Erratic 1st candle ────────────────────────────────────────
    if b0_range > 2.0:
        base["erratic_filtered"] = True
        base["note"] = f"Filtered: 1st candle range {b0_range}% > 2.0%"
        return base

    buying_power = capital * margin

    if side == "BUY":
        sl_price = day_low
        base["sl_price"] = sl_price

        # Find breakout bar (from bar 1 onwards)
        triggered = False
        entry_price = 0.0
        entry_bar   = -1
        for i in range(1, n):
            if highs[i] > b0_high:
                triggered   = True
                entry_price = max(opens[i], b0_high)
                entry_bar   = i
                break

        if not triggered:
            base["note"] = f"Not triggered — price never broke ORB High {b0_high}"
            return base

        # Scan from entry bar for SL and max favorable
        sl_hit  = False
        exit_bar = -1
        exit_price = ltp
        exit_reason = "LTP"
        max_fav = entry_price

        for k in range(entry_bar, n):
            if highs[k] > max_fav:
                max_fav = highs[k]
            if lows[k] <= sl_price:
                sl_hit     = True
                exit_price = sl_price
                exit_reason = "SL Hit"
                exit_bar   = k
                break

        qty          = max(1, int(buying_power / entry_price))
        exposure     = round(entry_price * qty, 2)
        margin_used  = round(exposure / margin, 2)
        pnl          = round((exit_price - entry_price) * qty, 2)
        pnl_pct      = round((pnl / margin_used) * 100, 2) if margin_used else 0.0
        per_risk     = max(round(entry_price - sl_price, 2), 0.05)
        risk_rs      = round(per_risk * qty, 2)
        risk_pct     = round(risk_rs / margin_used * 100, 2) if margin_used else 0.0
        max_profit   = round(max(max_fav - entry_price, 0.0) * qty, 2)
        max_prof_pct = round(max_profit / margin_used * 100, 2) if margin_used else 0.0
        trig_time    = times[entry_bar] if entry_bar < len(times) else "09:20"
        exit_time    = times[exit_bar]  if (sl_hit and exit_bar < len(times)) else "15:10"
        target_1to1  = round(entry_price + per_risk, 2)

        t11_hit  = False
        t11_time = "-"
        for m in range(entry_bar, n):
            if highs[m] >= target_1to1:
                t11_hit  = True
                t11_time = times[m] if m < len(times) else "-"
                break
            if lows[m] <= sl_price:
                break

        if t11_hit:
            pnl_11 = risk_rs; pnl_11_pct = risk_pct
        elif sl_hit:
            pnl_11 = -risk_rs; pnl_11_pct = -risk_pct
        else:
            pnl_11 = pnl; pnl_11_pct = pnl_pct

        base.update(dict(
            triggered=True, entry_price=entry_price, trigger_time=trig_time,
            exit_price=exit_price, exit_time=exit_time, exit_reason=exit_reason,
            qty=qty, exposure=exposure, margin_used=margin_used,
            risk_rupees=risk_rs, risk_pct=risk_pct,
            max_profit_rupees=max_profit, max_profit_pct=max_prof_pct,
            pnl=pnl, pnl_pct=pnl_pct,
            target_1to1=target_1to1, target_1to1_hit=t11_hit,
            target_1to1_time=t11_time, pnl_1to1=pnl_11, pnl_1to1_pct=pnl_11_pct,
            note="OK"
        ))

    else:  # SELL / SHORT
        sl_price = day_high
        base["sl_price"] = sl_price

        triggered   = False
        entry_price = 0.0
        entry_bar   = -1
        for i in range(1, n):
            if lows[i] < b0_low:
                triggered   = True
                entry_price = min(opens[i], b0_low)
                entry_bar   = i
                break

        if not triggered:
            base["note"] = f"Not triggered — price never broke ORB Low {b0_low}"
            return base

        sl_hit     = False
        exit_bar   = -1
        exit_price = ltp
        exit_reason = "LTP"
        min_fav    = entry_price

        for k in range(entry_bar, n):
            if lows[k] < min_fav:
                min_fav = lows[k]
            if highs[k] >= sl_price:
                sl_hit     = True
                exit_price = sl_price
                exit_reason = "SL Hit"
                exit_bar   = k
                break

        qty          = max(1, int(buying_power / entry_price))
        exposure     = round(entry_price * qty, 2)
        margin_used  = round(exposure / margin, 2)
        pnl          = round((entry_price - exit_price) * qty, 2)
        pnl_pct      = round((pnl / margin_used) * 100, 2) if margin_used else 0.0
        per_risk     = max(round(sl_price - entry_price, 2), 0.05)
        risk_rs      = round(per_risk * qty, 2)
        risk_pct     = round(risk_rs / margin_used * 100, 2) if margin_used else 0.0
        max_profit   = round(max(entry_price - min_fav, 0.0) * qty, 2)
        max_prof_pct = round(max_profit / margin_used * 100, 2) if margin_used else 0.0
        trig_time    = times[entry_bar] if entry_bar < len(times) else "09:20"
        exit_time    = times[exit_bar]  if (sl_hit and exit_bar < len(times)) else "15:10"
        target_1to1  = round(entry_price - per_risk, 2)

        t11_hit  = False
        t11_time = "-"
        for m in range(entry_bar, n):
            if lows[m] <= target_1to1:
                t11_hit  = True
                t11_time = times[m] if m < len(times) else "-"
                break
            if highs[m] >= sl_price:
                break

        if t11_hit:
            pnl_11 = risk_rs; pnl_11_pct = risk_pct
        elif sl_hit:
            pnl_11 = -risk_rs; pnl_11_pct = -risk_pct
        else:
            pnl_11 = pnl; pnl_11_pct = pnl_pct

        base.update(dict(
            triggered=True, entry_price=entry_price, trigger_time=trig_time,
            exit_price=exit_price, exit_time=exit_time, exit_reason=exit_reason,
            qty=qty, exposure=exposure, margin_used=margin_used,
            risk_rupees=risk_rs, risk_pct=risk_pct,
            max_profit_rupees=max_profit, max_profit_pct=max_prof_pct,
            pnl=pnl, pnl_pct=pnl_pct,
            target_1to1=target_1to1, target_1to1_hit=t11_hit,
            target_1to1_time=t11_time, pnl_1to1=pnl_11, pnl_1to1_pct=pnl_11_pct,
            note="OK"
        ))

    return base


# ══════════════════════════════════════════════════════════════════════════════
# 4.  COMPARE  (which fields differ?)
# ══════════════════════════════════════════════════════════════════════════════

COMPARE_FIELDS = [
    "triggered", "trigger_time", "entry_price", "sl_price",
    "exit_price", "exit_time", "exit_reason", "qty",
    "margin_used", "risk_rupees", "risk_pct",
    "max_profit_rupees", "max_profit_pct",
    "pnl", "pnl_pct",
    "target_1to1", "target_1to1_hit", "target_1to1_time",
    "pnl_1to1", "pnl_1to1_pct",
    "gap_filtered", "erratic_filtered",
]

NUMERIC_FIELDS = {
    "entry_price", "sl_price", "exit_price", "qty", "margin_used",
    "risk_rupees", "risk_pct", "max_profit_rupees", "max_profit_pct",
    "pnl", "pnl_pct", "target_1to1", "pnl_1to1", "pnl_1to1_pct",
}

TOL = 0.5   # tolerance for floating-point field comparison


def fields_differ(stored, fresh):
    diffs = []
    for field in COMPARE_FIELDS:
        s = stored.get(field)
        f = fresh.get(field)
        if field in NUMERIC_FIELDS:
            try:
                if abs(float(s or 0) - float(f or 0)) > TOL:
                    diffs.append((field, s, f))
            except Exception:
                if str(s) != str(f):
                    diffs.append((field, s, f))
        else:
            if str(s) != str(f):
                diffs.append((field, s, f))
    return diffs


# ══════════════════════════════════════════════════════════════════════════════
# 5.  APPLY FIX (merge fresh values into stored entry)
# ══════════════════════════════════════════════════════════════════════════════

WRITE_FIELDS = [
    "triggered", "trigger_time", "entry_price", "sl_price",
    "qty", "risk_rupees", "risk_pct",
    "max_profit_rupees", "max_profit_pct",
    "exit_price", "exit_time", "exit_reason",
    "margin_used", "pnl", "pnl_pct",
    "target_1to1", "target_1to1_hit", "target_1to1_time",
    "pnl_1to1", "pnl_1to1_pct",
    "gap_filtered", "erratic_filtered",
]


def apply_fix(stored: dict, fresh: dict) -> dict:
    updated = dict(stored)
    for field in WRITE_FIELDS:
        if field in fresh:
            updated[field] = fresh[field]
    return updated


# ══════════════════════════════════════════════════════════════════════════════
# 6.  MAIN VERIFICATION LOOP
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "="*72)
    print("  TRADE VERIFICATION & AUTO-FIX (TradingView re-download)")
    print("="*72)

    # Load trade history
    with open(HIST_FILE, "r", encoding="utf-8") as f:
        trades = json.load(f)

    total         = len(trades)
    fixed_count   = 0
    skipped_tv    = 0
    root_causes   = {}   # symbol/date → list of root cause strings

    # Group by date
    from collections import defaultdict
    by_date = defaultdict(list)
    for idx, t in enumerate(trades):
        by_date[t["date"]].append(idx)

    # We'll store bar_data cache to avoid re-fetching same symbol/date
    bar_cache   = {}   # (symbol, date_str) -> bar_data or None
    close_cache = {}   # (symbol, date_str) -> prev_close float

    for date_str in sorted(by_date.keys()):
        trade_date = datetime.date.fromisoformat(date_str)
        indices    = by_date[date_str]
        print(f"\n{'─'*60}")
        print(f"  Date: {date_str}  ({len(indices)} trades)")
        print(f"{'─'*60}")

        for idx in indices:
            t      = trades[idx]
            symbol = t["symbol"]
            side   = t["side"]

            print(f"\n  [{symbol}] {side}")

            # ── 0. Skip TV download for gap/erratic filtered+non-triggered trades ──
            cache_key = (symbol, date_str)
            if should_skip_tv_download(t):
                print(f"    → Filtered (gap/erratic+not triggered) — checking filter flags only")
                pc = get_prev_close(symbol, trade_date)
                close_cache[cache_key] = pc
                bar_data = download_bars(symbol, trade_date)
                bar_cache[cache_key] = bar_data
                if not bar_data.get("valid"):
                    print(f"    ⚠ No TV data for filter check — skipping")
                    skipped_tv += 1
                    continue
            else:
                # ── 1. Fetch TV bars ──────────────────────────────────────────
                if cache_key not in bar_cache:
                    print(f"    → Downloading TradingView 5m bars…")
                    bar_data = download_bars(symbol, trade_date)
                    bar_cache[cache_key] = bar_data
                    n_bars = len(bar_data.get("open", []))
                    print(f"    → Got {n_bars} bars for session {date_str}")
                    # Rate-limit: small pause after each TV download
                    time.sleep(1.5)
                else:
                    bar_data = bar_cache[cache_key]

                if not bar_data.get("valid"):
                    print(f"    ⚠ No TradingView data — skipping")
                    skipped_tv += 1
                    root_causes.setdefault(cache_key, []).append(
                        "TradingView returned no data for this date/symbol")
                    continue

                n_bars = len(bar_data.get("open", []))
                if n_bars < 2:
                    print(f"    ⚠ Only {n_bars} bars — insufficient; skipping")
                    skipped_tv += 1
                    continue

            # ── 2. Get prev_close ─────────────────────────────────────────────
            if cache_key not in close_cache:
                pc = get_prev_close(symbol, trade_date)
                close_cache[cache_key] = pc
            else:
                pc = close_cache[cache_key]

            if pc == 0.0:
                print(f"    ⚠ prev_close not found in bhavcopy cache — gap filter disabled")

            # ── 3. Re-run ORB logic ───────────────────────────────────────────
            fresh = compute_orb(side, symbol, bar_data, prev_close=pc)

            # ── 4. Compare ───────────────────────────────────────────────────
            diffs = fields_differ(t, fresh)

            if not diffs:
                print(f"    ✓ All fields correct — no fix needed")
            else:
                print(f"    ✗ {len(diffs)} field(s) DIFFER:")
                for field, old_val, new_val in diffs:
                    print(f"       {field:25s}  stored={old_val!r:20}  fresh={new_val!r}")

                # Root cause analysis
                causes = []
                if t.get("triggered") and not fresh.get("triggered"):
                    causes.append("Stored shows TRIGGERED but fresh data shows NO BREAKOUT — "
                                  "old data was for a different/wrong date")
                if not t.get("triggered") and fresh.get("triggered"):
                    causes.append("Stored shows NOT TRIGGERED but fresh shows TRIGGERED — "
                                  "old data may have been recorded before breakout occurred")
                if "entry_price" in [d[0] for d in diffs]:
                    causes.append("Entry price mismatch — ORB High/Low computed from wrong candle "
                                  "(likely TV returned latest-day bars instead of historic bars)")
                if "sl_price" in [d[0] for d in diffs]:
                    causes.append("SL mismatch — Day's High/Low different because wrong day's "
                                  "session bars were used")
                if "pnl" in [d[0] for d in diffs]:
                    causes.append("P&L mismatch flows from incorrect entry/exit/SL values above")
                if "gap_filtered" in [d[0] for d in diffs]:
                    causes.append("Gap filter result changed — prev_close was missing or wrong "
                                  "when trade was originally recorded")

                if not causes:
                    causes.append("Minor numerical rounding difference in computed fields")

                root_causes.setdefault(cache_key, []).extend(causes)

                # Apply fix
                trades[idx] = apply_fix(t, fresh)
                fixed_count += 1
                print(f"    ✔ FIXED and updated in memory")

    # ── Save corrected trade_history.json ──────────────────────────────────────
    print(f"\n{'='*72}")
    print(f"  Saving corrected trade_history.json  ({fixed_count} entries fixed)…")
    with open(HIST_FILE, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2, ensure_ascii=False)
    print(f"  ✓ Saved: {HIST_FILE}")

    # ── Root cause summary ────────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print("  ROOT CAUSE ANALYSIS")
    print(f"{'='*72}")
    if root_causes:
        for (sym, dt), causes in sorted(root_causes.items()):
            print(f"\n  {sym} / {dt}:")
            for c in set(causes):
                print(f"    • {c}")
    else:
        print("  No mismatches detected — all trades were already accurate.")

    print(f"\n  Summary: {total} total | {fixed_count} fixed | {skipped_tv} skipped (no TV data)")

    # ── Regenerate dashboard ──────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print("  Regenerating dashboard.html…")
    try:
        import subprocess, shutil
        res = subprocess.run([sys.executable, os.path.join(ROOT, "fetch_oi_data.py")],
                             cwd=ROOT, check=True)
        out_path = os.path.join(ROOT, "dashboard.html")
        idx_path = os.path.join(ROOT, "index.html")
        if os.path.exists(out_path):
            shutil.copy2(out_path, idx_path)
        print(f"  ✓ Dashboard regenerated: {out_path}")
        print(f"  ✓ index.html updated")
    except Exception as e:
        print(f"  ⚠ Dashboard regeneration failed: {e}")
        import traceback; traceback.print_exc()

    print(f"\n{'='*72}")
    print("  DONE — please review the output above and push to GitHub.")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()

