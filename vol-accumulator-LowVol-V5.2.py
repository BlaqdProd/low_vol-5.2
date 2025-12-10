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

MAX_TRADES = 1                 # maximum trades per session
COOLDOWN_TICKS = 3             # ticks to wait after each trade before allowing another trade
MONITOR_TICKS_AFTER_TRADE = 3  # how many ticks after trade to watch for breakout spike (~1 sec)

LOG_FILE = "consol_expansion_bot.log"

# --- Confluence parameters (low-spike exposure stack) ---
ATR_WINDOW = 60                # ATR lookback window (in ticks)
ATR_THRESHOLD = 140.0          # ATR threshold in pips (must be less than this to allow trade)

STD_Z_HISTORY = 200            # number of recent std_pips values used for z-score
Z_THRESHOLD = 0.3              # max allowed z-score for current STD

TICK_DELAY_WINDOW = 20         # how many recent tick delays to average (for tick speed)
TICK_DELAY_THRESHOLD_MS = 80   # average tick delay (ms) must be > this (i.e., ticks must be relatively slow)

MICRO_SPIKE_HISTORY = 10       # check last N moves for local spikes
MICRO_SPIKE_LIMIT = 230.0      # if any move > this in last MICRO_SPIKE_HISTORY ticks, block entry

# Strategy warm-up
STRATEGY_WARMUP_TICKS = 150    # collect 150 ticks before entering study

# ---------------- Study-phase settings (STD) ----------------
STUDY_TICKS = 300                # number of finalized std observations to collect for study
STUDY_LOOKAHEAD = 3              # watch this many ticks after each studied tick for a spike
BUCKET_WIDTH = 10.0              # pips bucket width for grouping STD(pips)
MIN_SAMPLES_PER_BUCKET = 8       # require at least this many samples to consider a bucket
SPIKE_DETECT_THRESHOLD = MICRO_SPIKE_LIMIT  # treat moves > this as a spike for study

# ---------------- ATR study-phase settings (NEW) ----------------
ATR_STUDY_TICKS = 300             # number of finalized ATR observations to collect
ATR_STUDY_LOOKAHEAD = 3           # lookahead for ATR study
ATR_BUCKET_WIDTH = 10.0           # pips bucket width for ATR grouping
ATR_MIN_SAMPLES_PER_BUCKET = 8
ATR_SPIKE_DETECT_THRESHOLD = MICRO_SPIKE_LIMIT  # reuse same micro-spike threshold

# ---------------- Moderate looseness & multi-bucket settings ----------------
LOOSENESS_OFFSET = 25.0         # widen each chosen STD/ATR bucket by ±25 pips
NEAR_RANGE = 30.0               # allow up to ±30 pips beyond widened zone with extra checks
RECENT_SMALL_MOVE_PIPS = 60.0   # require last 3 moves < this for near-boundary acceptance
RELAXED_SPIKE_LIMIT = 350.0     # relaxed micro-spike threshold when within near-boundary allowance

# ---------------- Multi-bucket risk threshold (Option A) ----------------
RISK_THRESHOLD = 0.30           # choose all buckets with spike rate <= this

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
    """Compute ATR-like average of absolute pip moves over `window` ticks (returns pips)."""
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
        log(f"📡 Subscribed to {SYMBOL} — collecting warm-up then studying STD & ATR behavior")

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

        # ---------------- STD study trackers (existing) ----------------
        study_active = False
        first_study_shown = False   # show full alert only for the first study
        study_entries = deque()
        study_finalized = 0
        bucket_total = defaultdict(int)
        bucket_spikes = defaultdict(int)
        allowed_buckets = []  # list of (low, high) pips ranges considered safe
        def bucket_of(std_pips):
            return (int(std_pips // BUCKET_WIDTH)) * BUCKET_WIDTH

        # ---------------- ATR study trackers (NEW) ----------------
        atr_study_active = False
        atr_first_study_shown = False
        atr_study_entries = deque()
        atr_study_finalized = 0
        atr_bucket_total = defaultdict(int)
        atr_bucket_spikes = defaultdict(int)
        allowed_atr_buckets = []  # list of (low, high) pips ranges considered safe for ATR
        def bucket_of_atr(atr_pips):
            return (int(atr_pips // ATR_BUCKET_WIDTH)) * ATR_BUCKET_WIDTH

        # helper to compare bucket sets (shared)
        def buckets_from_totals(bucket_total_dict, bucket_spikes_dict, risk_threshold, min_samples):
            candidates = []
            for b, total in bucket_total_dict.items():
                if total < min_samples:
                    continue
                spikes = bucket_spikes_dict.get(b, 0)
                rate = spikes / total
                if rate <= risk_threshold:
                    candidates.append(b)
            return sorted(candidates)

        # Main loop
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
                    # activate both studies
                    study_active = True
                    atr_study_active = True
                    first_study_shown = False
                    atr_first_study_shown = False
                    log("🔥 Warm-up complete — indicators ready. Entering STD & ATR study phases.")
                else:
                    continue

            # ---------- STD STUDY PHASE ----------
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
                    candidate_buckets = buckets_from_totals(bucket_total, bucket_spikes, RISK_THRESHOLD, MIN_SAMPLES_PER_BUCKET)
                    new_allowed = [(float(b), float(b + BUCKET_WIDTH)) for b in candidate_buckets]

                    if not first_study_shown:
                        log("✅ STD Study collection complete — analyzing STD buckets for spike rates")
                        for b, total in list(bucket_total.items()):
                            if total < MIN_SAMPLES_PER_BUCKET:
                                log(f"  STD bucket {b:.0f}-{b+BUCKET_WIDTH:.0f} pips -> samples={total} (ignored, too few)")
                                continue
                            spikes = bucket_spikes.get(b, 0)
                            rate = spikes / total
                            log(f"  STD bucket {b:.0f}-{b+BUCKET_WIDTH:.0f} pips -> samples={total} spikes={spikes} rate={rate:.3f}")
                        if new_allowed:
                            log(f"🎯 First-STD-study selected buckets: {', '.join([f'{low:.0f}-{high:.0f}' for low,high in new_allowed])}")
                            allowed_buckets = new_allowed
                        else:
                            log("⚠️ First-STD-study: no buckets met the risk threshold; keeping fallback STD_MIN/STD_MAX")
                            allowed_buckets = [(STD_MIN, STD_MAX)]
                        first_study_shown = True
                    else:
                        if new_allowed and set(new_allowed) != set(allowed_buckets):
                            allowed_buckets = new_allowed
                            log(f"🔄 STD profile updated — new safe buckets: {', '.join([f'{low:.0f}-{high:.0f}' for low,high in allowed_buckets])}")
                    # reset trackers for rolling study
                    study_entries.clear()
                    study_finalized = 0
                    bucket_total.clear()
                    bucket_spikes.clear()
                else:
                    if not first_study_shown:
                        if study_finalized > 0 and study_finalized % 25 == 0:
                            log(f"[STD STUDY] finalized {study_finalized}/{STUDY_TICKS} samples")

            # ---------- ATR STUDY PHASE (NEW) ----------
            # compute atr_pips for study use
            atr_pips = compute_atr(pip_moves, ATR_WINDOW) if pip_moves else None

            if atr_study_active:
                # finalize pending entries whose lookahead expired
                if len(atr_study_entries) > 0:
                    processed = 0
                    while processed < len(atr_study_entries) and len(atr_study_entries) > 0:
                        entry = atr_study_entries[0]
                        entry['remaining'] -= 1
                        # mark spike if current tick breached ATR spike detect threshold
                        if abs_pip_move > ATR_SPIKE_DETECT_THRESHOLD:
                            entry['spike'] = True
                        if entry['remaining'] <= 0:
                            b = entry['bucket']
                            atr_bucket_total[b] += 1
                            if entry['spike']:
                                atr_bucket_spikes[b] += 1
                            atr_study_entries.popleft()
                            atr_study_finalized += 1
                        else:
                            break
                        processed += 1

                # add current ATR observation to pending list
                if atr_pips is not None:
                    b = bucket_of_atr(atr_pips)
                    atr_study_entries.append({'bucket': b, 'remaining': ATR_STUDY_LOOKAHEAD, 'spike': False})

                # when enough finalized observations collected -> analyze ATR buckets
                if atr_study_finalized >= ATR_STUDY_TICKS:
                    candidate_buckets = buckets_from_totals(atr_bucket_total, atr_bucket_spikes, RISK_THRESHOLD, ATR_MIN_SAMPLES_PER_BUCKET)
                    new_allowed_atr = [(float(b), float(b + ATR_BUCKET_WIDTH)) for b in candidate_buckets]

                    if not atr_first_study_shown:
                        log("✅ ATR Study collection complete — analyzing ATR buckets for spike rates")
                        for b, total in list(atr_bucket_total.items()):
                            if total < ATR_MIN_SAMPLES_PER_BUCKET:
                                log(f"  ATR bucket {b:.0f}-{b+ATR_BUCKET_WIDTH:.0f} pips -> samples={total} (ignored, too few)")
                                continue
                            spikes = atr_bucket_spikes.get(b, 0)
                            rate = spikes / total
                            log(f"  ATR bucket {b:.0f}-{b+ATR_BUCKET_WIDTH:.0f} pips -> samples={total} spikes={spikes} rate={rate:.3f}")
                        if new_allowed_atr:
                            log(f"🎯 First-ATR-study selected buckets: {', '.join([f'{low:.0f}-{high:.0f}' for low,high in new_allowed_atr])}")
                            allowed_atr_buckets = new_allowed_atr
                        else:
                            log("⚠️ First-ATR-study: no buckets met the risk threshold; keeping fallback wide ATR range")
                            allowed_atr_buckets = [(0.0, 9999.0)]
                        atr_first_study_shown = True
                    else:
                        if new_allowed_atr and set(new_allowed_atr) != set(allowed_atr_buckets):
                            allowed_atr_buckets = new_allowed_atr
                            log(f"🔄 ATR profile updated — new safe buckets: {', '.join([f'{low:.0f}-{high:.0f}' for low,high in allowed_atr_buckets])}")

                    # reset trackers for rolling ATR study
                    atr_study_entries.clear()
                    atr_study_finalized = 0
                    atr_bucket_total.clear()
                    atr_bucket_spikes.clear()
                else:
                    if not atr_first_study_shown:
                        if atr_study_finalized > 0 and atr_study_finalized % 25 == 0:
                            log(f"[ATR STUDY] finalized {atr_study_finalized}/{ATR_STUDY_TICKS} samples")

            # If either first STD study or first ATR study hasn't shown (i.e., initial studies not complete)
            # we must not take trades yet. But still print tick logs.
            if not (first_study_shown and atr_first_study_shown):
                # logging tick summary during initial study (use safe display strings)
                std_display = f"{std_pips:.1f}" if std_pips is not None else "N/A"
                atrp_display = f"{atr_pips:.1f}" if atr_pips is not None else "N/A"
                log(f"[{int(epoch)}] Price: {price:.3f} | Move: {pip_move:+.1f} | STD(pips): {std_display} | ATR(pips): {atrp_display} | State: {state}")
                # skip trading until both first studies complete
                continue

            # ---------- NORMAL OPERATION (after initial studies shown) ----------
            # logging tick summary (use safe display strings)
            std_display = f"{std_pips:.1f}" if std_pips is not None else "N/A"
            atrp_display = f"{atr_pips:.1f}" if atr_pips is not None else "N/A"
            log(f"[{int(epoch)}] Price: {price:.3f} | Move: {pip_move:+.1f} | STD(pips): {std_display} | ATR(pips): {atrp_display} | State: {state}")

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

            # Build allowed ranges with looseness and near-boundary windows per bucket (for both STD and ATR)
            widened_std_ranges = [(low - LOOSENESS_OFFSET, high + LOOSENESS_OFFSET) for (low, high) in allowed_buckets]
            near_std_ranges = [(w_low - NEAR_RANGE, w_high + NEAR_RANGE) for (w_low, w_high) in widened_std_ranges]

            widened_atr_ranges = [(low - LOOSENESS_OFFSET, high + LOOSENESS_OFFSET) for (low, high) in allowed_atr_buckets]
            near_atr_ranges = [(w_low - NEAR_RANGE, w_high + NEAR_RANGE) for (w_low, w_high) in widened_atr_ranges]

            def last_n_moves_small(n=3, threshold=RECENT_SMALL_MOVE_PIPS):
                if not pip_moves:
                    return False
                arr = list(pip_moves)[-n:]
                if not arr:
                    return False
                return max(abs(x) for x in arr) < threshold

            def any_recent_micro_spike(limit):
                return recent_micro_spike(pip_moves, limit, MICRO_SPIKE_HISTORY)

            # confluence tight: std in any widened_range AND atr in widened_atr_ranges
            def confluences_ok_tight(current_std_pips, current_atr_pips):
                if current_std_pips is None or current_atr_pips is None:
                    return False
                # STD must be in any widened range
                in_std_widened = any((low <= current_std_pips <= high) for (low, high) in widened_std_ranges) if widened_std_ranges else False
                if not in_std_widened:
                    return False
                # ATR must also be in any widened ATR range
                in_atr_widened = any((low <= current_atr_pips <= high) for (low, high) in widened_atr_ranges) if widened_atr_ranges else False
                if not in_atr_widened:
                    return False
                if current_atr_pips >= ATR_THRESHOLD:
                    return False
                if current_std_pips is not None and std_z >= Z_THRESHOLD:
                    return False
                if avg_delay_ms != float("inf") and avg_delay_ms < TICK_DELAY_THRESHOLD_MS:
                    return False
                if any_recent_micro_spike(MICRO_SPIKE_LIMIT):
                    return False
                return True

            # confluence near: std in near range (but outside widened) AND atr in near range (but outside widened),
            # require calm recent moves and relaxed spike limit
            def confluences_ok_near(current_std_pips, current_atr_pips):
                if current_std_pips is None or current_atr_pips is None:
                    return False
                # STD near-match check
                in_std_near = False
                for (nlow, nhigh), (wlow, whigh) in zip(near_std_ranges, widened_std_ranges):
                    if nlow <= current_std_pips <= nhigh and not (wlow <= current_std_pips <= whigh):
                        in_std_near = True
                        break
                if not in_std_near:
                    return False
                # ATR near-match check
                in_atr_near = False
                for (nlow, nhigh), (wlow, whigh) in zip(near_atr_ranges, widened_atr_ranges):
                    if nlow <= current_atr_pips <= nhigh and not (wlow <= current_atr_pips <= whigh):
                        in_atr_near = True
                        break
                if not in_atr_near:
                    return False
                # last moves must be calm
                if not last_n_moves_small(3, RECENT_SMALL_MOVE_PIPS):
                    return False
                if current_atr_pips >= ATR_THRESHOLD:
                    return False
                if std_z >= Z_THRESHOLD:
                    return False
                if avg_delay_ms != float("inf") and avg_delay_ms < TICK_DELAY_THRESHOLD_MS:
                    return False
                # use relaxed spike limit
                if any_recent_micro_spike(RELAXED_SPIKE_LIMIT):
                    return False
                return True

            def confluences_ok(current_std_pips, current_atr_pips):
                # pass if tight OR near (both indicators must satisfy their version)
                return confluences_ok_tight(current_std_pips, current_atr_pips) or confluences_ok_near(current_std_pips, current_atr_pips)

            # ---------------- STATE MACHINE ----------------
            if state == "IDLE":
                # require zero pip_move (consolidation) and confluences ok
                if pip_move == 0 and std_pips is not None and atr is not None and confluences_ok(std_pips, atr):
                    state = "AWAIT_EXPANSION"
                    avg_delay_display = f"{avg_delay_ms:.0f}" if avg_delay_ms != float("inf") else "N/A"
                    log(f"🔎 Consolidation detected at tick {int(epoch)} (STD {std_pips:.1f} pips, ATR {atr:.1f} pips, avg_delay_ms {avg_delay_display} ms). Waiting for expansion tick.")
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
                    # prepare safe display strings
                    std_display = f"{std_pips:.1f}" if std_pips is not None else "N/A"
                    atr_display = f"{atr:.1f}" if atr is not None else "N/A"
                    if std_pips is not None and confluences_ok(std_pips, atr):
                        log(f"↘ Retracement detected ({abs_pip_move:.1f} < {expansion_high:.1f}) & STD {std_display} & ATR {atr_display} → Taking trade")
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
                        # show clear, safe display strings in the skip message
                        std_display = f"{std_pips:.1f}" if std_pips is not None else "N/A"
                        atr_display = f"{atr:.1f}" if atr is not None else "N/A"
                        log(f"⛔ Retracement found but confluences failed (STD {std_display}, ATR {atr_display}, std_z {std_z:.2f}) — skipping trade")
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
