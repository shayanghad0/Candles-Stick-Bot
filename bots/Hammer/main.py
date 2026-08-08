#!/usr/bin/env python3
"""
Hammer (Pin Bar) Signal Bot for MetaTrader 5
=============================================

Flow:
  1. Load trading accounts from accounts.env (custom block format) and
     bot settings from .env (TP/SL points, etc).
  2. Ask the user to pick an account (rich table), then a symbol and
     a timeframe.
  3. Connect to MT5 with the chosen account.
  4. Wait until the *current* candle closes (e.g. running at 15:45:14
     on M1 waits until 15:46:00 + a small buffer), then pull the last
     30 closed OHLC candles.
  5. Check the last closed candle for a Hammer / Pin Bar pattern.
  6. If found: build a signal (Entry / SL / TP using SL_POINTS and
     TP_POINTS from settings), save a 30-candle chart PNG, save the
     order to orders.json, then monitor live ticks until TP or SL is
     hit -> update the JSON (status, PNL %, PNL points) and redraw
     the chart with the outcome marked.
  7. If no signal: report "no signal" and wait for the next candle.

Requirements: MetaTrader5, python-dotenv, rich, mplfinance, matplotlib
Run with:     python main.py

IMPORTANT SAFETY NOTE
----------------------
This script does NOT place live/real orders on your MT5 account by
default -- it only *simulates* the trade (paper-trades it) using live
tick prices to decide when TP/SL would be hit. This is intentional:
an unattended bot that fires real market orders is a good way to lose
real money to a bug. If you want it to place real orders, read the
`LIVE_TRADING` setting in .env and the `place_real_order()` function
below -- flip it on only once you've watched it paper-trade correctly
and you understand the risk. Demo accounts are safe to flip it on for.
"""

import os
import re
import sys
import json
import time
import math
from pathlib import Path
from datetime import datetime, timedelta

from dotenv import dotenv_values
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, IntPrompt
from rich.live import Live
from rich.text import Text

console = Console()

BASE_DIR = Path(__file__).resolve().parent
ACCOUNTS_FILE = BASE_DIR / ".env"
SETTINGS_FILE = BASE_DIR / ".env"
CHARTS_DIR = BASE_DIR / "charts"
ORDERS_JSON = BASE_DIR / "orders.json"

CHARTS_DIR.mkdir(exist_ok=True)

TIMEFRAME_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400,
}


# --------------------------------------------------------------------------
# MetaTrader5 import guard -- the MetaTrader5 pip package only installs and
# works on Windows, alongside a running MT5 terminal. We import it lazily
# so the rest of the file (parsing, hammer logic, chart code) can still be
# read / unit-tested on any OS.
# --------------------------------------------------------------------------
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

try:
    import mplfinance as mpf
    import pandas as pd
    CHARTS_AVAILABLE = True
except ImportError:
    CHARTS_AVAILABLE = False


MT5_TIMEFRAME_MAP = {}
if MT5_AVAILABLE:
    MT5_TIMEFRAME_MAP = {
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }


# ==========================================================================
# 1. Account loading (accounts.env custom block format)
# ==========================================================================
def load_accounts(path: Path):
    """
    Parses a file made of blocks like:

        Name     : Shayan Ghadamian
        Type     : Forex Hedged USD
        Server   : MetaQuotes-Demo
        Login    : 5053757050
        Password : ********
        Investor : ********
        Typeacc  : Demo MT5 Server
        ==============================
        ... next block ...

    Returns a list of dicts.
    """
    if not path.exists():
        console.print(f"[red]Accounts file not found:[/red] {path}\n"
                       f"Copy accounts.env.example to accounts.env and fill in your accounts.")
        sys.exit(1)

    raw = path.read_text(encoding="utf-8")
    blocks = re.split(r"^=+\s*$", raw, flags=re.MULTILINE)

    accounts = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        entry = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            entry[key.strip().lower()] = value.strip()
        if "login" in entry and "server" in entry:
            accounts.append(entry)

    if not accounts:
        console.print("[red]No valid accounts found in accounts.env[/red]")
        sys.exit(1)
    return accounts


def choose_account(accounts):
    table = Table(title="MT5 Accounts", show_lines=True)
    table.add_column("#", justify="right")
    table.add_column("Name")
    table.add_column("Login")
    table.add_column("Server")
    table.add_column("Type")
    for i, acc in enumerate(accounts, start=1):
        table.add_row(
            str(i),
            acc.get("name", "-"),
            acc.get("login", "-"),
            acc.get("server", "-"),
            acc.get("typeacc", acc.get("type", "-")),
        )
    console.print(table)

    choice = IntPrompt.ask(
        "Select account #",
        choices=[str(i) for i in range(1, len(accounts) + 1)],
    )
    return accounts[choice - 1]


# ==========================================================================
# 2. Settings (.env) — bot behaviour, not account credentials
# ==========================================================================
def load_settings():
    values = dotenv_values(SETTINGS_FILE) if SETTINGS_FILE.exists() else {}
    return {
        "TP_POINTS": float(values.get("TP_POINTS", 150)),
        "SL_POINTS": float(values.get("SL_POINTS", 200)),
        "CANDLES_TO_KEEP": int(values.get("CANDLES_TO_KEEP", 30)),
        "LIVE_TRADING": values.get("LIVE_TRADING", "false").lower() == "true",
        "MAGIC": int(values.get("MAGIC", 990001)),
    }


# ==========================================================================
# 3. MT5 connection
# ==========================================================================
def connect(account: dict):
    if not MT5_AVAILABLE:
        console.print(
            "[red]MetaTrader5 package is not available.[/red] "
            "It only installs on Windows alongside a running MT5 terminal. "
            "Run this script on the Windows machine where your MT5 terminal is installed."
        )
        sys.exit(1)

    if not mt5.initialize():
        console.print(f"[red]MT5 initialize() failed:[/red] {mt5.last_error()}")
        sys.exit(1)

    authorized = mt5.login(
        login=int(account["login"]),
        password=account["password"],
        server=account["server"],
    )
    if not authorized:
        console.print(f"[red]Login failed:[/red] {mt5.last_error()}")
        mt5.shutdown()
        sys.exit(1)

    info = mt5.account_info()
    console.print(Panel.fit(
        f"Connected as [bold]{info.name}[/bold]\n"
        f"Server: {info.server}   Balance: {info.balance} {info.currency}",
        title="MT5 Connected", border_style="green"
    ))


def choose_symbol_and_timeframe():
    symbol = Prompt.ask("Symbol", default="EURUSD").upper()
    if MT5_AVAILABLE and not mt5.symbol_select(symbol, True):
        console.print(f"[red]Could not select symbol {symbol}[/red]")
        sys.exit(1)
    timeframe = Prompt.ask(
        "Timeframe", choices=list(TIMEFRAME_SECONDS.keys()), default="M1"
    )
    return symbol, timeframe


# ==========================================================================
# 4. Wait for candle close
# ==========================================================================
def wait_for_next_candle_close(timeframe: str, buffer_seconds: float = 2.0):
    """
    Sleeps until the next timeframe boundary + a small buffer, so the
    candle that just closed is guaranteed available from the broker.
    e.g. now = 15:45:14, timeframe M1 (60s) -> waits until 15:46:02.
    """
    tf_seconds = TIMEFRAME_SECONDS[timeframe]
    now = datetime.now()
    epoch = now.timestamp()
    next_boundary = math.ceil(epoch / tf_seconds) * tf_seconds
    target = datetime.fromtimestamp(next_boundary) + timedelta(seconds=buffer_seconds)

    with Live(console=console, refresh_per_second=4) as live:
        while True:
            remaining = (target - datetime.now()).total_seconds()
            if remaining <= 0:
                break
            live.update(Text(
                f"Waiting for {timeframe} candle to close... "
                f"target {target.strftime('%H:%M:%S')} "
                f"({remaining:0.1f}s left)",
                style="cyan"
            ))
            time.sleep(min(0.25, remaining))
    console.print(f"[green]Candle closed at {target.strftime('%H:%M:%S')} — fetching OHLC...[/green]")


# ==========================================================================
# 5. Candle fetching
# ==========================================================================
def fetch_candles(symbol: str, timeframe: str, count: int):
    """
    Returns a list of dicts: time, open, high, low, close.
    Uses copy_rates_from_pos(..., 1, count) so index 0 in the result
    is the OLDEST of the `count` most recently *closed* candles, and
    the LAST element is the most recently closed candle (skips the
    still-forming one at position 0).
    """
    if not MT5_AVAILABLE:
        raise RuntimeError("MetaTrader5 not available")

    tf = MT5_TIMEFRAME_MAP[timeframe]
    rates = mt5.copy_rates_from_pos(symbol, tf, 1, count)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"copy_rates_from_pos returned no data: {mt5.last_error()}")

    candles = []
    for r in rates:
        candles.append({
            "time": datetime.fromtimestamp(int(r["time"])),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        })
    return candles


# ==========================================================================
# 6. Hammer / Pin Bar detection
# ==========================================================================
def is_hammer(candle: dict, prior_candles=None,
              min_lower_shadow_ratio=2.0, max_upper_shadow_ratio=0.15):
    """
    Hammer (pin bar):
      - small real body near the TOP of the range
      - long lower shadow (>= min_lower_shadow_ratio * body)
      - little to no upper shadow (<= max_upper_shadow_ratio * total range)

    prior_candles (optional list, oldest->newest, not including `candle`)
    is used to require the pattern occurs after a downtrend, which is
    what makes it a bullish reversal signal rather than noise.
    """
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    total_range = h - l
    if total_range <= 0:
        return False

    body = abs(c - o)
    lower_shadow = min(o, c) - l
    upper_shadow = h - max(o, c)

    if body == 0:
        body = total_range * 0.01  # treat as a tiny body, avoid div-by-zero

    has_long_lower_shadow = lower_shadow >= min_lower_shadow_ratio * body
    has_small_upper_shadow = upper_shadow <= max_upper_shadow_ratio * total_range
    body_in_upper_half = min(o, c) >= l + total_range * 0.5

    pattern_ok = has_long_lower_shadow and has_small_upper_shadow and body_in_upper_half
    if not pattern_ok:
        return False

    if prior_candles:
        closes = [x["close"] for x in prior_candles[-5:]]
        if len(closes) >= 2 and not (closes[0] > closes[-1]):
            return False  # not preceded by a downtrend

    return True


# ==========================================================================
# 7. Order construction / PNL
# ==========================================================================
def build_signal(symbol: str, candle: dict, settings: dict):
    if MT5_AVAILABLE:
        point = mt5.symbol_info(symbol).point
    else:
        point = 0.00001  # fallback for offline testing

    entry = candle["close"]
    sl = entry - settings["SL_POINTS"] * point
    tp = entry + settings["TP_POINTS"] * point

    return {
        "symbol": symbol,
        "direction": "BUY",
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "point": point,
        "sl_points": settings["SL_POINTS"],
        "tp_points": settings["TP_POINTS"],
        "open_time": candle["time"].isoformat(),
        "candle": {k: (v.isoformat() if isinstance(v, datetime) else v)
                   for k, v in candle.items()},
        "status": "OPEN",
        "close_time": None,
        "close_price": None,
        "pnl_points": None,
        "pnl_percent": None,
    }


def compute_pnl(order: dict, current_price: float):
    diff = current_price - order["entry"]
    pnl_points = diff / order["point"]
    pnl_percent = (diff / order["entry"]) * 100
    return pnl_points, pnl_percent


def place_real_order(symbol: str, order: dict, settings: dict):
    """
    Only called if LIVE_TRADING=true in .env. Sends a real market BUY
    order with the computed SL/TP. Not called by default.
    """
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": 0.01,
        "type": mt5.ORDER_TYPE_BUY,
        "price": mt5.symbol_info_tick(symbol).ask,
        "sl": order["sl"],
        "tp": order["tp"],
        "magic": settings["MAGIC"],
        "comment": "hammer-bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    return result


# ==========================================================================
# 8. Persistence: orders.json
# ==========================================================================
def load_orders():
    if ORDERS_JSON.exists():
        return json.loads(ORDERS_JSON.read_text(encoding="utf-8"))
    return []


def save_orders(orders):
    ORDERS_JSON.write_text(json.dumps(orders, indent=2, default=str), encoding="utf-8")


# ==========================================================================
# 9. Chart snapshot (30 last candles)
# ==========================================================================
def save_chart(symbol: str, candles: list, order: dict, filename: Path):
    if not CHARTS_AVAILABLE:
        console.print("[yellow]mplfinance not installed — skipping chart save.[/yellow]")
        return

    df = pd.DataFrame(candles)
    df.set_index("time", inplace=True)
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
    df["Volume"] = 0

    hlines = dict(
        hlines=[order["entry"], order["sl"], order["tp"]],
        colors=["blue", "red", "green"],
        linestyle="--",
    )
    title = f"{symbol} — Hammer signal ({order['status']})"

    mpf.plot(
        df, type="candle", style="charles", title=title,
        hlines=hlines, savefig=dict(fname=str(filename), dpi=150),
    )


# ==========================================================================
# 10. Order monitoring loop (paper trade unless LIVE_TRADING=true)
# ==========================================================================
def monitor_order(symbol: str, order: dict, settings: dict, orders: list, chart_path: Path, candles: list):
    console.print(f"[bold cyan]Monitoring order — TP {order['tp']:.5f} / SL {order['sl']:.5f}[/bold cyan]")

    while True:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            time.sleep(1)
            continue

        price = tick.bid  # closing a BUY uses bid
        hit_tp = price >= order["tp"]
        hit_sl = price <= order["sl"]

        if hit_tp or hit_sl:
            order["status"] = "TP_HIT" if hit_tp else "SL_HIT"
            order["close_time"] = datetime.now().isoformat()
            order["close_price"] = price
            pnl_points, pnl_percent = compute_pnl(order, price)
            order["pnl_points"] = round(pnl_points, 1)
            order["pnl_percent"] = round(pnl_percent, 3)

            save_orders(orders)
            save_chart(symbol, candles, order, chart_path)

            style = "green" if hit_tp else "red"
            table = Table(title="Order Closed")
            table.add_column("Field")
            table.add_column("Value")
            for field in ("symbol", "direction", "entry", "sl", "tp",
                          "status", "close_price", "pnl_points", "pnl_percent"):
                table.add_row(field, str(order[field]))
            console.print(table, style=style)
            return order

        time.sleep(1)


# ==========================================================================
# Main
# ==========================================================================
def main():
    console.rule("[bold]Hammer (Pin Bar) Signal Bot")

    accounts = load_accounts(ACCOUNTS_FILE)
    account = choose_account(accounts)
    settings = load_settings()

    connect(account)
    symbol, timeframe = choose_symbol_and_timeframe()

    if settings["LIVE_TRADING"]:
        console.print("[bold red]LIVE_TRADING is ON — real orders will be sent![/bold red]")
    else:
        console.print("[yellow]LIVE_TRADING is off — signals are paper-traded only.[/yellow]")

    orders = load_orders()

    try:
        while True:
            wait_for_next_candle_close(timeframe)
            candles = fetch_candles(symbol, timeframe, settings["CANDLES_TO_KEEP"])
            last_candle = candles[-1]
            prior = candles[:-1]

            if is_hammer(last_candle, prior_candles=prior):
                console.print(Panel.fit(
                    f"[bold green]HAMMER SIGNAL[/bold green] on {symbol} {timeframe} "
                    f"at {last_candle['time'].strftime('%H:%M:%S')}\n"
                    f"O:{last_candle['open']:.5f} H:{last_candle['high']:.5f} "
                    f"L:{last_candle['low']:.5f} C:{last_candle['close']:.5f}",
                    border_style="green"
                ))

                order = build_signal(symbol, last_candle, settings)

                if settings["LIVE_TRADING"] and MT5_AVAILABLE:
                    result = place_real_order(symbol, order, settings)
                    order["mt5_result"] = str(result)

                orders.append(order)
                save_orders(orders)

                chart_path = CHARTS_DIR / f"{symbol}_{timeframe}_{last_candle['time'].strftime('%Y%m%d_%H%M%S')}.png"
                save_chart(symbol, candles, order, chart_path)

                monitor_order(symbol, order, settings, orders, chart_path, candles)
            else:
                console.print(f"[dim]{datetime.now().strftime('%H:%M:%S')} — no signal on last {timeframe} candle.[/dim]")

    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped by user.[/yellow]")
    finally:
        if MT5_AVAILABLE:
            mt5.shutdown()


if __name__ == "__main__":
    main()