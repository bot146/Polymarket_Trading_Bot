"""Find the correct proxy wallet via factory contracts."""
import httpx
from eth_utils import keccak
from eth_abi import encode

eoa = "0xa42bdDa7407C84eE508701EcE276CCD0Ca59ac9C"
proxy_env = "0x0df18f2e85aa500635ec19504f3713fdbe0754cc"
rpc = "https://polygon.drpc.org"

factories = [
    "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052",
    "0x71a2d1d2e09C0B9db4DeFeB57b70f62B09D1685E",
    "0x2aA537D400a0FE3E3FA05fDc577Bed0585E702c3",
    "0xC22D5b2b6671461e4b10919BA2610Cb3c1AbCfce",
]

funcs = [
    "getProxyWalletAddress(address)",
    "computeProxyAddress(address)",
    "getProxy(address)",
    "walletOf(address)",
    "proxyOf(address)",
    "getProxyFor(address)",
]

for factory in factories:
    payload = {"jsonrpc": "2.0", "method": "eth_getCode", "params": [factory, "latest"], "id": 1}
    resp = httpx.post(rpc, json=payload, timeout=10)
    code = resp.json().get("result", "0x")
    if code == "0x":
        continue

    code_len = (len(code) - 2) // 2
    print(f"Factory {factory} (code: {code_len} bytes)")

    for func_sig in funcs:
        sel = keccak(func_sig.encode())[:4].hex()
        calldata = "0x" + sel + encode(["address"], [eoa]).hex()
        payload = {"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": factory, "data": calldata}, "latest"], "id": 1}
        try:
            resp = httpx.post(rpc, json=payload, timeout=10)
            data = resp.json()
            result = data.get("result", "")
            if result and len(result) >= 66 and result != "0x":
                addr = "0x" + result[-40:]
                zero = "0x0000000000000000000000000000000000000000"
                if addr != zero:
                    tag = "== .env MATCH!" if addr.lower() == proxy_env.lower() else "!= .env (DIFFERENT)"
                    print(f"  {func_sig}: {addr} {tag}")
        except:
            pass
    print()

# Also try: compute CREATE2 for every factory with various salt schemes
print("=== CREATE2 brute-force ===")
impl = "0x44e999d5c2f66ef0861317f9a4805ac2e90aeb4f"
init_code_runtime = bytes.fromhex(
    "363d3d373d3d3d363d73" + impl[2:] + "5af43d82803e903d91602b57fd5bf3"
)
# EIP-1167 creation code (with different possible prefixes)
creation_codes = [
    bytes.fromhex("3d602d80600a3d3981f3") + init_code_runtime,  # standard
]
for creation_code in creation_codes:
    init_hash = keccak(creation_code)
    
    eoa_bytes = bytes.fromhex(eoa[2:])
    salts = [
        ("keccak(abi.encode(addr))", keccak(encode(["address"], [eoa]))),
        ("keccak(addr_bytes20)", keccak(eoa_bytes)),
        ("addr_left_padded", bytes(12) + eoa_bytes),
        ("keccak(addr_lower)", keccak(eoa.lower().encode())),
        ("keccak(addr_lower_no0x)", keccak(eoa.lower()[2:].encode())),
        ("raw_zero", bytes(32)),
    ]
    
    for factory in factories:
        payload = {"jsonrpc": "2.0", "method": "eth_getCode", "params": [factory, "latest"], "id": 1}
        resp = httpx.post(rpc, json=payload, timeout=10)
        code = resp.json().get("result", "0x")
        if code == "0x":
            continue
            
        factory_bytes = bytes.fromhex(factory[2:])
        for salt_name, salt in salts:
            create2_input = b"\xff" + factory_bytes + salt + init_hash
            computed = "0x" + keccak(create2_input).hex()[-40:]
            if computed.lower() == proxy_env.lower():
                print(f"FOUND! factory={factory}, salt={salt_name}")
                print(f"  Computed proxy: {computed}")

print()
print("=== Check proxy wallet creation tx via Polygonscan free API ===")
# Try to find the tx that created the proxy wallet by checking the proxy's first tx
try:
    url = f"https://api.polygonscan.com/api?module=account&action=txlist&address={proxy_env}&startblock=0&endblock=99999999&sort=asc&page=1&offset=5"
    resp = httpx.get(url, timeout=15)
    data = resp.json()
    if data.get("status") == "1":
        txns = data["result"]
        print(f"First {len(txns)} txns to/from proxy wallet:")
        for tx in txns:
            fr = tx.get("from", "?")
            to = tx.get("to", "?")
            print(f"  from={fr}  to={to}  method={tx.get('methodId','?')}  hash={tx.get('hash','?')[:16]}...")
    else:
        print(f"  API response: {data.get('message', '?')} / {data.get('result', '?')}")
except Exception as e:
    print(f"  Error: {e}")

print("\nDone.")
