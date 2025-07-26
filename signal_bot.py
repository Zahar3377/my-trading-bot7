# signal_bot.py

import os
import json
import time
from datetime import datetime
from binance.client import Client
import pandas as pd
import ta
import requests

# ----------------------
# 1. CONFIGURATION
# ----------------------
BOT_TOKEN      = "7909798572:AAFFjakfTYpLM2bWupQXpQznNEZCekZZpd0"
CHAT_IDS       = ["1214927670", "630320480"]
PAIRS          = [
    "BTCUSDT","ETHUSDT","BNBUSDT","ADAUSDT","XRPUSDT",
    "NEARUSDT","SOLUSDT","EPICUSDT","XMRUSDT"
]
MACD_FAST      = 12
MACD_SLOW      = 26
MACD_SIGNAL    = 9
RSI_PERIOD     = 6
RSI_LOOKBACK   = 7
TF_MAIN        = "5m"
TF_FILTER      = "15m"
SLEEP_INTERVAL = 60  # seconds

LEVERAGE       = 20
SIZE_STRONG    = 4.0   # $ for strong trade
SIZE_WEAK      = 2.0   # $ for weak trade
TAKER_FEE_RATE = 0.0004  # 0.04%

STATS_FILE = "stats.json"

client = Client()

# ----------------------
# 2. PERSISTENT STATISTICS
# ----------------------
# stats = { "1": {sym: [PnL, ...]}, "2": {sym: [PnL, ...]} }
try:
    with open(STATS_FILE, "r", encoding="utf-8") as f:
        stats = json.load(f)
        if not isinstance(stats, dict) or "1" not in stats or "2" not in stats:
            raise ValueError
except (FileNotFoundError, json.JSONDecodeError, ValueError):
    stats = {
        "1": {sym: [] for sym in PAIRS},
        "2": {sym: [] for sym in PAIRS}
    }

def save_stats():
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

# ----------------------
# 3. HELPERS
# ----------------------
def fetch_klines(symbol, interval, limit=100):
    try:
        raw = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    except Exception as e:
        print(f">>> fetch_klines error for {symbol}: {e}")
        return pd.DataFrame()
    df = pd.DataFrame(raw, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","q_av","n_trades","tb_base","tb_quote","ignore"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["close"]     = df["close"].astype(float)
    return df

def compute_indicators(df):
    macd = ta.trend.MACD(
        close=df["close"],
        window_fast=MACD_FAST,
        window_slow=MACD_SLOW,
        window_sign=MACD_SIGNAL
    )
    df["macd_hist"] = macd.macd_diff()
    rsi = ta.momentum.RSIIndicator(df["close"], window=RSI_PERIOD)
    df["rsi"]       = rsi.rsi()
    return df

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for cid in CHAT_IDS:
        try:
            res = requests.post(url, json={"chat_id": cid, "text": msg})
            print(f">>> Telegram to {cid}: {res.status_code}")
        except Exception as e:
            print(f">>> Telegram error for {cid}: {e}")

# ----------------------
# 4. ENTRY & EXIT LOGIC
# ----------------------
sent_signals   = set()       # set of (symbol, side, strength)
open_positions = {"1": {}, "2": {}}

def check_entry(symbol):
    # load data
    df5  = fetch_klines(symbol, TF_MAIN,   limit=100)
    df15 = fetch_klines(symbol, TF_FILTER, limit=100)
    if df5.empty or df15.empty:
        return None

    # calculate indicators
    df5  = compute_indicators(df5)
    df15 = compute_indicators(df15)

    prev15 = df15["macd_hist"].iloc[-2]
    cur15  = df15["macd_hist"].iloc[-1]
    prev5  = df5["macd_hist"].iloc[-2]
    cur5   = df5["macd_hist"].iloc[-1]
    rsi7   = df15["rsi"].iloc[-RSI_LOOKBACK:].tolist()
    tstamp = df15["open_time"].iloc[-1]
    price  = df5["close"].iloc[-1]  # actual price

    print(f">>> {symbol}: M15({prev15:.4f}->{cur15:.4f}), "
          f"M5({prev5:.4f}->{cur5:.4f}), RSI6={ [round(x,2) for x in rsi7] }")

    # LONG entry: M15 neg→pos + M5 pos
    if prev15 < 0 and cur15 > 0 and cur5 > 0:
        mn = min(rsi7)
        strength = "Сильний" if mn < 25 else "Слабкий"
        return ("LONG", strength, price, cur15, cur5, rsi7, tstamp)

    # SHORT entry: M15 pos→neg + M5 neg
    if prev15 > 0 and cur15 < 0 and cur5 < 0:
        mx = max(rsi7)
        strength = "Сильний" if mx > 75 else "Слабкий"
        return ("SHORT", strength, price, cur15, cur5, rsi7, tstamp)

    return None

def check_exit(symbol, strategy_id):
    pos = open_positions[str(strategy_id)].get(symbol)
    if not pos:
        return False

    entry_time = pos["entry_time"]
    df15 = fetch_klines(symbol, TF_FILTER, limit=100)
    if df15.empty:
        return False

    df15 = compute_indicators(df15)
    prev = df15["macd_hist"].iloc[-2]
    cur  = df15["macd_hist"].iloc[-1]
    tstamp = df15["open_time"].iloc[-1]
    side   = pos["side"]

    # wait for a new M15 bar
    if tstamp <= entry_time:
        return False

    # Strategy 1: exit on sign flip
    if strategy_id == 1:
        if side == "LONG"  and prev > 0 and cur < 0:
            return True
        if side == "SHORT" and prev < 0 and cur > 0:
            return True

    # Strategy 2: exit on reduction of |hist|
    else:
        if abs(cur) < abs(prev):
            return True

    return False

# ----------------------
# 5. MAIN LOOP
# ----------------------
last_update_id = 0

if __name__ == "__main__":
    print(">>> Bot started at", datetime.now())
    send_telegram("🤖 Бот старт: статистика сохраняется в stats.json")

    while True:
        # --- Handle /statistics command ---
        resp = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"offset": last_update_id}
        ).json()
        for upd in resp.get("result", []):
            last_update_id = upd["update_id"] + 1
            msg = upd.get("message", {})
            text = msg.get("text", "").strip().lower()
            chat_id = msg.get("chat", {}).get("id")
            if text in ("/statistics", "statistics") and chat_id:
                lines = ["📊 <b>Статистика PnL</b>"]
                for sid in ("1", "2"):
                    # flatten all pairs
                    all_pnls = []
                    for sym in PAIRS:
                        all_pnls += stats[sid].get(sym, [])
                    total = sum(all_pnls)
                    avg   = total / len(all_pnls) if all_pnls else 0
                    lines.append(f"\n— Стратегія {sid}: total = {total:.2f}$, avg = {avg:.2f}$")
                    # detail per pair
                    for sym in PAIRS:
                        lst = stats[sid].get(sym, [])
                        cnt = len(lst)
                        tsum = sum(lst) if cnt else 0
                        a = tsum / cnt if cnt else 0
                        lines.append(f"{sym}: trades={cnt}, total={tsum:.2f}$, avg={a:.2f}$")
                send_telegram("\n".join(lines))

        # --- ENTRY & EXIT per pair ---
        for sym in PAIRS:
            # ENTRY
            entry = check_entry(sym)
            if entry:
                side, strength, price, h15, h5, rsi7, tstamp = entry
                key = (sym, side, strength)
                if key not in sent_signals:
                    size = SIZE_STRONG if strength == "Сильний" else SIZE_WEAK
                    # open positions for both strategies
                    for sid in (1, 2):
                        open_positions[str(sid)][sym] = {
                            "side": side,
                            "entry_price": price,
                            "entry_time": tstamp,
                            "size": size
                        }
                    # formatted ENTRY message
                    msg = (
f"{'📈' if side=='LONG' else '📉'} {strength} {side}\n"
f"Пара: {sym}\n"
f"Час: {tstamp}\n"
f"M15 MACD_hist: {h15:.4f}\n"
f"M5  MACD_hist: {h5:.4f}\n"
f"RSI6 M15 ({RSI_LOOKBACK} барів): {[round(x,2) for x in rsi7]}\n"
f"{'Мін' if side=='LONG' else 'Макс'} RSI6: "
f"{(min(rsi7) if side=='LONG' else max(rsi7)):.2f}\n"
f"Відкриває позицію на {size}$ з плечем x{LEVERAGE}"
                    )
                    print(">>> ENTRY:\n", msg)
                    send_telegram(msg)
                    sent_signals.add(key)

            # EXIT
            for sid in (1, 2):
                if check_exit(sym, sid):
                    pos = open_positions[str(sid)].pop(sym)
                    ep  = pos["entry_price"]
                    xp  = fetch_klines(sym, TF_MAIN, limit=2)["close"].iloc[-1]
                    size = pos["size"]
                    side = pos["side"]
                    pnl  = (xp - ep) * size * LEVERAGE / ep
                    if side == "SHORT":
                        pnl = -pnl
                    fee = size * LEVERAGE * (ep + xp) * TAKER_FEE_RATE / 2 / xp
                    net = pnl - fee
                    stats[str(sid)][sym].append(net)
                    save_stats()
                    msg = (
f"EXIT strat{sid} {side} {sym} @ {xp:.4f}\n"
f"PnL = {net:.2f}$ (fee≈{fee:.4f}$)"
                    )
                    print(">>> EXIT:", msg)
                    send_telegram(msg)

        time.sleep(SLEEP_INTERVAL)
