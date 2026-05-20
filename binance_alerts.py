"""
Binance Long Setup Alert Bot
- Runs every hour
- Analyzes BTC, XRP, SOL using 1H candle data
- Claude scores each coin against 6-factor framework
- Sends Telegram alert only if score >= 7.5/10
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
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]

COINS = ["BTCUSDT", "XRPUSDT", "SOLUSDT"]
MIN_SCORE = float(os.environ.get("MIN_SCORE", "7.5"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "3600"))  # 1 hour

BINANCE_BASE = "https://fapi.binance.com"  # Futures API
BINANCE_SPOT = "https://api.binance.com"


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

def get_klines(symbol: str, interval: str = "1h", limit: int = 50) -> list:
    """Fetch candlestick data from Binance Futures."""
    try:
        r = requests.get(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Failed to fetch klines for {symbol}: {e}")
        return []


def get_current_price(symbol: str) -> float:
    """Get current mark price from Binance Futures."""
    try:
        r = requests.get(
            f"{BINANCE_BASE}/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            timeout=10
        )
        r.raise_for_status()
        return float(r.json()["markPrice"])
    except Exception as e:
        log.error(f"Failed to fetch price for {symbol}: {e}")
        return 0.0


def get_btc_dominance() -> float:
    """Get BTC dominance from CoinGecko (free, no auth)."""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=10
        )
        r.raise_for_status()
        return r.json()["data"]["market_cap_percentage"]["btc"]
    except Exception as e:
        log.error(f"Failed to fetch BTC dominance: {e}")
        return 0.0


def calculate_indicators(klines: list) -> dict:
    """Calculate key technical indicators from kline data."""
    if len(klines) < 20:
        return {}

    closes = [float(k[4]) for k in klines]
    highs  = [float(k[2]) for k in klines]
    lows   = [float(k[3]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    # Current candle (last closed)
    current_close = closes[-1]
    current_high  = highs[-1]
    current_low   = lows[-1]

    # EMA 20
    ema20 = closes[-20]
    k_val = 2 / (20 + 1)
    for c in closes[-19:]:
        ema20 = c * k_val + ema20 * (1 - k_val)

    # EMA 50
    ema50 = closes[-50] if len(closes) >= 50 else closes[0]
    k_val = 2 / (50 + 1)
    for c in closes[-(min(50, len(closes))):]:
        ema50 = c * k_val + ema50 * (1 - k_val)

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

    # Volume analysis
    avg_volume = sum(volumes[-20:-1]) / 19
    current_volume = volumes[-1]
    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1

    # Recent support (lowest low of last 20 candles excluding current)
    recent_support = min(lows[-21:-1])
    recent_resistance = max(highs[-21:-1])

    # Price vs EMAs
    above_ema20 = current_close > ema20
    above_ema50 = current_close > ema50

    # 24h change
    change_24h = ((current_close - closes[-25]) / closes[-25] * 100) if len(closes) >= 25 else 0

    return {
        "current_price": current_close,
        "current_high": current_high,
        "current_low": current_low,
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


def get_news(coin: str) -> str:
    """Quick web search for recent coin news."""
    coin_name = {"BTCUSDT": "Bitcoin BTC", "XRPUSDT": "XRP Ripple", "SOLUSDT": "Solana SOL"}
    name = coin_name.get(coin, coin)
    try:
        r = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": name,
                "sortBy": "publishedAt",
                "pageSize": 3,
                "language": "en"
            },
            timeout=10
        )
        if r.ok:
            articles = r.json().get("articles", [])
            headlines = [a["title"] for a in articles[:3]]
            return " | ".join(headlines) if headlines else "No recent news found"
    except:
        pass
    return "News unavailable"


# CLAUDE ANALYSIS

def analyse_coin(symbol: str, indicators: dict, btc_dominance: float, news: str, btc_indicators: dict) -> dict:
    """Send coin data to Claude for 6-factor analysis."""

    coin_name = {"BTCUSDT": "Bitcoin (BTC)", "XRPUSDT": "XRP", "SOLUSDT": "Solana (SOL)"}
    name = coin_name.get(symbol, symbol)
    is_btc = symbol == "BTCUSDT"

    btc_context = ""
    if not is_btc and btc_indicators:
        btc_context = f"""
BTC CONTEXT (for altcoin analysis):
- BTC Price: ${btc_indicators.get('current_price', 0):,.2f}
- BTC RSI: {btc_indicators.get('rsi', 0)}
- BTC above EMA20: {btc_indicators.get('above_ema20', False)}
- BTC above EMA50: {btc_indicators.get('above_ema50', False)}
- BTC 24h change: {btc_indicators.get('change_24h', 0)}%
"""

    prompt = f"""You are an expert crypto trader analyzing {name} for a potential LONG entry on perpetual futures with 20x leverage.

COIN: {name}
TIMEFRAME: 1 Hour candles

PRICE DATA:
- Current Price: ${indicators.get('current_price', 0):,.4f}
- 24h Change: {indicators.get('change_24h', 0)}%
- Recent Support: ${indicators.get('recent_support', 0):,.4f}
- Recent Resistance: ${indicators.get('recent_resistance', 0):,.4f}

TECHNICAL INDICATORS:
- EMA20: ${indicators.get('ema20', 0):,.4f} (price {'above' if indicators.get('above_ema20') else 'below'})
- EMA50: ${indicators.get('ema50', 0):,.4f} (price {'above' if indicators.get('above_ema50') else 'below'})
- RSI (14): {indicators.get('rsi', 0)}
- Volume vs 20-period avg: {indicators.get('volume_ratio', 0)}x
{btc_context}
MARKET CONTEXT:
- BTC Dominance: {btc_dominance:.1f}%
- Recent News: {news}

TRADER'S 6-FACTOR FRAMEWORK (score each 1-10):
1. Technical Analysis — EMAs, RSI, support/resistance, price structure
2. News & Catalysts — any positive/negative news affecting this coin
3. Market Sentiment — fear/greed, overall market conditions
4. Whale/On-Chain — volume spikes, unusual activity indicators
5. Bitcoin's Position — BTC trend and dominance (for alts: is BTC stable/bullish?)
6. Timeframe Alignment — does 1H align with likely 4H trend?

RULES:
- LONGS ONLY — only approve if setup is bullish
- Price should be near support, not extended
- RSI should not be overbought (above 75)
- For alts: BTC must be stable or bullish
- If any major negative news: reduce score significantly
- Minimum 1:2 risk/reward must be achievable

Respond in JSON only, no other text:
{{
  "score": 0.0-10.0,
  "factor_scores": {{
    "technical": 0-10,
    "news": 0-10,
    "sentiment": 0-10,
    "whale": 0-10,
    "btc_position": 0-10,
    "timeframe": 0-10
  }},
  "summary": "2-3 sentence analysis",
  "entry_zone": "price range e.g. 142.00-144.00",
  "invalidation": "price level where setup is invalid",
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
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        if not r.ok:
            log.error(f"Claude API error: {r.status_code}")
            return {}
        raw = r.json()["content"][0]["text"].strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"Claude analysis failed for {symbol}: {e}")
        return {}


# MAIN LOOP

def format_alert(symbol: str, indicators: dict, analysis: dict) -> str:
    coin_name = {"BTCUSDT": "BTC/USDT", "XRPUSDT": "XRP/USDT", "SOLUSDT": "SOL/USDT"}
    name = coin_name.get(symbol, symbol)
    score = analysis.get("score", 0)
    factors = analysis.get("factor_scores", {})

    return (
        f"🟢 <b>{name} LONG SETUP — {score}/10</b>\n\n"
        f"💰 <b>Price:</b> ${indicators.get('current_price', 0):,.4f}\n"
        f"📊 <b>RSI:</b> {indicators.get('rsi', 0)} | "
        f"<b>Vol:</b> {indicators.get('volume_ratio', 0)}x avg\n"
        f"📈 <b>EMA20:</b> {'✅ Above' if indicators.get('above_ema20') else '❌ Below'} | "
        f"<b>EMA50:</b> {'✅ Above' if indicators.get('above_ema50') else '❌ Below'}\n\n"
        f"<b>Factor Scores:</b>\n"
        f"  Technical: {factors.get('technical', 0)}/10\n"
        f"  News: {factors.get('news', 0)}/10\n"
        f"  Sentiment: {factors.get('sentiment', 0)}/10\n"
        f"  Whale/Volume: {factors.get('whale', 0)}/10\n"
        f"  BTC Position: {factors.get('btc_position', 0)}/10\n"
        f"  Timeframe: {factors.get('timeframe', 0)}/10\n\n"
        f"📝 {analysis.get('summary', '')}\n\n"
        f"✅ <b>Entry Zone:</b> {analysis.get('entry_zone', 'N/A')}\n"
        f"🛑 <b>Invalidation:</b> {analysis.get('invalidation', 'N/A')}\n"
        f"🎯 <b>Target:</b> {analysis.get('target', 'N/A')}\n"
        f"⚖️ <b>R:R:</b> {analysis.get('risk_reward', 'N/A')}\n\n"
        f"⚠️ <i>Run your full checklist before entering. 20x leverage — manage risk.</i>"
    )


def run_analysis():
    log.info(f"🔍 Running analysis at {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

    btc_dominance = get_btc_dominance()
    log.info(f"BTC Dominance: {btc_dominance:.1f}%")

    # Get BTC data first (needed for altcoin context)
    btc_klines = get_klines("BTCUSDT", "1h", 60)
    btc_indicators = calculate_indicators(btc_klines) if btc_klines else {}

    alerts_sent = 0

    for symbol in COINS:
        log.info(f"Analyzing {symbol}...")

        klines = get_klines(symbol, "1h", 60)
        if not klines:
            log.warning(f"No kline data for {symbol}")
            continue

        indicators = calculate_indicators(klines)
        if not indicators:
            continue

        news = get_news(symbol)
        btc_ctx = btc_indicators if symbol != "BTCUSDT" else {}
        analysis = analyse_coin(symbol, indicators, btc_dominance, news, btc_ctx)

        if not analysis:
            continue

        score = analysis.get("score", 0)
        log.info(f"{symbol}: Score {score}/10")

        if score >= MIN_SCORE:
            log.info(f"✅ {symbol} meets threshold — sending alert")
            alert = format_alert(symbol, indicators, analysis)
            send_telegram(alert)
            alerts_sent += 1
        else:
            log.info(f"❌ {symbol} score {score} below {MIN_SCORE} — no alert")

        time.sleep(2)  # Small delay between coins

    if alerts_sent == 0:
        log.info("No setups met threshold this hour")


def run():
    log.info("🚀 Binance Alert Bot starting...")
    send_telegram("🚀 <b>Binance Alert Bot Started!</b>\nAnalyzing BTC, XRP, SOL every hour\nMin score: 7.5/10 for alert")

    while True:
        try:
            run_analysis()
        except Exception as e:
            log.error(f"Analysis error: {e}")
            send_telegram(f"⚠️ Binance bot error: {e}")
        
        log.info(f"Sleeping {CHECK_INTERVAL}s until next analysis...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run()
