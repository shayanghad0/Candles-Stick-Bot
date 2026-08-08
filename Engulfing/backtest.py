"""
Engulfing Pattern Backtester (MetaTrader5)
-------------------------------------------
- Loads historical OHLC data from MT5.
- Detects engulfing patterns on each candle (except the first).
- Simulates trades with fixed TP/SL.
- Outputs trade log, performance summary, and equity curve chart.

Requirements:
    pip install MetaTrader5 pandas mplfinance rich matplotlib
"""

import os
import re
import json
import time
import traceback
from datetime import datetime

import MetaTrader5 as mt5
import pandas as pd
import mplfinance as mpf
import matplotlib.pyplot as plt   # <-- ADDED for equity curve

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import track
from rich.box import ROUNDED

# ---------------------------------------------------------------------------
# Configuration (edit these or make them interactive)
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
TRADES_JSON = os.path.join(BASE_DIR, "backtest_trades.json")
SUMMARY_CSV = os.path.join(BASE_DIR, "backtest_summary.csv")
EQUITY_CHART = os.path.join(BASE_DIR, "equity_curve.png")

TP_POINTS = 150
SL_POINTS = 200

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

# ---------------------------------------------------------------------------
# .env loading (same as original)
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

# ---------------------------------------------------------------------------
# MT5 connection (only for data)
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
# Engulfing classification (unchanged)
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
# Simulated Trade Management
# ---------------------------------------------------------------------------
class SimulatedTrade:
    def __init__(self, trade_id, symbol, direction, entry_price, entry_time, lot, point, tp_points, sl_points):
        self.id = trade_id
        self.symbol = symbol
        self.direction = direction  # "buy" or "sell"
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.lot = lot
        self.point = point
        self.tp_points = tp_points
        self.sl_points = sl_points
        self.tp_price = entry_price + tp_points * point if direction == "buy" else entry_price - tp_points * point
        self.sl_price = entry_price - sl_points * point if direction == "buy" else entry_price + sl_points * point
        self.status = "open"
        self.close_price = None
        self.close_time = None
        self.result = None  # "tp" or "sl"
        self.pnl_points = None
        self.pnl_percent = None

    def check_hit(self, high, low, timestamp):
        """Check if this trade's TP or SL is hit within the given candle."""
        if self.status != "open":
            return False
        if self.direction == "buy":
            if high >= self.tp_price:
                self.close_price = self.tp_price
                self.result = "tp"
                self.status = "closed"
                self.close_time = timestamp
                return True
            if low <= self.sl_price:
                self.close_price = self.sl_price
                self.result = "sl"
                self.status = "closed"
                self.close_time = timestamp
                return True
        else:  # sell
            if low <= self.tp_price:
                self.close_price = self.tp_price
                self.result = "tp"
                self.status = "closed"
                self.close_time = timestamp
                return True
            if high >= self.sl_price:
                self.close_price = self.sl_price
                self.result = "sl"
                self.status = "closed"
                self.close_time = timestamp
                return True
        return False

    def compute_pnl(self):
        if self.close_price is None:
            return None, None
        if self.direction == "buy":
            pnl_points = (self.close_price - self.entry_price) / self.point
        else:
            pnl_points = (self.entry_price - self.close_price) / self.point
        pnl_percent = (pnl_points * self.point * self.lot) / (self.entry_price * self.lot) * 100
        self.pnl_points = round(pnl_points, 1)
        self.pnl_percent = round(pnl_percent, 3)
        return self.pnl_points, self.pnl_percent

# ---------------------------------------------------------------------------
# Backtest Engine
# ---------------------------------------------------------------------------
def run_backtest(symbol, tf_const, tf_key, lot, candle_count):
    # Fetch historical data
    rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, candle_count)
    if rates is None or len(rates) < 2:
        console.print(f"[red]Failed to fetch data for {symbol} or not enough candles.[/red]")
        return

    # Convert to list of dict for easier access
    candles = [{"open": r[1], "high": r[2], "low": r[3], "close": r[4],
                "time": datetime.fromtimestamp(r[0])} for r in rates]

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        console.print(f"[red]Symbol info not found for {symbol}.[/red]")
        return
    point = symbol_info.point

    trades = []
    trade_id_counter = 0
    equity_curve = []  # list of (time, total_equity)

    console.print(f"Starting backtest on {symbol} {tf_key} with {len(candles)} candles...")

    # We'll iterate through candles, starting from index 1 (so we have a previous candle)
    for i in track(range(1, len(candles)), description="Processing candles..."):
        prev = candles[i-1]
        curr = candles[i]
        curr_time = curr["time"]

        # --- 1. Check existing open trades against this candle's high/low ---
        for trade in trades:
            if trade.status == "open":
                hit = trade.check_hit(curr["high"], curr["low"], curr_time)
                if hit:
                    trade.compute_pnl()

        # --- 2. Check for new engulfing signal ---
        pattern = classify_engulfing(prev, curr)
        if pattern:
            # Open a simulated trade at the close of the current candle
            entry_price = curr["close"]
            direction = "buy" if pattern == "bullish" else "sell"
            trade_id_counter += 1
            new_trade = SimulatedTrade(
                trade_id=trade_id_counter,
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                entry_time=curr_time,
                lot=lot,
                point=point,
                tp_points=TP_POINTS,
                sl_points=SL_POINTS
            )
            trades.append(new_trade)

        # --- 3. Update equity curve (using current price of open trades) ---
        # Compute total PnL: closed profit + floating PnL on open trades
        closed_pnl = sum(t.pnl_points for t in trades if t.status == "closed" and t.pnl_points is not None)
        floating_pnl = 0.0
        for t in trades:
            if t.status == "open":
                if t.direction == "buy":
                    pnl = (curr["close"] - t.entry_price) / point
                else:
                    pnl = (t.entry_price - curr["close"]) / point
                floating_pnl += pnl
        total_pnl = closed_pnl + floating_pnl
        equity_curve.append((curr_time, total_pnl))

    # After processing all candles, close any remaining open trades at the last price
    last_candle = candles[-1]
    for t in trades:
        if t.status == "open":
            t.close_price = last_candle["close"]
            t.close_time = last_candle["time"]
            t.result = "open_end"
            t.status = "closed"
            t.compute_pnl()

    # -----------------------------------------------------------------------
    # Generate summary and export
    # -----------------------------------------------------------------------
    console.print("\n[bold green]Backtest completed![/bold green]")
    print_summary(trades, symbol, tf_key, lot)
    export_trades(trades, symbol, tf_key)
    plot_equity_curve(equity_curve, symbol)
    return trades

# ---------------------------------------------------------------------------
# Summary & Export
# ---------------------------------------------------------------------------
def print_summary(trades, symbol, timeframe, lot):
    closed_trades = [t for t in trades if t.status == "closed"]
    total_trades = len(closed_trades)
    if total_trades == 0:
        console.print("[yellow]No trades were opened.[/yellow]")
        return

    winning = [t for t in closed_trades if t.result == "tp"]
    losing = [t for t in closed_trades if t.result == "sl"]
    win_count = len(winning)
    lose_count = len(losing)
    win_rate = win_count / total_trades * 100 if total_trades else 0

    total_pnl_points = sum(t.pnl_points for t in closed_trades if t.pnl_points is not None)
    total_pnl_percent = sum(t.pnl_percent for t in closed_trades if t.pnl_percent is not None)
    avg_pnl = total_pnl_points / total_trades if total_trades else 0

    table = Table(title=f"Backtest Summary – {symbol} {timeframe}", box=ROUNDED)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Total Trades", str(total_trades))
    table.add_row("Wins", str(win_count))
    table.add_row("Losses", str(lose_count))
    table.add_row("Win Rate", f"{win_rate:.2f}%")
    table.add_row("Total PnL (pts)", f"{total_pnl_points:.1f}")
    table.add_row("Total PnL (%)", f"{total_pnl_percent:.2f}%")
    table.add_row("Average PnL (pts)", f"{avg_pnl:.2f}")
    table.add_row("Lot size", str(lot))
    console.print(table)

def export_trades(trades, symbol, timeframe):
    # Save full trade log to JSON
    trade_list = []
    for t in trades:
        trade_list.append({
            "id": t.id,
            "symbol": t.symbol,
            "direction": t.direction,
            "entry_price": t.entry_price,
            "entry_time": t.entry_time.isoformat(),
            "tp_price": t.tp_price,
            "sl_price": t.sl_price,
            "status": t.status,
            "result": t.result,
            "close_price": t.close_price,
            "close_time": t.close_time.isoformat() if t.close_time else None,
            "pnl_points": t.pnl_points,
            "pnl_percent": t.pnl_percent,
            "lot": t.lot,
        })
    with open(TRADES_JSON, "w") as f:
        json.dump(trade_list, f, indent=2)
    console.print(f"[green]Trade log saved to {TRADES_JSON}[/green]")

    # Export summary CSV
    df = pd.DataFrame(trade_list)
    if not df.empty:
        # Select relevant columns if they exist
        cols = ["id", "direction", "entry_price", "tp_price", "sl_price",
                "result", "pnl_points", "pnl_percent"]
        df_export = df[cols] if all(c in df.columns for c in cols) else df
        df_export.to_csv(SUMMARY_CSV, index=False)
        console.print(f"[green]Summary CSV saved to {SUMMARY_CSV}[/green]")

def plot_equity_curve(equity_curve, symbol):
    """Plot equity curve using matplotlib (no OHLC requirements)."""
    if not equity_curve:
        console.print("[yellow]No equity curve data to plot.[/yellow]")
        return
    df = pd.DataFrame(equity_curve, columns=["time", "equity"])
    df.set_index("time", inplace=True)

    plt.figure(figsize=(10, 6))
    plt.plot(df.index, df["equity"], linewidth=1.5, color='blue')
    plt.title(f"Equity Curve – {symbol}")
    plt.xlabel("Time")
    plt.ylabel("PnL (points)")
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(EQUITY_CHART, dpi=120, bbox_inches="tight")
    plt.close()
    console.print(f"[green]Equity curve saved to {EQUITY_CHART}[/green]")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    accounts = load_accounts()
    if not accounts:
        console.print("[red]No accounts found in .env.[/red]")
        return
    account = choose_account(accounts)
    connect(account)

    symbol = console.input("Symbol (e.g. EURUSD): ").strip().upper()
    if not mt5.symbol_select(symbol, True):
        console.print(f"[red]Symbol '{symbol}' not found.[/red]")
        mt5.shutdown()
        return

    console.print("Available timeframes: " + ", ".join(TIMEFRAMES.keys()))
    tf_input = console.input("Timeframe : ").strip().upper() or "M1"
    if tf_input not in TIMEFRAMES:
        console.print(f"[yellow]Unknown timeframe '{tf_input}', defaulting to M1.[/yellow]")
        tf_input = "M1"
    tf_const, _ = TIMEFRAMES[tf_input]

    lot_input = console.input("Lot size (e.g. 1, 0.1, 0.01): ").strip()
    try:
        lot = float(lot_input)
        if lot <= 0:
            raise ValueError
    except:
        console.print("[red]Invalid lot size. Using 0.01.[/red]")
        lot = 0.01

    candle_input = console.input("Number of OHLC candles to backtest (e.g. 750): ").strip()
    try:
        candle_count = int(candle_input)
        if candle_count < 2:
            raise ValueError
    except:
        console.print("[red]Invalid number, using 500.[/red]")
        candle_count = 500

    run_backtest(symbol, tf_const, tf_input, lot, candle_count)

    mt5.shutdown()

if __name__ == "__main__":
    main()