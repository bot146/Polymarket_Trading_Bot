"""Deep dive into 5-minute crypto markets: structure, volume, timing, and opportunity."""
import requests, json
from datetime import datetime, timezone, timedelta

GAMMA = "https://gamma-api.polymarket.com"
now = datetime.now(timezone.utc)

# Get ALL active markets sorted by most recently created
print(f"Current UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}")

resp = requests.get(f"{GAMMA}/markets", params={
    "limit": 500,
    "active": "true",
    "closed": "false",
    "order": "createdAt",
    "ascending": "false",
})
all_recent = resp.json()

# Filter for 5-minute crypto pattern
five_min = [m for m in all_recent if any(kw in m.get("question", "").lower() for kw in ["up or down", "5-min", "10:"])]
print(f"\n5-minute crypto markets found: {len(five_min)}")

# Group by crypto
btc = [m for m in five_min if "bitcoin" in m.get("question", "").lower()]
eth = [m for m in five_min if "ethereum" in m.get("question", "").lower()]
sol = [m for m in five_min if "solana" in m.get("question", "").lower()]
xrp = [m for m in five_min if "xrp" in m.get("question", "").lower()]
print(f"  Bitcoin: {len(btc)}, Ethereum: {len(eth)}, Solana: {len(sol)}, XRP: {len(xrp)}")

# Examine one Bitcoin 5-min market in detail
if btc:
    print("\n=== Sample Bitcoin 5-min market (full details) ===")
    sample = btc[0]
    important_fields = [
        "question", "conditionId", "slug", "volume", "liquidity",
        "endDateIso", "endDate", "createdAt", "startDate",
        "outcomes", "outcomePrices", "clobTokenIds",
        "negRisk", "negRiskMarketID", "active", "closed", "resolved",
        "gameStartTime", "secondsDelay", "fpmm",
        "enableOrderBook", "orderPriceMinTickSize", "orderMinSize",
        "description", "tags", "groupItemTitle", "negRiskRequestID",
    ]
    for field in important_fields:
        val = sample.get(field)
        if val is not None:
            if isinstance(val, str) and len(val) > 100:
                val = val[:100] + "..."
            print(f"  {field}: {val}")
    
    # Check all fields we might be missing
    print("\n  --- All raw fields ---")
    for k, v in sorted(sample.items()):
        if k not in important_fields:
            if isinstance(v, str) and len(v) > 80:
                v = v[:80] + "..."
            print(f"  {k}: {v}")

# Check which ones resolve TODAY vs TOMORROW
print("\n=== Resolution timing for Bitcoin 5-min markets ===")
for m in btc[:10]:
    q = m.get("question", "?")
    end_iso = m.get("endDateIso", "?")
    end_full = m.get("endDate", "?")
    created = m.get("createdAt", "?")[:19]
    vol = float(m.get("volume", 0))
    liq = float(m.get("liquidity", 0))
    prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
    outcomes = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
    tokens = json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds", [])
    neg_risk = m.get("negRisk", False)
    nr_id = m.get("negRiskMarketID", "")
    
    print(f"\n  Q: {q}")
    print(f"  created: {created}  endDateIso: {end_iso}  endDate: {end_full}")
    print(f"  vol: ${vol:.0f}  liq: ${liq:.0f}  negRisk: {neg_risk}")
    print(f"  outcomes: {outcomes}  prices: {prices}")
    if nr_id:
        print(f"  negRiskMarketID: {nr_id[:30]}...")
    if tokens:
        print(f"  tokens: {[t[:12]+'...' for t in tokens]}")

# Also check: are there any CLOSED 5-min Bitcoin markets that resolved?
print("\n=== Resolved 5-min Bitcoin markets (to see P&L potential) ===")
resp2 = requests.get(f"{GAMMA}/markets", params={
    "limit": 500,
    "closed": "true",
    "order": "createdAt",
    "ascending": "false",
})
closed = resp2.json()
closed_5min = [m for m in closed if "bitcoin" in m.get("question", "").lower() and "up or down" in m.get("question", "").lower()]
print(f"Found {len(closed_5min)} closed Bitcoin 5-min markets")
for m in closed_5min[:10]:
    q = m.get("question", "?")[:70]
    vol = float(m.get("volume", 0))
    resolved = m.get("resolved")
    winner = m.get("winningOutcome") or m.get("winner", "?")
    prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
    outcomes = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
    created = m.get("createdAt", "?")[:19]
    end = m.get("endDateIso", "?")
    
    # Find winner from tokens
    tokens_data = m.get("tokens", [])
    winner_info = ""
    for t in (tokens_data or []):
        if isinstance(t, dict) and t.get("winner"):
            winner_info = f" WINNER={t.get('outcome')}"
    
    print(f"  [{created}] ${vol:>8,.0f} resolved={resolved}{winner_info}")
    print(f"    {q}")
    print(f"    outcomes={outcomes} prices={prices}")
