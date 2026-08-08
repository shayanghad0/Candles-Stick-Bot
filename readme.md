# CSTradingBot

MetaTrader5-based candlestick pattern trading bots with live dashboards, real/paper trading, and backtesting.

## Prerequisites

1. Install MetaTrader5 terminal on Windows
2. Install Python dependencies:

```bash
pip install MetaTrader5 rich pandas mplfinance matplotlib requests
```

| Package | Purpose |
|---------|---------|
| MetaTrader5 | MT5 terminal connection and trading |
| rich | Terminal UI, tables, live dashboard |
| pandas | Data manipulation and OHLC handling |
| mplfinance | Chart snapshot generation (PNG) |
| matplotlib | Backtest equity/drawdown charts |
| requests | Bale/Telegram notifications (Engulfing Bale variant) |

## Bots

| Bot | Pattern | Direction | Mode |
|-----|---------|-----------|------|
| `bots/Engulfing/` | Bullish/Bearish Engulfing | BUY/SELL | Live trading |
| `bots/Doji/` | Dragonfly/Gravestone Doji | BUY/SELL | Paper trading |
| `bots/Hammer/` | Hammer / Pin Bar | BUY | Paper/Live (toggle) |
| `bots/em star/` | Morning/Evening Star | BUY/SELL | Live trading |

## Project Structure

```
CSTradingBot/
├── bots/
│   ├── Engulfing/        # Engulfing pattern bot + backtester + Bale variant
│   ├── Doji/             # Doji pattern bot + backtester
│   ├── Hammer/           # Hammer/Pin Bar bot + backtester
│   └── em star/          # Morning/Evening Star bot + backtester
├── data/
├── docs/
├── sample.env            # Example MT5 account config
├── sample.api.env        # Example Bale/Telegram tokens
└── readme.md
```

## Configuration

### .env File (MT5 Accounts)

All bots use the same custom block format:

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

### api.env (Bale Notifications — Engulfing only)

```
Api=your_bale_bot_token
Admin=admin_chat_id
Group=group_chat_id
Channel=channel_chat_id
```

## Quick Start

```bash
# Pick a bot
cd bots/Engulfing

# Run live trading
python engulfing.py

# Or run backtest
python backtest.py
```

Each bot folder has its own `readme.md` with detailed usage instructions.

## Common Trading Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `TP_POINTS` | 100-150 | Take profit in points |
| `SL_POINTS` | 200-250 | Stop loss in points |
| `CANDLES_TO_SHOW` | 30-60 | Candles displayed in terminal |

## Backtest Output

All backtesters produce:

- **Terminal summary** — wins, losses, win rate, profit factor, balance
- **JSON trade log** — full trade data
- **CSV summary** — spreadsheet-friendly export
- **HTML report** — self-contained page with equity curve, drawdown chart, and trade list
