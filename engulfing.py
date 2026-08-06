"""
Engulfing Pattern Watcher (MetaTrader5)
----------------------------------------
- Loads MT5 accounts from accounts.env (kept next to this script, not
  hard-coded in the code) and lets the user pick which account to use.
- Asks the user for a symbol and timeframe.
- Waits until the START of the NEXT candle on the chosen timeframe
  (e.g. if it's 15:45:14 and timeframe = M1, it waits until 15:46:00),
  then waits a tiny bit more so the candle that just closed is fully formed.
- Pulls the last closed OHLC candles and checks the last CLOSED candle pair
  for a Bullish or Bearish Engulfing pattern.
- Prints the result. Loops forever, checking every new candle.

Requirements:
    pip install MetaTrader5

Files expected in the same folder:
    accounts.env   -> your account blocks (see format below)

accounts.env format (blocks separated by a line of "="):
    Name     : Shayan
    Type     : demo.ecn.mt5 (USD)
    Server   : Alpari-MT5-Demo
    Login    : 53070009
    Password : @8ZsLhHb
    Investor : @7UrOhMw
    Typeacc  : Demo Alpari Server

Run:
    python engulfing_watcher.py
"""

import os
import re
import time
from datetime import datetime, timedelta

import MetaTrader5 as mt5

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.env")

TIMEFRAMES = {
    "M1": (mt5.TIMEFRAME_M1, 60),
    "M5": (mt5.TIMEFRAME_M5, 300),
    "M15": (mt5.TIMEFRAME_M15, 900),
    "M30": (mt5.TIMEFRAME_M30, 1800),
    "H1": (mt5.TIMEFRAME_H1, 3600),
    "H4": (mt5.TIMEFRAME_H4, 14400),
    "D1": (mt5.TIMEFRAME_D1, 86400),
}


# ---------------------------------------------------------------------------
# accounts.env loading
# ---------------------------------------------------------------------------

def load_accounts(path=ENV_PATH):
    """
    Parses accounts.env into a list of dicts:
    [{"name": ..., "type": ..., "server": ..., "login": ..., "password": ...,
      "investor": ..., "typeacc": ...}, ...]
    Blocks are separated by a line of '=' characters (any length).
    Each field line looks like "Key : Value".
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"accounts.env not found at {path}. Create it next to this script."
        )

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = re.split(r"^=+\s*$", content, flags=re.MULTILINE)
    accounts = []

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        fields = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key.strip().lower()] = value.strip()

        if "login" in fields and "password" in fields and "server" in fields:
            accounts.append({
                "name": fields.get("name", "Unnamed"),
                "type": fields.get("type", ""),
                "server": fields["server"],
                "login": fields["login"],
                "password": fields["password"],
                "investor": fields.get("investor", ""),
                "typeacc": fields.get("typeacc", ""),
            })

    return accounts


def choose_account(accounts):
    print("=== Available Accounts ===")
    for i, acc in enumerate(accounts, start=1):
        print(f"{i}. {acc['name']} | {acc['typeacc'] or acc['type']} | "
              f"Server: {acc['server']} | Login: {acc['login']}")
    print()

    while True:
        choice = input(f"Select account [1-{len(accounts)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(accounts):
            return accounts[int(choice) - 1]
        print("Invalid choice, try again.")


def get_symbol_and_timeframe():
    symbol = input("Symbol (e.g. EURUSD): ").strip().upper()

    print("Available timeframes:", ", ".join(TIMEFRAMES.keys()))
    tf_input = input("Timeframe [default M1]: ").strip().upper() or "M1"
    if tf_input not in TIMEFRAMES:
        print(f"Unknown timeframe '{tf_input}', defaulting to M1.")
        tf_input = "M1"

    return symbol, tf_input


# ---------------------------------------------------------------------------
# MT5 connection
# ---------------------------------------------------------------------------

def connect(account):
    if not mt5.initialize():
        raise RuntimeError(f"initialize() failed, error code = {mt5.last_error()}")

    authorized = mt5.login(
        int(account["login"]),
        password=account["password"],
        server=account["server"],
    )
    if not authorized:
        mt5.shutdown()
        raise RuntimeError(f"login() failed, error code = {mt5.last_error()}")

    print(f"Connected as {account['name']} ({account['login']}) on {account['server']}.")


# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------

def seconds_to_next_candle_close(period_seconds: int) -> float:
    """
    Returns how many seconds to wait until the NEXT candle boundary
    (the moment the current candle closes / the next one opens),
    plus a small buffer so the broker has time to finalize the bar.
    e.g. now=15:45:14, period=60s (M1) -> waits until 15:46:00 (+buffer).
    """
    now = datetime.now()
    epoch_seconds = now.timestamp()
    remainder = epoch_seconds % period_seconds
    wait = period_seconds - remainder
    buffer_seconds = 1.5
    return wait + buffer_seconds


# ---------------------------------------------------------------------------
# Engulfing detection
# ---------------------------------------------------------------------------

def classify_engulfing(prev_candle, curr_candle):
    prev_open, prev_close = prev_candle["open"], prev_candle["close"]
    curr_open, curr_close = curr_candle["open"], curr_candle["close"]

    prev_bearish = prev_close < prev_open
    prev_bullish = prev_close > prev_open
    curr_bullish = curr_close > curr_open
    curr_bearish = curr_close < curr_open

    prev_body_low, prev_body_high = sorted([prev_open, prev_close])
    curr_body_low, curr_body_high = sorted([curr_open, curr_close])

    engulfs = curr_body_low <= prev_body_low and curr_body_high >= prev_body_high

    if prev_bearish and curr_bullish and engulfs:
        return "bullish"
    if prev_bullish and curr_bearish and engulfs:
        return "bearish"
    return None


def fetch_last_closed_candles(symbol, tf_const, count=2):
    """pos=1 skips the currently-forming candle, so we only look at closed bars."""
    rates = mt5.copy_rates_from_pos(symbol, tf_const, 1, count)
    if rates is None or len(rates) < 2:
        return None
    return rates


def check_pattern(symbol, tf_const):
    rates = fetch_last_closed_candles(symbol, tf_const, count=2)
    if rates is None:
        print("Could not fetch OHLC data.")
        return

    prev = {"open": rates[0]["open"], "close": rates[0]["close"]}
    curr = {"open": rates[1]["open"], "close": rates[1]["close"]}
    curr_time = datetime.fromtimestamp(rates[1]["time"])

    result = classify_engulfing(prev, curr)

    if result == "bullish":
        print(f"[{curr_time}] {symbol}: BULLISH ENGULFING detected.")
    elif result == "bearish":
        print(f"[{curr_time}] {symbol}: BEARISH ENGULFING detected.")
    else:
        print(f"[{curr_time}] {symbol}: no engulfing pattern.")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    accounts = load_accounts()
    if not accounts:
        print("No accounts found in accounts.env.")
        return

    account = choose_account(accounts)
    connect(account)

    symbol, tf_key = get_symbol_and_timeframe()
    tf_const, period_seconds = TIMEFRAMES[tf_key]

    if not mt5.symbol_select(symbol, True):
        print(f"Symbol '{symbol}' not found or could not be selected.")
        mt5.shutdown()
        return

    print(f"Watching {symbol} on {tf_key}. Press Ctrl+C to stop.\n")

    try:
        while True:
            wait_seconds = seconds_to_next_candle_close(period_seconds)
            next_check = datetime.now() + timedelta(seconds=wait_seconds)
            print(f"Now: {datetime.now().strftime('%H:%M:%S')} -> "
                  f"waiting {wait_seconds:.1f}s for next {tf_key} candle "
                  f"(check at ~{next_check.strftime('%H:%M:%S')})...")
            time.sleep(wait_seconds)

            check_pattern(symbol, tf_const)
            print()

    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()