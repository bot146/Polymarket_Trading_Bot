"""Check proxy wallet tx history via Etherscan V2."""
import httpx, json

proxy = "0x0df18f2e85aa500635ec19504f3713fdbe0754cc"
eoa = "0xa42bdDa7407C84eE508701EcE276CCD0Ca59ac9C"

# Etherscan V2
url = f"https://api.etherscan.io/v2/api?chainid=137&module=account&action=txlist&address={proxy}&startblock=0&endblock=99999999&sort=asc&page=1&offset=10"
resp = httpx.get(url, timeout=15)
data = resp.json()
print("Status:", data.get("status"), "Message:", data.get("message"))
results = data.get("result", [])
if isinstance(results, list):
    print(f"Found {len(results)} txns:")
    for tx in results[:10]:
        fr = tx.get("from", "?")
        to = tx.get("to", "?")
        mid = tx.get("methodId", "?")
        h = tx.get("hash", "?")[:20]
        print(f"  from={fr} to={to} method={mid} hash={h}...")
else:
    print(f"Result: {str(results)[:300]}")

# Also check internal txns (contract creation)
print()
url2 = f"https://api.etherscan.io/v2/api?chainid=137&module=account&action=txlistinternal&address={proxy}&startblock=0&endblock=99999999&sort=asc&page=1&offset=10"
resp2 = httpx.get(url2, timeout=15)
data2 = resp2.json()
results2 = data2.get("result", [])
if isinstance(results2, list):
    print(f"Found {len(results2)} internal txns:")
    for tx in results2[:10]:
        fr = tx.get("from", "?")
        to = tx.get("to", "?")
        typ = tx.get("type", "?")
        h = tx.get("hash", "?")[:20]
        print(f"  from={fr} to={to} type={typ} hash={h}...")
else:
    print(f"Internal result: {str(results2)[:300]}")
