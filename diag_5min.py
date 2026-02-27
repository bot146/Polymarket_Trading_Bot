"""Investigate 5-minute Bitcoin markets on Polymarket."""
import requests, json
from datetime import datetime, timezone, timedelta

GAMMA = "https://gamma-api.polymarket.com"

# Search for Bitcoin 5-minute markets
print("=== Search 1: Bitcoin markets with keyword '5' ===")
resp = requests.get(f"{GAMMA}/markets", params={
    "limit": 100,
    "order": "endDate",
    "ascending": "true",
    "active": "true",
    "closed": "false",
})
ms = resp.json()
now = datetime.now(timezone.utc)
print(f"Fetched {len(ms)} markets ordered by soonest endDate\n")

# Show markets resolving very soon (within 1h)
soon = []
for m in ms:
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
    if hours > 0:
        soon.append((hours, m))

soon.sort(key=lambda x: x[0])
print(f"Markets with future end dates: {len(soon)}")
print(f"\nClosest 20 markets:")
for hours, m in soon[:20]:
    q = m.get("question", "?")[:70]
    vol = float(m.get("volume", 0))
    neg = " [NR]" if m.get("negRisk") else ""
    mins = hours * 60
    if mins < 60:
        time_str = f"{mins:.0f}min"
    else:
        time_str = f"{hours:.1f}h"
    print(f"  [{time_str:>7s}] ${vol:>12,.0f} | {q}{neg}")

# Also search specifically for "5 minute" or "bitcoin" keywords
print("\n=== Search 2: Keyword 'minute' in active markets ===")
resp2 = requests.get(f"{GAMMA}/markets", params={
    "limit": 500,
    "active": "true",
    "closed": "false",
})
ms2 = resp2.json()
minute_markets = [m for m in ms2 if "minute" in m.get("question", "").lower() or "5-min" in m.get("question", "").lower()]
print(f"Found {len(minute_markets)} markets with 'minute' or '5-min' in question")
for m in minute_markets[:10]:
    q = m.get("question", "?")[:80]
    end_str = m.get("endDateIso") or m.get("endDate")
    vol = float(m.get("volume", 0))
    print(f"  ${vol:>12,.0f} | {q} | end={end_str}")

# Search for very-short-lived markets that might already have passed
print("\n=== Search 3: All Bitcoin markets (including resolved/closed) ===")
resp3 = requests.get(f"{GAMMA}/markets", params={
    "limit": 50,
    "tag": "bitcoin",
    "order": "endDate",
    "ascending": "false",
})
ms3 = resp3.json()
print(f"Fetched {len(ms3)} Bitcoin-tagged markets")
for m in ms3[:10]:
    q = m.get("question", "?")[:70]
    end = m.get("endDateIso") or m.get("endDate", "?")
    active = m.get("active")
    closed = m.get("closed")
    resolved = m.get("resolved")
    vol = float(m.get("volume", 0))
    print(f"  ${vol:>12,.0f} | active={active} closed={closed} resolved={resolved} | end={end}")
    print(f"    {q}")

# Search events/groups
print("\n=== Search 4: Events search ===")
try:
    resp4 = requests.get(f"{GAMMA}/events", params={
        "limit": 20,
        "order": "endDate",
        "ascending": "true",
        "active": "true",
        "closed": "false",
    })
    events = resp4.json()
    print(f"Fetched {len(events)} events")
    for ev in events[:10]:
        title = ev.get("title", "?")[:70]
        end = ev.get("endDate", "?")
        n_markets = len(ev.get("markets", []))
        print(f"  {title} | end={end} | markets={n_markets}")
except Exception as e:
    print(f"Events query failed: {e}")

# Try slug-based search for bitcoin 5 minute
print("\n=== Search 5: Slug search for 5-minute ===")
try:
    resp5 = requests.get(f"{GAMMA}/markets", params={
        "limit": 50,
        "slug_contains": "5-minute",
    })
    ms5 = resp5.json()
    print(f"Slug '5-minute': {len(ms5)} results")
    for m in ms5[:5]:
        q = m.get("question", "?")[:80]
        print(f"  {q}")
except Exception as e:
    print(f"  Failed: {e}")

try:
    resp6 = requests.get(f"{GAMMA}/markets", params={
        "limit": 50,
        "slug_contains": "bitcoin-5",
    })
    ms6 = resp6.json()
    print(f"Slug 'bitcoin-5': {len(ms6)} results")
    for m in ms6[:5]:
        q = m.get("question", "?")[:80]
        print(f"  {q}")
except Exception as e:
    print(f"  Failed: {e}")
