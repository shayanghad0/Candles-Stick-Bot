"""
Engulfing Candle Watcher for MetaTrader 5 (live chart + real trades)
--------------------------------------------------------------------
LIVE MODE:
  - Connects to your MT5 account
  - Polls real-time ticks every ~200ms (sub-second)
  - Opens a matplotlib live candlestick chart window
  - Detects bullish/bearish engulfing on candle close
  - Opens and closes REAL MT5 orders automatically
  - Console dashboard shows live bid/ask, open position, PnL
  - TP/SL enforced by MT5 server (pending orders)

SIMULATE MODE:
  - No MT5 login. Synthetic candles for testing the pattern detector.

Requirements:
    pip install rich MetaTrader5 pandas matplotlib

WARNING: Live mode places REAL trades on your account.
         Use a demo account first.
"""

import glob
import json
import multiprocessing
import os
import random
import sys
import time
from collections import deque
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

TP_POINTS = 180
SL_POINTS = 150
LOT_SIZE = 0.01

CHART_CANDLES = 20
CHART_HEIGHT = 14
TICK_POLL_MS = 200  # sub-second tick polling


# --------------------------------------------------------------------------
# Terminal auto-detection
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
        candidates += glob.glob(
            os.path.join(appdata, "MetaQuotes", "Terminal", "*", "terminal64.exe")
        )
    return candidates[0] if candidates else None


def connect_live(login, password, server, retries=3):
    import MetaTrader5 as mt5

    with console.status(
        "[cyan]Auto-detecting your MT5 terminal installation...", spinner="dots"
    ):
        path = find_terminal_path()

    path_line = (
        f"[green]Found:[/green] {path}"
        if path
        else "[yellow]Not found - using default launch[/yellow]"
    )

    last_error = None
    with console.status("", spinner="dots") as status:
        for attempt in range(1, retries + 1):
            status.update(
                f"[cyan]Connecting (attempt {attempt}/{retries})...[/cyan]  {path_line}"
            )
            mt5.shutdown()
            time.sleep(1)

            ok_init = mt5.initialize(path=path) if path else mt5.initialize()
            if not ok_init:
                last_error = mt5.last_error()
                continue

            if mt5.login(login, password=password, server=server):
                console.print(
                    Panel(
                        f"{path_line}\n[bold green]Connected[/bold green] - account {login} @ {server}",
                        title="MT5 Auto-Terminal",
                        border_style="green",
                    )
                )
                return mt5

            last_error = mt5.last_error()

    mt5.shutdown()
    console.print(
        Panel(
            f"[bold red]Could not connect after {retries} attempts.[/bold red]\n"
            f"Last error: {last_error}\n\n"
            "Checklist:\n"
            "  1. Fully quit MT5 (Task Manager) and retry.\n"
            "  2. Log in manually with same credentials first.\n"
            "  3. Use TRADE password, not investor password.\n"
            "  4. Server name must match exactly.",
            border_style="red",
            title="Connection failed",
        )
    )
    raise SystemExit(1)


# --------------------------------------------------------------------------
# MT5 trade execution (real trades)
# --------------------------------------------------------------------------

def mt5_open_order(mt5, symbol, direction, entry_price, tp_price, sl_price, lot):
    import MetaTrader5 as mt5mod

    order_type = mt5mod.ORDER_TYPE_BUY if direction == "bull" else mt5mod.ORDER_TYPE_SELL
    price_info = mt5.symbol_info_tick(symbol)
    if not price_info:
        return None, "Cannot get tick"

    if direction == "bull":
        price = price_info.ask
    else:
        price = price_info.bid

    request = {
        "action": mt5mod.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "tp": tp_price,
        "sl": sl_price,
        "deviation": 20,
        "magic": 123456,
        "comment": f"ENG_{direction}",
        "type_time": mt5mod.ORDER_TIME_GTC,
        "type_filling": mt5mod.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result is None:
        return None, f"order_send returned None: {mt5.last_error()}"
    if result.retcode != mt5mod.TRADE_RETCODE_DONE:
        return None, f"Error {result.retcode}: {result.comment}"
    return result, None


def mt5_close_position(mt5, symbol, magic=123456):
    import MetaTrader5 as mt5mod

    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return None, "No position"

    for pos in positions:
        if pos.magic != magic:
            continue
        if pos.type == mt5mod.ORDER_TYPE_BUY:
            close_type = mt5mod.ORDER_TYPE_SELL
            price = mt5.symbol_info_tick(symbol).bid
        else:
            close_type = mt5mod.ORDER_TYPE_BUY
            price = mt5.symbol_info_tick(symbol).ask

        request = {
            "action": mt5mod.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": pos.ticket,
            "price": price,
            "deviation": 20,
            "magic": magic,
            "comment": "ENG_close",
            "type_time": mt5mod.ORDER_TIME_GTC,
            "type_filling": mt5mod.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None:
            return None, f"order_send returned None: {mt5.last_error()}"
        if result.retcode != mt5mod.TRADE_RETCODE_DONE:
            return None, f"Error {result.retcode}: {result.comment}"
        return result, None

    return None, "No matching position"


def mt5_get_position(mt5, symbol, magic=123456):
    import MetaTrader5 as mt5mod

    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return None
    for pos in positions:
        if pos.magic == magic:
            return pos
    return None


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
# JSON logging
# --------------------------------------------------------------------------

def save_signal_json(symbol, pattern, prev, curr, ts, entry_price, tp_price, sl_price):
    os.makedirs(SIGNALS_DIR, exist_ok=True)
    entry = {
        "symbol": symbol,
        "pattern": "bullish_engulfing" if pattern == "bull" else "bearish_engulfing",
        "time": _ts_str(ts),
        "prev_open": prev["open"], "prev_close": prev["close"],
        "curr_open": curr["open"], "curr_close": curr["close"],
        "entry_price": entry_price, "tp_price": tp_price, "sl_price": sl_price,
        "status": "open",
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


def update_signal_json_status(entry_time, status):
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
            entry["status"] = status
            break
    with open(JSON_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


# --------------------------------------------------------------------------
# Live chart (matplotlib, separate process so TkAgg works)
# --------------------------------------------------------------------------

def _chart_process(queue, stop_event):
    """Runs in a child process with its own main thread."""
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    plt.ion()
    fig, ax = plt.subplots(figsize=(12, 6))
    fig.suptitle("Live Engulfing Chart", fontsize=14, fontweight="bold")

    candles = []
    bid = ask = entry = tp = sl = 0.0

    while not stop_event.is_set():
        # drain all pending messages, keep only latest
        while not queue.empty():
            msg = queue.get_nowait()
            if msg[0] == "candles":
                candles = msg[1]
            elif msg[0] == "price":
                bid, ask = msg[1], msg[2]
            elif msg[0] == "levels":
                entry, tp, sl = msg[1], msg[2], msg[3]

        try:
            ax.clear()
            if not candles:
                ax.set_title("Waiting for data...")
                fig.canvas.draw_idle()
                fig.canvas.flush_events()
                plt.pause(0.3)
                continue

            for i, c in enumerate(candles):
                color = "#2ecc71" if c["close"] >= c["open"] else "#e74c3c"
                body_low = min(c["open"], c["close"])
                body_high = max(c["open"], c["close"])
                body_h = max(body_high - body_low, 0.001)
                ax.add_patch(Rectangle(
                    (i - 0.35, body_low), 0.7, body_h,
                    color=color, zorder=3, alpha=0.9,
                ))
                ax.plot([i, i], [c["low"], c["high"]],
                        color=color, linewidth=1.2, zorder=2)

            if entry:
                ax.axhline(entry, color="dodgerblue", linestyle="--",
                           linewidth=1, label="Entry", alpha=0.7)
            if tp:
                ax.axhline(tp, color="#2ecc71", linestyle="-",
                           linewidth=1, label="TP", alpha=0.7)
            if sl:
                ax.axhline(sl, color="#e74c3c", linestyle="-",
                           linewidth=1, label="SL", alpha=0.7)
            if bid:
                ax.axhline(bid, color="cyan", linestyle=":",
                           linewidth=0.8, alpha=0.6)
            if ask:
                ax.axhline(ask, color="magenta", linestyle=":",
                           linewidth=0.8, alpha=0.6)

            labels = []
            for c in candles:
                t = c.get("time", "")
                labels.append(t.strftime("%H:%M") if hasattr(t, "strftime") else str(t)[-8:])
            ax.set_xticks(range(len(candles)))
            ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
            ax.set_ylabel("Price", fontsize=10)
            title = ""
            if bid:
                title += f"Bid: {bid:.5f}   "
            if ask:
                title += f"Ask: {ask:.5f}"
            ax.set_title(title, fontsize=11)
            if entry or tp or sl:
                ax.legend(loc="upper left", fontsize=8)
            ax.margins(x=0.05)
            fig.tight_layout()
            fig.canvas.draw_idle()
            fig.canvas.flush_events()
            plt.pause(0.2)

        except Exception:
            plt.pause(0.5)

    plt.ioff()
    plt.close(fig)


class LiveChart:
    def __init__(self):
        self._queue = None
        self._process = None
        self._stop = None

    def start(self):
        self._queue = multiprocessing.Queue()
        self._stop = multiprocessing.Event()
        self._process = multiprocessing.Process(
            target=_chart_process, args=(self._queue, self._stop), daemon=True,
        )
        self._process.start()

    def stop(self):
        if self._stop:
            self._stop.set()
        if self._process:
            self._process.join(timeout=3)
            if self._process.is_alive():
                self._process.terminate()

    def update_candles(self, candles):
        if self._queue:
            try:
                safe = []
                for c in candles:
                    sc = dict(c)
                    if "time" in sc and hasattr(sc["time"], "strftime"):
                        sc["time"] = sc["time"].strftime("%H:%M:%S")
                    safe.append(sc)
                self._queue.put_nowait(("candles", safe))
            except Exception:
                pass

    def update_price(self, bid, ask):
        if self._queue:
            try:
                self._queue.put_nowait(("price", bid, ask))
            except Exception:
                pass

    def set_levels(self, entry=None, tp=None, sl=None):
        if self._queue:
            try:
                self._queue.put_nowait(("levels", entry or 0, tp or 0, sl or 0))
            except Exception:
                pass


# --------------------------------------------------------------------------
# Console dashboard
# --------------------------------------------------------------------------

def render_ascii_chart(candles, height=CHART_HEIGHT):
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

    axis = Text(
        f"  high {top:.5f}" + " " * max(0, len(candles) * 2 - 24) + f"low {bottom:.5f}",
        style="dim",
    )
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
        self.bid = 0.0
        self.ask = 0.0
        self.spread = 0.0
        self.position = None  # MT5 position object
        self.trades = []
        self.events = []
        self.trade_count = 0
        self.win_count = 0
        self.loss_count = 0

    def add_event(self, text):
        ts = datetime.now().strftime("%H:%M:%S")
        self.events.append(f"[{ts}] {text}")
        self.events = self.events[-8:]

    def render(self):
        # Live price section
        bid_color = "green" if self.bid else "dim"
        ask_color = "red" if self.ask else "dim"
        spread_pts = (self.spread / self.point_size) if self.point_size and self.spread else 0

        price_line = Text()
        price_line.append(f"{self.symbol}  ", style="bold cyan")
        price_line.append(f"{self.tf_key}  ", style="bold")
        price_line.append(f"point={self.point_size}  TP={TP_POINTS}pts SL={SL_POINTS}pts lot={LOT_SIZE}\n")
        price_line.append("BID ", style="bold")
        price_line.append(f"{self.bid:.5f}   ", style=f"bold {bid_color}")
        price_line.append("ASK ", style="bold")
        price_line.append(f"{self.ask:.5f}   ", style=f"bold {ask_color}")
        price_line.append("Spread ")
        price_line.append(f"{spread_pts:.1f}pts", style="yellow")

        # Status
        status = Text.from_markup(self.status_line)

        # OHLC
        if self.ohlc:
            o, h, l, c = self.ohlc["open"], self.ohlc["high"], self.ohlc["low"], self.ohlc["close"]
            dc = "green" if c >= o else "red"
            ohlc_text = Text()
            ohlc_text.append("O "); ohlc_text.append(f"{o:.5f}  ", style="bold")
            ohlc_text.append("H "); ohlc_text.append(f"{h:.5f}  ", style="bold")
            ohlc_text.append("L "); ohlc_text.append(f"{l:.5f}  ", style="bold")
            ohlc_text.append("C "); ohlc_text.append(f"{c:.5f}  ", style=f"bold {dc}")
            ohlc_text.append(f"@ {self.candle_ts}")
        else:
            ohlc_text = Text("(waiting for first candle close)", style="dim")

        chart = render_ascii_chart(self.chart_candles)

        # Position table
        pos_table = Table(box=None, pad_edge=False, header_style="bold cyan")
        pos_table.add_column("Side")
        pos_table.add_column("Entry", justify="right")
        pos_table.add_column("Current", justify="right")
        pos_table.add_column("TP", justify="right")
        pos_table.add_column("SL", justify="right")
        pos_table.add_column("PnL pts", justify="right")
        pos_table.add_column("Status")

        if self.position:
            p = self.position
            side = "LONG" if p.type == 0 else "SHORT"
            sc = "green" if p.type == 0 else "red"
            pnl_pts = p.profit / max(p.volume, 0.01) * (1.0 / max(self.point_size, 0.0001))
            pnl_color = "green" if p.profit >= 0 else "red"
            pos_table.add_row(
                f"[{sc}]{side}[/{sc}]",
                f"{p.price_open:.5f}",
                f"{self.bid:.5f}" if p.type == 0 else f"{self.ask:.5f}",
                f"[green]{p.tp:.5f}[/green]" if p.tp else "-",
                f"[red]{p.sl:.5f}[/red]" if p.sl else "-",
                f"[{pnl_color}]{pnl_pts:+.1f}[/{pnl_color}]",
                f"[green]OPEN[/green]",
            )
        else:
            pos_table.add_row("-", "-", "-", "-", "-", "-", "[dim]no position[/dim]")

        # Stats
        total = self.win_count + self.loss_count
        wr = (self.win_count / total * 100) if total else 0
        stats = Text.from_markup(
            f"Trades: [bold]{self.trade_count}[/bold]  "
            f"Wins: [green]{self.win_count}[/green]  "
            f"Losses: [red]{self.loss_count}[/red]  "
            f"WR: [bold]{wr:.0f}%[/bold]"
        )

        events_text = "\n".join(self.events) if self.events else "[dim](none yet)[/dim]"

        body = Group(
            price_line,
            Rule(style="dim"),
            status,
            Rule(style="dim"),
            Text.from_markup("[bold]OHLC[/bold]"),
            ohlc_text,
            Rule(style="dim"),
            Text.from_markup(f"[bold]Chart[/bold] (last {len(self.chart_candles)} candles)"),
            chart,
            Rule(style="dim"),
            Text.from_markup("[bold]Position[/bold]"),
            pos_table,
            stats,
            Rule(style="dim"),
            Text.from_markup("[bold]Recent[/bold]\n" + events_text),
        )
        return Panel(body, title="Engulfing Watcher - LIVE", border_style="cyan")


# --------------------------------------------------------------------------
# Tick-to-candle builder (sub-second)
# --------------------------------------------------------------------------

class TickCandleBuilder:
    def __init__(self, tf_seconds):
        self.tf_seconds = tf_seconds
        self.candles = []
        self.current = None

    def add_tick(self, bid, ask, ts):
        mid = (bid + ask) / 2.0
        epoch = int(ts.timestamp())
        boundary = (epoch // self.tf_seconds) * self.tf_seconds

        if self.current is None or self.current["time_epoch"] != boundary:
            # new candle
            if self.current is not None:
                self.candles.append(self.current)
            self.current = {
                "time": ts,
                "time_epoch": boundary,
                "open": mid,
                "high": mid,
                "low": mid,
                "close": mid,
            }
        else:
            self.current["high"] = max(self.current["high"], mid)
            self.current["low"] = min(self.current["low"], mid)
            self.current["close"] = mid
            self.current["time"] = ts

    def get_closed(self):
        """Return list of closed candles (excluding the forming one)."""
        if len(self.candles) < 1:
            return []
        return self.candles[:-1] if self.current else self.candles

    def get_all(self):
        """Return closed + forming candle."""
        result = list(self.candles)
        if self.current:
            result.append(self.current)
        return result

    def get_last_closed(self):
        if len(self.candles) >= 2:
            return self.candles[-2]
        return None

    def on_new_boundary(self):
        """Check if a new candle boundary was just crossed."""
        if len(self.candles) < 2:
            return False
        return True


# --------------------------------------------------------------------------
# Live mode
# --------------------------------------------------------------------------

def run_live():
    import MetaTrader5 as mt5

    login = IntPrompt.ask("Account ID (login)")
    password = Prompt.ask("Password", password=False)
    server = Prompt.ask("Server")
    symbol = Prompt.ask("Symbol", default="EURUSD").strip().upper()
    tf_key = Prompt.ask("Timeframe", choices=list(TIMEFRAMES.keys()), default="M1")
    tf_seconds = TIMEFRAMES[tf_key]
    lot = float(Prompt.ask("Lot size", default=str(LOT_SIZE)))

    mt5_conn = connect_live(login, password, server)

    if not mt5_conn.symbol_select(symbol, True):
        mt5_conn.shutdown()
        console.print(f"[bold red]Symbol '{symbol}' not found in Market Watch.[/bold red]")
        raise SystemExit(1)

    info = mt5_conn.symbol_info(symbol)
    point_size = info.point if info and info.point else 0.01

    dash = Dashboard(symbol, tf_key, point_size)
    builder = TickCandleBuilder(tf_seconds)
    chart = LiveChart()

    # state
    last_candle_count = 0
    active_position = None
    last_tick_time = time.time()
    check_interval = TICK_POLL_MS / 1000.0

    # start live chart
    chart.start()

    try:
        with Live(dash.render(), console=console, refresh_per_second=10, screen=False) as live:
            while True:
                # poll tick from MT5 (sub-second)
                tick = mt5_conn.symbol_info_tick(symbol)
                if tick is None:
                    time.sleep(0.1)
                    continue

                now = datetime.fromtimestamp(tick.time)
                bid = tick.bid
                ask = tick.ask

                # update dashboard price
                dash.bid = bid
                dash.ask = ask
                dash.spread = ask - bid

                # feed tick into candle builder
                builder.add_tick(bid, ask, now)
                all_candles = builder.get_all()

                # update chart
                chart.update_candles(all_candles)
                chart.update_price(bid, ask)

                # update position info
                pos = mt5_get_position(mt5_conn, symbol)
                dash.position = pos
                if pos:
                    chart.set_levels(pos.price_open, pos.tp, pos.sl)
                else:
                    chart.set_levels()

                # check for new candle close (under 1 second)
                closed = builder.get_closed()
                if len(closed) >= 2 and len(closed) > last_candle_count:
                    # a new candle just closed
                    last_candle_count = len(closed)
                    prev = closed[-2]
                    curr = closed[-1]

                    dash.ohlc = curr
                    dash.candle_ts = curr["time"].strftime("%H:%M:%S")
                    dash.chart_candles = closed[-CHART_CANDLES:]

                    # check TP/SL hit on the closed candle
                    if active_position:
                        if active_position.type == 0:  # BUY
                            if curr["high"] >= active_position.tp and active_position.tp > 0:
                                # TP hit - close via MT5
                                res, err = mt5_close_position(mt5_conn, symbol)
                                if not err:
                                    dash.add_event("[bold green]TP HIT[/bold green] - closed via MT5")
                                    dash.win_count += 1
                                    dash.trade_count += 1
                                    update_signal_json_status(active_position.time, "TP_HIT")
                                    active_position = None
                                else:
                                    dash.add_event(f"[red]TP close error: {err}[/red]")
                            elif curr["low"] <= active_position.sl and active_position.sl > 0:
                                res, err = mt5_close_position(mt5_conn, symbol)
                                if not err:
                                    dash.add_event("[bold red]SL HIT[/bold red] - closed via MT5")
                                    dash.loss_count += 1
                                    dash.trade_count += 1
                                    update_signal_json_status(active_position.time, "SL_HIT")
                                    active_position = None
                                else:
                                    dash.add_event(f"[red]SL close error: {err}[/red]")
                        else:  # SELL
                            if curr["low"] <= active_position.tp and active_position.tp > 0:
                                res, err = mt5_close_position(mt5_conn, symbol)
                                if not err:
                                    dash.add_event("[bold green]TP HIT[/bold green] - closed via MT5")
                                    dash.win_count += 1
                                    dash.trade_count += 1
                                    update_signal_json_status(active_position.time, "TP_HIT")
                                    active_position = None
                                else:
                                    dash.add_event(f"[red]TP close error: {err}[/red]")
                            elif curr["high"] >= active_position.sl and active_position.sl > 0:
                                res, err = mt5_close_position(mt5_conn, symbol)
                                if not err:
                                    dash.add_event("[bold red]SL HIT[/bold red] - closed via MT5")
                                    dash.loss_count += 1
                                    dash.trade_count += 1
                                    update_signal_json_status(active_position.time, "SL_HIT")
                                    active_position = None
                                else:
                                    dash.add_event(f"[red]SL close error: {err}[/red]")

                    # pattern detection
                    if is_bullish_engulfing(prev, curr):
                        result = "bull"
                    elif is_bearish_engulfing(prev, curr):
                        result = "bear"
                    else:
                        result = None

                    if result is not None:
                        if active_position is None:
                            entry_price = curr["close"]
                            if result == "bull":
                                tp_price = entry_price + TP_POINTS * point_size
                                sl_price = entry_price - SL_POINTS * point_size
                            else:
                                tp_price = entry_price - TP_POINTS * point_size
                                sl_price = entry_price + SL_POINTS * point_size

                            # open real MT5 order
                            res, err = mt5_open_order(
                                mt5_conn, symbol, result,
                                entry_price, tp_price, sl_price, lot
                            )
                            if err:
                                dash.add_event(f"[red]Order error: {err}[/red]")
                            else:
                                active_position = mt5_get_position(mt5_conn, symbol)
                                save_signal_json(symbol, result, prev, curr, now,
                                                 entry_price, tp_price, sl_price)
                                label = "LONG" if result == "bull" else "SHORT"
                                dash.add_event(
                                    f"New [bold]{label}[/bold] trade: entry {entry_price:.5f} "
                                    f"TP {tp_price:.5f} SL {sl_price:.5f}"
                                )
                        else:
                            dash.add_event(
                                f"{'Bullish' if result == 'bull' else 'Bearish'} pattern - "
                                f"skipped (position active)"
                            )

                    live.update(dash.render())

                # update status line
                remaining_tf = tf_seconds - (int(now.timestamp()) % tf_seconds)
                dash.status_line = (
                    f"[yellow]Next candle in {remaining_tf}s[/yellow]  "
                    f"  Tick poll: {TICK_POLL_MS}ms"
                )
                live.update(dash.render())

                # sub-second sleep
                time.sleep(check_interval)

    except KeyboardInterrupt:
        console.print("\n[bold]Stopped by user.[/bold]")
    finally:
        chart.stop()
        mt5_conn.shutdown()


# --------------------------------------------------------------------------
# Simulate mode
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

        candle = {
            "time": datetime.now(), "open": open_p,
            "high": high_p, "low": low_p, "close": close_p,
        }
        self.candles.append(candle)
        self.last_close = close_p
        return candle

    def last_n(self, n=CHART_CANDLES):
        return self.candles[-n:] if len(self.candles) >= 2 else None


def run_simulate():
    symbol = Prompt.ask("Symbol (simulated)", default="XAUUSD").strip().upper()
    tf_key = Prompt.ask("Timeframe label", choices=list(TIMEFRAMES.keys()), default="M1")
    interval = IntPrompt.ask("Seconds between simulated candles", default=3)
    point_size = float(Prompt.ask("Point size", default="0.01"))

    sim = CandleSimulator(symbol)
    dash = Dashboard(symbol, tf_key, point_size)
    chart = LiveChart()
    chart.start()

    try:
        with Live(dash.render(), console=console, refresh_per_second=4, screen=False) as live:
            while True:
                time.sleep(interval)
                sim.next_candle()
                history = sim.last_n(CHART_CANDLES)
                if not history or len(history) < 2:
                    continue

                curr = history[-1]
                prev = history[-2]

                dash.ohlc = curr
                dash.candle_ts = curr["time"].strftime("%H:%M:%S")
                dash.chart_candles = history[-CHART_CANDLES:]
                chart.update_candles(history)
                chart.update_price(curr["close"], curr["close"] + point_size * 5)

                if is_bullish_engulfing(prev, curr):
                    label = "LONG"
                    dash.add_event(f"New [bold]{label}[/bold] signal @ {curr['close']:.5f}")
                elif is_bearish_engulfing(prev, curr):
                    label = "SHORT"
                    dash.add_event(f"New [bold]{label}[/bold] signal @ {curr['close']:.5f}")

                dash.status_line = "[green]Simulating...[/green]"
                live.update(dash.render())
    except KeyboardInterrupt:
        console.print("\n[bold]Simulation stopped.[/bold]")
    finally:
        chart.stop()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    console.print(Panel.fit(
        "[bold cyan]Engulfing Candle Watcher[/bold cyan]\n"
        "Live MT5 (real trades + live chart) or simulation mode.",
        border_style="cyan",
    ))
    mode = Prompt.ask("Mode", choices=["live", "simulate"], default="simulate")
    if mode == "live":
        run_live()
    else:
        run_simulate()


if __name__ == "__main__":
    main()
