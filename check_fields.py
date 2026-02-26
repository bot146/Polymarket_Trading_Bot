import requests
r = requests.get(
    "https://gamma-api.polymarket.com/markets",
    params={"active": "true", "closed": "false", "limit": "2",
            "order": "volume24hr", "ascending": "false"},
    timeout=10,
)
if r.ok and r.json():
    m = r.json()[0]
    date_keys = [k for k in m.keys() if "date" in k.lower() or "end" in k.lower() or "resolut" in k.lower()]
    for k in date_keys:
        print(f"{k}: {m[k]}")
    print("---")
    print("All keys:", sorted(m.keys()))
