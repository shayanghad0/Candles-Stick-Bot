"""
MT5 Doji Pattern Signal Bot
============================
Connects to a MetaTrader 5 account (selected from .env), waits for the close
of each new candle on a chosen timeframe, pulls the last 30 OHLC candles,
detects Doji / Dragonfly Doji / Gravestone Doji patterns on the candle that
just closed, opens a virtual (paper) signal with TP/SL expressed in points,
tracks it tick by tick until TP or SL is hit, and persists everything to
trades_history.json plus a chart image (last 30 candles) per signal.

Requirements:
    pip install MetaTrader5 rich pandas mplfinance

Notes:
    - The MetaTrader5 python package only works on Windows (or Wine on
      Linux) because it talks to a locally running MT5 terminal.
    - This bot does NOT place real orders. It tracks "virtual" signals
      (paper trades) using live tick prices. Wire in mt5.order_send()
      yourself in open_trade()/close_trade() if you want real execution.
    - The .env file is NOT a standard KEY=VALUE dotenv file. It uses
      labeled blocks (Name / Type / Server / Login / Password / Investor /
      Typeacc) separated by lines of "=". See .env.example for the exact
      shape. Copy .env.example to .env and fill in your real accounts.
      Never commit your real .env anywhere public.
"""

import os
import re
import sys
import json
import time
import math
from datetime import datetime, timedelta
from pathlib import Path

try:
    import MetaTrader5 as mt5
except ImportError:
    print("MetaTrader5 package not installed or not supported on this OS.")
    print("Install with: pip install MetaTrader5  (Windows only)")
    sys.exit(1)

import pandas as pd

try:
    import mplfinance as mpf
except ImportError:
    mpf = None

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, IntPrompt, FloatPrompt
from rich.live import Live
from rich.text import Text
from rich.console import Group
from rich import box

# --------------------------------------------------------------------------
# Config / constants
# --------------------------------------------------------------------------

console = Console()

BASE_DIR = Path(__file__).resolve().parent
CHARTS_DIR = BASE_DIR / "charts"
DATA_FILE = BASE_DIR / "trades_history.json"

CHARTS_DIR.mkdir(exist_ok=True)

TP_POINTS = 150
SL_POINTS = 200
CANDLE_HISTORY = 30
CHART_DISPLAY_CANDLES = 60   # candles shown in the live terminal chart
CHART_ROWS = 20              # vertical resolution of the ASCII chart
LOG_LINES = 8                # rolling event log length in the dashboard
TICK_SECONDS = 1             # live refresh interval

TIMEFRAMES = {
    "M1": (mt5.TIMEFRAME_M1, 1),
    "M5": (mt5.TIMEFRAME_M5, 5),
    "M15": (mt5.TIMEFRAME_M15, 15),
    "M30": (mt5.TIMEFRAME_M30, 30),
    "H1": (mt5.TIMEFRAME_H1, 60),
    "H4": (mt5.TIMEFRAME_H4, 240),
    "D1": (mt5.TIMEFRAME_D1, 1440),
}

DOJI_BODY_RATIO = 0.10      # body must be <= 10% of full range to count as a doji
LONG_WICK_RATIO = 0.60      # the "long" wick must be >= 60% of full range
SHORT_WICK_RATIO = 0.15     # the opposite wick must be <= 15% of full range


# --------------------------------------------------------------------------
# .env account loading
# --------------------------------------------------------------------------

def load_accounts():
    """Parses the labeled-block .env format:

        Name     : Shayan Ghadamian
        Type     : Forex Hedged USD
        Server   : MetaQuotes-Demo
        Login    : 5053757050
        Password : XlU@4nGr
        Investor : !v2uKkOu
        Typeacc  : Demo MT5 Server

        ==============================

        Name     : ...
        ...

    Blocks are separated by a line made only of "=" characters. Within a
    block, each line is "Label : value" (colon-separated, label
    case-insensitive). Only the part after the FIRST colon is used as the
    value, so values containing ":" are preserved correctly.
    """
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return []

    text = env_path.read_text(encoding="utf-8")
    blocks = re.split(r"^\s*=+\s*$", text, flags=re.MULTILINE)

    accounts = []
    for block in blocks:
        fields = {}
        for line in block.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key and value:
                fields[key] = value

        if "login" in fields and "password" in fields:
            accounts.append({
                "name": fields.get("name", f"Account {len(accounts) + 1}"),
                "type": fields.get("type", ""),
                "server": fields.get("server", ""),
                "login": fields.get("login", ""),
                "password": fields.get("password", ""),
                "investor": fields.get("investor", ""),
                "typeacc": fields.get("typeacc", fields.get("type", "")),
            })
    return accounts


def pick_account(accounts):
    table = Table(title="Available MT5 Accounts", box=box.ROUNDED)
    table.add_column("#", justify="right")
    table.add_column("Name")
    table.add_column("Server")
    table.add_column("Login")
    table.add_column("Type")
    for idx, acc in enumerate(accounts, start=1):
        table.add_row(str(idx), acc["name"], acc["server"], acc["login"], acc["typeacc"])
    console.print(table)

    choice = IntPrompt.ask(
        "Select an account",
        choices=[str(i) for i in range(1, len(accounts) + 1)],
    )
    return accounts[choice - 1]


def connect(account):
    ok = mt5.initialize(
        login=int(account["login"]),
        password=account["password"],
        server=account["server"],
    )
    if not ok:
        console.print(f"[red]mt5.initialize() failed: {mt5.last_error()}[/red]")
        sys.exit(1)
    info = mt5.account_info()
    console.print(Panel.fit(
        f"Connected as [bold]{account['name']}[/bold] "
        f"(login {account['login']} @ {account['server']})\n"
        f"Balance: {info.balance} {info.currency}" if info else "Connected.",
        title="MT5 Connection",
        border_style="green",
    ))


# --------------------------------------------------------------------------
# Candle helpers
# --------------------------------------------------------------------------

def fetch_candles(symbol, timeframe, count=CANDLE_HISTORY):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df


def next_close_boundary(timeframe_minutes, buffer_seconds=2, after=None):
    """Returns the datetime `buffer_seconds` after the next candle boundary
    close, relative to `after` (defaults to now). Does NOT sleep - the main
    loop polls against this so the live chart keeps refreshing meanwhile."""
    now = after or datetime.now()
    epoch_minutes = now.hour * 60 + now.minute
    next_boundary_minutes = (epoch_minutes // timeframe_minutes + 1) * timeframe_minutes
    next_close = now.replace(second=0, microsecond=0) + timedelta(
        minutes=next_boundary_minutes - epoch_minutes
    )
    return next_close + timedelta(seconds=buffer_seconds)


# --------------------------------------------------------------------------
# Pattern detection
# --------------------------------------------------------------------------

def classify_candle(o, h, l, c):
    """Returns one of: 'dragonfly_doji', 'gravestone_doji', 'doji', None."""
    rng = h - l
    if rng <= 0:
        return None

    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    body_ratio = body / rng
    upper_ratio = upper_wick / rng
    lower_ratio = lower_wick / rng

    if body_ratio > DOJI_BODY_RATIO:
        return None  # body too big, not a doji family candle

    if lower_ratio >= LONG_WICK_RATIO and upper_ratio <= SHORT_WICK_RATIO:
        return "dragonfly_doji"
    if upper_ratio >= LONG_WICK_RATIO and lower_ratio <= SHORT_WICK_RATIO:
        return "gravestone_doji"
    return "doji"


PATTERN_DIRECTION = {
    "dragonfly_doji": "BUY",   # bullish reversal signal
    "gravestone_doji": "SELL",  # bearish reversal signal
    "doji": None,               # pure indecision, no directional trade
}


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def load_history():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except json.JSONDecodeError:
            return []
    return []


def save_history(trades):
    DATA_FILE.write_text(json.dumps(trades, indent=2, default=str))


# --------------------------------------------------------------------------
# Charting
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Live terminal candlestick chart (pure rich, no external chart lib)
# --------------------------------------------------------------------------

def _price_decimals(top, bottom):
    """Pick a sensible number of decimal places for axis labels based on
    the instrument's price magnitude (XAUUSD ~ 3dp, EURUSD ~ 5dp, etc.)."""
    magnitude = max(abs(top), abs(bottom), 1e-9)
    if magnitude >= 1000:
        return 2
    if magnitude >= 100:
        return 3
    if magnitude >= 10:
        return 4
    return 5


def build_ascii_candles(df, rows=CHART_ROWS, candle_gap=1):
    """Renders the given OHLC dataframe as a compact, TradingView-style
    ASCII candlestick chart and returns a rich Text object. Uses half-block
    characters (▀ ▄ █) so candle bodies land on fractional row boundaries
    instead of always snapping to a full cell, and adds a 1-column gap
    between candles so they read as distinct bars instead of a solid wall."""
    view = df.reset_index(drop=True)
    n = len(view)
    if n == 0:
        return Text("no data yet", style="dim")

    highs = view["high"].tolist()
    lows = view["low"].tolist()
    top = max(highs)
    bottom = min(lows)
    price_range = (top - bottom) or (top * 0.0001 or 1.0)

    sub_rows = rows * 2  # half-block vertical resolution

    def subrow_for_price(p):
        frac = (top - p) / price_range
        sr = frac * (sub_rows - 1)
        return min(sub_rows - 1, max(0, int(round(sr))))

    col_width = 1 + candle_gap
    total_cols = n * col_width - candle_gap  # no trailing gap
    grid = [[" "] * total_cols for _ in range(rows)]
    styles = [[""] * total_cols for _ in range(rows)]

    for i in range(n):
        o = float(view.loc[i, "open"])
        h = float(view.loc[i, "high"])
        l = float(view.loc[i, "low"])
        c = float(view.loc[i, "close"])
        bullish = c >= o
        color = "bright_green" if bullish else "bright_red"
        col = i * col_width

        wick_top_sr = subrow_for_price(h)
        wick_bot_sr = subrow_for_price(l)
        body_top_sr = subrow_for_price(max(o, c))
        body_bot_sr = subrow_for_price(min(o, c))
        if body_top_sr == body_bot_sr:
            # doji-like: guarantee at least one visible half-row for the body
            body_top_sr = max(0, body_top_sr - 1)

        wick_subrows = set(range(wick_top_sr, wick_bot_sr + 1))
        body_subrows = set(range(body_top_sr, body_bot_sr + 1))

        for r in range(rows):
            top_sr, bot_sr = r * 2, r * 2 + 1
            top_body = top_sr in body_subrows
            bot_body = bot_sr in body_subrows
            top_wick = top_sr in wick_subrows
            bot_wick = bot_sr in wick_subrows

            if top_body and bot_body:
                ch = "\u2588"       # █ full body
            elif top_body:
                ch = "\u2580"       # ▀ upper half body
            elif bot_body:
                ch = "\u2584"       # ▄ lower half body
            elif top_wick or bot_wick:
                ch = "\u2502"       # │ thin wick line
            else:
                continue

            grid[r][col] = ch
            styles[r][col] = color

    dp = _price_decimals(top, bottom)
    label_width = 10
    text = Text()
    for r in range(rows):
        price_at_row = top - (r / (rows - 1)) * price_range if rows > 1 else top
        text.append(f"{price_at_row:>{label_width - 1}.{dp}f} ", style="dim")
        for col in range(total_cols):
            ch = grid[r][col]
            style = styles[r][col] or None
            text.append(ch, style=style)
        text.append("\n")

    # time axis: a handful of evenly spaced timestamps under the grid
    text.append(" " * label_width)
    times = view["time"].tolist()
    label_every_candles = max(1, n // 5)
    axis_row = [" "] * total_cols
    for i in range(0, n, label_every_candles):
        stamp = times[i].strftime("%H:%M")
        col = i * col_width
        for j, ch in enumerate(stamp):
            if col + j < total_cols:
                axis_row[col + j] = ch
    text.append("".join(axis_row), style="dim")
    return text


def build_dashboard(symbol, tf_name, chart_df, open_trades, closed_trades, log_lines, next_close_at):
    header = Text.from_markup(
        f"[bold]{symbol}[/bold]  timeframe=[bold]{tf_name}[/bold]  "
        f"now={datetime.now().strftime('%H:%M:%S')}  "
        f"next candle close={next_close_at.strftime('%H:%M:%S')}"
    )

    chart_panel = Panel(
        build_ascii_candles(chart_df),
        title=f"{symbol} [{tf_name}] - last {min(len(chart_df), CHART_DISPLAY_CANDLES)} candles",
        border_style="cyan",
        expand=False,   # hug the chart's actual width instead of stretching
    )

    open_table = Table(title="Open Signals", box=box.SIMPLE_HEAVY, expand=True)
    for col in ["ID", "Pattern", "Dir", "Entry", "TP", "SL", "Open Time"]:
        open_table.add_column(col)
    for t in open_trades:
        open_table.add_row(
            t["id"], t["pattern"], t["direction"],
            f"{t['entry_price']:.5f}", f"{t['tp']:.5f}", f"{t['sl']:.5f}",
            t["open_time"],
        )

    closed_table = Table(title="Closed Signals (last 8)", box=box.SIMPLE_HEAVY, expand=True)
    for col in ["ID", "Result", "Entry", "Close", "PnL pts", "PnL %"]:
        closed_table.add_column(col)
    for t in closed_trades[-8:]:
        closed_table.add_row(
            t["id"], t["status"], f"{t['entry_price']:.5f}",
            f"{t['close_price']:.5f}", str(t["pnl_points"]), f"{t['pnl_percent']}%",
        )

    log_text = Text("\n".join(log_lines) or "...", style="dim")

    return Panel(
        Group(header, chart_panel, open_table, closed_table, Panel(log_text, title="Log", border_style="grey50")),
        title="MT5 Doji Signal Bot",
        border_style="magenta",
    )


def save_chart(df, trade, filename, extra_lines=None):
    if mpf is None:
        return None
    plot_df = df.set_index("time")[["open", "high", "low", "close", "tick_volume"]]
    plot_df.columns = ["Open", "High", "Low", "Close", "Volume"]

    hlines = {}
    if extra_lines:
        hlines = {
            "hlines": list(extra_lines.values()),
            "colors": ["blue", "green", "red"][: len(extra_lines)],
            "linestyle": "--",
        }

    path = CHARTS_DIR / filename
    mpf.plot(
        plot_df,
        type="candle",
        style="charles",
        title=f"{trade['symbol']} {trade['timeframe']} - {trade['pattern']}",
        volume=False,
        hlines=hlines if extra_lines else None,
        savefig=dict(fname=str(path), dpi=120, pad_inches=0.2),
    )
    return str(path)


# --------------------------------------------------------------------------
# Trade lifecycle
# --------------------------------------------------------------------------

def open_trade(symbol, timeframe_name, pattern, direction, signal_candle, df, history):
    info = mt5.symbol_info(symbol)
    if info is None:
        console.print(f"[red]Symbol {symbol} not found[/red]")
        return None
    point = info.point

    entry = float(signal_candle["close"])
    if direction == "BUY":
        tp = entry + TP_POINTS * point
        sl = entry - SL_POINTS * point
    else:
        tp = entry - TP_POINTS * point
        sl = entry + SL_POINTS * point

    trade = {
        "id": f"{symbol}-{int(time.time())}",
        "symbol": symbol,
        "timeframe": timeframe_name,
        "pattern": pattern,
        "direction": direction,
        "entry_price": entry,
        "open_price": float(signal_candle["open"]),
        "high": float(signal_candle["high"]),
        "low": float(signal_candle["low"]),
        "close_price_signal_candle": entry,
        "tp": tp,
        "sl": sl,
        "tp_points": TP_POINTS,
        "sl_points": SL_POINTS,
        "open_time": str(signal_candle["time"]),
        "status": "OPEN",
        "close_price": None,
        "close_time": None,
        "pnl_percent": None,
        "pnl_points": None,
        "candles": df.to_dict(orient="records"),
    }

    img_name = f"{trade['id']}_open.png"
    trade["image_open"] = save_chart(
        df, trade, img_name,
        extra_lines={"entry": entry, "tp": tp, "sl": sl},
    )

    history.append(trade)
    save_history(history)
    return trade


def check_trade(trade, symbol):
    """Checks live price against TP/SL. Returns True if trade closed."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False
    price = tick.bid if trade["direction"] == "BUY" else tick.ask

    hit = None
    if trade["direction"] == "BUY":
        if price >= trade["tp"]:
            hit = "TP"
        elif price <= trade["sl"]:
            hit = "SL"
    else:
        if price <= trade["tp"]:
            hit = "TP"
        elif price >= trade["sl"]:
            hit = "SL"

    if hit is None:
        return False

    close_trade(trade, price, hit, symbol)
    return True


def close_trade(trade, close_price, result, symbol):
    entry = trade["entry_price"]
    direction = trade["direction"]

    points = (close_price - entry) if direction == "BUY" else (entry - close_price)
    info = mt5.symbol_info(symbol)
    point_size = info.point if info else 0.0001
    pnl_points = points / point_size
    pnl_percent = (points / entry) * 100

    trade["status"] = result  # "TP" or "SL"
    trade["close_price"] = close_price
    trade["close_time"] = str(datetime.now())
    trade["pnl_points"] = round(pnl_points, 2)
    trade["pnl_percent"] = round(pnl_percent, 4)

    df = fetch_candles(symbol, TIMEFRAMES[trade["timeframe"]][0], CANDLE_HISTORY)
    if df is not None:
        img_name = f"{trade['id']}_closed.png"
        trade["image_closed"] = save_chart(
            df, trade, img_name,
            extra_lines={"entry": entry, "tp": trade["tp"], "sl": trade["sl"]},
        )


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def main():
    accounts = load_accounts()
    if not accounts:
        console.print("[red]No accounts found in .env. Copy .env.example to .env and fill it in.[/red]")
        sys.exit(1)

    account = pick_account(accounts)
    connect(account)

    symbol = Prompt.ask("Symbol (e.g. EURUSD)").strip().upper()
    if not mt5.symbol_select(symbol, True):
        console.print(f"[red]Could not select symbol {symbol}[/red]")
        sys.exit(1)

    tf_name = Prompt.ask(
        "Timeframe", choices=list(TIMEFRAMES.keys()), default="M1"
    ).upper()
    tf_const, tf_minutes = TIMEFRAMES[tf_name]

    history = load_history()
    open_trades = [t for t in history if t["status"] == "OPEN"]
    closed_trades = [t for t in history if t["status"] != "OPEN"]

    log_lines = []

    def log(msg):
        log_lines.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
        while len(log_lines) > LOG_LINES:
            log_lines.pop(0)

    log(f"Watching {symbol} on {tf_name}. TP={TP_POINTS}pts SL={SL_POINTS}pts.")

    next_close_at = next_close_boundary(tf_minutes)
    processed_boundary = None

    try:
        with Live(console=console, refresh_per_second=4, screen=False) as live:
            while True:
                now = datetime.now()

                # 1. Check every open trade against the live tick price.
                still_open = []
                for t in open_trades:
                    if check_trade(t, symbol):
                        color = "green" if t["status"] == "TP" else "red"
                        log(f"[{color}]{t['id']} hit {t['status']}[/{color}] -> "
                            f"PnL {t['pnl_points']} pts ({t['pnl_percent']}%)")
                        closed_trades.append(t)
                    else:
                        still_open.append(t)
                open_trades = still_open
                save_history(history)

                # 2. Refresh the live chart every tick using the freshest
                #    candles available (including the currently forming one).
                chart_df = fetch_candles(symbol, tf_const, CHART_DISPLAY_CANDLES)

                # 3. If we've crossed the next candle boundary, evaluate the
                #    just-closed candle for a pattern.
                if now >= next_close_at and next_close_at != processed_boundary:
                    df = fetch_candles(symbol, tf_const, CANDLE_HISTORY)
                    processed_boundary = next_close_at
                    next_close_at = next_close_boundary(tf_minutes, after=now)

                    if df is None or len(df) < 2:
                        log("[yellow]no candle data yet[/yellow]")
                    else:
                        signal_candle = df.iloc[-2]  # last fully closed candle
                        pattern = classify_candle(
                            signal_candle["open"], signal_candle["high"],
                            signal_candle["low"], signal_candle["close"],
                        )

                        if pattern is None:
                            log("no doji pattern")
                        else:
                            log(f"[bold yellow]pattern found: {pattern}[/bold yellow] "
                                f"on {signal_candle['time']}")
                            direction = PATTERN_DIRECTION[pattern]
                            if direction is None:
                                log("plain doji = indecision only, no trade opened")
                            else:
                                trade = open_trade(
                                    symbol, tf_name, pattern, direction,
                                    signal_candle, df, history,
                                )
                                if trade:
                                    open_trades.append(trade)
                                    log(f"[bold green]OPENED {direction} {trade['id']}[/bold green] "
                                        f"entry={trade['entry_price']:.5f} "
                                        f"tp={trade['tp']:.5f} sl={trade['sl']:.5f}")

                # 4. Redraw the dashboard.
                if chart_df is not None:
                    live.update(build_dashboard(
                        symbol, tf_name, chart_df, open_trades, closed_trades,
                        log_lines, next_close_at,
                    ))

                time.sleep(TICK_SECONDS)

    except KeyboardInterrupt:
        console.print("\n[cyan]Stopped by user.[/cyan]")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()