# NSE F&O OI Dashboard & Intraday ORB Performance Tracker

An automated dashboard for analyzing **NSE Open Interest (OI) Quadrants** (Fresh Longs, Fresh Shorts, Short Covering, Long Unwinding) paired with a **TradingView-powered Intraday ORB (Opening Range Breakout) Execution Engine** and a **5X Margin Capital P&L Tracker**.

---

## ✨ Features

- **Tab 1: Daily Dashboard & Picks**:
  - Top 10 stocks classified into 4 quadrants based on official NSE Bhavcopy data.
  - Delivery % accumulation analysis with institutional threshold filters.
  - Automated Top 5 BUY and Top 5 SELL ranked candidates.

- **Tab 2: Capital & Day's P&L Tracker**:
  - ₹10,000 capital per stock with **5X Intraday MIS Margin** (₹50,000 buying power).
  - Authentic **TradingView WebSocket feed** for 1st 5-minute candle ORB entry & Day's High/Low Stop Loss checks.
  - Opening Gap alignment rules + 2.0% erratic opening candle filter.
  - Live dynamic auto-refresh with real-time TradingView tick updates.

- **Tab 3: 1-Week Historic Trend & Multi-Day Accumulation**:
  - 5-day rolling cumulative OI buildup & delivery absorption.

---

## 🚀 Quick Start

### 1. Requirements
```bash
pip install yfinance requests pandas websocket-client
```

### 2. Generate EOD Dashboard
```bash
python fetch_oi_data.py
```
*To inspect a past date:*
```bash
python fetch_oi_data.py 2026-11-05
```

### 3. Run Live Interactive Server
```bash
python live_server.py
```
Then open `http://localhost:7384` in your browser.
