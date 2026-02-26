"""Check what end_date the Gamma API returns for the markets in our neg-risk group."""
import requests

gid = "0xa69729ae3d9838ec5754e0f74bf57dedd5ddbecd9e31b15a04f48f081168ba00"
r = requests.get(
    f"https://gamma-api.polymarket.com/markets?neg_risk_market_id={gid}&limit=5",
    timeout=10,
)
if r.ok and r.json():
    m = r.json()[0]
    print("endDateIso:", m.get("endDateIso"))
    print("end_date_iso:", m.get("end_date_iso"))
    print("endDate:", m.get("endDate"))
    print("active:", m.get("active"))
    print("closed:", m.get("closed"))
    print("neg_risk:", m.get("neg_risk"))

# Also check: how many markets in the scanner's 500 have end_date in the past?
from datetime import datetime, timezone
r2 = requests.get(
    "https://gamma-api.polymarket.com/markets",
    params={"active": "true", "closed": "false", "limit": "500",
            "order": "volume24hr", "ascending": "false"},
    timeout=15,
)
past = 0
future_12h = 0
no_date = 0
if r2.ok:
    for m in r2.json():
        ed = m.get("endDateIso") or m.get("end_date_iso")
        if not ed:
            no_date += 1
            continue
        try:
            dt = datetime.fromisoformat(ed.replace("Z", "+00:00"))
            hrs = (dt - datetime.now(timezone.utc)).total_seconds() / 3600
            if hrs < 0:
                past += 1
            elif hrs <= 12:
                future_12h += 1
        except Exception:
            no_date += 1

print(f"\nOf 500 high-volume markets:")
print(f"  Past end_date (already expired): {past}")
print(f"  Within next 12h: {future_12h}")
print(f"  No parseable date: {no_date}")
