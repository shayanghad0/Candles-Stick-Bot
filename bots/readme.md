# Engulfing & Doji Pattern Trading Bots

MetaTrader5-based candlestick pattern trading bots with live dashboards and backtesting.

## Prerequisites

1. Install MetaTrader5 terminal on Windows
2. Install Python dependencies:

```bash
pip install MetaTrader5 rich pandas mplfinance matplotlib
```

| Package | Purpose |
|---------|---------|
| MetaTrader5 | MT5 terminal connection and trading |
| rich | Terminal UI, tables, live dashboard |
| pandas | Data manipulation and OHLC handling |
| mplfinance | Chart snapshot generation (PNG) |
| matplotlib | Backtest equity/drawdown charts |

## Projects

| Bot | Pattern | Mode |
|-----|---------|------|
| `Engulfing/` | Bullish/Bearish Engulfing | Live trading |
| `Doji/` | Dragonfly/Gravestone Doji | Paper trading |
