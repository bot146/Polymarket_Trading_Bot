"""Test signature_type=0 vs signature_type=1 to find which is correct."""
import os, sys
from dotenv import load_dotenv
load_dotenv()

from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import OrderArgs, OrderType

pk = os.getenv("POLY_PRIVATE_KEY")
funder = os.getenv("POLY_FUNDER_ADDRESS")
acct = Account.from_key(pk)

print(f"EOA (signer):   {acct.address}")
print(f"Funder (maker): {funder}")
print(f"Same address:   {acct.address.lower() == funder.lower()}")
print()

# Find a cheap token first using sig_type=1 client (for data access)
import httpx, json
resp = httpx.get("https://gamma-api.polymarket.com/markets?limit=50&active=true&closed=false", timeout=30)
markets = resp.json()

test_token = None
for m in markets:
    raw = m.get("clobTokenIds", "")
    try:
        tokens = json.loads(raw) if isinstance(raw, str) else raw
    except:
        continue
    if not tokens:
        continue
    test_token = tokens[0].strip()
    if test_token and len(test_token) > 10:
        break

if not test_token:
    print("ERROR: no token found")
    sys.exit(1)

print(f"Test token: {test_token[:30]}...")
print()

for sig_type_val, label, funder_val in [
    (0, "EOA (sig_type=0, no funder)", None),
    (1, "POLY_PROXY (sig_type=1, with funder)", funder),
    (2, "POLY_GNOSIS (sig_type=2, with funder)", funder),
]:
    print(f"=== {label} ===")
    try:
        kwargs = {"key": pk, "chain_id": POLYGON, "signature_type": sig_type_val}
        if funder_val:
            kwargs["funder"] = funder_val
        client = ClobClient("https://clob.polymarket.com", **kwargs)
        api_creds = client.create_or_derive_api_creds()
        client.set_api_creds(api_creds)
        
        print(f"  signer:  {client.get_address()}")
        print(f"  funder:  {client.builder.funder}")
        print(f"  sig_type: {client.builder.sig_type}")
        
        # Test L2 auth
        try:
            orders = client.get_orders()
            print(f"  L2 auth: OK ({len(orders)} open orders)")
        except Exception as e:
            err = str(e)[:80]
            print(f"  L2 auth: FAILED ({err})")
        
        # Test order posting
        try:
            tick = client.get_tick_size(test_token)
            neg_risk = client.get_neg_risk(test_token)
            price = float(tick)  # minimum price
            
            order_args = OrderArgs(
                token_id=test_token,
                price=price,
                size=10.0,
                side="BUY",
            )
            signed_order = client.create_order(order_args)
            d = signed_order.dict()
            print(f"  order maker:  {d.get('maker')}")
            print(f"  order signer: {d.get('signer')}")
            print(f"  order sigType: {d.get('signatureType')}")
            
            resp = client.post_order(signed_order, orderType=OrderType.GTC)
            print(f"  POST ORDER: SUCCESS! {resp}")
        except Exception as e:
            err = str(e)
            if "invalid signature" in err:
                print(f"  POST ORDER: invalid signature")
            elif "403" in err or "restricted" in err.lower():
                print(f"  POST ORDER: geo-restricted")
            else:
                print(f"  POST ORDER: {err[:100]}")
    except Exception as e:
        print(f"  SETUP ERROR: {str(e)[:100]}")
    print()

print("Done.")
