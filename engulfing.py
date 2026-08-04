"""
Engulfing Candle Watcher for MetaTrader 5 (with rich UI + simulator)
----------------------------------------------------------------------
Two modes:
  1. Live  - logs into your MT5 account, waits for each new candle
             close on the timeframe you choose, and reports bullish /
             bearish engulfing patterns on the real market.
  2. Simulate - no MT5 login needed. Generates synthetic OHLC candles
             (with occasional guaranteed engulfing setups mixed with
             random noise) so you can watch the detector work without
             touching a live/demo account.

Auto-terminal detection: in live mode the script searches common
install locations for terminal64.exe and passes that path to MT5,
instead of relying on whichever terminal happens to be open - this
avoids the "stuck session" auth errors from a stale terminal.

On every detected pattern the script now:
  1. Appends a record to a JSON log file (signals/engulfing_signals.json).
  2. Exports a PNG chart of the last 10 candles with a big up/down
     arrow marking the pattern candle (signals/charts/...).

Requirements:
    pip install rich MetaTrader5 pandas matplotlib

This script is for educational / technical-analysis purposes only.
It does not place trades and is not financial advice.
"""

import glob
import json
import os
import random
import time
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, IntPrompt
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeRemainingColumn

console = Console()

TIMEFRAMES = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}

SIGNALS_DIR = "signals"
JSON_LOG_PATH = os.path.join(SIGNALS_DIR, "engulfing_signals.json")
TRADES_JSON_PATH = os.path.join(SIGNALS_DIR, "trade_results.json")
CHARTS_DIR = os.path.join(SIGNALS_DIR, "charts")

# Follow-the-trade thresholds (in broker "points", i.e. the smallest
# quoted price increment - NOT necessarily the same as a "pip").
# Per spec: TP = 180 points (~8 pip), SL = 150 points (~15 pip),
# same thresholds for both bullish and bearish signals.
TP_POINTS = 180
SL_POINTS = 150


# --------------------------------------------------------------------------
# Terminal auto-detection (live mode)
# --------------------------------------------------------------------------

def find_terminal_path():
    """Search common Windows install locations for terminal64.exe."""
    candidates = []

    program_dirs = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
    ]
    for pf in program_dirs:
        candidates += glob.glob(os.path.join(pf, "*", "terminal64.exe"))

    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates += glob.glob(
            os.path.join(appdata, "MetaQuotes", "Terminal", "*", "terminal64.exe")
        )

    return candidates[0] if candidates else None


def print_terminal_panel(path, attempt=None, retries=None, connected=False, login=None, server=None):
    """Single, updating panel showing the auto-terminal-detection status."""
    lines = []
    if path:
        lines.append(f"[green]Terminal:[/green] auto-detected")
        lines.append(f"[bold]Path:[/bold] {path}")
    else:
        lines.append("[yellow]Terminal:[/yellow] not found automatically - using default launch")

    if attempt is not None:
        lines.append(f"[bold]Connection attempt:[/bold] {attempt}/{retries}")

    if connected:
        lines.append(f"[bold green]Status: CONNECTED[/bold green]  account {login} @ {server}")
        style = "green"
    else:
        lines.append("[bold]Status:[/bold] connecting...")
        style = "cyan"

    console.print(Panel("\n".join(lines), title="MT5 Auto-Terminal", border_style=style))


def connect_live(login, password, server, retries=3):
    import MetaTrader5 as mt5

    with console.status("[cyan]Auto-detecting your MT5 terminal installation...", spinner="dots"):
        path = find_terminal_path()

    last_error = None
    for attempt in range(1, retries + 1):
        print_terminal_panel(path, attempt=attempt, retries=retries)

        mt5.shutdown()
        time.sleep(1)

        ok_init = mt5.initialize(path=path) if path else mt5.initialize()
        if not ok_init:
            last_error = mt5.last_error()
            console.print(f"[red]initialize() failed:[/red] {last_error}")
            continue

        if mt5.login(login, password=password, server=server):
            print_terminal_panel(path, connected=True, login=login, server=server)
            return mt5

        last_error = mt5.last_error()
        console.print(f"[red]login() failed:[/red] {last_error}")

    mt5.shutdown()
    console.print(Panel(
        f"[bold red]Could not connect after {retries} attempts.[/bold red]\n"
        f"Last error: {last_error}\n\n"
        "Checklist:\n"
        "  1. Fully quit MT5 (check Task Manager for terminal64.exe) and retry.\n"
        "  2. Log in manually in the MT5 desktop app with the same account/\n"
        "     password/server to confirm the credentials work outside Python.\n"
        "  3. Password must be the TRADE password, not the investor (read-only) one.\n"
        "  4. Server name must match exactly what's listed in MT5's server list.",
        border_style="red",
        title="Connection failed",
    ))
    raise SystemExit(1)


def get_live_closed_candles(mt5, symbol, tf_key, n_closed=10):
    """Returns the last n_closed CLOSED candles (oldest -> newest).
    Fetches n_closed+1 bars and drops the still-forming last one."""
    import pandas as pd

    tf_const = getattr(mt5, f"TIMEFRAME_{tf_key}")
    rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, n_closed + 1)
    if rates is None or len(rates) < 2:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    records = df.to_dict("records")
    return records[:-1]  # drop the forming bar


# --------------------------------------------------------------------------
# Candle simulator
# --------------------------------------------------------------------------

class CandleSimulator:
    """Generates synthetic OHLC candles - mostly random noise, with
    occasional deliberately-engineered bullish/bearish engulfing
    setups so the detector has something to catch."""

    def __init__(self, symbol, start_price=2000.0):
        self.symbol = symbol
        self.last_close = start_price
        self.pending = None
        self.candles = []

    def _random_candle(self):
        open_p = self.last_close
        move = random.gauss(0, 1.2)
        close_p = open_p + move
        high_p = max(open_p, close_p) + abs(random.gauss(0, 0.5))
        low_p = min(open_p, close_p) - abs(random.gauss(0, 0.5))
        return open_p, high_p, low_p, close_p

    def next_candle(self):
        if self.pending == "bull_setup":
            open_p = self.last_close
            close_p = open_p - abs(random.gauss(0.3, 0.15)) - 0.1
            high_p, low_p = open_p + 0.1, close_p - 0.1
            self.pending = "bull_confirm"
        elif self.pending == "bull_confirm":
            prev = self.candles[-1]
            open_p = prev["close"] - abs(random.gauss(0.1, 0.05))
            close_p = prev["open"] + abs(random.gauss(0.3, 0.15))
            high_p, low_p = close_p + 0.1, open_p - 0.1
            self.pending = None
        elif self.pending == "bear_setup":
            open_p = self.last_close
            close_p = open_p + abs(random.gauss(0.3, 0.15)) + 0.1
            high_p, low_p = close_p + 0.1, open_p - 0.1
            self.pending = "bear_confirm"
        elif self.pending == "bear_confirm":
            prev = self.candles[-1]
            open_p = prev["close"] + abs(random.gauss(0.1, 0.05))
            close_p = prev["open"] - abs(random.gauss(0.3, 0.15))
            high_p, low_p = open_p + 0.1, close_p - 0.1
            self.pending = None
        else:
            open_p, high_p, low_p, close_p = self._random_candle()
            if random.random() < 0.12:
                self.pending = random.choice(["bull_setup", "bear_setup"])

        candle = {
            "time": datetime.now(),
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
        }
        self.candles.append(candle)
        self.last_close = close_p
        return candle

    def last_n(self, n=3):
        return self.candles[-n:] if len(self.candles) >= 2 else None


# --------------------------------------------------------------------------
# Pattern detection
# --------------------------------------------------------------------------

def is_bullish_engulfing(prev, curr):
    prev_bearish = prev["close"] < prev["open"]
    curr_bullish = curr["close"] > curr["open"]
    engulfs = curr["open"] <= prev["close"] and curr["close"] >= prev["open"]
    return prev_bearish and curr_bullish and engulfs


def is_bearish_engulfing(prev, curr):
    prev_bullish = prev["close"] > prev["open"]
    curr_bearish = curr["close"] < curr["open"]
    engulfs = curr["open"] >= prev["close"] and curr["close"] <= prev["open"]
    return prev_bullish and curr_bearish and engulfs


def classify_direction(row):
    if row["close"] > row["open"]:
        return "Bullish", "green"
    if row["close"] < row["open"]:
        return "Bearish", "red"
    return "Doji", "yellow"


# --------------------------------------------------------------------------
# Trade following (TP/SL tracking after a signal fires)
# --------------------------------------------------------------------------

def open_trade(symbol, direction, entry_price, entry_time, point_size,
                 tp_points=TP_POINTS, sl_points=SL_POINTS):
    if direction == "bull":
        tp_price = entry_price + tp_points * point_size
        sl_price = entry_price - sl_points * point_size
    else:
        tp_price = entry_price - tp_points * point_size
        sl_price = entry_price + sl_points * point_size

    return {
        "symbol": symbol,
        "direction": direction,
        "entry_price": entry_price,
        "entry_time": _ts_str(entry_time),
        "tp_price": tp_price,
        "sl_price": sl_price,
        "tp_points": tp_points,
        "sl_points": sl_points,
        "point_size": point_size,
        "status": "open",
        "tp_status": None,
        "sl_status": None,
    }


def check_trade(trade, candle):
    """Check if this candle's high/low touched TP or SL. Returns 'tp', 'sl', or None."""
    if trade["direction"] == "bull":
        if candle["high"] >= trade["tp_price"]:
            return "tp"
        if candle["low"] <= trade["sl_price"]:
            return "sl"
    else:
        if candle["low"] <= trade["tp_price"]:
            return "tp"
        if candle["high"] >= trade["sl_price"]:
            return "sl"
    return None


def save_trade_result_json(trade_result):
    os.makedirs(SIGNALS_DIR, exist_ok=True)

    data = []
    if os.path.exists(TRADES_JSON_PATH):
        try:
            with open(TRADES_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = []

    data.append(trade_result)
    with open(TRADES_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)

    return TRADES_JSON_PATH


def report_trade_result(trade, outcome, hit_candle):
    exit_price = trade["tp_price"] if outcome == "tp" else trade["sl_price"]
    status = "TP_HIT" if outcome == "tp" else "SL_HIT"

    trade_result = dict(trade)
    trade_result["status"] = status
    trade_result["exit_price"] = exit_price
    trade_result["exit_time"] = _ts_str(hit_candle["time"])

    json_path = save_trade_result_json(trade_result)

    color = "green" if outcome == "tp" else "red"
    label = "TAKE PROFIT HIT" if outcome == "tp" else "STOP LOSS HIT"
    console.print(Panel(
        f"[bold]{trade['symbol']} {trade['direction'].upper()} trade -> {label}[/bold]\n"
        f"Entry: {trade['entry_price']:.3f}   Exit: {exit_price:.3f}\n"
        f"[dim]Saved -> {json_path}[/dim]",
        border_style=color,
        title="Trade closed",
    ))

    return trade_result


# --------------------------------------------------------------------------
# JSON logging + PNG chart export (on every detected pattern)
# --------------------------------------------------------------------------

def _ts_str(ts):
    return ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(ts, "strftime") else str(ts)


def save_signal_json(symbol, pattern, prev, curr, ts):
    os.makedirs(SIGNALS_DIR, exist_ok=True)

    entry = {
        "symbol": symbol,
        "pattern": "bullish_engulfing" if pattern == "bull" else "bearish_engulfing",
        "time": _ts_str(ts),
        "prev_open": prev["open"],
        "prev_close": prev["close"],
        "curr_open": curr["open"],
        "curr_close": curr["close"],
    }

    data = []
    if os.path.exists(JSON_LOG_PATH):
        try:
            with open(JSON_LOG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = []

    data.append(entry)
    with open(JSON_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)

    return JSON_LOG_PATH


def export_chart_png(symbol, pattern, history, ts):
    """Draw the last up-to-10 candles and mark the pattern candle with
    a big up/down arrow. Returns the saved file path."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(CHARTS_DIR, exist_ok=True)

    candles = history[-10:]
    fig, ax = plt.subplots(figsize=(10, 6))

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    span = max(highs) - min(lows) or 1.0

    for i, c in enumerate(candles):
        color = "#2ecc71" if c["close"] >= c["open"] else "#e74c3c"
        body_low = min(c["open"], c["close"])
        body_high = max(c["open"], c["close"])
        ax.add_patch(plt.Rectangle(
            (i - 0.3, body_low), 0.6, (body_high - body_low) or span * 0.01,
            color=color, zorder=3,
        ))
        ax.plot([i, i], [c["low"], c["high"]], color=color, linewidth=1, zorder=2)

    last_idx = len(candles) - 1
    last = candles[-1]
    if pattern == "bull":
        arrow, y, va, color = "\U0001F53C", last["low"] - span * 0.08, "top", "#2ecc71"
    else:
        arrow, y, va, color = "\U0001F53D", last["high"] + span * 0.08, "bottom", "#e74c3c"

    try:
        ax.annotate(arrow, xy=(last_idx, y), fontsize=28, ha="center", va=va,
                    fontname="Segoe UI Emoji")
    except Exception:
        ax.annotate(arrow, xy=(last_idx, y), fontsize=28, ha="center", va=va)

    labels = [
        c["time"].strftime("%H:%M") if hasattr(c["time"], "strftime") else str(c["time"])
        for c in candles
    ]
    ax.set_xticks(range(len(candles)))
    ax.set_xticklabels(labels, rotation=45, ha="right")

    title_word = "BULLISH ENGULFING" if pattern == "bull" else "BEARISH ENGULFING"
    ax.set_title(f"{symbol} - {title_word} @ {_ts_str(ts)}")
    ax.set_ylabel("Price")
    ax.margins(x=0.05)
    fig.tight_layout()

    fname = f"{symbol}_{pattern}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    fpath = os.path.join(CHARTS_DIR, fname)
    fig.savefig(fpath, dpi=150)
    plt.close(fig)

    return fpath


def export_trade_result_chart(trade_result, history):
    """Chart the last up-to-10 candles plus entry/TP/SL lines and the
    final WIN/LOSS outcome for a closed trade."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(CHARTS_DIR, exist_ok=True)

    candles = history[-10:]
    fig, ax = plt.subplots(figsize=(10, 6))

    for i, c in enumerate(candles):
        color = "#2ecc71" if c["close"] >= c["open"] else "#e74c3c"
        body_low = min(c["open"], c["close"])
        body_high = max(c["open"], c["close"])
        ax.add_patch(plt.Rectangle(
            (i - 0.3, body_low), 0.6, (body_high - body_low) or 0.01,
            color=color, zorder=3,
        ))
        ax.plot([i, i], [c["low"], c["high"]], color=color, linewidth=1, zorder=2)

    ax.axhline(trade_result["entry_price"], color="dodgerblue", linestyle="--", linewidth=1.2, label="Entry")
    ax.axhline(trade_result["tp_price"], color="orange", linestyle="-", linewidth=1.2, label="TP")
    ax.axhline(trade_result["sl_price"], color="red", linestyle="-", linewidth=1.2, label="SL")

    win = trade_result["status"] == "TP_HIT"
    outcome_text = "TP HIT (WIN)" if win else "SL HIT (LOSS)"
    outcome_color = "#2ecc71" if win else "#e74c3c"

    ax.set_title(
        f"{trade_result['symbol']} {trade_result['direction'].upper()} - {outcome_text}",
        color=outcome_color, fontweight="bold",
    )
    ax.legend(loc="upper left", fontsize=8)

    labels = [
        c["time"].strftime("%H:%M") if hasattr(c["time"], "strftime") else str(c["time"])
        for c in candles
    ]
    ax.set_xticks(range(len(candles)))
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.set_ylabel("Price")
    ax.margins(x=0.05)
    fig.tight_layout()

    fname = (
        f"{trade_result['symbol']}_{trade_result['direction']}_{trade_result['status']}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    )
    fpath = os.path.join(CHARTS_DIR, fname)
    fig.savefig(fpath, dpi=150)
    plt.close(fig)

    return fpath


# --------------------------------------------------------------------------
# Rich display helpers
# --------------------------------------------------------------------------

def print_candle_table(symbol, prev, curr, ts):
    table = Table(title=f"{symbol} - last two candles @ {ts}", header_style="bold cyan")
    table.add_column("Candle")
    table.add_column("Open", justify="right")
    table.add_column("Close", justify="right")
    table.add_column("Direction")

    for label, row in [("Previous", prev), ("Current", curr)]:
        direction, color = classify_direction(row)
        table.add_row(label, f"{row['open']:.3f}", f"{row['close']:.3f}", f"[{color}]{direction}[/{color}]")

    console.print(table)


def announce_pattern(kind, symbol, ts, repeated=False):
    if kind == "bull":
        label = "BULLISH ENGULFING (still active)" if repeated else "BULLISH ENGULFING"
        console.print(Panel(
            f"[bold white on green] {label} [/bold white on green]  {symbol} @ {ts}",
            border_style="green",
        ))
    elif kind == "bear":
        label = "BEARISH ENGULFING (still active)" if repeated else "BEARISH ENGULFING"
        console.print(Panel(
            f"[bold white on red] {label} [/bold white on red]  {symbol} @ {ts}",
            border_style="red",
        ))
    else:
        msg = "Same as last check - still no engulfing pattern" if repeated else "No engulfing pattern"
        console.print(f"[dim]{msg} @ {ts}[/dim]")


def check_and_report(symbol, history, ts, last_result=None):
    """history: list of the most recent CLOSED candles (oldest -> newest,
    up to 10). The last two entries are the pair being checked."""
    prev, curr = history[-2], history[-1]

    console.print(f"[bold blue]Candle closed[/bold blue] @ {ts}")
    print_candle_table(symbol, prev, curr, ts)

    if is_bullish_engulfing(prev, curr):
        result = "bull"
    elif is_bearish_engulfing(prev, curr):
        result = "bear"
    else:
        result = None

    announce_pattern(result, symbol, ts, repeated=(result == last_result))

    if result is not None:
        json_path = save_signal_json(symbol, result, prev, curr, ts)
        png_path = export_chart_png(symbol, result, history, ts)
        console.print(
            f"[dim]Saved log -> {json_path}[/dim]\n"
            f"[dim]Saved chart -> {png_path}[/dim]"
        )

    return result


def handle_candle_close(symbol, history, ts, last_result, active_trade, point_size):
    """Runs on every candle close: checks any active trade for TP/SL,
    then runs pattern detection, and opens a new trade to follow if a
    fresh signal fires and nothing is already being followed."""
    curr = history[-1]

    if active_trade is not None:
        outcome = check_trade(active_trade, curr)
        if outcome:
            trade_result = report_trade_result(active_trade, outcome, curr)
            png_path = export_trade_result_chart(trade_result, history)
            console.print(f"[dim]Saved trade chart -> {png_path}[/dim]")
            active_trade = None

    new_result = check_and_report(symbol, history, ts, last_result)

    if new_result is not None and active_trade is None:
        entry_price = curr["close"]
        active_trade = open_trade(symbol, new_result, entry_price, curr["time"], point_size)
        direction_label = "BULLISH" if new_result == "bull" else "BEARISH"
        console.print(Panel(
            f"[bold]Following {symbol} {direction_label}[/bold]\n"
            f"Entry: {entry_price:.3f}\n"
            f"TP: {active_trade['tp_price']:.3f}  (+{TP_POINTS} pts)\n"
            f"SL: {active_trade['sl_price']:.3f}  (-{SL_POINTS} pts)",
            border_style="cyan",
            title="Trade opened - following until TP/SL",
        ))

    return new_result, active_trade


def wait_with_countdown(seconds, label):
    seconds = max(0, seconds)
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(label, total=seconds)
        remaining = seconds
        while remaining > 0:
            step = min(1, remaining)
            time.sleep(step)
            progress.update(task, advance=step)
            remaining -= step


def seconds_until_next_close(tf_seconds):
    now = datetime.now()
    epoch = now.timestamp()
    next_boundary = (int(epoch) // tf_seconds + 1) * tf_seconds
    wait_seconds = next_boundary - epoch
    return wait_seconds, datetime.fromtimestamp(next_boundary)


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

def run_live():
    login = IntPrompt.ask("Account ID (login)")
    password = Prompt.ask("Password", password=False)
    server = Prompt.ask("Server")
    symbol = Prompt.ask("Symbol", default="EURUSD").strip().upper()
    tf_key = Prompt.ask("Timeframe", choices=list(TIMEFRAMES.keys()), default="M1")
    tf_seconds = TIMEFRAMES[tf_key]

    mt5 = connect_live(login, password, server)

    if not mt5.symbol_select(symbol, True):
        mt5.shutdown()
        console.print(f"[bold red]Symbol '{symbol}' not found or not visible in Market Watch.[/bold red]")
        raise SystemExit(1)

    info = mt5.symbol_info(symbol)
    point_size = info.point if info and info.point else 0.01
    console.print(f"[dim]Using point size {point_size} for {symbol} (TP {TP_POINTS} pts / SL {SL_POINTS} pts).[/dim]")

    console.print(f"[bold]Watching {symbol} on {tf_key}.[/bold] Press Ctrl+C to stop.\n")

    last_result = None
    active_trade = None
    try:
        while True:
            wait_s, next_close = seconds_until_next_close(tf_seconds)
            total_wait = wait_s + 2  # buffer for broker to publish the bar
            wait_with_countdown(
                total_wait,
                f"Next {tf_key} candle closes at {next_close.strftime('%H:%M:%S')}",
            )
            history = get_live_closed_candles(mt5, symbol, tf_key, n_closed=10)
            if not history or len(history) < 2:
                console.print("[yellow]Not enough candle data returned, skipping this check.[/yellow]")
                continue
            last_result, active_trade = handle_candle_close(
                symbol, history, history[-1]["time"], last_result, active_trade, point_size
            )
    except KeyboardInterrupt:
        console.print("\n[bold]Stopped by user.[/bold]")
    finally:
        mt5.shutdown()


def run_simulate():
    symbol = Prompt.ask("Symbol (simulated)", default="XAUUSD").strip().upper()
    tf_key = Prompt.ask("Timeframe label (cosmetic only)", choices=list(TIMEFRAMES.keys()), default="M1")
    interval = IntPrompt.ask("Seconds between simulated candles (e.g. 3 for a fast demo)", default=3)
    point_size = float(Prompt.ask("Point size (price value of 1 point)", default="0.01"))

    sim = CandleSimulator(symbol)
    console.print(f"[bold]Simulating {symbol} ({tf_key}) candles every {interval}s.[/bold] Press Ctrl+C to stop.\n")

    last_result = None
    active_trade = None
    try:
        while True:
            wait_with_countdown(interval, "Generating next simulated candle")
            sim.next_candle()
            history = sim.last_n(10)
            if not history or len(history) < 2:
                continue
            ts = history[-1]["time"].strftime("%H:%M:%S")
            last_result, active_trade = handle_candle_close(
                symbol, history, ts, last_result, active_trade, point_size
            )
    except KeyboardInterrupt:
        console.print("\n[bold]Simulation stopped by user.[/bold]")


def main():
    console.print(Panel.fit(
        "[bold cyan]Engulfing Candle Watcher[/bold cyan]\nLive MT5 detection or a synthetic candle simulator.",
        border_style="cyan",
    ))
    mode = Prompt.ask("Mode", choices=["live", "simulate"], default="simulate")
    if mode == "live":
        run_live()
    else:
        run_simulate()


if __name__ == "__main__":
    main()