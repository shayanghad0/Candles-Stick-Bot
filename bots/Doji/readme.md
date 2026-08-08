# Doji Pattern Signal Bot

A MetaTrader5-based paper trading bot that detects Doji, Dragonfly Doji, and Gravestone Doji candlestick patterns. Generates virtual trade signals with TP/SL tracking — no real orders are placed.

## Features

- **Paper Trading** — Virtual signals only, no real money at risk
- **Pattern Detection** — Doji, Dragonfly Doji (BUY), Gravestone Doji (SELL)
- **Live Dashboard** — ASCII candlestick chart, open/closed signals, event log
- **Chart Snapshots** — PNG charts saved on trade open and close
- **Trade History** — Full JSON log with candle data per signal
- **Backtesting** — Historical simulation with equity curves and HTML reports

## Requirements

```
pip install MetaTrader5 rich pandas mplfinance
```

- MetaTrader5 terminal installed and running
- Windows OS (MT5 Python API is Windows-only)

> **Note:** This bot does NOT place real orders. It tracks virtual signals using live tick prices. Wire in `mt5.order_send()` yourself if you want real execution.

## Project Structure

```
Doji/
├── doji.py               # Live paper trading bot
├── backtest.py            # Backtesting engine
├── .env                   # MT5 account credentials
├── trades_history.json    # Live trade log
├── doji_trades.json       # Backtest trade log
├── doji_summary.csv       # Backtest CSV export
├── doji_report.html       # HTML backtest report
└── charts/                # Chart snapshots (PNG)
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
Investor: investor_password
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
| `DOJI_BODY_RATIO` | 0.10 | Body must be ≤10% of range |
| `LONG_WICK_RATIO` | 0.60 | Long wick must be ≥60% of range |
| `SHORT_WICK_RATIO` | 0.15 | Opposite wick must be ≤15% of range |

## Usage

### Live Paper Trading

```bash
python doji.py
```

1. Select account from the list
2. Enter symbol (e.g., EURUSD)
3. Select timeframe (M1, M5, M15, M30, H1, H4, D1)

The bot displays a live dashboard with ASCII candlestick chart and monitors for doji patterns. Press `Ctrl+C` to stop.

### Backtesting

```bash
python backtest.py
```

1. Select account
2. Enter symbol, timeframe, lot size
3. Enter initial balance and number of candles

Output:
- Summary table in terminal
- `doji_trades.json` — trade data
- `doji_summary.csv` — CSV export
- `doji_report.html` — full HTML report with equity/drawdown charts

## Pattern Detection

| Pattern | Body Ratio | Wick Behavior | Signal |
|---------|-----------|---------------|--------|
| Dragonfly Doji | ≤10% | Long lower wick (≥60%), short upper (≤15%) | BUY |
| Gravestone Doji | ≤10% | Long upper wick (≥60%), short lower (≤15%) | SELL |
| Plain Doji | ≤10% | Neither condition met | None (indecision) |

## How It Works

1. Bot polls for candle close at timeframe boundary
2. Last closed candle is evaluated for doji patterns
3. Dragonfly Doji → BUY signal, Gravestone Doji → SELL signal
4. Plain Doji logged as indecision, no trade opened
5. Open signals tracked tick-by-tick against live price
6. Auto-closed when TP or SL level is hit
7. Chart snapshots saved at open and close
