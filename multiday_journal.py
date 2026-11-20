"""
Multi-Day Historic Trade Journal & Performance Engine
=====================================================
Manages and renders the complete Historical Trade Journal for both 4-Day
and 5-Day (1-Week) multi-day OI strategies inside Tab 3.
Persists executions to SQLite (trade_executions with rule_version 'ORB_5M_4D' & 'ORB_5M_5D')
and JSON files ('trade_history_4d.json' & 'trade_history_5d.json').
"""

import json
import os
import sqlite3
from datetime import datetime

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(OUT_DIR, "oi_trading.db")

JOURNAL_FILE_4D = os.path.join(OUT_DIR, "trade_history_4d.json")
JOURNAL_FILE_5D = os.path.join(OUT_DIR, "trade_history_5d.json")

def fmt(v, dec=2):
    if v is None:
        return "0.00"
    try:
        return f"{float(v):,.{dec}f}"
    except (ValueError, TypeError):
        return "0.00"

def load_trade_journal_multiday(n_days=5):
    filepath = JOURNAL_FILE_5D if n_days == 5 else JOURNAL_FILE_4D
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_trade_journal_multiday(journal, n_days=5):
    filepath = JOURNAL_FILE_5D if n_days == 5 else JOURNAL_FILE_4D
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(journal, f, indent=2)
    except Exception as e:
        print(f"[warn] Could not save {filepath}: {e}")

def audit_and_sync_multiday_journal(journal, n_days=5):
    """
    Audits the multi-day journal for missing or live trades on or before today
    using real 5m candles cached in SQLite.
    """
    if not journal:
        return journal

    from fetch_oi_data import compute_orb_execution
    import db_store

    today_str = datetime.now().strftime("%Y-%m-%d")
    by_date = {}
    for t in journal:
        by_date.setdefault(t.get("date"), []).append(t)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    modified = False

    for dt_str, trades in sorted(by_date.items()):
        if not dt_str or dt_str > today_str:
            continue
        needs_eval = any(
            (t.get("bar0_range_pct", 0.0) == 0.0 and not t.get("triggered", False) and not t.get("gap_filtered", False))
            for t in trades
        )
        if needs_eval or dt_str == today_str:
            for t in trades:
                sym = t.get("symbol")
                c.execute("""
                    SELECT bar_time, open, high, low, close, volume 
                    FROM intraday_candles_5m 
                    WHERE symbol = ? AND trade_date = ? 
                    ORDER BY bar_time ASC
                """, (sym, dt_str))
                rows = c.fetchall()
                if len(rows) >= 5:
                    bars = {
                        "valid": True,
                        "times": [r[0] for r in rows],
                        "open": [r[1] for r in rows],
                        "high": [r[2] for r in rows],
                        "low": [r[3] for r in rows],
                        "close": [r[4] for r in rows],
                        "volume": [r[5] for r in rows],
                    }
                    prev_close = t.get("prev_close") or 0.0
                    res = compute_orb_execution(t.get("side", "BUY"), sym, bars, prev_close=prev_close, capital=10000)
                    res["date"] = dt_str
                    t.update(res)
                    modified = True

    conn.close()

    if modified:
        save_trade_journal_multiday(journal, n_days)
        rule_ver = f"ORB_5M_{n_days}D"
        try:
            for dt_str, day_trades in by_date.items():
                if dt_str <= today_str:
                    db_store.save_trade_executions(dt_str, day_trades, rule_version=rule_ver, prune_stale=True, db_path=DB_PATH)
        except Exception as e:
            print(f"[warn] DB multi-day sync failed: {e}")

    return journal

def extract_top_multiday_candidates(hist_rows):
    """
    Extracts top 5 BUY candidates and top 5 SELL candidates based on institutional multi-day scoring.
    """
    hist_buys, hist_sells = [], []
    for r in hist_rows:
        if r["price_chg"] > 0:
            deliv = r.get("avg_deliv") or 30.0
            score = (deliv * 1.2) + (r["oi_pct"] * 0.8) + (r["price_chg"] * 1.5)
            r_c = dict(r)
            r_c["score"] = score
            r_c["side"] = "BUY"
            hist_buys.append(r_c)
        elif r["price_chg"] < 0:
            deliv = r.get("avg_deliv") or 30.0
            score = (deliv * 1.1) + (abs(r["oi_pct"]) * 0.9) + (abs(r["price_chg"]) * 1.6)
            r_c = dict(r)
            r_c["score"] = score
            r_c["side"] = "SELL"
            hist_sells.append(r_c)
    top_buys = sorted(hist_buys, key=lambda x: x["score"], reverse=True)[:5]
    top_sells = sorted(hist_sells, key=lambda x: x["score"], reverse=True)[:5]
    return top_buys, top_sells

def record_multiday_candidates_to_journal(candidates, session_date_str, n_days=5):
    """
    Ensures today's multi-day candidates exist in the multi-day journal,
    fetches their 5m intraday candles, computes ORB execution, and saves to JSON & DB.
    """
    if not candidates:
        return load_trade_journal_multiday(n_days)

    from fetch_oi_data import fetch_intraday_5m_bars, compute_orb_execution
    import db_store

    journal = load_trade_journal_multiday(n_days)
    symbols = [c["symbol"] for c in candidates]
    bars = fetch_intraday_5m_bars(symbols, target_date=session_date_str)

    modified = False
    for c in candidates:
        sym = c["symbol"]
        side = c["side"]
        prev_close = c.get("curr_close") or c.get("prev_close") or 0.0
        b = bars.get(sym)
        t = compute_orb_execution(side, sym, b, prev_close=prev_close, capital=10000)

        record = {
            "date": session_date_str,
            "symbol": sym,
            "side": side,
            "triggered": t.get("triggered", False),
            "trigger_time": t.get("trigger_time", "-"),
            "entry_price": t.get("entry_price", 0.0),
            "sl_price": t.get("sl_price", 0.0),
            "qty": t.get("qty", 0),
            "risk_rupees": t.get("risk_rupees", 0.0),
            "risk_pct": t.get("risk_pct", 0.0),
            "max_profit_rupees": t.get("max_profit_rupees", 0.0),
            "max_profit_pct": t.get("max_profit_pct", 0.0),
            "exit_price": t.get("exit_price", 0.0),
            "exit_time": t.get("exit_time", "15:10"),
            "exit_reason": t.get("exit_reason", "-"),
            "margin_used": t.get("margin_used", 0.0),
            "pnl": t.get("pnl", 0.0),
            "pnl_pct": t.get("pnl_pct", 0.0),
            "target_1to1": t.get("target_1to1", 0.0),
            "target_1to1_hit": t.get("target_1to1_hit", False),
            "target_1to1_time": t.get("target_1to1_time", "-"),
            "pnl_1to1": t.get("pnl_1to1", 0.0),
            "pnl_1to1_pct": t.get("pnl_1to1_pct", 0.0),
            "gap_filtered": t.get("gap_filtered", False),
            "erratic_filtered": t.get("erratic_filtered", False),
            "prev_close": prev_close,
            "gap_pct": t.get("gap_pct", 0.0),
            "bar0_range_pct": t.get("bar0_range_pct", 0.0),
            "filter_open_prev_failed": t.get("filter_open_prev_failed", False),
            "filter_extreme_gap_failed": t.get("filter_extreme_gap_failed", False),
            "filter_erratic_range_failed": t.get("filter_erratic_range_failed", False),
            "sim_triggered": t.get("sim_triggered", False),
            "sim_entry_price": t.get("sim_entry_price", 0.0),
            "sim_trigger_time": t.get("sim_trigger_time", "-"),
            "sim_sl_price": t.get("sim_sl_price", 0.0),
            "sim_exit_price": t.get("sim_exit_price", 0.0),
            "sim_exit_time": t.get("sim_exit_time", "15:10"),
            "sim_exit_reason": t.get("sim_exit_reason", "LTP"),
            "sim_qty": t.get("sim_qty", 0),
            "sim_margin_used": t.get("sim_margin_used", 0.0),
            "sim_risk_rupees": t.get("sim_risk_rupees", 0.0),
            "sim_risk_pct": t.get("sim_risk_pct", 0.0),
            "sim_max_profit_rupees": t.get("sim_max_profit_rupees", 0.0),
            "sim_max_profit_pct": t.get("sim_max_profit_pct", 0.0),
            "sim_target_1to1": t.get("sim_target_1to1", 0.0),
            "sim_target_1to1_hit": t.get("sim_target_1to1_hit", False),
            "sim_target_1to1_time": t.get("sim_target_1to1_time", "-"),
            "sim_pnl": t.get("sim_pnl", 0.0),
            "sim_pnl_pct": t.get("sim_pnl_pct", 0.0),
            "sim_pnl_1to1": t.get("sim_pnl_1to1", 0.0),
            "sim_pnl_1to1_pct": t.get("sim_pnl_1to1_pct", 0.0),
        }

        replaced = False
        for idx, item in enumerate(journal):
            if item.get("date") == session_date_str and item.get("symbol") == sym:
                journal[idx] = record
                replaced = True
                modified = True
                break
        if not replaced:
            journal.append(record)
            modified = True

    if modified:
        save_trade_journal_multiday(journal, n_days)
        rule_ver = f"ORB_5M_{n_days}D"
        try:
            today_trades = [t for t in journal if t.get("date") == session_date_str]
            db_store.save_trade_executions(session_date_str, today_trades, rule_version=rule_ver, prune_stale=True, db_path=DB_PATH)
        except Exception as e:
            print(f"[warn] DB multi-day save failed: {e}")

    return journal

def render_multiday_journal_deck(journal, horizon_id="5d", horizon_title="1-Week (5-Day Trend)"):
    """
    Renders the complete Historical Trade Journal Deck for the given multi-day horizon:
    - Hero KPIs
    - Dual Equity Curve Chart canvas
    - Strategy Mode simulator bar (Default vs What-If)
    - Interactive 'Show Only Executed Trades' toggle
    - Full day-by-day executed trades table
    """
    if not journal:
        return f"""
        <div class="sec" style="padding:25px;margin:12px;text-align:center">
          <h3 style="color:#fff;margin-bottom:8px">&#128220; No {horizon_title} Historical Trades Recorded Yet</h3>
          <p style="color:var(--muted);font-size:11px">Multi-day historical journal accumulates as sessions complete.</p>
        </div>
        """

    trig_trades = [t for t in journal if t.get("triggered")]
    total_trades = len(trig_trades)
    total_pnl = round(sum(t.get("pnl", 0.0) for t in trig_trades), 2)
    wins = [t for t in trig_trades if t.get("pnl", 0.0) > 0]
    losses = [t for t in trig_trades if t.get("pnl", 0.0) < 0]
    win_rate = round((len(wins) / total_trades * 100), 1) if total_trades > 0 else 0.0

    total_profit = sum(t.get("pnl", 0.0) for t in wins)
    total_loss = abs(sum(t.get("pnl", 0.0) for t in losses))
    profit_factor = round(total_profit / total_loss, 2) if total_loss > 0 else (round(total_profit, 2) if total_profit > 0 else 0.0)

    # Day-wise chronological equity points
    all_dates_asc = sorted(list(set(t.get("date", "") for t in journal if t.get("date"))))
    cum_norm = 0.0
    cum_1to1 = 0.0

    for d in all_dates_asc:
        day_trig_trades = [t for t in journal if t.get("date") == d and t.get("triggered")]
        cum_norm += sum(t.get("pnl", 0.0) for t in day_trig_trades)
        cum_1to1 += sum(t.get("pnl_1to1", 0.0) for t in day_trig_trades)

    total_risk = sum(t.get("risk_rupees", 0.0) for t in trig_trades)
    total_max_profit = sum(t.get("max_profit_rupees", 0.0) for t in trig_trades)

    pnl_sign = "+" if total_pnl >= 0 else ""
    pnl_color = "var(--green)" if total_pnl >= 0 else "var(--red)"

    total_1to1_pnl = round(cum_1to1, 2)
    pnl_1to1_sign = "+" if total_1to1_pnl >= 0 else ""
    pnl_1to1_color = "var(--green)" if total_1to1_pnl >= 0 else "var(--red)"

    dates_desc = sorted(list(set(t.get("date", "") for t in journal if t.get("date"))), reverse=True)

    tbody_html = ""
    for d in dates_desc:
        day_trades = [t for t in journal if t.get("date") == d]
        day_trig = [t for t in day_trades if t.get("triggered")]
        day_pnl = round(sum(t.get("pnl", 0.0) for t in day_trig), 2)
        day_sign = "+" if day_pnl >= 0 else ""
        day_color = "var(--green)" if day_pnl >= 0 else ("var(--red)" if day_pnl < 0 else "var(--muted)")
        day_margin = sum(t.get("margin_used", 0.0) for t in day_trig)
        day_roi = round(day_pnl / max(day_margin, 1) * 100, 2) if day_margin > 0 else 0.0

        day_1to1 = round(sum(t.get("pnl_1to1", 0.0) for t in day_trig), 2)
        day_1to1_sign = "+" if day_1to1 >= 0 else ""
        day_1to1_color = "var(--green)" if day_1to1 >= 0 else ("var(--red)" if day_1to1 < 0 else "var(--muted)")

        tbody_html += f"""<tr class="journal-day-header" id="day-header-{horizon_id}-{d}" style="background:#0f1522;border-top:2px solid var(--blue);border-bottom:1px solid #28324a">
          <td colspan="9" style="padding:10px 14px;font-size:12px;font-weight:700">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px">
              <span id="day-header-title-{horizon_id}-{d}" style="color:#fff">&#128197; Trading Session: <span style="color:var(--blue)">{d}</span> ({len(day_trig)} / {len(day_trades)} Executed)</span>
              <div style="display:flex;gap:16px;align-items:center">
                <span id="day-header-1to1-{horizon_id}-{d}">1:1 Target Net P&amp;L: <b style="color:{day_1to1_color};font-size:13px">{day_1to1_sign}&#8377;{fmt(day_1to1, 1)}</b></span>
                <span id="day-header-norm-{horizon_id}-{d}">Normal Net P&amp;L (3:10 PM): <b style="color:{day_color};font-size:13px">{day_sign}&#8377;{fmt(day_pnl, 1)}</b> ({day_sign}{day_roi}% 5X ROI)</span>
              </div>
            </div>
          </td>
        </tr>"""

        for idx, t in enumerate(day_trades, 1):
            side_badge = f'<span class="rank-badge rank-buy">BUY</span>' if t["side"] == "BUY" else f'<span class="rank-badge rank-sell">SELL</span>'
            row_class = f"journal-row-exec-{horizon_id}" if t["triggered"] else f"journal-row-unexec-{horizon_id}"

            if t["triggered"]:
                now_ist = datetime.now()
                is_live_intraday = (t.get("date") == now_ist.strftime("%Y-%m-%d")) and (now_ist.hour < 15 or (now_ist.hour == 15 and now_ist.minute < 10))
                sign = "+" if t["pnl"] >= 0 else ""
                pnl_cls = "pos" if t["pnl"] >= 0 else "neg"
                pnl_cell = f'<span class="pc {pnl_cls}">{sign}&#8377;{fmt(t["pnl"], 1)} ({sign}{fmt(t["pnl_pct"], 2)}%)</span>'
                if t.get("exit_reason") == "SL Hit":
                    status_txt = f'<span style="color:var(--red);font-weight:700">&#128721; SL Hit</span>'
                    details = f"Trigger: <b>{t.get('trigger_time','-')}</b> &middot; Qty: <b>{t.get('qty',0)}</b> @ &#8377;{fmt(t.get('entry_price',0),1)} &rarr; Exit: &#8377;{fmt(t.get('exit_price',0),1)} ({t.get('exit_time','-')})"
                elif is_live_intraday:
                    status_txt = '<span style="color:var(--blue);font-weight:700">&#128994; In Trade</span>'
                    details = f"Trigger: <b>{t.get('trigger_time','-')}</b> &middot; Qty: <b>{t.get('qty',0)}</b> @ &#8377;{fmt(t.get('entry_price',0),1)} &rarr; LTP: &#8377;{fmt(t.get('exit_price',0),1)} (Live)"
                else:
                    status_txt = '<span style="color:var(--green);font-weight:700">&#9200; 3:10 PM Squareoff</span>'
                    details = f"Trigger: <b>{t.get('trigger_time','-')}</b> &middot; Qty: <b>{t.get('qty',0)}</b> @ &#8377;{fmt(t.get('entry_price',0),1)} &rarr; Exit: &#8377;{fmt(t.get('exit_price',0),1)} ({t.get('exit_time','-')})"
                risk_cell = f"<span style='color:#ff7799'>&#8377;{fmt(t.get('risk_rupees',0),1)}<br><span style='font-size:9.5px;color:var(--muted)'>({t.get('risk_pct',0)}% risk)</span></span>"
                max_prof_cell = f"<span style='color:var(--green);font-weight:700'>+&#8377;{fmt(t.get('max_profit_rupees',0),1)}<br><span style='font-size:9.5px;color:var(--green)'>({t.get('max_profit_pct',0)}% peak)</span></span>"
                sign_1to1 = "+" if t.get("pnl_1to1", 0.0) >= 0 else ""
                cls_1to1 = "pos" if t.get("pnl_1to1", 0.0) >= 0 else "neg"
                if t.get("target_1to1_hit"):
                    hit_note = f"<span style='font-size:9.5px;color:var(--green)'>&#10004; Hit @ {t.get('target_1to1_time')}</span>"
                elif t.get("exit_reason") == "SL Hit":
                    hit_note = "<span style='font-size:9.5px;color:var(--red)'>&#10006; Stopped Out</span>"
                elif is_live_intraday:
                    hit_note = "<span style='font-size:9.5px;color:var(--blue)'>&#128994; In Trade</span>"
                else:
                    hit_note = "<span style='font-size:9.5px;color:var(--yel)'>&#9200; 3:10 PM Squareoff</span>"
                pnl_1to1_cell = f'<span class="pc {cls_1to1}">{sign_1to1}&#8377;{fmt(t.get("pnl_1to1",0), 1)} ({sign_1to1}{fmt(t.get("pnl_1to1_pct",0), 2)}%)</span><br>{hit_note}'
            elif t.get("gap_filtered"):
                pnl_cell = '<span class="neu">&#8377;0.00</span>'
                status_txt = '<span class="pill neg" style="background:rgba(255,119,153,0.15);color:var(--red)">&#9888; Skipped (Gap Rule)</span>'
                details = "<span style='color:var(--muted)'>Disqualified by opening gap alignment</span>"
                risk_cell = "-"
                max_prof_cell = "-"
                pnl_1to1_cell = '<span class="neu">&#8377;0.00</span>'
            elif t.get("erratic_filtered"):
                pnl_cell = '<span class="neu">&#8377;0.00</span>'
                status_txt = '<span class="pill neg" style="background:rgba(245,200,66,0.15);color:var(--yel)">&#9888; Skipped (Erratic 2%)</span>'
                details = "<span style='color:var(--muted)'>1st 5-min candle range &gt; 2.0%</span>"
                risk_cell = "-"
                max_prof_cell = "-"
                pnl_1to1_cell = '<span class="neu">&#8377;0.00</span>'
            else:
                pnl_cell = '<span class="neu">&#8377;0.00</span>'
                status_txt = '<span class="neu">&#9203; Not Triggered</span>'
                details = "<span style='color:var(--muted)'>Price never broke 1st 5-min breakout level</span>"
                risk_cell = "-"
                max_prof_cell = "-"
                pnl_1to1_cell = '<span class="neu">&#8377;0.00</span>'

            row_id = f"trade-row-{horizon_id}-{t.get('date')}-{t.get('symbol')}"
            tbody_html += f"""<tr class="{row_class}" id="{row_id}">
              <td><span style="color:var(--muted)">#{idx}</span> <b>{t.get('date')}</b></td>
              <td><b>{t['symbol']}</b></td>
              <td>{side_badge}</td>
              <td class="cell-status">{status_txt}</td>
              <td class="cell-details">{details}</td>
              <td class="cell-risk">{risk_cell}</td>
              <td class="cell-maxprof">{max_prof_cell}</td>
              <td class="cell-1to1">{pnl_1to1_cell}</td>
              <td class="cell-norm">{pnl_cell}</td>
            </tr>"""

    html = f"""
    <!-- {horizon_title.upper()} HISTORIC TRADE JOURNAL -->
    <div style="margin:20px 10px 10px 10px;padding:12px 16px;background:linear-gradient(135deg,#0e1526,#162035);border:1px solid #233555;border-radius:6px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
      <div>
        <div style="font-size:14px;font-weight:700;color:#fff;display:flex;align-items:center;gap:8px">
          &#128220; {horizon_title} Strategy Historical Trade Journal
          <span style="background:rgba(61,155,255,0.18);color:var(--blue);font-size:10px;padding:2px 8px;border-radius:4px;font-weight:700">10 SESSIONS AUDITED</span>
        </div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px">
          Intraday ORB performance (5X leverage, &#8377;10k margin per trade, 3:10 PM squareoff &amp; 1:1 risk-reward target exit).
        </div>
      </div>
      <div style="font-size:11px;color:#00ff9d;font-weight:700;background:rgba(0,196,122,0.12);padding:4px 10px;border-radius:4px;border:1px solid rgba(0,196,122,0.25)">
        Net Normal P&amp;L: {pnl_sign}&#8377;{fmt(total_pnl, 1)} ({win_rate}% Win Rate)
      </div>
    </div>

    <!-- JOURNAL HERO METRICS -->
    <div class="pnl-hero">
      <div class="pnl-kpi">
        <div class="pnl-kpi-lbl">Cumulative Normal P&amp;L (3:10 PM)</div>
        <div class="pnl-kpi-val" id="kpi-normal-pnl-val-{horizon_id}" style="color:{pnl_color}">{pnl_sign}&#8377;{fmt(total_pnl, 1)}</div>
        <div class="pnl-kpi-sub" id="kpi-normal-trades-sub-{horizon_id}">Total Triggered Trades: <b>{total_trades}</b></div>
      </div>
      <div class="pnl-kpi">
        <div class="pnl-kpi-lbl">Cumulative 1:1 Target P&amp;L</div>
        <div class="pnl-kpi-val" id="kpi-1to1-pnl-val-{horizon_id}" style="color:{pnl_1to1_color}">{pnl_1to1_sign}&#8377;{fmt(total_1to1_pnl, 1)}</div>
        <div class="pnl-kpi-sub">Fixed 1:1 Risk-Reward Strategy</div>
      </div>
      <div class="pnl-kpi">
        <div class="pnl-kpi-lbl">Win Rate %</div>
        <div class="pnl-kpi-val" id="kpi-winrate-val-{horizon_id}" style="color:var(--blue)">{win_rate}%</div>
        <div class="pnl-kpi-sub" id="kpi-wins-losses-sub-{horizon_id}"><span style="color:var(--green)">{len(wins)} Wins</span> &middot; <span style="color:var(--red)">{len(losses)} Losses</span></div>
      </div>
      <div class="pnl-kpi">
        <div class="pnl-kpi-lbl">Peak Profit Before Exit</div>
        <div class="pnl-kpi-val" id="kpi-maxprofit-val-{horizon_id}" style="color:var(--green)">+&#8377;{fmt(total_max_profit, 0)}</div>
        <div class="pnl-kpi-sub" id="kpi-profitfactor-sub-{horizon_id}">Profit Factor: <b>{profit_factor}</b></div>
      </div>
    </div>

    <!-- DUAL EQUITY CURVE CHART -->
    <div class="sec" style="margin:0 10px 15px 10px;padding:14px">
      <div class="sh" style="background:#131824;margin:-14px -14px 12px -14px;border-bottom:1px solid var(--border)">
        <div>&#128200; {horizon_title} Comparative Equity Curve: Normal P&amp;L vs 1:1 Target Exit</div>
        <div style="font-size:10px;display:flex;gap:12px">
          <span style="color:var(--blue);font-weight:700">&mdash; Normal Intraday P&amp;L (3:10 PM)</span>
          <span style="color:var(--green);font-weight:700">&mdash; 1:1 Target Exit P&amp;L</span>
        </div>
      </div>
      <div style="width:100%;height:220px;position:relative">
        <canvas id="equityCurveChart_{horizon_id}"></canvas>
      </div>
    </div>

    <!-- WHAT-IF STRATEGY RULES SIMULATOR CONTROL BAR -->
    <div class="sec" style="margin:0 10px 15px 10px;padding:12px 16px;background:#101726;border:1px solid #23304c;border-radius:6px">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:12px">
        <div style="display:flex;align-items:center;gap:10px">
          <span style="font-size:13.5px;font-weight:700;color:#fff">&#9881;&#65039; Strategy Mode:</span>
          <div style="display:inline-flex;background:#0b0f19;border-radius:5px;border:1px solid #1c263c;padding:2px">
            <button id="btn-mode-default-{horizon_id}" onclick="setJournalModeMulti('{horizon_id}', 'default')" style="background:#1b2438;color:#00ff9d;border:none;padding:5px 12px;border-radius:4px;font-size:11.5px;font-weight:700;cursor:pointer;display:flex;align-items:center;gap:5px">
              &#128737;&#65039; Default Strategy (Standard Baseline)
            </button>
            <button id="btn-mode-whatif-{horizon_id}" onclick="setJournalModeMulti('{horizon_id}', 'whatif')" style="background:transparent;color:var(--muted);border:none;padding:5px 12px;border-radius:4px;font-size:11.5px;font-weight:600;cursor:pointer;display:flex;align-items:center;gap:5px">
              &#129514; Custom What-If Simulator
            </button>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:10px">
          <div id="sim-status-summary-{horizon_id}" style="font-size:11px;color:var(--green);font-weight:600">
            &#9679; Default Strategy Active &middot; {total_trades} Executed &middot; Net P&amp;L: {pnl_sign}&#8377;{fmt(total_pnl, 1)}
          </div>
          <button id="btn-reset-rules-{horizon_id}" onclick="resetSimulatorToDefaultMulti('{horizon_id}')" style="display:none;background:#172033;color:var(--blue);border:1px solid #263553;padding:4px 9px;border-radius:4px;font-size:10.5px;cursor:pointer;font-weight:600">
            &#8634; Reset to Default Settings
          </button>
        </div>
      </div>

      <!-- Simulator Interactive Checkboxes (Visible only in What-If mode) -->
      <div id="whatif-controls-panel-{horizon_id}" style="display:none;padding:10px 14px;background:#0b0f19;border-radius:5px;border:1px solid #1c263c;margin-top:6px">
        <div style="font-size:10.5px;color:var(--muted);margin-bottom:8px">
          Toggle rules below to backtest alternative entry logic across all historical trades:
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:16px;align-items:center">
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:11.5px;color:#fff" title="Toggle rule: BUY must open above or equal to Prev Close, SELL must open below or equal to Prev Close">
            <input type="checkbox" id="sim-rule-open-prev-{horizon_id}" checked onchange="recomputeWhatIfSimulatorMulti('{horizon_id}')" style="accent-color:var(--blue);cursor:pointer;width:15px;height:15px">
            <span><b>Rule 1: Open vs Prev Close</b> <span style="color:var(--muted);font-size:10.5px">(BUY &ge; Prev Close, SELL &le; Prev Close)</span></span>
          </label>
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:11.5px;color:#fff" title="Toggle rule: Filter runaway gap-ups (> +2.5%) or panic gap-downs (< -2.5%)">
            <input type="checkbox" id="sim-rule-extreme-gap-{horizon_id}" checked onchange="recomputeWhatIfSimulatorMulti('{horizon_id}')" style="accent-color:var(--blue);cursor:pointer;width:15px;height:15px">
            <span><b>Rule 2: Max Opening Gap</b> <span style="color:var(--muted);font-size:10.5px">(&le; 2.5% exhaustion cap)</span></span>
          </label>
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:11.5px;color:#fff" title="Toggle rule: Filter stocks where 1st 5-minute candle range exceeds 2.0%">
            <input type="checkbox" id="sim-rule-erratic-{horizon_id}" checked onchange="recomputeWhatIfSimulatorMulti('{horizon_id}')" style="accent-color:var(--blue);cursor:pointer;width:15px;height:15px">
            <span><b>Rule 3: 1st 5-Min Range</b> <span style="color:var(--muted);font-size:10.5px">(&le; 2.0% volatility cap)</span></span>
          </label>
        </div>
      </div>
    </div>

    <!-- TABLE OF ALL HISTORIC TRADES WITH FILTER & DAILY BREAKS -->
    <div class="sec" style="margin:0 10px 15px 10px">
      <div class="sh" style="background:#131824;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
        <div>&#128220; {horizon_title} Complete Trade Execution History &amp; Risk/Reward Journal</div>
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <label style="font-size:11px;color:#00ff9d;cursor:pointer;display:flex;align-items:center;gap:6px">
            <input type="checkbox" id="journal-exec-filter-{horizon_id}" checked onchange="onFilterExecutedToggledMulti('{horizon_id}', this.checked)" style="accent-color:var(--green);cursor:pointer;width:14px;height:14px">
            <span><b>Show Only Executed Trades</b></span>
          </label>
          <span class="badge" style="background:rgba(0,196,122,0.15);color:var(--green)">Permanent Audit Log</span>
        </div>
      </div>
      <div class="tw">
        <table class="rec-tbl pnl-table" id="journal-table-{horizon_id}">
          <thead>
            <tr>
              <th>Date</th>
              <th>Symbol</th>
              <th>Side</th>
              <th>Status</th>
              <th>Execution Details (Entry &rarr; Exit)</th>
              <th>Risk Taken (&#8377; &amp; %)</th>
              <th>Max Profit Reached</th>
              <th>1:1 Target Exit P&amp;L</th>
              <th>Normal P&amp;L (3:10 PM)</th>
            </tr>
          </thead>
          <tbody>{tbody_html}</tbody>
        </table>
      </div>
    </div>
    """
    return html

def generate_multiday_journal_scripts(journal_4d, journal_5d):
    """
    Generates the client-side JavaScript engine for both 4D and 5D multi-day journals.
    """
    j4_json = json.dumps(journal_4d)
    j5_json = json.dumps(journal_5d)

    return f"""
    <script>
    const MULTIDAY_JOURNAL_DATA_4D = {j4_json};
    const MULTIDAY_JOURNAL_DATA_5D = {j5_json};

    var multiEquityCharts = {{ '4d': null, '5d': null }};
    var multiJournalModes = {{ '4d': 'default', '5d': 'default' }};

    function initEquityChartMulti(h) {{
      var canvas = document.getElementById('equityCurveChart_' + h);
      if (!canvas) return;
      if (typeof Chart === 'undefined') return;

      var ctx = canvas.getContext('2d');
      if (multiEquityCharts[h]) {{
        multiEquityCharts[h].destroy();
      }}

      var dataList = (h === '4d') ? MULTIDAY_JOURNAL_DATA_4D : MULTIDAY_JOURNAL_DATA_5D;
      var dateSet = [];
      dataList.forEach(function(t) {{
        if (t.date && dateSet.indexOf(t.date) === -1) dateSet.push(t.date);
      }});
      dateSet.sort();

      var labels = ['Start'];
      var curveNorm = [0];
      var curve1to1 = [0];
      var cumN = 0;
      var cum1 = 0;

      dateSet.forEach(function(d) {{
        labels.push(d);
        var dayTrades = dataList.filter(function(t) {{ return t.date === d && t.triggered; }});
        var dayN = dayTrades.reduce(function(acc, t) {{ return acc + (t.pnl || 0); }}, 0);
        var day1 = dayTrades.reduce(function(acc, t) {{ return acc + (t.pnl_1to1 || 0); }}, 0);
        cumN += dayN;
        cum1 += day1;
        curveNorm.push(Math.round(cumN * 10) / 10);
        curve1to1.push(Math.round(cum1 * 10) / 10);
      }});

      multiEquityCharts[h] = new Chart(ctx, {{
        type: 'line',
        data: {{
          labels: labels,
          datasets: [
            {{
              label: 'Normal Intraday P&L (3:10 PM)',
              data: curveNorm,
              borderColor: '#3d9bff',
              backgroundColor: 'rgba(61,155,255,0.08)',
              borderWidth: 2.2,
              pointRadius: 4,
              pointBackgroundColor: '#3d9bff',
              tension: 0.25,
              fill: true
            }},
            {{
              label: '1:1 Target Exit P&L',
              data: curve1to1,
              borderColor: '#00c47a',
              backgroundColor: 'rgba(0,196,122,0.06)',
              borderWidth: 2.2,
              pointRadius: 4,
              pointBackgroundColor: '#00c47a',
              tension: 0.25,
              fill: true
            }}
          ]
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          plugins: {{
            legend: {{ display: false }},
            tooltip: {{
              callbacks: {{
                label: function(ctx) {{
                  var v = ctx.parsed.y;
                  return ' ' + ctx.dataset.label + ': ' + (v >= 0 ? '+' : '') + '₹' + v.toLocaleString('en-IN', {{minimumFractionDigits: 1, maximumFractionDigits: 1}});
                }}
              }}
            }}
          }},
          scales: {{
            x: {{
              grid: {{ color: 'rgba(255,255,255,0.05)' }},
              ticks: {{ color: '#8892b0', font: {{ size: 10 }} }}
            }},
            y: {{
              grid: {{ color: 'rgba(255,255,255,0.07)' }},
              ticks: {{
                color: '#8892b0',
                font: {{ size: 10 }},
                callback: function(val) {{
                  return (val >= 0 ? '+' : '') + '₹' + val.toLocaleString('en-IN');
                }}
              }}
            }}
          }}
        }}
      }});
    }}

    function onFilterExecutedToggledMulti(h, showOnlyExec) {{
      var rowsUnexec = document.querySelectorAll('.journal-row-unexec-' + h);
      rowsUnexec.forEach(function(row) {{
        row.style.display = showOnlyExec ? 'none' : '';
      }});
    }}

    function setJournalModeMulti(h, mode) {{
      multiJournalModes[h] = mode;
      var btnDefault = document.getElementById('btn-mode-default-' + h);
      var btnWhatIf = document.getElementById('btn-mode-whatif-' + h);
      var panel = document.getElementById('whatif-controls-panel-' + h);
      var btnReset = document.getElementById('btn-reset-rules-' + h);

      if (mode === 'default') {{
        if (btnDefault) {{ btnDefault.style.background = '#1b2438'; btnDefault.style.color = '#00ff9d'; }}
        if (btnWhatIf) {{ btnWhatIf.style.background = 'transparent'; btnWhatIf.style.color = 'var(--muted)'; }}
        if (panel) panel.style.display = 'none';
        if (btnReset) btnReset.style.display = 'none';
        renderDefaultAuditLogMulti(h);
      }} else {{
        if (btnDefault) {{ btnDefault.style.background = 'transparent'; btnDefault.style.color = 'var(--muted)'; }}
        if (btnWhatIf) {{ btnWhatIf.style.background = '#1b2438'; btnWhatIf.style.color = 'var(--blue)'; }}
        if (panel) panel.style.display = 'block';
        if (btnReset) btnReset.style.display = 'inline-block';
        recomputeWhatIfSimulatorMulti(h);
      }}
    }}

    function resetSimulatorToDefaultMulti(h) {{
      var r1 = document.getElementById('sim-rule-open-prev-' + h); if (r1) r1.checked = true;
      var r2 = document.getElementById('sim-rule-extreme-gap-' + h); if (r2) r2.checked = true;
      var r3 = document.getElementById('sim-rule-erratic-' + h); if (r3) r3.checked = true;
      recomputeWhatIfSimulatorMulti(h);
    }}

    function renderDefaultAuditLogMulti(h) {{
      var dataList = (h === '4d') ? MULTIDAY_JOURNAL_DATA_4D : MULTIDAY_JOURNAL_DATA_5D;
      var trigTrades = dataList.filter(function(t) {{ return t.triggered; }});
      var totalPnl = trigTrades.reduce(function(a, b) {{ return a + (b.pnl || 0); }}, 0);
      var total1to1 = trigTrades.reduce(function(a, b) {{ return a + (b.pnl_1to1 || 0); }}, 0);
      var wins = trigTrades.filter(function(t) {{ return (t.pnl || 0) > 0; }});
      var losses = trigTrades.filter(function(t) {{ return (t.pnl || 0) < 0; }});
      var winRate = trigTrades.length > 0 ? (wins.length / trigTrades.length * 100).toFixed(1) : 0;
      var maxProfit = trigTrades.reduce(function(a, b) {{ return a + (b.max_profit_rupees || 0); }}, 0);

      var elNorm = document.getElementById('kpi-normal-pnl-val-' + h);
      if (elNorm) {{
        elNorm.style.color = totalPnl >= 0 ? 'var(--green)' : 'var(--red)';
        elNorm.innerHTML = (totalPnl >= 0 ? '+' : '') + '&#8377;' + totalPnl.toLocaleString('en-IN', {{minimumFractionDigits: 1, maximumFractionDigits: 1}});
      }}
      var el1to1 = document.getElementById('kpi-1to1-pnl-val-' + h);
      if (el1to1) {{
        el1to1.style.color = total1to1 >= 0 ? 'var(--green)' : 'var(--red)';
        el1to1.innerHTML = (total1to1 >= 0 ? '+' : '') + '&#8377;' + total1to1.toLocaleString('en-IN', {{minimumFractionDigits: 1, maximumFractionDigits: 1}});
      }}
      var elWin = document.getElementById('kpi-winrate-val-' + h);
      if (elWin) elWin.textContent = winRate + '%';
      var elSub = document.getElementById('kpi-wins-losses-sub-' + h);
      if (elSub) elSub.innerHTML = '<span style="color:var(--green)">' + wins.length + ' Wins</span> &middot; <span style="color:var(--red)">' + losses.length + ' Losses</span>';
      var elTrig = document.getElementById('kpi-normal-trades-sub-' + h);
      if (elTrig) elTrig.innerHTML = 'Total Triggered Trades: <b>' + trigTrades.length + '</b>';

      initEquityChartMulti(h);
      var cb = document.getElementById('journal-exec-filter-' + h);
      if (cb) onFilterExecutedToggledMulti(h, cb.checked);
    }}

    function recomputeWhatIfSimulatorMulti(h) {{
      var r1 = document.getElementById('sim-rule-open-prev-' + h) ? document.getElementById('sim-rule-open-prev-' + h).checked : true;
      var r2 = document.getElementById('sim-rule-extreme-gap-' + h) ? document.getElementById('sim-rule-extreme-gap-' + h).checked : true;
      var r3 = document.getElementById('sim-rule-erratic-' + h) ? document.getElementById('sim-rule-erratic-' + h).checked : true;

      var dataList = (h === '4d') ? MULTIDAY_JOURNAL_DATA_4D : MULTIDAY_JOURNAL_DATA_5D;
      var trigTrades = [];

      dataList.forEach(function(t) {{
        var passes = true;
        if (r1 && t.filter_open_prev_failed) passes = false;
        if (r2 && t.filter_extreme_gap_failed) passes = false;
        if (r3 && t.filter_erratic_range_failed) passes = false;

        var simTrig = passes && t.sim_triggered;
        if (simTrig) trigTrades.push(t);
      }});

      var totalPnl = trigTrades.reduce(function(a, b) {{ return a + (b.sim_pnl || 0); }}, 0);
      var total1to1 = trigTrades.reduce(function(a, b) {{ return a + (b.sim_pnl_1to1 || 0); }}, 0);
      var wins = trigTrades.filter(function(t) {{ return (t.sim_pnl || 0) > 0; }});
      var losses = trigTrades.filter(function(t) {{ return (t.sim_pnl || 0) < 0; }});
      var winRate = trigTrades.length > 0 ? (wins.length / trigTrades.length * 100).toFixed(1) : 0;

      var elNorm = document.getElementById('kpi-normal-pnl-val-' + h);
      if (elNorm) {{
        elNorm.style.color = totalPnl >= 0 ? 'var(--green)' : 'var(--red)';
        elNorm.innerHTML = (totalPnl >= 0 ? '+' : '') + '&#8377;' + totalPnl.toLocaleString('en-IN', {{minimumFractionDigits: 1, maximumFractionDigits: 1}});
      }}
      var el1to1 = document.getElementById('kpi-1to1-pnl-val-' + h);
      if (el1to1) {{
        el1to1.style.color = total1to1 >= 0 ? 'var(--green)' : 'var(--red)';
        el1to1.innerHTML = (total1to1 >= 0 ? '+' : '') + '&#8377;' + total1to1.toLocaleString('en-IN', {{minimumFractionDigits: 1, maximumFractionDigits: 1}});
      }}
      var elWin = document.getElementById('kpi-winrate-val-' + h);
      if (elWin) elWin.textContent = winRate + '%';
      var elSub = document.getElementById('kpi-wins-losses-sub-' + h);
      if (elSub) elSub.innerHTML = '<span style="color:var(--green)">' + wins.length + ' Wins</span> &middot; <span style="color:var(--red)">' + losses.length + ' Losses</span>';
      var elTrig = document.getElementById('kpi-normal-trades-sub-' + h);
      if (elTrig) elTrig.innerHTML = 'Simulated Triggered: <b>' + trigTrades.length + '</b>';

      var sumEl = document.getElementById('sim-status-summary-' + h);
      if (sumEl) {{
        sumEl.innerHTML = '<span style="color:var(--blue)">&#9679; Custom What-If Active &middot; ' + trigTrades.length + ' Executed &middot; Net P&L: ' + (totalPnl >= 0 ? '+' : '') + '&#8377;' + totalPnl.toFixed(1) + '</span>';
      }}
    }}
    </script>
    """

