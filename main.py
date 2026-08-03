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

# ----------------------------------------------------------------------
# Logging configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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

# ----------------------------------------------------------------------
# Candlestick pattern detection functions
# Each function takes a numpy array of candles (assumed to have OHLC columns)
# and returns (signal: 1=bullish, -1=bearish, 0=none, pattern_name: str)
# The most recent candle is the last element.

def is_bullish_engulfing(candles):
    """Bullish Engulfing: previous bearish, current bullish engulfing previous."""
    if len(candles) < 2:
        return 0, ""
    prev = candles[-2]
    curr = candles[-1]
    # prev bearish
    if prev['close'] < prev['open']:
        # curr bullish and body covers prev body
        if curr['close'] > curr['open'] and curr['open'] <= prev['close'] and curr['close'] >= prev['open']:
            return 1, "Bullish Engulfing"
    return 0, ""

def is_bearish_engulfing(candles):
    """Bearish Engulfing: previous bullish, current bearish engulfing previous."""
    if len(candles) < 2:
        return 0, ""
    prev = candles[-2]
    curr = candles[-1]
    if prev['close'] > prev['open']:
        if curr['close'] < curr['open'] and curr['open'] >= prev['close'] and curr['close'] <= prev['open']:
            return -1, "Bearish Engulfing"
    return 0, ""

def is_doji(candles):
    """Generic Doji: open and close very close (body <= 10% of high-low range)."""
    curr = candles[-1]
    body = abs(curr['close'] - curr['open'])
    range_hl = curr['high'] - curr['low']
    if range_hl > 0 and body <= 0.1 * range_hl:
        return 0, "Doji"  # neutral signal
    return 0, ""

def is_dragonfly_doji(candles):
    """Dragonfly Doji: Doji with long lower shadow, very small upper shadow."""
    if is_doji(candles)[0] != 0 or is_doji(candles)[1] != "Doji":
        return 0, ""
    curr = candles[-1]
    upper_shadow = curr['high'] - max(curr['open'], curr['close'])
    lower_shadow = min(curr['open'], curr['close']) - curr['low']
    body = abs(curr['close'] - curr['open'])
    if lower_shadow >= 2 * body and upper_shadow <= 0.2 * body:
        return 1, "Dragonfly Doji"
    return 0, ""

def is_gravestone_doji(candles):
    """Gravestone Doji: Doji with long upper shadow, very small lower shadow."""
    if is_doji(candles)[0] != 0 or is_doji(candles)[1] != "Doji":
        return 0, ""
    curr = candles[-1]
    upper_shadow = curr['high'] - max(curr['open'], curr['close'])
    lower_shadow = min(curr['open'], curr['close']) - curr['low']
    body = abs(curr['close'] - curr['open'])
    if upper_shadow >= 2 * body and lower_shadow <= 0.2 * body:
        return -1, "Gravestone Doji"
    return 0, ""

def is_morning_star(candles):
    """Morning Star: three-candle bullish reversal."""
    if len(candles) < 3:
        return 0, ""
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    # First bearish candle with significant body
    if c1['close'] >= c1['open']:
        return 0, ""
    body1 = abs(c1['close'] - c1['open'])
    range1 = c1['high'] - c1['low']
    if range1 == 0 or body1 < 0.5 * range1:
        return 0, ""
    # Second candle small body (or doji) that gaps down
    body2 = abs(c2['close'] - c2['open'])
    range2 = c2['high'] - c2['low']
    if range2 == 0 or body2 > 0.3 * range2:
        return 0, ""
    if c2['open'] > c1['close']:  # gap down: open lower than previous close?
        # Actually morning star gap down: open of 2nd below close of 1st
        return 0, ""  # we want c2 open < c1 close
    if c2['open'] >= c1['close']:
        return 0, ""
    # Third bullish candle closing above midpoint of first candle
    if c3['close'] <= c3['open']:
        return 0, ""
    body3 = abs(c3['close'] - c3['open'])
    range3 = c3['high'] - c3['low']
    if range3 == 0 or body3 < 0.5 * range3:
        return 0, ""
    midpoint = (c1['open'] + c1['close']) / 2
    if c3['close'] > midpoint:
        return 1, "Morning Star"
    return 0, ""

def is_evening_star(candles):
    """Evening Star: three-candle bearish reversal."""
    if len(candles) < 3:
        return 0, ""
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    # First bullish candle large body
    if c1['close'] <= c1['open']:
        return 0, ""
    body1 = abs(c1['close'] - c1['open'])
    range1 = c1['high'] - c1['low']
    if range1 == 0 or body1 < 0.5 * range1:
        return 0, ""
    # Second small body (or doji) that gaps up
    body2 = abs(c2['close'] - c2['open'])
    range2 = c2['high'] - c2['low']
    if range2 == 0 or body2 > 0.3 * range2:
        return 0, ""
    if c2['open'] <= c1['close']:  # gap up: open above previous close
        return 0, ""
    # Third bearish candle closing below midpoint of first
    if c3['close'] >= c3['open']:
        return 0, ""
    body3 = abs(c3['close'] - c3['open'])
    range3 = c3['high'] - c3['low']
    if range3 == 0 or body3 < 0.5 * range3:
        return 0, ""
    midpoint = (c1['open'] + c1['close']) / 2
    if c3['close'] < midpoint:
        return -1, "Evening Star"
    return 0, ""

def is_hammer(candles):
    """Hammer: small body at upper end, long lower shadow >= 2*body, appears after downtrend."""
    if len(candles) < 2:
        return 0, ""
    curr = candles[-1]
    body = abs(curr['close'] - curr['open'])
    range_hl = curr['high'] - curr['low']
    if range_hl == 0:
        return 0, ""
    # Body in top third
    body_top = max(curr['open'], curr['close'])
    body_bottom = min(curr['open'], curr['close'])
    upper_body_center = (body_top + body_bottom) / 2  # not used
    # Simple: lower shadow >= 2*body, upper shadow <= body
    lower_shadow = body_bottom - curr['low']
    upper_shadow = curr['high'] - body_top
    if lower_shadow >= 2 * body and upper_shadow <= body:
        return 1, "Hammer"
    return 0, ""

def is_shooting_star(candles):
    """Shooting Star: small body at lower end, long upper shadow >= 2*body, appears after uptrend."""
    if len(candles) < 2:
        return 0, ""
    curr = candles[-1]
    body = abs(curr['close'] - curr['open'])
    range_hl = curr['high'] - curr['low']
    if range_hl == 0:
        return 0, ""
    body_top = max(curr['open'], curr['close'])
    body_bottom = min(curr['open'], curr['close'])
    upper_shadow = curr['high'] - body_top
    lower_shadow = body_bottom - curr['low']
    if upper_shadow >= 2 * body and lower_shadow <= body:
        return -1, "Shooting Star"
    return 0, ""

def is_bullish_harami(candles):
    """Bullish Harami: first candle bearish, second bullish entirely within first."""
    if len(candles) < 2:
        return 0, ""
    prev = candles[-2]
    curr = candles[-1]
    if prev['close'] < prev['open']:  # bearish
        if curr['close'] > curr['open']:  # bullish
            if curr['high'] <= prev['high'] and curr['low'] >= prev['low']:
                return 1, "Bullish Harami"
    return 0, ""

def is_bearish_harami(candles):
    """Bearish Harami: first candle bullish, second bearish entirely inside."""
    if len(candles) < 2:
        return 0, ""
    prev = candles[-2]
    curr = candles[-1]
    if prev['close'] > prev['open']:  # bullish
        if curr['close'] < curr['open']:  # bearish
            if curr['high'] <= prev['high'] and curr['low'] >= prev['low']:
                return -1, "Bearish Harami"
    return 0, ""

def is_tweezer_tops(candles):
    """Tweezer Tops: two candles with equal highs, first bullish, second bearish."""
    if len(candles) < 2:
        return 0, ""
    c1, c2 = candles[-2], candles[-1]
    if c1['close'] > c1['open'] and c2['close'] < c2['open']:  # first bullish, second bearish
        high_diff = abs(c1['high'] - c2['high'])
        avg_range = (abs(c1['high'] - c1['low']) + abs(c2['high'] - c2['low'])) / 2
        if avg_range > 0 and high_diff < 0.1 * avg_range:
            return -1, "Tweezer Tops"
    return 0, ""

def is_tweezer_bottoms(candles):
    """Tweezer Bottoms: two candles with equal lows, first bearish, second bullish."""
    if len(candles) < 2:
        return 0, ""
    c1, c2 = candles[-2], candles[-1]
    if c1['close'] < c1['open'] and c2['close'] > c2['open']:  # first bearish, second bullish
        low_diff = abs(c1['low'] - c2['low'])
        avg_range = (abs(c1['high'] - c1['low']) + abs(c2['high'] - c2['low'])) / 2
        if avg_range > 0 and low_diff < 0.1 * avg_range:
            return 1, "Tweezer Bottoms"
    return 0, ""

def is_inside_bar_false_breakout(candles):
    """Inside Bar False Breakout: three candles. Mother, inside, breakout that closes back inside mother."""
    if len(candles) < 3:
        return 0, ""
    mother = candles[-3]
    inside = candles[-2]
    breakout = candles[-1]
    # Check inside bar condition
    if not (inside['high'] <= mother['high'] and inside['low'] >= mother['low']):
        return 0, ""
    # Check false breakout up (bearish)
    if breakout['high'] > inside['high'] and breakout['close'] <= mother['high'] and breakout['close'] >= mother['low']:
        return -1, "False Breakout Up"
    # False breakdown (bullish)
    if breakout['low'] < inside['low'] and breakout['close'] >= mother['low'] and breakout['close'] <= mother['high']:
        return 1, "False Breakout Down"
    return 0, ""

# List of all detection functions
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
    """Return SMA series."""
    return df['close'].rolling(window=period).mean()

def detect_swing_points(df, window=3):
    """Detect recent swing highs and lows using pivot points.
    Returns list of swing high prices and swing low prices."""
    highs = df['high'].values
    lows = df['low'].values
    swing_highs = []
    swing_lows = []
    for i in range(window, len(df)-window):
        if highs[i] == max(highs[i-window:i+window+1]):
            swing_highs.append(highs[i])
        if lows[i] == min(lows[i-window:i+window+1]):
            swing_lows.append(lows[i])
    return swing_highs, swing_lows

def is_near_level(price, levels, threshold):
    """Check if price is within threshold of any level."""
    for lvl in levels:
        if abs(price - lvl) <= threshold:
            return True
    return False

# ----------------------------------------------------------------------
# Volume filter
def is_high_volume(df, index, vol_multiplier=1.5):
    """Check if volume at index is significantly higher than recent average (last 20 bars)."""
    if len(df) < 20:
        return True
    recent_vol = df['tick_volume'].iloc[-21:-1].mean()
    return df['tick_volume'].iloc[index] > vol_multiplier * recent_vol

# ----------------------------------------------------------------------
# Trading logic
def place_order(symbol, order_type, lot, sl_price, tp_price, deviation=20):
    """Place a market order with SL/TP. Returns order result."""
    point = mt5.symbol_info(symbol).point
    tick = mt5.symbol_info_tick(symbol)
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
    else:  # SELL
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
    return result

def run_bot():
    """Main function to run the trading bot."""
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

    # Ensure symbol is available
    sym_info = get_symbol_info(symbol)
    if sym_info is None:
        mt5.shutdown()
        sys.exit(1)
    point = sym_info.point
    digits = sym_info.digits
    logger.info(f"Symbol {symbol} ready. Point={point}, Digits={digits}")

    # Validate SL/TP against minimum stop level
    stops_level = sym_info.trade_stops_level
    if sl_points < stops_level:
        logger.warning(f"SL points ({sl_points}) less than minimum stop level ({stops_level}). Using {stops_level} instead.")
        sl_points = stops_level
    if tp_points < stops_level:
        logger.warning(f"TP points ({tp_points}) less than minimum stop level ({stops_level}). Using {stops_level} instead.")
        tp_points = stops_level

    # Main monitoring loop
    last_bar_time = 0
    sleep_seconds = 60  # check every minute

    logger.info("Starting monitoring...")
    try:
        while True:
            # Fetch recent 200 candles
            rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, 200)
            if rates is None or len(rates) < 5:
                logger.warning("Not enough data. Waiting...")
                time.sleep(sleep_seconds)
                continue

            df = pd.DataFrame(rates)
            df['time'] = pd.to_datetime(df['time'], unit='s')
            latest_bar = df.iloc[-1]
            current_bar_time = latest_bar['time']

            if current_bar_time == last_bar_time:
                # No new bar yet
                time.sleep(sleep_seconds)
                continue

            # New bar detected
            logger.info(f"New bar at {current_bar_time}")
            last_bar_time = current_bar_time

            # Prepare OHLC data for pattern detection (numpy structured array)
            candles = df[['open', 'high', 'low', 'close', 'tick_volume']].to_records(index=False)

            # Trend filter (SMA 21)
            sma = compute_sma(df, 21).iloc[-1]
            if pd.isna(sma):
                logger.info("SMA not available yet.")
                continue
            trend = "uptrend" if latest_bar['close'] > sma else "downtrend"
            logger.info(f"Trend: {trend} (SMA21={sma:.{digits}f})")

            # Support/resistance levels
            swing_highs, swing_lows = detect_swing_points(df, window=3)
            atr = (df['high'] - df['low']).tail(14).mean()
            proximity = atr * 0.5  # near level if within half ATR

            # Scan patterns
            signal = 0
            pattern_name = ""
            for detector in PATTERN_DETECTORS:
                sig, pname = detector(candles)
                if sig != 0:
                    signal = sig
                    pattern_name = pname
                    logger.info(f"Pattern detected: {pattern_name} (signal={sig})")
                    break

            if signal == 0:
                time.sleep(sleep_seconds)
                continue

            # Check trend alignment or S/R level
            is_reversal = True  # all patterns considered reversal
            pattern_aligned = (signal == 1 and trend == "uptrend") or (signal == -1 and trend == "downtrend")
            near_resistance = (signal == -1 and is_near_level(latest_bar['high'], swing_highs, proximity))
            near_support = (signal == 1 and is_near_level(latest_bar['low'], swing_lows, proximity))

            if not (pattern_aligned or (is_reversal and (near_support or near_resistance))):
                logger.info("Pattern not aligned with trend or significant level. Skipping trade.")
                time.sleep(sleep_seconds)
                continue

            # Volume filter for breakout patterns (optional)
            if pattern_name in ["Bullish Engulfing", "Bearish Engulfing", "False Breakout Up", "False Breakout Down",
                                "Morning Star", "Evening Star"]:
                if not is_high_volume(df, -1):  # check last bar volume
                    logger.info("Volume not confirming pattern. Skipping.")
                    time.sleep(sleep_seconds)
                    continue

            # Check existing positions
            positions = mt5.positions_get(symbol=symbol)
            if positions and len(positions) > 0:
                logger.info(f"Already have an open position on {symbol}. Skipping new trade.")
                time.sleep(sleep_seconds)
                continue

            # Execute trade
            tick = mt5.symbol_info_tick(symbol)
            if signal == 1:  # Buy
                entry_price = tick.ask
                sl_price = entry_price - sl_points * point
                tp_price = entry_price + tp_points * point
                order_type = mt5.ORDER_TYPE_BUY
            else:  # Sell
                entry_price = tick.bid
                sl_price = entry_price + sl_points * point
                tp_price = entry_price - tp_points * point
                order_type = mt5.ORDER_TYPE_SELL

            # Round prices to correct digits
            sl_price = round(sl_price, digits)
            tp_price = round(tp_price, digits)

            logger.info(f"Placing order: {order_type}, Lot={lot}, Entry~{entry_price:.{digits}f}, "
                        f"SL={sl_price}, TP={tp_price}")

            result = place_order(symbol, order_type, lot, sl_price, tp_price)
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                logger.error(f"Order failed: {result.comment} (retcode={result.retcode})")
            else:
                logger.info(f"Order executed: {result.order}")

            time.sleep(sleep_seconds)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
    finally:
        mt5.shutdown()
        logger.info("MT5 connection closed.")

if __name__ == "__main__":
    run_bot()