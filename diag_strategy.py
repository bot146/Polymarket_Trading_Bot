"""Diagnostic: What markets made it through the filter, and why no strategy signals?"""
import json, requests
from datetime import datetime, timezone
from decimal import Decimal

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# Fetch same 500 markets the scanner uses
resp = requests.get(f"{GAMMA}/markets", params={
    "closed": "false", "active": "true", "limit": 500,
    "order": "volumeNum", "ascending": "false",
})
all_markets = resp.json()
now = datetime.now(timezone.utc)

# Apply same filters: min_volume=1000, within 72h, not expired
MIN_VOL = 1000
WINDOW_H = 72
kept = []
for m in all_markets:
    vol = float(m.get("volume", 0))
    if vol < MIN_VOL:
        continue
    end_str = m.get("endDateIso") or m.get("endDate")
    if not end_str:
        continue
    try:
        if "T" in end_str:
            edt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        else:
            edt = datetime.fromisoformat(end_str + "T00:00:00+00:00")
    except:
        continue
    hours = (edt - now).total_seconds() / 3600
    if hours < 0 or hours > WINDOW_H:
        continue
    kept.append((hours, m))

print(f"Markets passing filter: {len(kept)}\n")

# Group by neg-risk
neg_risk_groups = {}
binary_markets = []
for hours, m in kept:
    nrid = m.get("negRiskMarketID")
    if nrid:
        neg_risk_groups.setdefault(nrid, []).append((hours, m))
    else:
        binary_markets.append((hours, m))

print(f"Binary (YES/NO) markets: {len(binary_markets)}")
print(f"Neg-risk groups: {len(neg_risk_groups)} ({sum(len(v) for v in neg_risk_groups.values())} brackets)")

# Check binary markets for arb/guaranteed_win
print("\n--- Binary Markets ---")
for hours, m in binary_markets:
    q = m.get("question", "?")[:60]
    prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
    outcomes = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
    
    if len(prices) == 2:
        y, n = float(prices[0]), float(prices[1])
        total = y + n
        fee_rate = 0.02  # 2% taker fee
        total_with_fees = total + total * fee_rate
        edge_cents = (1.0 - total_with_fees) * 100
    else:
        y, n, total, edge_cents = "?", "?", "?", "?"
    
    print(f"  [{hours:.0f}h] {q}")
    print(f"     YES={y} NO={n} sum={total} edge_after_fees={edge_cents:.2f}c")
    
    # Also check CLOB order book
    token_ids = json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds", [])
    if len(token_ids) == 2:
        for i, tid in enumerate(token_ids):
            try:
                r = requests.get(f"{CLOB}/book", params={"token_id": tid})
                book = r.json()
                best_ask = book.get("asks", [{}])[0].get("price") if book.get("asks") else None
                best_bid = book.get("bids", [{}])[-1].get("price") if book.get("bids") else None
                print(f"     Token {outcomes[i] if i < len(outcomes) else i}: bid={best_bid} ask={best_ask}")
            except Exception as e:
                print(f"     Token {i}: CLOB error: {e}")

# Check neg-risk groups for multi-outcome/conditional arb
print("\n--- Neg-Risk Groups ---")
for nrid, brackets in neg_risk_groups.items():
    print(f"\nGroup {nrid[:20]}... ({len(brackets)} brackets)")
    total_ask_sum = 0
    for hours, m in brackets:
        q = (m.get("groupItemTitle") or m.get("question", "?"))[:50]
        prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
        
        if len(prices) >= 1:
            yes_price = float(prices[0])
        else:
            yes_price = 0
        
        total_ask_sum += yes_price
        print(f"  [{hours:.0f}h] {q}: YES={yes_price:.4f}")
    
    multi_edge = 1.0 - total_ask_sum
    print(f"  -> Sum of YES prices: {total_ask_sum:.4f}, multi-arb edge = {multi_edge:.4f} (${multi_edge:.4f})")
    if multi_edge > 0:
        print(f"  *** MULTI-OUTCOME ARB: buy all for ${total_ask_sum:.4f}, payout = $1.00")
    else:
        print(f"  -> No multi-outcome arb (sum >= $1)")
