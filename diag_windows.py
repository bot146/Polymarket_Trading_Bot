"""Check how many markets exist at different time windows."""
import requests
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"

resp = requests.get(f"{GAMMA}/markets", params={
    "closed": "false",
    "active": "true",
    "limit": 500,
    "order": "volumeNum",
    "ascending": "false",
})
all_markets = resp.json()
now = datetime.now(timezone.utc)
print(f"Total active markets: {len(all_markets)}")
print(f"Current time (UTC): {now.strftime('%Y-%m-%d %H:%M')}\n")

# Count markets in various windows
windows = [6, 12, 24, 48, 72, 168, 720]  # hours
for window_h in windows:
    count = 0
    neg_risk_count = 0
    for m in all_markets:
        end_str = m.get("endDateIso") or m.get("endDate")
        if not end_str:
            continue
        try:
            if "T" in end_str:
                edt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            else:
                edt = datetime.fromisoformat(end_str + "T00:00:00+00:00")
        except Exception:
            continue
        hours = (edt - now).total_seconds() / 3600
        if 0 < hours <= window_h:
            count += 1
            if m.get("negRisk"):
                neg_risk_count += 1
    label = f"{window_h}h" if window_h < 24 else f"{window_h//24}d"
    print(f"  {label:>4s}: {count:3d} markets ({neg_risk_count} neg-risk)")

# Show first 10 closest markets
print("\nClosest non-expired markets:")
items = []
for m in all_markets:
    end_str = m.get("endDateIso") or m.get("endDate")
    if not end_str:
        continue
    try:
        if "T" in end_str:
            edt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        else:
            edt = datetime.fromisoformat(end_str + "T00:00:00+00:00")
    except Exception:
        continue
    hours = (edt - now).total_seconds() / 3600
    if hours > 0:
        items.append((hours, m))

items.sort(key=lambda x: x[0])
for hours, m in items[:15]:
    q = m.get("question", "?")[:65]
    neg = " [NR]" if m.get("negRisk") else ""
    vol = float(m.get("volume", 0))
    print(f"  [{hours:6.1f}h] ${vol:>12,.0f} | {q}{neg}")
