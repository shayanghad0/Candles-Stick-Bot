# Candlestick Pattern Trading Bots

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

## Bots Overview

| Bot | Pattern | Direction | Trading Mode |
|-----|---------|-----------|-------------|
| `Engulfing/` | Bullish/Bearish Engulfing | BUY on bullish, SELL on bearish | Live (real orders) |
| `Doji/` | Dragonfly/Gravestone Doji | BUY on dragonfly, SELL on gravestone | Paper trading |
| `Hammer/` | Hammer / Pin Bar | BUY only (bullish reversal) | Paper trading (LIVE_TRADING flag) |
| `em star/` | Morning Star / Evening Star | BUY on morning, SELL on evening | Live (real orders) |

## Shared Architecture

All bots follow the same structure:

```
BotName/
├── main.py (or pattern.py)   # Live signal bot
├── backtest.py                # Backtesting engine
├── .env                       # MT5 account credentials
├── api.env                    # (optional) Bale/Telegram tokens
├── orders.json / trades.json  # Trade log
├── charts/                    # Chart snapshots (PNG)
└── *report.html               # Backtest HTML report
```

### .env Account Format

All bots use the same custom block format (NOT standard KEY=VALUE dotenv):

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

### Common Trading Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `TP_POINTS` | 100-150 | Take profit in points |
| `SL_POINTS` | 200-250 | Stop loss in points |
| `CANDLES_TO_SHOW` | 30-60 | Candles displayed in terminal |
| `PNG_CANDLES` | 15-30 | Candles in chart snapshots |

---

## Engulfing Bot

**Files:** `engulfing.py` (live), `engulfing bale.py` (live + Bale notifications), `backtest.py`

Detects bullish/bearish engulfing patterns at candle close. The `engulfing bale.py` variant adds:
- Half-volume close at 50% TP
- Bale channel notifications with chart images
- Admin start/stop messages

```bash
python engulfing.py          # live trading
python "engulfing bale.py"   # live + Bale
python backtest.py           # backtest
```

## Doji Bot

**Files:** `doji.py` (paper), `backtest.py`

Detects Dragonfly Doji (BUY), Gravestone Doji (SELL), and plain Doji (no trade). Uses ASCII half-block candlestick charts. Paper trading only — no real orders.

```bash
python doji.py       # paper trading
python backtest.py   # backtest
```

## Hammer Bot

**Files:** `main.py` (paper/live), `backtest.py`

Detects Hammer/Pin Bar patterns (long lower shadow, small body near top of range). Requires prior downtrend for confirmation. Set `LIVE_TRADING=true` in `.env` to enable real orders.

```bash
python main.py       # paper or live (check LIVE_TRADING in .env)
python backtest.py   # backtest
```

## Morning/Evening Star Bot

**Files:** `main.py` (live), `backtest.py`

Detects Morning Star (bullish reversal: big bearish → small indecision → big bullish) and Evening Star (bearish reversal). Places real market orders.

```bash
python main.py       # live trading
python backtest.py   # backtest
```

---

## Backtest Output

All backtesters produce:

- **Terminal summary** — wins, losses, win rate, profit factor, balance
- **JSON trade log** — full trade data per pattern
- **CSV summary** — spreadsheet-friendly export
- **HTML report** — self-contained page with equity curve, drawdown chart, and trade list

## Risk Management

- Fixed lot size per trade
- Configurable TP/SL in points
- 50% TP partial close (Engulfing Bale variant)
- No multiple positions on the same symbol
- All trades logged with entry/exit prices and PnL
