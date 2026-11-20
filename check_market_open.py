"""
check_market_open.py
====================
Checks if today is an active NSE Trading Day (Market Open Day).
Returns exit code 0 if market is open today, or exit code 1 if it is a Weekend
or an official NSE Trading Holiday.

Used by GitHub Actions and automation scripts to prevent running or committing
when markets are closed.
"""

import sys
import datetime
import os

# Official NSE F&O Trading Holidays for 2026
NSE_HOLIDAYS_2026 = {
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr (Ramadan Eid)",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan*",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas"
}

def is_market_open_today(target_date=None, check_hours=False):
    if target_date is None:
        # Determine date in IST
        utc_now = datetime.datetime.now(datetime.timezone.utc)
        ist_now = utc_now + datetime.timedelta(hours=5, minutes=30)
        target_date = ist_now.date()
        target_time = ist_now.time()
    else:
        target_time = datetime.time(12, 0)

    date_str = target_date.strftime("%Y-%m-%d")

    # 1. Weekend check (Saturday = 5, Sunday = 6)
    if target_date.weekday() >= 5:
        day_name = "Saturday" if target_date.weekday() == 5 else "Sunday"
        print(f"[MARKET CLOSED] Today ({date_str}, {day_name}) is a weekend. NSE is closed.")
        return False, f"Weekend ({day_name})"

    # 2. Official NSE Trading Holiday check
    if date_str in NSE_HOLIDAYS_2026:
        reason = NSE_HOLIDAYS_2026[date_str]
        print(f"[MARKET CLOSED] Today ({date_str}) is an official NSE Trading Holiday: {reason}.")
        return False, f"Holiday: {reason}"

    # 3. Optional: Check intraday trading hours (09:15 to 15:35 IST)
    if check_hours:
        market_start = datetime.time(9, 15)
        market_end = datetime.time(15, 35)
        if not (market_start <= target_time <= market_end):
            print(f"[MARKET CLOSED] Current IST time ({target_time.strftime('%H:%M')}) is outside active market hours (09:15 to 15:35).")
            return False, "Outside market hours"

    print(f"[MARKET OPEN] Today ({date_str}) is an active NSE Trading Day.")
    return True, "Market Open"

if __name__ == "__main__":
    check_hours = "--check-hours" in sys.argv
    is_open, reason = is_market_open_today(check_hours=check_hours)
    if not is_open:
        # Exit with status code 1 so callers (like CI) know market is closed
        sys.exit(1)
    sys.exit(0)
