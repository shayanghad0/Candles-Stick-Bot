"""
Engulfing Pattern Watcher (MetaTrader5 + rich) – Live + Bale alerts
----------------------------------------------------------------------
- Live candlestick chart (30 candles) + orders table + status line.
- Engulfing detection at candle close.
- Paper trades with TP/SL, JSON log, PNG snapshots (15 candles).
- 50% TP notification (terminal + Bale).
- Bale integration: sends open/close charts + text to channel, start/stop to admin.
- All data from MT5.

Requirements:
    pip install MetaTrader5 rich mplfinance pandas requests

Files:
    .env          – MT5 account blocks
    api.env       – Bale API tokens & chat IDs (Api, Group, Channel, Admin)
    trades.json   – trade log
    charts/       – snapshots
"""

import os
import re
import json
import time
import sys
import traceback
from datetime import datetime

import MetaTrader5 as mt5
import pandas as pd
import mplfinance as mpf
import requests

from rich.console import Console, Group
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich import box

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
API_ENV_PATH = os.path.join(BASE_DIR, "api.env")
TRADES_JSON = os.path.join(BASE_DIR, "trades.json")
CHARTS_DIR = os.path.join(BASE_DIR, "charts")

TP_POINTS = 150
SL_POINTS = 200
CANDLES_TO_SHOW = 30          # terminal display
PNG_CANDLES = 15              # snapshot size

TIMEFRAMES = {
    "M1": (mt5.TIMEFRAME_M1, 60),
    "M5": (mt5.TIMEFRAME_M5, 300),
    "M15": (mt5.TIMEFRAME_M15, 900),
    "M30": (mt5.TIMEFRAME_M30, 1800),
    "H1": (mt5.TIMEFRAME_H1, 3600),
    "H4": (mt5.TIMEFRAME_H4, 14400),
    "D1": (mt5.TIMEFRAME_D1, 86400),
}

console = Console()
os.makedirs(CHARTS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Bale API integration
# ---------------------------------------------------------------------------
class BaleNotifier:
    """
    Sends messages & photos to Bale messenger.
    api.env should contain:
        Api=TOKEN
        Group=chat_id or Null
        Channel=chat_id or Null
        Admin=chat_id or Null
    """
    def __init__(self, token, admin_id=None, group_id=None, channel_id=None):
        self.token = token
        self.base = f"https://tapi.bale.ai/bot{token}"
        self.admin_id = self._to_int(admin_id)
        self.group_id = self._to_int(group_id)
        self.channel_id = self._to_int(channel_id)

    @staticmethod
    def _to_int(val):
        if val is None or str(val).strip().lower() in ("null", "none", ""):
            return None
        try:
            return int(val)
        except:
            return None

    def send_message(self, chat_id, text):
        if chat_id is None:
            return
        url = f"{self.base}/sendMessage"
        data = {"chat_id": chat_id, "text": text}
        try:
            requests.post(url, json=data, timeout=10)
        except Exception as e:
            console.print(f"[red]Bale sendMessage error: {e}[/red]")

    def send_photo(self, chat_id, photo_path, caption=""):
        if chat_id is None or not os.path.exists(photo_path):
            return
        url = f"{self.base}/sendPhoto"
        try:
            with open(photo_path, "rb") as f:
                requests.post(url, data={"chat_id": chat_id, "caption": caption},
                              files={"photo": f}, timeout=15)
        except Exception as e:
            console.print(f"[red]Bale sendPhoto error: {e}[/red]")

    def notify_admin(self, text):
        self.send_message(self.admin_id, text)

    def notify_channel(self, text):
        self.send_message(self.channel_id, text)

    def notify_channel_photo(self, photo_path, caption=""):
        self.send_photo(self.channel_id, photo_path, caption)

# Global Bale notifier (set in main)
bale: BaleNotifier = None

# ---------------------------------------------------------------------------
# .env loading (MT5 accounts)
# ---------------------------------------------------------------------------
def load_accounts(path=ENV_PATH):
    if not os.path.exists(path):
        raise FileNotFoundError(f".env not found at {path}. Create it next to this script.")
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
    table = Table(title="Available Accounts")
    table.add_column("#", justify="right")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Server")
    table.add_column("Login")
    for i, acc in enumerate(accounts, start=1):
        table.add_row(str(i), acc["name"], acc["typeacc"] or acc["type"], acc["server"], acc["login"])
    console.print(table)
    while True:
        choice = console.input(f"Select account [1-{len(accounts)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(accounts):
            return accounts[int(choice) - 1]
        console.print("[red]Invalid choice, try again.[/red]")

def get_symbol_and_timeframe():
    symbol = console.input("Symbol (e.g. EURUSD): ").strip().upper()
    console.print("Available timeframes: " + ", ".join(TIMEFRAMES.keys()))
    tf_input = console.input("Timeframe [default M1]: ").strip().upper() or "M1"
    if tf_input not in TIMEFRAMES:
        console.print(f"[yellow]Unknown timeframe '{tf_input}', defaulting to M1.[/yellow]")
        tf_input = "M1"
    return symbol, tf_input

# ---------------------------------------------------------------------------
# MT5 connection
# ---------------------------------------------------------------------------
def connect(account):
    if not mt5.initialize():
        raise RuntimeError(f"initialize() failed, error code = {mt5.last_error()}")
    authorized = mt5.login(int(account["login"]), password=account["password"], server=account["server"])
    if not authorized:
        mt5.shutdown()
        raise RuntimeError(f"login() failed, error code = {mt5.last_error()}")
    console.print(f"[green]Connected[/green] as {account['name']} ({account['login']}) on {account['server']}.")

# ---------------------------------------------------------------------------
# OHLC helpers
# ---------------------------------------------------------------------------
def fetch_display_rates(symbol, tf_const, total=CANDLES_TO_SHOW):
    closed = mt5.copy_rates_from_pos(symbol, tf_const, 1, total - 1)
    live = mt5.copy_rates_from_pos(symbol, tf_const, 0, 1)
    if closed is None or len(closed) == 0:
        return None
    rates = list(closed)
    if live is not None and len(live) > 0:
        rates.append(live[0])
    return rates

def rates_to_df(rates):
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)
    df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close",
                        "tick_volume": "Volume"}, inplace=True)
    return df[["Open", "High", "Low", "Close", "Volume"]]

# ---------------------------------------------------------------------------
# Terminal chart rendering
# ---------------------------------------------------------------------------
def render_candles_chart(rates, symbol, timeframe, height=22):
    candles = list(rates[-CANDLES_TO_SHOW:])
    live_idx = len(candles) - 1
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    price_max = max(highs)
    price_min = min(lows)
    price_range = (price_max - price_min) or (price_max * 0.0001 or 1.0)

    def to_row(price):
        ratio = (price_max - price) / price_range
        row = int(round(ratio * (height - 1)))
        return max(0, min(height - 1, row))

    plotted = []
    for i, c in enumerate(candles):
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        open_row, close_row = to_row(o), to_row(cl)
        plotted.append({
            "body_top": min(open_row, close_row),
            "body_bottom": max(open_row, close_row),
            "wick_top": to_row(h),
            "wick_bottom": to_row(l),
            "bullish": cl >= o,
            "live": (i == live_idx),
        })

    label_width = 11
    body = Text()
    for r in range(height):
        if r == 0 or r == height - 1 or r % 5 == 0:
            price_at_row = price_max - (r / (height - 1)) * price_range
            label = f"{price_at_row:.5f}".rjust(label_width - 1) + " "
        else:
            label = " " * label_width
        body.append(label, style="dim")

        for cd in plotted:
            if cd["live"]:
                body_color = "yellow" if cd["bullish"] else "orange"
                wick_color = f"dim {body_color}"
            else:
                body_color = "green" if cd["bullish"] else "red"
                wick_color = f"dim {body_color}"

            if cd["body_top"] <= r <= cd["body_bottom"]:
                body.append("█ ", style=body_color)
            elif cd["wick_top"] <= r <= cd["wick_bottom"]:
                body.append("│ ", style=wick_color)
            else:
                body.append("  ")
        body.append("\n")

    axis_chars = [" "] * (label_width + len(candles) * 2)
    def place(text, col):
        start = label_width + col * 2
        for i, ch in enumerate(text):
            if 0 <= start + i < len(axis_chars):
                axis_chars[start + i] = ch

    place(datetime.fromtimestamp(candles[0]["time"]).strftime("%H:%M"), 0)
    place(datetime.fromtimestamp(candles[len(candles)//2]["time"]).strftime("%H:%M"), len(candles)//2)
    place(datetime.fromtimestamp(candles[-1]["time"]).strftime("%H:%M"), len(candles)-3)
    body.append("".join(axis_chars), style="dim")
    return Panel(body, title=f"{symbol} [{timeframe}] - last {len(candles)} candles")

# ---------------------------------------------------------------------------
# Engulfing classification
# ---------------------------------------------------------------------------
def classify_engulfing(prev_candle, curr_candle):
    prev_open, prev_close = prev_candle["open"], prev_candle["close"]
    curr_open, curr_close = curr_candle["open"], curr_candle["close"]
    prev_bearish = prev_close < prev_open
    prev_bullish = prev_close > prev_open
    curr_bullish = curr_close > curr_open
    curr_bearish = curr_close < curr_open
    prev_lo, prev_hi = sorted([prev_open, prev_close])
    curr_lo, curr_hi = sorted([curr_open, curr_close])
    engulfs = curr_lo <= prev_lo and curr_hi >= prev_hi
    if prev_bearish and curr_bullish and engulfs:
        return "bullish"
    if prev_bullish and curr_bearish and engulfs:
        return "bearish"
    return None

# ---------------------------------------------------------------------------
# Chart snapshots
# ---------------------------------------------------------------------------
def save_chart(rates, symbol, trade_id, tag, entry=None, tp=None, sl=None):
    df = rates_to_df(rates)
    addplots = []
    if entry is not None:
        addplots.append(mpf.make_addplot([entry] * len(df), color='blue', linestyle='--', width=1.5, label='Entry'))
    if tp is not None:
        addplots.append(mpf.make_addplot([tp] * len(df), color='green', linestyle='--', width=1.5, label='TP'))
    if sl is not None:
        addplots.append(mpf.make_addplot([sl] * len(df), color='red', linestyle='--', width=1.5, label='SL'))
    path = os.path.join(CHARTS_DIR, f"trade_{trade_id}_{tag}.png")
    mpf.plot(df, type="candle", style="charles",
             title=f"{symbol} - trade #{trade_id} ({tag})",
             volume=False,
             addplot=addplots if addplots else None,
             savefig=dict(fname=path, dpi=120, bbox_inches="tight"))
    return path

# ---------------------------------------------------------------------------
# Trade log
# ---------------------------------------------------------------------------
def load_trades():
    if not os.path.exists(TRADES_JSON):
        return []
    with open(TRADES_JSON, "r", encoding="utf-8") as f:
        return json.load(f)

def save_trades(trades):
    with open(TRADES_JSON, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2)

def open_trade(trades, direction, entry_price, point, symbol, tf_const, live_console):
    trade_id = (trades[-1]["id"] + 1) if trades else 1
    if direction == "bullish":
        tp_price = entry_price + TP_POINTS * point
        sl_price = entry_price - SL_POINTS * point
        order_type = "Long"
    else:
        tp_price = entry_price - TP_POINTS * point
        sl_price = entry_price + SL_POINTS * point
        order_type = "Short"

    # Fetch PNG_CANDLES closed bars for snapshot
    snapshot_rates = mt5.copy_rates_from_pos(symbol, tf_const, 1, PNG_CANDLES)
    chart_path = None
    if snapshot_rates is not None and len(snapshot_rates) > 0:
        chart_path = save_chart(snapshot_rates, symbol, trade_id, "open",
                                entry=entry_price, tp=tp_price, sl=sl_price)

    trade = {
        "id": trade_id,
        "symbol": symbol,
        "timeframe": None,
        "direction": "buy" if direction == "bullish" else "sell",
        "entry_price": entry_price,
        "entry_time": datetime.now().isoformat(timespec="seconds"),
        "tp_price": tp_price,
        "sl_price": sl_price,
        "tp_points": TP_POINTS,
        "sl_points": SL_POINTS,
        "status": "open",
        "result": None,
        "close_price": None,
        "close_time": None,
        "pnl_points": None,
        "pnl_percent": None,
        "chart_open": chart_path,
        "chart_close": None,
        "half_tp_notified": False,
        "tp1_price": entry_price + (TP_POINTS/2)*point if direction == "bullish" else entry_price - (TP_POINTS/2)*point,
        "direction_raw": direction,
    }
    trades.append(trade)
    save_trades(trades)

    # Terminal log
    color = "green" if direction == "bullish" else "red"
    live_console.log(Panel(
        f"[{color}]{order_type} signal[/{color}] on {symbol}\n"
        f"Entry: {entry_price:.5f}  TP1: {trade['tp1_price']:.5f}  TP2: {tp_price:.5f}  SL: {sl_price:.5f}\n"
        f"Chart saved: {chart_path}",
        title=f"New Trade #{trade_id}",
    ))

    # Bale notification: send open chart to channel
    if bale and bale.channel_id and chart_path:
        caption = (
            f"order id on json db : {trade_id}\n"
            f"symbol : {symbol}\n"
            f"Type Order : {order_type}\n"
            f"Entry price : {entry_price:.5f}\n"
            f"TP 1 : {trade['tp1_price']:.5f} => 75 point\n"
            f"Full TP (TP 2) : {tp_price:.5f} => 150 point\n"
            f"StopLess : {sl_price:.5f}"
        )
        bale.notify_channel_photo(chart_path, caption)

    return trade

def check_open_trades(trades, symbol, point, tf_const, live_console):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return

    changed = False
    for trade in trades:
        if trade["status"] != "open" or trade["symbol"] != symbol:
            continue

        current_price = tick.bid if trade["direction"] == "sell" else tick.ask

        # ---- 50% TP notification ----
        if not trade.get("half_tp_notified", False):
            tp1_price = trade.get("tp1_price")
            if tp1_price is not None:
                if trade["direction"] == "buy" and current_price >= tp1_price:
                    trade["half_tp_notified"] = True
                    changed = True
                    # compute PNL at this moment
                    sign = 1 if trade["direction"] == "buy" else -1
                    pnl_points = sign * (current_price - trade["entry_price"]) / point
                    pnl_percent = sign * (current_price - trade["entry_price"]) / trade["entry_price"] * 100
                    # Terminal
                    live_console.log(Panel(
                        f"[bold yellow]50% TP reached[/bold yellow] on trade #{trade['id']} ({trade['symbol']})\n"
                        f"Level: {tp1_price:.5f}",
                        title="Partial TP Alert",
                    ))
                    # Bale
                    if bale and bale.channel_id:
                        bale.notify_channel(
                            f"order id on json db : {trade['id']}\n"
                            f"is hit tp 1 on price TP 1 : {tp1_price:.5f} => 75point\n"
                            f"PNL per % : {pnl_percent:.2f}%\n"
                            f"PNL per point : {pnl_points:.1f}"
                        )
                elif trade["direction"] == "sell" and current_price <= tp1_price:
                    trade["half_tp_notified"] = True
                    changed = True
                    sign = 1 if trade["direction"] == "buy" else -1
                    pnl_points = sign * (current_price - trade["entry_price"]) / point
                    pnl_percent = sign * (current_price - trade["entry_price"]) / trade["entry_price"] * 100
                    live_console.log(Panel(
                        f"[bold yellow]50% TP reached[/bold yellow] on trade #{trade['id']} ({trade['symbol']})\n"
                        f"Level: {tp1_price:.5f}",
                        title="Partial TP Alert",
                    ))
                    if bale and bale.channel_id:
                        bale.notify_channel(
                            f"order id on json db : {trade['id']}\n"
                            f"is hit tp 1 on price TP 1 : {tp1_price:.5f} => 75point\n"
                            f"PNL per % : {pnl_percent:.2f}%\n"
                            f"PNL per point : {pnl_points:.1f}"
                        )

        # ---- Full TP/SL ----
        hit = None
        if trade["direction"] == "buy":
            if current_price >= trade["tp_price"]:
                hit = "tp"
            elif current_price <= trade["sl_price"]:
                hit = "sl"
        else:
            if current_price <= trade["tp_price"]:
                hit = "tp"
            elif current_price >= trade["sl_price"]:
                hit = "sl"

        if hit:
            close_price = trade["tp_price"] if hit == "tp" else trade["sl_price"]
            direction_sign = 1 if trade["direction"] == "buy" else -1
            pnl_points = direction_sign * (close_price - trade["entry_price"]) / point
            pnl_percent = direction_sign * (close_price - trade["entry_price"]) / trade["entry_price"] * 100

            trade["status"] = "closed"
            trade["result"] = hit
            trade["close_price"] = close_price
            trade["close_time"] = datetime.now().isoformat(timespec="seconds")
            trade["pnl_points"] = round(pnl_points, 1)
            trade["pnl_percent"] = round(pnl_percent, 3)

            # Snapshot only on full TP
            if hit == "tp":
                snapshot_rates = mt5.copy_rates_from_pos(symbol, tf_const, 1, PNG_CANDLES)
                if snapshot_rates is not None and len(snapshot_rates) > 0:
                    chart_close = save_chart(snapshot_rates, symbol, trade["id"],
                                             f"closed_{hit}",
                                             entry=trade["entry_price"],
                                             tp=trade["tp_price"],
                                             sl=None)
                    trade["chart_close"] = chart_close
                    # Bale: send close photo to channel
                    if bale and bale.channel_id:
                        caption = (
                            f"order id on json db : {trade['id']}\n"
                            f"is hit full TP : {close_price:.5f} => 150point\n"
                            f"PNL per % : {pnl_percent:.2f}%\n"
                            f"PNL per point : {pnl_points:.1f}"
                        )
                        bale.notify_channel_photo(chart_close, caption)
            else:
                trade["chart_close"] = None

            color = "green" if hit == "tp" else "red"
            chart_msg = f"Chart saved: {trade['chart_close']}" if trade.get("chart_close") else "No chart saved"
            live_console.log(Panel(
                f"[{color}]{hit.upper()} HIT[/{color}] on trade #{trade['id']} ({symbol})\n"
                f"Close price: {close_price:.5f}  PNL: {trade['pnl_points']} pts "
                f"({trade['pnl_percent']}%)\n"
                f"{chart_msg}",
                title=f"Trade #{trade['id']} Closed",
            ))
            changed = True

    if changed:
        save_trades(trades)

def render_orders_table(trades, symbol, point):
    open_trades = [t for t in trades if t["status"] == "open" and t["symbol"] == symbol]
    if not open_trades:
        return Text("No open orders", style="dim")

    tick = mt5.symbol_info_tick(symbol)
    table = Table(title="Open Orders", box=box.SIMPLE)
    table.add_column("#", justify="right")
    table.add_column("Dir")
    table.add_column("Entry", justify="right")
    table.add_column("TP", justify="right")
    table.add_column("SL", justify="right")
    table.add_column("PNL (%)", justify="right")
    table.add_column("PNL (Point)", justify="right")

    for t in open_trades:
        if tick:
            current_price = tick.bid if t["direction"] == "sell" else tick.ask
            sign = 1 if t["direction"] == "buy" else -1
            pnl_points = sign * (current_price - t["entry_price"]) / point
            pnl_percent = sign * (current_price - t["entry_price"]) / t["entry_price"] * 100
        else:
            pnl_points = pnl_percent = 0.0

        color = "green" if pnl_points >= 0 else "red"
        table.add_row(
            str(t["id"]), t["direction"].upper(), f"{t['entry_price']:.5f}",
            f"{t['tp_price']:.5f}", f"{t['sl_price']:.5f}",
            f"[{color}]{pnl_percent:.3f}%[/{color}]", f"[{color}]{pnl_points:.1f}[/{color}]",
        )
    return table

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global bale

    # Load MT5 accounts
    accounts = load_accounts()
    if not accounts:
        console.print("[red]No accounts found in .env.[/red]")
        return
    account = choose_account(accounts)
    connect(account)

    symbol, tf_key = get_symbol_and_timeframe()
    tf_const, period_seconds = TIMEFRAMES[tf_key]

    if not mt5.symbol_select(symbol, True):
        console.print(f"[red]Symbol '{symbol}' not found.[/red]")
        mt5.shutdown()
        return

    symbol_info = mt5.symbol_info(symbol)
    point = symbol_info.point
    trades = load_trades()

    # ----------------- Bale setup -----------------
    bale = None
    if os.path.exists(API_ENV_PATH):
        try:
            api_conf = {}
            with open(API_ENV_PATH, "r") as f:
                for line in f:
                    line = line.strip()
                    if "=" in line:
                        key, val = line.split("=", 1)
                        api_conf[key.strip()] = val.strip()
            token = api_conf.get("Api", "")
            if token and token.lower() != "null":
                bale = BaleNotifier(
                    token,
                    admin_id=api_conf.get("Admin"),
                    group_id=api_conf.get("Group"),
                    channel_id=api_conf.get("Channel")
                )
                # Send startup message to Admin
                start_msg = (
                    "ربات با موفقیت روشن شد\n"
                    f"symbol : {symbol}\n"
                    f"Time Start : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    "Mode Trade : Engulfing CandleStick"
                )
                bale.notify_admin(start_msg)
                console.print("[green]Bale notifications enabled.[/green]")
        except Exception as e:
            console.print(f"[yellow]Bale setup error: {e}[/yellow]")
    else:
        console.print("[dim]api.env not found, skipping Bale.[/dim]")

    console.print(Panel(
        f"Watching [bold]{symbol}[/bold] on [bold]{tf_key}[/bold]\n"
        f"TP: {TP_POINTS} pts  |  SL: {SL_POINTS} pts\n"
        f"Trade log: {TRADES_JSON}\nCharts: {CHARTS_DIR}",
        title="Engulfing Watcher (Live)",
    ))
    console.print("[dim]Press Ctrl+C to stop.[/dim]\n")

    # Initial live candle time
    live = mt5.copy_rates_from_pos(symbol, tf_const, 0, 1)
    if live is None or len(live) == 0:
        console.print("[red]Could not fetch initial live candle.[/red]")
        mt5.shutdown()
        return
    last_candle_time = live[0]["time"]
    last_status = "[dim]Waiting for first candle...[/dim]"

    # Main loop wrapped for graceful Bale notification on stop
    try:
        with Live(console=console, refresh_per_second=1000) as live_display:
            while True:
                rates = fetch_display_rates(symbol, tf_const, CANDLES_TO_SHOW)
                if rates is None:
                    live_display.update(Group(Text("Waiting for data...", style="yellow"),
                                              Text.from_markup(last_status)))
                    time.sleep(0.01)
                    continue

                chart_panel = render_candles_chart(rates, symbol, tf_key)
                orders_rend = render_orders_table(trades, symbol, point)
                status_text = Text.from_markup(last_status)
                live_display.update(Group(chart_panel, orders_rend, status_text))

                check_open_trades(trades, symbol, point, tf_const, live_display.console)

                live_check = mt5.copy_rates_from_pos(symbol, tf_const, 0, 1)
                if live_check is not None and len(live_check) > 0:
                    current_candle_time = live_check[0]["time"]
                    if current_candle_time != last_candle_time:
                        closed_rates = mt5.copy_rates_from_pos(symbol, tf_const, 1, 2)
                        if closed_rates is not None and len(closed_rates) >= 2:
                            prev = {"open": closed_rates[-2]["open"], "close": closed_rates[-2]["close"]}
                            curr = {"open": closed_rates[-1]["open"], "close": closed_rates[-1]["close"]}
                            result = classify_engulfing(prev, curr)
                            now_str = datetime.now().strftime('%H:%M:%S')
                            if result:
                                entry_price = float(closed_rates[-1]["close"])
                                open_trade(trades, result, entry_price, point, symbol, tf_const,
                                           live_display.console)
                                last_status = f"[green]Last signal: {result.upper()} at {now_str}[/green]"
                            else:
                                last_status = f"[dim]No engulfing at {now_str}[/dim]"
                        last_candle_time = current_candle_time

                time.sleep(0.001)

    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped by user.[/yellow]")
        if bale:
            bale.notify_admin("کد متوقف شد\nدلیل : توقف توسط کاربر")
    except Exception as e:
        console.print(f"\n[red]Unexpected error: {e}[/red]")
        if bale:
            bale.notify_admin(f"کد متوقف شد\nدلیل : {str(e)}")
        traceback.print_exc()
    finally:
        mt5.shutdown()

if __name__ == "__main__":
    main()