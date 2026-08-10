"""
MT5 Doji Real Trade Bot
========================
- Detects Doji patterns (Dragonfly / Gravestone)
- Opens REAL market orders (no TP/SL on order)
- Monitors price tick-by-tick and closes when TP (120 pts) or SL (175 pts) hit
- Asks for lot size at startup
"""

import os, re, sys, json, time, math
from datetime import datetime, timedelta
from pathlib import Path

try:
    import MetaTrader5 as mt5
except ImportError:
    print("MetaTrader5 package not installed or not supported on this OS.")
    sys.exit(1)

import pandas as pd
try:
    import mplfinance as mpf
except ImportError:
    mpf = None

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, IntPrompt, FloatPrompt
from rich.live import Live
from rich.text import Text
from rich.console import Group
from rich import box

console = Console()

BASE_DIR = Path(__file__).resolve().parent
CHARTS_DIR = BASE_DIR / "charts"
DATA_FILE = BASE_DIR / "trades_history.json"
CHARTS_DIR.mkdir(exist_ok=True)

# ====== NEW: TP/SL in points ======
TP_POINTS = 120   # changed from 150 to 120 as requested
SL_POINTS = 175   # changed from 200 to 175

CANDLE_HISTORY = 30
CHART_DISPLAY_CANDLES = 60
CHART_ROWS = 20
LOG_LINES = 8
TICK_SECONDS = 0.01   # <--- 10 ms refresh (as close as possible to 0.01 sec)

TIMEFRAMES = {
    "M1": (mt5.TIMEFRAME_M1, 1),
    "M5": (mt5.TIMEFRAME_M5, 5),
    "M15": (mt5.TIMEFRAME_M15, 15),
    "M30": (mt5.TIMEFRAME_M30, 30),
    "H1": (mt5.TIMEFRAME_H1, 60),
    "H4": (mt5.TIMEFRAME_H4, 240),
    "D1": (mt5.TIMEFRAME_D1, 1440),
}

DOJI_BODY_RATIO = 0.10
LONG_WICK_RATIO = 0.60
SHORT_WICK_RATIO = 0.15

# --------------------------------------------------------------------------
# Load accounts from .env (same as before)
# --------------------------------------------------------------------------
def load_accounts():
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return []
    text = env_path.read_text(encoding="utf-8")
    blocks = re.split(r"^\s*=+\s*$", text, flags=re.MULTILINE)
    accounts = []
    for block in blocks:
        fields = {}
        for line in block.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key and value:
                fields[key] = value
        if "login" in fields and "password" in fields:
            accounts.append({
                "name": fields.get("name", f"Account {len(accounts)+1}"),
                "type": fields.get("type", ""),
                "server": fields.get("server", ""),
                "login": fields.get("login", ""),
                "password": fields.get("password", ""),
                "investor": fields.get("investor", ""),
                "typeacc": fields.get("typeacc", fields.get("type", "")),
            })
    return accounts

def pick_account(accounts):
    table = Table(title="Available MT5 Accounts", box=box.ROUNDED)
    table.add_column("#", justify="right")
    table.add_column("Name")
    table.add_column("Server")
    table.add_column("Login")
    table.add_column("Type")
    for idx, acc in enumerate(accounts, start=1):
        table.add_row(str(idx), acc["name"], acc["server"], acc["login"], acc["typeacc"])
    console.print(table)
    choice = IntPrompt.ask(
        "Select an account",
        choices=[str(i) for i in range(1, len(accounts)+1)],
    )
    return accounts[choice-1]

def connect(account):
    ok = mt5.initialize(
        login=int(account["login"]),
        password=account["password"],
        server=account["server"],
    )
    if not ok:
        console.print(f"[red]mt5.initialize() failed: {mt5.last_error()}[/red]")
        sys.exit(1)
    info = mt5.account_info()
    console.print(Panel.fit(
        f"Connected as [bold]{account['name']}[/bold] "
        f"(login {account['login']} @ {account['server']})\n"
        f"Balance: {info.balance} {info.currency}" if info else "Connected.",
        title="MT5 Connection",
        border_style="green",
    ))

# --------------------------------------------------------------------------
# Candle helpers
# --------------------------------------------------------------------------
def fetch_candles(symbol, timeframe, count=CANDLE_HISTORY):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df

def next_close_boundary(timeframe_minutes, buffer_seconds=2, after=None):
    now = after or datetime.now()
    epoch_minutes = now.hour * 60 + now.minute
    next_boundary_minutes = (epoch_minutes // timeframe_minutes + 1) * timeframe_minutes
    next_close = now.replace(second=0, microsecond=0) + timedelta(
        minutes=next_boundary_minutes - epoch_minutes
    )
    return next_close + timedelta(seconds=buffer_seconds)

# --------------------------------------------------------------------------
# Pattern detection (unchanged)
# --------------------------------------------------------------------------
def classify_candle(o, h, l, c):
    rng = h - l
    if rng <= 0:
        return None
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    body_ratio = body / rng
    upper_ratio = upper_wick / rng
    lower_ratio = lower_wick / rng
    if body_ratio > DOJI_BODY_RATIO:
        return None
    if lower_ratio >= LONG_WICK_RATIO and upper_ratio <= SHORT_WICK_RATIO:
        return "dragonfly_doji"
    if upper_ratio >= LONG_WICK_RATIO and lower_ratio <= SHORT_WICK_RATIO:
        return "gravestone_doji"
    return "doji"

PATTERN_DIRECTION = {
    "dragonfly_doji": "BUY",
    "gravestone_doji": "SELL",
    "doji": None,
}

# --------------------------------------------------------------------------
# Persistence (unchanged)
# --------------------------------------------------------------------------
def load_history():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except:
            return []
    return []

def save_history(trades):
    DATA_FILE.write_text(json.dumps(trades, indent=2, default=str))

# --------------------------------------------------------------------------
# Charting (unchanged)
# --------------------------------------------------------------------------
def _price_decimals(top, bottom):
    magnitude = max(abs(top), abs(bottom), 1e-9)
    if magnitude >= 1000:
        return 2
    if magnitude >= 100:
        return 3
    if magnitude >= 10:
        return 4
    return 5

def build_ascii_candles(df, rows=CHART_ROWS, candle_gap=1):
    view = df.reset_index(drop=True)
    n = len(view)
    if n == 0:
        return Text("no data yet", style="dim")
    highs = view["high"].tolist()
    lows = view["low"].tolist()
    top = max(highs)
    bottom = min(lows)
    price_range = (top - bottom) or (top * 0.0001 or 1.0)
    sub_rows = rows * 2
    def subrow_for_price(p):
        frac = (top - p) / price_range
        sr = frac * (sub_rows - 1)
        return min(sub_rows - 1, max(0, int(round(sr))))
    col_width = 1 + candle_gap
    total_cols = n * col_width - candle_gap
    grid = [[" "] * total_cols for _ in range(rows)]
    styles = [[""] * total_cols for _ in range(rows)]
    for i in range(n):
        o = float(view.loc[i, "open"])
        h = float(view.loc[i, "high"])
        l = float(view.loc[i, "low"])
        c = float(view.loc[i, "close"])
        bullish = c >= o
        color = "bright_green" if bullish else "bright_red"
        col = i * col_width
        wick_top_sr = subrow_for_price(h)
        wick_bot_sr = subrow_for_price(l)
        body_top_sr = subrow_for_price(max(o, c))
        body_bot_sr = subrow_for_price(min(o, c))
        if body_top_sr == body_bot_sr:
            body_top_sr = max(0, body_top_sr - 1)
        wick_subrows = set(range(wick_top_sr, wick_bot_sr + 1))
        body_subrows = set(range(body_top_sr, body_bot_sr + 1))
        for r in range(rows):
            top_sr, bot_sr = r * 2, r * 2 + 1
            top_body = top_sr in body_subrows
            bot_body = bot_sr in body_subrows
            top_wick = top_sr in wick_subrows
            bot_wick = bot_sr in wick_subrows
            if top_body and bot_body:
                ch = "\u2588"
            elif top_body:
                ch = "\u2580"
            elif bot_body:
                ch = "\u2584"
            elif top_wick or bot_wick:
                ch = "\u2502"
            else:
                continue
            grid[r][col] = ch
            styles[r][col] = color
    dp = _price_decimals(top, bottom)
    label_width = 10
    text = Text()
    for r in range(rows):
        price_at_row = top - (r / (rows - 1)) * price_range if rows > 1 else top
        text.append(f"{price_at_row:>{label_width-1}.{dp}f} ", style="dim")
        for col in range(total_cols):
            ch = grid[r][col]
            style = styles[r][col] or None
            text.append(ch, style=style)
        text.append("\n")
    text.append(" " * label_width)
    times = view["time"].tolist()
    label_every_candles = max(1, n // 5)
    axis_row = [" "] * total_cols
    for i in range(0, n, label_every_candles):
        stamp = times[i].strftime("%H:%M")
        col = i * col_width
        for j, ch in enumerate(stamp):
            if col + j < total_cols:
                axis_row[col + j] = ch
    text.append("".join(axis_row), style="dim")
    return text

def save_chart(df, trade, filename, extra_lines=None):
    if mpf is None:
        return None
    plot_df = df.set_index("time")[["open", "high", "low", "close", "tick_volume"]]
    plot_df.columns = ["Open", "High", "Low", "Close", "Volume"]
    hlines = {}
    if extra_lines:
        hlines = {
            "hlines": list(extra_lines.values()),
            "colors": ["blue", "green", "red"][:len(extra_lines)],
            "linestyle": "--",
        }
    path = CHARTS_DIR / filename
    mpf.plot(
        plot_df,
        type="candle",
        style="charles",
        title=f"{trade['symbol']} {trade['timeframe']} - {trade['pattern']}",
        volume=False,
        hlines=hlines if extra_lines else None,
        savefig=dict(fname=str(path), dpi=120, pad_inches=0.2),
    )
    return str(path)

def build_dashboard(symbol, tf_name, chart_df, open_trades, closed_trades, log_lines, next_close_at):
    header = Text.from_markup(
        f"[bold]{symbol}[/bold]  timeframe=[bold]{tf_name}[/bold]  "
        f"now={datetime.now().strftime('%H:%M:%S')}  "
        f"next candle close={next_close_at.strftime('%H:%M:%S')}"
    )
    chart_panel = Panel(
        build_ascii_candles(chart_df),
        title=f"{symbol} [{tf_name}] - last {min(len(chart_df), CHART_DISPLAY_CANDLES)} candles",
        border_style="cyan",
        expand=False,
    )
    open_table = Table(title="Open Real Positions", box=box.SIMPLE_HEAVY, expand=True)
    for col in ["Ticket", "Pattern", "Dir", "Lot", "Entry", "TP", "SL", "Open Time"]:
        open_table.add_column(col)
    for t in open_trades:
        open_table.add_row(
            str(t.get("ticket", "N/A")), t["pattern"], t["direction"],
            str(t.get("lot", 0)), f"{t['entry_price']:.5f}",
            f"{t['tp']:.5f}", f"{t['sl']:.5f}", t["open_time"],
        )
    closed_table = Table(title="Closed Positions (last 8)", box=box.SIMPLE_HEAVY, expand=True)
    for col in ["Ticket", "Result", "Entry", "Close", "PnL pts", "PnL %"]:
        closed_table.add_column(col)
    for t in closed_trades[-8:]:
        closed_table.add_row(
            str(t.get("ticket", "N/A")), t["status"], f"{t['entry_price']:.5f}",
            f"{t['close_price']:.5f}", str(t["pnl_points"]), f"{t['pnl_percent']}%",
        )
    log_text = Text("\n".join(log_lines) or "...", style="dim")
    return Panel(
        Group(header, chart_panel, open_table, closed_table, Panel(log_text, title="Log", border_style="grey50")),
        title="MT5 Doji Real Trade Bot",
        border_style="magenta",
    )

# ==========================================================================
# NEW: REAL TRADE FUNCTIONS
# ==========================================================================
def real_open_trade(symbol, timeframe_name, pattern, direction, signal_candle, df, history, lot):
    """Opens a REAL market order with no TP/SL."""
    info = mt5.symbol_info(symbol)
    if info is None:
        console.print(f"[red]Symbol {symbol} not found[/red]")
        return None
    point = info.point

    # Determine price: BUY -> ask, SELL -> bid
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        console.print(f"[red]Cannot get tick for {symbol}[/red]")
        return None

    if direction == "BUY":
        price = tick.ask
        order_type = mt5.ORDER_TYPE_BUY
    else:
        price = tick.bid
        order_type = mt5.ORDER_TYPE_SELL

    # Build request
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot),
        "type": order_type,
        "price": price,
        "deviation": 20,           # allowed slippage
        "magic": 123456,
        "comment": "Doji signal",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        console.print(f"[red]Order failed: {result.comment} (retcode {result.retcode})[/red]")
        return None

    # Get the position ticket from the result (order ticket is result.order)
    # But we need the position ticket; we can get it from the deal list in result.
    # However, we can simply fetch the open position for this symbol.
    # We'll assume only one position for this symbol.
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        console.print("[red]Position not found after order send[/red]")
        return None
    pos = positions[0]  # take first (should be the one we just opened)
    ticket = pos.ticket

    entry = pos.price_open

    # Calculate TP/SL prices
    if direction == "BUY":
        tp = entry + TP_POINTS * point
        sl = entry - SL_POINTS * point
    else:
        tp = entry - TP_POINTS * point
        sl = entry + SL_POINTS * point

    trade = {
        "ticket": ticket,
        "symbol": symbol,
        "timeframe": timeframe_name,
        "pattern": pattern,
        "direction": direction,
        "lot": lot,
        "entry_price": entry,
        "tp": tp,
        "sl": sl,
        "tp_points": TP_POINTS,
        "sl_points": SL_POINTS,
        "open_time": str(datetime.now()),
        "status": "OPEN",
        "close_price": None,
        "close_time": None,
        "pnl_percent": None,
        "pnl_points": None,
        "candles": df.to_dict(orient="records"),
    }
    # Save chart
    img_name = f"{symbol}-{ticket}_open.png"
    trade["image_open"] = save_chart(
        df, trade, img_name,
        extra_lines={"entry": entry, "tp": tp, "sl": sl},
    )
    history.append(trade)
    save_history(history)
    return trade

def real_close_position(trade, close_price, result, symbol):
    """Closes the position via MT5 PositionClose."""
    ticket = trade["ticket"]
    # Prepare request: position close
    # We need to know the volume and type of the position.
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        console.print(f"[red]Position ticket {ticket} not found[/red]")
        return
    pos = pos[0]
    # Determine action: if it's a buy, close with sell; if sell, close with buy.
    if pos.type == mt5.POSITION_TYPE_BUY:
        order_type = mt5.ORDER_TYPE_SELL
        price = mt5.symbol_info_tick(symbol).bid
    else:
        order_type = mt5.ORDER_TYPE_BUY
        price = mt5.symbol_info_tick(symbol).ask

    close_request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(pos.volume),
        "type": order_type,
        "position": ticket,
        "price": price,
        "deviation": 20,
        "magic": 123456,
        "comment": "Close by TP/SL",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(close_request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        console.print(f"[red]Close order failed: {result.comment}[/red]")
        # Still mark as closed but with warning
    else:
        console.print(f"[green]Position {ticket} closed at {price}[/green]")

    # Update trade record
    entry = trade["entry_price"]
    direction = trade["direction"]
    points = (close_price - entry) if direction == "BUY" else (entry - close_price)
    info = mt5.symbol_info(symbol)
    point_size = info.point if info else 0.0001
    pnl_points = points / point_size
    pnl_percent = (points / entry) * 100

    trade["status"] = result  # "TP" or "SL"
    trade["close_price"] = close_price
    trade["close_time"] = str(datetime.now())
    trade["pnl_points"] = round(pnl_points, 2)
    trade["pnl_percent"] = round(pnl_percent, 4)

    # Save chart with close
    df = fetch_candles(symbol, TIMEFRAMES[trade["timeframe"]][0], CANDLE_HISTORY)
    if df is not None:
        img_name = f"{symbol}-{ticket}_closed.png"
        trade["image_closed"] = save_chart(
            df, trade, img_name,
            extra_lines={"entry": entry, "tp": trade["tp"], "sl": trade["sl"]},
        )

# --------------------------------------------------------------------------
# MAIN LOOP (modified)
# --------------------------------------------------------------------------
def main():
    accounts = load_accounts()
    if not accounts:
        console.print("[red]No accounts found in .env. Copy .env.example to .env and fill it in.[/red]")
        sys.exit(1)
    account = pick_account(accounts)
    connect(account)

    symbol = Prompt.ask("Symbol (e.g. EURUSD)").strip().upper()
    if not mt5.symbol_select(symbol, True):
        console.print(f"[red]Could not select symbol {symbol}[/red]")
        sys.exit(1)

    # ===== NEW: Ask for lot size =====
    lot = FloatPrompt.ask("Enter lot size (e.g. 0.01)", default=0.01)

    tf_name = Prompt.ask(
        "Timeframe", choices=list(TIMEFRAMES.keys()), default="M1"
    ).upper()
    tf_const, tf_minutes = TIMEFRAMES[tf_name]

    history = load_history()
    # We'll separate open and closed trades based on status
    open_trades = [t for t in history if t["status"] == "OPEN"]
    closed_trades = [t for t in history if t["status"] != "OPEN"]

    log_lines = []
    def log(msg):
        log_lines.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
        while len(log_lines) > LOG_LINES:
            log_lines.pop(0)

    log(f"Watching {symbol} on {tf_name}. TP={TP_POINTS}pts SL={SL_POINTS}pts. Lot={lot}")

    next_close_at = next_close_boundary(tf_minutes)
    processed_boundary = None

    try:
        with Live(console=console, refresh_per_second=4, screen=False) as live:
            while True:
                now = datetime.now()

                # ---- 1. Monitor each open real trade ----
                still_open = []
                for t in open_trades:
                    # Check if this position still exists in MT5
                    pos = mt5.positions_get(ticket=t["ticket"])
                    if not pos:
                        # Position might have been closed externally; close it in our records
                        # We'll mark as closed with unknown close price? Better to get last known price.
                        # For safety, we'll treat as closed and use current tick as close.
                        tick = mt5.symbol_info_tick(symbol)
                        if tick:
                            close_price = tick.bid if t["direction"] == "BUY" else tick.ask
                        else:
                            close_price = t["entry_price"]  # fallback
                        real_close_position(t, close_price, "CLOSED_EXTERNALLY", symbol)
                        closed_trades.append(t)
                        log(f"[yellow]Position {t['ticket']} closed externally[/yellow]")
                        continue

                    # Get current price for checking TP/SL
                    tick = mt5.symbol_info_tick(symbol)
                    if tick is None:
                        still_open.append(t)
                        continue

                    current_price = tick.bid if t["direction"] == "BUY" else tick.ask
                    hit = None
                    if t["direction"] == "BUY":
                        if current_price >= t["tp"]:
                            hit = "TP"
                        elif current_price <= t["sl"]:
                            hit = "SL"
                    else:
                        if current_price <= t["tp"]:
                            hit = "TP"
                        elif current_price >= t["sl"]:
                            hit = "SL"

                    if hit:
                        # Close the position
                        real_close_position(t, current_price, hit, symbol)
                        color = "green" if hit == "TP" else "red"
                        log(f"[{color}]Position {t['ticket']} hit {hit}[/{color}] -> PnL {t['pnl_points']} pts ({t['pnl_percent']}%)")
                        closed_trades.append(t)
                        # Do not keep in open list
                    else:
                        still_open.append(t)
                open_trades = still_open
                save_history(history)

                # ---- 2. Refresh chart ----
                chart_df = fetch_candles(symbol, tf_const, CHART_DISPLAY_CANDLES)

                # ---- 3. Pattern detection on new candle close ----
                if now >= next_close_at and next_close_at != processed_boundary:
                    df = fetch_candles(symbol, tf_const, CANDLE_HISTORY)
                    processed_boundary = next_close_at
                    next_close_at = next_close_boundary(tf_minutes, after=now)

                    if df is None or len(df) < 2:
                        log("[yellow]no candle data yet[/yellow]")
                    else:
                        signal_candle = df.iloc[-2]
                        pattern = classify_candle(
                            signal_candle["open"], signal_candle["high"],
                            signal_candle["low"], signal_candle["close"],
                        )
                        if pattern is None:
                            log("no doji pattern")
                        else:
                            log(f"[bold yellow]pattern found: {pattern}[/bold yellow] on {signal_candle['time']}")
                            direction = PATTERN_DIRECTION[pattern]
                            if direction is None:
                                log("plain doji = indecision only, no trade opened")
                            else:
                                # Open REAL trade
                                trade = real_open_trade(
                                    symbol, tf_name, pattern, direction,
                                    signal_candle, df, history, lot
                                )
                                if trade:
                                    open_trades.append(trade)
                                    log(f"[bold green]OPENED REAL {direction} ticket {trade['ticket']}[/bold green] "
                                        f"entry={trade['entry_price']:.5f} "
                                        f"tp={trade['tp']:.5f} sl={trade['sl']:.5f}")

                # ---- 4. Update dashboard ----
                if chart_df is not None:
                    live.update(build_dashboard(
                        symbol, tf_name, chart_df, open_trades, closed_trades,
                        log_lines, next_close_at,
                    ))

                time.sleep(TICK_SECONDS)

    except KeyboardInterrupt:
        console.print("\n[cyan]Stopped by user.[/cyan]")
    finally:
        mt5.shutdown()

if __name__ == "__main__":
    main()