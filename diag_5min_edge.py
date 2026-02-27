"""Analyze today's resolved 5-min markets: volumes, spreads, and timing for edge assessment."""
import requests, json
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
now = datetime.now(timezone.utc)

# Get recently closed markets (more pages)
all_closed_5min = []
for page_order in ["createdAt"]:
    resp = requests.get(f"{GAMMA}/markets", params={
        "limit": 500,
        "closed": "true",
        "order": page_order,
        "ascending": "false",
    })
    for m in resp.json():
        if "up or down" in m.get("question", "").lower():
            all_closed_5min.append(m)

# Deduplicate by conditionId
seen = set()
unique = []
for m in all_closed_5min:
    cid = m.get("conditionId", "")
    if cid not in seen:
        seen.add(cid)
        unique.append(m)
all_closed_5min = unique

print(f"Recent closed 'Up or Down' markets: {len(all_closed_5min)}")

# Group by crypto
for crypto in ["bitcoin", "ethereum", "solana", "xrp"]:
    subset = [m for m in all_closed_5min if crypto in m.get("question", "").lower()]
    print(f"  {crypto}: {len(subset)}")

# Detailed analysis of Bitcoin markets
btc_closed = [m for m in all_closed_5min if "bitcoin" in m.get("question", "").lower()]
btc_closed.sort(key=lambda m: m.get("endDate", ""))

print(f"\n=== Bitcoin 5-min Markets - Detailed ===")
print(f"{'Question':<55} {'Vol':>10} {'Liq':>10} {'Winner':>6} {'EndDate':>22}")
print("-" * 110)

total_vol = 0
for m in btc_closed:
    q = m.get("question", "?")
    # Extract just the time portion
    q_short = q.replace("Bitcoin Up or Down - ", "")[:35]
    
    vol = float(m.get("volume", 0))
    liq = float(m.get("liquidity", 0))
    end = m.get("endDate", "?")
    total_vol += vol
    
    # Determine winner
    prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
    outcomes = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
    winner = "?"
    if len(prices) == 2 and len(outcomes) == 2:
        if prices[0] == "1":
            winner = outcomes[0]
        elif prices[1] == "1":
            winner = outcomes[1]
    
    print(f"  {q_short:<53} ${vol:>9,.0f} ${liq:>9,.0f} {winner:>6} {end}")

print(f"\n  Total volume across {len(btc_closed)} BTC markets: ${total_vol:,.0f}")
avg_vol = total_vol / len(btc_closed) if btc_closed else 0
print(f"  Average volume per market: ${avg_vol:,.0f}")

# Check what today's live-trading BTC markets look like (the ones that should be traded tomorrow)
print(f"\n=== Tomorrow's BTC 5-min Markets (currently booking orders) ===")
resp2 = requests.get(f"{GAMMA}/markets", params={
    "limit": 500,
    "active": "true", 
    "closed": "false",
    "order": "createdAt",
    "ascending": "false",
})
active = resp2.json()
btc_tomorrow = [m for m in active if "bitcoin" in m.get("question", "").lower() and "up or down" in m.get("question", "").lower() and "february 27" in m.get("question", "").lower()]
btc_tomorrow.sort(key=lambda m: m.get("endDate", ""))

print(f"Active BTC 5-min for Feb 27: {len(btc_tomorrow)}")
for m in btc_tomorrow[:5]:
    q = m.get("question", "?").replace("Bitcoin Up or Down - ", "")[:40]
    end = m.get("endDate", "?")
    event_start = m.get("eventStartTime", "?")
    liq = float(m.get("liquidity", 0))
    prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
    best_bid = m.get("bestBid", "?")
    best_ask = m.get("bestAsk", "?")
    spread = m.get("spread", "?")
    order_min = m.get("orderMinSize", "?")
    rewards_min = m.get("rewardsMinSize", "?")
    rewards_spread = m.get("rewardsMaxSpread", "?")
    
    print(f"\n  {q}")
    print(f"    event_start={event_start} end={end}")
    print(f"    liq=${liq:,.0f} bid={best_bid} ask={best_ask} spread={spread}")
    print(f"    prices={prices} min_order={order_min} rewards_min={rewards_min} rewards_spread={rewards_spread}")
    
    # Check CLOB book
    token_ids = json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds", [])
    outcomes = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
    for i, tid in enumerate(token_ids[:2]):
        try:
            r = requests.get(f"{CLOB}/book", params={"token_id": tid})
            book = r.json()
            asks = book.get("asks", [])[:3]
            bids = book.get("bids", [])[-3:]
            out = outcomes[i] if i < len(outcomes) else f"token{i}"
            ask_str = " | ".join(f"{a['price']}x{a['size']}" for a in asks) if asks else "empty"
            bid_str = " | ".join(f"{b['price']}x{b['size']}" for b in reversed(bids)) if bids else "empty"
            print(f"    {out}: bids=[{bid_str}] asks=[{ask_str}]")
        except Exception as e:
            print(f"    token {i}: err={e}")

# Summary: fee structure for these markets
print(f"\n=== FEE & EDGE ANALYSIS ===")
if btc_tomorrow:
    m = btc_tomorrow[0]
    fee_type = m.get("feeType", "?")
    taker_fee = m.get("takerBaseFee", "?")  # in basis points
    maker_fee = m.get("makerBaseFee", "?")
    print(f"  Fee type: {fee_type}")
    print(f"  Taker fee: {taker_fee} bps")
    print(f"  Maker fee: {maker_fee} bps")
    print(f"  At 50/50 pricing (0.50 each):")
    taker_bps = int(taker_fee) if str(taker_fee).isdigit() else 200
    maker_bps = int(maker_fee) if str(maker_fee).isdigit() else 100
    print(f"    Taker cost on $0.50 buy: ${0.50 * taker_bps / 10000:.4f}")
    print(f"    If you buy 'Up' at $0.50 and it wins:")
    print(f"      Payout: $1.00")
    print(f"      Cost: $0.50 + fee = ${0.50 + 0.50 * taker_bps / 10000:.4f}")
    print(f"      Profit: ${1.0 - 0.50 - 0.50 * taker_bps / 10000:.4f}")
    print(f"    If you buy 'Up' at $0.90 (clear direction) and it wins:")
    print(f"      Payout: $1.00")
    print(f"      Cost: $0.90 + fee = ${0.90 + 0.90 * taker_bps / 10000:.4f}")
    print(f"      Profit: ${1.0 - 0.90 - 0.90 * taker_bps / 10000:.4f}")
