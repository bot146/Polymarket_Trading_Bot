"""Quick diagnostic: what are the 19 markets in the 12h window and why no signals?"""
import json, requests, sys
from datetime import datetime, timezone, timedelta

GAMMA = "https://gamma-api.polymarket.com"

# Fetch high-volume markets (same as scanner)
resp = requests.get(f"{GAMMA}/markets", params={
    "closed": "false",
    "active": "true",
    "limit": 500,
    "order": "volumeNum",
    "ascending": "false",
})
all_markets = resp.json()
print(f"Total markets fetched: {len(all_markets)}")

now = datetime.now(timezone.utc)
window_hours = 12

kept = []
for m in all_markets:
    end_str = m.get("endDateIso") or m.get("endDate")
    if not end_str:
        continue
    try:
        # Parse ISO date
        if "T" in end_str:
            edt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        else:
            edt = datetime.fromisoformat(end_str + "T00:00:00+00:00")
    except Exception:
        continue
    hours = (edt - now).total_seconds() / 3600
    if hours < 0:
        continue  # past due
    if hours > window_hours:
        continue  # too far out
    kept.append((hours, m))

print(f"\nMarkets within {window_hours}h window: {len(kept)}\n")

for hours, m in sorted(kept, key=lambda x: x[0]):
    q = m.get("question", "?")[:60]
    cid = m.get("conditionId", "?")[:16]
    neg_risk = m.get("negRisk", False)
    neg_risk_id = m.get("negRiskMarketID", "")
    
    # Get token prices
    tokens = json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds", [])
    prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
    outcomes = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
    
    if len(prices) == 2:
        try:
            p0, p1 = float(prices[0]), float(prices[1])
            total = p0 + p1
            edge = 1.0 - total
            edge_cents = edge * 100
        except:
            total = None
            edge_cents = None
    else:
        total = None
        edge_cents = None

    neg_tag = " [NEG-RISK]" if neg_risk else ""
    edge_tag = f" edge={edge_cents:.2f}c" if edge_cents is not None else ""
    total_tag = f" sum={total:.4f}" if total is not None else ""
    
    print(f"  [{hours:.1f}h] {q}{neg_tag}")
    print(f"         cid={cid}... outcomes={outcomes} prices={prices}{total_tag}{edge_tag}")
    if neg_risk_id:
        print(f"         neg_risk_group={neg_risk_id[:20]}...")
    print()
