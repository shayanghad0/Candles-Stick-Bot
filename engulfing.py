"""
Engulfing Candle Watcher for MetaTrader 5 - REAL orders + live dashboard
--------------------------------------------------------------------------
WARNING: In "live" mode this now sends REAL market orders to your MT5
account (buy/sell with attached TP/SL), unlike earlier versions of
this script which only simulated trades on paper. Test on a demo
account first. This is not financial advice and past signals do not
guarantee future results.

Two modes:
  1. Live  - opens MT5, sends a real market order when a pattern
             fires (if no position is already open), polls the real
             position status every <1s, and closes out the trade
             record the moment MT5 shows the position has actually
             closed (via its own attached TP/SL, or manually/stop-out).
  2. Simulate - no MT5 login, no real orders. Simulates a live tick
             feed and candle formation with paper trades, for testing
             the detection/UI without touching any account.

Auto-terminal detection: searches common install locations for
terminal64.exe and connects through that exact path, avoiding
"stuck session" auth errors from a stale terminal.

------------------------------- BUGFIXES ----------------------------------
1) "After TP/SL hits, it can't find the trade" - the old close logic
   used TRADE_ACTION_CLOSE_BY, which only works on hedging accounts
   with TWO opposite open positions (it closes one position "by"
   another). This bot only ever opens ONE position, so that action
   was invalid and would fail. Worse: since TP/SL were already
   attached to the order, the broker's server closes the position
   automatically the moment price touches either level - by the time
   the bot got around to trying to close it "manually", positions_get()
   already returned empty, which is exactly the "can't find the
   trade" symptom. FIX: stop trying to close positions manually.
   Instead, poll positions_get(ticket=...) every tick; once the
   position is gone, read the exact close reason and price from
   history_deals_get(position=ticket) - MT5 tags that deal with
   DEAL_REASON_SL or DEAL_REASON_TP directly, so there's no guessing.

2) Buy/sell TP/SL inconsistency - TP/SL used to be computed from the
   candle's close price, then the real order filled at the live
   ask/bid (which differs due to spread), leaving stops slightly
   mismatched from the real entry. FIX: TP/SL are now computed from
   the exact same tick price used to send the order.

3) Order reliability - added volume normalization to the symbol's
   min/max/step, a spread guard (skip the trade if spread is too wide
   relative to TP/SL), and retries across FOK/IOC/RETURN filling modes
   since brokers differ in which one they accept.

4) Strategy precision - added a minimum engulfing body size filter so
   tiny/noise candles don't count as a signal.

Requirements:
    pip install rich MetaTrader5 pandas matplotlib

This script is for educational / technical-analysis purposes only and
is not financial advice. Live mode places real orders with real money.
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
from rich.prompt import Prompt, IntPrompt, Confirm
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

# --- strategy precision filters ---
MIN_BODY_POINTS = 40         # ignore engulfing candles whose body is smaller than this (noise filter)
MAX_SPREAD_POINTS = 50       # skip entries if the live spread is wider than this many points

CHART_CANDLES = 15
CHART_HEIGHT = 12
TICK_INTERVAL = 0.5           # seconds between live checks - under 1s


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
            status.update(f"[cyan]Opening MT5 (attempt {attempt}/{retries})...[/cyan]  {path_line}")
            mt5.shutdown()
            time.sleep(1)

            ok_init = mt5.initialize(path=path) if path else mt5.initialize()
            if not ok_init:
                last_error = mt5.last_error()
                continue

            if mt5.login(login, password=password, server=server):
                console.print(Panel(
                    f"{path_line}\n[bold green]MT5 OPENED[/bold green] - account {login} @ {server}",
                    title="MT5 Auto-Terminal", border_style="green",
                ))
                return mt5

            last_error = mt5.last_error()

    mt5.shutdown()
    console.print(Panel(
        f"[bold red]Could not open MT5 after {retries} attempts.[/bold red]\n"
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


def close_live(mt5):
    mt5.shutdown()
    console.print(Panel("[bold red]MT5 CLOSED[/bold red]", title="MT5 Auto-Terminal", border_style="red"))


def get_live_closed_candles(mt5, symbol, tf_key, n_closed=CHART_CANDLES):
    import pandas as pd
    tf_const = getattr(mt5, f"TIMEFRAME_{tf_key}")
    rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, n_closed + 1)
    if rates is None or len(rates) < 2:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df.to_dict("records")[:-1]


def get_live_tick(mt5, symbol):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    info = mt5.symbol_info(symbol)
    digits = info.digits if info else 5
    point = info.point if info else 0.00001
    spread_pts = round((tick.ask - tick.bid) / point) if point else 0
    return {
        "bid": tick.bid, "ask": tick.ask,
        "last": tick.last if tick.last else tick.bid,
        "spread_points": spread_pts,
        "spread_price": round(tick.ask - tick.bid, digits),
        "time": datetime.fromtimestamp(tick.time),
    }


# --------------------------------------------------------------------------
# Candle simulator (paper trading only, no MT5 needed)
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
# Pattern detection (with a minimum body filter for precision)
# --------------------------------------------------------------------------

def _body_points(candle, point_size):
    return abs(candle["close"] - candle["open"]) / point_size if point_size else 0


def is_bullish_engulfing(prev, curr, point_size=None):
    prev_bearish = prev["close"] < prev["open"]
    curr_bullish = curr["close"] > curr["open"]
    engulfs = curr["open"] <= prev["close"] and curr["close"] >= prev["open"]
    if point_size and _body_points(curr, point_size) < MIN_BODY_POINTS:
        return False
    return prev_bearish and curr_bullish and engulfs


def is_bearish_engulfing(prev, curr, point_size=None):
    prev_bullish = prev["close"] > prev["open"]
    curr_bearish = curr["close"] < curr["open"]
    engulfs = curr["open"] >= prev["close"] and curr["close"] <= prev["open"]
    if point_size and _body_points(curr, point_size) < MIN_BODY_POINTS:
        return False
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
        "lot": trade.get("lot"), "ticket": trade.get("ticket"),
        "status": "open", "tp_status": None, "sl_status": None,
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


def update_signal_json_status(entry_time, tp_status, sl_status, status, exit_price=None, profit=None):
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
            if exit_price is not None:
                entry["exit_price"] = exit_price
            if profit is not None:
                entry["profit"] = profit
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
    candles = history[-10:] if history else []
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
    outcome_color = "#2ecc71" if win else "#e74c3c"
    ax.set_title(f"{trade_result['symbol']} {trade_result['direction'].upper()} - {outcome_text}",
                 color=outcome_color, fontweight="bold")
    ax.legend(loc="upper left", fontsize=8)

    if candles:
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
# LIVE mode: real MT5 orders
# --------------------------------------------------------------------------

def mt5_open_position(mt5, symbol, direction, lot, point_size,
                       tp_points=TP_POINTS, sl_points=SL_POINTS,
                       max_spread_points=MAX_SPREAD_POINTS):
    """Sends a real market order with TP/SL attached, computed from
    the SAME tick used for entry (fixes the old close-vs-fill mismatch).
    Tries FOK -> IOC -> RETURN filling modes since brokers differ in
    which one they accept. Returns a dict with either the fill info
    or an 'error' key - never raises.

    BUGFIX (entry hit -> TP hit -> position never closed -> SL also
    hit): two real problems were compounding here:

    1) result.order is the ORDER ticket, not necessarily the POSITION
       ticket - on many brokers/account types they differ. Using the
       wrong id meant positions_get(ticket=...) could never find the
       real position, so the bot never noticed it was still open (or
       thought it was already closed when it wasn't). Fixed by
       resolving the true position id from the opening deal:
       history_deals_get(ticket=result.deal)[0].position_id.

    2) Some brokers (Market Execution accounts) silently DROP the
       tp/sl fields on the initial TRADE_ACTION_DEAL and require a
       separate TRADE_ACTION_SLTP request to attach stops to the
       resulting position. If that happens, no server-side TP/SL
       exists at all - price can sail straight through both levels
       with nothing to close it. Fixed by verifying the position's
       actual sl/tp after opening and re-attaching them via
       TRADE_ACTION_SLTP if the broker dropped them.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        return {"error": f"symbol_info returned None for {symbol}"}
    if not info.visible:
        mt5.symbol_select(symbol, True)

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return {"error": "symbol_info_tick returned None"}

    spread_points = round((tick.ask - tick.bid) / point_size) if point_size else 0
    if max_spread_points and spread_points > max_spread_points:
        return {"error": f"spread too wide ({spread_points} pts > max {max_spread_points} pts) - skipped"}

    if direction == "bull":
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
        tp_price = price + tp_points * point_size
        sl_price = price - sl_points * point_size
    else:
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
        tp_price = price - tp_points * point_size
        sl_price = price + sl_points * point_size

    step = info.volume_step or 0.01
    lot = max(info.volume_min, min(info.volume_max, round(lot / step) * step))

    last_result = None
    result = None
    for filling in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN):
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": lot,
            "type": order_type, "price": price, "tp": tp_price, "sl": sl_price,
            "deviation": 20, "magic": 234000, "comment": "engulfing",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling,
        }
        result = mt5.order_send(request)
        last_result = result
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            break
    else:
        retcode = last_result.retcode if last_result else "?"
        comment = last_result.comment if last_result else "order_send returned None"
        return {"error": f"retcode={retcode} comment={comment}"}

    # Resolve the REAL position id from the opening deal - not result.order.
    position_id = result.order
    deal_ticket = getattr(result, "deal", None)
    if deal_ticket:
        deals = mt5.history_deals_get(ticket=deal_ticket)
        if deals:
            position_id = deals[0].position_id

    # Verify the broker actually attached our SL/TP to the position.
    # If not (common on Market Execution accounts), attach them now
    # with a follow-up modify request.
    sltp_warning = None
    positions = mt5.positions_get(ticket=position_id)
    if positions:
        pos = positions[0]
        if abs(pos.tp - tp_price) > point_size or abs(pos.sl - sl_price) > point_size:
            modify_request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": symbol,
                "position": position_id,
                "tp": tp_price,
                "sl": sl_price,
            }
            modify_result = mt5.order_send(modify_request)
            if not modify_result or modify_result.retcode != mt5.TRADE_RETCODE_DONE:
                sltp_warning = (f"broker did not accept inline TP/SL and the follow-up "
                                f"TRADE_ACTION_SLTP fix also failed (retcode="
                                f"{modify_result.retcode if modify_result else '?'}) - "
                                f"this position may have NO server-side stop")
    else:
        sltp_warning = "position not found immediately after opening - could not verify SL/TP attached"

    return {"ticket": position_id, "price": result.price,
            "tp_price": tp_price, "sl_price": sl_price, "lot": lot,
            "warning": sltp_warning}


def mt5_manually_close_position(mt5, symbol, position_id, direction, lot):
    """Correctly close ONE position: send an OPPOSITE market order with
    the 'position' field set to its ticket. This is the right way -
    unlike the old broken TRADE_ACTION_CLOSE_BY (which only closes one
    position "by" an opposite one on hedging accounts, and this bot
    never has two opposite positions open). Used as a safety net if
    the broker's own attached SL/TP never actually fires, so a stuck
    position can't block every future signal forever."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return {"error": "symbol_info_tick returned None"}
    if direction == "bull":
        close_type, price = mt5.ORDER_TYPE_SELL, tick.bid
    else:
        close_type, price = mt5.ORDER_TYPE_BUY, tick.ask

    last_result = None
    for filling in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN):
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": lot,
            "type": close_type, "position": position_id, "price": price,
            "deviation": 20, "magic": 234000, "comment": "engulfing safety close",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling,
        }
        result = mt5.order_send(request)
        last_result = result
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return {"price": result.price}

    retcode = last_result.retcode if last_result else "?"
    comment = last_result.comment if last_result else "order_send returned None"
    return {"error": f"retcode={retcode} comment={comment}"}


def check_position_status(mt5, trade):
    """Poll the REAL position every tick. Returns None while still
    open. Once MT5 shows it closed - via its own attached TP/SL, a
    stop-out, or manual close - reads the exact close reason and
    price from the deal history (DEAL_REASON_SL / DEAL_REASON_TP),
    instead of guessing or trying to close it again ourselves."""
    ticket = trade.get("ticket")
    if not ticket:
        return None

    positions = mt5.positions_get(ticket=ticket)
    if positions:
        return None  # still open

    deals = mt5.history_deals_get(position=ticket)
    if not deals:
        return None  # broker hasn't published the closing deal yet - keep polling

    close_deal = None
    for d in deals:
        if d.entry == mt5.DEAL_ENTRY_OUT:
            close_deal = d
            break
    if close_deal is None:
        return None

    if close_deal.reason == mt5.DEAL_REASON_SL:
        outcome = "sl"
    elif close_deal.reason == mt5.DEAL_REASON_TP:
        outcome = "tp"
    else:
        # closed some other way (manual/stop-out) - classify by nearer level

        outcome = "tp" if abs(close_deal.price - trade["tp_price"]) < abs(close_deal.price - trade["sl_price"]) else "sl"

    return {"outcome": outcome, "exit_price": close_deal.price,
            "profit": close_deal.profit, "time": datetime.fromtimestamp(close_deal.time)}


def pnl_for(trade):
    entry = trade["entry_price"]
    current = trade["current_price"]
    point_size = trade["point_size"] or 0.01
    diff = (current - entry) if trade["direction"] == "bull" else (entry - current)
    pnl_points = diff / point_size
    pnl_pct = (diff / entry) * 100 if entry else 0.0
    return pnl_points, pnl_pct


# --------------------------------------------------------------------------
# Live dashboard
# --------------------------------------------------------------------------

def render_ascii_chart(candles, height=CHART_HEIGHT, forming_idx=None):
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
        if forming_idx is not None and i == forming_idx:
            color = "cyan"
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

    axis = Text(f"  high {top:.3f}" + " " * max(0, len(candles) * 2 - 20) + f"low {bottom:.3f}", style="dim")
    note = Text("  (last bar = still forming)", style="dim") if forming_idx is not None else Text("")
    return Group(*lines, axis, note)


class Dashboard:
    def __init__(self, symbol, tf_key, point_size):
        self.symbol = symbol
        self.tf_key = tf_key
        self.point_size = point_size
        self.status_line = "Starting..."
        self.chart_candles = []
        self.forming_present = False
        self.ohlc = None
        self.candle_ts = "-"
        self.live_tick = None
        self.trades = []
        self.events = []

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
            live_text = Text.from_markup(
                f"BID [bold]{t['bid']:.5f}[/bold]   ASK [bold]{t['ask']:.5f}[/bold]   "
                f"spread [yellow]{t['spread_points']}pts[/yellow]  "
                f"[dim]@ {t['time'].strftime('%H:%M:%S')}[/dim]"
            )
        else:
            live_text = Text("(waiting for first tick)", style="dim")

        if self.ohlc:
            o, h, l, c = self.ohlc["open"], self.ohlc["high"], self.ohlc["low"], self.ohlc["close"]
            direction_color = "green" if c >= o else "red"
            ohlc_text = Text.from_markup(
                f"O [bold]{o:.3f}[/bold]   H [bold]{h:.3f}[/bold]   "
                f"L [bold]{l:.3f}[/bold]   C [{direction_color}]{c:.3f}[/{direction_color}]   @ {self.candle_ts}"
            )
        else:
            ohlc_text = Text("(waiting for first closed candle)", style="dim")

        forming_idx = len(self.chart_candles) - 1 if self.forming_present and self.chart_candles else None
        chart = render_ascii_chart(self.chart_candles, forming_idx=forming_idx)

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
                trades_table.add_row(
                    f"[{side_color}]{side}[/{side_color}]",
                    f"{t.get('lot', 0) or 0:.2f}",
                    f"{t['entry_price']:.3f}", f"{t['current_price']:.3f}",
                    f"[{pnl_color}]{pnl_pts:+.1f}[/{pnl_color}]",
                    f"[{pnl_color}]{pnl_pct:+.3f}%[/{pnl_color}]",
                    t["status"],
                )
        else:
            trades_table.add_row("-", "-", "-", "-", "-", "-", "no trades yet")

        events_text = "\n".join(self.events) if self.events else "[dim](none yet)[/dim]"

        body = Group(
            header,
            Rule(style="dim"),
            Text.from_markup("[bold]Live price[/bold]"),
            live_text,
            Rule(style="dim"),
            Text.from_markup("[bold]OHLC (last closed candle)[/bold]"),
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
        title = "Engulfing Watcher - LIVE (REAL ORDERS)" if self.status_line.startswith("[green]Live") else "Engulfing Watcher"
        return Panel(body, title=title, border_style="cyan")


def seconds_until_next_close_epoch(tf_seconds):
    now = datetime.now().timestamp()
    return (int(now) // tf_seconds + 1) * tf_seconds


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

def run_live():
    console.print(Panel(
        "[bold yellow]This mode places REAL market orders on your MT5 account.[/bold yellow]\n"
        "Test on a demo account first. Not financial advice.",
        border_style="yellow",
    ))
    if not Confirm.ask("Continue with LIVE real-order trading?", default=False):
        console.print("Cancelled.")
        return

    login = IntPrompt.ask("Account ID (login)")
    password = Prompt.ask("Password", password=False)
    server = Prompt.ask("Server")
    symbol = Prompt.ask("Symbol", default="EURUSD").strip().upper()
    tf_key = Prompt.ask("Timeframe", choices=list(TIMEFRAMES.keys()), default="M1")
    tf_seconds = TIMEFRAMES[tf_key]
    lot = float(Prompt.ask("Lot size (e.g. 0.01, 0.1, 1.0)", default="0.01"))

    mt5 = connect_live(login, password, server)

    if not mt5.symbol_select(symbol, True):
        close_live(mt5)
        console.print(f"[bold red]Symbol '{symbol}' not found or not visible in Market Watch.[/bold red]")
        raise SystemExit(1)

    info = mt5.symbol_info(symbol)
    point_size = info.point if info and info.point else 0.01

    dash = Dashboard(symbol, tf_key, point_size)
    active_trade = None
    history = get_live_closed_candles(mt5, symbol, tf_key, n_closed=CHART_CANDLES) or []
    if history:
        dash.ohlc = history[-1]
        dash.candle_ts = history[-1]["time"]
    next_close_epoch = seconds_until_next_close_epoch(tf_seconds) + 2

    try:
        with Live(dash.render(), console=console, refresh_per_second=4, screen=False) as live:
            dash.status_line = "[green]Live - polling position/price every <1s[/green]"
            while True:
                tick = get_live_tick(mt5, symbol)
                if tick:
                    dash.live_tick = tick
                    if active_trade:
                        active_trade["current_price"] = tick["bid"] if active_trade["direction"] == "bull" else tick["ask"]

                    forming = {"time": datetime.now(),
                               "open": history[-1]["close"] if history else tick["bid"],
                               "high": max(tick["bid"], history[-1]["close"] if history else tick["bid"]),
                               "low": min(tick["bid"], history[-1]["close"] if history else tick["bid"]),
                               "close": tick["bid"]}
                    dash.chart_candles = (history[-(CHART_CANDLES - 1):] if history else []) + [forming]
                    dash.forming_present = True
                    # BUGFIX: OHLC used to only update once per full candle
                    # close (e.g. every 60s on M1), so it looked frozen
                    # between closes even though the live price ticked.
                    # Now it tracks the forming candle live, every tick.
                    dash.ohlc = forming
                    dash.candle_ts = "live (forming)"

                # poll the REAL position every tick - this is the fix for
                # "can't find the trade after TP/SL hits"
                if active_trade:
                    result = check_position_status(mt5, active_trade)
                    if result:
                        outcome = result["outcome"]
                        status = "TP_HIT" if outcome == "tp" else "SL_HIT"
                        active_trade["status"] = status
                        active_trade["current_price"] = result["exit_price"]
                        active_trade["tp_status"] = "hit" if outcome == "tp" else "Canceled by SL"
                        active_trade["sl_status"] = "hit" if outcome == "sl" else "Canceled by TP"

                        update_signal_json_status(active_trade["entry_time"], active_trade["tp_status"],
                                                   active_trade["sl_status"], status,
                                                   exit_price=result["exit_price"], profit=result["profit"])
                        png_path = export_trade_result_chart(active_trade, history)

                        label = "TP HIT" if outcome == "tp" else "SL HIT"
                        dash.add_event(f"[bold]{label}[/bold] {symbol} {active_trade['direction']} "
                                        f"@ {result['exit_price']:.5f}  profit={result['profit']:.2f} -> {png_path}")
                        active_trade = None
                    elif tick:
                        # SAFETY NET: MT5 still shows this position open,
                        # but if the live price has already blown past
                        # BOTH the intended TP and SL, the broker's stop
                        # clearly never fired (e.g. it was silently
                        # dropped - see mt5_open_position's warning).
                        # Left alone this position would sit open
                        # forever and every future signal would keep
                        # getting skipped as "trade already active" -
                        # which is exactly what happened. Force-close it.
                        live_price = tick["bid"] if active_trade["direction"] == "bull" else tick["ask"]
                        if active_trade["direction"] == "bull":
                            breached = live_price >= active_trade["tp_price"] or live_price <= active_trade["sl_price"]
                        else:
                            breached = live_price <= active_trade["tp_price"] or live_price >= active_trade["sl_price"]
                        if breached:
                            close_res = mt5_manually_close_position(
                                mt5, symbol, active_trade["ticket"], active_trade["direction"], active_trade["lot"])
                            if "price" in close_res:
                                dash.add_event(
                                    f"[bold yellow]Safety-net close[/bold yellow] - broker stop never fired, "
                                    f"closed manually @ {close_res['price']:.5f}"
                                )
                                # next tick's check_position_status will pick up the
                                # official close reason/price from history and finish
                                # clearing active_trade normally
                            else:
                                dash.add_event(f"[bold red]Safety-net close FAILED: {close_res['error']}[/bold red]")

                if time.time() >= next_close_epoch:
                    new_history = get_live_closed_candles(mt5, symbol, tf_key, n_closed=CHART_CANDLES)
                    if new_history and len(new_history) >= 2:
                        prev, curr = new_history[-2], new_history[-1]
                        history = new_history
                        dash.ohlc = curr
                        dash.candle_ts = curr["time"]
                        dash.chart_candles = history[-CHART_CANDLES:]
                        dash.forming_present = False

                        if is_bullish_engulfing(prev, curr, point_size):
                            result = "bull"
                        elif is_bearish_engulfing(prev, curr, point_size):
                            result = "bear"
                        else:
                            result = None

                        if result is not None:
                            if active_trade is None:
                                order = mt5_open_position(mt5, symbol, result, lot, point_size)
                                if "ticket" in order:
                                    active_trade = {
                                        "symbol": symbol, "direction": result,
                                        "entry_price": order["price"], "entry_time": _ts_str(curr["time"]),
                                        "tp_price": order["tp_price"], "sl_price": order["sl_price"],
                                        "lot": order["lot"], "ticket": order["ticket"],
                                        "point_size": point_size, "status": "open",
                                        "current_price": order["price"],
                                    }
                                    save_signal_json(symbol, result, prev, curr, curr["time"], active_trade)
                                    export_chart_png(symbol, result, history, curr["time"])
                                    dash.trades.append(active_trade)
                                    label = "LONG" if result == "bull" else "SHORT"
                                    dash.add_event(f"[green]Order filled[/green] {label} ticket={order['ticket']} "
                                                    f"@ {order['price']:.5f} TP {order['tp_price']:.5f} SL {order['sl_price']:.5f}")
                                    if order.get("warning"):
                                        dash.add_event(f"[bold red]WARNING: {order['warning']}[/bold red]")
                                else:
                                    dash.add_event(f"[red]Order failed: {order['error']}[/red]")
                            else:
                                dash.add_event(f"{'Bullish' if result == 'bull' else 'Bearish'} pattern seen - "
                                                f"not followed (position already open)")
                    next_close_epoch = seconds_until_next_close_epoch(tf_seconds) + 2

                live.update(dash.render())
                time.sleep(TICK_INTERVAL)
    except KeyboardInterrupt:
        console.print("\n[bold]Stopped by user.[/bold]")
    finally:
        close_live(mt5)


def run_simulate():
    """Paper trading only - never touches a real account."""
    symbol = Prompt.ask("Symbol (simulated)", default="XAUUSD").strip().upper()
    tf_key = Prompt.ask("Timeframe label (cosmetic only)", choices=list(TIMEFRAMES.keys()), default="M1")
    interval = IntPrompt.ask("Seconds between simulated candles (e.g. 3 for a fast demo)", default=3)
    point_size = float(Prompt.ask("Point size (price value of 1 point)", default="0.01"))

    sim = CandleSimulator(symbol)
    dash = Dashboard(symbol, tf_key, point_size)
    active_trade = None

    def check_paper_trade(trade, candle):
        if trade["direction"] == "bull":
            hit_tp = candle["high"] >= trade["tp_price"]
            hit_sl = candle["low"] <= trade["sl_price"]
        else:
            hit_tp = candle["low"] <= trade["tp_price"]
            hit_sl = candle["high"] >= trade["sl_price"]
        if hit_tp and hit_sl:
            return "sl"  # ambiguous within one bar - conservative
        if hit_tp:
            return "tp"
        if hit_sl:
            return "sl"
        return None

    try:
        with Live(dash.render(), console=console, refresh_per_second=4, screen=False) as live:
            dash.status_line = "[cyan]Simulating (paper trades only)[/cyan]"
            while True:
                dash.status_line = f"[yellow]Generating next simulated candle[/yellow]"
                live.update(dash.render())
                time.sleep(interval)

                candle = sim.next_candle()
                history = sim.last_n(CHART_CANDLES)
                if not history or len(history) < 2:
                    continue
                prev, curr = history[-2], history[-1]
                ts = curr["time"].strftime("%H:%M:%S")
                dash.ohlc = curr
                dash.candle_ts = ts
                dash.chart_candles = history[-CHART_CANDLES:]
                dash.forming_present = False

                if active_trade:
                    active_trade["current_price"] = curr["close"]
                    outcome = check_paper_trade(active_trade, curr)
                    if outcome:
                        exit_price = active_trade["tp_price"] if outcome == "tp" else active_trade["sl_price"]
                        status = "TP_HIT" if outcome == "tp" else "SL_HIT"
                        active_trade["status"] = status
                        active_trade["current_price"] = exit_price
                        update_signal_json_status(active_trade["entry_time"],
                                                   "hit" if outcome == "tp" else "Canceled by SL",
                                                   "hit" if outcome == "sl" else "Canceled by TP", status)
                        png_path = export_trade_result_chart(active_trade, history)
                        label = "TP HIT" if outcome == "tp" else "SL HIT"
                        dash.add_event(f"[bold]{label}[/bold] {active_trade['direction']} @ {exit_price:.3f} -> {png_path}")
                        active_trade = None

                if is_bullish_engulfing(prev, curr, point_size):
                    result = "bull"
                elif is_bearish_engulfing(prev, curr, point_size):
                    result = "bear"
                else:
                    result = None

                if result is not None:
                    if active_trade is None:
                        entry_price = curr["close"]
                        if result == "bull":
                            tp_price = entry_price + TP_POINTS * point_size
                            sl_price = entry_price - SL_POINTS * point_size
                        else:
                            tp_price = entry_price - TP_POINTS * point_size
                            sl_price = entry_price + SL_POINTS * point_size
                        active_trade = {
                            "symbol": symbol, "direction": result, "entry_price": entry_price,
                            "entry_time": ts, "tp_price": tp_price, "sl_price": sl_price,
                            "lot": None, "ticket": None, "point_size": point_size,
                            "status": "open", "current_price": entry_price,
                        }
                        save_signal_json(symbol, result, prev, curr, ts, active_trade)
                        export_chart_png(symbol, result, history, ts)
                        dash.trades.append(active_trade)
                        label = "LONG" if result == "bull" else "SHORT"
                        dash.add_event(f"New [bold]{label}[/bold] paper trade: entry {entry_price:.3f} "
                                        f"TP {tp_price:.3f} SL {sl_price:.3f}")
                    else:
                        dash.add_event(f"{'Bullish' if result == 'bull' else 'Bearish'} pattern seen - "
                                        f"not followed (trade already active)")

                live.update(dash.render())
    except KeyboardInterrupt:
        console.print("\n[bold]Simulation stopped by user.[/bold]")


def main():
    console.print(Panel.fit(
        "[bold cyan]Engulfing Candle Watcher[/bold cyan]\n"
        "Live mode places REAL MT5 orders. Simulate mode is paper trading only.",
        border_style="cyan",
    ))
    mode = Prompt.ask("Mode", choices=["live", "simulate"], default="simulate")
    if mode == "live":
        run_live()
    else:
        run_simulate()


if __name__ == "__main__":
    main()