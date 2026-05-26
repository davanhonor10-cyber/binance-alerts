"""
Binance Long Setup Alert Bot v2
- Runs every 30 minutes
- Analyzes BTC, XRP, SOL using 1H candle data
- Claude scores each coin against 6-factor framework
- Alerts only if score >= 7.5/10
- Daily status update at 11:00 UTC (7am Trinidad)
- Uses BINANCE_TELEGRAM_TOKEN (separate from Polymarket bot)
"""

import os
import time
import json
import logging
import requests
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Use dedicated Binance Telegram bot
TELEGRAM_TOKEN    = os.environ.get("BINANCE_TELEGRAM_TOKEN") or os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]

COINS = ["BTCUSDT", "XRPUSDT", "SOLUSDT"]
MIN_SCORE       = float(os.environ.get("MIN_SCORE", "7.5"))
CHECK_INTERVAL  = int(os.environ.get("CHECK_INTERVAL", "1800"))  # 30 minutes
STATUS_HOUR_UTC = int(os.environ.get("STATUS_HOUR_UTC", "11"))   # 11 UTC = 7am Trinidad

BINANCE_FUTURES = "https://fapi.binance.com"

# Tracking
alerts_sent_today = 0
last_status_date = None
last_analysis_time = None
analysis_count_today = 0


# TELEGRAM

def send_telegram(message: str):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        if not r.ok:
            log.error(f"Telegram error: {r.text}")
    except Exception as e:
        log.error(f"Telegram failed: {e}")


# BINANCE DATA

def get_klines(symbol: str, interval: str = "1h", limit: int = 60) -> list:
    try:
        r = requests.get(
            f"{BINANCE_FUTURES}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Failed to fetch klines for {symbol}: {e}")
        return []


def get_current_price(symbol: str) -> float:
    try:
        r = requests.get(
            f"{BINANCE_FUTURES}/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            timeout=10
        )
        r.raise_for_status()
        return float(r.json()["markPrice"])
    except Exception as e:
        log.error(f"Failed to fetch price for {symbol}: {e}")
        return 0.0


def get_btc_dominance() -> float:
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
        r.raise_for_status()
        return r.json()["data"]["market_cap_percentage"]["btc"]
    except Exception as e:
        log.error(f"Failed to fetch BTC dominance: {e}")
        return 0.0


def calculate_indicators(klines: list) -> dict:
    if len(klines) < 20:
        return {}

    closes  = [float(k[4]) for k in klines]
    highs   = [float(k[2]) for k in klines]
    lows    = [float(k[3]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    current_close = closes[-1]

    # EMA 20
    ema20 = closes[-20]
    k20 = 2 / 21
    for c in closes[-19:]:
        ema20 = c * k20 + ema20 * (1 - k20)

    # EMA 50
    ema50 = closes[0]
    k50 = 2 / 51
    for c in closes[1:]:
        ema50 = c * k50 + ema50 * (1 - k50)

    # RSI 14
    gains, losses = [], []
    for i in range(-14, 0):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / 14
    avg_loss = sum(losses) / 14
    rs = avg_gain / avg_loss if avg_loss > 0 else 100
    rsi = 100 - (100 / (1 + rs))

    avg_volume     = sum(volumes[-20:-1]) / 19
    current_volume = volumes[-1]
    volume_ratio   = current_volume / avg_volume if avg_volume > 0 else 1

    recent_support    = min(lows[-21:-1])
    recent_resistance = max(highs[-21:-1])
    above_ema20       = current_close > ema20
    above_ema50       = current_close > ema50
    change_24h        = ((current_close - closes[-25]) / closes[-25] * 100) if len(closes) >= 25 else 0

    return {
        "current_price": current_close,
        "ema20": round(ema20, 4),
        "ema50": round(ema50, 4),
        "rsi": round(rsi, 1),
        "volume_ratio": round(volume_ratio, 2),
        "recent_support": round(recent_support, 4),
        "recent_resistance": round(recent_resistance, 4),
        "above_ema20": above_ema20,
        "above_ema50": above_ema50,
        "change_24h": round(change_24h, 2),
    }


# CLAUDE ANALYSIS

def analyse_coin(symbol: str, indicators: dict, btc_dominance: float, btc_indicators: dict) -> dict:
    coin_name = {"BTCUSDT": "Bitcoin (BTC)", "XRPUSDT": "XRP", "SOLUSDT": "Solana (SOL)"}
    name = coin_name.get(symbol, symbol)
    is_btc = symbol == "BTCUSDT"

    btc_context = ""
    if not is_btc and btc_indicators:
        btc_context = f"""
BTC CONTEXT:
- BTC Price: ${btc_indicators.get('current_price', 0):,.2f}
- BTC RSI: {btc_indicators.get('rsi', 0)}
- BTC above EMA20: {btc_indicators.get('above_ema20', False)}
- BTC above EMA50: {btc_indicators.get('above_ema50', False)}
- BTC 24h change: {btc_indicators.get('change_24h', 0)}%
"""

    prompt = f"""You are an expert crypto trader analyzing {name} for a LONG entry on perpetual futures with 20x leverage.

COIN: {name} | TIMEFRAME: 1H

PRICE DATA:
- Current: ${indicators.get('current_price', 0):,.4f}
- 24h Change: {indicators.get('change_24h', 0)}%
- Support: ${indicators.get('recent_support', 0):,.4f}
- Resistance: ${indicators.get('recent_resistance', 0):,.4f}

INDICATORS:
- EMA20: ${indicators.get('ema20', 0):,.4f} ({'above' if indicators.get('above_ema20') else 'below'})
- EMA50: ${indicators.get('ema50', 0):,.4f} ({'above' if indicators.get('above_ema50') else 'below'})
- RSI(14): {indicators.get('rsi', 0)}
- Volume: {indicators.get('volume_ratio', 0)}x avg
{btc_context}
BTC Dominance: {btc_dominance:.1f}%

TRADER RULES:
- LONGS ONLY
- Entry near support, not extended
- RSI not overbought (above 75 = reduce score)
- For alts: BTC must be stable/bullish
- Minimum 1:2 risk/reward required
- 20x leverage means only HIGH conviction setups

6-FACTOR SCORING (1-10 each):
1. Technical Analysis
2. News & Catalysts
3. Market Sentiment
4. Whale/Volume Activity
5. Bitcoin Position
6. Timeframe Alignment

Respond in JSON only:
{{
  "score": 0.0-10.0,
  "factor_scores": {{"technical": 0, "news": 0, "sentiment": 0, "whale": 0, "btc_position": 0, "timeframe": 0}},
  "summary": "2-3 sentences",
  "entry_zone": "e.g. 142.00-144.00",
  "invalidation": "price level",
  "target": "price target",
  "risk_reward": "e.g. 1:2.5"
}}"""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-haiku-4-5",
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        if not r.ok:
            log.error(f"Claude API error: {r.status_code}")
            return {}
        raw = r.json()["content"][0]["text"].strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        # Extract just the JSON object to handle extra text after it
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            log.error(f"No JSON found in response: {raw[:100]}")
            return {}
        raw = raw[start:end]
        return json.loads(raw)
    except Exception as e:
        log.error(f"Claude analysis failed for {symbol}: {e}")
        return {}


def format_alert(symbol: str, indicators: dict, analysis: dict) -> str:
    coin_name = {"BTCUSDT": "BTC/USDT", "XRPUSDT": "XRP/USDT", "SOLUSDT": "SOL/USDT"}
    name = coin_name.get(symbol, symbol)
    score = analysis.get("score", 0)
    factors = analysis.get("factor_scores", {})

    return (
        f"🟢 <b>{name} LONG SETUP — {score}/10</b>\n\n"
        f"💰 <b>Price:</b> ${indicators.get('current_price', 0):,.4f}\n"
        f"📊 <b>RSI:</b> {indicators.get('rsi', 0)} | "
        f"<b>Vol:</b> {indicators.get('volume_ratio', 0)}x\n"
        f"📈 EMA20: {'✅' if indicators.get('above_ema20') else '❌'} | "
        f"EMA50: {'✅' if indicators.get('above_ema50') else '❌'}\n\n"
        f"<b>Scores:</b>\n"
        f"  Tech: {factors.get('technical', 0)} | News: {factors.get('news', 0)} | "
        f"Sentiment: {factors.get('sentiment', 0)}\n"
        f"  Whale: {factors.get('whale', 0)} | BTC: {factors.get('btc_position', 0)} | "
        f"TF: {factors.get('timeframe', 0)}\n\n"
        f"📝 {analysis.get('summary', '')}\n\n"
        f"✅ <b>Entry:</b> {analysis.get('entry_zone', 'N/A')}\n"
        f"🛑 <b>Invalidation:</b> {analysis.get('invalidation', 'N/A')}\n"
        f"🎯 <b>Target:</b> {analysis.get('target', 'N/A')}\n"
        f"⚖️ <b>R:R:</b> {analysis.get('risk_reward', 'N/A')}\n\n"
        f"⚠️ <i>Run full checklist. 20x leverage — manage risk.</i>"
    )


def send_status_update(prices: dict, btc_dominance: float):
    now_utc = datetime.now(timezone.utc)
    send_telegram(
        f"📊 <b>Daily Status Update</b>\n"
        f"🕖 7:00 AM Trinidad | {now_utc.strftime('%Y-%m-%d')}\n\n"
        f"✅ <b>Bot Status:</b> Running\n"
        f"⏱ <b>Check interval:</b> Every 30 mins\n"
        f"📈 <b>Analyses today:</b> {analysis_count_today}\n"
        f"🔔 <b>Alerts sent today:</b> {alerts_sent_today}\n\n"
        f"<b>Current Prices:</b>\n"
        f"  BTC: ${prices.get('BTCUSDT', 0):,.2f}\n"
        f"  XRP: ${prices.get('XRPUSDT', 0):,.4f}\n"
        f"  SOL: ${prices.get('SOLUSDT', 0):,.2f}\n\n"
        f"🌐 <b>BTC Dominance:</b> {btc_dominance:.1f}%\n\n"
        f"Min score for alert: {MIN_SCORE}/10"
    )


def run_analysis():
    global alerts_sent_today, analysis_count_today, last_analysis_time

    now = datetime.now(timezone.utc)
    last_analysis_time = now
    analysis_count_today += 1
    log.info(f"🔍 Analysis #{analysis_count_today} at {now.strftime('%H:%M UTC')}")

    btc_dominance = get_btc_dominance()
    log.info(f"BTC Dominance: {btc_dominance:.1f}%")

    btc_klines = get_klines("BTCUSDT", "1h", 60)
    btc_indicators = calculate_indicators(btc_klines) if btc_klines else {}

    prices = {}
    for symbol in COINS:
        prices[symbol] = get_current_price(symbol)

    for symbol in COINS:
        log.info(f"Analyzing {symbol}...")
        klines = get_klines(symbol, "1h", 60)
        if not klines:
            continue
        indicators = calculate_indicators(klines)
        if not indicators:
            continue
        btc_ctx = btc_indicators if symbol != "BTCUSDT" else {}
        analysis = analyse_coin(symbol, indicators, btc_dominance, btc_ctx)
        if not analysis:
            continue
        score = analysis.get("score", 0)
        log.info(f"{symbol}: {score}/10")
        if score >= MIN_SCORE:
            log.info(f"✅ {symbol} alert!")
            send_telegram(format_alert(symbol, indicators, analysis))
            alerts_sent_today += 1
        time.sleep(2)

    return prices, btc_dominance


def run():
    global last_status_date, alerts_sent_today, analysis_count_today

    log.info("🚀 Binance Alert Bot starting...")
    send_telegram(
        f"🚀 <b>Binance Alert Bot Started!</b>\n"
        f"Analyzing BTC, XRP, SOL every 30 mins\n"
        f"Min score: {MIN_SCORE}/10 for alert\n"
        f"Daily status: 7am Trinidad time"
    )

    last_prices = {}
    last_dominance = 0.0

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            today = now_utc.date()

            # Reset daily counters
            if last_status_date != today:
                alerts_sent_today = 0
                analysis_count_today = 0

            # Send daily status at 11 UTC (7am Trinidad)
            if (now_utc.hour == STATUS_HOUR_UTC and
                now_utc.minute < 31 and
                last_status_date != today):
                send_status_update(last_prices, last_dominance)
                last_status_date = today

            last_prices, last_dominance = run_analysis()

        except Exception as e:
            log.error(f"Analysis error: {e}")
            send_telegram(f"⚠️ Binance bot error: {e}")

        log.info(f"Sleeping {CHECK_INTERVAL}s...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run()
