"""
Engulfing Candle Watcher for MetaTrader 5 (live dashboard + simulator)
------------------------------------------------------------------------
Two modes:
  1. Live  - logs into your MT5 account, waits for each new candle
             close on the timeframe you choose, and reports bullish /
             bearish engulfing patterns on the real market.
  2. Simulate - no MT5 login needed. Generates synthetic OHLC candles
             (with occasional guaranteed engulfing setups mixed with
             random noise) so you can watch the detector work without
             touching a live/demo account.

Auto-terminal detection: in live mode the script searches common
install locations for terminal64.exe and connects through that exact
path, avoiding "stuck session" auth errors from a stale terminal.

Trade following (SIMULATED - never touches your real MT5 account):
  When a pattern fires and nothing is already being followed, the
  script opens a paper trade:
    Long (bullish):  TP = entry + 180 points, SL = entry - 150 points
    Short (bearish): TP = entry - 180 points, SL = entry + 150 points
  It's followed candle-by-candle until TP or SL is touched. If a new
  signal fires while a trade is already active, it's shown in the
  console but not logged or followed - only followed trades get
  written to JSON/PNG.

Same-candle TP/SL ambiguity: OHLC alone can't tell which level was
touched first inside one bar. If both are in range, it's counted as
SL (the conservative/worse outcome) and flagged "ambiguous": true.

UI: a single live-updating dashboard panel showing:
  - status / countdown
  - latest closed candle OHLC
  - a small ASCII candlestick chart of the last ~15 candles
  - a table of ALL trades this session (long/short, entry, current
    price, PnL in points, PnL in %)
  - a short rolling log of recent events
No more scrolling panel-per-candle spam.

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

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt, IntPrompt
from rich.table import Table
from rich.rule import Rule
from rich.text import Text

console = Console()

TIMEFRAMES = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400,
}

SIGNALS_DIR = "signals"
JSON_LOG_PATH = os.path.join(SIGNALS_DIR, "engulfing_signals.json")
CHARTS_DIR = os.path.join(SIGNALS_DIR, "charts")

# Simulated trade thresholds, in broker "points" (smallest quoted
# price increment). Same thresholds for long and short.
TP_POINTS = 180
SL_POINTS = 150

CHART_CANDLES = 15   # candles shown in the ASCII chart
CHART_HEIGHT = 12    # rows of the ASCII chart


# --------------------------------------------------------------------------
# Terminal auto-detection (live mode)
# --------------------------------------------------------------------------

def find_terminal_path():
    candidates = []
    program_dirs = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
    ]
    for pf in program_dirs:
        candidates += glob.glob(os.path.join(pf, "*", "terminal64.exe"))
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates += glob.glob(os.path.join(appdata, "MetaQuotes", "Terminal", "*", "terminal64.exe"))
    return candidates[0] if candidates else None


def connect_live(login, password, server, retries=3):
    import MetaTrader5 as mt5

    with console.status("[cyan]Auto-detecting your MT5 terminal installation...", spinner="dots"):
        path = find_terminal_path()

    path_line = f"[green]Found:[/green] {path}" if path else "[yellow]Not found - using default launch[/yellow]"

    last_error = None
    with console.status("", spinner="dots") as status:
        for attempt in range(1, retries + 1):
            status.update(f"[cyan]Connecting (attempt {attempt}/{retries})...[/cyan]  {path_line}")
            mt5.shutdown()
            time.sleep(1)

            ok_init = mt5.initialize(path=path) if path else mt5.initialize()
            if not ok_init:
                last_error = mt5.last_error()
                continue

            if mt5.login(login, password=password, server=server):
                console.print(Panel(
                    f"{path_line}\n[bold green]Connected[/bold green] - account {login} @ {server}",
                    title="MT5 Auto-Terminal", border_style="green",
                ))
                return mt5

            last_error = mt5.last_error()

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
        border_style="red", title="Connection failed",
    ))
    raise SystemExit(1)


def get_live_closed_candles(mt5, symbol, tf_key, n_closed=CHART_CANDLES):
    import pandas as pd
    tf_const = getattr(mt5, f"TIMEFRAME_{tf_key}")
    rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, n_closed + 1)
    if rates is None or len(rates) < 2:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df.to_dict("records")[:-1]  # drop the still-forming bar


def get_live_tick(mt5, symbol):
    """Fetch the latest tick (bid/ask/last) for the symbol. Returns dict or None."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    info = mt5.symbol_info(symbol)
    digits = info.digits if info else 5
    point = info.point if info else 0.00001
    spread = info.spread if info else 0
    return {
        "bid": round(tick.bid, digits),
        "ask": round(tick.ask, digits),
        "last": round(tick.last, digits) if tick.last else tick.bid,
        "spread": spread,
        "spread_price": round(tick.ask - tick.bid, digits),
        "time": datetime.fromtimestamp(tick.time),
    }


def get_forming_candle(mt5, symbol, tf_key):
    """Get the current still-forming candle (open/high/low/close from live ticks)."""
    import pandas as pd
    tf_const = getattr(mt5, f"TIMEFRAME_{tf_key}")
    rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, 1)
    if rates is None or len(rates) < 1:
        return None
    rate = rates[0]
    info = mt5.symbol_info(symbol)
    digits = info.digits if info else 5
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    last_price = tick.last if tick.last else tick.bid
    return {
        "time": datetime.fromtimestamp(rate["time"]),
        "open": round(rate["open"], digits),
        "high": round(rate["high"], digits),
        "low": round(rate["low"], digits),
        "close": round(last_price, digits),
    }


# --------------------------------------------------------------------------
# Candle simulator
# --------------------------------------------------------------------------

class CandleSimulator:
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

        candle = {"time": datetime.now(), "open": open_p, "high": high_p, "low": low_p, "close": close_p}
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


def _ts_str(ts):
    return ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(ts, "strftime") else str(ts)


# --------------------------------------------------------------------------
# JSON logging + PNG export
# --------------------------------------------------------------------------

def save_signal_json(symbol, pattern, prev, curr, ts, trade):
    os.makedirs(SIGNALS_DIR, exist_ok=True)
    entry = {
        "symbol": symbol,
        "pattern": "bullish_engulfing" if pattern == "bull" else "bearish_engulfing",
        "time": _ts_str(ts),
        "prev_open": prev["open"], "prev_close": prev["close"],
        "curr_open": curr["open"], "curr_close": curr["close"],
        "entry_price": trade["entry_price"], "tp_price": trade["tp_price"], "sl_price": trade["sl_price"],
        "status": "open", "tp_status": None, "sl_status": None, "ambiguous": None,
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


def update_signal_json_status(entry_time, tp_status, sl_status, status, ambiguous=False):
    if not os.path.exists(JSON_LOG_PATH):
        return
    try:
        with open(JSON_LOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    time_str = _ts_str(entry_time)
    for entry in reversed(data):
        if entry.get("time") == time_str:
            entry["tp_status"] = tp_status
            entry["sl_status"] = sl_status
            entry["status"] = status
            entry["ambiguous"] = ambiguous
            break
    with open(JSON_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def export_chart_png(symbol, pattern, history, ts):
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
        body_low, body_high = min(c["open"], c["close"]), max(c["open"], c["close"])
        ax.add_patch(plt.Rectangle((i - 0.3, body_low), 0.6, (body_high - body_low) or span * 0.01,
                                    color=color, zorder=3))
        ax.plot([i, i], [c["low"], c["high"]], color=color, linewidth=1, zorder=2)

    last_idx = len(candles) - 1
    last = candles[-1]
    if pattern == "bull":
        arrow, y, va = "\U0001F53C", last["low"] - span * 0.08, "top"
    else:
        arrow, y, va = "\U0001F53D", last["high"] + span * 0.08, "bottom"
    try:
        ax.annotate(arrow, xy=(last_idx, y), fontsize=28, ha="center", va=va, fontname="Segoe UI Emoji")
    except Exception:
        ax.annotate(arrow, xy=(last_idx, y), fontsize=28, ha="center", va=va)

    labels = [c["time"].strftime("%H:%M") if hasattr(c["time"], "strftime") else str(c["time"]) for c in candles]
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
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(CHARTS_DIR, exist_ok=True)
    candles = history[-10:]
    fig, ax = plt.subplots(figsize=(10, 6))

    for i, c in enumerate(candles):
        color = "#2ecc71" if c["close"] >= c["open"] else "#e74c3c"
        body_low, body_high = min(c["open"], c["close"]), max(c["open"], c["close"])
        ax.add_patch(plt.Rectangle((i - 0.3, body_low), 0.6, (body_high - body_low) or 0.01,
                                    color=color, zorder=3))
        ax.plot([i, i], [c["low"], c["high"]], color=color, linewidth=1, zorder=2)

    ax.axhline(trade_result["entry_price"], color="dodgerblue", linestyle="--", linewidth=1.2, label="Entry")
    ax.axhline(trade_result["tp_price"], color="orange", linestyle="-", linewidth=1.2, label="TP")
    ax.axhline(trade_result["sl_price"], color="red", linestyle="-", linewidth=1.2, label="SL")

    win = trade_result["status"] == "TP_HIT"
    outcome_text = "TP HIT (WIN)" if win else "SL HIT (LOSS)"
    if trade_result.get("ambiguous"):
        outcome_text += " *ambiguous candle*"
    outcome_color = "#2ecc71" if win else "#e74c3c"
    ax.set_title(f"{trade_result['symbol']} {trade_result['direction'].upper()} - {outcome_text}",
                 color=outcome_color, fontweight="bold")
    ax.legend(loc="upper left", fontsize=8)

    labels = [c["time"].strftime("%H:%M") if hasattr(c["time"], "strftime") else str(c["time"]) for c in candles]
    ax.set_xticks(range(len(candles)))
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.set_ylabel("Price")
    ax.margins(x=0.05)
    fig.tight_layout()

    fname = (f"{trade_result['symbol']}_{trade_result['direction']}_{trade_result['status']}_"
             f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
    fpath = os.path.join(CHARTS_DIR, fname)
    fig.savefig(fpath, dpi=150)
    plt.close(fig)
    return fpath


# --------------------------------------------------------------------------
# Trade following - real MT5 orders
# --------------------------------------------------------------------------

def mt5_open_position(mt5, symbol, direction, lot, tp_price, sl_price):
    """Send a market order to MT5. Returns (ticket, price) or (None, error)."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return None, f"symbol_info returned None for {symbol}"
    if not info.visible:
        mt5.symbol_select(symbol, True)

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None, "symbol_info_tick returned None"

    if direction == "bull":
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
    else:
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "tp": tp_price,
        "sl": sl_price,
        "deviation": 20,
        "magic": 234000,
        "comment": "engulfing",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }
    result = mt5.order_send(request)
    if result is None:
        return None, "order_send returned None"
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return None, f"retcode={result.retcode} comment={result.comment}"
    return result.order, result.price


def mt5_close_position(mt5, symbol, direction, ticket, lot):
    """Close an open position by sending an opposite market order."""
    # Find position by symbol and direction
    positions = mt5.positions_get(symbol=symbol)
    if positions is None or len(positions) == 0:
        return None, f"No positions found for {symbol}"
    
    # Find the position matching our direction
    pos = None
    for p in positions:
        if (direction == "bull" and p.type == mt5.ORDER_TYPE_BUY) or \
           (direction == "bear" and p.type == mt5.ORDER_TYPE_SELL):
            pos = p
            break
    
    if pos is None:
        return None, f"No {direction} position found for {symbol}"
    
    # Use TRADE_ACTION_CLOSE_BY to close the position
    request = {
        "action": mt5.TRADE_ACTION_CLOSE_BY,
        "position": pos.ticket,
        "symbol": symbol,
        "volume": lot,
        "deviation": 20,
        "magic": 234000,
        "comment": "engulfing close",
    }
    result = mt5.order_send(request)
    if result is None:
        return None, "order_send returned None"
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return None, f"retcode={result.retcode} comment={result.comment}"
    return result.order, pos.price

def open_trade(symbol, direction, entry_price, entry_time, point_size, lot, ticket=None,
               tp_points=TP_POINTS, sl_points=SL_POINTS):
    if direction == "bull":
        tp_price = entry_price + tp_points * point_size
        sl_price = entry_price - sl_points * point_size
    else:
        tp_price = entry_price - tp_points * point_size
        sl_price = entry_price + sl_points * point_size
    return {
        "symbol": symbol, "direction": direction, "entry_price": entry_price,
        "entry_time": _ts_str(entry_time), "tp_price": tp_price, "sl_price": sl_price,
        "tp_points": tp_points, "sl_points": sl_points, "point_size": point_size,
        "lot": lot, "ticket": ticket,
        "status": "open", "tp_status": None, "sl_status": None,
        "current_price": entry_price,
    }


def check_trade(trade, candle):
    if trade["direction"] == "bull":
        hit_tp = candle["high"] >= trade["tp_price"]
        hit_sl = candle["low"] <= trade["sl_price"]
    else:
        hit_tp = candle["low"] <= trade["tp_price"]
        hit_sl = candle["high"] >= trade["sl_price"]
    if hit_tp and hit_sl:
        return "sl", True
    if hit_tp:
        return "tp", False
    if hit_sl:
        return "sl", False
    return None, False


def pnl_for(trade):
    """Returns (pnl_points, pnl_pct) given trade['current_price']."""
    entry = trade["entry_price"]
    current = trade["current_price"]
    point_size = trade["point_size"] or 0.01
    if trade["direction"] == "bull":
        diff = current - entry
    else:
        diff = entry - current
    pnl_points = diff / point_size
    pnl_pct = (diff / entry) * 100 if entry else 0.0
    return pnl_points, pnl_pct


# --------------------------------------------------------------------------
# Live dashboard
# --------------------------------------------------------------------------

def render_ascii_chart(candles, height=CHART_HEIGHT):
    """Small in-terminal candlestick chart using block characters. Forming candle shown in cyan."""
    if not candles:
        return Text("(no candle data yet)", style="dim")

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    top, bottom = max(highs), min(lows)
    price_range = (top - bottom) or 1.0

    def row_for(price):
        ratio = (price - bottom) / price_range
        row = int(round((1 - ratio) * (height - 1)))
        return max(0, min(height - 1, row))

    grid = [[(" ", None) for _ in candles] for _ in range(height)]
    for i, c in enumerate(candles):
        is_forming = c.get("forming", False)
        if is_forming:
            color = "cyan"
        else:
            color = "green" if c["close"] >= c["open"] else "red"
        top_row, bot_row = row_for(c["high"]), row_for(c["low"])
        body_top, body_bot = row_for(max(c["open"], c["close"])), row_for(min(c["open"], c["close"]))
        for r in range(top_row, bot_row + 1):
            grid[r][i] = ("\u2502", color)
        for r in range(body_top, body_bot + 1):
            grid[r][i] = ("\u2588", color)

    lines = []
    for r in range(height):
        row_text = Text()
        for ch, color in grid[r]:
            if color:
                row_text.append(ch + " ", style=color)
            else:
                row_text.append("  ")
        lines.append(row_text)

    # Mark forming candle in axis
    forming_idx = len(candles) - 1 if candles and candles[-1].get("forming") else -1
    axis = Text(f"  high {top:.3f}" + " " * max(0, len(candles) * 2 - 20) + f"low {bottom:.3f}", style="dim")
    if forming_idx >= 0:
        lines.append(Text("  " + "  " * forming_idx + "\u25bc forming", style="cyan"))
    return Group(*lines, axis)


class Dashboard:
    def __init__(self, symbol, tf_key, point_size):
        self.symbol = symbol
        self.tf_key = tf_key
        self.point_size = point_size
        self.status_line = "Starting..."
        self.chart_candles = []
        self.ohlc = None
        self.candle_ts = "-"
        self.trades = []  # session trade history, most recent last
        self.events = []
        self.live_tick = None  # latest tick from MT5 (bid/ask/last/spread)

    def add_event(self, text):
        ts = datetime.now().strftime("%H:%M:%S")
        self.events.append(f"[{ts}] {text}")
        self.events = self.events[-6:]

    def render(self):
        header = Text.from_markup(
            f"[bold cyan]{self.symbol}[/bold cyan]  timeframe [bold]{self.tf_key}[/bold]  "
            f"point size {self.point_size}   TP {TP_POINTS}pts / SL {SL_POINTS}pts\n"
            f"{self.status_line}"
        )

        if self.live_tick:
            t = self.live_tick
            bid_color = "green" if t["bid"] >= (self.ohlc["close"] if self.ohlc else t["bid"]) else "red"
            ask_color = "green" if t["ask"] >= (self.ohlc["close"] if self.ohlc else t["ask"]) else "red"
            last_color = "green" if t["last"] >= (self.ohlc["close"] if self.ohlc else t["last"]) else "red"
            tick_time = t["time"].strftime("%H:%M:%S.") + f"{t['time'].microsecond // 1000:03d}"
            live_price_text = Text.from_markup(
                f"  BID [{bid_color} bold]{t['bid']:.5f}[/{bid_color} bold]   "
                f"ASK [{ask_color} bold]{t['ask']:.5f}[/{ask_color} bold]   "
                f"LAST [{last_color} bold]{t['last']:.5f}[/{last_color} bold]   "
                f"SPREAD [yellow]{t['spread']}[/yellow] pts "
                f"({t['spread_price']:.5f})   "
                f"[dim]@ {tick_time}[/dim]"
            )
        else:
            live_price_text = Text("  (waiting for tick data...)", style="dim")

        if self.ohlc:
            o, h, l, c = self.ohlc["open"], self.ohlc["high"], self.ohlc["low"], self.ohlc["close"]
            direction_color = "green" if c >= o else "red"
            ohlc_text = Text.from_markup(
                f"O [bold]{o:.3f}[/bold]   H [bold]{h:.3f}[/bold]   "
                f"L [bold]{l:.3f}[/bold]   C [{direction_color}]{c:.3f}[/{direction_color}]   "
                f"@ {self.candle_ts}"
            )
        else:
            ohlc_text = Text("(waiting for first closed candle)", style="dim")

        chart = render_ascii_chart(self.chart_candles)

        trades_table = Table(box=None, pad_edge=False, header_style="bold cyan")
        trades_table.add_column("Side")
        trades_table.add_column("Lot", justify="right")
        trades_table.add_column("Entry", justify="right")
        trades_table.add_column("Current", justify="right")
        trades_table.add_column("PnL/pt", justify="right")
        trades_table.add_column("PnL %", justify="right")
        trades_table.add_column("Status")

        if self.trades:
            for t in self.trades[-8:]:
                side = "LONG" if t["direction"] == "bull" else "SHORT"
                side_color = "green" if t["direction"] == "bull" else "red"
                pnl_pts, pnl_pct = pnl_for(t)
                pnl_color = "green" if pnl_pts >= 0 else "red"
                status = t["status"]
                trades_table.add_row(
                    f"[{side_color}]{side}[/{side_color}]",
                    f"{t.get('lot', 0.01):.2f}",
                    f"{t['entry_price']:.3f}",
                    f"{t['current_price']:.3f}",
                    f"[{pnl_color}]{pnl_pts:+.1f}[/{pnl_color}]",
                    f"[{pnl_color}]{pnl_pct:+.3f}%[/{pnl_color}]",
                    status,
                )
        else:
            trades_table.add_row("-", "-", "-", "-", "-", "-", "no trades yet")

        events_text = "\n".join(self.events) if self.events else "[dim](none yet)[/dim]"

        body = Group(
            header,
            Rule(style="dim"),
            Text.from_markup("[bold]LIVE PRICE[/bold]"),
            live_price_text,
            Rule(style="dim"),
            Text.from_markup("[bold]OHLC[/bold]"),
            ohlc_text,
            Rule(style="dim"),
            Text.from_markup(f"[bold]Chart[/bold] (last {len(self.chart_candles)} candles)"),
            chart,
            Rule(style="dim"),
            Text.from_markup("[bold]All trades this session[/bold]"),
            trades_table,
            Rule(style="dim"),
            Text.from_markup("[bold]Recent[/bold]\n" + events_text),
        )
        return Panel(body, title="Engulfing Watcher", border_style="cyan")


def wait_with_countdown(live, dash, seconds, label, tick_poller=None, candle_updater=None):
    """Wait with countdown, polling live ticks and updating chart with forming candle."""
    seconds = max(0, seconds)
    remaining = seconds
    poll_interval = 0.5
    while remaining > 0:
        dash.status_line = f"[yellow]{label}[/yellow]  ({int(remaining)}s left)"
        if tick_poller:
            tick = tick_poller()
            if tick:
                dash.live_tick = tick
        if candle_updater:
            forming = candle_updater()
            if forming:
                # Append forming candle to chart if not already there, or update last
                if dash.chart_candles and dash.chart_candles[-1].get("forming"):
                    dash.chart_candles[-1] = {**forming, "forming": True}
                elif dash.chart_candles:
                    dash.chart_candles.append({**forming, "forming": True})
                    # Keep only CHART_CANDLES + 1 (the forming one)
                    if len(dash.chart_candles) > CHART_CANDLES + 1:
                        dash.chart_candles = dash.chart_candles[-(CHART_CANDLES + 1):]
                # Update OHLC preview with forming candle close
                dash.ohlc = forming
                dash.candle_ts = "forming"
        live.update(dash.render())
        step = min(poll_interval, remaining)
        time.sleep(step)
        remaining -= step
    dash.status_line = "[green]Checking candle...[/green]"
    if tick_poller:
        tick = tick_poller()
        if tick:
            dash.live_tick = tick
    live.update(dash.render())


def seconds_until_next_close(tf_seconds):
    now = datetime.now()
    epoch = now.timestamp()
    next_boundary = (int(epoch) // tf_seconds + 1) * tf_seconds
    wait_seconds = next_boundary - epoch
    return wait_seconds, datetime.fromtimestamp(next_boundary)


def handle_candle_close(live, dash, symbol, history, ts, active_trade, point_size,
                         mt5_instance=None, lot=0.01):
    curr = history[-1]
    prev = history[-2]

    dash.ohlc = curr
    dash.candle_ts = ts
    dash.chart_candles = history[-CHART_CANDLES:]

    # update PnL of the active trade to this candle's close, live
    if active_trade is not None:
        active_trade["current_price"] = curr["close"]

        outcome, ambiguous = check_trade(active_trade, curr)
        if outcome:
            exit_price = active_trade["tp_price"] if outcome == "tp" else active_trade["sl_price"]
            status = "TP_HIT" if outcome == "tp" else "SL_HIT"
            active_trade["status"] = status
            active_trade["current_price"] = exit_price
            active_trade["tp_status"] = "hit" if outcome == "tp" else "Canceled by SL"
            active_trade["sl_status"] = "hit" if outcome == "sl" else "Canceled by TP"
            active_trade["ambiguous"] = ambiguous

            # close the real MT5 position
            if mt5_instance and active_trade.get("ticket"):
                close_ticket, close_price = mt5_close_position(
                    mt5_instance, symbol, active_trade["direction"],
                    active_trade["ticket"], active_trade["lot"])
                if close_ticket:
                    active_trade["current_price"] = close_price
                    dash.add_event(f"[green]MT5 position closed[/green] ticket={close_ticket} "
                                   f"price={close_price:.5f}")
                else:
                    dash.add_event(f"[red]MT5 close failed: {close_price}[/red]")

            update_signal_json_status(active_trade["entry_time"], active_trade["tp_status"],
                                       active_trade["sl_status"], status, ambiguous)
            png_path = export_trade_result_chart(active_trade, history)

            label = "TP HIT" if outcome == "tp" else "SL HIT"
            amb_note = " (ambiguous candle - counted as SL)" if ambiguous else ""
            dash.add_event(f"[bold]{label}[/bold]{amb_note} {symbol} {active_trade['direction']} "
                            f"@ {exit_price:.3f} -> {png_path}")
            active_trade = None

    # pattern detection
    if is_bullish_engulfing(prev, curr):
        result = "bull"
    elif is_bearish_engulfing(prev, curr):
        result = "bear"
    else:
        result = None

    if result is not None:
        if active_trade is None:
            entry_price = curr["close"]
            active_trade = open_trade(symbol, result, entry_price, curr["time"], point_size, lot)

            # place real MT5 order
            if mt5_instance:
                mt5_ticket, mt5_price = mt5_open_position(
                    mt5_instance, symbol, result, lot,
                    active_trade["tp_price"], active_trade["sl_price"])
                if mt5_ticket:
                    active_trade["ticket"] = mt5_ticket
                    active_trade["entry_price"] = mt5_price
                    dash.add_event(f"[green]MT5 order filled[/green] ticket={mt5_ticket} "
                                   f"price={mt5_price:.5f}")
                else:
                    dash.add_event(f"[red]MT5 order FAILED: {mt5_price}[/red]")

            json_path = save_signal_json(symbol, result, prev, curr, ts, active_trade)
            export_chart_png(symbol, result, history, ts)
            dash.trades.append(active_trade)
            label = "LONG" if result == "bull" else "SHORT"
            dash.add_event(f"New [bold]{label}[/bold] trade ({lot} lot): entry {active_trade['entry_price']:.3f} "
                            f"TP {active_trade['tp_price']:.3f} SL {active_trade['sl_price']:.3f}")
        else:
            dash.add_event(f"{'Bullish' if result == 'bull' else 'Bearish'} pattern seen - "
                            f"not followed (trade already active)")

    live.update(dash.render())
    return active_trade


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
    lot = float(Prompt.ask("Lot size (e.g. 0.01, 0.1, 1.0)", default="0.01"))

    mt5 = connect_live(login, password, server)

    if not mt5.symbol_select(symbol, True):
        mt5.shutdown()
        console.print(f"[bold red]Symbol '{symbol}' not found or not visible in Market Watch.[/bold red]")
        raise SystemExit(1)

    info = mt5.symbol_info(symbol)
    point_size = info.point if info and info.point else 0.01

    dash = Dashboard(symbol, tf_key, point_size)
    active_trade = None

    try:
        with Live(dash.render(), console=console, refresh_per_second=10, screen=False) as live:
            while True:
                wait_s, next_close = seconds_until_next_close(tf_seconds)
                wait_with_countdown(live, dash, wait_s + 2,
                                     f"Next {tf_key} candle closes at {next_close.strftime('%H:%M:%S')}",
                                     tick_poller=lambda: get_live_tick(mt5, symbol),
                                     candle_updater=lambda: get_forming_candle(mt5, symbol, tf_key))
                history = get_live_closed_candles(mt5, symbol, tf_key, n_closed=CHART_CANDLES)
                if not history or len(history) < 2:
                    dash.add_event("[yellow]Not enough candle data, skipping[/yellow]")
                    live.update(dash.render())
                    continue
                active_trade = handle_candle_close(live, dash, symbol, history, history[-1]["time"],
                                                    active_trade, point_size,
                                                    mt5_instance=mt5, lot=lot)
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
    dash = Dashboard(symbol, tf_key, point_size)
    active_trade = None

    try:
        with Live(dash.render(), console=console, refresh_per_second=4, screen=False) as live:
            while True:
                wait_with_countdown(live, dash, interval, "Generating next simulated candle")
                sim.next_candle()
                history = sim.last_n(CHART_CANDLES)
                if not history or len(history) < 2:
                    continue
                ts = history[-1]["time"].strftime("%H:%M:%S")
                active_trade = handle_candle_close(live, dash, symbol, history, ts, active_trade, point_size)
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