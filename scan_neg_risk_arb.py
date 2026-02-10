"""Scan for multi-outcome arbitrage: buy all YES tokens in a group for < $1."""
import requests
import json
import time

r = requests.get(
    "https://gamma-api.polymarket.com/markets",
    params={"limit": 500, "active": True, "closed": False},
)
markets = r.json()
print(f"Fetched {len(markets)} markets")

# Group negRisk markets by negRiskMarketID
groups: dict[str, list] = {}
for m in markets:
    nrid = m.get("negRiskMarketID")
    if nrid:
        if nrid not in groups:
            groups[nrid] = []
        groups[nrid].append(m)

# Only groups with 2+ brackets
groups = {k: v for k, v in groups.items() if len(v) >= 2}
print(f"Found {len(groups)} multi-outcome groups with 2+ brackets\n")

for gid, ms in sorted(groups.items(), key=lambda x: -len(x[1])):
    q0 = ms[0].get("question", "")[:50]
    print(f"Group ({len(ms)} brackets): {q0}...")
    
    total_yes_mid = 0.0
    total_yes_best_ask = 0.0
    all_ok = True
    bracket_details = []
    
    for m in ms:
        tids = json.loads(m.get("clobTokenIds", "[]"))
        prices = json.loads(m.get("outcomePrices", "[]"))
        title = m.get("groupItemTitle", m.get("question", "")[:30])
        yes_mid = float(prices[0]) if prices else 0
        total_yes_mid += yes_mid
        
        # Fetch CLOB best ask for YES token
        if tids:
            try:
                br = requests.get(
                    "https://clob.polymarket.com/book",
                    params={"token_id": tids[0]},
                    timeout=5,
                )
                book = br.json()
                book_asks = book.get("asks", [])
                # Best ask = last element (sorted descending)
                best_ask = float(book_asks[-1]["price"]) if book_asks else None
                if best_ask is not None:
                    total_yes_best_ask += best_ask
                    bracket_details.append(f"  {title}: mid={yes_mid:.4f} ask={best_ask:.4f}")
                else:
                    all_ok = False
                    bracket_details.append(f"  {title}: mid={yes_mid:.4f} ask=N/A")
            except Exception as e:
                all_ok = False
                bracket_details.append(f"  {title}: mid={yes_mid:.4f} ask=ERR")
        else:
            all_ok = False
            bracket_details.append(f"  {title}: mid={yes_mid:.4f} no_token")
        
        time.sleep(0.05)  # Rate limit
    
    for d in bracket_details:
        print(d)
    
    print(f"  SUM(YES mid) = {total_yes_mid:.4f} (should be ~1.0)")
    
    if all_ok and total_yes_best_ask > 0:
        raw_edge = (1.0 - total_yes_best_ask) * 100
        # Fee: 2% taker on each leg
        fee_cents = total_yes_best_ask * 2.0
        net_edge = raw_edge - fee_cents
        marker = " *** OPPORTUNITY ***" if net_edge > 0 else ""
        print(f"  SUM(YES ask) = {total_yes_best_ask:.4f}  raw={raw_edge:+.2f}c  net={net_edge:+.2f}c{marker}")
    print()
