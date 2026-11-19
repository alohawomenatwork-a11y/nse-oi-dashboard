"""
telegram_notify.py
==================
Sends a concise Daily Summary notification to Telegram after the EOD GitHub Actions run.

Required environment variables (stored as GitHub Actions Secrets):
    TELEGRAM_BOT_TOKEN : Bot token from @BotFather
    TELEGRAM_CHAT_ID   : Your Telegram user/group/channel chat ID

Usage:
    python telegram_notify.py            # send to Telegram
    python telegram_notify.py --dry-run  # print message only (no send)
"""

import json
import os
import sys
import html
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
HIST_FILE = os.path.join(ROOT, "trade_history.json")
OI_FILE = os.path.join(ROOT, "oi_data.json")
DASHBOARD_URL = "https://alohawomenatwork-a11y.github.io/nse-oi-dashboard/"


def _fmt_inr(val: float) -> str:
    sign = "+" if val > 0 else ("-" if val < 0 else "")
    return f"{sign}₹{abs(val):,.2f}"


def build_summary_message() -> str:
    if not os.path.exists(HIST_FILE):
        return "<b>NSE OI & ORB Tracker</b>\nNo trade history found yet."

    with open(HIST_FILE, "r", encoding="utf-8") as f:
        trades = json.load(f)

    if not trades:
        return "<b>NSE OI & ORB Tracker</b>\nTrade history is empty."

    latest_date = max(t["date"] for t in trades)
    day_trades = [t for t in trades if t["date"] == latest_date]

    triggered = [t for t in day_trades if t.get("triggered")]
    filtered = [t for t in day_trades if not t.get("triggered") and (t.get("gap_filtered") or t.get("erratic_filtered"))]
    not_trig = len(day_trades) - len(triggered) - len(filtered)

    day_pnl = sum(float(t.get("pnl") or 0.0) for t in triggered)
    day_pnl_1to1 = sum(float(t.get("pnl_1to1") or 0.0) for t in triggered)

    all_triggered = [t for t in trades if t.get("triggered")]
    cum_pnl = sum(float(t.get("pnl") or 0.0) for t in all_triggered)
    cum_pnl_1to1 = sum(float(t.get("pnl_1to1") or 0.0) for t in all_triggered)
    wins = sum(1 for t in all_triggered if float(t.get("pnl") or 0.0) > 0)
    win_rate = (wins / len(all_triggered) * 100) if all_triggered else 0.0

    lines = [
        f"<b>📊 NSE OI & ORB Daily Summary — {html.escape(latest_date)}</b>",
        "",
        f"<b>Trades:</b> {len(triggered)} Triggered | {len(filtered)} Filtered | {not_trig} Not Triggered",
        f"<b>Day P&amp;L (EOD):</b> {_fmt_inr(day_pnl)}  |  <b>1:1 P&amp;L:</b> {_fmt_inr(day_pnl_1to1)}",
    ]

    if triggered:
        lines.append("")
        lines.append("<b>Executed Trades:</b>")
        for t in triggered:
            sym = html.escape(t["symbol"])
            side = html.escape(t["side"])
            pnl = float(t.get("pnl") or 0.0)
            icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
            reason = "SL Hit" if t.get("exit_reason") == "SL Hit" else t.get("exit_reason", "LTP")
            t11 = " | 🎯 1:1 Hit" if t.get("target_1to1_hit") else ""
            lines.append(
                f"{icon} <b>{sym}</b> ({side} @ {t.get('trigger_time', '-')}) "
                f"₹{t.get('entry_price', 0):.1f} → ₹{t.get('exit_price', 0):.1f} [{reason}{t11}] "
                f"<b>{_fmt_inr(pnl)}</b>"
            )

    lines.extend([
        "",
        f"<b>Overall ({len(all_triggered)} trades, {win_rate:.1f}% win):</b>",
        f"• Cumulative EOD P&amp;L: <b>{_fmt_inr(cum_pnl)}</b>",
        f"• Cumulative 1:1 P&amp;L: <b>{_fmt_inr(cum_pnl_1to1)}</b>",
    ])

    if os.path.exists(OI_FILE):
        try:
            with open(OI_FILE, "r", encoding="utf-8") as f:
                oi_data = json.load(f)
            as_of = html.escape(str(oi_data.get("as_of", "")))
            fl = [r["symbol"] for r in oi_data.get("fresh_longs", [])[:3]]
            fs = [r["symbol"] for r in oi_data.get("fresh_shorts", [])[:3]]
            if fl or fs:
                lines.append("")
                lines.append(f"<b>Top OI Movers ({as_of}):</b>")
                if fl:
                    lines.append(f"🟢 Fresh Longs: {', '.join(html.escape(s) for s in fl)}")
                if fs:
                    lines.append(f"🔴 Fresh Shorts: {', '.join(html.escape(s) for s in fs)}")
        except Exception:
            pass

    lines.append("")
    lines.append(f'🔗 <a href="{DASHBOARD_URL}">Open Live Dashboard</a>')
    return "\n".join(lines)


def send_telegram(message: str, token: str, chat_id: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=15)
    if resp.status_code == 200 and resp.json().get("ok"):
        print("[OK] Telegram daily summary sent.")
        return True
    print(f"[warn] Telegram API error ({resp.status_code}): {resp.text}")
    return False


def main():
    dry_run = "--dry-run" in sys.argv
    msg = build_summary_message()

    if dry_run:
        print(msg)
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[info] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping Telegram notification.")
        return

    send_telegram(msg, token, chat_id)


if __name__ == "__main__":
    main()

