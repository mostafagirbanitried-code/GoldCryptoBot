import os
import time
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

BASE_URL = "https://fapi.binance.com"
LEVERAGE = 50
TIMEFRAME = "1h"
MAX_WORKERS = 8
MIN_QUOTE_VOLUME = 5_000_000
MAX_SYMBOLS = 500

if not TOKEN or not CHAT_ID:
    raise SystemExit("BOT_TOKEN and CHAT_ID must be set in GitHub Secrets")

session = requests.Session()
session.headers.update({"User-Agent": "MostafaTradePro/1.0"})


def api_get(path, params=None):
    response = session.get(BASE_URL + path, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def send_message(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20
    )
    response.raise_for_status()


def ema(values, period):
    if len(values) < period:
        return None
    alpha = 2 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = value * alpha + result * (1 - alpha)
    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period
    if avg_loss == 0:
        return 100
    return 100 - 100 / (1 + avg_gain / avg_loss)


def atr(highs, lows, closes, period=14):
    if len(closes) <= period:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        ))
    return sum(trs[-period:]) / period


def get_symbols():
    info = api_get("/fapi/v1/exchangeInfo")
    allowed = {
        item["symbol"] for item in info["symbols"]
        if item.get("status") == "TRADING"
        and item.get("contractType") == "PERPETUAL"
        and item.get("quoteAsset") == "USDT"
    }
    tickers = api_get("/fapi/v1/ticker/24hr")
    ranked = []
    for ticker in tickers:
        symbol = ticker.get("symbol")
        if symbol in allowed:
            try:
                volume = float(ticker.get("quoteVolume", 0))
                if volume >= MIN_QUOTE_VOLUME:
                    ranked.append((symbol, volume))
            except (TypeError, ValueError):
                pass
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [symbol for symbol, _ in ranked[:MAX_SYMBOLS]]


def analyze(symbol):
    try:
        candles = api_get(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": TIMEFRAME, "limit": 120}
        )
        if len(candles) < 60:
            return None

        # آخرین کندل ممکن است هنوز بسته نشده باشد؛ آن را کنار می‌گذاریم.
        candles = candles[:-1]
        closes = [float(c[4]) for c in candles]
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]
        volumes = [float(c[5]) for c in candles]

        fast = ema(closes, 20)
        slow = ema(closes, 50)
        previous_fast = ema(closes[:-1], 20)
        current_rsi = rsi(closes)
        current_atr = atr(highs, lows, closes)

        if not all(x is not None for x in
                   [fast, slow, previous_fast, current_rsi, current_atr]):
            return None

        entry = closes[-1]
        last_volume = volumes[-1]
        avg_volume = sum(volumes[-21:-1]) / 20
        if current_atr <= 0 or avg_volume <= 0:
            return None

        # Trend + RSI + volume confirmation.
        long_ok = (
            fast > slow
            and fast > previous_fast
            and entry > fast
            and 52 <= current_rsi <= 68
            and last_volume >= avg_volume * 0.8
        )
        short_ok = (
            fast < slow
            and fast < previous_fast
            and entry < fast
            and 32 <= current_rsi <= 48
            and last_volume >= avg_volume * 0.8
        )

        if not long_ok and not short_ok:
            return None

        side = "LONG" if long_ok else "SHORT"
        risk = current_atr * 1.5
        if risk / entry > 0.04:
            return None

        if side == "LONG":
            stop = entry - risk
            targets = [entry + risk, entry + 2 * risk, entry + 3 * risk]
        else:
            stop = entry + risk
            targets = [entry - risk, entry - 2 * risk, entry - 3 * risk]

        score = abs(fast - slow) / entry + abs(current_rsi - 50) / 1000
        return {
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "stop": stop,
            "targets": targets,
            "rsi": current_rsi,
            "score": score,
            "time": candles[-1][6]
        }
    except Exception as exc:
        print(f"Could not analyze {symbol}: {exc}")
        return None


def format_price(value):
    if value >= 1000:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.4f}"
    if value >= 0.01:
        return f"{value:.6f}"
    return f"{value:.8f}"


def main():
    print("Starting hourly crypto scan...")
    symbols = get_symbols()
    print(f"Scanning {len(symbols)} USDT perpetual markets")

    candidates = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(analyze, symbol) for symbol in symbols]
        for future in as_completed(futures):
            result = future.result()
            if result:
                candidates.append(result)

    if not candidates:
        send_message(
            "🔎 Mostafa Trade Pro\n"
            "تحلیل یک‌ساعته تمام ارزهای واجد شرایط انجام شد.\n"
            "در حال حاضر سیگنال معتبر مطابق فیلترهای ربات پیدا نشد.\n"
            "اهرم نمایشی: 50x"
        )
        print("No qualifying signal found.")
        return

    # در هر اجرا فقط یک فرصت با امتیاز بالاتر ارسال می‌شود.
    best = max(candidates, key=lambda item: item["score"])
    direction = "🟢 LONG / لانگ" if best["side"] == "LONG" else "🔴 SHORT / شورت"
    targets = best["targets"]

    message = (
        "📊 Mostafa Trade Pro\n"
        "🪙 Coin: #" + best["symbol"] + "\n"
        "📌 Type: " + direction + "\n"
        "⚙️ Leverage: 50x\n"
        "⏱ Timeframe: 1H\n"
        "━━━━━━━━━━━━━━\n"
        "🎯 Entry: " + format_price(best["entry"]) + "\n"
        "✅ Target 1: " + format_price(targets[0]) + "\n"
        "✅ Target 2: " + format_price(targets[1]) + "\n"
        "✅ Target 3: " + format_price(targets[2]) + "\n"
        "🛑 Stoploss: " + format_price(best["stop"]) + "\n"
        "━━━━━━━━━━━━━━\n"
        "RSI: " + f"{best['rsi']:.1f}" + "\n"
        "📅 Candle: " + datetime.fromtimestamp(
            best["time"] / 1000, timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC") + "\n"
        "⚠️ سیگنال آزمایشی است؛ وین‌ریت تضمین‌شده نیست."
    )
    send_message(message)
    print("Signal sent:", best["symbol"], best["side"])


if __name__ == "__main__":
    main()
