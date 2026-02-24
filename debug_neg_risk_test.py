"""
Test order posting with both neg-risk and non-neg-risk tokens
to isolate whether the issue is domain-separator related.
"""
import os, sys, json, traceback
from dotenv import load_dotenv
load_dotenv()

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from py_clob_client.constants import POLYGON

HOST = "https://clob.polymarket.com"
PK = os.getenv("POLY_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
FUNDER = os.getenv("POLY_FUNDER_ADDRESS") or os.getenv("POLYMARKET_PROXY_ADDRESS")

print(f"PK length: {len(PK) if PK else 'NONE'}")
print(f"Funder: {FUNDER}")

# Build client
client = ClobClient(HOST, key=PK, chain_id=POLYGON, signature_type=1, funder=FUNDER)
api_creds = client.create_or_derive_api_creds()
client.set_api_creds(api_creds)
print(f"API key: {api_creds.api_key[:16]}...")
print(f"Signer (EOA): {client.get_address()}")
print(f"Builder funder: {client.builder.funder}")

# Verify L2 auth
orders = client.get_orders()
print(f"L2 auth OK - open orders: {len(orders)}")

# ---- STEP 1: Find a NON-neg-risk token ----
print("\n=== Finding NON-neg-risk token ===")
import httpx
resp = httpx.get("https://gamma-api.polymarket.com/markets?limit=50&active=true&closed=false", timeout=30)
markets = resp.json()

non_neg_token = None
neg_token = None

for m in markets:
    raw_tokens = m.get("clobTokenIds", "")
    if not raw_tokens:
        continue
    # Parse the JSON array string
    try:
        token_list = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
    except:
        continue
    if not token_list:
        continue
    token_id = token_list[0].strip()
    if not token_id or len(token_id) < 10:
        continue
    
    try:
        nr = client.get_neg_risk(token_id)
        tick = client.get_tick_size(token_id)
    except Exception as ex:
        print(f"  skip {token_id[:20]}... ({ex})")
        continue
    
    price = float(tick)  # use minimum price
    q = m.get("question", "?")[:50]
    
    if not nr and non_neg_token is None:
        non_neg_token = {"token_id": token_id, "neg_risk": False, "tick_size": tick, "price": price, "question": q}
        print(f"  Found non-neg-risk: {q}")
    if nr and neg_token is None:
        neg_token = {"token_id": token_id, "neg_risk": True, "tick_size": tick, "price": price, "question": q}
        print(f"  Found neg-risk: {q}")
    
    if non_neg_token and neg_token:
        break

print(f"Non-neg-risk: {non_neg_token}")
print(f"Neg-risk: {neg_token}")

# ---- STEP 2: Try posting orders for each ----
def try_post_order(label, token_info):
    if not token_info:
        print(f"\n=== {label}: SKIPPED (no token found) ===")
        return
    
    print(f"\n=== {label}: {token_info['question'][:60]} ===")
    print(f"  token_id: {token_info['token_id'][:20]}...")
    print(f"  neg_risk: {token_info['neg_risk']}")
    print(f"  tick_size: {token_info['tick_size']}")
    print(f"  price: {token_info['price']}")
    
    tick = token_info["tick_size"]
    # Use minimum price for the tick size
    price = float(tick)
    size = 10.0  # small
    
    print(f"  Order: BUY {size} @ {price} (cost ~${price * size:.4f})")
    
    order_args = OrderArgs(
        token_id=token_info["token_id"],
        price=price,
        size=size,
        side=BUY,
    )
    
    try:
        signed_order = client.create_order(order_args)
        d = signed_order.dict()
        print(f"  Signed order:")
        print(f"    maker: {d['maker']}")
        print(f"    signer: {d['signer']}")
        print(f"    signatureType: {d['signatureType']}")
        print(f"    makerAmount: {d['makerAmount']}")
        print(f"    takerAmount: {d['takerAmount']}")
        print(f"    salt: {d['salt']} (type: {type(d['salt']).__name__})")
        print(f"    signature: {d['signature'][:20]}...")
        
        resp = client.post_order(signed_order, orderType=OrderType.GTC)
        print(f"  POST RESULT: {resp}")
        return True
    except Exception as e:
        print(f"  ERROR: {e}")
        # Try to get more details
        tb = traceback.format_exc()
        if "400" in tb or "invalid" in tb.lower():
            print(f"  Full traceback: {tb[-500:]}")
        return False

# Test non-neg-risk first
r1 = try_post_order("NON-NEG-RISK", non_neg_token)
r2 = try_post_order("NEG-RISK", neg_token)

print(f"\n=== SUMMARY ===")
print(f"Non-neg-risk: {'SUCCESS' if r1 else 'FAILED'}")
print(f"Neg-risk: {'SUCCESS' if r2 else 'FAILED'}")
