# Morning/Evening Star Signal Bot

A MetaTrader5-based trading bot that detects Morning Star and Evening Star candlestick patterns and executes live trades with automatic TP/SL management.

## Features

- **Pattern Detection** — Morning Star (bullish) and Evening Star (bearish) recognition
- **Live Dashboard** — ASCII candlestick chart with real-time price updates
- **Live Trading** — Places real market orders via MT5
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
em star/
├── main.py                      # Live signal bot
├── backtest.py                  # Backtesting engine
├── .env                         # MT5 account credentials
├── trade_data/
│   ├── trades.json              # Trade log
│   └── charts/                  # Chart snapshots (PNG)
├── morning_star_trades.json     # Backtest trade log
├── morning_star_summary.csv     # Backtest CSV export
└── morning_star_report.html     # HTML backtest report
```

## Configuration

### .env File Format

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
| `CANDLES_COUNT` | 30 | Candles for pattern detection |

## Usage

### Live Trading

```bash
python main.py
```

1. Select account from the list
2. Enter symbol (e.g., EURUSD)
3. Select timeframe (M1, M5, M15, M30, H1, H4, D1)

The bot waits for each candle to close, checks for morning/evening star patterns, and monitors trades until TP/SL. Press `Ctrl+C` to stop.

### Backtesting

```bash
python backtest.py
```

1. Select account
2. Enter symbol, timeframe, lot size
3. Enter initial balance and number of candles

Output:
- Summary table in terminal
- `morning_star_trades.json` — trade data
- `morning_star_summary.csv` — CSV export
- `morning_star_report.html` — full HTML report with equity/drawdown charts

## How It Works

### Morning Star (Bullish Reversal)

1. First candle: large bearish candle
2. Second candle: small-bodied indecision candle (body < 50% of first candle)
3. Third candle: large bullish candle closing above midpoint of first candle's body

Signal: BUY at close of third candle

### Evening Star (Bearish Reversal)

1. First candle: large bullish candle
2. Second candle: small-bodied indecision candle (body < 50% of first candle)
3. Third candle: large bearish candle closing below midpoint of first candle's body

Signal: SELL at close of third candle

### Trade Execution

1. Bot waits for candle close at timeframe boundary
2. Last 3 closed candles are evaluated for pattern
3. Market order placed with TP/SL levels
4. Position monitored every tick
5. Auto-closed when TP or SL is hit

### Risk Management

- Fixed lot size per trade (0.01 default)
- No multiple positions on same symbol
- All trades logged with entry/exit prices and PnL
