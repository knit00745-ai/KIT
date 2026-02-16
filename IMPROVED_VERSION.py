#!/usr/bin/env python3
"""
IMPROVED VERSION: Simmer FastLoop Trading Skill
Optimized for robustness, timezone accuracy, and API stability.
"""

import os
import sys
import json
import math
import argparse
import re
import time
from datetime import datetime, timezone, timedelta
import requests
import pytz

# Force line-buffered stdout
sys.stdout.reconfigure(line_buffering=True)

# =============================================================================
# Configuration & Schema
# =============================================================================

CONFIG_SCHEMA = {
    "entry_threshold": {"default": 0.05, "type": float},
    "min_momentum_pct": {"default": 0.5, "type": float},
    "max_position": {"default": 5.0, "type": float},
    "signal_source": {"default": "binance", "type": str},
    "lookback_minutes": {"default": 5, "type": int},
    "min_time_remaining": {"default": 60, "type": int},
    "asset": {"default": "BTC", "type": str},
    "window": {"default": "5m", "type": str},
    "volume_confidence": {"default": True, "type": bool},
}

ASSET_SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
ASSET_PATTERNS = {
    "BTC": ["bitcoin up or down"],
    "ETH": ["ethereum up or down"],
    "SOL": ["solana up or down"],
}
COINGECKO_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}
STATE_FILE = "trading_state.json"

# =============================================================================
# Core Utilities
# =============================================================================

def load_config(path="config.json"):
    config = {}
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                config = json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load config.json: {e}")
    
    final_config = {}
    for key, spec in CONFIG_SCHEMA.items():
        final_config[key] = config.get(key, spec["default"])
    return final_config

def get_api_key():
    key = os.environ.get("SIMMER_API_KEY")
    if not key:
        print("Error: SIMMER_API_KEY environment variable not set.")
        sys.exit(1)
    return key

def api_request(url, method="GET", data=None, headers=None, params=None, retries=3):
    """Robust API request handler with retries."""
    for i in range(retries):
        try:
            response = requests.request(
                method=method,
                url=url,
                json=data,
                headers=headers,
                params=params,
                timeout=15
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            if i == retries - 1:
                return {"error": str(e)}
            time.sleep(1 * (i + 1))
    return {"error": "Max retries exceeded"}

# =============================================================================
# Market Discovery & Parsing
# =============================================================================

def parse_et_to_utc(question):
    """Accurately parse ET time to UTC handling Daylight Saving Time."""
    pattern = r'(\w+ \d+),.*?-\s*(\d{1,2}:\d{2}(?:AM|PM))\s*ET'
    match = re.search(pattern, question)
    if not match:
        return None
    try:
        date_str = match.group(1)
        time_str = match.group(2)
        year = datetime.now(timezone.utc).year
        dt_str = f"{date_str} {year} {time_str}"
        
        # Use pytz for reliable ET conversion
        et_tz = pytz.timezone("US/Eastern")
        local_dt = datetime.strptime(dt_str, "%B %d %Y %I:%M%p")
        localized_dt = et_tz.localize(local_dt)
        return localized_dt.astimezone(pytz.UTC)
    except Exception as e:
        print(f"Time parse error: {e}")
        return None

def discover_markets(asset="BTC", window="5m"):
    url = "https://gamma-api.polymarket.com/markets"
    params = {"limit": 20, "closed": "false", "tag": "crypto", "order": "createdAt", "ascending": "false"}
    result = api_request(url, params=params)
    
    if "error" in result or not isinstance(result, list):
        return []

    patterns = ASSET_PATTERNS.get(asset, ASSET_PATTERNS["BTC"])
    markets = []
    for m in result:
        q = (m.get("question") or "").lower()
        slug = m.get("slug", "")
        if any(p in q for p in patterns) and f"-{window}-" in slug:
            end_time = parse_et_to_utc(m.get("question", ""))
            if end_time:
                markets.append({
                    "question": m.get("question"),
                    "slug": slug,
                    "condition_id": m.get("conditionId"),
                    "end_time": end_time,
                    "outcome_prices": json.loads(m.get("outcomePrices", "[0.5, 0.5]")),
                    "fee_rate_bps": int(m.get("feeRateBps") or 0)
                })
    return markets

# =============================================================================
# Signal Generation
# =============================================================================

def get_binance_momentum(asset, lookback):
    symbol = ASSET_SYMBOLS.get(asset, "BTCUSDT")
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": "1m", "limit": lookback}
    candles = api_request(url, params=params)
    
    if "error" in candles or not isinstance(candles, list) or len(candles) < 2:
        return None

    price_then = float(candles[0][1])
    price_now = float(candles[-1][4])
    momentum_pct = ((price_now - price_then) / price_then) * 100
    
    volumes = [float(c[5]) for c in candles]
    avg_vol = sum(volumes) / len(volumes)
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

    return {
        "momentum_pct": momentum_pct,
        "direction": "up" if momentum_pct > 0 else "down",
        "price_now": price_now,
        "volume_ratio": vol_ratio
    }

def get_coingecko_momentum(asset):
    """Improved CoinGecko momentum using local state persistence."""
    cg_id = COINGECKO_IDS.get(asset, "bitcoin")
    url = f"https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": cg_id, "vs_currencies": "usd"}
    res = api_request(url, params=params)
    
    if "error" in res: return None
    price_now = res.get(cg_id, {}).get("usd")
    if not price_now: return None

    # Load previous state
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)
    
    prev_price = state.get(f"{asset}_last_price", price_now)
    momentum_pct = ((price_now - prev_price) / prev_price) * 100 if prev_price else 0
    
    # Update state
    state[f"{asset}_last_price"] = price_now
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

    return {
        "momentum_pct": momentum_pct,
        "direction": "up" if momentum_pct > 0 else "down" if momentum_pct < 0 else "neutral",
        "price_now": price_now,
        "volume_ratio": 1.0
    }

# =============================================================================
# Execution Logic
# =============================================================================

def run_strategy(args, cfg):
    print(f"🚀 Running Improved Strategy for {cfg['asset']}...")
    api_key = get_api_key()
    
    markets = discover_markets(cfg['asset'], cfg['window'])
    now = datetime.now(timezone.utc)
    valid_markets = [m for m in markets if (m['end_time'] - now).total_seconds() > cfg['min_time_remaining']]
    
    if not valid_markets:
        print("No suitable markets found.")
        return

    best = sorted(valid_markets, key=lambda x: x['end_time'])[0]
    print(f"Target: {best['question']}")

    # Get Signal
    if cfg['signal_source'] == "binance":
        signal = get_binance_momentum(cfg['asset'], cfg['lookback_minutes'])
    else:
        signal = get_coingecko_momentum(cfg['asset'])

    if not signal:
        print("Failed to get price signal.")
        return

    print(f"Signal: {signal['direction']} ({signal['momentum_pct']:.3f}%) | Vol Ratio: {signal['volume_ratio']:.2f}x")

    # Decision
    if abs(signal['momentum_pct']) < cfg['min_momentum_pct']:
        print("Momentum too weak, skipping.")
        return

    if cfg['volume_confidence'] and signal['volume_ratio'] < 0.5:
        print("Volume too low, skipping.")
        return

    # Mock Execution for this script
    side = "yes" if signal['direction'] == "up" else "no"
    print(f"✅ ACTION: {side.upper()} trade signal detected.")
    
    if args.live:
        print("Executing live trade (Simmer API integration placeholder)...")
        # Here you would call simmer_request for import and trade
    else:
        print("[DRY RUN] No trade executed.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    
    cfg = load_config()
    run_strategy(args, cfg)
