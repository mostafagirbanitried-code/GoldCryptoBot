import os
import json
import base64
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

API = "https://api.bybit.com"
TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
GH_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
STATE_PATH = "data/state.json"

s = requests.Session()
s.headers["User-Agent"] = "GoldCryptoBot/1.0"


def api(path, params=None):
    r = s.get(API + path, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(data.get("retMsg", "Bybit API error"))
    return data["result"]


def send(text):
    r = s.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=25
    )
    r.raise_for_status()
    if not r.json().get("ok"):
        raise RuntimeError("Telegram send failed")


def load_state():
    url = f"https://api.github.com/repos/{REPO}/contents/{STATE_PATH}"
    r = s.get(url, headers={"Authorization": f"Bearer {GH_TOKEN}"}, timeout=20)

    if r.status_code == 404:
        return {"active": [], "history": [], "last_scan": ""}, None

    r.raise_for_status()
    obj = r.json()
    raw = base64.b64decode(obj["content"]).decode("utf-8")
    return json.loads(raw), obj["sha"]


def save_state(state, sha):
    url = f"https://api.github.com/repos/{REPO}/contents/{STATE_PATH}"
    body = {
        "message": "Update bot trade tracking",
        "content": base64.b64encode(
            json.dumps(state, indent=2, ensure_ascii=False).encode()
        ).decode()
    }
    if sha:
        body["sha"] = sha

    r = s.put(
        url,
        headers={"Authorization": f"Bearer {GH_TOKEN}"},
        json=body,
        timeout=25
    )
    r.raise_for_status()


def get_symbols():
    symbols = []
    cursor = None

    while True:
        params = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor

        result = api("/v5/market/instruments-info", params)
        for item in result["list"]:
            if (
                item.get("status") == "Trading"
                and item.get("quoteCoin") == "USDT"
                and item.get("contractType") == "LinearPerpetual"
            ):
                symbols.append(item["symbol"])

        cursor = result.get("nextPageCursor")
        if not cursor:
            break

    return symbols


def get_tickers():
    result = api("/v5/market/tickers", {"category": "linear"})
    return {
        x["symbol"]: x for x in result["list"]
        if x["symbol"].endswith("USDT")
    }


def ema(values, period):
    k = 2 / (period + 1)
    value = values[0]
    for x in values[1:]:
        value = x * k + value * (1 - k)
    return value


def rsi(values, period=14):
    changes = [b - a for a, b in zip(values[:-1], values[1:])]
    changes = changes[-period:]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100
    return 100 - 100 / (1 + avg_gain / avg_loss)


def calculate_atr(candles, period=14):
    tr = []
    for i in range(1, len(candles)):
        high = float(candles[i][2])
        low = float(candles[i][3])
        prev = float(candles[i - 1][4])
        tr.append(max(high - low, abs(high - prev), abs(low - prev)))
    return sum(tr[-period:]) / period


def analyze(symbol, ticker):
    try:
        result = api("/v5/market/kline", {
            "category": "linear",
            "symbol": symbol,
            "interval": "60",
            "limit": 100
        })

        # Bybit کندل‌ها را از جدید به قدیم برمی‌گرداند.
        candles = list(reversed(result["list"]))
        if len(candles) < 60:
            return None

        # حذف کندل جاری که هنوز بسته نشده است.
        candles = candles[:-1]
        closes = [float(c[4]) for c in candles]
        volumes = [float(c[5]) for c in candles]

        price = closes[-1]
        fast = ema(closes[-50:], 20)
        slow = ema(closes[-80:], 50)
        momentum = rsi(closes)
        volatility = calculate_atr(candles)

        avg_volume = sum(volumes[-21:-1]) / 20
        if avg_volume <= 0 or volatility <= 0:
            return None
        if volumes[-1] < avg_volume:
            return None

        direction = None
        score = 0

        if fast > slow and price > fast and 52 <= momentum <= 68:
            direction = "LONG"
            score = 3 + int(momentum >= 55)
        elif fast < slow and price < fast and 32 <= momentum <= 48:
            direction = "SHORT"
            score = 3 + int(momentum <= 45)

        if not direction:
            return None

        distance = volatility * 1.5
        if direction == "LONG":
            stop = price - distance
            targets = [
                price + distance,
                price + 2 * distance,
                price + 3 * distance
            ]
        else:
            stop = price + distance
            targets = [
                price - distance,
                price - 2 * distance,
                price - 3 * distance
            ]

        return {
            "symbol": symbol,
            "direction": direction,
            "entry": price,
            "stop": stop,
            "targets": targets,
            "score": score,
            "created": datetime.now(timezone.utc).isoformat(),
            "hits": [],
            "closed": False
        }
    except Exception as e:
        print(f"Analysis error {symbol}: {e}")
        return None


def format_signal(x):
    return (
        f"📊 GoldCryptoBot | سیگنال جدید\n\n"
        f"Coin: #{x['symbol']}\n"
        f"Direction: {x['direction']}\n"
        f"Leverage: 50x\n"
        f"Entry: {x['entry']:.8g}\n"
        f"TP1: {x['targets'][0]:.8g}\n"
        f"TP2: {x['targets'][1]:.8g}\n"
        f"TP3: {x['targets'][2]:.8g}\n"
        f"Stoploss: {x['stop']:.8g}\n\n"
        f"⚠️ سیگنال آزمایشی است؛ معامله واقعی باز نمی‌شود."
    )


def monitor(state):
    changed = False

    for trade in list(state["active"]):
        try:
            result = api("/v5/market/kline", {
                "category": "linear",
                "symbol": trade["symbol"],
                "interval": "5",
                "limit": 3
            })
            candles = list(reversed(result["list"]))
            if len(candles) < 2:
                continue

            # آخرین کندل بسته‌شده
            candle = candles[-2]
            high = float(candle[2])
            low = float(candle[3])
            is_long = trade["direction"] == "LONG"

            stop_hit = (
                low <= trade["stop"] if is_long
                else high >= trade["stop"]
            )

            if stop_hit:
                send(
                    f"🔴 Stoploss لمس شد: #{trade['symbol']}\n"
                    f"قیمت استاپ: {trade['stop']:.8g}\n"
                    f"توجه: نتیجه بر اساس کندل ۵ دقیقه‌ای است."
                )
                trade["closed"] = True
                trade["result"] = "SL"
                trade["closed_at"] = datetime.now(timezone.utc).isoformat()
                state["history"].append(trade.copy())
                state["active"].remove(trade)
                changed = True
                continue

            for i, target in enumerate(trade["targets"]):
                if i in trade["hits"]:
                    continue

                hit = high >= target if is_long else low <= target
                if hit:
                    send(
                        f"🎯 TP{i+1} لمس شد: #{trade['symbol']}\n"
                        f"قیمت هدف: {target:.8g}"
                    )
                    trade["hits"].append(i)
                    changed = True

            if 2 in trade["hits"]:
                trade["closed"] = True
                trade["result"] = "TP3"
                trade["closed_at"] = datetime.now(timezone.utc).isoformat()
                state["history"].append(trade.copy())
                state["active"].remove(trade)
                changed = True

        except Exception as e:
            print(f"Monitor error {trade['symbol']}: {e}")

    return changed


def main():
    state, sha = load_state()
    changed = monitor(state)

    hour = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")
    if state.get("last_scan") != hour:
        state["last_scan"] = hour
        changed = True

        try:
            symbols = get_symbols()
            tickers = get_tickers()
            active = {x["symbol"] for x in state["active"]}

            # حذف ارزهای بدون حجم قابل‌توجه
            symbols = [
                symbol for symbol in symbols
                if symbol in tickers
                and float(tickers[symbol].get("turnover24h", 0)) >= 5_000_000
            ]

            candidates = []
            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {
                    pool.submit(analyze, symbol, tickers[symbol]): symbol
                    for symbol in symbols
                    if symbol not in active
                }
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        candidates.append(result)

            candidates.sort(key=lambda x: x["score"], reverse=True)

            if candidates:
                selected = candidates[0]
                send(format_signal(selected))
                state["active"].append(selected)
            else:
                send(
                    "🔎 بررسی ساعتی انجام شد.\n"
                    "در حال حاضر سیگنال جدیدی پیدا نشد."
                )

        except Exception as e:
            print(f"Scan error: {e}")
            send(f"⚠️ خطا در تحلیل بازار:\n{e}")

    if changed:
        save_state(state, sha)


if __name__ == "__main__":
    main()
