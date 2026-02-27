import requests
resp = requests.get("https://gamma-api.polymarket.com/markets", params={"closed":"false","active":"true","limit":500})
ms = resp.json()
has_iso = sum(1 for m in ms if m.get("endDateIso"))
has_end = sum(1 for m in ms if m.get("endDate"))
has_both = sum(1 for m in ms if m.get("endDateIso") and m.get("endDate"))
has_neither = sum(1 for m in ms if not m.get("endDateIso") and not m.get("endDate"))
print(f"Total: {len(ms)}")
print(f"endDateIso: {has_iso}")
print(f"endDate: {has_end}")
print(f"Both: {has_both}")  
print(f"Neither: {has_neither}")
for m in ms:
    if m.get("endDate") and not m.get("endDateIso"):
        q = m.get("question", "?")[:50]
        print(f"Example missing endDateIso: {q}, endDate={m['endDate']}")
        break
