"""
MT5 Candlestick Pattern Trading Bot
====================================
Connects to MetaTrader5, lets you pick one of the accounts stored in a local
`.env` file, pick a symbol and a timeframe, then precisely waits for every
new candle to close (e.g. running at 15:45:14 on M1 -> sleeps until
15:46:00), pulls the last 30 OHLC candles and checks them for a
Morning Star (bullish reversal) or Evening Star (bearish reversal) pattern.

On a signal it:
  - prints the signal with rich
  - saves a PNG chart of the last 30 candles
  - opens a market order with TP = 150 points / SL = 200 points
  - saves a JSON trade record
  - monitors price tick by tick, live-updating PnL(%) / PnL(points) in the
    terminal, and when TP or SL is hit it updates the JSON record and
    regenerates the chart image.

Requirements (Windows only - the MetaTrader5 python package requires the
MT5 terminal to be installed and running on the same machine):

    pip install MetaTrader5 pandas mplfinance rich

Accounts live in a local `.env` file next to this script. That file is NOT
standard KEY=VALUE dotenv - it uses the same "Key : Value" block format you
already keep your accounts in, with blocks separated by a line of "="
characters. See .env.example for the exact shape. Copy it to `.env` and
fill in your real login/password/server per account.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

try:
    import MetaTrader5 as mt5
except ImportError:
    print("MetaTrader5 package not found. Install it with: pip install MetaTrader5")
    print("Note: the MetaTrader5 python package only works on Windows, with the")
    print("MT5 terminal installed on the same machine.")
    sys.exit(1)

try:
    import mplfinance as mpf
except ImportError:
    print("mplfinance not found. Install it with: pip install mplfinance")
    sys.exit(1)

from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt
from rich.panel import Panel
from rich.live import Live
from rich import box

console = Console()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "trade_data"
CHARTS_DIR = DATA_DIR / "charts"
TRADES_JSON = DATA_DIR / "trades.json"

TP_POINTS = 150
SL_POINTS = 200
CANDLES_COUNT = 30
MONITOR_POLL_SECONDS = 1

TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}

TIMEFRAME_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}


# ---------------------------------------------------------------------------
# .env loading (custom "Key : Value" block format, NOT standard dotenv)
# ---------------------------------------------------------------------------
def load_accounts(path: Path = ENV_PATH) -> list[dict]:
    if not path.exists():
        console.print(f"[bold red]No .env file found at {path}[/bold red]")
        console.print("Copy .env.example to .env and fill in your real accounts.")
        sys.exit(1)

    raw = path.read_text(encoding="utf-8")
    # Blocks are separated by a line made only of '=' characters.
    blocks = [b.strip() for b in re.split(r"^=+\s*$", raw, flags=re.MULTILINE) if b.strip()]

    accounts = []
    for block in blocks:
        entry: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            entry[key.strip().lower()] = value.strip()
        if entry.get("login") and entry.get("password") and entry.get("server"):
            accounts.append(entry)

    if not accounts:
        console.print("[bold red]No valid accounts parsed from .env[/bold red]")
        sys.exit(1)
    return accounts


def select_account(accounts: list[dict]) -> dict:
    table = Table(title="Available MT5 Accounts", box=box.ROUNDED)
    table.add_column("#", justify="right")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Server")
    table.add_column("Login")
    table.add_column("Account Type")

    for i, acc in enumerate(accounts, start=1):
        table.add_row(
            str(i),
            acc.get("name", ""),
            acc.get("type", ""),
            acc.get("server", ""),
            acc.get("login", ""),
            acc.get("typeacc", ""),
        )
    console.print(table)

    choice = Prompt.ask(
        "Select account #",
        choices=[str(i) for i in range(1, len(accounts) + 1)],
    )
    return accounts[int(choice) - 1]


# ---------------------------------------------------------------------------
# MT5 connection
# ---------------------------------------------------------------------------
def connect_mt5(account: dict) -> None:
    if not mt5.initialize():
        console.print(f"[bold red]mt5.initialize() failed: {mt5.last_error()}[/bold red]")
        sys.exit(1)

    authorized = mt5.login(
        login=int(account["login"]),
        password=account["password"],
        server=account["server"],
    )
    if not authorized:
        console.print(f"[bold red]Login failed: {mt5.last_error()}[/bold red]")
        mt5.shutdown()
        sys.exit(1)

    info = mt5.account_info()
    console.print(
        Panel(
            f"Connected as [bold]{account.get('name', account['login'])}[/bold] "
            f"on [bold]{account['server']}[/bold]\n"
            f"Balance: {info.balance} {info.currency}",
            title="MT5 Connected",
            style="green",
        )
    )


def select_symbol() -> str:
    while True:
        symbol = Prompt.ask("Enter symbol (e.g. EURUSD)").strip().upper()
        if mt5.symbol_select(symbol, True):
            info = mt5.symbol_info(symbol)
            if info is not None:
                console.print(f"[green]Symbol {symbol} selected. Point size: {info.point}[/green]")
                return symbol
        console.print(f"[red]Symbol '{symbol}' not found on this server. Try again.[/red]")


def select_timeframe() -> tuple[str, int]:
    tf_name = Prompt.ask("Select timeframe", choices=list(TIMEFRAMES.keys()), default="M1")
    return tf_name, TIMEFRAMES[tf_name]


# ---------------------------------------------------------------------------
# Timing: wait precisely for the next candle to close
# ---------------------------------------------------------------------------
def wait_for_next_close(tf_seconds: int) -> None:
    now = datetime.now()
    epoch = int(now.timestamp())
    next_close_epoch = ((epoch // tf_seconds) + 1) * tf_seconds
    wait_seconds = next_close_epoch - epoch + 1  # +1s buffer so the broker has posted the candle
    next_close_dt = datetime.fromtimestamp(next_close_epoch)

    with console.status(
        f"[cyan]Now {now.strftime('%H:%M:%S')} -> waiting for candle close at "
        f"{next_close_dt.strftime('%H:%M:%S')} ({wait_seconds}s)...[/cyan]"
    ):
        time.sleep(max(wait_seconds, 0))


# ---------------------------------------------------------------------------
# OHLC fetching
# ---------------------------------------------------------------------------
def fetch_candles(symbol: str, timeframe: int, count: int = CANDLES_COUNT) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        console.print(f"[red]Could not fetch candles: {mt5.last_error()}[/red]")
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df


# ---------------------------------------------------------------------------
# Pattern detection: morning star / evening star
# ---------------------------------------------------------------------------
def _body(c) -> float:
    return abs(c["close"] - c["open"])


def _is_bullish(c) -> bool:
    return c["close"] > c["open"]


def _is_bearish(c) -> bool:
    return c["close"] < c["open"]


def detect_morning_star(df: pd.DataFrame) -> bool:
    """Bullish reversal: big bearish candle, small indecision candle,
    big bullish candle closing above the midpoint of candle 1's body."""
    if len(df) < 3:
        return False
    c1, c2, c3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    b1, b2, b3 = _body(c1), _body(c2), _body(c3)
    if b1 == 0:
        return False

    cond_first_bearish = _is_bearish(c1) and b1 > 0
    cond_small_middle = b2 < b1 * 0.5
    cond_third_bullish = _is_bullish(c3) and b3 > b2
    midpoint1 = (c1["open"] + c1["close"]) / 2
    cond_closes_above_mid = c3["close"] > midpoint1

    return cond_first_bearish and cond_small_middle and cond_third_bullish and cond_closes_above_mid


def detect_evening_star(df: pd.DataFrame) -> bool:
    """Bearish reversal: big bullish candle, small indecision candle,
    big bearish candle closing below the midpoint of candle 1's body."""
    if len(df) < 3:
        return False
    c1, c2, c3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    b1, b2, b3 = _body(c1), _body(c2), _body(c3)
    if b1 == 0:
        return False

    cond_first_bullish = _is_bullish(c1) and b1 > 0
    cond_small_middle = b2 < b1 * 0.5
    cond_third_bearish = _is_bearish(c3) and b3 > b2
    midpoint1 = (c1["open"] + c1["close"]) / 2
    cond_closes_below_mid = c3["close"] < midpoint1

    return cond_first_bullish and cond_small_middle and cond_third_bearish and cond_closes_below_mid


# ---------------------------------------------------------------------------
# Chart saving
# ---------------------------------------------------------------------------
def save_chart(df: pd.DataFrame, filename: Path, title: str = "") -> None:
    plot_df = df.set_index("time")[["open", "high", "low", "close"]].rename(
        columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"}
    )
    mpf.plot(
        plot_df,
        type="candle",
        style="charles",
        title=title,
        savefig=dict(fname=str(filename), dpi=150, bbox_inches="tight"),
    )


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------
def calc_levels(symbol: str, direction: str) -> tuple[float, float, float, float]:
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    point = info.point

    if direction == "buy":
        entry = tick.ask
        sl = entry - SL_POINTS * point
        tp = entry + TP_POINTS * point
    else:
        entry = tick.bid
        sl = entry + SL_POINTS * point
        tp = entry - TP_POINTS * point

    return entry, sl, tp, point


def place_order(symbol: str, direction: str, volume: float = 0.01) -> dict | None:
    entry, sl, tp, point = calc_levels(symbol, direction)
    order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": entry,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": 123456,
        "comment": "morning/evening star bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        console.print(f"[bold red]Order failed: {result}[/bold red]")
        return None

    console.print(f"[bold green]Order placed: {direction.upper()} {symbol} @ {entry}[/bold green]")
    return {
        "ticket": result.order,
        "symbol": symbol,
        "direction": direction,
        "volume": volume,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "point": point,
        "open_time": datetime.now().isoformat(),
        "status": "open",
    }


# ---------------------------------------------------------------------------
# Trade JSON log
# ---------------------------------------------------------------------------
def load_trades() -> list[dict]:
    if TRADES_JSON.exists():
        return json.loads(TRADES_JSON.read_text(encoding="utf-8"))
    return []


def save_trades(trades: list[dict]) -> None:
    TRADES_JSON.write_text(json.dumps(trades, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# Live monitoring of an open trade until TP or SL is hit
# ---------------------------------------------------------------------------
def monitor_trade(trade: dict, trades: list[dict]) -> None:
    symbol = trade["symbol"]
    direction = trade["direction"]
    entry = trade["entry"]
    sl = trade["sl"]
    tp = trade["tp"]
    point = trade["point"]
    ticket = trade["ticket"]
    sign = 1 if direction == "buy" else -1

    def make_panel(price: float, pnl_points: float, pnl_pct: float) -> Panel:
        color = "green" if pnl_points >= 0 else "red"
        body = (
            f"Symbol: {symbol}   Direction: {direction.upper()}\n"
            f"Entry: {entry:.5f}   SL: {sl:.5f}   TP: {tp:.5f}\n"
            f"Price: {price:.5f}\n"
            f"[{color}]PnL: {pnl_points:.1f} points ({pnl_pct:.2f}%)[/{color}]"
        )
        return Panel(body, title=f"Monitoring ticket #{ticket}", border_style=color)

    with Live(console=console, refresh_per_second=2) as live:
        while True:
            tick = mt5.symbol_info_tick(symbol)
            price = tick.bid if direction == "buy" else tick.ask
            pnl_points = (price - entry) / point * sign
            pnl_pct = (pnl_points / SL_POINTS) * 100
            live.update(make_panel(price, pnl_points, pnl_pct))

            positions = mt5.positions_get(ticket=ticket)
            still_open = positions is not None and len(positions) > 0

            hit_tp = (direction == "buy" and price >= tp) or (direction == "sell" and price <= tp)
            hit_sl = (direction == "buy" and price <= sl) or (direction == "sell" and price >= sl)

            if not still_open or hit_tp or hit_sl:
                result = "TP" if hit_tp else ("SL" if hit_sl else "CLOSED")
                trade.update(
                    {
                        "status": "closed",
                        "close_reason": result,
                        "close_price": price,
                        "close_time": datetime.now().isoformat(),
                        "pnl_points": round(pnl_points, 2),
                        "pnl_pct": round(pnl_pct, 2),
                    }
                )
                for i, t in enumerate(trades):
                    if t.get("ticket") == ticket:
                        trades[i] = trade
                        break
                save_trades(trades)

                df = fetch_candles(symbol, TIMEFRAMES_BY_NAME_CACHE.get(symbol, mt5.TIMEFRAME_M1), CANDLES_COUNT)
                if df is not None:
                    fname = CHARTS_DIR / f"trade_{ticket}_closed.png"
                    save_chart(df, fname, title=f"{symbol} {direction.upper()} closed ({result})")

                console.print(
                    Panel(
                        f"Trade #{ticket} closed by [bold]{result}[/bold]\n"
                        f"PnL: {pnl_points:.1f} points ({pnl_pct:.2f}%)",
                        style="bold yellow",
                    )
                )
                break

            time.sleep(MONITOR_POLL_SECONDS)


# Small cache so monitor_trade knows which timeframe const to refetch charts with
TIMEFRAMES_BY_NAME_CACHE: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    CHARTS_DIR.mkdir(exist_ok=True)

    console.print(Panel("Morning/Evening Star MT5 Bot", style="bold cyan"))

    accounts = load_accounts()
    account = select_account(accounts)
    connect_mt5(account)

    symbol = select_symbol()
    tf_name, tf_const = select_timeframe()
    TIMEFRAMES_BY_NAME_CACHE[symbol] = tf_const
    tf_seconds = TIMEFRAME_SECONDS[tf_name]

    trades = load_trades()

    console.print(
        f"[bold]Watching {symbol} on {tf_name}. TP={TP_POINTS}pts SL={SL_POINTS}pts. "
        f"Ctrl+C to stop.[/bold]"
    )

    try:
        while True:
            wait_for_next_close(tf_seconds)

            df = fetch_candles(symbol, tf_const, CANDLES_COUNT)
            if df is None:
                continue

            direction = None
            pattern_name = None
            if detect_morning_star(df):
                direction, pattern_name = "buy", "Morning Star"
            elif detect_evening_star(df):
                direction, pattern_name = "sell", "Evening Star"

            last = df.iloc[-1]
            console.print(
                f"[dim]{last['time']}  O:{last['open']:.5f} H:{last['high']:.5f} "
                f"L:{last['low']:.5f} C:{last['close']:.5f}[/dim]"
            )

            if direction is None:
                console.print("[dim]No signal on this candle.[/dim]")
                continue

            console.print(Panel(f"[bold magenta]{pattern_name} detected -> {direction.upper()}[/bold magenta]"))

            chart_path = CHARTS_DIR / f"signal_{symbol}_{int(time.time())}.png"
            save_chart(df, chart_path, title=f"{symbol} {pattern_name} signal")

            trade = place_order(symbol, direction)
            if trade is None:
                continue

            trades.append(trade)
            save_trades(trades)

            monitor_trade(trade, trades)

    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped by user.[/yellow]")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()