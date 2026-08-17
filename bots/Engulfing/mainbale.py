"""
Engulfing Pattern Watcher (MetaTrader5 + rich) – Live + Real Trading + Bale
-----------------------------------------------------------------------------
- Live candlestick chart (30 candles) + orders table + status line.
- Engulfing detection at candle close → opens real market order (no broker TP/SL).
- At 50% TP: closes half of the position, leaves the rest to full TP/SL.
- Full TP/SL: closes remaining volume manually.
- Bale integration: sends open/close charts & SL/TP1/TP2 messages to channel.
- Start/stop messages to admin.
- Prompts for lot size.
- Automatically adjusts lot if margin is insufficient (manual calculation fallback).

Requirements:
    pip install MetaTrader5 rich mplfinance pandas requests

Files:
    .env          – MT5 accounts
    api.env       – Bale tokens (Api, Group, Channel, Admin)
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

TP_POINTS = 1200
SL_POINTS = 750
CANDLES_TO_SHOW = 60
PNG_CANDLES = 30

MAGIC = 123456
DEVIATION = 20

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
# Bale Notifier
# ---------------------------------------------------------------------------
class BaleNotifier:
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

bale: BaleNotifier = None

# ---------------------------------------------------------------------------
# MT5 account loading
# ---------------------------------------------------------------------------
def load_accounts(path=ENV_PATH):
    if not os.path.exists(path):
        raise FileNotFoundError(f".env not found at {path}.")
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

def get_lot():
    while True:
        lot_str = console.input("Lot size (e.g. 1, 0.1, 0.01): ").strip()
        try:
            lot = float(lot_str)
            if lot <= 0:
                console.print("[red]Lot must be positive.[/red]")
                continue
            return lot
        except:
            console.print("[red]Invalid number.[/red]")

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
# Improved symbol selection with fallback and listing
# ---------------------------------------------------------------------------
def resolve_symbol(user_symbol):
    """
    Tries to find a valid symbol on the broker.
    If user_symbol is not found, searches for symbols containing that text
    (case-insensitive) and lets the user choose.
    Also attempts to strip common suffixes like '.d', '.m', '.pro' etc.
    """
    # First, try exactly as entered
    if mt5.symbol_select(user_symbol, True):
        return user_symbol

    # Get all symbols for search
    all_symbols = mt5.symbols_get()
    if not all_symbols:
        return None

    symbol_names = [s.name for s in all_symbols]

    # Try stripped version: remove common suffixes
    stripped = re.sub(r'\.(d|m|pro|ecn|raw|stp|demo|real)$', '', user_symbol, flags=re.IGNORECASE)
    if stripped != user_symbol:
        # Check if stripped exists
        for name in symbol_names:
            if name.upper() == stripped.upper():
                console.print(f"[green]Found symbol '{name}' (suggested from '{user_symbol}'). Using it.[/green]")
                mt5.symbol_select(name, True)
                return name

    # Search for symbols containing the user input (case-insensitive)
    matches = [name for name in symbol_names if user_symbol.lower() in name.lower()]
    if not matches:
        # If no match, also try searching by common names (e.g. for gold)
        if "XAU" in user_symbol or "GOLD" in user_symbol:
            gold_matches = [name for name in symbol_names if "XAU" in name.upper() or "GOLD" in name.upper()]
            if gold_matches:
                matches = gold_matches

    if not matches:
        console.print(f"[red]No symbol found matching '{user_symbol}'.[/red]")
        return None

    if len(matches) == 1:
        chosen = matches[0]
        console.print(f"[green]Using symbol '{chosen}' (only match).[/green]")
        mt5.symbol_select(chosen, True)
        return chosen

    # Multiple matches – let user choose
    console.print(f"[yellow]Multiple symbols match '{user_symbol}':[/yellow]")
    for i, name in enumerate(matches, start=1):
        console.print(f"  {i}. {name}")
    while True:
        choice = console.input("Select number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(matches):
            chosen = matches[int(choice)-1]
            mt5.symbol_select(chosen, True)
            return chosen
        console.print("[red]Invalid choice.[/red]")

# ---------------------------------------------------------------------------
# Margin adjustment with manual fallback
# ---------------------------------------------------------------------------
def adjust_lot_for_margin(symbol, requested_lot):
    """
    Checks available margin and adjusts lot if needed.
    Uses manual calculation if margin_initial is missing.
    Returns (adjusted_lot, warning_message)
    """
    account_info = mt5.account_info()
    if account_info is None:
        return requested_lot, "[yellow]Cannot get account info – using requested lot.[/yellow]"
    
    free_margin = account_info.margin_free
    if free_margin <= 0:
        return 0.0, "[red]Free margin is zero or negative – cannot trade.[/red]"
    
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        return requested_lot, "[yellow]Cannot get symbol info – using requested lot.[/yellow]"
    
    # Try to get margin per lot from symbol info
    margin_per_lot = symbol_info.margin_initial
    if margin_per_lot is None or margin_per_lot <= 0:
        margin_per_lot = symbol_info.margin_maintenance
        if margin_per_lot is None or margin_per_lot <= 0:
            # Manual calculation
            contract_size = symbol_info.trade_contract_size
            if contract_size is None or contract_size <= 0:
                contract_size = 100  # default for many forex/CFDs
            leverage = account_info.leverage
            if leverage is None or leverage <= 0:
                leverage = 100  # fallback
            # Get current price (ask for buy, bid for sell – we use ask for conservative estimate)
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                return requested_lot, "[yellow]Cannot get tick – using requested lot.[/yellow]"
            price = tick.ask
            margin_per_lot = (contract_size * price) / leverage
            console.print(f"[dim]Manual margin per lot: {margin_per_lot:.2f} (ContractSize={contract_size}, Price={price}, Leverage={leverage})[/dim]")
    
    if margin_per_lot <= 0:
        return requested_lot, "[yellow]Margin per lot still unknown – using requested lot.[/yellow]"
    
    max_lot_possible = free_margin / margin_per_lot
    max_lot_safe = max_lot_possible * 0.9  # safety buffer
    
    if requested_lot <= max_lot_safe:
        return requested_lot, None
    else:
        adjusted = round(max_lot_safe, 2)
        if adjusted <= 0:
            return 0.0, f"[red]Requested lot {requested_lot} exceeds margin. Max possible: {max_lot_possible:.3f} – cannot trade.[/red]"
        else:
            warning = f"[yellow]Lot adjusted from {requested_lot} to {adjusted} (max safe: {max_lot_safe:.3f}).[/yellow]"
            return adjusted, warning

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
# Real order placement
# ---------------------------------------------------------------------------
def place_market_order(symbol, direction, volume, point, live_console):
    order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        live_console.log("[red]No tick data for order placement.[/red]")
        return None
    price = tick.ask if direction == "buy" else tick.bid
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "deviation": DEVIATION,
        "magic": MAGIC,
        "comment": "EngulfingBot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        live_console.log(f"[red]Order failed: retcode={result.retcode}, comment={result.comment}[/red]")
        return None
    return result.order

# ---------------------------------------------------------------------------
# Close a specific volume from a position (used for partial and full close)
# ---------------------------------------------------------------------------
def close_position_volume(symbol, ticket, volume, live_console):
    """Close `volume` lots from position `ticket`."""
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return False
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return False
    pos = pos[0]
    close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": close_type,
        "position": ticket,
        "price": price,
        "deviation": DEVIATION,
        "magic": MAGIC,
        "comment": "CloseByBot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        live_console.log(f"[red]Close failed: retcode={result.retcode}, {result.comment}[/red]")
        return False
    return True

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

def open_trade(trades, direction, entry_price, point, symbol, tf_const, lot, live_console):
    trade_id = (trades[-1]["id"] + 1) if trades else 1
    if direction == "bullish":
        tp_price = entry_price + TP_POINTS * point
        sl_price = entry_price - SL_POINTS * point
        order_type = "Long"
        mt5_direction = "buy"
    else:
        tp_price = entry_price - TP_POINTS * point
        sl_price = entry_price + SL_POINTS * point
        order_type = "Short"
        mt5_direction = "sell"

    # Adjust lot again (in case margin changed)
    adjusted_lot, warn_msg = adjust_lot_for_margin(symbol, lot)
    if warn_msg:
        live_console.log(warn_msg)
    if adjusted_lot <= 0:
        live_console.log("[red]Lot too small to trade – skipping.[/red]")
        return
    lot = adjusted_lot  # use adjusted

    ticket = place_market_order(symbol, mt5_direction, lot, point, live_console)
    if not ticket:
        live_console.log("[red]Failed to open real order, skipping trade.[/red]")
        return

    snapshot_rates = mt5.copy_rates_from_pos(symbol, tf_const, 1, PNG_CANDLES)
    chart_path = None
    if snapshot_rates is not None and len(snapshot_rates) > 0:
        chart_path = save_chart(snapshot_rates, symbol, trade_id, "open",
                                entry=entry_price, tp=tp_price, sl=sl_price)

    tp1_price = entry_price + (TP_POINTS/2)*point if direction == "bullish" else entry_price - (TP_POINTS/2)*point

    trade = {
        "id": trade_id,
        "symbol": symbol,
        "timeframe": None,
        "direction": mt5_direction,
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
        "tp1_price": tp1_price,
        "direction_raw": direction,
        "ticket": ticket,
        "lot": lot,            # current remaining lot
        "initial_lot": lot,
    }
    trades.append(trade)
    save_trades(trades)

    color = "green" if direction == "bullish" else "red"
    live_console.log(Panel(
        f"[{color}]{order_type} signal[/{color}] on {symbol} (Ticket: {ticket})\n"
        f"Entry: {entry_price:.5f}  TP1: {tp1_price:.5f}  TP2: {tp_price:.5f}  SL: {sl_price:.5f}  Lot: {lot}",
        title=f"New Trade #{trade_id}",
    ))

    if bale and bale.channel_id and chart_path:
        caption = (
            f"order id on json db : {trade_id}\n"
            f"symbol : {symbol}\n"
            f"Type Order : {order_type}\n"
            f"Entry price : {entry_price:.5f}\n"
            f"TP 1 : {tp1_price:.5f} => 75 point\n"
            f"Full TP (TP 2) : {tp_price:.5f} => 150 point\n"
            f"StopLess : {sl_price:.5f}"
        )
        bale.notify_channel_photo(chart_path, caption)

# ---------------------------------------------------------------------------
# Monitor positions (with half-volume close at 50% TP)
# ---------------------------------------------------------------------------
def monitor_positions(trades, symbol, point, tf_const, live_console):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return

    changed = False
    for trade in trades:
        if trade["status"] != "open" or trade["symbol"] != symbol:
            continue

        pos_list = mt5.positions_get(ticket=trade["ticket"])
        if not pos_list:
            trade["status"] = "closed"
            trade["result"] = "unknown"
            trade["close_time"] = datetime.now().isoformat(timespec="seconds")
            changed = True
            live_console.log(f"[yellow]Position #{trade['ticket']} missing, marked closed.[/yellow]")
            continue

        pos = pos_list[0]
        current_price = pos.price_current
        current_lot = pos.volume

        # --- 50% TP (partial close) ---
        if not trade.get("half_tp_notified", False):
            tp1 = trade.get("tp1_price")
            if tp1 is not None:
                hit_tp1 = (trade["direction"] == "buy" and current_price >= tp1) or \
                          (trade["direction"] == "sell" and current_price <= tp1)
                if hit_tp1:
                    half_lot = trade["initial_lot"] / 2.0
                    if half_lot > current_lot:
                        half_lot = current_lot
                    if half_lot > 0:
                        if close_position_volume(symbol, trade["ticket"], half_lot, live_console):
                            trade["half_tp_notified"] = True
                            trade["lot"] = current_lot - half_lot
                            changed = True

                            sign = 1 if trade["direction"] == "buy" else -1
                            closed_pnl_points = sign * (current_price - trade["entry_price"]) / point
                            closed_pnl_percent = sign * (current_price - trade["entry_price"]) / trade["entry_price"] * 100

                            live_console.log(Panel(
                                f"[bold yellow]50% TP reached – closed {half_lot} lot[/bold yellow] on trade #{trade['id']} ({trade['symbol']})\n"
                                f"Level: {tp1:.5f}  PNL: {closed_pnl_points:.1f} pts ({closed_pnl_percent:.2f}%)",
                                title="Partial TP + Half Close",
                            ))

                            if bale and bale.channel_id:
                                bale.notify_channel(
                                    f"order id on json db : {trade['id']}\n"
                                    f"is hit tp 1 on price TP 1 : {tp1:.5f} => 75point\n"
                                    f"Half volume ({half_lot} lot) closed.\n"
                                    f"PNL per % : {closed_pnl_percent:.2f}%\n"
                                    f"PNL per point : {closed_pnl_points:.1f}\n"
                                    f"Remaining lot: {trade['lot']}"
                                )
                        else:
                            trade["half_tp_notified"] = True
                            changed = True
                            live_console.log(f"[red]Failed to partial close for trade #{trade['id']}, flagged as notified.[/red]")

        # --- Full TP/SL ---
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

            remaining_volume = trade.get("lot", trade["initial_lot"])
            if not close_position_volume(symbol, trade["ticket"], remaining_volume, live_console):
                live_console.log(f"[red]Failed to close remaining volume for trade #{trade['id']}, will retry.[/red]")
                continue

            trade["status"] = "closed"
            trade["result"] = hit
            trade["close_price"] = close_price
            trade["close_time"] = datetime.now().isoformat(timespec="seconds")
            trade["pnl_points"] = round(pnl_points, 1)
            trade["pnl_percent"] = round(pnl_percent, 3)

            if hit == "tp":
                snapshot_rates = mt5.copy_rates_from_pos(symbol, tf_const, 1, PNG_CANDLES)
                if snapshot_rates is not None and len(snapshot_rates) > 0:
                    chart_close = save_chart(snapshot_rates, symbol, trade["id"],
                                             f"closed_{hit}",
                                             entry=trade["entry_price"],
                                             tp=trade["tp_price"],
                                             sl=None)
                    trade["chart_close"] = chart_close
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
                if bale and bale.channel_id:
                    bale.notify_channel(
                        f"order id on json db : {trade['id']}\n"
                        f"symbol : {symbol}\n"
                        f"SL HIT at price : {close_price:.5f}\n"
                        f"PNL per % : {pnl_percent:.2f}%\n"
                        f"PNL per point : {pnl_points:.1f}"
                    )

            color = "green" if hit == "tp" else "red"
            chart_msg = f"Chart saved: {trade.get('chart_close')}" if trade.get('chart_close') else "No chart saved"
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

# ---------------------------------------------------------------------------
# Orders table
# ---------------------------------------------------------------------------
def render_orders_table(trades, symbol, point):
    open_trades = [t for t in trades if t["status"] == "open" and t["symbol"] == symbol]
    if not open_trades:
        return Text("No open orders", style="dim")

    tick = mt5.symbol_info_tick(symbol)
    table = Table(title="Open Orders", box=box.SIMPLE)
    table.add_column("#", justify="right")
    table.add_column("Ticket")
    table.add_column("Dir")
    table.add_column("Lot", justify="right")
    table.add_column("Entry", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("TP", justify="right")
    table.add_column("SL", justify="right")
    table.add_column("PNL (%)", justify="right")
    table.add_column("PNL (pts)", justify="right")

    for t in open_trades:
        if tick:
            current_price = tick.bid if t["direction"] == "sell" else tick.ask
            sign = 1 if t["direction"] == "buy" else -1
            pnl_points = sign * (current_price - t["entry_price"]) / point
            pnl_percent = sign * (current_price - t["entry_price"]) / t["entry_price"] * 100
        else:
            current_price = pnl_points = pnl_percent = 0.0

        color = "green" if pnl_points >= 0 else "red"
        table.add_row(
            str(t["id"]), str(t["ticket"]), t["direction"].upper(),
            f"{t.get('lot', t.get('initial_lot', 0)):.2f}",
            f"{t['entry_price']:.5f}",
            f"{current_price:.5f}",
            f"{t['tp_price']:.5f}", f"{t['sl_price']:.5f}",
            f"[{color}]{pnl_percent:.3f}%[/{color}]", f"[{color}]{pnl_points:.1f}[/{color}]",
        )
    return table

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global bale

    accounts = load_accounts()
    if not accounts:
        console.print("[red]No accounts found in .env.[/red]")
        return
    account = choose_account(accounts)
    connect(account)

    symbol_input, tf_key = get_symbol_and_timeframe()
    tf_const, period_seconds = TIMEFRAMES[tf_key]
    lot = get_lot()

    symbol = resolve_symbol(symbol_input)
    if not symbol:
        console.print("[red]Could not find a valid symbol. Exiting.[/red]")
        mt5.shutdown()
        return

    symbol_info = mt5.symbol_info(symbol)
    point = symbol_info.point

    # Adjust lot for margin
    adjusted_lot, warn_msg = adjust_lot_for_margin(symbol, lot)
    if warn_msg:
        console.print(warn_msg)
    if adjusted_lot <= 0:
        console.print("[red]Cannot trade due to insufficient margin. Exiting.[/red]")
        mt5.shutdown()
        return
    if adjusted_lot != lot:
        console.print(f"[yellow]Lot adjusted from {lot} to {adjusted_lot} due to margin constraints.[/yellow]")
        lot = adjusted_lot

    trades = load_trades()

    # Bale setup
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
        f"Lot: {lot}  |  TP: {TP_POINTS} pts  |  SL: {SL_POINTS} pts\n"
        f"Trade log: {TRADES_JSON}\nCharts: {CHARTS_DIR}",
        title="Engulfing Watcher (Live Trading)",
    ))
    console.print("[dim]Press Ctrl+C to stop.[/dim]\n")

    live = mt5.copy_rates_from_pos(symbol, tf_const, 0, 1)
    if live is None or len(live) == 0:
        console.print("[red]Could not fetch initial live candle.[/red]")
        mt5.shutdown()
        return
    last_candle_time = live[0]["time"]
    last_status = "[dim]Waiting for first candle...[/dim]"

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

                monitor_positions(trades, symbol, point, tf_const, live_display.console)

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
                                open_trade(trades, result, entry_price, point, symbol, tf_const, lot, live_display.console)
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