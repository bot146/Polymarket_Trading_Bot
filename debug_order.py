"""Comprehensive diagnostic for the 'invalid signature' error.

This script steps through every stage of order creation and posting,
dumping all intermediate data so we can pinpoint where things diverge.
"""

from __future__ import annotations
import json, sys, os, logging
from decimal import Decimal

# ── Setup ──────────────────────────────────────────────────────────────
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "src")

from polymarket_bot.config import load_settings
from polymarket_bot.scanner import MarketScanner
from polymarket_bot.log_config import setup_logging

setup_logging("DEBUG")
settings = load_settings()

# ── 1. Build client ────────────────────────────────────────────────────
from py_clob_client.client import ClobClient
print("=" * 70)
print("STEP 1  –  Build ClobClient")
print("=" * 70)

client = ClobClient(
    settings.poly_host,
    key=settings.poly_private_key,
    chain_id=settings.poly_chain_id,
    signature_type=settings.poly_signature_type,
    funder=settings.poly_funder_address,
)
api_creds = client.create_or_derive_api_creds()
client.set_api_creds(api_creds)

print(f"  Host:           {client.host}")
print(f"  Chain ID:       {client.chain_id}")
print(f"  Signer (EOA):   {client.signer.address()}")
print(f"  Funder:         {settings.poly_funder_address}")
print(f"  Sig-type cfg:   {settings.poly_signature_type}")
print(f"  Builder funder: {client.builder.funder}")
print(f"  Builder sig_t:  {client.builder.sig_type}")
print(f"  API key:        {api_creds.api_key[:12]}...")
print(f"  builder_config: {client.builder_config}")

# Verify L2 auth
try:
    orders = client.get_orders()
    print(f"  L2 auth:        ✅ ({len(orders) if isinstance(orders, list) else '?'} open orders)")
except Exception as e:
    print(f"  L2 auth:        ❌ {e}")
    sys.exit(1)

# ── 2. Pick a token ───────────────────────────────────────────────────
print("\n" + "=" * 70)
print("STEP 2  –  Find a cheap token")
print("=" * 70)
scanner = MarketScanner()
markets = scanner.get_all_markets(limit=500, active_only=True)
print(f"  Fetched {len(markets)} markets")

token_info = None
for m in markets:
    if float(m.volume) < 5_000:
        continue
    for t in m.tokens:
        ask = float(t.price)
        if 0.02 <= ask <= 0.08 and t.token_id:
            if token_info is None or ask < token_info["ask"]:
                token_info = {
                    "token_id": t.token_id,
                    "ask": ask,
                    "outcome": t.outcome,
                    "question": m.question[:80],
                    "condition_id": m.condition_id,
                }

if token_info is None:
    print("  ❌ No cheap token found")
    sys.exit(1)

print(f"  Market:    {token_info['question']}")
print(f"  Outcome:   {token_info['outcome']}")
print(f"  Token ID:  {token_info['token_id'][:30]}...")
print(f"  Ask:       ${token_info['ask']}")

# ── 3. Check neg_risk and tick_size ───────────────────────────────────
print("\n" + "=" * 70)
print("STEP 3  –  Resolve neg_risk & tick_size")
print("=" * 70)

token_id = token_info["token_id"]

# Query neg_risk from CLOB
neg_risk = client.get_neg_risk(token_id)
print(f"  neg_risk (from CLOB): {neg_risk}")

# Get contract config for both modes
from py_clob_client.config import get_contract_config
config_normal = get_contract_config(137, neg_risk=False)
config_neg    = get_contract_config(137, neg_risk=True)
config_used   = get_contract_config(137, neg_risk=neg_risk)
print(f"  Exchange (normal):    {config_normal.exchange}")
print(f"  Exchange (neg_risk):  {config_neg.exchange}")
print(f"  Exchange (USED):      {config_used.exchange}")

# Resolve tick_size
tick_size = client._ClobClient__resolve_tick_size(token_id, None)
print(f"  tick_size:            {tick_size}")

# ── 4. Create the order ──────────────────────────────────────────────
print("\n" + "=" * 70)
print("STEP 4  –  Create & sign the order")
print("=" * 70)

from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

price = token_info["ask"]
size  = 10  # small

order_args = OrderArgs(
    price=price,
    size=float(size),
    side=BUY,
    token_id=token_id,
)

signed_order = client.create_order(order_args)
print(f"  Order created successfully")
print(f"  Type: {type(signed_order).__name__}")

# Dump the signed order's dict representation
order_dict = signed_order.dict() if hasattr(signed_order, 'dict') else signed_order
print(f"\n  Signed order fields:")
for k, v in order_dict.items():
    print(f"    {k}: {v}")

# ── 5. Verify signature recovery ─────────────────────────────────────
print("\n" + "=" * 70)
print("STEP 5  –  Verify EIP-712 signature (ecrecover)")
print("=" * 70)

try:
    from py_order_utils.model.order import Order
    from py_order_utils.signer import Signer as UtilsSigner
    from py_order_utils.builders.order_builder import OrderBuilder as UtilsOrderBuilder
    from py_order_utils.model.sides import BUY as SIDE_BUY, SELL as SIDE_SELL
    from eth_utils import to_checksum_address, keccak, to_int
    from eth_account import Account
    from poly_eip712_structs import make_domain

    # Rebuild the Order struct exactly as the lib does
    side_int = 0 if order_dict["side"] == "BUY" else 1
    sig_type_int = int(order_dict["signatureType"])

    order_struct = Order(
        salt=int(order_dict["salt"]),
        maker=to_checksum_address(order_dict["maker"]),
        signer=to_checksum_address(order_dict["signer"]),
        taker=to_checksum_address(order_dict["taker"]),
        tokenId=int(order_dict["tokenId"]),
        makerAmount=int(order_dict["makerAmount"]),
        takerAmount=int(order_dict["takerAmount"]),
        expiration=int(order_dict["expiration"]),
        nonce=int(order_dict["nonce"]),
        feeRateBps=int(order_dict["feeRateBps"]),
        side=side_int,
        signatureType=sig_type_int,
    )

    # Build domain separator with the SAME exchange address the lib used
    domain = make_domain(
        name="Polymarket CTF Exchange",
        version="1",
        chainId=str(137),
        verifyingContract=config_used.exchange,
    )

    # Compute EIP-712 digest
    signable = order_struct.signable_bytes(domain=domain)
    digest = keccak(signable)
    digest_hex = "0x" + digest.hex()
    print(f"  EIP-712 digest:     {digest_hex[:20]}...")

    # Recover signer from signature
    sig_hex = order_dict["signature"]
    recovered = Account._recover_hash(digest_hex, signature=bytes.fromhex(sig_hex.replace("0x", "")))
    print(f"  Recovered signer:   {recovered}")
    print(f"  Expected signer:    {order_dict['signer']}")
    print(f"  Expected maker:     {order_dict['maker']}")
    
    if recovered.lower() == order_dict["signer"].lower():
        print(f"  ✅ Signature recovery MATCHES signer field")
    else:
        print(f"  ❌ MISMATCH — recovered signer does not match order.signer!")

    # Also check that signer == our EOA
    our_eoa = client.signer.address()
    print(f"  Our EOA:            {our_eoa}")
    if recovered.lower() == our_eoa.lower():
        print(f"  ✅ Recovered address matches our EOA")
    else:
        print(f"  ❌ MISMATCH — signature was NOT made by our EOA!")

except Exception as e:
    print(f"  ❌ Signature verification failed: {e}")
    import traceback; traceback.print_exc()

# ── 6. Build the exact HTTP body ─────────────────────────────────────
print("\n" + "=" * 70)
print("STEP 6  –  Inspect exact HTTP request body")
print("=" * 70)

from py_clob_client.utilities import order_to_json

body = order_to_json(signed_order, api_creds.api_key, OrderType.GTC)
serialized = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
print(f"  Body length: {len(serialized)} bytes")
print(f"  Body (pretty):")
print(json.dumps(body, indent=2, default=str))

# ── 7. Post the order with full error capture ────────────────────────
print("\n" + "=" * 70)
print("STEP 7  –  POST the order (with full error capture)")
print("=" * 70)

try:
    # Monkey-patch requests.post to capture what's being sent
    import requests
    _original_post = requests.post

    def capturing_post(url, **kwargs):
        print(f"  → POST {url}")
        if "headers" in kwargs:
            print(f"  → Headers:")
            for k, v in kwargs["headers"].items():
                # Redact long values
                display = v if len(str(v)) < 80 else str(v)[:60] + "..."
                print(f"       {k}: {display}")
        if "data" in kwargs:
            print(f"  → Body length: {len(kwargs['data'])} bytes")
        
        resp = _original_post(url, **kwargs)
        print(f"  ← Status: {resp.status_code}")
        print(f"  ← Response headers:")
        for k, v in resp.headers.items():
            print(f"       {k}: {v[:80] if len(v) > 80 else v}")
        print(f"  ← Response body:")
        try:
            print(json.dumps(resp.json(), indent=2))
        except Exception:
            print(f"     {resp.text[:500]}")
        return resp
    
    requests.post = capturing_post
    
    response = client.post_order(signed_order, orderType=OrderType.GTC)
    print(f"\n  ✅ Order posted successfully!")
    print(f"  Response: {response}")
    
except Exception as e:
    print(f"\n  ❌ POST FAILED: {e}")
finally:
    requests.post = _original_post

print("\n" + "=" * 70)
print("DIAGNOSTIC COMPLETE")
print("=" * 70)
