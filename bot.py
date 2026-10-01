import os
import json
import time
import base64
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, ROUND_HALF_UP

BINANCE = "https://fapi.binance.com"
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
GH_TOKEN = os.environ["GH_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
STATE_FILE = "data/state.json"

HEADERS = {"Authorization": f"Bearer {GH_TOKEN}",
           "Accept": "application/vnd.github+json"}
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "GoldCryptoBot"})


def telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = SESSION.post(url, json={
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": True
    }, timeout=20)
    r.raise_for_status()


def get_state():
    url = f"https://api.github.com/repos/{REPO}/contents/{STATE_FILE}"
    r = SESSION.get(url, headers=HEADERS, timeout=20)
    if r.status_code == 404:
        return {"active": [], "history": [], "last_scan_hour": ""}
    r.raise_for_status()
    content = base64.b64decode(r.json()["content"]).decode()
    state = json.loads(content)
    state["_sha"] = r.json()["sha"]
    return state


def save_state(state):
    sha = state.pop("_sha", None)
    url = f"https://api.github.com/repos/{REPO}/contents/{STATE_FILE}"
    body = {
        "message": "Update GoldCryptoBot state",
        "content": base64.b64encode(
            json.dumps(state, ensure_ascii=False, indent=2).encode()
        ).decode()
    }
    if sha:
        body["sha"] = sha
    r = SESSION.put(url, headers=HEADERS, json=body, timeout=20)
    r.raise_for_status()
    state["_sha"] = r.json()["content"]["sha"]


def get_json(path, params=None):
    r = SESSION.get(BINANCE + path, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def ema(values, period):
    k = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = value * k + result * (1 - k)
    return result


def rsi(values, period=14):
    gains, losses = [], []
    for a, b in zip(values[-period-1:-1], values[-period:]):
        change = b - a
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    return 100 - 100 / (1 + avg_gain / avg_loss)


def atr(candles, period=14):
    ranges = []
    for i in range(1, len(candles)):
        high = float(candles[i][2])
        low = float(candles[i][3])
        prev_close = float(candles[i-1][4])
        ranges.append(max(high-low, abs(high-prev_close),
                          abs(low-prev_close)))
    return sum(ranges[-period:]) / period


def round_tick(price, tick):
    p, t = Decimal(str(price)), Decimal(str(tick))
    return float((p / t).quantize(Decimal("1"),
                                  rounding=ROUND_HALF_UP) * t)


def get_symbols():
    info = get_json("/fapi/v1/exchangeInfo")
    return {
        s["symbol"]: s for s in info["symbols"]
        if s["contractType"] == "PERPETUAL"
        and s["quoteAsset"] == "USDT"
        and s["status"] == "TRADING"
    }


def analyze_one(symbol, info):
    try:
        candles = get_json("/fapi/v1/klines", {
            "symbol": symbol, "interval": "1h", "limit": 100
        })
        if len(candles) < 60:
            return None

        # آخرین کندل بسته‌شده
        candles = candles[:-1]
        closes = [float(c[4]) for c in candles]
        volumes = [float(c[5]) for c in candles]
        price = closes[-1]
        e20 = ema(closes[-50:], 20)
        e50 = ema(closes[-80:], 50)
        current_rsi = rsi(closes)
        current_atr = atr(candles)

        avg_volume = sum(volumes[-21:-1]) / 20
        if current_atr <= 0 or avg_volume <= 0:
            return None
        if volumes[-1] < avg_volume:
            return None

        direction = None
        score = 0

        if e20 > e50 and price > e20 and 52 <= current_rsi <= 68:
            direction = "LONG"
            score = 3
            score += int(current_rsi >= 55)
        elif e20 < e50 and price < e20 and 32 <= current_rsi <= 48:
            direction = "SHORT"
            score = 3
            score += int(current_rsi <= 45)

        if not direction:
            return None

        distance = current_atr * 1.5
        if direction == "LONG":
            stop = price - distance
            targets = [price + distance, price + 2*distance,
                       price + 3*distance]
        else:
            stop = price + distance
            targets = [price - distance, price - 2*distance,
                       price - 3*distance]

        tick = next(
            (f["tickSize"] for f in info["filters"]
             if f["filterType"] == "PRICE_FILTER"), "0.00000001"
        )
        price = round_tick(price, tick)
        stop = round_tick(stop, tick)
        targets = [round_tick(t, tick) for t in targets]

        return {
            "symbol": symbol, "direction": direction,
            "entry": price, "stop": stop, "targets": targets,
            "score": score, "created": datetime.now(timezone.utc).isoformat(),
            "hits": [], "closed": False
        }
    except Exception:
        return None


def scan(symbols):
    results = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [
            pool.submit(analyze_one, symbol, info)
            for symbol, info in symbols.items()
        ]
        for future in as_completed(futures):
            result = future.result()
            if result:
                results.append(result)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def signal_message(s):
    return (
        f"🟢 GoldCryptoBot | سیگنال جدید\n\n"
        f"Coin: #{s['symbol']}\n"
        f"Direction: {s['direction']}\n"
        f"Leverage: 50x\n"
        f"Entry: {s['entry']}\n"
        f"TP1: {s['targets'][0]}\n"
        f"TP2: {s['targets'][1]}\n"
        f"TP3: {s['targets'][2]}\n"
        f"Stoploss: {s['stop']}\n\n"
        f"توجه: سیگنال آزمایشی است؛ معامله خودکار انجام نمی‌شود."
    )


def monitor(state):
    changed = False
    for s in list(state["active"]):
        try:
            candles = get_json("/fapi/v1/klines", {
                "symbol": s["symbol"], "interval": "5m", "limit": 2
            })
            # از کندل بسته‌شده اخیر استفاده می‌کنیم
            candle = candles[-2]
            high, low = float(candle[2]), float(candle[3])
            long = s["direction"] == "LONG"

            stop_hit = low <= s["stop"] if long else high >= s["stop"]
            targets_hit = [
                i for i, target in enumerate(s["targets"])
                if (high >= target if long else low <= target)
                and i not in s["hits"]
            ]

            # اگر استاپ و تارگت در یک کندل باشند، نتیجه محافظه‌کارانه SL است.
            if stop_hit:
                telegram(f"🔴 Stoploss خورد: #{s['symbol']}\n"
                         f"جهت: {s['direction']}\n"
                         f"قیمت استاپ: {s['stop']}")
                s["closed"] = True
                s["result"] = "SL"
                s["closed_at"] = datetime.now(timezone.utc).isoformat()
                state["history"].append(s.copy())
                state["active"].remove(s)
                changed = True
                continue

            for i in targets_hit:
                s["hits"].append(i)
                telegram(f"🎯 TP{i+1} خورد: #{s['symbol']}\n"
                         f"قیمت هدف: {s['targets'][i]}")
                changed = True

            if 2 in s["hits"]:
                s["closed"] = True
                s["result"] = "TP3"
                s["closed_at"] = datetime.now(timezone.utc).isoformat()
                state["history"].append(s.copy())
                state["active"].remove(s)
                changed = True

        except Exception as e:
            print(f"Monitor error {s['symbol']}: {e}")
    return changed


def main():
    state = get_state()
    changed = monitor(state)

    now = datetime.now(timezone.utc)
    hour_key = now.strftime("%Y-%m-%d-%H")

    if state.get("last_scan_hour") != hour_key:
        state["last_scan_hour"] = hour_key
        changed = True
        try:
            symbols = get_symbols()
            candidates = scan(symbols)

            active_symbols = {s["symbol"] for s in state["active"]}
            candidate = next(
                (s for s in candidates
                 if s["symbol"] not in active_symbols), None
            )

            if candidate:
                telegram(signal_message(candidate))
                state["active"].append(candidate)
            else:
                telegram("🔎 بررسی ساعتی انجام شد.\n"
                         "در حال حاضر سیگنال جدیدی پیدا نشد.")
        except Exception as e:
            telegram(f"⚠️ خطا در تحلیل بازار:\n{e}")
            print(e)

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
