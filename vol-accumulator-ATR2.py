import asyncio
import websockets
import json
from collections import deque
from datetime import datetime
from dotenv import load_dotenv
import os

# ---------------- CONFIG ----------------
load_dotenv()
SYMBOL = "1HZ10V"
APP_ID = os.getenv("DERIV_APP_ID") or "80958"
DERIV_TOKEN = os.getenv("DERIV_API_TOKEN")
ATR_PERIOD = 14
VOL_WINDOW = 20
LOG_FILE = "atr_volatility_spike_log.txt"
STAKE = 50
MAX_TRADES = 1
TAKE_PROFIT = 5
CURRENCY = "USD"
COOLDOWN_TICKS = 3  # adjustable cooldown in ticks

# ---------------- HELPERS ----------------
def log(message: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {message}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def to_pips(diff):
    return round(diff * 1000, 2)

def calculate_atr(tr_values, period=ATR_PERIOD):
    if len(tr_values) < period:
        return sum(tr_values) / max(len(tr_values), 1)
    atr = tr_values[0]
    for tr in tr_values[1:period]:
        atr = (atr * (period - 1) + tr) / period
    return atr

# ---------------- DERIV API ----------------
async def send(ws, payload):
    await ws.send(json.dumps(payload))

async def recv(ws):
    msg_text = await ws.recv()
    msg = json.loads(msg_text)
    if "error" in msg:
        raise RuntimeError(msg["error"])
    return msg

async def authorize(ws):
    await send(ws, {"authorize": DERIV_TOKEN})
    while True:
        msg = await recv(ws)
        if msg.get("msg_type") == "authorize":
            loginid = msg.get("authorize", {}).get("loginid")
            log(f"🔑 Authorized as {loginid}")
            return

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

# ---------------- MAIN BOT ----------------
async def tick_stream(spike_threshold_pips: float):
    uri = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

    async with websockets.connect(uri) as ws:
        await authorize(ws)
        await send(ws, {"ticks": SYMBOL, "subscribe": 1})
        log(f"📡 Subscribed to {SYMBOL} | Conservative bot active")

        prev_price = None
        tr_window = deque(maxlen=ATR_PERIOD)
        atr_window = deque(maxlen=VOL_WINDOW)
        last_5_moves = deque(maxlen=5)
        prev_20_spikes = deque(maxlen=20)
        trade_count = 0
        cooldown = 0  # cooldown counter

        while True:
            msg = await ws.recv()
            data = json.loads(msg)
            if "tick" not in data:
                continue

            tick = data["tick"]
            price = float(tick["quote"])
            epoch = tick["epoch"]

            if prev_price is None:
                log(f"[{epoch}] First tick: {price:.3f}")
                prev_price = price
                continue

            # --- PRICE MOVEMENT ---
            price_change = price - prev_price
            abs_price_change = abs(price_change)
            pip_move = to_pips(price_change)
            abs_pip_move = abs(pip_move)

            # --- Spike detection ---
            spike_signal = ""
            if abs_pip_move >= spike_threshold_pips:
                spike_signal = "🔥 HIGH SPIKE DETECTED"
                prev_20_spikes.append(True)
            else:
                prev_20_spikes.append(False)

            # --- ATR using price ---
            tr_window.append(abs_price_change)
            atr = calculate_atr(list(tr_window), ATR_PERIOD)
            atr_window.append(atr)

            # --- Volatility trend ---
            vol_signal = "CALCULATING"
            if len(atr_window) == VOL_WINDOW:
                first_half = list(atr_window)[:VOL_WINDOW//2]
                second_half = list(atr_window)[VOL_WINDOW//2:]
                avg_first = sum(first_half) / len(first_half)
                avg_second = sum(second_half) / len(second_half)
                if avg_second > avg_first * 1.01:
                    vol_signal = "EXPANDING"
                elif avg_second < avg_first * 0.99:
                    vol_signal = "CONTRACTING"
                else:
                    vol_signal = "STABLE"

            # --- Update last 5 moves ---
            last_5_moves.append(abs_pip_move)

            # --- Prepare cooldown display ---
            cooldown_text = f" | ⏳ Cooldown: {cooldown} ticks remaining" if cooldown > 0 else ""

            # --- Log tick info ---
            log(
                f"[{epoch}] Price: {price:.3f} | ΔPrice: {price_change:+.5f} | "
                f"Pips: {pip_move:+.2f} | ATR({ATR_PERIOD}): {atr:.5f} | "
                f"Volatility: {vol_signal} {spike_signal} | "
                f"Last 5 moves: {list(last_5_moves)} | Trades: {trade_count}{cooldown_text}"
            )

            # --- Handle cooldown countdown ---
            if cooldown > 0:
                cooldown -= 1
                prev_price = price
                continue  # skip trading while in cooldown

            # --- Check conservative trade conditions ---
            conditions_met = (
                vol_signal == "CONTRACTING" and
                atr <= 0.08 and
                all(m <= 120 for m in last_5_moves) and
                all(not s for s in prev_20_spikes)
            )

            if conditions_met and trade_count < MAX_TRADES:
                try:
                    proposal_id, quote = await proposal_accu(ws)
                    contract_id = await buy(ws, proposal_id)
                    trade_count += 1
                    log(f"✅ Trade executed! Contract ID: {contract_id} | Total trades: {trade_count}")
                    # reset previous 20 spikes after trade
                    prev_20_spikes.clear()
                    # start cooldown
                    cooldown = COOLDOWN_TICKS
                    log(f"🧊 Cooldown started: {COOLDOWN_TICKS} ticks")
                except Exception as e:
                    log(f"⚠️ Trade failed: {e}")

            prev_price = price

# ---------------- ENTRY ----------------
if __name__ == "__main__":
    try:
        spike_input = input("Enter pip spike threshold (e.g., 200): ").strip()
        spike_threshold_pips = float(spike_input)
        asyncio.run(tick_stream(spike_threshold_pips))
    except KeyboardInterrupt:
        log("🛑 Bot stopped by user.")
