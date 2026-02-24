"""Find the correct proxy wallet address for the exported Magic key."""
import os, sys, json
from dotenv import load_dotenv
load_dotenv()

import httpx
from eth_account import Account
from eth_utils import keccak
from eth_abi import encode

pk = os.getenv("POLY_PRIVATE_KEY")
funder_env = os.getenv("POLY_FUNDER_ADDRESS")
acct = Account.from_key(pk)
eoa = acct.address

print(f"EOA (derived from PK): {eoa}")
print(f"Proxy in .env:         {funder_env}")
print()

# ============================================================
# Method 1: Check Polymarket's Proxy Wallet Factory on polygon
# Polymarket uses a deterministic CREATE2 factory.
# The factory address is typically 0xaB45c5A4B0c941a2F231C04C3f49182e1A254052
# and the implementation is 0x44e999d5c2f66ef0861317f9a4805ac2e90aeb4f
# ============================================================
print("=== Method 1: Search factory events for proxy wallet creation ===")

# Known Polymarket proxy wallet factories
FACTORIES = [
    "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052",
    "0x71a2d1d2e09C0B9db4DeFeB57b70f62B09D1685E",
]

rpc = "https://polygon.drpc.org"

# Method 1b: Try computing the CREATE2 address
# CREATE2: address = keccak256(0xff ++ factory ++ salt ++ keccak256(init_code))[12:]
# Salt is typically keccak256(signer_address)
# Init code is the EIP-1167 minimal proxy creation code pointing to implementation

impl = "0x44e999d5c2f66ef0861317f9a4805ac2e90aeb4f"
# EIP-1167 init code: 3d602d80600a3d3981f3363d3d373d3d3d363d73<impl>5af43d82803e903d91602b57fd5bf3
init_code = bytes.fromhex("3d602d80600a3d3981f3363d3d373d3d3d363d73" + impl[2:] + "5af43d82803e903d91602b57fd5bf3")
init_code_hash = keccak(init_code)

for factory in FACTORIES:
    # Try salt = keccak256(abi.encode(address))
    salt1 = keccak(encode(["address"], [eoa]))
    # Try salt = keccak256(address bytes, no padding)
    salt2 = keccak(bytes.fromhex(eoa[2:]))
    # Try salt = just the address padded to 32 bytes
    salt3 = bytes(12) + bytes.fromhex(eoa[2:])
    
    for salt_name, salt in [("keccak(abi.encode(addr))", salt1), ("keccak(addr_bytes)", salt2), ("addr_padded", salt3)]:
        create2_input = b"\xff" + bytes.fromhex(factory[2:]) + salt + init_code_hash
        computed = "0x" + keccak(create2_input).hex()[-40:]
        match = computed.lower() == funder_env.lower()
        if match:
            print(f"  MATCH! factory={factory}, salt={salt_name}")
            print(f"  Computed: {computed}")
        # Also print if close
        # print(f"  factory={factory}, salt={salt_name}: {computed} {'MATCH!' if match else ''}")

print()

# ============================================================
# Method 2: Query Polymarket profile API
# ============================================================
print("=== Method 2: Polymarket Profile API ===")
try:
    resp = httpx.get(f"https://gamma-api.polymarket.com/profiles/{eoa}", timeout=15)
    if resp.status_code == 200:
        profile = resp.json()
        print(f"  Profile for EOA: {json.dumps(profile, indent=2)[:500]}")
    else:
        print(f"  No profile for EOA (status {resp.status_code})")
except Exception as e:
    print(f"  Error: {e}")

try:
    resp = httpx.get(f"https://gamma-api.polymarket.com/profiles/{funder_env}", timeout=15)
    if resp.status_code == 200:
        profile = resp.json()
        print(f"  Profile for funder: {json.dumps(profile, indent=2)[:500]}")
    else:
        print(f"  No profile for funder (status {resp.status_code})")
except Exception as e:
    print(f"  Error: {e}")

print()

# ============================================================
# Method 3: Check who the proxy wallet's actual authorized signer is
# by looking at transaction history / proxy creation
# ============================================================
print("=== Method 3: Check proxy wallet transactions on PolygonScan ===")
try:
    # Use the free PolygonScan API (no key needed for basic queries)
    # Get internal txns to the proxy wallet to find the factory that created it
    url = f"https://api.polygonscan.com/api?module=account&action=txlistinternal&address={funder_env}&startblock=0&endblock=99999999&sort=asc&page=1&offset=5"
    resp = httpx.get(url, timeout=15)
    data = resp.json()
    if data.get("status") == "1" and data.get("result"):
        txns = data["result"]
        print(f"  Found {len(txns)} internal txns")
        for tx in txns[:3]:
            print(f"    from={tx.get('from','?')[:20]}... hash={tx.get('hash','?')[:20]}... type={tx.get('type','?')}")
    else:
        print(f"  No internal txns or rate limited: {data.get('message', '?')}")
except Exception as e:
    print(f"  Error: {e}")

print()

# ============================================================
# Method 4: Check CLOB API for our address mapping
# Use the derive endpoint to see what the server knows about us
# ============================================================
print("=== Method 4: CLOB API address check ===")
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

# With funder (sig_type=1)
client1 = ClobClient("https://clob.polymarket.com", key=pk, chain_id=POLYGON, signature_type=1, funder=funder_env)
api_creds = client1.create_or_derive_api_creds()
client1.set_api_creds(api_creds)

# Check balance for proxy wallet
try:
    bal = client1.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=1))
    print(f"  Balance (sig_type=1, funder={funder_env[:10]}...): {bal}")
except Exception as e:
    print(f"  Balance check failed: {str(e)[:100]}")

# Without funder (sig_type=0, EOA only)
client0 = ClobClient("https://clob.polymarket.com", key=pk, chain_id=POLYGON, signature_type=0)
api_creds0 = client0.create_or_derive_api_creds()
client0.set_api_creds(api_creds0)

try:
    bal0 = client0.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0))
    print(f"  Balance (sig_type=0, EOA={eoa[:10]}...): {bal0}")
except Exception as e:
    print(f"  Balance check (sig_type=0) failed: {str(e)[:100]}")

print()

# ============================================================
# Method 5: Try to use the proxy factory's getProxy(address) function
# ============================================================
print("=== Method 5: Query proxy factory for correct wallet ===")
# Common factory selectors
for factory in FACTORIES:
    for func_name, func_sig in [
        ("getProxy(address)", "getProxy(address)"),
        ("wallets(address)", "wallets(address)"),
        ("proxyMap(address)", "proxyMap(address)"),
        ("getWallet(address)", "getWallet(address)"),
    ]:
        selector = keccak(func_sig.encode())[:4].hex()
        calldata = "0x" + selector + encode(["address"], [eoa]).hex()
        payload = {"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": factory, "data": calldata}, "latest"], "id": 1}
        try:
            resp = httpx.post(rpc, json=payload, timeout=10)
            result = resp.json().get("result", "")
            err = resp.json().get("error", "")
            if result and result != "0x" and len(result) >= 66:
                addr = "0x" + result[-40:]
                if addr != "0x0000000000000000000000000000000000000000":
                    print(f"  {factory[:10]}...{func_name}: {addr}")
                    if addr.lower() == funder_env.lower():
                        print(f"    ^ MATCHES .env funder!")
                    else:
                        print(f"    ^ DIFFERENT from .env funder ({funder_env})")
        except:
            pass

print()
print("=== DONE ===")
