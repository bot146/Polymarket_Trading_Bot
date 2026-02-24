"""Check what proxy wallet the CLOB server associates with our key."""
import os, json
from dotenv import load_dotenv
load_dotenv()

import httpx
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

pk = os.getenv("POLY_PRIVATE_KEY")
funder_env = os.getenv("POLY_FUNDER_ADDRESS")
acct = Account.from_key(pk)

print(f"EOA from current PK: {acct.address}")
print(f"Funder in .env:      {funder_env}")
print()

# The derive_api_key endpoint tells the server about our EOA
# The server should know which proxy wallet is associated
client = ClobClient("https://clob.polymarket.com", key=pk, chain_id=POLYGON, signature_type=1, funder=funder_env)

# Check the derive endpoint response - it may contain address info
try:
    creds = client.create_or_derive_api_creds()
    print(f"API key derived successfully: {creds.api_key[:20]}...")
except Exception as e:
    print(f"Derive failed: {e}")

# Query open orders / trades for the proxy address to see if there's history
try:
    resp = httpx.get(
        "https://clob.polymarket.com/trades",
        params={"maker_address": funder_env},
        timeout=15,
    )
    print(f"Trades for proxy: status={resp.status_code} body={resp.text[:200]}")
except Exception as e:
    print(f"Trades query: {e}")

# Also check the data-api for activity
try:
    resp = httpx.get(
        f"https://data-api.polymarket.com/activity?address={funder_env}&limit=5",
        timeout=15,
    )
    print(f"Activity for proxy: status={resp.status_code} body={resp.text[:300]}")
except Exception as e:
    print(f"Activity: {e}")

# Check Polymarket profile/positions endpoint 
try:
    resp = httpx.get(
        f"https://data-api.polymarket.com/positions?address={funder_env}&limit=5",
        timeout=15,
    )
    print(f"Positions for proxy: status={resp.status_code} body={resp.text[:300]}")
except Exception as e:
    print(f"Positions: {e}")

print()
print("=== TWO OPTIONS TO FIX ===")
print("Option A: Re-export key from Magic and update POLY_PRIVATE_KEY in .env")
print("          (If Magic rotated the key, the new one should match the proxy wallet)")
print()
print("Option B: Use signature_type=0 (EOA mode, no proxy)")
print(f"          Deposit USDC to your EOA: {acct.address}")
print("          Set POLY_SIGNATURE_TYPE=0 and remove POLY_FUNDER_ADDRESS from .env")
print("          (This bypasses the proxy wallet entirely)")
