"""Check Polymarket APIs for address info."""
import httpx, json

eoa = "0xa42bdDa7407C84eE508701EcE276CCD0Ca59ac9C"
proxy = "0x0df18f2e85aa500635ec19504f3713fdbe0754cc"

endpoints = [
    ("gamma portfolio (proxy)", f"https://gamma-api.polymarket.com/portfolio?user={proxy}"),
    ("gamma portfolio (eoa)", f"https://gamma-api.polymarket.com/portfolio?user={eoa}"),
    ("data profiles (proxy)", f"https://data-api.polymarket.com/profiles/{proxy}"),
    ("data profiles (eoa)", f"https://data-api.polymarket.com/profiles/{eoa}"),
]

for label, url in endpoints:
    try:
        resp = httpx.get(url, timeout=10)
        body = resp.text[:300]
        print(f"[{resp.status_code}] {label}: {body}")
    except Exception as e:
        print(f"[ERR] {label}: {e}")
    print()

# Also check on-chain USDC balance of the proxy wallet
from eth_utils import keccak
from eth_abi import encode, decode

rpc = "https://polygon.drpc.org"
usdc = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# balanceOf(address) selector
sel = keccak(b"balanceOf(address)")[:4].hex()
for label, addr in [("proxy", proxy), ("eoa", eoa)]:
    calldata = "0x" + sel + encode(["address"], [addr]).hex()
    payload = {"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": usdc, "data": calldata}, "latest"], "id": 1}
    try:
        resp = httpx.post(rpc, json=payload, timeout=15)
        result = resp.json().get("result", "0x0")
        balance = int(result, 16) if result != "0x" else 0
        print(f"USDC on-chain balance ({label} {addr[:10]}...): {balance / 1e6:.6f} USDC")
    except Exception as e:
        print(f"USDC balance error ({label}): {e}")

# Check MATIC balance too
for label, addr in [("proxy", proxy), ("eoa", eoa)]:
    payload = {"jsonrpc": "2.0", "method": "eth_getBalance", "params": [addr, "latest"], "id": 1}
    try:
        resp = httpx.post(rpc, json=payload, timeout=15)
        result = resp.json().get("result", "0x0")
        balance = int(result, 16)
        print(f"MATIC balance ({label} {addr[:10]}...): {balance / 1e18:.6f} MATIC")
    except Exception as e:
        print(f"MATIC balance error ({label}): {e}")
