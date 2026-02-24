"""Smoke-test: post ONE tiny live trade to verify the bot can interact with Polymarket.

Usage:
    python test_live_trade.py              # dry-run (default) — signs but does NOT post
    python test_live_trade.py --execute    # actually posts the order

The script:
1. Loads .env credentials and builds a CLOB client.
2. Fetches markets, picks a liquid one with a cheap YES token (ask < $0.10).
3. Places a single BUY order for 1 share at the best ask (total cost < $0.50).
4. Prints the full response so you can verify success.

This is a GTC (good-til-cancelled) limit order at the ask price, so it should
fill immediately if liquidity is there.  Total risk: < $0.10 + fees ≈ $0.10.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal

from polymarket_bot.config import load_settings
from polymarket_bot.clob_client import build_clob_client
from polymarket_bot.log_config import setup_logging
from polymarket_bot.scanner import MarketScanner

# py-clob-client types
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY


def find_cheap_token(scanner: MarketScanner) -> dict | None:
    """Find a liquid market with a cheap YES token to buy."""
    print("🔍 Fetching markets...")
    try:
        markets = scanner.get_all_markets(limit=500, active_only=True)
    except Exception as e:
        print(f"   Error fetching markets: {e}")
        return None

    print(f"   Got {len(markets)} markets. Scanning for a cheap, liquid token...")

    best = None
    for m in markets:
        # Need decent volume
        vol = float(m.volume)
        if vol < 5_000:
            continue

        for t in m.tokens:
            ask = float(t.price)

            # We want something cheap: $0.02–$0.08 range
            if not (0.02 <= ask <= 0.08):
                continue

            token_id = t.token_id
            if not token_id:
                continue

            # Pick the cheapest qualifying token
            if best is None or ask < best["ask"]:
                best = {
                    "token_id": token_id,
                    "ask": ask,
                    "outcome": t.outcome,
                    "question": m.question[:80],
                    "condition_id": m.condition_id,
                    "volume": vol,
                }

    return best


def main() -> None:
    parser = argparse.ArgumentParser(description="Post a single tiny test trade on Polymarket")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually post the order. Without this flag, only signs and prints (dry-run).",
    )
    args = parser.parse_args()

    setup_logging("INFO")
    settings = load_settings()

    if not settings.poly_private_key:
        print("❌ POLY_PRIVATE_KEY not set in .env — cannot trade.")
        sys.exit(1)

    # Build client directly (skip build_clob_client to avoid double derive)
    print("🔑 Building CLOB client...")
    from py_clob_client.client import ClobClient

    client = ClobClient(
        settings.poly_host,
        key=settings.poly_private_key,
        chain_id=settings.poly_chain_id,
        signature_type=settings.poly_signature_type,
        funder=settings.poly_funder_address,
    )
    api_creds = client.create_or_derive_api_creds()
    client.set_api_creds(api_creds)
    print("   ✅ Client ready (L2 auth set)")

    # Quick L2 auth check
    try:
        open_orders = client.get_orders()
        print(f"   ✅ L2 auth verified ({len(open_orders) if isinstance(open_orders, list) else '?'} open orders)\n")
    except Exception as e:
        print(f"   ⚠️  L2 auth check failed: {e}\n")

    # Find a cheap token
    scanner = MarketScanner()
    token = find_cheap_token(scanner)

    if token is None:
        print("❌ Could not find a suitable cheap token. Try again later.")
        sys.exit(1)

    price = token["ask"]
    size = 20  # 20 shares — keeps total well under $0.50
    cost = price * size
    fee_est = cost * 0.02  # 2% taker fee estimate

    print("=" * 60)
    print("📋 TRADE PLAN")
    print("=" * 60)
    print(f"   Market:     {token['question']}")
    print(f"   Outcome:    {token['outcome']}")
    print(f"   Token ID:   {token['token_id'][:20]}...")
    print(f"   Side:       BUY")
    print(f"   Price:      ${price:.4f}")
    print(f"   Size:       {size} share(s)")
    print(f"   Est. Cost:  ${cost:.4f}")
    print(f"   Est. Fee:   ${fee_est:.4f}")
    print(f"   Est. Total: ${cost + fee_est:.4f}")
    print(f"   Order Type: GTC (limit at ask)")
    print("=" * 60)

    if cost + fee_est > 0.50:
        print("⚠️  Total exceeds $0.50 safety cap — aborting.")
        sys.exit(1)

    # Create the order
    order_args = OrderArgs(
        price=price,
        size=float(size),
        side=BUY,
        token_id=token["token_id"],
    )

    print("\n🔏 Signing order...")
    signed_order = client.create_order(order_args)
    print("   ✅ Order signed successfully")

    if not args.execute:
        print("\n🏁 DRY-RUN complete. Order was signed but NOT posted.")
        print("   Re-run with --execute to actually post the trade.")
        return

    # Post it
    print("\n📤 Posting order to Polymarket CLOB...")
    print(f"   Signed order keys: {list(signed_order.keys()) if isinstance(signed_order, dict) else dir(signed_order)}")
    try:
        response = client.post_order(signed_order, orderType="GTC")
    except Exception as e:
        print(f"\n❌ POST FAILED: {e}")
        # Print signed order for debugging
        if isinstance(signed_order, dict):
            print(f"   Signed order: {json.dumps(signed_order, indent=2, default=str)}")
        else:
            print(f"   Signed order: {signed_order}")
        sys.exit(1)

    print("\n✅ ORDER POSTED! Response:")
    if hasattr(response, "model_dump"):
        print(json.dumps(response.model_dump(), indent=2, default=str))
    elif isinstance(response, dict):
        print(json.dumps(response, indent=2, default=str))
    else:
        print(response)

    # Try to extract order ID
    order_id = None
    if isinstance(response, dict):
        order_id = response.get("orderID") or response.get("orderId")
    elif hasattr(response, "orderID"):
        order_id = response.orderID

    if order_id:
        print(f"\n🎫 Order ID: {order_id}")
        print("   You can check status with: client.get_order(order_id)")
    else:
        print("\n⚠️  Could not extract order ID from response.")

    print("\n🏁 Done! Check your Polymarket portfolio to confirm the fill.")


if __name__ == "__main__":
    main()
