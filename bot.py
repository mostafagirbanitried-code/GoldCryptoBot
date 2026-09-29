import os
import time
import requests
from datetime import datetime, timezone

TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
SYMBOLS = ["BTCUSDT", "XAUUSDT"]
INTERVAL = "30m"
CHECK_EVERY = 1800

if not TOKEN or not CHAT_ID:
    raise SystemExit("Set BOT_TOKEN and CHAT_ID environment variables first.")

API = "https://api.telegram.org/bot" + TOKEN
last_signal = {}


def send_message(text):
    response = requests.post(
        API + "/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20
    )
    response.raise_for_status()


def get_candles(symbol):
    response = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": symbol, "interval": INTERVAL, "limit": 60},
        timeout=20
    )
    response.raise_for_status()
    return response.json()


def ema(values, period):
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def analyze(symbol):
    candles = get_candles(symbol)
    closes = [float(candle[4]) for candle in candles[:-1]]
    if len(closes) < 30:
        return None
    fast = ema(closes, 9)
    slow = ema(closes, 21)
    if fast[-2] <= slow[-2] and fast[-1] > slow[-1]:
        return "BUY", closes[-1]
    if fast[-2] >= slow[-2] and fast[-1] < slow[-1]:
        return "SELL", closes[-1]
    return None


def main():
    send_message("ربات تحلیل بازار روشن شد. بررسی هر ۳۰ دقیقه انجام می‌شود.")
    while True:
        for symbol in SYMBOLS:
            try:
                signal = analyze(symbol)
                if signal:
                    side, price = signal
                    key = (symbol, side)
                    if last_signal.get(symbol) != key:
                        emoji = "🟢" if side == "BUY" else "🔴"
                        message = (
                            f"{emoji} سیگنال {side}\n"
                            f"نماد: {symbol}\n"
                            f"تایم‌فریم: {INTERVAL}\n"
                            f"قیمت: {price}\n"
                            "روش: تقاطع EMA 9 و EMA 21\n"
                            "توجه: سیگنال تضمینی نیست؛ حد ضرر و مدیریت ریسک را خودتان تعیین کنید."
                        )
                        send_message(message)
                        last_signal[symbol] = key
            except Exception as error:
                print(f"Error for {symbol}: {error}")
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
