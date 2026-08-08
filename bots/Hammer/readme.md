# Hammer (Pin Bar) Signal Bot

A MetaTrader5-based trading bot that detects Hammer/Pin Bar candlestick patterns and executes paper or live trades with automatic TP/SL management.

## Features

- **Pattern Detection** — Hammer/Pin Bar recognition with configurable downtrend confirmation
- **Live Dashboard** — ASCII candlestick chart with real-time price updates
- **Paper & Live Trading** — Toggle via `LIVE_TRADING` in `.env`
- **Position Monitoring** — Tracks open trades tick-by-tick until TP or SL hit
- **Chart Snapshots** — Saves PNG charts on trade open and close
- **Trade Logging** — JSON log of all trades with full details
- **Backtesting** — Historical simulation with equity curves and HTML reports

## Requirements

```
pip install MetaTrader5 rich pandas mplfinance matplotlib
```

- MetaTrader5 terminal installed and running
- Windows OS (MT5 Python API is Windows-only)

## Project Structure

```
Hammer/
├── main.py             # Live signal bot
├── backtest.py         # Backtesting engine
├── .env                # MT5 account credentials + settings
├── orders.json         # Trade log
├── hammer_trades.json  # Backtest trade log
├── hammer_summary.csv  # Backtest CSV export
├── hammer_report.html  # HTML backtest report
└── charts/             # Chart snapshots (PNG)
```

## Configuration

### .env File

Uses a merged format — account blocks + settings in the same file:

```
Name: My Account
Type: Real
Server: MetaQuotes-Demo
Login: 12345678
Password: your_password
Investor: investor_password
TypeAcc: Standard
====================
TP_POINTS=150
SL_POINTS=200
CANDLES_TO_KEEP=30
LIVE_TRADING=false
MAGIC=990001
```

### Settings

| Parameter | Default | Description |
|-----------|---------|-------------|
| `TP_POINTS` | 150 | Take profit in points |
| `SL_POINTS` | 200 | Stop loss in points |
| `CANDLES_TO_KEEP` | 30 | Candles for pattern detection |
| `LIVE_TRADING` | false | Enable real order placement |
| `MAGIC` | 990001 | Magic number for order identification |

### Hammer Detection Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MIN_LOWER_SHADOW_RATIO` | 2.0 | Lower shadow must be >= 2x body |
| `MAX_UPPER_SHADOW_RATIO` | 0.15 | Upper shadow must be <= 15% of range |
| `REQUIRE_DOWNTREND` | true | Require prior downtrend for confirmation |
| `DOWNTREND_LOOKBACK` | 5 | Prior candles to check for downtrend |

## Usage

### Live Trading

```bash
python main.py
```

1. Select account from the list
2. Enter symbol (e.g., EURUSD)
3. Select timeframe (M1, M5, M15, M30, H1, H4, D1)

The bot waits for each candle to close, checks for hammer patterns, and monitors trades until TP/SL. Press `Ctrl+C` to stop.

### Backtesting

```bash
python backtest.py
```

1. Select account
2. Enter symbol, timeframe, lot size
3. Enter initial balance and number of candles

Output:
- Summary table in terminal
- `hammer_trades.json` — trade data
- `hammer_summary.csv` — CSV export
- `hammer_report.html` — full HTML report with equity/drawdown charts

## How It Works

### Hammer Pattern

A hammer (pin bar) occurs when:
- Small real body near the top of the candle range
- Long lower shadow (>= 2x the body)
- Little to no upper shadow (<= 15% of total range)
- Optionally preceded by a downtrend (closes decreasing over last 5 candles)

### Trade Execution

1. Bot waits for candle close at timeframe boundary
2. Last closed candle is evaluated for hammer pattern
3. Signal built with entry at close, TP/SL calculated from points
4. Position monitored every tick
5. Auto-closed when TP or SL is hit

### Risk Management

- Fixed lot size per trade (0.01 default for paper trading)
- No multiple positions on same symbol
- All trades logged with entry/exit prices and PnL
- Paper trading by default — set `LIVE_TRADING=true` to enable real orders
