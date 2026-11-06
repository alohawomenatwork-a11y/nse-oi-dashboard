"""
TradingView Intraday Candle Fetcher
===================================
Fetches authentic 5m NSE candles directly from TradingView websocket API.
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

def fetch_tv_candles(symbol: str, exchange: str = "NSE", interval: str = "5", n_bars: int = 150):
    """
    Fetch exact real-time historical OHLC candles directly from TradingView websocket.
    Returns list of dicts: [{'time': timestamp, 'open': float, 'high': float, 'low': float, 'close': float, 'volume': float}, ...]
    """
    try:
        ws = websocket.create_connection(
            "wss://data.tradingview.com/socket.io/websocket",
            headers={"Origin": "https://data.tradingview.com"},
            timeout=8
        )
    except Exception as e:
        print(f"    [tv] Connection failed for {symbol}: {e}")
        return []

    chart_session = _gen_session("cs_")
    ticker = f"{exchange}:{symbol}"

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

        bars = []
        for _ in range(18):
            raw = ws.recv()
            pings = re.findall(r"~m~\d+~m~~h~(\d+)", raw)
            for p in pings:
                ws.send(f"~m~{len('~h~' + p)}~m~~h~{p}")

            messages = re.findall(r"~m~\d+~m~({.*?})(?=(?:~m~\d+~m~)|$)", raw)
            for msg in messages:
                try:
                    data = json.loads(msg)
                    if data.get("m") == "timescale_update":
                        plots = data["p"][1].get("sds_1", {}).get("s", [])
                        for pt in plots:
                            v = pt.get("v", [])
                            if len(v) >= 6:
                                bars.append({
                                    "time": v[0],
                                    "open": round(float(v[1]), 2),
                                    "high": round(float(v[2]), 2),
                                    "low": round(float(v[3]), 2),
                                    "close": round(float(v[4]), 2),
                                    "volume": round(float(v[5]), 2)
                                })
                        if bars:
                            ws.close()
                            return bars
                except Exception:
                    pass
        ws.close()
        return bars
    except Exception as e:
        try:
            ws.close()
        except Exception:
            pass
        return []

