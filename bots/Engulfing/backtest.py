"""
Engulfing Pattern Backtester (MetaTrader5) – with Balance & HTML Export
-----------------------------------------------------------------------
- Loads historical OHLC data from MT5.
- Detects engulfing patterns and simulates trades.
- Uses mt5.order_calc_profit to compute profit in account currency.
- Tracks balance, equity, and drawdown.
- Exports a complete HTML report with charts and trade list.

Requirements:
    pip install MetaTrader5 pandas mplfinance rich matplotlib
"""

import os
import re
import json
import base64
import io
from datetime import datetime

import MetaTrader5 as mt5
import pandas as pd
import matplotlib.pyplot as plt
from rich.console import Console
from rich.table import Table
from rich.progress import track
from rich.box import ROUNDED

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
TRADES_JSON = os.path.join(BASE_DIR, "backtest_trades.json")
SUMMARY_CSV = os.path.join(BASE_DIR, "backtest_summary.csv")
HTML_REPORT = os.path.join(BASE_DIR, "backtest_report.html")

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
# .env loading (unchanged)
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

def connect(account):
    if not mt5.initialize():
        raise RuntimeError(f"initialize() failed, error code = {mt5.last_error()}")
    authorized = mt5.login(int(account["login"]), password=account["password"], server=account["server"])
    if not authorized:
        mt5.shutdown()
        raise RuntimeError(f"login() failed, error code = {mt5.last_error()}")
    console.print(f"[green]Connected[/green] as {account['name']} ({account['login']}) on {account['server']}.")

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
# Simulated Trade – extended with profit in currency
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
        self.result = None  # "tp", "sl", or "open_end"
        self.pnl_points = None
        self.pnl_percent = None
        self.profit_currency = None   # in account currency

    def check_hit(self, high, low, timestamp):
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

    def compute_profit_currency(self):
        """Use MT5's order_calc_profit to get profit in account currency."""
        if self.close_price is None:
            return None
        order_type = mt5.ORDER_TYPE_BUY if self.direction == "buy" else mt5.ORDER_TYPE_SELL
        profit = mt5.order_calc_profit(
            order_type,
            self.symbol,
            self.lot,
            self.entry_price,
            self.close_price
        )
        self.profit_currency = profit
        return profit

# ---------------------------------------------------------------------------
# Backtest Engine with Balance
# ---------------------------------------------------------------------------
def run_backtest(symbol, tf_const, tf_key, lot, candle_count, initial_balance):
    # Fetch historical data
    rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, candle_count)
    if rates is None or len(rates) < 2:
        console.print(f"[red]Failed to fetch data for {symbol} or not enough candles.[/red]")
        return

    candles = [{"open": r[1], "high": r[2], "low": r[3], "close": r[4],
                "time": datetime.fromtimestamp(r[0])} for r in rates]

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        console.print(f"[red]Symbol info not found for {symbol}.[/red]")
        return
    point = symbol_info.point

    trades = []
    trade_id_counter = 0
    balance = initial_balance
    equity_curve = []        # list of (time, equity)
    balance_curve = []       # list of (time, balance) after closed trades

    console.print(f"Starting backtest on {symbol} {tf_key} with {len(candles)} candles...")

    for i in track(range(1, len(candles)), description="Processing candles..."):
        prev = candles[i-1]
        curr = candles[i]
        curr_time = curr["time"]

        # --- 1. Check existing open trades ---
        for trade in trades:
            if trade.status == "open":
                hit = trade.check_hit(curr["high"], curr["low"], curr_time)
                if hit:
                    trade.compute_pnl()
                    profit = trade.compute_profit_currency()
                    if profit is not None:
                        balance += profit   # update balance on trade close

        # --- 2. Check for new engulfing signal ---
        pattern = classify_engulfing(prev, curr)
        if pattern:
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

        # --- 3. Compute floating equity ---
        floating_pnl = 0.0
        for t in trades:
            if t.status == "open":
                order_type = mt5.ORDER_TYPE_BUY if t.direction == "buy" else mt5.ORDER_TYPE_SELL
                profit_now = mt5.order_calc_profit(
                    order_type,
                    t.symbol,
                    t.lot,
                    t.entry_price,
                    curr["close"]
                )
                if profit_now is not None:
                    floating_pnl += profit_now
        equity = balance + floating_pnl
        equity_curve.append((curr_time, equity))
        balance_curve.append((curr_time, balance))

    # Close any remaining open trades at the last price
    last_candle = candles[-1]
    for t in trades:
        if t.status == "open":
            t.close_price = last_candle["close"]
            t.close_time = last_candle["time"]
            t.result = "open_end"
            t.status = "closed"
            t.compute_pnl()
            profit = t.compute_profit_currency()
            if profit is not None:
                balance += profit
                # update last balance_curve entry
                balance_curve[-1] = (balance_curve[-1][0], balance)

    # Update final equity
    equity_curve[-1] = (equity_curve[-1][0], balance)

    # -----------------------------------------------------------------------
    # Summary and Export
    # -----------------------------------------------------------------------
    console.print("\n[bold green]Backtest completed![/bold green]")
    print_summary(trades, symbol, tf_key, lot, initial_balance, balance)
    export_trades(trades, symbol, tf_key)
    generate_html_report(trades, equity_curve, balance_curve, symbol, tf_key, lot, initial_balance, balance)
    return trades, equity_curve, balance_curve

# ---------------------------------------------------------------------------
# Summary (extended with balance)
# ---------------------------------------------------------------------------
def print_summary(trades, symbol, timeframe, lot, initial_balance, final_balance):
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

    total_profit = sum(t.profit_currency for t in closed_trades if t.profit_currency is not None)
    profit_factor = abs(sum(t.profit_currency for t in winning if t.profit_currency is not None)) / abs(sum(t.profit_currency for t in losing if t.profit_currency is not None)) if losing else float('inf')

    table = Table(title=f"Backtest Summary – {symbol} {timeframe}", box=ROUNDED)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Total Trades", str(total_trades))
    table.add_row("Wins", str(win_count))
    table.add_row("Losses", str(lose_count))
    table.add_row("Win Rate", f"{win_rate:.2f}%")
    table.add_row("Total Profit (currency)", f"{total_profit:.2f}")
    table.add_row("Profit Factor", f"{profit_factor:.2f}")
    table.add_row("Initial Balance", f"{initial_balance:.2f}")
    table.add_row("Final Balance", f"{final_balance:.2f}")
    table.add_row("Lot size", str(lot))
    console.print(table)

# ---------------------------------------------------------------------------
# Export functions (JSON, CSV)
# ---------------------------------------------------------------------------
def export_trades(trades, symbol, timeframe):
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
            "profit_currency": t.profit_currency,
            "lot": t.lot,
        })
    with open(TRADES_JSON, "w") as f:
        json.dump(trade_list, f, indent=2)
    console.print(f"[green]Trade log saved to {TRADES_JSON}[/green]")

    df = pd.DataFrame(trade_list)
    if not df.empty:
        cols = ["id", "direction", "entry_price", "tp_price", "sl_price",
                "result", "pnl_points", "profit_currency"]
        df_export = df[cols] if all(c in df.columns for c in cols) else df
        df_export.to_csv(SUMMARY_CSV, index=False)
        console.print(f"[green]Summary CSV saved to {SUMMARY_CSV}[/green]")

# ---------------------------------------------------------------------------
# HTML Report Generator (FIXED)
# ---------------------------------------------------------------------------
def generate_html_report(trades, equity_curve, balance_curve, symbol, timeframe, lot, initial_balance, final_balance):
    """Create a self-contained HTML report with charts and trade list."""
    closed = [t for t in trades if t.status == "closed"]
    total = len(closed)
    if total == 0:
        console.print("[yellow]No trades to report.[/yellow]")
        return

    winning = [t for t in closed if t.result == "tp"]
    losing = [t for t in closed if t.result == "sl"]
    win_count = len(winning)
    lose_count = len(losing)
    win_rate = win_count / total * 100 if total else 0
    total_profit = sum(t.profit_currency for t in closed if t.profit_currency is not None)
    profit_factor = abs(sum(t.profit_currency for t in winning if t.profit_currency is not None)) / abs(sum(t.profit_currency for t in losing if t.profit_currency is not None)) if losing else float('inf')

    # ---- Equity & Drawdown Charts ----
    if equity_curve:
        df_eq = pd.DataFrame(equity_curve, columns=["time", "equity"])
        df_eq.set_index("time", inplace=True)
        # drawdown
        running_max = df_eq["equity"].cummax()
        drawdown = (running_max - df_eq["equity"]) / running_max * 100
        drawdown.fillna(0, inplace=True)

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
        ax1.plot(df_eq.index, df_eq["equity"], label="Equity", color="blue", linewidth=1.5)
        ax1.set_title(f"Equity Curve – {symbol} {timeframe}")
        ax1.set_ylabel("Balance (currency)")
        ax1.grid(True, alpha=0.3)
        ax1.legend()

        ax2.fill_between(drawdown.index, 0, drawdown, color="red", alpha=0.3)
        ax2.plot(drawdown.index, drawdown, color="red", linewidth=1)
        ax2.set_title("Drawdown (%)")
        ax2.set_ylabel("Drawdown %")
        ax2.set_xlabel("Time")
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()
        # Save to bytes
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=120, bbox_inches="tight")
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode("utf-8")
        plt.close()
        chart_html = f'<img src="data:image/png;base64,{img_base64}" alt="Equity & Drawdown" style="max-width:100%;">'
    else:
        chart_html = "<p>No equity data available.</p>"

    # ---- Trade table (FIXED) ----
    trade_rows = ""
    for t in closed:
        profit = t.profit_currency if t.profit_currency is not None else 0
        color = "green" if profit >= 0 else "red"
        # FIX: format close price only if not None
        close_str = f"{t.close_price:.5f}" if t.close_price is not None else "-"
        trade_rows += f"""
        <tr>
            <td>{t.id}</td>
            <td>{t.direction}</td>
            <td>{t.entry_price:.5f}</td>
            <td>{close_str}</td>
            <td>{t.result}</td>
            <td style="color:{color};">{profit:.2f}</td>
        </tr>
        """

    # ---- Build HTML ----
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Backtest Report – {symbol} {timeframe}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #f4f4f4; }}
        .container {{ max-width: 1200px; margin: auto; background: white; padding: 20px; border-radius: 8px; }}
        h1, h2 {{ color: #333; }}
        .summary {{ display: flex; flex-wrap: wrap; gap: 20px; background: #e9ecef; padding: 15px; border-radius: 5px; }}
        .summary-item {{ flex: 1; min-width: 120px; }}
        .summary-item label {{ font-weight: bold; display: block; color: #555; }}
        .summary-item span {{ font-size: 1.2em; }}
        .chart {{ margin: 20px 0; }}
        table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
        th, td {{ padding: 8px 12px; border: 1px solid #ddd; text-align: right; }}
        th {{ background: #007bff; color: white; }}
        tr:nth-child(even) {{ background: #f2f2f2; }}
        .footer {{ text-align: center; margin-top: 30px; color: #888; font-size: 0.9em; }}
    </style>
</head>
<body>
<div class="container">
    <h1>Backtest Report – {symbol} {timeframe}</h1>
    <div class="summary">
        <div class="summary-item"><label>Total Trades</label><span>{total}</span></div>
        <div class="summary-item"><label>Wins</label><span>{win_count}</span></div>
        <div class="summary-item"><label>Losses</label><span>{lose_count}</span></div>
        <div class="summary-item"><label>Win Rate</label><span>{win_rate:.1f}%</span></div>
        <div class="summary-item"><label>Total Profit</label><span>{total_profit:.2f}</span></div>
        <div class="summary-item"><label>Profit Factor</label><span>{profit_factor:.2f}</span></div>
        <div class="summary-item"><label>Initial Balance</label><span>{initial_balance:.2f}</span></div>
        <div class="summary-item"><label>Final Balance</label><span>{final_balance:.2f}</span></div>
        <div class="summary-item"><label>Lot Size</label><span>{lot}</span></div>
    </div>
    <div class="chart">{chart_html}</div>
    <h2>Trade List</h2>
    <table>
        <thead><tr><th>ID</th><th>Direction</th><th>Entry</th><th>Close</th><th>Result</th><th>Profit</th></tr></thead>
        <tbody>{trade_rows}</tbody>
    </table>
    <div class="footer">Generated by Engulfing Backtester</div>
</div>
</body>
</html>"""

    with open(HTML_REPORT, "w", encoding="utf-8") as f:
        f.write(html)
    console.print(f"[green]HTML report saved to {HTML_REPORT}[/green]")

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

    balance_input = console.input("Initial balance (e.g. 10000): ").strip()
    try:
        initial_balance = float(balance_input)
        if initial_balance <= 0:
            raise ValueError
    except:
        console.print("[red]Invalid balance. Using 10000.[/red]")
        initial_balance = 10000.0

    candle_input = console.input("Number of OHLC candles to backtest (e.g. 750): ").strip()
    try:
        candle_count = int(candle_input)
        if candle_count < 2:
            raise ValueError
    except:
        console.print("[red]Invalid number, using 500.[/red]")
        candle_count = 500

    run_backtest(symbol, tf_const, tf_input, lot, candle_count, initial_balance)

    mt5.shutdown()

if __name__ == "__main__":
    main()