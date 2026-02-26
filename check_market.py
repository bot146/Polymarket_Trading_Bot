"""Quick check: is our held market resolved yet?"""
import requests

cid = "0x49686d26fb712515cd5e12c23f0a1c7e10214c7faa3cb0a730aabe0c33694082"
r = requests.get(f"https://gamma-api.polymarket.com/markets?condition_id={cid}", timeout=10)
if r.ok and r.json():
    m = r.json()[0]
    print(f"Question:    {m.get('question', '?')[:80]}")
    print(f"End date:    {m.get('endDate', m.get('end_date_iso', '?'))}")
    print(f"Closed:      {m.get('closed', '?')}")
    print(f"Resolved:    {m.get('resolved', '?')}")
    print(f"Active:      {m.get('active', '?')}")
    print(f"Outcome:     {m.get('outcome', '?')}")
    print(f"Best bid:    {m.get('bestBid', '?')}")
    print(f"Best ask:    {m.get('bestAsk', '?')}")
else:
    print(f"No data for {cid[:16]}...")

# Also check: what markets within 12h are available?
print("\n--- Markets resolving within 12h ---")
from datetime import datetime, timezone, timedelta
cutoff = datetime.now(timezone.utc) + timedelta(hours=12)
r2 = requests.get(
    "https://gamma-api.polymarket.com/markets",
    params={"closed": "false", "active": "true", "limit": "500", "order": "volume24hr", "ascending": "false"},
    timeout=15,
)
if r2.ok:
    count = 0
    for m in r2.json():
        end = m.get("endDate") or m.get("end_date_iso")
        if not end:
            continue
        try:
            ed = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except Exception:
            continue
        if ed <= cutoff:
            hrs = (ed - datetime.now(timezone.utc)).total_seconds() / 3600
            resolved = m.get("resolved", False)
            q = m.get("question", "?")[:60]
            print(f"  [{hrs:+.1f}h] resolved={resolved} {q}")
            count += 1
    print(f"\nTotal: {count} markets within 12h window")
