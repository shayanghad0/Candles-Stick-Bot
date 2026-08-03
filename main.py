"""
Disclaimer: This trading bot is for educational purposes only. It does not constitute financial advice.
Trading financial instruments involves substantial risk of loss and is not suitable for all investors.
Use at your own risk. The developers assume no liability for any financial losses incurred.
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import time
from datetime import datetime
import sys
import logging
import json
import os
from threading import Thread, Event

# ----------------------------------------------------------------------
# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Global settings (updated for high‑speed monitoring)
JSON_DB_FILE = "trading_log.json"
DASHBOARD_INTERVAL = 1.0           # seconds between dashboard refreshes
POSITION_MONITOR_INTERVAL = 0.01   # 10 ms (0.01 seconds) between position checks
MAIN_LOOP_SLEEP = 0.01             # 10 ms polling interval for new bars

# ----------------------------------------------------------------------
# Helper functions
def get_symbol_info(symbol):
    """Get symbol info and ensure it's available."""
    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        logger.error(f"Symbol {symbol} not found. Enable it in Market Watch.")
        return None
    if not sym_info.visible:
        if not mt5.symbol_select(symbol, True):
            logger.error(f"Failed to select symbol {symbol}")
            return None
    return sym_info

def get_timeframe(tf_str):
    """Map timeframe string to MT5 timeframe constant."""
    mapping = {
        'M1': mt5.TIMEFRAME_M1,
        'M5': mt5.TIMEFRAME_M5,
        'M15': mt5.TIMEFRAME_M15,
        'M30': mt5.TIMEFRAME_M30,
        'H1': mt5.TIMEFRAME_H1,
        'H4': mt5.TIMEFRAME_H4,
        'D1': mt5.TIMEFRAME_D1
    }
    return mapping.get(tf_str.upper(), mt5.TIMEFRAME_H1)

def load_order_db():
    """Load order history from JSON file."""
    if os.path.exists(JSON_DB_FILE):
        try:
            with open(JSON_DB_FILE, 'r') as f:
                return json.load(f)
        except:
            return []
    return []

def save_order_db(db):
    """Save order history to JSON file."""
    with open(JSON_DB_FILE, 'w') as f:
        json.dump(db, f, indent=2, default=str)

# ----------------------------------------------------------------------
# Candlestick pattern detection functions
# Each returns (signal: 1=bullish, -1=bearish, 0=none, pattern_name: str)

def is_bullish_engulfing(candles):
    if len(candles) < 2: return 0, ""
    prev, curr = candles[-2], candles[-1]
    if prev['close'] < prev['open'] and curr['close'] > curr['open'] and curr['open'] <= prev['close'] and curr['close'] >= prev['open']:
        return 1, "Bullish Engulfing"
    return 0, ""

def is_bearish_engulfing(candles):
    if len(candles) < 2: return 0, ""
    prev, curr = candles[-2], candles[-1]
    if prev['close'] > prev['open'] and curr['close'] < curr['open'] and curr['open'] >= prev['close'] and curr['close'] <= prev['open']:
        return -1, "Bearish Engulfing"
    return 0, ""

def is_doji(candles):
    curr = candles[-1]
    body = abs(curr['close'] - curr['open'])
    range_hl = curr['high'] - curr['low']
    if range_hl > 0 and body <= 0.1 * range_hl:
        return 0, "Doji"
    return 0, ""

def is_dragonfly_doji(candles):
    if is_doji(candles)[1] != "Doji": return 0, ""
    curr = candles[-1]
    upper_shadow = curr['high'] - max(curr['open'], curr['close'])
    lower_shadow = min(curr['open'], curr['close']) - curr['low']
    body = abs(curr['close'] - curr['open'])
    if lower_shadow >= 2 * body and upper_shadow <= 0.2 * body:
        return 1, "Dragonfly Doji"
    return 0, ""

def is_gravestone_doji(candles):
    if is_doji(candles)[1] != "Doji": return 0, ""
    curr = candles[-1]
    upper_shadow = curr['high'] - max(curr['open'], curr['close'])
    lower_shadow = min(curr['open'], curr['close']) - curr['low']
    body = abs(curr['close'] - curr['open'])
    if upper_shadow >= 2 * body and lower_shadow <= 0.2 * body:
        return -1, "Gravestone Doji"
    return 0, ""

def is_morning_star(candles):
    if len(candles) < 3: return 0, ""
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    if c1['close'] >= c1['open']: return 0, ""
    body1 = abs(c1['close'] - c1['open'])
    range1 = c1['high'] - c1['low']
    if range1 == 0 or body1 < 0.5 * range1: return 0, ""
    body2 = abs(c2['close'] - c2['open'])
    range2 = c2['high'] - c2['low']
    if range2 == 0 or body2 > 0.3 * range2: return 0, ""
    if c2['open'] >= c1['close']: return 0, ""
    if c3['close'] <= c3['open']: return 0, ""
    body3 = abs(c3['close'] - c3['open'])
    range3 = c3['high'] - c3['low']
    if range3 == 0 or body3 < 0.5 * range3: return 0, ""
    midpoint = (c1['open'] + c1['close']) / 2
    if c3['close'] > midpoint: return 1, "Morning Star"
    return 0, ""

def is_evening_star(candles):
    if len(candles) < 3: return 0, ""
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    if c1['close'] <= c1['open']: return 0, ""
    body1 = abs(c1['close'] - c1['open'])
    range1 = c1['high'] - c1['low']
    if range1 == 0 or body1 < 0.5 * range1: return 0, ""
    body2 = abs(c2['close'] - c2['open'])
    range2 = c2['high'] - c2['low']
    if range2 == 0 or body2 > 0.3 * range2: return 0, ""
    if c2['open'] <= c1['close']: return 0, ""
    if c3['close'] >= c3['open']: return 0, ""
    body3 = abs(c3['close'] - c3['open'])
    range3 = c3['high'] - c3['low']
    if range3 == 0 or body3 < 0.5 * range3: return 0, ""
    midpoint = (c1['open'] + c1['close']) / 2
    if c3['close'] < midpoint: return -1, "Evening Star"
    return 0, ""

def is_hammer(candles):
    if len(candles) < 2: return 0, ""
    curr = candles[-1]
    body = abs(curr['close'] - curr['open'])
    range_hl = curr['high'] - curr['low']
    if range_hl == 0: return 0, ""
    lower_shadow = min(curr['open'], curr['close']) - curr['low']
    upper_shadow = curr['high'] - max(curr['open'], curr['close'])
    if lower_shadow >= 2 * body and upper_shadow <= body:
        return 1, "Hammer"
    return 0, ""

def is_shooting_star(candles):
    if len(candles) < 2: return 0, ""
    curr = candles[-1]
    body = abs(curr['close'] - curr['open'])
    range_hl = curr['high'] - curr['low']
    if range_hl == 0: return 0, ""
    upper_shadow = curr['high'] - max(curr['open'], curr['close'])
    lower_shadow = min(curr['open'], curr['close']) - curr['low']
    if upper_shadow >= 2 * body and lower_shadow <= body:
        return -1, "Shooting Star"
    return 0, ""

def is_bullish_harami(candles):
    if len(candles) < 2: return 0, ""
    prev, curr = candles[-2], candles[-1]
    if prev['close'] < prev['open'] and curr['close'] > curr['open'] and curr['high'] <= prev['high'] and curr['low'] >= prev['low']:
        return 1, "Bullish Harami"
    return 0, ""

def is_bearish_harami(candles):
    if len(candles) < 2: return 0, ""
    prev, curr = candles[-2], candles[-1]
    if prev['close'] > prev['open'] and curr['close'] < curr['open'] and curr['high'] <= prev['high'] and curr['low'] >= prev['low']:
        return -1, "Bearish Harami"
    return 0, ""

def is_tweezer_tops(candles):
    if len(candles) < 2: return 0, ""
    c1, c2 = candles[-2], candles[-1]
    if c1['close'] > c1['open'] and c2['close'] < c2['open']:
        high_diff = abs(c1['high'] - c2['high'])
        avg_range = (abs(c1['high'] - c1['low']) + abs(c2['high'] - c2['low'])) / 2
        if avg_range > 0 and high_diff < 0.1 * avg_range:
            return -1, "Tweezer Tops"
    return 0, ""

def is_tweezer_bottoms(candles):
    if len(candles) < 2: return 0, ""
    c1, c2 = candles[-2], candles[-1]
    if c1['close'] < c1['open'] and c2['close'] > c2['open']:
        low_diff = abs(c1['low'] - c2['low'])
        avg_range = (abs(c1['high'] - c1['low']) + abs(c2['high'] - c2['low'])) / 2
        if avg_range > 0 and low_diff < 0.1 * avg_range:
            return 1, "Tweezer Bottoms"
    return 0, ""

def is_inside_bar_false_breakout(candles):
    if len(candles) < 3: return 0, ""
    mother, inside, breakout = candles[-3], candles[-2], candles[-1]
    if not (inside['high'] <= mother['high'] and inside['low'] >= mother['low']):
        return 0, ""
    # false breakout up (bearish)
    if breakout['high'] > inside['high'] and mother['low'] <= breakout['close'] <= mother['high']:
        return -1, "False Breakout Up"
    # false breakdown (bullish)
    if breakout['low'] < inside['low'] and mother['low'] <= breakout['close'] <= mother['high']:
        return 1, "False Breakout Down"
    return 0, ""

PATTERN_DETECTORS = [
    is_bullish_engulfing,
    is_bearish_engulfing,
    is_doji,
    is_dragonfly_doji,
    is_gravestone_doji,
    is_morning_star,
    is_evening_star,
    is_hammer,
    is_shooting_star,
    is_bullish_harami,
    is_bearish_harami,
    is_tweezer_tops,
    is_tweezer_bottoms,
    is_inside_bar_false_breakout
]

# ----------------------------------------------------------------------
# Trend and support/resistance
def compute_sma(df, period=21):
    return df['close'].rolling(window=period).mean()

def detect_swing_points(df, window=3):
    highs, lows = df['high'].values, df['low'].values
    swing_highs, swing_lows = [], []
    for i in range(window, len(df)-window):
        if highs[i] == max(highs[i-window:i+window+1]):
            swing_highs.append(highs[i])
        if lows[i] == min(lows[i-window:i+window+1]):
            swing_lows.append(lows[i])
    return swing_highs, swing_lows

def is_near_level(price, levels, threshold):
    return any(abs(price - lvl) <= threshold for lvl in levels)

def is_high_volume(df, index, vol_multiplier=1.5):
    if len(df) < 20: return True
    recent_vol = df['tick_volume'].iloc[-21:-1].mean()
    return df['tick_volume'].iloc[index] > vol_multiplier * recent_vol

# ----------------------------------------------------------------------
# Order handling and position management
def place_market_order(symbol, order_type, lot, sl_price, tp_price, deviation=20):
    """Place a market order with SL/TP. Returns (result, success_bool)."""
    tick = mt5.symbol_info_tick(symbol)
    point = mt5.symbol_info(symbol).point
    digits = mt5.symbol_info(symbol).digits

    if order_type == mt5.ORDER_TYPE_BUY:
        price = tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": sl_price,
            "tp": tp_price,
            "deviation": deviation,
            "magic": 234000,
            "comment": "py_bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
    else:
        price = tick.bid
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": sl_price,
            "tp": tp_price,
            "deviation": deviation,
            "magic": 234000,
            "comment": "py_bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error(f"Order failed: {result.comment} (retcode={result.retcode})")
        return result, False

    logger.info(f"Order executed: ticket={result.order}")
    # Check if SL/TP were set correctly by reading the opened position
    position = mt5.positions_get(ticket=result.order)
    if position:
        pos = position[0]
        sl_ok = (abs(pos.sl - sl_price) < point*10) if sl_price else False
        tp_ok = (abs(pos.tp - tp_price) < point*10) if tp_price else False
        if not sl_ok or not tp_ok:
            logger.warning("SL/TP not set correctly in broker. Attempting to modify position...")
            modify = mt5.PositionModify(
                ticket=pos.ticket,
                sl=sl_price,
                tp=tp_price
            )
            if modify.retcode != mt5.TRADE_RETCODE_DONE:
                logger.error(f"Position modification failed: {modify.comment}")
            else:
                logger.info("SL/TP modified successfully.")
        else:
            logger.info("SL/TP confirmed.")
    return result, True

def close_position(ticket, volume=None, deviation=20):
    """Close an open position by its ticket."""
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        logger.warning(f"Cannot close: ticket {ticket} not found.")
        return False
    pos = pos[0]
    tick = mt5.symbol_info_tick(pos.symbol)
    if pos.type == mt5.POSITION_TYPE_BUY:
        price = tick.bid
        order_type = mt5.ORDER_TYPE_SELL
    else:
        price = tick.ask
        order_type = mt5.ORDER_TYPE_BUY

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": pos.symbol,
        "volume": pos.volume if volume is None else volume,
        "type": order_type,
        "position": ticket,
        "price": price,
        "deviation": deviation,
        "magic": 234000,
        "comment": "py_bot_close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        logger.info(f"Closed position #{ticket} at {price}")
        return True
    else:
        logger.error(f"Failed to close #{ticket}: {result.comment}")
        return False

# ----------------------------------------------------------------------
# Position monitor (runs in a separate thread, high frequency)
class PositionMonitor:
    def __init__(self, symbol, json_db):
        self.symbol = symbol
        self.json_db = json_db  # reference to the list of orders in memory
        self.stop_event = Event()
        self.thread = Thread(target=self.run)
        self.thread.daemon = True

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)

    def run(self):
        logger.info("Position monitor started (10 ms resolution).")
        while not self.stop_event.is_set():
            positions = mt5.positions_get(symbol=self.symbol)
            if positions:
                for pos in positions:
                    ticket = pos.ticket
                    # find order in DB
                    order = next((o for o in self.json_db if o.get('ticket') == ticket and o.get('status') == 'open'), None)
                    if order is None:
                        continue
                    tick = mt5.symbol_info_tick(self.symbol)
                    current_price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
                    # Check SL/TP levels
                    sl = order.get('sl')
                    tp = order.get('tp')
                    hit_sl = False
                    hit_tp = False
                    if pos.type == mt5.POSITION_TYPE_BUY:
                        if current_price <= sl:
                            hit_sl = True
                        elif current_price >= tp:
                            hit_tp = True
                    else:  # SELL
                        if current_price >= sl:
                            hit_sl = True
                        elif current_price <= tp:
                            hit_tp = True

                    if hit_sl or hit_tp:
                        reason = "SL" if hit_sl else "TP"
                        logger.info(f"Position #{ticket} hit {reason} at {current_price}. Closing...")
                        if close_position(ticket):
                            order['status'] = 'closed'
                            order['close_time'] = datetime.now().isoformat()
                            order['close_price'] = current_price
                            order['close_reason'] = reason
                            save_order_db(self.json_db)
            self.stop_event.wait(POSITION_MONITOR_INTERVAL)  # 0.01 s

# ----------------------------------------------------------------------
# Dashboard (refreshed every 1 second now)
def print_dashboard(symbol, account_info):
    """Print a live summary to the terminal (updated every 1 second)."""
    os.system('cls' if os.name == 'nt' else 'clear')
    print(f"=== MT5 Candlestick Bot Dashboard ===  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Account: {account_info.login}  Balance: {account_info.balance:.2f}  Equity: {account_info.equity:.2f}  Profit: {account_info.profit:.2f}")

    sym_info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if sym_info and tick:
        digits = sym_info.digits
        spread = round(tick.ask - tick.bid, digits)
        print(f"{symbol}  Bid: {tick.bid}  Ask: {tick.ask}  Spread: {spread}")

    positions = mt5.positions_get(symbol=symbol)
    if positions:
        print("\nOpen Positions:")
        for p in positions:
            pos_type = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
            print(f"  #{p.ticket}  {pos_type}  Vol: {p.volume}  Open: {p.price_open}  SL: {p.sl}  TP: {p.tp}  Current P/L: {p.profit:.2f}")
    else:
        print("\nNo open positions.")
    print("\n" + "="*50)

# ----------------------------------------------------------------------
# Main bot loop (10 ms polling for new bars)
def run_bot():
    print("=== MetaTrader 5 Candlestick Pattern Trading Bot ===")
    print("Educational purpose only. Use at your own risk.\n")

    # User inputs
    try:
        login = int(input("MT5 account login: "))
        password = input("MT5 password: ")
        server = input("MT5 server name: ")
        symbol = input("Trading symbol (e.g., EURUSD): ").upper()
        lot = float(input("Lot size: "))
        sl_points = int(input("Stop Loss in points: "))
        tp_points = int(input("Take Profit in points: "))
        tf_str = input("Timeframe (M1, M5, M15, M30, H1, H4, D1) [H1]: ").strip()
        if tf_str == "":
            tf_str = "H1"
    except ValueError:
        logger.error("Invalid input format.")
        sys.exit(1)

    timeframe = get_timeframe(tf_str)

    # Connect to MT5
    if not mt5.initialize():
        logger.error("MT5 initialization failed")
        sys.exit(1)
    authorized = mt5.login(login, password=password, server=server)
    if not authorized:
        logger.error(f"Login failed: {mt5.last_error()}")
        mt5.shutdown()
        sys.exit(1)
    logger.info("Connected to MT5 successfully")

    # Symbol setup
    sym_info = get_symbol_info(symbol)
    if sym_info is None:
        mt5.shutdown()
        sys.exit(1)
    point = sym_info.point
    digits = sym_info.digits
    logger.info(f"Symbol {symbol} ready. Point={point}, Digits={digits}")

    # Adjust SL/TP to broker minimum
    stops_level = sym_info.trade_stops_level
    if sl_points < stops_level:
        logger.warning(f"SL points ({sl_points}) less than min stop level ({stops_level}). Using {stops_level}.")
        sl_points = stops_level
    if tp_points < stops_level:
        logger.warning(f"TP points ({tp_points}) less than min stop level ({stops_level}). Using {stops_level}.")
        tp_points = stops_level

    # Load order database
    order_db = load_order_db()

    # Start position monitor thread (high speed)
    monitor = PositionMonitor(symbol, order_db)
    monitor.start()

    # Main loop variables
    last_bar_time = None
    last_dashboard_update = time.time()

    logger.info("Bot started (10 ms main loop). Waiting for new bars...")
    try:
        while True:
            current_time = time.time()

            # Dashboard update (every 1 second)
            if current_time - last_dashboard_update >= DASHBOARD_INTERVAL:
                account_info = mt5.account_info()
                if account_info:
                    print_dashboard(symbol, account_info)
                last_dashboard_update = current_time

            # Get latest candles (every 10 ms, very light MT5 call)
            rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, 200)
            if rates is None or len(rates) < 5:
                time.sleep(MAIN_LOOP_SLEEP)
                continue

            df = pd.DataFrame(rates)
            df['time'] = pd.to_datetime(df['time'], unit='s')
            latest_bar = df.iloc[-1]
            current_bar_time = latest_bar['time']

            if last_bar_time is None or current_bar_time > last_bar_time:
                last_bar_time = current_bar_time
                logger.info(f"New bar detected: {current_bar_time}")

                # Prepare OHLC array
                candles = df[['open', 'high', 'low', 'close', 'tick_volume']].to_records(index=False)

                # Trend filter
                sma = compute_sma(df, 21).iloc[-1]
                if pd.notna(sma):
                    trend = "uptrend" if latest_bar['close'] > sma else "downtrend"
                    logger.info(f"Trend: {trend} (SMA21={sma:.{digits}f})")
                else:
                    trend = "neutral"

                # Support/resistance
                swing_highs, swing_lows = detect_swing_points(df, window=3)
                atr = (df['high'] - df['low']).tail(14).mean()
                proximity = atr * 0.5 if not pd.isna(atr) else 0

                # Pattern scan
                signal = 0
                pattern_name = ""
                for detector in PATTERN_DETECTORS:
                    sig, pname = detector(candles)
                    if sig != 0:
                        signal = sig
                        pattern_name = pname
                        logger.info(f"Pattern: {pattern_name} (signal={sig})")
                        break

                if signal == 0:
                    continue

                # Check alignment or S/R
                pattern_aligned = (signal == 1 and trend == "uptrend") or (signal == -1 and trend == "downtrend")
                near_support = signal == 1 and is_near_level(latest_bar['low'], swing_lows, proximity)
                near_resistance = signal == -1 and is_near_level(latest_bar['high'], swing_highs, proximity)

                if not (pattern_aligned or near_support or near_resistance):
                    logger.info("Pattern not aligned with trend or S/R. Skipping.")
                    continue

                # Volume filter (optional)
                if pattern_name in ["Bullish Engulfing", "Bearish Engulfing", "False Breakout Up", "False Breakout Down"]:
                    if not is_high_volume(df, -1):
                        logger.info("Low volume. Skipping.")
                        continue

                # Check existing positions
                open_positions = mt5.positions_get(symbol=symbol)
                if open_positions and len(open_positions) > 0:
                    logger.info(f"Already have open position(s) on {symbol}. Skipping new trade.")
                    continue

                # Place order
                tick = mt5.symbol_info_tick(symbol)
                if signal == 1:  # BUY
                    entry_price = tick.ask
                    sl_price = entry_price - sl_points * point
                    tp_price = entry_price + tp_points * point
                    order_type = mt5.ORDER_TYPE_BUY
                else:  # SELL
                    entry_price = tick.bid
                    sl_price = entry_price + sl_points * point
                    tp_price = entry_price - tp_points * point
                    order_type = mt5.ORDER_TYPE_SELL

                sl_price = round(sl_price, digits)
                tp_price = round(tp_price, digits)

                logger.info(f"Sending order: {order_type}, Lot={lot}, Entry~{entry_price:.{digits}f}, SL={sl_price}, TP={tp_price}")

                result, success = place_market_order(symbol, order_type, lot, sl_price, tp_price)
                if success:
                    # Add to DB
                    order_record = {
                        "ticket": result.order,
                        "symbol": symbol,
                        "type": "BUY" if order_type == mt5.ORDER_TYPE_BUY else "SELL",
                        "volume": lot,
                        "entry_price": entry_price,
                        "sl": sl_price,
                        "tp": tp_price,
                        "open_time": datetime.now().isoformat(),
                        "status": "open",
                        "pattern": pattern_name,
                        "close_time": None,
                        "close_price": None,
                        "close_reason": None
                    }
                    order_db.append(order_record)
                    save_order_db(order_db)
                    logger.info(f"Trade recorded in database. Total trades: {len(order_db)}")

            # Ultra‑fast sleep (10 ms) for near‑real‑time monitoring
            time.sleep(MAIN_LOOP_SLEEP)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
    finally:
        monitor.stop()
        mt5.shutdown()
        logger.info("MT5 connection closed. Goodbye.")

if __name__ == "__main__":
    run_bot()