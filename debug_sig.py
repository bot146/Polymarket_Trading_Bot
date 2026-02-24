"""Debug script: inspect signed order fields to diagnose 'invalid signature'."""
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY
from polymarket_bot.config import load_settings
from polymarket_bot.scanner import MarketScanner

settings = load_settings()
client = ClobClient(
    settings.poly_host,
    key=settings.poly_private_key,
    chain_id=settings.poly_chain_id,
    signature_type=settings.poly_signature_type,
    funder=settings.poly_funder_address,
)
api_creds = client.create_or_derive_api_creds()
client.set_api_creds(api_creds)

# Find a cheap token
scanner = MarketScanner()
markets = scanner.get_all_markets(limit=500, active_only=True)
token_id = None
price = None
for m in markets:
    if float(m.volume) < 5000:
        continue
    for t in m.tokens:
        if 0.02 <= float(t.price) <= 0.08 and t.token_id:
            token_id = t.token_id
            price = float(t.price)
            break
    if token_id:
        break

print(f"Token ID: {token_id}")
print(f"Token ID length: {len(token_id)}")
print(f"Price: {price}")
print()

# Build and inspect
order_args = OrderArgs(price=price, size=20.0, side=BUY, token_id=token_id)
signed = client.create_order(order_args)

print(f"Signature: {signed.signature}")
print(f"Sig length (hex chars): {len(signed.signature)}")
print()

order = signed.order
print("Order fields:")
for attr in sorted(dir(order)):
    if not attr.startswith("_"):
        val = getattr(order, attr, "???")
        if not callable(val):
            print(f"  {attr} = {val}")
print()

# Client internal state
print(f"Client creds.signature_type = {client.creds.signature_type}")
print(f"Client creds.funder = {client.creds.funder}")
print()

# Signer from private key
try:
    from eth_account import Account
    signer = Account.from_key(settings.poly_private_key).address
    print(f"Signer (EOA from private key): {signer}")
    print(f"Funder (from .env): {settings.poly_funder_address}")
    print(f"Match? signer==funder: {signer.lower() == settings.poly_funder_address.lower()}")
except Exception as e:
    print(f"Could not derive signer: {e}")
