import requests, json
from datetime import datetime, timezone
GAMMA = "https://gamma-api.polymarket.com"
now = datetime.now(timezone.utc)

et_hour = (now.hour - 5) % 24
print(f"UTC now: {now.strftime('%H:%M')}  ({et_hour}:{now.strftime('%M')} ET)")

# Most recently created
resp = requests.get(f"{GAMMA}/markets", params={"limit": 20, "active": "true", "closed": "false", "order": "createdAt", "ascending": "false"})
recent = resp.json()
print(f"\nMost recently created active markets:")
for m in recent[:6]:
    q = m.get("question","")[:60]
    created = m.get("createdAt","")[:19]
    end = m.get("endDate","")
    event_start = m.get("eventStartTime","")
    liq = float(m.get("liquidity",0))
    print(f"  created={created} evt_start={event_start} end={end}")
    print(f"    liq=${liq:,.0f} | {q}")
    print()

# All active BTC up/down 
resp2 = requests.get(f"{GAMMA}/markets", params={"limit": 500, "active": "true", "closed": "false", "order": "endDate", "ascending": "true"})
ms = resp2.json()
btc = [m for m in ms if "bitcoin" in m.get("question","").lower() and "up or down" in m.get("question","").lower()]
print(f"\nAll active BTC 5-min: {len(btc)}, sorted by endDate")
print("Earliest 3:")
for m in btc[:3]:
    q = m.get("question","")[:55]
    end = m.get("endDate","")
    print(f"  {q} end={end}")
print("Latest 3:")
for m in btc[-3:]:
    q = m.get("question","")[:55]
    end = m.get("endDate","")
    print(f"  {q} end={end}")
