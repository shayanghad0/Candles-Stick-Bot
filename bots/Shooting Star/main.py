#!/usr/bin/env python3
"""
main.py — Shooting Star / Hammer candle-pattern trader for MetaTrader 5.

WHAT THIS DOES
---------------
1. Reads one or more MT5 demo accounts from a local .env file and lets you
   pick which one to log in with (rich table + prompt).
2. Asks for a symbol and a timeframe (M1 / M5 / M15 / M30 / H1).
3. Sleeps precisely until the *next* candle close on that timeframe
   (e.g. if it's 15:45:14 and timeframe = M1, it waits until 15:46:00),
   then pulls the OHLC data.
4. Checks the last CLOSED candle for a shooting star (bearish pin bar,
   uptrend + long upper shadow) or a hammer (bullish pin bar, downtrend +
   long lower shadow).
5. If a signal fires: opens a market order, sets TP/SL a fixed number of
   points away, saves a JSON trade record + a candlestick chart image of
   the last 30 candles, then live-monitors the position in the terminal
   (entry, current price, PnL in points, PnL in %, TP, SL) using `rich`.
6. When the position closes (TP or SL hit), it updates the JSON record
   with the outcome and re-renders the chart, then goes back to watching
   for the next candle.

REQUIREMENTS
------------
    pip install MetaTrader5 python-dotenv pandas mplfinance rich

IMPORTANT: the `MetaTrader5` package talks to a locally running MT5
terminal process — it only works on Windows (or Wine on Linux) with the
terminal installed and logged in to that broker's server list. It cannot
run headless in an arbitrary Linux container.

FILES
-----
    main.py   <- this file
    .env      <- your accounts (ACCOUNT_1_..., ACCOUNT_2_..., etc.)
    trades/   <- created automatically; one .json + one .png per trade

SECURITY NOTE
-------------
.env stores your MT5 passwords in plain text. Keep this folder private,
never commit .env to git, and prefer demo accounts (as you're already
using) when testing automated strategies.
"""

import json
import math
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

try:
    import mplfinance as mpf
except ImportError:
    mpf = None


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
console = Console()

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "trades"
DATA_DIR.mkdir(exist_ok=True)

TP_POINTS = 150     # take profit distance, in points
SL_POINTS = 200     # stop loss distance, in points
LOT_SIZE = 0.02
CANDLES_TO_SAVE = 30
MAGIC_NUMBER = 990011

TIMEFRAME_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
}


def timeframe_const(name):
    """Resolve the MT5 TIMEFRAME_* constant lazily (mt5 must be initialized-safe to import)."""
    return {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
    }[name]


# --------------------------------------------------------------------------
# Account loading from .env
#
# The .env file holds one or more account blocks in this format, separated
# by a line of "=" characters:
#
#   Name     : Shayan Ghadamian
#   Type     : Forex Hedged USD
#   Server   : MetaQuotes-Demo
#   Login    : 5053757050
#   Password : XlU@4nGr
#   Investor : !v2uKkOu
#   Typeacc  : Demo MT5 Server
#   ==============================
#   Name     : ...
#   ...
# --------------------------------------------------------------------------
FIELD_LINE = re.compile(r"^\s*([A-Za-z]+)\s*:\s*(.*?)\s*$")


def load_accounts():
    if not ENV_PATH.exists():
        console.print(f"[red]No .env file found at {ENV_PATH}[/red]")
        sys.exit(1)

    text = ENV_PATH.read_text(encoding="utf-8")
    blocks = re.split(r"^=+\s*$", text, flags=re.MULTILINE)

    accounts = []
    for block in blocks:
        fields = {}
        for line in block.splitlines():
            if not line.strip():
                continue
            m = FIELD_LINE.match(line)
            if not m:
                continue
            key, value = m.group(1).lower(), m.group(2).strip()
            # if a key repeats within one block (e.g. a stray "Name : Name : X"
            # line), keep the LAST value after the last ':' as the real one
            fields[key] = value

        required = {"login", "password", "server"}
        if required.issubset(fields.keys()):
            accounts.append(fields)

    if not accounts:
        console.print(
            "[red]No accounts found in .env — each block needs at least "
            "Name/Type/Server/Login/Password/Investor/Typeacc lines, "
            "separated by a line of '='.[/red]"
        )
        sys.exit(1)
    return accounts


def choose_account(accounts):
    table = Table(title="Available MT5 Accounts")
    table.add_column("#", justify="right")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Server")
    table.add_column("Login")
    table.add_column("Account Type")

    for i, acc in enumerate(accounts, start=1):
        table.add_row(
            str(i),
            acc.get("name", "-"),
            acc.get("type", "-"),
            acc.get("server", "-"),
            acc.get("login", "-"),
            acc.get("typeacc", "-"),
        )
    console.print(table)

    idx = IntPrompt.ask(
        "Select account number",
        choices=[str(i) for i in range(1, len(accounts) + 1)],
    )
    return accounts[idx - 1]


# --------------------------------------------------------------------------
# Timing — wait until the next candle close
# --------------------------------------------------------------------------
def seconds_until_next_close(timeframe_seconds):
    now = datetime.now(timezone.utc).timestamp()
    next_boundary = math.ceil(now / timeframe_seconds) * timeframe_seconds
    if next_boundary - now < 0.5:
        # we're basically already at a boundary, aim for the following one
        next_boundary += timeframe_seconds
    return next_boundary - now


def wait_for_next_candle(timeframe_seconds, publish_buffer=1.5):
    """
    Sleep until the current candle finishes, plus a small buffer so the
    broker has actually published the closed bar (e.g. run at 15:45:14 on
    M1 -> sleeps to 15:46:00, then a bit more).
    """
    wait_s = seconds_until_next_close(timeframe_seconds)
    target = datetime.now(timezone.utc) + timedelta(seconds=wait_s)
    with console.status(
        f"[cyan]Waiting for candle close at {target.strftime('%H:%M:%S')} UTC "
        f"({wait_s:0.1f}s)...[/cyan]"
    ):
        time.sleep(max(wait_s, 0))
    time.sleep(publish_buffer)


# --------------------------------------------------------------------------
# Pattern detection
# --------------------------------------------------------------------------
def detect_pattern(df):
    """
    df: DataFrame of recent CLOSED candles with columns open/high/low/close,
    oldest first, most recent closed candle last (df.iloc[-1]).

    Returns "shooting_star", "hammer", or None.
    """
    if len(df) < 6:
        return None

    last = df.iloc[-1]
    o, h, l, c = last["open"], last["high"], last["low"], last["close"]

    candle_range = h - l
    if candle_range <= 0:
        return None

    body = abs(c - o)
    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - l

    # treat a doji-like zero body as a tiny body so ratios still work
    body_for_ratio = body if body > 0 else candle_range * 0.01

    trend_window = df.iloc[-6:-1]["close"]
    is_uptrend = trend_window.iloc[-1] > trend_window.iloc[0]
    is_downtrend = trend_window.iloc[-1] < trend_window.iloc[0]

    # shooting star: long upper shadow (>= 2x body), small/no lower shadow,
    # appearing after an uptrend
    if (
        upper_shadow >= 2 * body_for_ratio
        and lower_shadow <= body_for_ratio * 0.5
        and is_uptrend
    ):
        return "shooting_star"

    # hammer: mirror image, appearing after a downtrend
    if (
        lower_shadow >= 2 * body_for_ratio
        and upper_shadow <= body_for_ratio * 0.5
        and is_downtrend
    ):
        return "hammer"

    return None


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------
def place_order(symbol, signal):
    info = mt5.symbol_info(symbol)
    if info is None:
        console.print(f"[red]Symbol {symbol} not found on this server[/red]")
        return None
    if not info.visible:
        mt5.symbol_select(symbol, True)

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        console.print(f"[red]Could not get a live tick for {symbol}[/red]")
        return None

    point = info.point

    if signal == "shooting_star":
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
        sl = price + SL_POINTS * point
        tp = price - TP_POINTS * point
    else:  # hammer
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
        sl = price - SL_POINTS * point
        tp = price + TP_POINTS * point

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": LOT_SIZE,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": MAGIC_NUMBER,
        "comment": f"{signal} auto-trade",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    return mt5.order_send(request)


# --------------------------------------------------------------------------
# Chart + JSON persistence
# --------------------------------------------------------------------------
def save_chart(df, path, title):
    if mpf is None:
        console.print("[yellow]mplfinance not installed — skipping chart image[/yellow]")
        return
    plot_df = df.copy()
    plot_df["time"] = pd.to_datetime(plot_df["time"], unit="s")
    plot_df = plot_df.set_index("time")[["open", "high", "low", "close"]]
    mpf.plot(
        plot_df,
        type="candle",
        style="charles",
        title=title,
        savefig=dict(fname=str(path), dpi=150, bbox_inches="tight"),
    )


def save_trade_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def render_ascii_candles(df, symbol, tf_name, height=22, gap=2):
    """
    TradingView-style candlestick chart for the terminal: spaced-out candles,
    faint horizontal gridlines, a dark background panel, price axis on the
    right, and a time axis along the bottom.
    """
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    times = df["time"].values
    n = len(df)

    col_width = 1 + gap                 # 1 char for the candle + `gap` blank cols after it
    total_cols = n * col_width - gap    # no trailing gap

    max_high = highs.max()
    min_low = lows.min()
    price_range = max_high - min_low
    if price_range <= 0:
        price_range = 1e-6

    def to_row(price):
        ratio = (price - min_low) / price_range
        row = int(round((1 - ratio) * (height - 1)))
        return max(0, min(height - 1, row))

    grid = [[" "] * total_cols for _ in range(height)]
    colors = [[""] * total_cols for _ in range(height)]

    for c in range(n):
        col = c * col_width
        o, h, l, cl = opens[c], highs[c], lows[c], closes[c]
        top, bottom = to_row(h), to_row(l)
        body_top, body_bottom = to_row(max(o, cl)), to_row(min(o, cl))
        color = "bright_green" if cl >= o else "bright_red"
        for r in range(top, bottom + 1):
            grid[r][col] = "│"
            colors[r][col] = color
        for r in range(body_top, body_bottom + 1):
            grid[r][col] = "█"
            colors[r][col] = color

    label_rows = {round(i * (height - 1) / 4) for i in range(5)}

    text = Text(no_wrap=True, overflow="crop")
    for r in range(height):
        for c in range(total_cols):
            ch = grid[r][c]
            if ch == " " and r in label_rows:
                text.append("─", style="grey27")
            else:
                text.append(ch, style=colors[r][c] or "white")
        if r in label_rows:
            price_at_row = max_high - (r / (height - 1)) * price_range
            text.append(f"  {price_at_row:>10.5f}", style="dim")
        text.append("\n")

    step = max(1, n // 5)
    axis_chars = [" "] * total_cols
    for c in range(0, n, step):
        col = c * col_width
        ts = datetime.fromtimestamp(int(times[c]), tz=timezone.utc).strftime("%H:%M")
        for i, ch in enumerate(ts):
            if col + i < total_cols:
                axis_chars[col + i] = ch
    text.append("".join(axis_chars), style="dim")

    return Panel(
        text,
        title=f"[bold]{symbol}[/bold]  ·  {tf_name}  ·  last {n} candles",
        border_style="grey50",
        style="on grey11",
        padding=(1, 2),
        expand=False,
    )


# --------------------------------------------------------------------------
# Position monitoring
# --------------------------------------------------------------------------
def monitor_position(ticket, symbol, entry_price, tp, sl, order_type, json_path, trade_record):
    info = mt5.symbol_info(symbol)
    point = info.point if info else 0.0001

    console.print(f"[green]Monitoring position #{ticket} on {symbol}...[/green]")

    with Live(refresh_per_second=1, console=console) as live:
        while True:
            positions = mt5.positions_get(ticket=ticket)

            if not positions:
                # position is gone -> it closed (TP, SL, or manual close)
                deals = mt5.history_deals_get(position=ticket)
                close_price = None
                profit = 0.0
                if deals:
                    for d in deals:
                        if d.entry == 1:  # DEAL_ENTRY_OUT
                            close_price = d.price
                            profit += d.profit

                reason = "UNKNOWN"
                if close_price is not None and tp is not None and sl is not None:
                    reason = "TP" if abs(close_price - tp) < abs(close_price - sl) else "SL"

                trade_record.update(
                    status="closed",
                    close_price=close_price,
                    closed_reason=reason,
                    profit=profit,
                    closed_time=datetime.now(timezone.utc).isoformat(),
                )
                save_trade_json(json_path, trade_record)
                console.print(
                    f"[bold magenta]Position #{ticket} closed — hit {reason} | profit: {profit:.2f}[/bold magenta]"
                )
                return trade_record

            pos = positions[0]
            pnl_points = (pos.price_current - entry_price) / point
            if order_type == mt5.ORDER_TYPE_SELL:
                pnl_points = -pnl_points

            account_info = mt5.account_info()
            equity = account_info.equity if account_info else None
            pnl_pct = (pos.profit / equity * 100) if equity else 0.0

            table = Table(title=f"Position #{ticket} — {symbol}")
            table.add_column("Entry")
            table.add_column("Current")
            table.add_column("PnL (Point)")
            table.add_column("PnL (%)")
            table.add_column("TP")
            table.add_column("SL")
            table.add_row(
                f"{entry_price:.5f}",
                f"{pos.price_current:.5f}",
                f"{pnl_points:.1f}",
                f"{pnl_pct:.2f}%",
                f"{tp:.5f}" if tp else "-",
                f"{sl:.5f}" if sl else "-",
            )
            live.update(table)
            time.sleep(2)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def main():
    if mt5 is None:
        console.print("[red]The MetaTrader5 package is not installed. Run: pip install MetaTrader5[/red]")
        sys.exit(1)

    accounts = load_accounts()
    account = choose_account(accounts)

    if not mt5.initialize():
        console.print(f"[red]MT5 initialize() failed: {mt5.last_error()}[/red]")
        sys.exit(1)

    authorized = mt5.login(
        int(account["login"]),
        password=account["password"],
        server=account["server"],
    )
    if not authorized:
        console.print(f"[red]Login failed: {mt5.last_error()}[/red]")
        mt5.shutdown()
        sys.exit(1)

    console.print(
        Panel(
            f"Logged in as [bold]{account.get('name')}[/bold] on "
            f"[bold]{account['server']}[/bold] (login {account['login']})"
        )
    )

    symbol = Prompt.ask("Symbol", default="XAUUSD")
    tf_name = Prompt.ask("Timeframe", choices=list(TIMEFRAME_SECONDS.keys()), default="M1")
    tf_seconds = TIMEFRAME_SECONDS[tf_name]
    tf_const = timeframe_const(tf_name)

    console.print(f"[cyan]Watching {symbol} on {tf_name}. Press Ctrl+C to stop.[/cyan]")

    try:
        while True:
            wait_for_next_candle(tf_seconds)

            rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, CANDLES_TO_SAVE + 1)
            if rates is None or len(rates) < CANDLES_TO_SAVE + 1:
                console.print("[yellow]Not enough candle data yet, retrying next candle...[/yellow]")
                continue

            df = pd.DataFrame(rates)
            # drop the still-forming candle (index -1 from copy_rates_from_pos is the
            # currently open one only if we're right at the boundary — copy_rates_from_pos(0)
            # returns the most recent CLOSED candles once we're past the buffer, so we
            # simply take the last CANDLES_TO_SAVE rows as "closed" candles)
            closed_candles = df.tail(CANDLES_TO_SAVE).reset_index(drop=True)

            console.print(render_ascii_candles(closed_candles, symbol, tf_name))

            signal = detect_pattern(closed_candles)
            now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")

            if signal is None:
                console.print(f"[dim]{now_str} UTC — no pattern on {symbol} {tf_name}[/dim]")
                continue

            console.print(
                f"[bold yellow]{now_str} UTC — {signal.replace('_', ' ').title()} "
                f"detected on {symbol} {tf_name}![/bold yellow]"
            )

            result = place_order(symbol, signal)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                console.print(f"[red]Order failed: {result}[/red]")
                continue

            ticket = result.order
            entry_price = result.price
            tp = getattr(result.request, "tp", None) if hasattr(result, "request") else None
            sl = getattr(result.request, "sl", None) if hasattr(result, "request") else None

            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            json_path = DATA_DIR / f"{symbol}_{stamp}.json"
            chart_path = DATA_DIR / f"{symbol}_{stamp}.png"

            order_type = mt5.ORDER_TYPE_SELL if signal == "shooting_star" else mt5.ORDER_TYPE_BUY

            trade_record = {
                "ticket": ticket,
                "symbol": symbol,
                "timeframe": tf_name,
                "signal": signal,
                "order_type": "SELL" if order_type == mt5.ORDER_TYPE_SELL else "BUY",
                "entry_price": entry_price,
                "tp": tp,
                "sl": sl,
                "tp_points": TP_POINTS,
                "sl_points": SL_POINTS,
                "volume": LOT_SIZE,
                "open_time": datetime.now(timezone.utc).isoformat(),
                "status": "open",
                "candles": closed_candles.to_dict(orient="records"),
            }
            save_trade_json(json_path, trade_record)
            save_chart(closed_candles, chart_path, f"{symbol} {tf_name} - {signal}")

            trade_record = monitor_position(
                ticket, symbol, entry_price, tp, sl, order_type, json_path, trade_record
            )

            # re-render the chart with fresh candles now that the trade is closed
            rates2 = mt5.copy_rates_from_pos(symbol, tf_const, 0, CANDLES_TO_SAVE)
            if rates2 is not None:
                df2 = pd.DataFrame(rates2)
                save_chart(
                    df2,
                    chart_path,
                    f"{symbol} {tf_name} - {signal} - closed ({trade_record.get('closed_reason')})",
                )

    except KeyboardInterrupt:
        console.print("[cyan]Stopped by user.[/cyan]")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()