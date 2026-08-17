"""
CoinDCX XAUUSDT (Gold) Futures - PAPER TRADING Bot (GitHub Actions version)
------------------------------------------------------------------------------
This version runs ONCE per invocation - GitHub Actions triggers it every
5 minutes via a cron schedule (see .github/workflows/run-bot.yml).

State (capital, open position, last processed candle time) is persisted to
state.json. The GitHub Actions workflow commits this file (and the trade
log CSV) back to the repo after every run, so progress survives between runs
without needing any server of your own running 24/7.

Strategy: 5 EMA / 9 EMA crossover with 50 EMA trend filter
  Entry Long  : EMA5 crosses ABOVE EMA9, AND both EMA5 & EMA9 are ABOVE EMA50
  Entry Short : EMA5 crosses BELOW EMA9, AND both EMA5 & EMA9 are BELOW EMA50
  Exit        : EMA5 and EMA9 cross the opposite side
  Timeframe   : 5 minute candles
  Pair        : B-XAU_USDT

Capital & Risk (paper/simulated):
  Starting capital : 1000 (unit = quote currency, USDT)
  Capital per trade: 50% of current capital used as margin
  Leverage         : 10x

100% PAPER TRADING - only public endpoints, no API key needed, no real orders.
"""

import time
import csv
import os
import json
from datetime import datetime, timezone

import requests

# ---------------- CONFIG ----------------
PAIR = "B-XAU_USDT"
RESOLUTION = "5"
CANDLE_DURATION_MS = 5 * 60 * 1000

STARTING_CAPITAL = 1000.0
CAPITAL_PER_TRADE_PCT = 0.5
LEVERAGE = 10
FEE_RATE = 0.0005

HISTORY_HOURS = 72  # lookback fetched every run - enough for EMA50 to stabilize

EMA_FAST = 5
EMA_MED = 9
EMA_SLOW = 50

STATE_FILE = "state.json"
TRADE_LOG_FILE = "xauusdt_paper_trades.csv"
CANDLES_URL = "https://public.coindcx.com/market_data/candlesticks"


# ---------------- DATA FETCH ----------------
def fetch_candles(lookback_hours):
    now = int(time.time())
    frm = now - lookback_hours * 3600
    params = {
        "pair": PAIR,
        "from": frm,
        "to": now,
        "resolution": RESOLUTION,
        "pcode": "f",
    }
    resp = requests.get(CANDLES_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    dedup = {c["time"]: c for c in data}
    return [dedup[t] for t in sorted(dedup)]


def only_closed_candles(candles):
    now_ms = int(time.time() * 1000)
    return [c for c in candles if c["time"] + CANDLE_DURATION_MS <= now_ms]


# ---------------- INDICATORS ----------------
def compute_ema_series(values, period):
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def add_emas(candles):
    closes = [c["close"] for c in candles]
    ema5 = compute_ema_series(closes, EMA_FAST)
    ema9 = compute_ema_series(closes, EMA_MED)
    ema50 = compute_ema_series(closes, EMA_SLOW)
    for i, c in enumerate(candles):
        c["ema5"] = ema5[i]
        c["ema9"] = ema9[i]
        c["ema50"] = ema50[i]
    return candles


# ---------------- STATE (persisted across runs) ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"capital": STARTING_CAPITAL, "position": None, "last_processed_time": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def init_trade_log():
    if not os.path.exists(TRADE_LOG_FILE):
        with open(TRADE_LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "entry_time", "exit_time", "side", "entry_price", "exit_price",
                "quantity", "pnl", "reason", "capital_after"
            ])


def log_trade(row):
    with open(TRADE_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(row)


# ---------------- TRADE LOGIC ----------------
def open_position(state, side, price, ts):
    margin = state["capital"] * CAPITAL_PER_TRADE_PCT
    notional = margin * LEVERAGE
    quantity = notional / price
    liq_price = price * (1 - 1 / LEVERAGE) if side == "long" else price * (1 + 1 / LEVERAGE)

    state["position"] = {
        "side": side,
        "entry_price": price,
        "entry_time": ts,
        "quantity": quantity,
        "margin": margin,
        "liq_price": liq_price,
    }
    print(f"[{ts}] OPEN {side.upper()} @ {price:.2f} | qty={quantity:.6f} "
          f"| margin={margin:.2f} | liq~{liq_price:.2f}")


def close_position(state, price, ts, reason):
    pos = state["position"]
    direction = 1 if pos["side"] == "long" else -1
    gross_pnl = (price - pos["entry_price"]) * pos["quantity"] * direction

    entry_notional = pos["entry_price"] * pos["quantity"]
    exit_notional = price * pos["quantity"]
    fees = (entry_notional + exit_notional) * FEE_RATE

    pnl = gross_pnl - fees
    if reason == "liquidated":
        pnl = -pos["margin"]

    state["capital"] += pnl
    print(f"[{ts}] CLOSE {pos['side'].upper()} @ {price:.2f} | pnl={pnl:.2f} "
          f"| reason={reason} | capital={state['capital']:.2f}")

    log_trade([
        pos["entry_time"], ts, pos["side"], pos["entry_price"], price,
        pos["quantity"], round(pnl, 4), reason, round(state["capital"], 4)
    ])
    state["position"] = None


def check_liquidation(state, current_price, ts):
    pos = state.get("position")
    if not pos:
        return
    if pos["side"] == "long" and current_price <= pos["liq_price"]:
        close_position(state, pos["liq_price"], ts, "liquidated")
    elif pos["side"] == "short" and current_price >= pos["liq_price"]:
        close_position(state, pos["liq_price"], ts, "liquidated")


def check_signals(prev_c, curr_c):
    bullish_cross = prev_c["ema5"] <= prev_c["ema9"] and curr_c["ema5"] > curr_c["ema9"]
    bearish_cross = prev_c["ema5"] >= prev_c["ema9"] and curr_c["ema5"] < curr_c["ema9"]

    above_50 = curr_c["ema5"] > curr_c["ema50"] and curr_c["ema9"] > curr_c["ema50"]
    below_50 = curr_c["ema5"] < curr_c["ema50"] and curr_c["ema9"] < curr_c["ema50"]

    if bullish_cross and above_50:
        return "enter_long"
    if bearish_cross and below_50:
        return "enter_short"
    if bullish_cross or bearish_cross:
        return "exit"
    return None


# ---------------- MAIN (one run) ----------------
def main():
    init_trade_log()
    state = load_state()
    is_first_run = state["last_processed_time"] == 0

    print(f"Loaded state: capital={state['capital']:.2f}, "
          f"position={'flat' if not state['position'] else state['position']['side']}")

    raw = fetch_candles(HISTORY_HOURS)
    if not raw:
        print("No candle data returned this run, skipping.")
        save_state(state)
        return

    current_price = float(raw[-1]["close"])
    check_liquidation(state, current_price, datetime.now(timezone.utc).isoformat())

    closed = only_closed_candles(raw)
    if not closed:
        print("No closed candles yet, skipping.")
        save_state(state)
        return

    closed = add_emas(closed)

    if is_first_run:
        # Warm up only - don't act on the whole 72h lookback history as if
        # it just happened. Start watching for crossovers from here on.
        state["last_processed_time"] = closed[-1]["time"]
        print("First run: EMAs warmed up, no historical signals executed. "
              "Bot will start reacting to the next new candle.")
    else:
        new_candles = [c for c in closed if c["time"] > state["last_processed_time"]]

        start_idx = len(closed) - len(new_candles)
        for i in range(max(start_idx, 1), len(closed)):
            prev_c = closed[i - 1]
            curr_c = closed[i]
            ts = datetime.fromtimestamp(curr_c["time"] / 1000, tz=timezone.utc).isoformat()
            price = float(curr_c["close"])

            signal = check_signals(prev_c, curr_c)

            if state["position"] and signal == "exit":
                close_position(state, price, ts, "signal_exit")
                signal = check_signals(prev_c, curr_c)

            if not state["position"]:
                if signal == "enter_long":
                    open_position(state, "long", price, ts)
                elif signal == "enter_short":
                    open_position(state, "short", price, ts)

        state["last_processed_time"] = closed[-1]["time"]

    save_state(state)
    print(f"Run complete. Capital: {state['capital']:.2f} | "
          f"Position: {'flat' if not state['position'] else state['position']['side']}")


if __name__ == "__main__":
    main()
