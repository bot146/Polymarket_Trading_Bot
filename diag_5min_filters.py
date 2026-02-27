"""Why scanner misses 5-min markets + resolution delay analysis."""
import requests, json
from datetime import datetime, timezone, timedelta

GAMMA = "https://gamma-api.polymarket.com"
now = datetime.now(timezone.utc)
print(f"Current UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}")

# 1. Check if they pass our volume filter (min_volume=1000)
print("\n=== FILTER ANALYSIS: Why 5-min markets get excluded ===")
resp = requests.get(f"{GAMMA}/markets", params={
    "limit": 500,
    "active": "true",
    "closed": "false",
    "order": "createdAt",
    "ascending": "false",
})
all_active = resp.json()

five_min_btc = [m for m in all_active if "bitcoin" in m.get("question", "").lower() and "up or down" in m.get("question", "").lower()]
print(f"Active 5-min Bitcoin markets: {len(five_min_btc)}")

for m in five_min_btc[:5]:
    q = m.get("question", "?")[:60]
    vol = float(m.get("volume", 0))
    liq = float(m.get("liquidity", 0))
    end_iso = m.get("endDateIso", "?")
    end_full = m.get("endDate", "?")
    
    # Check each filter:
    pass_volume = vol >= 1000
    pass_enddate_iso = end_iso is not None
    
    # The scanner uses endDateIso for end_date → parse
    try:
        if "T" in str(end_iso):
            edt = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
        else:
            edt = datetime.fromisoformat(str(end_iso) + "T00:00:00+00:00")
        hours_iso = (edt - now).total_seconds() / 3600
    except:
        hours_iso = None
    
    # The endDate field has precise time
    try:
        edt_full = datetime.fromisoformat(str(end_full).replace("Z", "+00:00"))
        hours_full = (edt_full - now).total_seconds() / 3600
    except:
        hours_full = None
    
    pass_72h_iso = hours_iso is not None and 0 < hours_iso <= 72
    pass_72h_full = hours_full is not None and 0 < hours_full <= 72
    
    print(f"\n  {q}")
    print(f"    volume=${vol:.0f} → {'PASS' if pass_volume else 'FAIL (need $1000)'}")
    print(f"    endDateIso={end_iso} → hours={hours_iso:.1f}h → {'PASS' if pass_72h_iso else 'FAIL'} 72h window")
    print(f"    endDate={end_full} → hours={hours_full:.1f}h → {'PASS' if pass_72h_full else 'FAIL'} 72h window")

# 2. Look at the series data for volume info (series has 24h volume!)
print("\n\n=== SERIES DATA (aggregate stats) ===")
if five_min_btc:
    events = five_min_btc[0].get("events", [])
    if events:
        ev = events[0]
        series = ev.get("series", [])
        if series:
            s = series[0]
            print(f"  Series: {s.get('title', '?')}")
            print(f"  Recurrence: {s.get('recurrence', '?')}")
            print(f"  24h volume: ${float(s.get('volume24hr', 0)):,.0f}")
            print(f"  Total volume: ${float(s.get('volume', 0)):,.0f}")
            print(f"  Liquidity: ${float(s.get('liquidity', 0)):,.0f}")

# 3. Check resolution delay: how long after 5-min window ends does resolution happen?
print("\n\n=== RESOLUTION DELAY ANALYSIS ===")
resp2 = requests.get(f"{GAMMA}/markets", params={
    "limit": 500,
    "closed": "true",
    "order": "createdAt",
    "ascending": "false",
})
closed = resp2.json()
closed_5min_btc = [m for m in closed if "bitcoin" in m.get("question", "").lower() and "up or down" in m.get("question", "").lower()]
print(f"Closed 5-min Bitcoin markets: {len(closed_5min_btc)}")

for m in closed_5min_btc[:15]:
    q = m.get("question", "?")[:70]
    end_full = m.get("endDate", "?")
    closed_at = m.get("closedTime") or m.get("closedAt") or m.get("updatedAt", "?")
    created = m.get("createdAt", "?")[:19]
    vol = float(m.get("volume", 0))
    
    prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
    outcomes = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
    
    # Determine winner
    winner = "?"
    if len(prices) == 2:
        if prices[0] == "1":
            winner = outcomes[0] if outcomes else "Up"
        elif prices[1] == "1":
            winner = outcomes[1] if outcomes else "Down"
    
    # Calculate delay between endDate and closedTime
    delay_str = "?"
    try:
        edt_full = datetime.fromisoformat(str(end_full).replace("Z", "+00:00"))
        if closed_at != "?":
            cdt = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
            delay_secs = (cdt - edt_full).total_seconds()
            delay_str = f"{delay_secs:.0f}s"
    except:
        pass
    
    print(f"\n  {q}")
    print(f"    vol=${vol:,.0f} winner={winner} endDate={end_full}")
    print(f"    closedAt={closed_at}  delay={delay_str}")

# 4. Check eventStartTime vs endDate — does eventStartTime tell us the 5-min window?
print("\n\n=== EVENT TIMING for active markets ===")
for m in five_min_btc[:5]:
    q = m.get("question", "?")[:60]
    event_start = m.get("eventStartTime", "?")
    end = m.get("endDate", "?")
    accepting_orders_ts = m.get("acceptingOrdersTimestamp", "?")
    print(f"  {q}")
    print(f"    eventStartTime={event_start} endDate={end} acceptingOrders={accepting_orders_ts}")
