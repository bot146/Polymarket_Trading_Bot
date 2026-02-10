"""Quick scan for multi-outcome arbitrage opportunities."""
import requests
import json

# Find multi-outcome group markets
r = requests.get(
    "https://gamma-api.polymarket.com/markets",
    params={"limit": 500, "active": True, "closed": False},
)
markets = r.json()

# Group negRisk markets by their shared slug pattern
groups: dict[str, list] = {}
for m in markets:
    neg_risk = m.get("negRisk", False)
    if not neg_risk:
        continue
    # Group by common slug prefix or group slug
    slug = m.get("slug", "")
    # Polymarket slugs for grouped markets share a prefix
    # e.g. "will-trump-deport-less-than-250000", "will-trump-deport-250000-500000"
    group_key = slug.rsplit("-", 1)[0] if slug else m.get("conditionId", "")[:20]
    # Better: use the first meaningful words
    q = m.get("question", "")
    # Try to group by removing the numeric/bracket part
    # For now, just group by the first 40 chars
    group_key = q[:40]
    if group_key not in groups:
        groups[group_key] = []
    groups[group_key].append(m)

# Only keep groups with 2+ brackets
groups = {k: v for k, v in groups.items() if len(v) >= 2}

print(f"Found {len(groups)} multi-outcome groups with 2+ brackets")

# For each group, check if buying all YES tokens sums < 1.0
for gid, ms in sorted(groups.items(), key=lambda x: -len(x[1]))[:10]:
    q0 = ms[0].get("question", "")[:60]
    print(f"\nGroup ({len(ms)} brackets): {q0}...")
    total_yes_mid = 0
    total_yes_ask = 0
    all_asks_found = True
    
    for m in ms:
        tids = json.loads(m.get("clobTokenIds", "[]"))
        outcomes = json.loads(m.get("outcomes", "[]"))
        prices = json.loads(m.get("outcomePrices", "[]"))
        q = m.get("question", "")[:50]
        yes_price = float(prices[0]) if prices else 0
        total_yes_mid += yes_price
        
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
                best_ask = float(book_asks[-1]["price"]) if book_asks else None
                if best_ask:
                    total_yes_ask += best_ask
                    print(f"  {q}: mid={yes_price:.4f} ask={best_ask:.4f}")
                else:
                    all_asks_found = False
                    print(f"  {q}: mid={yes_price:.4f} ask=N/A")
            except Exception as e:
                all_asks_found = False
                print(f"  {q}: mid={yes_price:.4f} ask=ERROR")
    
    print(f"  => SUM(YES mid) = {total_yes_mid:.4f}")
    if all_asks_found:
        raw_edge = (1.0 - total_yes_ask) * 100
        fee = total_yes_ask * 2  # 2% taker fee in cents
        net_edge = raw_edge - fee
        marker = " *** OPPORTUNITY ***" if net_edge > 0 else ""
        print(f"  => SUM(YES ask) = {total_yes_ask:.4f}  raw_edge={raw_edge:+.2f}c  net_edge={net_edge:+.2f}c{marker}")


# Also check binary markets — just the top opportunities
print("\n\n=== BINARY YES+NO ARB SCAN ===")
binary = [m for m in markets if not m.get("negRisk", False)]
print(f"Checking {len(binary)} binary markets...")

opps = []
for m in binary:
    tids = json.loads(m.get("clobTokenIds", "[]"))
    outcomes = json.loads(m.get("outcomes", "[]"))
    if len(tids) != 2:
        continue
    
    asks = []
    for tid in tids:
        try:
            br = requests.get(
                "https://clob.polymarket.com/book",
                params={"token_id": tid},
                timeout=5,
            )
            book = br.json()
            book_asks = book.get("asks", [])
            best_ask = float(book_asks[-1]["price"]) if book_asks else None
            asks.append(best_ask)
        except:
            asks.append(None)
    
    if all(a is not None for a in asks):
        combined = sum(asks)
        raw_edge = (1.0 - combined) * 100
        net_edge = raw_edge - (combined * 2)
        opps.append((net_edge, raw_edge, combined, asks, m.get("question", "")[:60]))

opps.sort(key=lambda x: -x[0])
print(f"\nTop 10 binary arb (by net edge):")
for net, raw, comb, asks, q in opps[:10]:
    marker = " *** PROFIT ***" if net > 0 else ""
    print(f"  net={net:+.2f}c raw={raw:+.2f}c  YES={asks[0]:.4f} NO={asks[1]:.4f} sum={comb:.4f}  {q}{marker}")
