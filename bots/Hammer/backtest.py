"""
Hammer (Pin Bar) Backtester (MetaTrader5) – Optimized Strategy
---------------------------------------------------------------
Advanced Features:
    - Multi-EMA trend alignment (fast + slow EMA)
    - RSI confirmation (avoid overbought entries – BUY only)
    - ATR-based dynamic TP/SL with wider multipliers
    - Trailing stop loss
    - Risk-per-trade lot sizing
    - Trade cooldown (no rapid-fire entries)
    - Max drawdown circuit breaker
    - Liquidation detection (balance <= 0)

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
import numpy as np
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
TRADES_JSON = os.path.join(BASE_DIR, "hammer_trades.json")
SUMMARY_CSV = os.path.join(BASE_DIR, "hammer_summary.csv")
HTML_REPORT = os.path.join(BASE_DIR, "hammer_report.html")

# --- Strategy Mode ---
USE_ADVANCED = True

# --- Classic Settings ---
TP_POINTS = 150
SL_POINTS = 200

# --- Advanced Settings ---
ATR_PERIOD = 14
ATR_TP_MULTIPLIER = 2.0       # Tight TP — get out fast
ATR_SL_MULTIPLIER = 3.0       # Wide SL — room to breathe
ATR_MIN_BODY_RATIO = 0.3      # Hammer body must be >= 30% of ATR
RISK_PERCENT = 1.0
TRAILING_ACTIVATION = 0.4     # Activate trailing at 40% of TP
TRAILING_STEP = 50
MAX_DRAWDOWN_PERCENT = 20.0
TRADE_COOLDOWN = 3            # Min candles between trades

# --- Trend Filter ---
EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

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
# .env loading
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
# Technical Indicators
# ---------------------------------------------------------------------------
def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i-1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period

def calc_ema(closes, period):
    if len(closes) < period:
        return None
    ema = sum(closes[:period]) / period
    multiplier = 2 / (period + 1)
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def get_trend(candles, ema_fast=20, ema_slow=50):
    closes = [c["close"] for c in candles]
    ema_f = calc_ema(closes, ema_fast)
    ema_s = calc_ema(closes, ema_slow)
    if ema_f is None or ema_s is None:
        return "neutral"
    if ema_f > ema_s * 1.0005:
        return "bullish"
    elif ema_f < ema_s * 0.9995:
        return "bearish"
    return "neutral"

# ---------------------------------------------------------------------------
# Hammer detection (as in live bot) with ATR quality filter
# ---------------------------------------------------------------------------
def is_hammer(candle, prior_candles=None,
              min_lower_shadow_ratio=MIN_LOWER_SHADOW_RATIO,
              max_upper_shadow_ratio=MAX_UPPER_SHADOW_RATIO,
              require_downtrend=REQUIRE_DOWNTREND,
              lookback=DOWNTREND_LOOKBACK,
              atr=None):
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

    # Quality filter: hammer body must be significant relative to ATR
    if atr is not None and atr > 0:
        if body < atr * ATR_MIN_BODY_RATIO:
            return False  # Skip tiny hammer candles

    has_long_lower = lower_shadow >= min_lower_shadow_ratio * body
    has_small_upper = upper_shadow <= max_upper_shadow_ratio * total_range
    body_in_upper_half = min(o, c) >= l + total_range * 0.5

    if not (has_long_lower and has_small_upper and body_in_upper_half):
        return False

    if require_downtrend and prior_candles is not None and len(prior_candles) >= lookback:
        closes = [x["close"] for x in prior_candles[-lookback:]]
        if not (closes[0] > closes[-1]):
            return False

    return True

# ---------------------------------------------------------------------------
# Simulated Trade (BUY only) – with trailing stop support
# ---------------------------------------------------------------------------
class SimulatedTrade:
    def __init__(self, trade_id, symbol, entry_price, entry_time, lot, point,
                 tp_points, sl_points, use_trailing=False,
                 trailing_activation=0.5, trailing_step=100):
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
        self.result = None
        self.pnl_points = None
        self.pnl_percent = None
        self.profit_currency = None
        self.use_trailing = use_trailing
        self.trailing_activation = trailing_activation
        self.trailing_step = trailing_step
        self.trailing_activated = False
        self.best_price = entry_price

    def update_trailing(self, high, low):
        if not self.use_trailing or self.status != "open":
            return
        tp_dist = abs(self.tp_price - self.entry_price)
        activation_dist = tp_dist * self.trailing_activation
        if high > self.best_price:
            self.best_price = high
        profit_dist = self.best_price - self.entry_price
        if profit_dist >= activation_dist:
            self.trailing_activated = True
            new_sl = self.best_price - self.trailing_step * self.point
            if new_sl > self.sl_price:
                self.sl_price = new_sl

    def check_hit(self, high, low, timestamp):
        if self.status != "open":
            return False
        self.update_trailing(high, low)
        # Hammer is a BUY signal
        if high >= self.tp_price:
            self.close_price = self.tp_price
            self.result = "tp"
            self.status = "closed"
            self.close_time = timestamp
            return True
        if low <= self.sl_price:
            self.close_price = self.sl_price
            # Trailing stop may have moved into profit
            if self.trailing_activated and self.close_price > self.entry_price:
                self.result = "tp"
            else:
                self.result = "sl"
            self.status = "closed"
            self.close_time = timestamp
            return True
        return False

    def compute_pnl(self):
        if self.close_price is None:
            return None, None
        pnl_points = (self.close_price - self.entry_price) / self.point
        pnl_percent = (pnl_points * self.point * self.lot) / (self.entry_price * self.lot) * 100
        self.pnl_points = round(pnl_points, 1)
        self.pnl_percent = round(pnl_percent, 3)
        return self.pnl_points, self.pnl_percent

    def compute_profit_currency(self):
        if self.close_price is None:
            return None
        profit = mt5.order_calc_profit(
            mt5.ORDER_TYPE_BUY,
            self.symbol,
            self.lot,
            self.entry_price,
            self.close_price
        )
        self.profit_currency = profit
        return profit

# ---------------------------------------------------------------------------
# Backtest Engine
# ---------------------------------------------------------------------------
def run_backtest(symbol, tf_const, tf_key, lot, candle_count, initial_balance):
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
    equity_curve = []
    balance_curve = []
    liquidated = False
    max_dd_hit = False
    running_max_balance = initial_balance
    last_trade_candle = -TRADE_COOLDOWN  # Allow first trade immediately

    strategy_label = "Advanced" if USE_ADVANCED else "Classic"
    console.print(f"Starting {strategy_label} backtest on {symbol} {tf_key} with {len(candles)} candles...")

    for i in track(range(1, len(candles)), description="Processing candles..."):
        prev = candles[i-1]
        curr = candles[i]
        curr_time = curr["time"]
        prior = candles[:i]

        # --- 1. Check existing open trades ---
        for trade in trades:
            if trade.status == "open":
                hit = trade.check_hit(curr["high"], curr["low"], curr_time)
                if hit:
                    trade.compute_pnl()
                    profit = trade.compute_profit_currency()
                    if profit is not None:
                        balance += profit

        # --- Liquidation check ---
        if balance <= 0:
            console.print(f"[bold red]LIQUIDATED! Balance reached {balance:.2f} at candle {i}[/bold red]")
            for t in trades:
                if t.status == "open":
                    t.close_price = curr["close"]
                    t.close_time = curr_time
                    t.result = "liquidated"
                    t.status = "closed"
                    t.compute_pnl()
                    t.compute_profit_currency()
            liquidated = True
            break

        # --- Max drawdown check ---
        if balance > running_max_balance:
            running_max_balance = balance
        current_dd = (running_max_balance - balance) / running_max_balance * 100 if running_max_balance > 0 else 0
        if current_dd >= MAX_DRAWDOWN_PERCENT:
            console.print(f"[bold red]MAX DRAWDOWN {current_dd:.1f}% hit! Stopping.[/bold red]")
            for t in trades:
                if t.status == "open":
                    t.close_price = curr["close"]
                    t.close_time = curr_time
                    t.result = "dd_stop"
                    t.status = "closed"
                    t.compute_pnl()
                    t.compute_profit_currency()
                    profit = t.profit_currency
                    if profit is not None:
                        balance += profit
            max_dd_hit = True
            break

        # --- 2. Check for new Hammer signal ---
        if USE_ADVANCED:
            # Cooldown check
            if (i - last_trade_candle) < TRADE_COOLDOWN:
                pass  # Skip signal check during cooldown
            else:
                atr = calc_atr(candles[:i+1], ATR_PERIOD)
                if is_hammer(curr, prior_candles=prior,
                             min_lower_shadow_ratio=MIN_LOWER_SHADOW_RATIO,
                             max_upper_shadow_ratio=MAX_UPPER_SHADOW_RATIO,
                             require_downtrend=REQUIRE_DOWNTREND,
                             lookback=DOWNTREND_LOOKBACK,
                             atr=atr):

                    # Trend filter: EMA alignment – Hammer is BUY-only, filter out bearish
                    trend = get_trend(candles[:i+1], EMA_FAST, EMA_SLOW)
                    if trend == "bearish":
                        pass  # Skip bearish trend
                    else:
                        # RSI filter – BUY only, don't buy when overbought
                        closes = [c["close"] for c in candles[:i+1]]
                        rsi = calc_rsi(closes, RSI_PERIOD)
                        if rsi is not None and rsi > RSI_OVERBOUGHT:
                            pass  # Don't buy when overbought
                        else:
                            entry_price = curr["close"]

                            # ATR-based TP/SL
                            if atr is not None:
                                tp_pts = int(atr / point * ATR_TP_MULTIPLIER)
                                sl_pts = int(atr / point * ATR_SL_MULTIPLIER)
                            else:
                                tp_pts = TP_POINTS
                                sl_pts = SL_POINTS

                            # Risk-per-trade sizing
                            risk_amount = balance * (RISK_PERCENT / 100)
                            sl_distance = sl_pts * point
                            if sl_distance > 0:
                                contract_size = symbol_info.trade_contract_size if symbol_info.trade_contract_size else 100
                                calc_lot = round(risk_amount / (sl_distance * contract_size), 2)
                                calc_lot = max(0.01, min(calc_lot, 10.0))
                            else:
                                calc_lot = lot
                            trade_lot = calc_lot

                            trade_id_counter += 1
                            new_trade = SimulatedTrade(
                                trade_id=trade_id_counter,
                                symbol=symbol,
                                entry_price=entry_price,
                                entry_time=curr_time,
                                lot=trade_lot,
                                point=point,
                                tp_points=tp_pts,
                                sl_points=sl_pts,
                                use_trailing=True,
                                trailing_activation=TRAILING_ACTIVATION,
                                trailing_step=TRAILING_STEP
                            )
                            trades.append(new_trade)
                            last_trade_candle = i
        else:
            # Classic mode
            if is_hammer(curr, prior_candles=prior,
                         min_lower_shadow_ratio=MIN_LOWER_SHADOW_RATIO,
                         max_upper_shadow_ratio=MAX_UPPER_SHADOW_RATIO,
                         require_downtrend=REQUIRE_DOWNTREND,
                         lookback=DOWNTREND_LOOKBACK):
                entry_price = curr["close"]
                trade_id_counter += 1
                new_trade = SimulatedTrade(
                    trade_id=trade_id_counter,
                    symbol=symbol,
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
                profit_now = mt5.order_calc_profit(
                    mt5.ORDER_TYPE_BUY,
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
    if not liquidated and not max_dd_hit:
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

    if equity_curve:
        equity_curve[-1] = (equity_curve[-1][0], balance)

    # -----------------------------------------------------------------------
    # Summary and Export
    # -----------------------------------------------------------------------
    console.print("\n[bold green]Backtest completed![/bold green]")
    if liquidated:
        console.print("[bold red]ACCOUNT LIQUIDATED DURING BACKTEST[/bold red]")
    elif max_dd_hit:
        console.print("[bold red]MAX DRAWDOWN LIMIT REACHED[/bold red]")

    print_summary(trades, symbol, tf_key, lot, initial_balance, balance, liquidated, max_dd_hit)
    export_trades(trades, symbol, tf_key, liquidated, max_dd_hit)
    generate_html_report(trades, equity_curve, balance_curve, symbol, tf_key, lot,
                         initial_balance, balance, liquidated, max_dd_hit)
    return trades, equity_curve, balance_curve

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def print_summary(trades, symbol, timeframe, lot, initial_balance, final_balance,
                  liquidated=False, max_dd_hit=False):
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

    # Average win / average loss
    avg_win = np.mean([t.profit_currency for t in winning if t.profit_currency is not None]) if winning else 0
    avg_loss = np.mean([t.profit_currency for t in losing if t.profit_currency is not None]) if losing else 0
    rr_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')

    # Max consecutive losses
    max_consec_loss = 0
    current_consec = 0
    for t in closed_trades:
        if t.result == "sl":
            current_consec += 1
            max_consec_loss = max(max_consec_loss, current_consec)
        else:
            current_consec = 0

    status_str = ""
    if liquidated:
        status_str = " [bold red]>>> LIQUIDATED <<<[/bold red]"
    elif max_dd_hit:
        status_str = " [bold red]>>> MAX DRAWDOWN STOP <<<[/bold red]"

    table = Table(title=f"Hammer Backtest Summary – {symbol} {timeframe}{status_str}", box=ROUNDED)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    if liquidated:
        table.add_row("Status", "[bold red]LIQUIDATED[/bold red]")
    elif max_dd_hit:
        table.add_row("Status", "[bold red]MAX DRAWDOWN STOP[/bold red]")
    else:
        table.add_row("Status", "Completed")
    table.add_row("Total Trades", str(total_trades))
    table.add_row("Wins", str(win_count))
    table.add_row("Losses", str(lose_count))
    table.add_row("Win Rate", f"{win_rate:.2f}%")
    table.add_row("Avg Win", f"{avg_win:.2f}")
    table.add_row("Avg Loss", f"{avg_loss:.2f}")
    table.add_row("Risk:Reward", f"1:{rr_ratio:.2f}")
    table.add_row("Max Consec Losses", str(max_consec_loss))
    table.add_row("Total Profit (currency)", f"{total_profit:.2f}")
    table.add_row("Profit Factor", f"{profit_factor:.2f}")
    table.add_row("Initial Balance", f"{initial_balance:.2f}")
    table.add_row("Final Balance", f"{final_balance:.2f}")
    table.add_row("Return %", f"{((final_balance - initial_balance) / initial_balance * 100):.2f}%")
    table.add_row("Lot size", str(lot))
    console.print(table)

# ---------------------------------------------------------------------------
# Export functions (JSON, CSV)
# ---------------------------------------------------------------------------
def export_trades(trades, symbol, timeframe, liquidated=False, max_dd_hit=False):
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

    export_data = {
        "symbol": symbol,
        "timeframe": timeframe,
        "liquidated": liquidated,
        "max_dd_hit": max_dd_hit,
        "trades": trade_list
    }

    with open(TRADES_JSON, "w") as f:
        json.dump(export_data, f, indent=2)
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
def generate_html_report(trades, equity_curve, balance_curve, symbol, timeframe, lot,
                         initial_balance, final_balance, liquidated=False, max_dd_hit=False):
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

    avg_win = np.mean([t.profit_currency for t in winning if t.profit_currency is not None]) if winning else 0
    avg_loss = np.mean([t.profit_currency for t in losing if t.profit_currency is not None]) if losing else 0
    rr_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')

    max_consec_loss = 0
    current_consec = 0
    for t in closed:
        if t.result == "sl":
            current_consec += 1
            max_consec_loss = max(max_consec_loss, current_consec)
        else:
            current_consec = 0

    if liquidated:
        status_badge = '<span style="color:white;background:red;padding:4px 12px;border-radius:4px;font-weight:bold;">LIQUIDATED</span>'
    elif max_dd_hit:
        status_badge = '<span style="color:white;background:orange;padding:4px 12px;border-radius:4px;font-weight:bold;">MAX DRAWDOWN STOP</span>'
    else:
        status_badge = '<span style="color:white;background:green;padding:4px 12px;border-radius:4px;">Completed</span>'

    # ---- Equity & Drawdown Charts ----
    if equity_curve:
        df_eq = pd.DataFrame(equity_curve, columns=["time", "equity"])
        df_eq.set_index("time", inplace=True)
        running_max = df_eq["equity"].cummax()
        drawdown = (running_max - df_eq["equity"]) / running_max * 100
        drawdown.fillna(0, inplace=True)

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
        ax1.plot(df_eq.index, df_eq["equity"], label="Equity", color="blue", linewidth=1.5)
        ax1.axhline(y=initial_balance, color='gray', linestyle=':', linewidth=1, alpha=0.5, label='Initial Balance')
        if liquidated:
            ax1.axhline(y=0, color='red', linestyle='--', linewidth=2, label='Liquidation Line')
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
        result_style = ""
        if t.result == "liquidated":
            result_style = 'style="color:white;background:red;padding:2px 6px;border-radius:3px;"'
        elif t.result == "dd_stop":
            result_style = 'style="color:white;background:orange;padding:2px 6px;border-radius:3px;"'
        trade_rows += f"""
        <tr>
            <td>{t.id}</td>
            <td>{t.pattern}</td>
            <td>{t.direction}</td>
            <td>{t.entry_price:.5f}</td>
            <td>{close_str}</td>
            <td {result_style}>{t.result}</td>
            <td>{t.lot}</td>
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
    <h1>Hammer Backtest Report – {symbol} {timeframe} {status_badge}</h1>
    <div class="summary">
        <div class="summary-item"><label>Total Trades</label><span>{total}</span></div>
        <div class="summary-item"><label>Wins</label><span>{win_count}</span></div>
        <div class="summary-item"><label>Losses</label><span>{lose_count}</span></div>
        <div class="summary-item"><label>Win Rate</label><span>{win_rate:.1f}%</span></div>
        <div class="summary-item"><label>Avg Win</label><span>{avg_win:.2f}</span></div>
        <div class="summary-item"><label>Avg Loss</label><span>{avg_loss:.2f}</span></div>
        <div class="summary-item"><label>R:R Ratio</label><span>1:{rr_ratio:.2f}</span></div>
        <div class="summary-item"><label>Max Consec Losses</label><span>{max_consec_loss}</span></div>
        <div class="summary-item"><label>Total Profit</label><span>{total_profit:.2f}</span></div>
        <div class="summary-item"><label>Profit Factor</label><span>{profit_factor:.2f}</span></div>
        <div class="summary-item"><label>Initial Balance</label><span>{initial_balance:.2f}</span></div>
        <div class="summary-item"><label>Final Balance</label><span>{final_balance:.2f}</span></div>
        <div class="summary-item"><label>Return</label><span>{((final_balance - initial_balance) / initial_balance * 100):.2f}%</span></div>
        <div class="summary-item"><label>Lot Size</label><span>{lot}</span></div>
    </div>
    <div class="chart">{chart_html}</div>
    <h2>Trade List</h2>
    <table>
        <thead><tr><th>ID</th><th>Pattern</th><th>Direction</th><th>Entry</th><th>Close</th><th>Result</th><th>Lot</th><th>Profit</th></tr></thead>
        <tbody>{trade_rows}</tbody>
    </table>
    <div class="footer">Generated by Hammer Backtester (Optimized)</div>
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
