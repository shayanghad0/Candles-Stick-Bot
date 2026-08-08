"""
Hammer (Pin Bar) Backtester (MetaTrader5) – with Balance & HTML Export
----------------------------------------------------------------------
- Loads historical OHLC data from MT5.
- Detects Hammer / Pin Bar patterns on each closed candle (optionally checks for prior downtrend).
- Simulates BUY trades with fixed TP/SL.
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
# Configuration – tweak as needed
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
TRADES_JSON = os.path.join(BASE_DIR, "hammer_trades.json")
SUMMARY_CSV = os.path.join(BASE_DIR, "hammer_summary.csv")
HTML_REPORT = os.path.join(BASE_DIR, "hammer_report.html")

TP_POINTS = 150
SL_POINTS = 200

# Optional slippage (in points) – set to 0 to disable
SLIPPAGE_POINTS = 0

# Hammer detection parameters (as in live bot)
MIN_LOWER_SHADOW_RATIO = 2.0      # lower shadow >= 2 * body
MAX_UPPER_SHADOW_RATIO = 0.15     # upper shadow <= 15% of total range
REQUIRE_DOWNTREND = True          # require prior 5 candles to show a downtrend (close[0] > close[-1])
DOWNTREND_LOOKBACK = 5            # number of prior candles to check for downtrend

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
# .env loading (custom block format)
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
# Hammer detection (as in live bot)
# ---------------------------------------------------------------------------
def is_hammer(candle, prior_candles=None,
              min_lower_shadow_ratio=MIN_LOWER_SHADOW_RATIO,
              max_upper_shadow_ratio=MAX_UPPER_SHADOW_RATIO,
              require_downtrend=REQUIRE_DOWNTREND,
              lookback=DOWNTREND_LOOKBACK):
    """
    Hammer (pin bar):
      - small real body near the TOP of the range
      - long lower shadow (>= min_lower_shadow_ratio * body)
      - little to no upper shadow (<= max_upper_shadow_ratio * total range)
      - optionally, preceded by a downtrend (last 'lookback' closes show decreasing trend)
    """
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    total_range = h - l
    if total_range <= 0:
        return False

    body = abs(c - o)
    lower_shadow = min(o, c) - l
    upper_shadow = h - max(o, c)

    # Avoid division by zero
    if body == 0:
        body = total_range * 0.01

    has_long_lower = lower_shadow >= min_lower_shadow_ratio * body
    has_small_upper = upper_shadow <= max_upper_shadow_ratio * total_range
    body_in_upper_half = min(o, c) >= l + total_range * 0.5

    if not (has_long_lower and has_small_upper and body_in_upper_half):
        return False

    if require_downtrend and prior_candles is not None and len(prior_candles) >= lookback:
        closes = [x["close"] for x in prior_candles[-lookback:]]
        # Downtrend: first close > last close (price decreased over lookback period)
        if not (closes[0] > closes[-1]):
            return False

    return True

# ---------------------------------------------------------------------------
# Simulated Trade (BUY only)
# ---------------------------------------------------------------------------
class SimulatedTrade:
    def __init__(self, trade_id, symbol, entry_price, entry_time, lot, point, tp_points, sl_points):
        self.id = trade_id
        self.symbol = symbol
        self.direction = "BUY"
        self.pattern = "Hammer"
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.lot = lot
        self.point = point
        self.tp_points = tp_points
        self.sl_points = sl_points
        self.tp_price = entry_price + tp_points * point
        self.sl_price = entry_price - sl_points * point
        self.status = "open"
        self.close_price = None
        self.close_time = None
        self.result = None  # "TP" or "SL"
        self.pnl_points = None
        self.pnl_percent = None
        self.profit_currency = None

    def check_hit(self, high, low, timestamp):
        if self.status != "open":
            return False
        # Hammer is a BUY signal, so we check for TP (high >= tp) or SL (low <= sl)
        if high >= self.tp_price:
            self.close_price = self.tp_price
            self.result = "TP"
            self.status = "closed"
            self.close_time = timestamp
            return True
        if low <= self.sl_price:
            self.close_price = self.sl_price
            self.result = "SL"
            self.status = "closed"
            self.close_time = timestamp
            return True
        return False

    def compute_pnl(self):
        if self.close_price is None:
            return None, None
        # BUY only
        pnl_points = (self.close_price - self.entry_price) / self.point
        pnl_percent = (pnl_points * self.point * self.lot) / (self.entry_price * self.lot) * 100
        self.pnl_points = round(pnl_points, 1)
        self.pnl_percent = round(pnl_percent, 3)
        return self.pnl_points, self.pnl_percent

    def compute_profit_currency(self):
        if self.close_price is None:
            return None
        # BUY only
        profit = mt5.order_calc_profit(
            mt5.ORDER_TYPE_BUY,
            self.symbol,
            self.lot,
            self.entry_price,
            self.close_price
        )
        if profit is None:
            profit = 0.0
        self.profit_currency = profit
        return profit

# ---------------------------------------------------------------------------
# Backtest Engine
# ---------------------------------------------------------------------------
def run_backtest(symbol, tf_const, tf_key, lot, candle_count, initial_balance):
    # Fetch historical data
    rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, candle_count)
    if rates is None or len(rates) < 3:
        console.print(f"[red]Failed to fetch data for {symbol} or not enough candles (need >=3).[/red]")
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
    equity_curve = []        # (time, equity)
    balance_curve = []       # (time, balance after closed trades)

    console.print(f"Starting backtest on {symbol} {tf_key} with {len(candles)} candles...")

    for i in track(range(1, len(candles)), description="Processing candles..."):
        # We need at least one prior candle for the hammer check (prior_candles may be empty for the first few)
        # For downtrend check, we need enough prior candles
        curr = candles[i]          # current candle (just closed)
        prior = candles[:i]        # all candles before curr (oldest to newest)

        # --- 1. Check existing open trades against current candle's high/low ---
        for trade in trades:
            if trade.status == "open":
                hit = trade.check_hit(curr["high"], curr["low"], curr["time"])
                if hit:
                    trade.compute_pnl()
                    profit = trade.compute_profit_currency()
                    if profit is not None:
                        balance += profit

        # --- 2. Detect Hammer pattern on the just-closed candle (curr) ---
        if is_hammer(curr, prior_candles=prior,
                     min_lower_shadow_ratio=MIN_LOWER_SHADOW_RATIO,
                     max_upper_shadow_ratio=MAX_UPPER_SHADOW_RATIO,
                     require_downtrend=REQUIRE_DOWNTREND,
                     lookback=DOWNTREND_LOOKBACK):
            # Entry at the close of the signal candle
            entry_price = curr["close"]

            # Optional slippage (BUY: add slippage)
            if SLIPPAGE_POINTS != 0:
                entry_price += SLIPPAGE_POINTS * point

            trade_id_counter += 1
            new_trade = SimulatedTrade(
                trade_id=trade_id_counter,
                symbol=symbol,
                entry_price=entry_price,
                entry_time=curr["time"],
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
                profit_now = mt5.order_calc_profit(
                    mt5.ORDER_TYPE_BUY,
                    t.symbol,
                    t.lot,
                    t.entry_price,
                    curr["close"]  # use current close as hypothetical exit
                )
                if profit_now is not None:
                    floating_pnl += profit_now
        equity = balance + floating_pnl
        equity_curve.append((curr["time"], equity))
        balance_curve.append((curr["time"], balance))

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
                balance_curve[-1] = (balance_curve[-1][0], balance)

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
# Summary
# ---------------------------------------------------------------------------
def print_summary(trades, symbol, timeframe, lot, initial_balance, final_balance):
    closed_trades = [t for t in trades if t.status == "closed"]
    total_trades = len(closed_trades)
    if total_trades == 0:
        console.print("[yellow]No trades were opened.[/yellow]")
        return

    winning = [t for t in closed_trades if t.result == "TP"]
    losing = [t for t in closed_trades if t.result == "SL"]
    win_count = len(winning)
    lose_count = len(losing)
    win_rate = win_count / total_trades * 100 if total_trades else 0

    total_profit = sum(t.profit_currency for t in closed_trades if t.profit_currency is not None)
    profit_factor = abs(sum(t.profit_currency for t in winning if t.profit_currency is not None)) / abs(sum(t.profit_currency for t in losing if t.profit_currency is not None)) if losing else float('inf')

    table = Table(title=f"Hammer Backtest Summary – {symbol} {timeframe}", box=ROUNDED)
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
# Export functions
# ---------------------------------------------------------------------------
def export_trades(trades, symbol, timeframe):
    trade_list = []
    for t in trades:
        trade_list.append({
            "id": t.id,
            "symbol": t.symbol,
            "pattern": t.pattern,
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
        cols = ["id", "pattern", "direction", "entry_price", "tp_price", "sl_price",
                "result", "pnl_points", "profit_currency"]
        df_export = df[cols] if all(c in df.columns for c in cols) else df
        df_export.to_csv(SUMMARY_CSV, index=False)
        console.print(f"[green]Summary CSV saved to {SUMMARY_CSV}[/green]")

# ---------------------------------------------------------------------------
# HTML Report Generator
# ---------------------------------------------------------------------------
def generate_html_report(trades, equity_curve, balance_curve, symbol, timeframe, lot, initial_balance, final_balance):
    closed = [t for t in trades if t.status == "closed"]
    total = len(closed)
    if total == 0:
        console.print("[yellow]No trades to report.[/yellow]")
        return

    winning = [t for t in closed if t.result == "TP"]
    losing = [t for t in closed if t.result == "SL"]
    win_count = len(winning)
    lose_count = len(losing)
    win_rate = win_count / total * 100 if total else 0
    total_profit = sum(t.profit_currency for t in closed if t.profit_currency is not None)
    profit_factor = abs(sum(t.profit_currency for t in winning if t.profit_currency is not None)) / abs(sum(t.profit_currency for t in losing if t.profit_currency is not None)) if losing else float('inf')

    # ---- Equity & Drawdown Charts ----
    if equity_curve:
        df_eq = pd.DataFrame(equity_curve, columns=["time", "equity"])
        df_eq.set_index("time", inplace=True)
        running_max = df_eq["equity"].cummax()
        drawdown = (running_max - df_eq["equity"]) / running_max * 100
        drawdown.fillna(0, inplace=True)

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
        ax1.plot(df_eq.index, df_eq["equity"], label="Equity", color="blue", linewidth=1.5)
        ax1.set_title(f"Hammer Equity Curve – {symbol} {timeframe}")
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
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=120, bbox_inches="tight")
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode("utf-8")
        plt.close()
        chart_html = f'<img src="data:image/png;base64,{img_base64}" alt="Equity & Drawdown" style="max-width:100%;">'
    else:
        chart_html = "<p>No equity data available.</p>"

    # ---- Trade table ----
    trade_rows = ""
    for t in closed:
        profit = t.profit_currency if t.profit_currency is not None else 0
        color = "green" if profit >= 0 else "red"
        close_str = f"{t.close_price:.5f}" if t.close_price is not None else "-"
        trade_rows += f"""
        <tr>
            <td>{t.id}</td>
            <td>{t.pattern}</td>
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
    <title>Hammer Backtest Report – {symbol} {timeframe}</title>
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
    <h1>Hammer Backtest Report – {symbol} {timeframe}</h1>
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
        <thead><tr><th>ID</th><th>Pattern</th><th>Direction</th><th>Entry</th><th>Close</th><th>Result</th><th>Profit</th></tr></thead>
        <tbody>{trade_rows}</tbody>
    </table>
    <div class="footer">Generated by Hammer Backtester</div>
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
        if candle_count < 3:
            raise ValueError
    except:
        console.print("[red]Invalid number, using 500.[/red]")
        candle_count = 500

    run_backtest(symbol, tf_const, tf_input, lot, candle_count, initial_balance)

    mt5.shutdown()

if __name__ == "__main__":
    main()