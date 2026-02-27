"""Find 5-min crypto markets currently ACTIVE and trading, with order book analysis."""
import requests, json
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
now = datetime.now(timezone.utc)
print(f"Current UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}")

# Find markets currently being traded (eventStartTime already passed or imminent)
resp = requests.get(f"{GAMMA}/markets", params={
    "limit": 500,
    "active": "true",
    "closed": "false",
    "order": "createdAt",
    "ascending": "false",
})
all_active = resp.json()

# Find all 5-min crypto "up or down" markets
five_min = [m for m in all_active if "up or down" in m.get("question", "").lower()]
print(f"Total active 'Up or Down' markets: {len(five_min)}")

# Separate into: future (tomorrow's window), and current/imminent
imminent = []
for m in five_min:
    end_str = m.get("endDate")
    event_start = m.get("eventStartTime")
    if not end_str or not event_start:
        continue
    try:
        edt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        est = datetime.fromisoformat(event_start.replace("Z", "+00:00"))
    except:
        continue
    
    hours_to_end = (edt - now).total_seconds() / 3600
    hours_to_start = (est - now).total_seconds() / 3600
    
    m["_hours_to_end"] = hours_to_end
    m["_hours_to_start"] = hours_to_start
    m["_end_dt"] = edt
    m["_start_dt"] = est
    
    # Market is "live" if event start is in the past (or within next 5 min)
    if hours_to_start < 0.1:  # started or about to start  
        imminent.append(m)

imminent.sort(key=lambda x: x["_hours_to_end"])
print(f"Currently live/imminent markets: {len(imminent)}")

# Show the ones resolving soonest
print("\n=== LIVE MARKETS (resolving soonest) ===")
for m in imminent[:20]:
    q = m.get("question", "?")[:65]
    hours_to_end = m["_hours_to_end"]
    mins_to_end = hours_to_end * 60
    vol = float(m.get("volume", 0))
    liq = float(m.get("liquidity", 0))
    prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
    outcomes = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
    
    time_label = f"{mins_to_end:.1f}min" if mins_to_end < 60 else f"{hours_to_end:.1f}h"
    
    print(f"\n  [{time_label:>8s}] {q}")
    print(f"    vol=${vol:,.0f} liq=${liq:,.0f} prices={prices} outcomes={outcomes}")
    
    # Check CLOB order book
    token_ids = json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds", [])
    if len(token_ids) >= 2 and len(outcomes) >= 2:
        for i, tid in enumerate(token_ids[:2]):
            try:
                r = requests.get(f"{CLOB}/book", params={"token_id": tid})
                book = r.json()
                asks = book.get("asks", [])
                bids = book.get("bids", [])
                best_ask = asks[0] if asks else None
                best_bid = bids[-1] if bids else None
                total_ask_depth = sum(float(a.get("size", 0)) for a in asks[:5])
                total_bid_depth = sum(float(b.get("size", 0)) for b in bids[:5])
                
                ask_str = f"ask={best_ask['price']}×{best_ask['size']}" if best_ask else "no asks"
                bid_str = f"bid={best_bid['price']}×{best_bid['size']}" if best_bid else "no bids"
                
                print(f"    {outcomes[i]:>5s}: {bid_str} | {ask_str} (depth: bid${total_bid_depth:.0f} ask${total_ask_depth:.0f})")
            except Exception as e:
                print(f"    {outcomes[i] if i < len(outcomes) else '?'}: CLOB error: {e}")

# Summary stats
print("\n\n=== SUMMARY ===")
btc_live = [m for m in imminent if "bitcoin" in m.get("question", "").lower()]
eth_live = [m for m in imminent if "ethereum" in m.get("question", "").lower()]
sol_live = [m for m in imminent if "solana" in m.get("question", "").lower()]
xrp_live = [m for m in imminent if "xrp" in m.get("question", "").lower()]
print(f"Live Bitcoin: {len(btc_live)}")
print(f"Live Ethereum: {len(eth_live)}")
print(f"Live Solana: {len(sol_live)}")
print(f"Live XRP: {len(xrp_live)}")
print(f"\nThese markets resolve with ~22-24 second delay after window end.")
print(f"New markets created every 5 minutes, 24h series volume: ~$14M")
print(f"\nKey issue: Individual market volume = $0 at creation → fails MIN_MARKET_VOLUME=$1000 filter")
