"""Analyze overnight position P&L."""
import json, statistics
from collections import Counter, defaultdict
from pathlib import Path

data = json.load(open(Path.home() / ".polymarket_bot" / "positions.json"))
positions = data["positions"]
closed = [p for p in positions if p["status"] == "closed"]
openp = [p for p in positions if p["status"] == "open"]

total_pnl = sum(float(p["realized_pnl"]) for p in closed)
wins = [p for p in closed if float(p["realized_pnl"]) > 0]
losses = [p for p in closed if float(p["realized_pnl"]) < 0]
breakeven = [p for p in closed if float(p["realized_pnl"]) == 0]

print("=== OVERNIGHT SUMMARY ===")
print(f"Total: {len(positions)} ({len(closed)} closed, {len(openp)} open)")
print(f"Realized P&L: ${total_pnl:.4f}")
print(f"Wallet: ${40 + total_pnl:.2f}")
print(f"Win/Loss: {len(wins)}W / {len(losses)}L / {len(breakeven)}BE")
print(f"Win rate: {len(wins)/len(closed)*100:.1f}%")
if wins:
    print(f"Avg win: ${sum(float(p['realized_pnl']) for p in wins)/len(wins):.4f}")
if losses:
    print(f"Avg loss: ${sum(float(p['realized_pnl']) for p in losses)/len(losses):.4f}")
print(f"Total wins: ${sum(float(p['realized_pnl']) for p in wins):.4f}")
print(f"Total losses: ${sum(float(p['realized_pnl']) for p in losses):.4f}")
print()

# By exit reason
exit_reasons = Counter()
pnl_by_reason = defaultdict(float)
count_by_reason = defaultdict(lambda: {"wins": 0, "losses": 0})
for p in closed:
    reason = p.get("close_reason", "unknown")
    exit_reasons[reason] += 1
    pnl_by_reason[reason] += float(p["realized_pnl"])
    if float(p["realized_pnl"]) > 0:
        count_by_reason[reason]["wins"] += 1
    else:
        count_by_reason[reason]["losses"] += 1

print("=== BY EXIT REASON ===")
for reason, count in exit_reasons.most_common():
    w = count_by_reason[reason]["wins"]
    l = count_by_reason[reason]["losses"]
    print(f"  {reason}: {count} trades (W:{w}/L:{l}), P&L=${pnl_by_reason[reason]:.4f}")

# By entry price
print()
print("=== BY ENTRY PRICE ===")
price_buckets = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0})
for p in closed:
    price = float(p["entry_price"])
    bucket = f"{price:.2f}"
    price_buckets[bucket]["count"] += 1
    price_buckets[bucket]["pnl"] += float(p["realized_pnl"])
    if float(p["realized_pnl"]) > 0:
        price_buckets[bucket]["wins"] += 1

for bucket in sorted(price_buckets.keys()):
    d = price_buckets[bucket]
    wr = d["wins"] / d["count"] * 100
    print(f"  ${bucket}: {d['count']} trades, P&L=${d['pnl']:.4f}, WR={wr:.0f}%")

# Return distribution
print()
print("=== RETURN DISTRIBUTION ===")
returns = []
for p in closed:
    entry = float(p["entry_price"])
    qty = float(p["quantity"])
    cost = entry * qty
    ret_pct = float(p["realized_pnl"]) / cost * 100 if cost > 0 else 0
    returns.append(ret_pct)
print(f"Mean return: {statistics.mean(returns):.1f}%")
print(f"Median return: {statistics.median(returns):.1f}%")
print(f"Stdev return: {statistics.stdev(returns):.1f}%")

# Exit price analysis
print()
print("=== EXIT PRICE ANALYSIS ===")
exit_at_zero = [p for p in closed if p.get("exit_price") and float(p["exit_price"]) < 0.05]
exit_at_one = [p for p in closed if p.get("exit_price") and float(p["exit_price"]) > 0.95]
exit_mid = [p for p in closed if p.get("exit_price") and 0.05 <= float(p["exit_price"]) <= 0.95]
print(f"Exit near $0 (<0.05): {len(exit_at_zero)} ({sum(float(p['realized_pnl']) for p in exit_at_zero):.4f})")
print(f"Exit near $1 (>0.95): {len(exit_at_one)} ({sum(float(p['realized_pnl']) for p in exit_at_one):.4f})")
print(f"Exit mid (0.05-0.95): {len(exit_mid)} ({sum(float(p['realized_pnl']) for p in exit_mid):.4f})")

# Top 5 winners and losers
print()
print("=== TOP 5 WINNERS ===")
for p in sorted(wins, key=lambda x: float(x["realized_pnl"]), reverse=True)[:5]:
    print(f"  {p['position_id']}: entry=${p['entry_price']} exit=${p['exit_price']} qty={p['quantity']} pnl=${float(p['realized_pnl']):.4f} reason={p.get('close_reason','?')}")

print()
print("=== TOP 5 LOSERS ===")
for p in sorted(losses, key=lambda x: float(x["realized_pnl"]))[:5]:
    print(f"  {p['position_id']}: entry=${p['entry_price']} exit=${p['exit_price']} qty={p['quantity']} pnl=${float(p['realized_pnl']):.4f} reason={p.get('close_reason','?')}")

# Analyze asymmetry problem: entry near 0.50 + binary outcome
print()
print("=== CORE ISSUE: ENTRY~50c ON BINARY MARKETS ===")
near_50 = [p for p in closed if 0.45 <= float(p["entry_price"]) <= 0.55]
print(f"Entries $0.45-$0.55: {len(near_50)} / {len(closed)} ({len(near_50)/len(closed)*100:.0f}%)")
if near_50:
    near_pnl = sum(float(p["realized_pnl"]) for p in near_50)
    near_wins = sum(1 for p in near_50 if float(p["realized_pnl"]) > 0)
    print(f"  P&L: ${near_pnl:.4f}, WR: {near_wins/len(near_50)*100:.1f}%")
    # How many resolved (exit near 0 or 1)?
    resolved = [p for p in near_50 if p.get("exit_price") and (float(p["exit_price"]) < 0.05 or float(p["exit_price"]) > 0.95)]
    profit_target = [p for p in near_50 if p.get("close_reason") == "profit_target"]
    stop_loss = [p for p in near_50 if p.get("close_reason") == "stop_loss"]
    age = [p for p in near_50 if p.get("close_reason") == "max_age"]
    print(f"  Resolved to 0/1: {len(resolved)}")
    print(f"  Profit target: {len(profit_target)} (pnl=${sum(float(p['realized_pnl']) for p in profit_target):.4f})")
    print(f"  Stop loss: {len(stop_loss)} (pnl=${sum(float(p['realized_pnl']) for p in stop_loss):.4f})")  
    print(f"  Max age: {len(age)} (pnl=${sum(float(p['realized_pnl']) for p in age):.4f})")
