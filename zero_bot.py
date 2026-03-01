import websocket
import json
import threading
import time
from datetime import datetime
import os
import platform
import sys
from dotenv import load_dotenv
import math

# Load environment variables
load_dotenv()

# Configuration
symbol = "JD10"
tick_history_limit = 500
tick_list = []
last_digit = None
recent_digits = []

script_start_time = datetime.now()
tick_counter = 0

# Trading variables
API_TOKEN = os.getenv('DERIV_API_TOKEN')
APP_ID = os.getenv('DERIV_APP_ID')
active_trade = None
trade_count = 0
win_count = 0
loss_count = 0
MAX_TRADES = 1
STAKE = 42
trading_active = True
authorized = False
ws_connection = None
bot_running = True

# Track trade execution and next tick
pending_trade_lock = False
pending_trade_time = None
pending_trade_price = None
last_execution_tick = None
next_tick_after_execution = None
execution_tick_number = 0
waiting_for_next_tick = False

# Entropy variables
zero_positions = []
zero_entropy = 0
zero_distance_threshold = 9
trading_allowed = False
history_processed = False

# Recent Zero Density
recent_zero_threshold = 3
recent_zero_count = 0
recent_density_met = True

# Trend Filter
trend_lookback = 5
trend_allowed = True
uptrend_digits = [5,6,7,8,9]
downtrend_digits = [0,1,2,3,4]

# Volatility - DYNAMIC
volatility_lookback = 10
volatility_history = 100
volatility_multiplier = 0.5
volatility_met = True
current_volatility = 0
dynamic_threshold = 0.5
avg_historical_range = 0

# Support/Resistance
support_resistance_lookback = 20
support_resistance_threshold = 0.1
price_position_met = True
current_support = 0
current_resistance = 0
price_position_status = ""

# Zero Streak Protection
consecutive_zeros = 0
max_consecutive_zeros = 1
consecutive_zero_met = True

# Zero Spacing Protection (will be dynamically set by statistical normal)
last_zero_tick = 0
min_ticks_since_last_zero = 20  # Default, will be updated
zero_spacing_met = True

# 🔴 NEW: Statistical Normal Range variables
zero_spacings_history = []  # Store all zero spacings
statistical_threshold_met = True
statistical_confidence = 0
normal_spacing = 20  # Default
current_spacing = 0

# 🔴 NEW: Pattern Detection variables
pattern_safe = True
pattern_warning = ""

# Debug mode
DEBUG_MODE = True

# Track last zero detection
last_zero_detection = {}

# Will be set by user input
decimal_places = 2

def clear_console():
    os.system('cls' if platform.system() == 'Windows' else 'clear')

def show_important_message(title, data):
    """Show an important message that stays on screen"""
    print("\n" + "!" * 80)
    print(f"!!! {title} !!!".center(80))
    print("!" * 80)
    for key, value in data.items():
        print(f"   {key}: {value}")
    print("!" * 80)
    print("\n" + "=" * 80)
    print("🔴 IMPORTANT: This message will stay for 5 seconds")
    print("=" * 80)
    time.sleep(5)

def get_user_input():
    """Ask user for decimal places"""
    global decimal_places
    
    clear_console()
    print("=" * 70)
    print(" ZERO TRIGGER BOT - SETUP".center(70))
    print("=" * 70)
    
    if not API_TOKEN or not APP_ID:
        print("\n❌ ERROR: Missing API credentials in .env file")
        print("Please ensure your .env file contains:")
        print("DERIV_APP_ID=80958")
        print("DERIV_API_TOKEN=your_token_here")
        sys.exit(1)
    
    print(f"\n✅ API Credentials loaded successfully")
    print(f"   App ID: {APP_ID}")
    
    print("\n🔢 Select the number of decimal places for your instrument:")
    print("   2 - For instruments with 2 decimals (e.g., 1HZ10V, Forex pairs)")
    print("   3 - For instruments with 3 decimals")
    print("   4 - For instruments with 4 decimals")
    print("   5 - For instruments with 5 decimals")
    print("\n❓ Enter number of decimal places (2, 3, 4, or 5): ", end="")
    
    try:
        choice = int(input().strip())
        if choice in [2, 3, 4, 5]:
            decimal_places = choice
            print(f"\n✅ Selected: {decimal_places} decimal places")
        else:
            print("\n⚠️  Invalid choice. Using default 2 decimal places.")
            decimal_places = 2
    except:
        print("\n⚠️  Invalid input. Using default 2 decimal places.")
        decimal_places = 2
    
    print(f"\n📊 Monitoring {symbol}")
    print(f"📊 Loading {tick_history_limit} historical ticks")
    print(f"🎯 Strategy: When digit 0 appears → Trade OVER 0")
    print(f"📏 Zero Entropy Threshold: > {zero_distance_threshold} ticks")
    print(f"🔍 Recent Zero Density: < {recent_zero_threshold} in last 20")
    print(f"📈 Trend Filter: Avoid strong downtrend")
    print(f"📊 Volatility: DYNAMIC - 50% of avg range")
    print(f"📍 Support/Resistance: Avoid near resistance")
    print(f"🔢 Consecutive Zero Limit: Max {max_consecutive_zeros} in a row")
    print(f"📊 Statistical Normal: 70% of average spacing")
    print(f"🔍 Pattern Detection: Block dangerous sequences")
    print(f"💰 Stake: ${STAKE} per trade")
    print(f"🛑 Max trades: {MAX_TRADES}")
    print(f"🚀 Starting in 3 seconds...")
    time.sleep(3)

def get_last_digit(price_value):
    """Extract last digit properly"""
    price_str = f"{price_value:.{decimal_places}f}"
    
    if '.' in price_str:
        decimal_part = price_str.split('.')[-1]
        if decimal_part and len(decimal_part) >= 1:
            return int(decimal_part[-1])
    
    return int(price_str.replace('.', '')[-1])

def debug_print(msg):
    """Print debug messages if DEBUG_MODE is on"""
    if DEBUG_MODE:
        print(f"🔍 DEBUG: {msg}")

# 🔴 NEW: Statistical Normal Range calculation
def calculate_normal_zero_spacing():
    """Calculate normal spacing between zeros (removing outliers)"""
    global zero_spacings_history, normal_spacing
    
    if len(zero_spacings_history) < 5:
        normal_spacing = 20  # Default when not enough data
        return
    
    # Sort spacings
    sorted_spacings = sorted(zero_spacings_history)
    
    # Remove top and bottom 10% (outliers)
    trim = max(1, int(len(sorted_spacings) * 0.1))
    trimmed = sorted_spacings[trim:-trim]
    
    if trimmed:
        normal_spacing = sum(trimmed) / len(trimmed)
    else:
        normal_spacing = sum(sorted_spacings) / len(sorted_spacings)

def check_statistical_spacing(current_tick):
    """Check if current zero spacing meets statistical normal"""
    global statistical_threshold_met, statistical_confidence, current_spacing, normal_spacing
    
    if last_zero_tick == 0:
        statistical_threshold_met = True
        statistical_confidence = 100
        return
    
    current_spacing = current_tick - last_zero_tick
    
    # Calculate threshold as 70% of normal spacing
    threshold = normal_spacing * 0.7
    
    # Calculate confidence percentage
    if current_spacing >= normal_spacing:
        statistical_confidence = 100
    else:
        statistical_confidence = (current_spacing / normal_spacing) * 100
    
    statistical_threshold_met = current_spacing >= threshold
    
    debug_print(f"Statistical: Current={current_spacing}, Normal={normal_spacing:.1f}, Threshold={threshold:.1f}, Met={statistical_threshold_met}")

# 🔴 NEW: Pattern Detection
def detect_dangerous_patterns():
    """Check for patterns that historically lead to losses"""
    global pattern_safe, pattern_warning, recent_digits
    
    pattern_safe = True
    pattern_warning = ""
    
    if len(recent_digits) < 5:
        return
    
    last_5 = recent_digits[-5:]
    last_3 = recent_digits[-3:]
    
    # Pattern 1: Double zero (0,0)
    if len(last_3) >= 2 and last_3[-2] == 0 and last_3[-1] == 0:
        pattern_safe = False
        pattern_warning = "Double zero detected"
        return
    
    # Pattern 2: Zero after high digits (7,8,9,0)
    if len(last_5) >= 4:
        if last_5[-4] in [7,8,9] and last_5[-3] in [7,8,9] and last_5[-2] in [7,8,9] and last_5[-1] == 0:
            pattern_safe = False
            pattern_warning = "Zero after high digits"
            return
    
    # Pattern 3: Alternating zeros (0,X,0 within 3 ticks)
    if len(last_5) >= 3:
        if last_5[-3] == 0 and last_5[-1] == 0:
            if last_5[-2] != 0:  # X is not zero
                pattern_safe = False
                pattern_warning = "Alternating zeros pattern"
                return
    
    # Pattern 4: Three zeros in last 7 digits
    if len(recent_digits) >= 7:
        last_7 = recent_digits[-7:]
        zero_count = sum(1 for d in last_7 if d == 0)
        if zero_count >= 3:
            pattern_safe = False
            pattern_warning = f"Too many zeros ({zero_count}) in last 7"
            return

def check_consecutive_zeros(digit):
    """Check if we're in a zero streak"""
    global consecutive_zeros, consecutive_zero_met
    
    old_streak = consecutive_zeros
    
    if digit == 0:
        consecutive_zeros += 1
        debug_print(f"Consecutive zeros: {old_streak} → {consecutive_zeros}")
    else:
        if consecutive_zeros > 0:
            debug_print(f"Non-zero detected! Resetting streak from {consecutive_zeros} to 0")
        consecutive_zeros = 0
    
    consecutive_zero_met = consecutive_zeros <= max_consecutive_zeros

def check_zero_spacing(tick_number, digit):
    """Check if enough ticks have passed since last zero"""
    global last_zero_tick, zero_spacing_met, min_ticks_since_last_zero
    
    if digit == 0:
        if last_zero_tick == 0:
            zero_spacing_met = True
        else:
            ticks_since_last = tick_number - last_zero_tick
            # Use statistical threshold instead of fixed
            threshold = normal_spacing * 0.7
            zero_spacing_met = ticks_since_last >= threshold
        last_zero_tick = tick_number
    else:
        zero_spacing_met = True

def debug_filters_before_trade():
    """Print ALL filter states before making a trade decision"""
    print("\n" + "=" * 70)
    print("🔍 PRE-TRADE FILTER DIAGNOSTIC")
    print("=" * 70)
    
    ticks_since = tick_counter - last_zero_tick if last_zero_tick > 0 else 999
    threshold = normal_spacing * 0.7
    
    print(f"\n📏 STATISTICAL NORMAL:")
    print(f"   Normal spacing: {normal_spacing:.1f} ticks")
    print(f"   Current spacing: {ticks_since} ticks")
    print(f"   Threshold (70%): {threshold:.1f} ticks")
    print(f"   Confidence: {statistical_confidence:.1f}%")
    print(f"   Statistical met: {statistical_threshold_met}")
    
    print(f"\n🔍 PATTERN DETECTION:")
    print(f"   Last 5 digits: {recent_digits[-5:] if len(recent_digits) >=5 else recent_digits}")
    print(f"   Pattern safe: {pattern_safe}")
    if not pattern_safe:
        print(f"   Warning: {pattern_warning}")
    
    print(f"\n📏 ZERO STATS:")
    print(f"   Consecutive zeros: {consecutive_zeros} (Max: {max_consecutive_zeros})")
    print(f"   Consecutive zero met: {consecutive_zero_met}")
    
    print(f"\n📊 OTHER FILTERS:")
    print(f"   Zero Entropy: {zero_entropy:.1f} > {zero_distance_threshold}? {zero_entropy > zero_distance_threshold}")
    print(f"   Recent Density: {recent_zero_count} < {recent_zero_threshold}? {recent_density_met}")
    print(f"   Trend Allowed: {trend_allowed}")
    print(f"   Volatility Met: {volatility_met}")
    print(f"   Price Position Met: {price_position_met}")
    
    all_conditions = [
        zero_entropy > zero_distance_threshold,
        recent_density_met,
        trend_allowed,
        volatility_met,
        price_position_met,
        consecutive_zero_met,
        statistical_threshold_met,
        pattern_safe
    ]
    
    if all(all_conditions):
        print(f"\n✅ TRADE WOULD BE ALLOWED")
    else:
        print(f"\n❌ TRADE WOULD BE BLOCKED")
        if not (zero_entropy > zero_distance_threshold): print(f"   ✗ Entropy")
        if not recent_density_met: print(f"   ✗ Density")
        if not trend_allowed: print(f"   ✗ Trend")
        if not volatility_met: print(f"   ✗ Volatility")
        if not price_position_met: print(f"   ✗ Price Position")
        if not consecutive_zero_met: print(f"   ✗ Consecutive Zeros")
        if not statistical_threshold_met: print(f"   ✗ Statistical Spacing ({ticks_since} < {threshold:.1f})")
        if not pattern_safe: print(f"   ✗ Pattern: {pattern_warning}")
    
    print("=" * 70)

def calculate_dynamic_threshold():
    """Calculate dynamic volatility threshold based on last 100 ticks"""
    global tick_list, volatility_history, volatility_multiplier, dynamic_threshold, avg_historical_range
    
    if len(tick_list) < volatility_history:
        if len(tick_list) >= volatility_lookback:
            ranges = []
            for i in range(len(tick_list) - volatility_lookback):
                slice_prices = tick_list[i:i+volatility_lookback]
                range_val = max(slice_prices) - min(slice_prices)
                ranges.append(range_val)
            if ranges:
                avg_historical_range = sum(ranges) / len(ranges)
                dynamic_threshold = avg_historical_range * volatility_multiplier
        return
    
    ranges = []
    start_idx = max(0, len(tick_list) - volatility_history)
    end_idx = len(tick_list) - volatility_lookback
    
    for i in range(start_idx, end_idx):
        slice_prices = tick_list[i:i+volatility_lookback]
        range_val = max(slice_prices) - min(slice_prices)
        ranges.append(range_val)
    
    if ranges:
        avg_historical_range = sum(ranges) / len(ranges)
        dynamic_threshold = avg_historical_range * volatility_multiplier

def check_volatility():
    """Check if market has enough movement using dynamic threshold"""
    global tick_list, volatility_met, current_volatility, volatility_lookback, dynamic_threshold
    
    if len(tick_list) < volatility_lookback:
        volatility_met = True
        current_volatility = 0
        return
    
    recent_prices = tick_list[-volatility_lookback:]
    price_high = max(recent_prices)
    price_low = min(recent_prices)
    current_volatility = price_high - price_low
    volatility_met = current_volatility > dynamic_threshold

def check_support_resistance():
    """Check if current price is near support or resistance"""
    global tick_list, support_resistance_lookback, support_resistance_threshold, price_position_met, current_support, current_resistance, price_position_status
    
    if len(tick_list) < support_resistance_lookback:
        price_position_met = True
        price_position_status = "⏳ Building S/R levels..."
        return
    
    recent_prices = tick_list[-support_resistance_lookback:]
    current_support = min(recent_prices)
    current_resistance = max(recent_prices)
    current_price = tick_list[-1]
    
    price_range = current_resistance - current_support
    near_threshold = price_range * support_resistance_threshold
    
    distance_to_resistance = current_resistance - current_price
    near_resistance = distance_to_resistance < near_threshold
    
    distance_to_support = current_price - current_support
    near_support = distance_to_support < near_threshold
    
    if near_resistance:
        price_position_met = False
        price_position_status = f"❌ Near resistance (within {near_threshold:.2f})"
    elif near_support:
        price_position_met = True
        price_position_status = f"✅ Near support"
    else:
        price_position_met = True
        price_position_status = f"📍 Middle of range"

def check_trend():
    """Check if market is in strong trend"""
    global recent_digits, trend_allowed, uptrend_digits, downtrend_digits, trend_lookback
    
    if len(recent_digits) < trend_lookback:
        trend_allowed = True
        return
    
    last_trend_digits = recent_digits[-trend_lookback:]
    all_uptrend = all(d in uptrend_digits for d in last_trend_digits)
    all_downtrend = all(d in downtrend_digits for d in last_trend_digits)
    
    if all_downtrend:
        trend_allowed = False
    else:
        trend_allowed = True

def update_recent_density(digit):
    """Update recent zero density check"""
    global recent_digits, recent_zero_count, recent_density_met
    
    recent_digits.append(digit)
    
    if len(recent_digits) > 20:
        recent_digits.pop(0)
    
    recent_zero_count = sum(1 for d in recent_digits if d == 0)
    recent_density_met = recent_zero_count < recent_zero_threshold
    check_trend()

def calculate_zero_entropy_from_history():
    """Calculate average distance between occurrences of digit 0 from historical data"""
    global zero_positions, zero_entropy, trading_allowed, history_processed, tick_list, recent_digits, recent_zero_count, recent_density_met, trend_allowed, volatility_met, current_volatility, dynamic_threshold, avg_historical_range, price_position_met, current_support, current_resistance, price_position_status, last_zero_tick, consecutive_zeros, zero_spacings_history
    
    if len(tick_list) < 10:
        print("⚠️ Not enough historical data for zero entropy calculation")
        return
    
    zero_positions = []
    recent_digits = []
    zero_spacings_history = []
    consecutive_zeros = 0
    last_zero_tick = 0
    
    print(f"\n📊 Processing {len(tick_list)} historical ticks...")
    
    # Find all zero occurrences in historical data
    for i, price in enumerate(tick_list, 1):
        digit = get_last_digit(price)
        recent_digits.append(digit)
        if digit == 0:
            if last_zero_tick > 0:
                spacing = i - last_zero_tick
                zero_spacings_history.append(spacing)
                print(f"   Found zero at position {i}, spacing: {spacing}")
            else:
                print(f"   Found zero at position {i} (first zero)")
            zero_positions.append(i)
            last_zero_tick = i
    
    if len(recent_digits) > 20:
        recent_digits = recent_digits[-20:]
    
    recent_zero_count = sum(1 for d in recent_digits if d == 0)
    recent_density_met = recent_zero_count < recent_zero_threshold
    
    # Calculate normal spacing from history
    calculate_normal_zero_spacing()
    
    calculate_dynamic_threshold()
    check_trend()
    check_volatility()
    check_support_resistance()
    
    if len(zero_positions) >= 2:
        distances = []
        for i in range(1, len(zero_positions)):
            distance = zero_positions[i] - zero_positions[i-1]
            distances.append(distance)
        
        zero_entropy = sum(distances) / len(distances)
        
        trading_allowed = (zero_entropy > zero_distance_threshold) and \
                         recent_density_met and \
                         trend_allowed and \
                         volatility_met and \
                         price_position_met
        
        print(f"\n✅ Zero Entropy calculated:")
        print(f"   Total zeros: {len(zero_positions)}")
        print(f"   Avg distance: {zero_entropy:.1f} ticks")
        print(f"   Normal spacing: {normal_spacing:.1f} ticks")
        print(f"   Trading allowed: {trading_allowed}")
    else:
        print(f"\n⚠️ Only {len(zero_positions)} zero(s) found")
        zero_entropy = 0
        trading_allowed = False
    
    history_processed = True

def calculate_zero_entropy():
    """Calculate average distance between occurrences of digit 0"""
    global zero_positions, zero_entropy, trading_allowed, recent_density_met, trend_allowed, volatility_met, price_position_met, consecutive_zero_met, zero_spacing_met, statistical_threshold_met, pattern_safe
    
    if len(zero_positions) < 2:
        zero_entropy = 0
        trading_allowed = False
        return
    
    distances = []
    for i in range(1, len(zero_positions)):
        distance = zero_positions[i] - zero_positions[i-1]
        distances.append(distance)
    
    zero_entropy = sum(distances) / len(distances)
    
    # Trading is allowed if ALL conditions are met
    trading_allowed = (zero_entropy > zero_distance_threshold) and \
                     recent_density_met and \
                     trend_allowed and \
                     volatility_met and \
                     price_position_met and \
                     consecutive_zero_met and \
                     zero_spacing_met and \
                     statistical_threshold_met and \
                     pattern_safe

def update_zero_positions(tick_number, digit):
    """Update zero positions when digit 0 appears"""
    global zero_positions, zero_spacings_history
    
    if digit == 0:
        if last_zero_tick > 0:
            spacing = tick_number - last_zero_tick
            zero_spacings_history.append(spacing)
            # Keep only last 50 spacings
            if len(zero_spacings_history) > 50:
                zero_spacings_history = zero_spacings_history[-50:]
            calculate_normal_zero_spacing()
        
        zero_positions.append(tick_number)
        if len(zero_positions) > 100:
            zero_positions = zero_positions[-100:]
        
        calculate_zero_entropy()
        return True
    return False

def place_over_zero_trade(detection_price, detection_digit, detection_tick):
    """Place an OVER 0 trade when digit 0 appears"""
    global active_trade, trade_count, authorized, ws_connection, trading_allowed, last_zero_detection, pending_trade_lock, pending_trade_time, pending_trade_price
    
    if pending_trade_lock:
        print(f"\n⏸️  TRADE BLOCKED - Already have pending trade from {pending_trade_time}")
        return False
    
    if not authorized:
        print("❌ Not authorized yet.")
        return False
    
    if trade_count >= MAX_TRADES:
        print(f"🛑 Max trades ({MAX_TRADES}) reached.")
        return False
    
    last_zero_detection = {
        'price': detection_price,
        'digit': detection_digit,
        'tick': detection_tick,
        'time': datetime.now()
    }
    
    print("\n" + "=" * 70)
    print("⚡ TRADE DECISION CHECK")
    print("=" * 70)
    print(f"Zero detected at tick #{detection_tick}")
    print(f"Detection price: {detection_price:.{decimal_places}f} | Digit: {detection_digit}")
    print("=" * 70)
    
    if not trading_allowed:
        reasons = []
        if zero_entropy <= zero_distance_threshold:
            reasons.append(f"Entropy ({zero_entropy:.1f} ≤ {zero_distance_threshold})")
        if not recent_density_met:
            reasons.append(f"Density ({recent_zero_count} ≥ {recent_zero_threshold})")
        if not trend_allowed:
            reasons.append(f"Strong downtrend detected")
        if not volatility_met:
            reasons.append(f"Low volatility ({current_volatility:.2f} ≤ {dynamic_threshold:.2f})")
        if not price_position_met:
            reasons.append(f"Price near resistance")
        if not consecutive_zero_met:
            reasons.append(f"Zero streak ({consecutive_zeros} in a row)")
        if not statistical_threshold_met:
            threshold = normal_spacing * 0.7
            reasons.append(f"Statistical spacing ({detection_tick - last_zero_tick} < {threshold:.1f})")
        if not pattern_safe:
            reasons.append(f"Pattern: {pattern_warning}")
        
        print(f"⏸️  Trade HOLD: {' and '.join(reasons)}")
        return False
    
    if not ws_connection:
        print("❌ No WebSocket connection.")
        return False
    
    buy_request = {
        "buy": 1,
        "price": STAKE,
        "parameters": {
            "amount": STAKE,
            "basis": "stake",
            "contract_type": "DIGITOVER",
            "currency": "USD",
            "duration": 1,
            "duration_unit": "t",
            "symbol": symbol,
            "barrier": "0"
        }
    }
    
    print(f"\n🎯 DIGIT 0 DETECTED at {detection_price:.{decimal_places}f}!")
    print(f"✅ ALL 8 CONDITIONS MET:")
    print(f"   - Zero Entropy: {zero_entropy:.1f} > {zero_distance_threshold}")
    print(f"   - Recent Density: {recent_zero_count} < {recent_zero_threshold}")
    print(f"   - Trend: Not in strong downtrend")
    print(f"   - Volatility: {current_volatility:.2f} > {dynamic_threshold:.2f}")
    print(f"   - Price: {price_position_status}")
    print(f"   - Zero Streak: {consecutive_zeros} in a row (allowed)")
    print(f"   - Statistical: {detection_tick - last_zero_tick} ticks (≥ {normal_spacing*0.7:.1f})")
    print(f"   - Pattern: No dangerous patterns")
    print(f"💰 Sending trade request...")
    
    try:
        ws_connection.send(json.dumps(buy_request))
        
        pending_trade_lock = True
        pending_trade_time = datetime.now()
        pending_trade_price = detection_price
        
        trade_count += 1
        active_trade = {
            'trigger': 'Digit 0',
            'trade_type': 'OVER 0',
            'time': datetime.now(),
            'status': 'buy_sent',
            'stake': STAKE,
            'detection_price': detection_price,
            'detection_digit': detection_digit,
            'detection_tick': detection_tick
        }
        return True
    except Exception as e:
        print(f"❌ Failed to send trade: {e}")
        pending_trade_lock = False
        return False

def shutdown_bot():
    """Force bot to shut down completely"""
    global bot_running, ws_connection
    
    print("\n" + "=" * 80)
    print("🛑 MAX TRADES REACHED - BOT SHUTTING DOWN".center(80))
    print(f"📊 FINAL STATISTICS:".center(80))
    print(f"   Total Trades: {trade_count}".center(80))
    print(f"   Wins: {win_count}".center(80))
    print(f"   Losses: {loss_count}".center(80))
    if trade_count > 0:
        print(f"   Win Rate: {(win_count/trade_count*100):.1f}%".center(80))
    print("=" * 80)
    
    bot_running = False
    
    if ws_connection:
        try:
            ws_connection.close()
        except:
            pass
    
    time.sleep(1)
    print("\n👋 Bot terminated. Goodbye!")
    os._exit(0)

def check_trade_outcome(message_data):
    """Check trade outcome and track next tick after execution"""
    global active_trade, win_count, loss_count, authorized, bot_running, trade_count, MAX_TRADES
    global pending_trade_lock, pending_trade_time, pending_trade_price
    global last_execution_tick, next_tick_after_execution, execution_tick_number
    global waiting_for_next_tick
    
    if 'msg_type' in message_data and message_data['msg_type'] == "authorize":
        if 'error' not in message_data:
            print(f"\n✅ Authorization successful!")
            if 'authorize' in message_data:
                print(f"   Account: {message_data['authorize']['loginid']}")
                print(f"   Balance: {message_data['authorize']['balance']} {message_data['authorize']['currency']}")
            authorized = True
        else:
            print(f"\n❌ Authorization failed: {message_data['error']['message']}")
        return
    
    if 'msg_type' in message_data and message_data['msg_type'] == "buy":
        if 'buy' in message_data:
            buy_info = message_data['buy']
            execution_price = buy_info.get('buy_price')
            
            print(f"\n✅ TRADE EXECUTED! Trade #{trade_count}/{MAX_TRADES}")
            print(f"   Transaction ID: {buy_info.get('transaction_id')}")
            print(f"   Contract ID: {buy_info.get('contract_id')}")
            print(f"   Execution Price: {execution_price}")
            
            last_execution_tick = {
                'price': execution_price,
                'time': datetime.now(),
                'tick_number': tick_counter
            }
            execution_tick_number = tick_counter
            
            if active_trade and 'detection_price' in active_trade:
                detection_price = active_trade['detection_price']
                price_diff = execution_price - detection_price
                print(f"   Detection Price: {detection_price:.{decimal_places}f}")
                print(f"   Price Difference: {price_diff:.{decimal_places}f}")
            
            if active_trade:
                active_trade['contract_id'] = buy_info.get('contract_id')
                active_trade['transaction_id'] = buy_info.get('transaction_id')
                active_trade['status'] = 'executed'
                active_trade['execution_price'] = execution_price
                active_trade['execution_tick'] = tick_counter
            
            pending_trade_lock = False
            
            if trade_count >= MAX_TRADES:
                waiting_for_next_tick = True
                print(f"\n⏳ Max trades reached - waiting for next tick before shutdown...")
    
    if 'msg_type' in message_data and message_data['msg_type'] == "proposal_open_contract":
        poc = message_data.get('proposal_open_contract', {})
        
        if active_trade and 'contract_id' in active_trade:
            if poc.get('contract_id') == active_trade['contract_id']:
                if poc.get('is_sold'):
                    profit = poc.get('profit', 0)
                    
                    if profit > 0:
                        win_count += 1
                        outcome = "WIN 🏆"
                    else:
                        loss_count += 1
                        outcome = "LOSS 💔"
                    
                    print(f"\n📊 TRADE RESULT: {outcome}")
                    print(f"   Profit: ${profit}")
                    
                    if 'detection_price' in active_trade:
                        print(f"   Detection Price: {active_trade['detection_price']:.{decimal_places}f}")
                    if 'execution_price' in active_trade:
                        print(f"   Execution Price: {active_trade['execution_price']:.{decimal_places}f}")
                    
                    if next_tick_after_execution:
                        print(f"\n🔜 NEXT TICK AFTER EXECUTION:")
                        print(f"   Price: {next_tick_after_execution['price']:.{decimal_places}f}")
                        print(f"   Digit: {next_tick_after_execution['digit']}")
                        print(f"   Tick #{next_tick_after_execution['tick_number']}")
                        
                        if next_tick_after_execution['digit'] == 0:
                            print(f"   ⚠️  This zero caused the loss!")
                    
                    print(f"   Win Rate: {win_count}/{trade_count} ({(win_count/trade_count*100):.1f}%)")
                    
                    # Show popup for losses
                    if profit < 0:
                        loss_data = {
                            'Loss Amount': f"${profit}",
                            'Detection Price': f"{active_trade['detection_price']:.{decimal_places}f}",
                            'Execution Price': f"{active_trade['execution_price']:.{decimal_places}f}",
                            'Normal Spacing': f"{normal_spacing:.1f} ticks",
                            'Current Spacing': f"{current_spacing} ticks",
                            'Pattern Warning': pattern_warning if not pattern_safe else "None"
                        }
                        if next_tick_after_execution:
                            loss_data['Next Tick Digit'] = next_tick_after_execution['digit']
                        show_important_message("💔 LOSS DETECTED - ANALYSIS", loss_data)
                    
                    active_trade = None
    
    if 'msg_type' in message_data and message_data['msg_type'] == "error":
        print(f"\n❌ API Error: {message_data['error']['message']}")
        pending_trade_lock = False

def display_status():
    """Display current status with all filters"""
    clear_console()
    
    print("=" * 120)
    print(" ZERO TRIGGER BOT - STATISTICAL + PATTERN PROTECTION".center(120))
    print(f" Time: {datetime.now().strftime('%H:%M:%S')}".center(120))
    print("=" * 120)
    
    auth_status = "✅ AUTHORIZED" if authorized else "⏳ AUTHORIZING..."
    print(f"\n🔐 Status: {auth_status}")
    
    hist_status = "✅ LOADED" if history_processed else "⏳ LOADING HISTORY..."
    print(f"📊 History: {hist_status} ({len(tick_list)} ticks)")
    
    if trade_count >= MAX_TRADES:
        status = "🔴 COMPLETED - WAITING FOR NEXT TICK" if waiting_for_next_tick else "🔴 COMPLETED"
    else:
        status = "🟢 ACTIVE"
    print(f"🤖 Bot Status: {status}")
    
    if pending_trade_lock:
        print(f"\n⏳ PENDING TRADE - Waiting for execution...")
    
    if waiting_for_next_tick:
        print(f"\n⏳ WAITING FOR NEXT TICK BEFORE SHUTDOWN...")
    
    # Statistical Normal Display
    print(f"\n📊 STATISTICAL NORMAL RANGE:")
    print(f"   Normal spacing: {normal_spacing:.1f} ticks (based on {len(zero_spacings_history)} zeros)")
    print(f"   Current spacing: {current_spacing} ticks")
    print(f"   Threshold (70%): {normal_spacing*0.7:.1f} ticks")
    print(f"   Confidence: {statistical_confidence:.1f}%")
    print(f"   {'✅' if statistical_threshold_met else '❌'} Statistical met")
    
    # Pattern Detection Display
    print(f"\n🔍 PATTERN DETECTION:")
    if len(recent_digits) >= 5:
        print(f"   Last 5 digits: {recent_digits[-5:]}")
    if pattern_safe:
        print(f"   ✅ No dangerous patterns")
    else:
        print(f"   ❌ {pattern_warning}")
    
    if last_zero_detection:
        print(f"\n🎯 Last Zero Detected:")
        print(f"   Price: {last_zero_detection['price']:.{decimal_places}f} | Digit: {last_zero_detection['digit']}")
        print(f"   Tick: #{last_zero_detection['tick']}")
    
    zero_count = len(zero_positions)
    print(f"\n📏 ZERO STATISTICS:")
    print(f"   Avg distance: {zero_entropy:.1f} ticks (Need > {zero_distance_threshold})")
    print(f"   Recent zeros (20): {recent_zero_count} (Need < {recent_zero_threshold})")
    print(f"   Total zeros: {zero_count}")
    print(f"   Current streak: {consecutive_zeros} in a row")
    ticks_since_last = tick_counter - last_zero_tick if last_zero_tick > 0 else 0
    print(f"   Ticks since last zero: {ticks_since_last}")
    
    if len(recent_digits) >= 5:
        last_trend = recent_digits[-5:]
        trend_text = ""
        if all(d in [5,6,7,8,9] for d in last_trend):
            trend_text = "📈 Strong uptrend"
        elif all(d in [0,1,2,3,4] for d in last_trend):
            trend_text = "📉 Strong downtrend"
        else:
            trend_text = "📊 Mixed"
        print(f"\n📈 TREND: {trend_text} {last_trend}")
    
    print(f"\n📊 VOLATILITY:")
    print(f"   Avg range (last 100): {avg_historical_range:.2f}")
    print(f"   Threshold: {dynamic_threshold:.2f}")
    print(f"   Current: {current_volatility:.2f}")
    print(f"   {'✅' if volatility_met else '❌'} Enough movement")
    
    print(f"\n📍 SUPPORT/RESISTANCE:")
    print(f"   Support: {current_support:.{decimal_places}f}")
    print(f"   Resistance: {current_resistance:.{decimal_places}f}")
    if tick_list:
        print(f"   Current: {tick_list[-1]:.{decimal_places}f}")
    print(f"   Status: {price_position_status}")
    
    if next_tick_after_execution:
        print(f"\n🔜 NEXT TICK AFTER LAST EXECUTION:")
        print(f"   Price: {next_tick_after_execution['price']:.{decimal_places}f}")
        print(f"   Digit: {next_tick_after_execution['digit']}")
        print(f"   Tick #{next_tick_after_execution['tick_number']}")
    
    # All conditions summary
    entropy_met = zero_entropy > zero_distance_threshold
    density_met = recent_density_met
    trend_met = trend_allowed
    vol_met = volatility_met
    price_pos_met = price_position_met
    zero_streak_met = consecutive_zero_met
    spacing_met = zero_spacing_met
    statistical_met = statistical_threshold_met
    pattern_met = pattern_safe
    
    conditions_met = [entropy_met, density_met, trend_met, vol_met, price_pos_met, 
                     zero_streak_met, spacing_met, statistical_met, pattern_met]
    
    if zero_entropy > 0:
        if all(conditions_met):
            print(f"\n   ✅ TRADING ALLOWED - All 9 conditions met")
        else:
            print(f"\n   ⏸️  TRADING HOLD - Conditions not met:")
            if not entropy_met: print(f"      ✗ Entropy")
            if not density_met: print(f"      ✗ Density")
            if not trend_met: print(f"      ✗ Trend")
            if not vol_met: print(f"      ✗ Volatility")
            if not price_pos_met: print(f"      ✗ Price")
            if not zero_streak_met: print(f"      ✗ Zero Streak")
            if not spacing_met: print(f"      ✗ Spacing")
            if not statistical_met: print(f"      ✗ Statistical Normal")
            if not pattern_met: print(f"      ✗ Pattern: {pattern_warning}")
    
    print(f"\n📊 STATISTICS:")
    print(f"   Trades: {trade_count}/{MAX_TRADES}")
    if trade_count > 0:
        print(f"   Wins: {win_count}")
        print(f"   Losses: {loss_count}")
        print(f"   Win Rate: {(win_count/trade_count*100):.1f}%")
    
    if last_digit is not None:
        print(f"\n🔢 Last digit: {last_digit}")
    
    if recent_digits:
        last_10 = recent_digits[-10:]
        print(f"\n🔍 Last 10 digits: {last_10}")
    
    if active_trade:
        status_emoji = "💰" if active_trade['status'] == 'buy_sent' else "💹"
        print(f"\n{status_emoji} Active Trade:")
        print(f"   Type: {active_trade['trade_type']}")
        if 'detection_price' in active_trade:
            print(f"   Detected at: {active_trade['detection_price']:.{decimal_places}f} (Digit: {active_trade['detection_digit']})")
        if 'execution_price' in active_trade:
            print(f"   Executed at: {active_trade['execution_price']:.{decimal_places}f}")
        print(f"   Status: {active_trade['status']}")
    
    if tick_list:
        current = tick_list[-1]
        digit = get_last_digit(current)
        print(f"\n📈 Current Tick: {current:.{decimal_places}f} | Digit: {digit}")
    
    runtime = datetime.now() - script_start_time
    minutes = runtime.seconds // 60
    seconds = runtime.seconds % 60
    print(f"\n🕒 Runtime: {minutes}m {seconds}s")
    print("=" * 120)

def on_message(ws, message):
    global tick_list, tick_counter, ws_connection, last_digit, history_processed, bot_running
    global next_tick_after_execution, execution_tick_number, waiting_for_next_tick
    
    if not bot_running:
        return
    
    ws_connection = ws
    
    try:
        data = json.loads(message)
        
        check_trade_outcome(data)
        
        if not bot_running:
            return
        
        if 'msg_type' in data and data['msg_type'] == "tick":
            tick = data['tick']
            price = float(tick['quote'])
            tick_counter += 1
            tick_list.append(price)
            
            if len(tick_list) > tick_history_limit:
                tick_list.pop(0)
            
            digit = get_last_digit(price)
            last_digit = digit
            
            # Track the next tick after execution
            if execution_tick_number > 0 and tick_counter == execution_tick_number + 1:
                next_tick_after_execution = {
                    'price': price,
                    'digit': digit,
                    'tick_number': tick_counter,
                    'time': datetime.now()
                }
                print(f"\n🔜 NEXT TICK AFTER EXECUTION CAPTURED: {price:.{decimal_places}f} | Digit: {digit}")
                
                popup_data = {
                    'Execution Tick #': execution_tick_number,
                    'Next Tick #': tick_counter,
                    'Next Price': f"{price:.{decimal_places}f}",
                    'Next Digit': digit,
                    'Time': datetime.now().strftime('%H:%M:%S')
                }
                show_important_message("🔜 NEXT TICK AFTER EXECUTION DETECTED", popup_data)
            
            print(f"\n📊 TICK #{tick_counter}: Price={price:.{decimal_places}f}, Digit={digit}")
            
            # If waiting for next tick after last trade, show it and shut down
            if waiting_for_next_tick:
                print("\n" + "!" * 80)
                print("!!! 🔜 NEXT TICK AFTER FINAL TRADE !!!".center(80))
                print("!" * 80)
                print(f"   Price: {price:.{decimal_places}f}")
                print(f"   Digit: {digit}")
                print(f"   Tick #{tick_counter}")
                print("!" * 80)
                print("\n" + "=" * 80)
                print("🔴 Bot will now shut down in 5 seconds")
                print("=" * 80)
                time.sleep(5)
                shutdown_bot()
            
            check_consecutive_zeros(digit)
            check_zero_spacing(tick_counter, digit)
            calculate_dynamic_threshold()
            check_volatility()
            check_support_resistance()
            update_recent_density(digit)
            
            # Run pattern detection
            detect_dangerous_patterns()
            
            # Check statistical spacing
            check_statistical_spacing(tick_counter)
            
            zero_appeared = update_zero_positions(tick_counter, digit)
            
            if zero_appeared and trade_count < MAX_TRADES and authorized and not active_trade and history_processed and bot_running:
                debug_filters_before_trade()
                time.sleep(0.1)
                place_over_zero_trade(price, digit, tick_counter)
            
            display_status()
        
        elif 'msg_type' in data and data['msg_type'] == "history":
            if 'history' in data and 'prices' in data['history']:
                prices = data['history']['prices']
                tick_list = [float(p) for p in prices]
                tick_counter = len(tick_list)
                
                print(f"\n📥 Loaded {len(tick_list)} historical ticks")
                
                for i, price in enumerate(tick_list, 1):
                    digit = get_last_digit(price)
                    if i > len(tick_list) - 20:
                        recent_digits.append(digit)
                    if digit == 0:
                        zero_positions.append(i)
                    if i == len(tick_list):
                        last_digit = digit
                
                recent_zero_count = sum(1 for d in recent_digits if d == 0)
                recent_density_met = recent_zero_count < recent_zero_threshold
                calculate_dynamic_threshold()
                check_trend()
                check_volatility()
                check_support_resistance()
                calculate_zero_entropy_from_history()
                display_status()
            
    except Exception as e:
        print(f"Error in on_message: {e}")

def on_open(ws):
    global ws_connection
    ws_connection = ws
    print("📡 Connected to Deriv WebSocket")
    
    auth_request = {"authorize": API_TOKEN}
    ws.send(json.dumps(auth_request))
    print("🔐 Authorization sent...")
    
    time.sleep(0.5)
    
    history_request = {
        "ticks_history": symbol,
        "count": tick_history_limit,
        "end": "latest",
        "style": "ticks"
    }
    ws.send(json.dumps(history_request))
    print(f"📊 Requesting {tick_history_limit} historical ticks...")
    
    time.sleep(1)
    
    subscribe_request = {
        "ticks": symbol,
        "subscribe": 1
    }
    ws.send(json.dumps(subscribe_request))
    print("📊 Subscribed to live tick stream...")

def on_error(ws, error):
    print(f"❌ Error: {error}")

def on_close(ws, close_status_code, close_msg):
    global bot_running
    if bot_running:
        print("\n🔌 Connection closed. Reconnecting in 5 seconds...")
        time.sleep(5)
        run_websocket()

def run_websocket():
    global ws_connection, bot_running
    if not bot_running:
        return
        
    ws_url = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
    ws = websocket.WebSocketApp(
        ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws_connection = ws
    ws.run_forever()

if __name__ == "__main__":
    get_user_input()
    run_websocket()