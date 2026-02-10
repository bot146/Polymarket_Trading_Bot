"""One-shot diagnostic: why did each strategy find zero signals?"""
import requests, json

# 1. Fetch active markets (same as the bot does)
r = requests.get("https://gamma-api.polymarket.com/markets",
                  params={"active": "true", "limit": 500, "closed": "false"}, timeout=15)
markets = r.json()
print(f"Fetched {len(markets)} active markets\n")

# ── ARBITRAGE DIAGNOSTIC ──────────────────────────────────────
print("=" * 60)
print("ARBITRAGE DIAGNOSTIC  (YES_ask + NO_ask < $1)")
print("=" * 60)
binary = 0
arb_candidates = []
for m in markets:
    tokens = m.get("tokens", [])
    if len(tokens) != 2:
        continue
    yes = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
    no = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), None)
    if not yes or not no:
        continue
    binary += 1
    yp = float(yes.get("price", 0))
    np_ = float(no.get("price", 0))
    total = yp + np_
    edge_cents = (1 - total) * 100
    fee_cents = (yp + np_) * 0.02 * 100  # 2% taker fee on both legs
    net_cents = edge_cents - fee_cents
    arb_candidates.append((edge_cents, net_cents, total, yp, np_, m.get("question", "")[:70]))

arb_candidates.sort(key=lambda x: x[0], reverse=True)
print(f"Binary markets: {binary}")
print(f"Markets with gross edge > 0: {sum(1 for a in arb_candidates if a[0] > 0)}")
print(f"Markets with gross edge > 1.5c (our threshold): {sum(1 for a in arb_candidates if a[0] > 1.5)}")
print(f"Markets with NET edge > 1.5c (after 2% fees): {sum(1 for a in arb_candidates if a[1] > 1.5)}")
print(f"\nTop 10 closest to arb (by gross edge):")
for edge, net, total, yp, np_, q in arb_candidates[:10]:
    marker = " <<<< TRADEABLE" if net > 1.5 else ""
    print(f"  gross={edge:+.2f}c  net={net:+.2f}c  Y={yp:.3f} N={np_:.3f}  total={total:.4f}  | {q}{marker}")

# ── GUARANTEED WIN DIAGNOSTIC ─────────────────────────────────
print(f"\n{'=' * 60}")
print("GUARANTEED WIN DIAGNOSTIC  (resolved winning token < $1)")
print("=" * 60)
r2 = requests.get("https://gamma-api.polymarket.com/markets",
                   params={"closed": "true", "limit": 100}, timeout=15)
resolved = r2.json()
print(f"Resolved markets fetched: {len(resolved)}")
gw_count = 0
for m in resolved:
    tokens = m.get("tokens", [])
    for t in tokens:
        price = float(t.get("price", 1.0))
        winner = t.get("winner", False)
        if winner and price < 0.99:
            gw_count += 1
            if gw_count <= 5:
                print(f"  Winner at {price:.3f}: {m.get('question', '')[:60]}")
print(f"Guaranteed win opportunities: {gw_count}")

# ── MARKET MAKING DIAGNOSTIC ─────────────────────────────────
print(f"\n{'=' * 60}")
print("MARKET MAKING DIAGNOSTIC  (needs order book bid/ask spread)")
print("=" * 60)
# The bot's MarketMakingStrategy needs BOTH best_bid and best_ask from WSS
# Let's check if the WSS feed was even providing that data
print("Market making requires live WebSocket order book data.")
print("The Gamma API only provides mid-prices, NOT bid/ask spreads.")
print("KEY QUESTION: Was the WSS feed connecting and providing best_bid/best_ask?")
print("(The bot logs showed no WSS connection messages - this is likely the issue)")

# ── SNIPING DIAGNOSTIC ────────────────────────────────────────
print(f"\n{'=' * 60}")
print("SNIPING DIAGNOSTIC  (sudden price moves)")
print("=" * 60)
print("Sniping needs consecutive price snapshots to detect moves.")
print("Without WSS data flowing, no price changes are detected.")

# ── ORACLE SNIPING DIAGNOSTIC ────────────────────────────────
print(f"\n{'=' * 60}")
print("ORACLE SNIPING DIAGNOSTIC  (crypto price vs Polymarket)")
print("=" * 60)
# Find crypto-related markets
crypto_keywords = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto", "xrp", "doge"]
crypto_markets = []
for m in markets:
    q = m.get("question", "").lower()
    if any(kw in q for kw in crypto_keywords):
        tokens = m.get("tokens", [])
        crypto_markets.append((q[:70], tokens))
print(f"Crypto-related markets found: {len(crypto_markets)}")
for q, tokens in crypto_markets[:10]:
    prices = ", ".join(f"{t.get('outcome')}={t.get('price')}" for t in tokens)
    print(f"  {q} | {prices}")

# ── OVERALL DIAGNOSIS ─────────────────────────────────────────
print(f"\n{'=' * 60}")
print("ROOT CAUSE ANALYSIS")
print("=" * 60)
print("""
1. ARBITRAGE: Polymarket mid-prices almost always sum to ~1.00. 
   Pure arb (YES+NO < $1) is extremely rare because market makers 
   keep prices efficient. The bot needs ORDER BOOK data (best_ask) 
   not mid-prices to find real arb edges.

2. GUARANTEED WIN: Resolved markets instantly price winners at $1. 
   This strategy is nearly impossible to execute in practice.

3. MARKET MAKING: Requires WSS best_bid/best_ask to compute spreads. 
   Without a working WebSocket feed, this strategy is blind.

4. SNIPING: Also needs WSS price movement data.

5. ORACLE SNIPING: Crypto markets exist, but the strategy only fires
   when there's a large discrepancy between CoinGecko and Polymarket.

RECOMMENDATION: The bot needs strategies that work with DIRECTIONAL 
BETS on outcomes, not just pure arbitrage. Most Polymarket profit 
comes from:
  - Buying underpriced outcomes (value betting / edge finding)
  - Market making with real order book access
  - Event-driven trading (news → price impact)
  - Multi-outcome arbitrage (markets with 3+ outcomes summing > $1)
""")

# ── MULTI-OUTCOME ARB CHECK ──────────────────────────────────
print("=" * 60)
print("MULTI-OUTCOME ARBITRAGE  (3+ outcomes summing > $1)")
print("=" * 60)
multi = []
for m in markets:
    tokens = m.get("tokens", [])
    if len(tokens) < 3:
        continue
    total = sum(float(t.get("price", 0)) for t in tokens)
    edge = (total - 1.0) * 100  # overpriced = sell opportunity
    multi.append((edge, total, len(tokens), m.get("question", "")[:60]))
multi.sort(reverse=True)
print(f"Markets with 3+ outcomes: {len(multi)}")
print(f"Markets where total > $1 (sell-side arb): {sum(1 for e,_,_,_ in multi if e > 0)}")
print(f"\nTop 10 multi-outcome markets:")
for edge, total, n_tok, q in multi[:10]:
    print(f"  {n_tok} outcomes  total={total:.4f}  edge={edge:+.2f}c  | {q}")

# ── VALUE BET CHECK (extreme prices) ─────────────────────────
print(f"\n{'=' * 60}")
print("VALUE BET CANDIDATES  (tokens priced near 0 or near 1)")
print("=" * 60)
value_bets = []
for m in markets:
    vol = float(m.get("volume", 0) or 0)
    if vol < 10000:
        continue
    tokens = m.get("tokens", [])
    for t in tokens:
        price = float(t.get("price", 0.5))
        if price <= 0.05 or price >= 0.95:
            value_bets.append((price, vol, t.get("outcome"), m.get("question", "")[:60]))

value_bets.sort(key=lambda x: x[0])
print(f"Tokens priced <= 5c or >= 95c (high volume): {len(value_bets)}")
print(f"\nCheapest (potential YES longshots):")
for price, vol, outcome, q in value_bets[:10]:
    print(f"  {outcome}={price:.3f}  vol=${vol:,.0f}  | {q}")
print(f"\nMost expensive (potential NO bets against):")
for price, vol, outcome, q in sorted(value_bets, key=lambda x: -x[0])[:10]:
    print(f"  {outcome}={price:.3f}  vol=${vol:,.0f}  | {q}")
