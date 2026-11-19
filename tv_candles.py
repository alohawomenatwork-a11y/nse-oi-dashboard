"""
TradingView Intraday Candle Fetcher
===================================
Fetches authentic 5m NSE candles directly from TradingView websocket API.

Supports optional timestamp filtering to isolate a specific historic trading session.
"""

import json
import random
import string
import re
import datetime
import websocket


def _gen_session(prefix="cs_"):
    return prefix + "".join(random.choices(string.ascii_lowercase, k=12))


def _create_msg(func, args):
    content = json.dumps({"m": func, "p": args})
    return f"~m~{len(content)}~m~{content}"


def fetch_tv_candles(symbol: str, exchange: str = "NSE", interval: str = "5",
                     n_bars: int = 300, start_ts: int = None, end_ts: int = None):
    """
    Fetch real historical OHLC candles from TradingView websocket (5-min by default).

    Parameters
    ----------
    symbol    : NSE ticker, e.g. "HDFCBANK"
    exchange  : default "NSE"
    interval  : candle interval in minutes, default "5"
    n_bars    : number of bars to request (use >= 300 to cover a full trading day
                PLUS enough previous days so historic dates can be found)
    start_ts  : optional Unix timestamp (seconds) — filter bars >= this time
    end_ts    : optional Unix timestamp (seconds) — filter bars <= this time

    Returns
    -------
    list of dicts: [{'time': int, 'open': float, 'high': float,
                     'low': float, 'close': float, 'volume': float}, ...]
    Sorted ascending by time.  Empty list on failure.
    """
    try:
        ws = websocket.create_connection(
            "wss://data.tradingview.com/socket.io/websocket",
            headers={"Origin": "https://data.tradingview.com"},
            timeout=6
        )
    except Exception as e:
        print(f"    [tv] Connection failed for {symbol}: {e}")
        return []

    chart_session = _gen_session("cs_")
    ticker = f"{exchange}:{symbol}"
    bars = []

    try:
        ws.send(_create_msg("set_auth_token", ["unauthorized_user_token"]))
        ws.send(_create_msg("chart_create_session", [chart_session, ""]))
        ws.send(_create_msg("resolve_symbol", [
            chart_session, "sds_sym_1",
            f'={{"adjustment":"splits","symbol":"{ticker}"}}'
        ]))
        ws.send(_create_msg("create_series", [
            chart_session, "sds_1", "s1", "sds_sym_1", interval, n_bars, ""
        ]))

        for _ in range(25):
            try:
                raw = ws.recv()
            except Exception:
                break

            # Respond to TradingView heartbeat pings
            pings = re.findall(r"~m~\d+~m~~h~(\d+)", raw)
            for p in pings:
                try:
                    ws.send(f"~m~{len('~h~' + p)}~m~~h~{p}")
                except Exception:
                    pass

            messages = re.findall(r"~m~\d+~m~({.*?})(?=(?:~m~\d+~m~)|$)", raw)
            done = False
            for msg in messages:
                try:
                    data = json.loads(msg)
                    m_type = data.get("m")
                    if m_type in ("symbol_error", "series_error"):
                        done = True
                        break
                    if m_type == "timescale_update":
                        plots = data["p"][1].get("sds_1", {}).get("s", [])
                        for pt in plots:
                            v = pt.get("v", [])
                            if len(v) >= 6:
                                bars.append({
                                    "time":   int(v[0]),
                                    "open":   round(float(v[1]), 2),
                                    "high":   round(float(v[2]), 2),
                                    "low":    round(float(v[3]), 2),
                                    "close":  round(float(v[4]), 2),
                                    "volume": round(float(v[5]), 2),
                                })
                        if bars:
                            # Got bars — no need to wait for more messages
                            done = True
                            break
                    elif m_type == "series_completed":
                        done = True
                        break
                except Exception:
                    pass

            if bars or done:
                break

        ws.close()

    except Exception as e:
        try:
            ws.close()
        except Exception:
            pass
        print(f"    [tv] Error fetching {symbol}: {e}")
        return []

    # Sort ascending
    bars.sort(key=lambda b: b["time"])

    # Apply optional timestamp filter
    if start_ts is not None or end_ts is not None:
        bars = [
            b for b in bars
            if (start_ts is None or b["time"] >= start_ts)
            and (end_ts is None or b["time"] <= end_ts)
        ]

    return bars


def fetch_tv_candles_for_date(symbol: str, trading_date, exchange: str = "NSE",
                               interval: str = "5"):
    """
    Convenience wrapper: fetches 5-min bars for a specific trading date (IST).

    trading_date : datetime.date or 'YYYY-MM-DD' string
    Returns bar_data dict ready for compute_orb_execution, or None on failure.
    """
    if isinstance(trading_date, str):
        trading_date = datetime.date.fromisoformat(trading_date)

    # IST = UTC+5:30  →  09:15 IST = 03:45 UTC, 15:30 IST = 10:00 UTC
    ist_offset = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    session_start = datetime.datetime(
        trading_date.year, trading_date.month, trading_date.day,
        9, 15, 0, tzinfo=ist_offset)
    session_end = datetime.datetime(
        trading_date.year, trading_date.month, trading_date.day,
        15, 30, 0, tzinfo=ist_offset)

    start_ts = int(session_start.timestamp())
    end_ts   = int(session_end.timestamp())

    # Request enough bars: 75 bars/day × 5 days = 375 to reach back a week
    raw = fetch_tv_candles(symbol, exchange=exchange, interval=interval,
                           n_bars=500, start_ts=start_ts, end_ts=end_ts)

    if not raw:
        return None

    return {
        "valid":  True,
        "open":   [b["open"]  for b in raw],
        "high":   [b["high"]  for b in raw],
        "low":    [b["low"]   for b in raw],
        "close":  [b["close"] for b in raw],
        "times":  [
            datetime.datetime.fromtimestamp(b["time"], tz=ist_offset).strftime("%H:%M")
            for b in raw
        ],
    }
