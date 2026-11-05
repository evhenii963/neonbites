#!/usr/bin/env python3
"""
Pocket Option OTC Currency Signal Scanner
READ-ONLY: fetches OTC candles from OTCharts and prints technical-analysis signals.
It does NOT log in to Pocket Option and cannot place trades.
"""
import os
import re
import sys
import time
import getpass
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("Missing package. Run: py -m pip install -r requirements.txt")
    raise SystemExit(1)

API_ROOT = "https://otcharts.com"
VENUE = "otc"
TIMEFRAME_SECONDS = 300  # 5-minute candles
CANDLE_COUNT = 120
SCAN_INTERVAL_MINUTES = 20  # conservative default to limit API usage
MIN_SCORE = 3


def api_get(path, api_key, params=None):
    response = requests.get(
        API_ROOT + path,
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": "PocketOTCSignalScanner/1.0"},
        params=params,
        timeout=25,
    )
    if not response.ok:
        try:
            detail = response.json().get("error", response.text[:250])
        except Exception:
            detail = response.text[:250]
        raise RuntimeError(f"API {response.status_code}: {detail}")
    return response.json()


def ema(values, period):
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd_histogram(values):
    fast = ema(values, 12)
    slow = ema(values, 26)
    macd_line = [a - b for a, b in zip(fast, slow)]
    signal_line = ema(macd_line, 9)
    return macd_line[-1] - signal_line[-1], macd_line[-2] - signal_line[-2]


def analyse(candles):
    # Reject incomplete or discontinuous samples rather than manufacturing a signal.
    if len(candles) < 60:
        return None, "not enough candles"
    candles = sorted(candles, key=lambda x: x["time"])
    closes = [float(c["close"]) for c in candles]
    if any(not (x > 0) for x in closes):
        return None, "invalid prices"

    e9, e21 = ema(closes, 9), ema(closes, 21)
    current_rsi = rsi(closes)
    hist_now, hist_prev = macd_histogram(closes)

    buy_score = 0
    sell_score = 0
    reasons_buy, reasons_sell = [], []

    if e9[-1] > e21[-1]:
        buy_score += 1
        reasons_buy.append("EMA 9 вище EMA 21")
    elif e9[-1] < e21[-1]:
        sell_score += 1
        reasons_sell.append("EMA 9 нижче EMA 21")

    if hist_now > 0 and hist_now > hist_prev:
        buy_score += 1
        reasons_buy.append("MACD гістограма зростає вище нуля")
    elif hist_now < 0 and hist_now < hist_prev:
        sell_score += 1
        reasons_sell.append("MACD гістограма падає нижче нуля")

    if current_rsi is not None:
        if 50 < current_rsi < 70:
            buy_score += 1
            reasons_buy.append(f"RSI {current_rsi:.1f} підтримує висхідний імпульс")
        elif 30 < current_rsi < 50:
            sell_score += 1
            reasons_sell.append(f"RSI {current_rsi:.1f} підтримує низхідний імпульс")

    # Candle momentum confirmation: compare last two completed candle closes.
    if closes[-1] > closes[-2]:
        buy_score += 1
        reasons_buy.append("остання свічка закрилася вище попередньої")
    elif closes[-1] < closes[-2]:
        sell_score += 1
        reasons_sell.append("остання свічка закрилася нижче попередньої")

    if buy_score >= MIN_SCORE and buy_score > sell_score:
        return {
            "direction": "BUY",
            "score": buy_score,
            "reasons": reasons_buy,
            "price": closes[-1],
            "rsi": current_rsi,
            "candle_time": candles[-1]["time"],
        }, None
    if sell_score >= MIN_SCORE and sell_score > buy_score:
        return {
            "direction": "SELL",
            "score": sell_score,
            "reasons": reasons_sell,
            "price": closes[-1],
            "rsi": current_rsi,
            "candle_time": candles[-1]["time"],
        }, None
    return {
        "direction": "WAIT",
        "score": max(buy_score, sell_score),
        "reasons": ["немає достатнього підтвердження"],
        "price": closes[-1],
        "rsi": current_rsi,
        "candle_time": candles[-1]["time"],
    }, None


def currency_otc_symbols(api_key):
    data = api_get("/v1/symbols", api_key, {"venue": VENUE})
    symbols = data.get("symbols", [])
    # Currency pairs only: six letters followed by _otc; excludes stocks/crypto/indices.
    pairs = [
        item for item in symbols
        if re.fullmatch(r"[A-Z]{6}_otc", item.get("symbol", ""))
        and item.get("isOtc", True) is True
        and item.get("open", True) is not False
    ]
    return pairs, data.get("count", len(symbols))


def scan_once(api_key):
    pairs, total = currency_otc_symbols(api_key)
    print(f"\nКаталог OTC: {total} інструментів; валютних OTC-пар, доступних зараз: {len(pairs)}")
    if not pairs:
        print("API не повернув відкритих валютних OTC-пар. Перевір доступ тарифу та статус інструментів.")
        return

    results = []
    for index, item in enumerate(pairs, 1):
        symbol = item["symbol"]
        try:
            payload = api_get("/v1/candles", api_key, {
                "venue": VENUE,
                "symbol": symbol,
                "tf": TIMEFRAME_SECONDS,
                "limit": CANDLE_COUNT,
            })
            candles = payload.get("candles", [])
            gaps = payload.get("gaps", {})
            if gaps.get("runs", 0) or gaps.get("missingBars", 0):
                results.append((symbol, "SKIP", "є пропуски в котируваннях", None))
                continue
            signal, error = analyse(candles)
            if error:
                results.append((symbol, "SKIP", error, None))
            else:
                results.append((symbol, signal["direction"], f'{signal["score"]}/4 • {signal["reasons"][0]}', signal))
        except Exception as exc:
            results.append((symbol, "ERROR", str(exc), None))
        print(f"Перевірено {index}/{len(pairs)}", end="\r", flush=True)

    print("\n" + "=" * 92)
    print(f"{'ПАРА':<16} {'СИГНАЛ':<9} {'ОЦІНКА':<10} ДОДАТКОВА ІНФОРМАЦІЯ")
    print("-" * 92)
    for symbol, direction, note, signal in results:
        if signal:
            rsi_text = f'RSI {signal["rsi"]:.1f}' if signal["rsi"] is not None else "RSI n/a"
            print(f"{symbol:<16} {direction:<9} {signal['score']}/4{'':<6} {rsi_text}; {note.split(' • ',1)[-1]}")
        else:
            print(f"{symbol:<16} {direction:<9} {'—':<10} {note[:65]}")
    print("=" * 92)
    print("ВАЖЛИВО: BUY/SELL — лише технічний сигнал, не прогноз із гарантованою точністю.")
    print("Перевіряй ціну та стан пари безпосередньо в Pocket Option перед будь-яким рішенням.")
    print("Не використовуй сигнал на реальному рахунку без тривалого тестування на демо.")


def main():
    print("POCKET OPTION OTC CURRENCY SCANNER — READ ONLY")
    print("Цей сканер не входить у Pocket Option і не відкриває угоди.\n")
    api_key = os.getenv("OTCHARTS_API_KEY")
    if not api_key:
        api_key = getpass.getpass("Встав свій OTCharts API key (ввід прихований): ").strip()
    if not api_key:
        print("API key не введено.")
        return

    while True:
        try:
            print(f"\nЧас сканування: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")
            scan_once(api_key)
        except KeyboardInterrupt:
            print("\nЗупинено.")
            break
        except Exception as exc:
            print(f"\nПомилка: {exc}")
            print("Перевір API key, доступ до книги OTC і ліміт запитів.")
        print(f"\nНаступне сканування приблизно через {SCAN_INTERVAL_MINUTES} хв.")
        try:
            time.sleep(SCAN_INTERVAL_MINUTES * 60)
        except KeyboardInterrupt:
            print("\nЗупинено.")
            break


if __name__ == "__main__":
    main()
