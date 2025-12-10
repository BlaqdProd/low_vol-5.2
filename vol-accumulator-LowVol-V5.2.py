import asyncio
import websockets
import json
import os
from datetime import datetime
from dotenv import load_dotenv
import statistics
import sys
from collections import deque, defaultdict

# ---------------- ENV / CONFIG ----------------
load_dotenv()
DERIV_TOKEN = os.getenv("DERIV_API_TOKEN")
if not DERIV_TOKEN:
    print("ERROR: DERIV_API_TOKEN not found in environment. Exiting.")
    sys.exit(1)
APP_ID = os.getenv("DERIV_APP_ID") or "80958"

SYMBOL = "1HZ10V"
STAKE = 50
TAKE_PROFIT = 5
CURRENCY = "USD"

STD_WINDOW = 60                # ticks used for STD calculation
STD_MAX = 260.0                # upper STD in pips threshold fallback
STD_MIN = 180.0                # lower STD in pips threshold fallback

MAX_TRADES = 1                # maximum trades per session
COOLDOWN_TICKS = 3             # ticks to wait after each trade before allowing another trade
MONITOR_TICKS_AFTER_TRADE = 3  # how many ticks after trade to watch for breakout spike (~1 sec)

LOG_FILE = "consol_expansion_bot.log"

# --- Confluence parameters (low-spike exposure stack) ---
ATR_WINDOW = 60                # ATR lookback window (in ticks) for ATR(14)
ATR_THRESHOLD = 160.0          # ATR threshold in pips (must be less than this to allow trade)

STD_Z_HISTORY = 200            # number of recent std_pips values used for z-score
Z_THRESHOLD = 0.3              # max allowed z-score for current STD

TICK_DELAY_WINDOW = 20         # how many recent tick delays to average (for tick speed)
TICK_DELAY_THRESHOLD_MS = 80   # average tick delay (ms) must be > this (i.e., ticks must be relatively slow)

MICRO_SPIKE_HISTORY = 10       # check last N moves for local spikes
MICRO_SPIKE_LIMIT = 230.0      # if any move > this in last MICRO_SPIKE_HISTORY ticks, block entry

# Strategy warm-up
STRATEGY_WARMUP_TICKS = 150    # collect 150 ticks before entering study

# ---------------- Study-phase settings ----------------
STUDY_TICKS = 300                # number of finalized std observations to collect for study
STUDY_LOOKAHEAD = 3              # watch this many ticks after each studied tick for a spike
BUCKET_WIDTH = 300.0              # pips bucket width for grouping STD(pips)
MIN_SAMPLES_PER_BUCKET = 10       # require at least this many samples to consider a bucket
SPIKE_DETECT_THRESHOLD = MICRO_SPIKE_LIMIT  # treat moves > this as a spike for study

# ---------------- Moderate looseness & multi-bucket settings ----------------
LOOSENESS_OFFSET = 25.0         # widen each chosen STD bucket by ±25 pips
NEAR_RANGE = 30.0               # allow up to ±30 pips beyond widened zone with extra checks
RECENT_SMALL_MOVE_PIPS = 60.0   # require last 3 moves < this for near-boundary acceptance
RELAXED_SPIKE_LIMIT = 350.0     # relaxed micro-spike threshold when within near-boundary allowance

# ---------------- Multi-bucket risk threshold (Option A) ----------------
RISK_THRESHOLD = 0.25           # choose all buckets with spike rate <= this (40%)

# ---------------- Helpers ----------------
def log(message: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {message}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def to_pips(diff: float) -> float:
    """Convert price difference to pips (1 pip = 0.001)."""
    return round(diff * 1000, 2)

def std_to_pips(std_value: float) -> float:
    return round(std_value * 1000, 2)

async def send(ws, payload):
    await ws.send(json.dumps(payload))

async def recv(ws):
    msg_text = await ws.recv()
    msg = json.loads(msg_text)
    if "error" in msg:
        raise RuntimeError(msg["error"])
    return msg

# ---------------- API Calls ----------------
async def authorize(ws):
    await send(ws, {"authorize": DERIV_TOKEN})
    resp = await recv(ws)
    loginid = resp.get("authorize", {}).get("loginid")
    log(f"🔑 Authorized as {loginid}")

async def proposal_accu(ws):
    payload = {
        "proposal": 1,
        "amount": STAKE,
        "basis": "stake",
        "contract_type": "ACCU",
        "currency": CURRENCY,
        "symbol": SYMBOL,
        "growth_rate": 0.05,
        "limit_order": {"take_profit": TAKE_PROFIT},
    }
    await send(ws, payload)
    while True:
        msg = await recv(ws)
        if msg.get("msg_type") == "proposal":
            return msg["proposal"]["id"], msg["proposal"]

async def buy(ws, proposal_id):
    await send(ws, {"buy": proposal_id, "price": STAKE})
    while True:
        msg = await recv(ws)
        if msg.get("msg_type") == "buy":
            return msg["buy"]["contract_id"]

# ---------------- Confluence utility functions ----------------
def compute_atr(pip_moves_deque: deque, window: int) -> float:
    if len(pip_moves_deque) < 1:
        return float("inf")
    arr = list(pip_moves_deque)[-window:]
    if not arr:
        return float("inf")
    return sum(abs(x) for x in arr) / len(arr)

def compute_std_zscore(std_history: deque, current_std: float) -> float:
    if not std_history or len(std_history) < 10:
        return 0.0
    mean = statistics.mean(std_history)
    stdev = statistics.pstdev(std_history)
    if stdev == 0:
        return 0.0
    return (current_std - mean) / stdev

def avg_tick_delay_ms(delay_deque: deque) -> float:
    if not delay_deque:
        return float("inf")
    return (sum(delay_deque) / len(delay_deque)) * 1000.0

def recent_micro_spike(pip_moves_deque: deque, limit: float, lookback: int) -> bool:
    arr = list(pip_moves_deque)[-lookback:]
    return any(abs(x) > limit for x in arr)

# ---------------- STATE MACHINE BOT ----------------
async def tick_stream(breakout_pip: float):
    global STD_MIN, STD_MAX
    uri = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
    log(f"Starting bot for symbol={SYMBOL} | breakout_pip={breakout_pip} | STRATEGY_WARMUP_TICKS={STRATEGY_WARMUP_TICKS}")
    async with websockets.connect(uri) as ws:
        await authorize(ws)
        await send(ws, {"ticks": SYMBOL, "subscribe": 1})
        log(f"📡 Subscribed to {SYMBOL} — collecting warm-up then studying STD behavior")

        price_history = deque(maxlen=STD_WINDOW + 50)
        prev_price = None

        state = "IDLE"
        expansion_high = None

        trade_count = 0
        cooldown = 0

        monitoring = None

        win_count = 2
        loss_count = 2

        pip_moves = deque(maxlen=500)
        std_history = deque(maxlen=STD_Z_HISTORY)
        tick_delays = deque(maxlen=TICK_DELAY_WINDOW)
        last_tick_epoch = None

        warmup_counter = 0
        warmup_complete = False

        # study trackers
        study_active = False
        study_done = False
        first_study_shown = False   # show full alert only for the first study
        study_entries = deque()
        study_finalized = 0
        bucket_total = defaultdict(int)
        bucket_spikes = defaultdict(int)

        allowed_buckets = []  # list of (low, high) pips ranges considered safe
        def bucket_of(std_pips):
            return (int(std_pips // BUCKET_WIDTH)) * BUCKET_WIDTH

        # helper to compare bucket sets
        def buckets_from_totals(bucket_total, bucket_spikes, risk_threshold):
            candidates = []
            for b, total in bucket_total.items():
                if total < MIN_SAMPLES_PER_BUCKET:
                    continue
                spikes = bucket_spikes.get(b, 0)
                rate = spikes / total
                if rate <= risk_threshold:
                    candidates.append(b)
            return sorted(candidates)

        while True:
            raw_msg = await ws.recv()
            data = json.loads(raw_msg)

            if "tick" not in data:
                continue

            tick = data["tick"]
            price = float(tick["quote"])
            epoch = float(tick["epoch"])

            if last_tick_epoch is None:
                tick_delay = None
            else:
                tick_delay = max(0.0, epoch - last_tick_epoch)
                tick_delays.append(tick_delay)
            last_tick_epoch = epoch

            price_history.append(price)
            raw_std = None
            std_pips = None
            if len(price_history) >= STD_WINDOW:
                raw_std = statistics.pstdev(list(price_history)[-STD_WINDOW:])
                std_pips = std_to_pips(raw_std)
                std_history.append(std_pips)

            if prev_price is None:
                prev_price = price
                warmup_counter += 1
                if warmup_counter >= STRATEGY_WARMUP_TICKS:
                    warmup_complete = False
                    log(f"[WARM-UP] Collected {warmup_counter}/{STRATEGY_WARMUP_TICKS} ticks (waiting for indicators)")
                else:
                    if warmup_counter % 10 == 0:
                        log(f"[WARM-UP] Collected {warmup_counter}/{STRATEGY_WARMUP_TICKS} ticks.")
                continue

            pip_move = to_pips(price - prev_price)
            abs_pip_move = abs(pip_move)
            pip_moves.append(pip_move)
            prev_price = price

            # ---------- indicator readiness & warm-up ----------
            std_ready = std_pips is not None
            atr_ready = len(pip_moves) >= ATR_WINDOW
            z_ready = len(std_history) >= 10
            indicators_ready = std_ready and atr_ready and z_ready

            if not warmup_complete:
                warmup_counter += 1
                if warmup_counter % 10 == 0 or warmup_counter == STRATEGY_WARMUP_TICKS:
                    log(f"[WARM-UP] {warmup_counter} ticks collected | STD_ready={std_ready} ATR_ready={atr_ready} Z_ready={z_ready}")
                if warmup_counter >= STRATEGY_WARMUP_TICKS and indicators_ready:
                    warmup_complete = True
                    study_active = True
                    study_done = False
                    first_study_shown = False
                    log("🔥 Warm-up complete — indicators ready. Entering STD study phase.")
                else:
                    continue

            # ---------- STUDY PHASE (initial and rolling) ----------
            if study_active:
                # finalize pending entries whose lookahead expired
                if len(study_entries) > 0:
                    processed = 0
                    while processed < len(study_entries) and len(study_entries) > 0:
                        entry = study_entries[0]
                        entry['remaining'] -= 1
                        # mark spike if current tick breached spike detect threshold
                        if abs_pip_move > SPIKE_DETECT_THRESHOLD:
                            entry['spike'] = True
                        if entry['remaining'] <= 0:
                            b = entry['bucket']
                            bucket_total[b] += 1
                            if entry['spike']:
                                bucket_spikes[b] += 1
                            study_entries.popleft()
                            study_finalized += 1
                        else:
                            break
                        processed += 1

                # add current std observation to pending list
                if std_pips is not None:
                    b = bucket_of(std_pips)
                    study_entries.append({'bucket': b, 'remaining': STUDY_LOOKAHEAD, 'spike': False})

                # when enough finalized observations collected -> analyze
                if study_finalized >= STUDY_TICKS:
                    # compute candidate buckets by spike rate
                    candidate_buckets = buckets_from_totals(bucket_total, bucket_spikes, RISK_THRESHOLD)

                    # convert bucket starts to (low, high) ranges
                    new_allowed = [(float(b), float(b + BUCKET_WIDTH)) for b in candidate_buckets]

                    # If this is the first study, show full UI logging (detailed)
                    if not first_study_shown:
                        log("✅ Study collection complete — analyzing STD buckets for spike rates")
                        for b, total in list(bucket_total.items()):
                            if total < MIN_SAMPLES_PER_BUCKET:
                                log(f"  bucket {b:.0f}-{b+BUCKET_WIDTH:.0f} pips -> samples={total} (ignored, too few)")
                                continue
                            spikes = bucket_spikes.get(b, 0)
                            rate = spikes / total
                            log(f"  bucket {b:.0f}-{b+BUCKET_WIDTH:.0f} pips -> samples={total} spikes={spikes} rate={rate:.3f}")
                        if new_allowed:
                            log(f"🎯 First-study selected buckets: {', '.join([f'{low:.0f}-{high:.0f}' for low,high in new_allowed])}")
                            allowed_buckets = new_allowed
                        else:
                            log("⚠️ First-study: no buckets met the risk threshold; keeping fallback STD_MIN/STD_MAX")
                            allowed_buckets = [(STD_MIN, STD_MAX)]
                        first_study_shown = True
                    else:
                        # rolling study - silent unless bucket set changes
                        if new_allowed and set(new_allowed) != set(allowed_buckets):
                            allowed_buckets = new_allowed
                            log(f"🔄 STD profile updated — new safe buckets: {', '.join([f'{low:.0f}-{high:.0f}' for low,high in allowed_buckets])}")
                        # if new_allowed empty: keep previous allowed_buckets

                    # Reset study trackers for next rolling cycle
                    study_entries.clear()
                    study_finalized = 0
                    bucket_total.clear()
                    bucket_spikes.clear()
                    # Keep study_active True so rolling studies continue silently
                else:
                    # still studying
                    # Show progress logs only DURING the FIRST study (to UI) and suppress for rolling studies
                    if not first_study_shown:
                        if study_finalized > 0 and study_finalized % 25 == 0:
                            log(f"[STUDY] finalized {study_finalized}/{STUDY_TICKS} samples")
                    # continue below so tick logs still print
                    pass

            # If first study hasn't shown (i.e. initial study not complete) we must not take trades yet.
            # But we still want tick logs visible. So if first_study_shown == False -> show tick summary and skip trading.
            if not first_study_shown:
                # logging tick summary during initial study
                if std_pips is not None:
                    log(f"[{int(epoch)}] Price: {price:.3f} | Move: {pip_move:+.1f} | STD(pips): {std_pips:.1f} | State: {state}")
                else:
                    log(f"[{int(epoch)}] Price: {price:.3f} | Move: {pip_move:+.1f} | STD: calculating... | State: {state}")
                # skip trading until the first study completes
                continue

            # ---------- NORMAL OPERATION (after initial study shown) ----------
            # logging tick summary
            if std_pips is not None:
                log(f"[{int(epoch)}] Price: {price:.3f} | Move: {pip_move:+.1f} | STD(pips): {std_pips:.1f} | State: {state}")
            else:
                log(f"[{int(epoch)}] Price: {price:.3f} | Move: {pip_move:+.1f} | STD: calculating... | State: {state}")

            # FIRST: handle monitoring of the just-executed trade (if any)
            if monitoring:
                monitoring['remaining_ticks'] -= 1
                if abs_pip_move > breakout_pip:
                    log(f"❌ LOSS #{loss_count} | Contract {monitoring['contract_id']} | Spike {abs_pip_move} > Breakout {breakout_pip}")
                    loss_count += 1
                    monitoring = None
                else:
                    if monitoring['remaining_ticks'] <= 0:
                        log(f"✅ WIN #{win_count} | Contract {monitoring['contract_id']} | No spike > {breakout_pip}")
                        win_count += 1
                        monitoring = None

            # then handle cooldown (prevents immediate re-trading)
            if cooldown > 0:
                cooldown -= 1
                prev_price = price
                continue

            # Max trades enforcement
            if trade_count >= MAX_TRADES:
                log("🛑 Max trades reached. Stopping bot.")
                log(f"Summary → Trades: {trade_count} | Wins started@2 -> {win_count - 1} | Losses started@2 -> {loss_count - 1}")
                break

            # ---------- confluence checks ----------
            atr = compute_atr(pip_moves, ATR_WINDOW) if pip_moves else float("inf")
            std_z = compute_std_zscore(std_history, std_pips) if std_pips is not None else 0.0
            avg_delay_ms = avg_tick_delay_ms(tick_delays)

            # dynamic micro spike check with default limit
            micro_spike = recent_micro_spike(pip_moves, MICRO_SPIKE_LIMIT, MICRO_SPIKE_HISTORY)

            # Helper: check if std_pips falls inside any allowed bucket
            def std_in_allowed_buckets(std_val):
                if not allowed_buckets:
                    return False
                for low, high in allowed_buckets:
                    if low <= std_val <= high:
                        return True
                return False

            # Build allowed ranges with looseness and near-boundary windows per bucket
            widened_ranges = [(low - LOOSENESS_OFFSET, high + LOOSENESS_OFFSET) for (low, high) in allowed_buckets]
            near_ranges = [(w_low - NEAR_RANGE, w_high + NEAR_RANGE) for (w_low, w_high) in widened_ranges]

            def last_n_moves_small(n=3, threshold=RECENT_SMALL_MOVE_PIPS):
                if not pip_moves:
                    return False
                arr = list(pip_moves)[-n:]
                if not arr:
                    return False
                return max(abs(x) for x in arr) < threshold

            def any_recent_micro_spike(limit):
                return recent_micro_spike(pip_moves, limit, MICRO_SPIKE_HISTORY)

            # confluence tight: std in any widened_range
            def confluences_ok_tight(current_std_pips):
                if current_std_pips is None:
                    return False
                # STD must be in any widened range
                in_widened = any((low <= current_std_pips <= high) for (low, high) in widened_ranges)
                if not in_widened:
                    return False
                if atr >= ATR_THRESHOLD:
                    return False
                if std_z >= Z_THRESHOLD:
                    return False
                if avg_delay_ms != float("inf") and avg_delay_ms < TICK_DELAY_THRESHOLD_MS:
                    return False
                if any_recent_micro_spike(MICRO_SPIKE_LIMIT):
                    return False
                return True

            # confluence near: std in near range but outside widened, require calm moves and relaxed spike limit
            def confluences_ok_near(current_std_pips):
                if current_std_pips is None:
                    return False
                # must be in any near_range and strictly outside corresponding widened range
                in_near = False
                for (nlow, nhigh), (wlow, whigh) in zip(near_ranges, widened_ranges):
                    if nlow <= current_std_pips <= nhigh and not (wlow <= current_std_pips <= whigh):
                        in_near = True
                        break
                if not in_near:
                    return False
                # last moves must be calm
                if not last_n_moves_small(3, RECENT_SMALL_MOVE_PIPS):
                    return False
                if atr >= ATR_THRESHOLD:
                    return False
                if std_z >= Z_THRESHOLD:
                    return False
                if avg_delay_ms != float("inf") and avg_delay_ms < TICK_DELAY_THRESHOLD_MS:
                    return False
                # use relaxed spike limit
                if any_recent_micro_spike(RELAXED_SPIKE_LIMIT):
                    return False
                return True

            def confluences_ok(current_std_pips):
                # pass if tight OR near
                return confluences_ok_tight(current_std_pips) or confluences_ok_near(current_std_pips)

            # ---------------- STATE MACHINE ----------------
            if state == "IDLE":
                if pip_move == 0 and std_pips is not None and confluences_ok(std_pips):
                    state = "AWAIT_EXPANSION"
                    log(f"🔎 Consolidation detected at tick {int(epoch)} (STD {std_pips:.1f} pips, ATR {atr:.1f} pips, avg_delay_ms {avg_delay_ms if avg_delay_ms!=float('inf') else 'N/A'}). Waiting for expansion tick.")
            elif state == "AWAIT_EXPANSION":
                if abs_pip_move > 0:
                    expansion_high = abs_pip_move
                    state = "EXPANSION"
                    log(f"⚡ Expansion started: expansion_high set to {expansion_high:.1f} pips")
                else:
                    log("↪ Expansion tick still zero, waiting for a non-zero expansion tick...")
            elif state == "EXPANSION":
                if abs_pip_move > expansion_high:
                    expansion_high = abs_pip_move
                    log(f"⬆ Expansion extended: new expansion_high = {expansion_high:.1f} pips")
                elif abs_pip_move < expansion_high:
                    if std_pips is not None and confluences_ok(std_pips):
                        log(f"↘ Retracement detected ({abs_pip_move:.1f} < {expansion_high:.1f}) & STD {std_pips:.1f} & ATR {atr:.1f} → Taking trade")
                        try:
                            proposal_id, proposal = await proposal_accu(ws)
                            ask_price = proposal.get('ask_price')
                            log(f"Quoted Ask: {ask_price} | Proposal ID: {proposal_id}")
                            contract_id = await buy(ws, proposal_id)
                            trade_count += 1
                            cooldown = COOLDOWN_TICKS
                            monitoring = {'contract_id': contract_id, 'remaining_ticks': MONITOR_TICKS_AFTER_TRADE}
                            log(f"🎯 Trade Executed! Contract ID: {contract_id} | Trades: {trade_count}/{MAX_TRADES}")
                        except Exception as e:
                            log(f"❌ Trade Error: {e}")
                    else:
                        log(f"⛔ Retracement found but confluences failed (STD {std_pips if std_pips else 'N/A'}, ATR {atr:.1f}, std_z {std_z:.2f}) — skipping trade")
                    state = "IDLE"
                    expansion_high = None

# ---------------- ENTRY ----------------
if __name__ == "__main__":
    try:
        breakout_pip_input = input("Enter breakout pip value to determine win/loss (e.g. 150): ")
        breakout_pip = float(breakout_pip_input)
    except Exception as e:
        print("Invalid breakout pip. Please enter a number. Exiting.")
        sys.exit(1)

    try:
        asyncio.run(tick_stream(breakout_pip))
    except KeyboardInterrupt:
        log("Interrupted by user. Exiting.")
