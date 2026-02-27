"""Deep search for short-duration Bitcoin/crypto markets on Polymarket."""
import requests, json
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"

# Strategy 1: Full text search via Gamma
print("=== Text search for 'bitcoin' in questions ===")
resp = requests.get(f"{GAMMA}/markets", params={
    "limit": 500,
    "active": "true",
    "closed": "false",
})
all_active = resp.json()
btc_markets = [m for m in all_active if "bitcoin" in m.get("question", "").lower() or "btc" in m.get("question", "").lower()]
print(f"Active markets with 'bitcoin'/'btc': {len(btc_markets)}")

now = datetime.now(timezone.utc)
for m in sorted(btc_markets, key=lambda x: x.get("endDateIso", "9999")):
    q = m.get("question", "?")[:70]
    end = m.get("endDateIso") or m.get("endDate", "?")
    vol = float(m.get("volume", 0))
    neg = " [NR]" if m.get("negRisk") else ""
    try:
        if "T" in str(end):
            edt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        else:
            edt = datetime.fromisoformat(str(end) + "T00:00:00+00:00")
        hours = (edt - now).total_seconds() / 3600
        time_str = f"{hours:.1f}h"
    except:
        time_str = "?"
    print(f"  [{time_str:>8s}] ${vol:>12,.0f} | {q}{neg}")

# Strategy 2: Search for very recent events/markets
print("\n=== Markets created recently (might be short-lived) ===")
resp2 = requests.get(f"{GAMMA}/markets", params={
    "limit": 100,
    "active": "true",
    "closed": "false",
    "order": "createdAt",
    "ascending": "false",
})
recent = resp2.json()
print(f"Most recently created active markets: {len(recent)}")
for m in recent[:20]:
    q = m.get("question", "?")[:60]
    end = m.get("endDateIso") or m.get("endDate", "?")
    created = m.get("createdAt", "?")[:19]
    vol = float(m.get("volume", 0))
    neg = " [NR]" if m.get("negRisk") else ""
    print(f"  created={created} end={end} ${vol:>10,.0f} | {q}{neg}")

# Strategy 3: Look for markets resolving between now and now+6h
# by searching closed=false AND endDate range
print("\n=== Markets within 6h via filtering ===")
from datetime import timedelta
end_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
end_max = (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
print(f"Searching endDate between {end_min} and {end_max}")

# Try various Gamma API query params
for params in [
    {"limit": 100, "end_date_min": end_min, "end_date_max": end_max},
    {"limit": 100, "endDate_min": end_min, "endDate_max": end_max},
    {"limit": 100, "startDateMin": end_min, "startDateMax": end_max},  
]:
    try:
        resp3 = requests.get(f"{GAMMA}/markets", params=params)
        ms3 = resp3.json()
        pkeys = list(params.keys())
        print(f"  Params {pkeys}: {len(ms3)} results")
        if ms3 and len(ms3) < 20:
            for m in ms3[:5]:
                print(f"    {m.get('question', '?')[:60]} end={m.get('endDateIso','?')}")
    except Exception as e:
        print(f"  Failed: {e}")

# Strategy 4: Look at CLOB API for short-term market listings
print("\n=== CLOB API: sampling markets ===")
try:
    resp4 = requests.get("https://clob.polymarket.com/markets", params={"limit": 10, "next_cursor": "MA=="})
    clob_ms = resp4.json()
    if isinstance(clob_ms, dict):
        print(f"Keys: {list(clob_ms.keys())}")
        data = clob_ms.get("data", clob_ms.get("markets", []))
        print(f"Data items: {len(data)}")
        for item in data[:5]:
            if isinstance(item, dict):
                q = item.get("question", "?")[:60]
                end = item.get("end_date_iso", item.get("endDateIso", "?"))
                game = item.get("game_start_time", "?")
                print(f"  {q} end={end} game={game}")
    else:
        print(f"Response type: {type(clob_ms)}, len={len(clob_ms)}")
except Exception as e:
    print(f"CLOB markets failed: {e}")

# Strategy 5: Check for "event" type markets that might be recurring/rapid
print("\n=== Search for rapid/recurring market patterns ===")
patterns = ["5 minute", "1 minute", "hourly", "next hour", "next 5", "price at"]
for pat in patterns:
    matches = [m for m in all_active if pat in m.get("question", "").lower()]
    if matches:
        print(f"  '{pat}': {len(matches)} matches")
        for m in matches[:3]:
            print(f"    {m.get('question', '?')[:70]}")
    else:
        print(f"  '{pat}': 0 matches")
