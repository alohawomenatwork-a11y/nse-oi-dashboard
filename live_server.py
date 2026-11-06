"""
NSE OI Dashboard — Live Price Server
=====================================
Serves dashboard.html at http://localhost:7384/
Provides /api/live endpoint for real-time NSE prices via yfinance.
Provides /api/refresh to regenerate the dashboard.

Usage:
    python live_server.py
Then open: http://localhost:7384
"""

import json
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests

PORT      = 7384
DASH_DIR  = os.path.dirname(os.path.abspath(__file__))
DASH_FILE = os.path.join(DASH_DIR, "dashboard.html")

_price_cache = {}
_cache_lock  = threading.Lock()


def fetch_live_prices(symbols: list[str]) -> dict:
    """
    Fetch ultra-accurate real-time prices directly from TradingView Scanner API.
    Accepts symbols with or without .NS suffix.
    Returns {symbol: price, ...}
    """
    result = {}
    if not symbols:
        return result

    # Clean symbols to pure tickers
    clean_syms = [s.replace(".NS", "").replace("NSE:", "").strip() for s in symbols if s.strip()]
    tv_tickers = [f"NSE:{s}" for s in clean_syms]

    url = "https://scanner.tradingview.com/india/scan"
    payload = {
        "symbols": {"tickers": tv_tickers},
        "columns": ["close", "open", "high", "low", "change", "volume", "VWAP"]
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/"
    }

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            for row in data.get("data", []):
                sym = row["s"].replace("NSE:", "")
                vals = row.get("d", [])
                if vals and vals[0] is not None:
                    result[sym] = round(float(vals[0]), 2)
            return result
    except Exception as e:
        print(f"[live] TradingView API error: {e}")

    # Fallback to yfinance if TradingView is unreachable
    try:
        import yfinance as yf
        import pandas as pd
        tickers_str = " ".join([f"{s}.NS" for s in clean_syms])
        df = yf.download(tickers_str, period="1d", interval="1m", progress=False, threads=True)
        if df is not None and not df.empty:
            for s in clean_syms:
                ns_sym = f"{s}.NS"
                if isinstance(df.columns, pd.MultiIndex):
                    if ("Close", ns_sym) in df.columns:
                        ser = df[("Close", ns_sym)].dropna()
                        if not ser.empty:
                            result[s] = round(float(ser.iloc[-1]), 2)
                elif "Close" in df.columns:
                    ser = df["Close"].dropna()
                    if not ser.empty:
                        result[s] = round(float(ser.iloc[-1]), 2)
    except Exception as e:
        print(f"[live] yfinance fallback error: {e}")

    return result


class DashHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        # Suppress verbose default logging; only print key events
        pass

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        query  = urllib.parse.parse_qs(parsed.query)

        # ── Route: /api/live?symbols=SYM1.NS,SYM2.NS ─────────────────────
        if path == "/api/live":
            sym_param = query.get("symbols", [""])[0]
            symbols   = [s.strip() for s in sym_param.split(",") if s.strip()]
            print(f"[live] Fetching prices for: {symbols}")
            prices = fetch_live_prices(symbols)
            print(f"[live] Got: {prices}")
            body = json.dumps(prices).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.send_cors()
            self.end_headers()
            self.wfile.write(body)

        # ── Route: /api/refresh ───────────────────────────────────────────
        elif path == "/api/refresh":
            script = os.path.join(DASH_DIR, "fetch_oi_data.py")
            args   = [sys.executable, script] + sys.argv[1:]
            print(f"[refresh] Regenerating dashboard: {' '.join(args)}")
            try:
                proc = subprocess.run(
                    args, cwd=DASH_DIR, capture_output=True, text=True, timeout=120
                )
                ok   = proc.returncode == 0
                body = json.dumps({
                    "ok":     ok,
                    "stdout": proc.stdout[-2000:],
                    "stderr": proc.stderr[-500:],
                }).encode("utf-8")
                print(f"[refresh] Done (rc={proc.returncode})")
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.send_cors()
            self.end_headers()
            self.wfile.write(body)

        # ── Route: / or /dashboard.html → serve dashboard ─────────────────
        elif path in ("/", "/dashboard.html"):
            if not os.path.exists(DASH_FILE):
                self._404("dashboard.html not found. Run: python fetch_oi_data.py")
                return
            with open(DASH_FILE, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        else:
            self._404(f"Unknown path: {path}")

    def _404(self, msg):
        body = msg.encode("utf-8")
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


def main():
    # Allow passing a date arg to auto-pass to fetch_oi_data.py on /api/refresh
    server = HTTPServer(("localhost", PORT), DashHandler)
    print("=" * 55)
    print(f"  NSE OI Live Dashboard Server")
    print(f"  Open in browser: http://localhost:{PORT}")
    print(f"  Live prices API: http://localhost:{PORT}/api/live?symbols=RELIANCE.NS")
    print(f"  Refresh dashboard: http://localhost:{PORT}/api/refresh")
    print(f"  Press Ctrl+C to stop.")
    print("=" * 55)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[live_server] Stopped.")
        server.server_close()


if __name__ == "__main__":
    main()

