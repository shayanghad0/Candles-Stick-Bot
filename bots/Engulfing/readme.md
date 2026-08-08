# Engulfing Pattern Watcher

A MetaTrader5-based trading bot that detects bullish and bearish engulfing candlestick patterns and executes live trades with automatic TP/SL management.

## Features

- **Live Trading** — Real-time candlestick chart (30 candles) with order monitoring
- **Pattern Detection** — Automatic engulfing pattern recognition at candle close
- **Auto Trading** — Opens market orders with configurable lot size
- **Position Management** — Monitors price and closes at TP/SL levels
- **50% TP Alert** — Terminal notification when partial profit target is reached
- **Chart Snapshots** — Saves PNG charts on trade open and close
- **Trade Logging** — JSON log of all trades with full details
- **Backtesting** — Historical simulation with equity curves and HTML reports

## Requirements

```
pip install MetaTrader5 rich mplfinance pandas matplotlib
```

- MetaTrader5 terminal installed and running
- Windows OS (MT5 Python API is Windows-only)

## Project Structure

```
Engulfing/
├── engulfing.py          # Live trading bot
├── backtest.py           # Backtesting engine
├── .env                  # MT5 account credentials
├── trade.json            # Live trade log
├── backtest_trades.json  # Backtest trade log
├── backtest_report.html  # HTML backtest report
└── charts/               # Chart snapshots (PNG)
```

## Configuration

### .env File Format

Create a `.env` file in the project root:

```
Name: My Account
Type: Real
Server: MetaQuotes-Demo
Login: 12345678
Password: your_password
 investor: investor_password
TypeAcc: Standard
====================
Name: Demo Account
Type: Demo
Server: MetaQuotes-Demo
Login: 87654321
Password: demo_password
TypeAcc: Demo
```

### Trading Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `TP_POINTS` | 150 | Take profit in points |
| `SL_POINTS` | 200 | Stop loss in points |
| `CANDLES_TO_SHOW` | 30 | Candles displayed in terminal |
| `PNG_CANDLES` | 15 | Candles in chart snapshots |

## Usage

### Live Trading

```bash
python engulfing.py
```

1. Select account from the list
2. Enter symbol (e.g., EURUSD)
3. Enter lot size
4. Select timeframe (M1, M5, M15, M30, H1, H4, D1)

The bot will display a live candlestick chart and monitor for engulfing patterns. Press `Ctrl+C` to stop.

### Backtesting

```bash
python backtest.py
```

1. Select account
2. Enter symbol, timeframe, lot size
3. Enter initial balance and number of candles

Output:
- Summary table in terminal
- `backtest_trades.json` — trade data
- `backtest_summary.csv` — CSV export
- `backtest_report.html` — full HTML report with equity/drawdown charts

## How It Works

### Engulfing Pattern

A bullish engulfing pattern occurs when:
- Previous candle is bearish (close < open)
- Current candle is bullish (close > open)
- Current candle body completely engulfs previous candle body

Bearish engulfing is the inverse.

### Trade Execution

1. Pattern detected at candle close
2. Market order opened at closing price
3. TP/SL levels calculated from entry
4. Position monitored every tick
5. Auto-closed when TP or SL is hit

### Risk Management

- Fixed lot size per trade
- No multiple positions on same symbol
- 50% TP notification for partial profit awareness
- All trades logged with entry/exit prices and PnL
