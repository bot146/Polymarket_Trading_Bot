"""Standalone position analysis - no bot imports, writes to file."""
import json, os, sys
from collections import Counter

POSITIONS_FILE = os.path.expanduser("~/.polymarket_bot/positions.json")
OUTPUT_FILE = os.path.expanduser("~/.polymarket_bot/analysis_clean.txt")

with open(POSITIONS_FILE) as f:
    data = json.load(f)

positions = data.get("positions", [])
closed = [p for p in positions if p["status"] == "closed"]
opened = [p for p in positions if p["status"] == "open"]

lines = []
def out(s=""):
    lines.append(s)

out("=" * 70)
out("OVERNIGHT POSITION ANALYSIS")
out("=" * 70)
out(f"Total positions: {len(positions)}")
out(f"Closed: {len(closed)}, Open: {len(opened)}")

# P&L summary
pnls = [float(p.get("realized_pnl", 0)) for p in closed]
total_pnl = sum(pnls)
wins = [x for x in pnls if x > 0.001]
losses = [x for x in pnls if x < -0.001]
zeros = [x for x in pnls if abs(x) <= 0.001]

out(f"\nTotal Realized P&L: ${total_pnl:.4f}")
out(f"Wins: {len(wins)}, Losses: {len(losses)}, Break-even: {len(zeros)}")
if len(wins) + len(losses) > 0:
    out(f"Win Rate (excl breakeven): {len(wins)/(len(wins)+len(losses))*100:.1f}%")

if wins:
    out(f"\nAvg Win: ${sum(wins)/len(wins):.4f}")
    out(f"Max Win: ${max(wins):.4f}")
    out(f"Total Wins: ${sum(wins):.4f}")
if losses:
    out(f"\nAvg Loss: ${sum(losses)/len(losses):.4f}")
    out(f"Max Loss: ${min(losses):.4f}")
    out(f"Total Losses: ${sum(losses):.4f}")

# Entry price distribution
out("\n" + "=" * 70)
out("ENTRY PRICE DISTRIBUTION")
out("=" * 70)
entry_prices = [float(p["entry_price"]) for p in closed]
buckets = {"< 0.15": 0, "0.15-0.30": 0, "0.30-0.45": 0, "0.45-0.55": 0, "0.55-0.70": 0, "0.70-0.85": 0, "> 0.85": 0}
for ep in entry_prices:
    if ep < 0.15: buckets["< 0.15"] += 1
    elif ep < 0.30: buckets["0.15-0.30"] += 1
    elif ep < 0.45: buckets["0.30-0.45"] += 1
    elif ep < 0.55: buckets["0.45-0.55"] += 1
    elif ep < 0.70: buckets["0.55-0.70"] += 1
    elif ep < 0.85: buckets["0.70-0.85"] += 1
    else: buckets["> 0.85"] += 1

for b, c in buckets.items():
    pct = c/len(closed)*100 if closed else 0
    # P&L for this bucket
    bucket_pnl = []
    for p in closed:
        ep = float(p["entry_price"])
        if b == "< 0.15" and ep < 0.15: bucket_pnl.append(float(p.get("realized_pnl", 0)))
        elif b == "0.15-0.30" and 0.15 <= ep < 0.30: bucket_pnl.append(float(p.get("realized_pnl", 0)))
        elif b == "0.30-0.45" and 0.30 <= ep < 0.45: bucket_pnl.append(float(p.get("realized_pnl", 0)))
        elif b == "0.45-0.55" and 0.45 <= ep < 0.55: bucket_pnl.append(float(p.get("realized_pnl", 0)))
        elif b == "0.55-0.70" and 0.55 <= ep < 0.70: bucket_pnl.append(float(p.get("realized_pnl", 0)))
        elif b == "0.70-0.85" and 0.70 <= ep < 0.85: bucket_pnl.append(float(p.get("realized_pnl", 0)))
        elif b == "> 0.85" and ep >= 0.85: bucket_pnl.append(float(p.get("realized_pnl", 0)))
    bpnl = sum(bucket_pnl) if bucket_pnl else 0
    out(f"  {b:>10}: {c:3d} ({pct:5.1f}%)  P&L: ${bpnl:+.4f}")

# Exit price distribution
out("\n" + "=" * 70)
out("EXIT PRICE DISTRIBUTION")
out("=" * 70)
exit_prices = [float(p["exit_price"]) for p in closed if p.get("exit_price")]
near_zero = sum(1 for ep in exit_prices if ep < 0.10)
near_one = sum(1 for ep in exit_prices if ep > 0.90)
mid = sum(1 for ep in exit_prices if 0.10 <= ep <= 0.90)
out(f"  Near $0 (< 0.10): {near_zero} ({near_zero/len(closed)*100:.1f}%)")
out(f"  Mid (0.10-0.90):  {mid} ({mid/len(closed)*100:.1f}%)")
out(f"  Near $1 (> 0.90): {near_one} ({near_one/len(closed)*100:.1f}%)")

# More detailed exit price buckets
exit_buckets = {"0.00-0.10": 0, "0.10-0.20": 0, "0.20-0.30": 0, "0.30-0.40": 0,
                "0.40-0.50": 0, "0.50-0.60": 0, "0.60-0.70": 0, "0.70-0.80": 0,
                "0.80-0.90": 0, "0.90-1.00": 0}
for ep in exit_prices:
    if ep < 0.10: exit_buckets["0.00-0.10"] += 1
    elif ep < 0.20: exit_buckets["0.10-0.20"] += 1
    elif ep < 0.30: exit_buckets["0.20-0.30"] += 1
    elif ep < 0.30: exit_buckets["0.20-0.30"] += 1
    elif ep < 0.40: exit_buckets["0.30-0.40"] += 1
    elif ep < 0.50: exit_buckets["0.40-0.50"] += 1
    elif ep < 0.60: exit_buckets["0.50-0.60"] += 1
    elif ep < 0.70: exit_buckets["0.60-0.70"] += 1
    elif ep < 0.80: exit_buckets["0.70-0.80"] += 1
    elif ep < 0.90: exit_buckets["0.80-0.90"] += 1
    else: exit_buckets["0.90-1.00"] += 1

for b, c in exit_buckets.items():
    if c > 0:
        out(f"  {b}: {c}")

# Exit reason analysis: entry vs exit price difference
out("\n" + "=" * 70)
out("EXIT REASON ANALYSIS")
out("=" * 70)
# Since we don't have exit_reason in the data, infer from price movement
stop_loss_exits = []  # exit_price << entry_price (big drop)
profit_target_exits = []  # exit_price >> entry_price (big gain)
flat_exits = []  # exit_price ≈ entry_price
resolution_exits = []  # exit_price near 0 or near 1

for p in closed:
    ep = float(p["entry_price"])
    xp = float(p.get("exit_price", ep))
    pnl = float(p.get("realized_pnl", 0))
    
    if xp < 0.05 or xp > 0.95:
        resolution_exits.append(p)
    elif abs(xp - ep) <= 0.001:
        flat_exits.append(p)
    elif pnl < -0.001:
        stop_loss_exits.append(p)
    elif pnl > 0.001:
        profit_target_exits.append(p)
    else:
        flat_exits.append(p)

out(f"  Resolution exits (price→0 or →1): {len(resolution_exits)}")
if resolution_exits:
    res_pnl = sum(float(p.get("realized_pnl", 0)) for p in resolution_exits)
    res_wins = sum(1 for p in resolution_exits if float(p.get("realized_pnl", 0)) > 0.001)
    res_losses = sum(1 for p in resolution_exits if float(p.get("realized_pnl", 0)) < -0.001)
    out(f"    Win: {res_wins}, Loss: {res_losses}, P&L: ${res_pnl:+.4f}")

out(f"  Stop-loss exits (price drop): {len(stop_loss_exits)}")
if stop_loss_exits:
    sl_pnl = sum(float(p.get("realized_pnl", 0)) for p in stop_loss_exits)
    out(f"    P&L: ${sl_pnl:+.4f}")

out(f"  Profit-target exits (price gain): {len(profit_target_exits)}")
if profit_target_exits:
    pt_pnl = sum(float(p.get("realized_pnl", 0)) for p in profit_target_exits)
    out(f"    P&L: ${pt_pnl:+.4f}")

out(f"  Flat exits (no move): {len(flat_exits)}")
if flat_exits:
    fl_pnl = sum(float(p.get("realized_pnl", 0)) for p in flat_exits)
    out(f"    P&L: ${fl_pnl:+.4f}")

# Resolution exit details
if resolution_exits:
    out("\n  Resolution exit details:")
    for p in resolution_exits[:20]:
        ep = float(p["entry_price"])
        xp = float(p.get("exit_price", ep))
        pnl = float(p.get("realized_pnl", 0))
        outcome = "WIN ($1)" if xp > 0.5 else "LOSS ($0)"
        out(f"    Entry: ${ep:.3f} → Exit: ${xp:.3f} = ${pnl:+.4f} [{outcome}]")

# Duration analysis
out("\n" + "=" * 70)
out("HOLD DURATION ANALYSIS")
out("=" * 70)
durations = []
for p in closed:
    entry_t = float(p.get("entry_time", 0))
    exit_t = float(p.get("exit_time", 0))
    if entry_t > 0 and exit_t > 0:
        dur_min = (exit_t - entry_t) / 60
        durations.append((dur_min, p))

if durations:
    dur_vals = [d[0] for d in durations]
    out(f"  Min duration: {min(dur_vals):.1f} min")
    out(f"  Max duration: {max(dur_vals):.1f} min")
    out(f"  Mean duration: {sum(dur_vals)/len(dur_vals):.1f} min")
    
    # Duration buckets
    dur_buckets = {"< 2 min": 0, "2-10 min": 0, "10-60 min": 0, "1-12 hr": 0, "12-24 hr": 0, "> 24 hr": 0}
    for d in dur_vals:
        if d < 2: dur_buckets["< 2 min"] += 1
        elif d < 10: dur_buckets["2-10 min"] += 1
        elif d < 60: dur_buckets["10-60 min"] += 1
        elif d < 720: dur_buckets["1-12 hr"] += 1
        elif d < 1440: dur_buckets["12-24 hr"] += 1
        else: dur_buckets["> 24 hr"] += 1
    for b, c in dur_buckets.items():
        out(f"    {b:>10}: {c}")

# P&L by duration
out("\n  P&L by hold duration:")
for label, lo, hi in [("< 2 min", 0, 2), ("2-10 min", 2, 10), ("10-60 min", 10, 60), ("1-12 hr", 60, 720), ("12-24 hr", 720, 1440), ("> 24 hr", 1440, 999999)]:
    bucket_p = [(d, p) for d, p in durations if lo <= d < hi]
    if bucket_p:
        bpnl = sum(float(p.get("realized_pnl", 0)) for _, p in bucket_p)
        out(f"    {label:>10}: {len(bucket_p)} trades, P&L: ${bpnl:+.4f}")

# Top winners and losers
out("\n" + "=" * 70)
out("TOP 10 WINNERS")
out("=" * 70)
sorted_by_pnl = sorted(closed, key=lambda p: float(p.get("realized_pnl", 0)), reverse=True)
for p in sorted_by_pnl[:10]:
    ep = float(p["entry_price"])
    xp = float(p.get("exit_price", ep))
    pnl = float(p.get("realized_pnl", 0))
    qty = float(p.get("quantity", 0))
    out(f"  {p['position_id']:>8}: entry=${ep:.3f} exit=${xp:.3f} qty={qty:.1f} P&L=${pnl:+.4f}")

out("\n" + "=" * 70)
out("TOP 10 LOSERS")
out("=" * 70)
for p in sorted_by_pnl[-10:]:
    ep = float(p["entry_price"])
    xp = float(p.get("exit_price", ep))
    pnl = float(p.get("realized_pnl", 0))
    qty = float(p.get("quantity", 0))
    out(f"  {p['position_id']:>8}: entry=${ep:.3f} exit=${xp:.3f} qty={qty:.1f} P&L=${pnl:+.4f}")

# Core asymmetry analysis
out("\n" + "=" * 70)
out("CORE ASYMMETRY ANALYSIS")
out("=" * 70)
# For entries near 0.50: calculate win/loss when market resolves
near_50_entries = [(p, float(p["entry_price"])) for p in closed if 0.45 <= float(p["entry_price"]) <= 0.55]
out(f"Entries near $0.50 (0.45-0.55): {len(near_50_entries)} of {len(closed)} ({len(near_50_entries)/len(closed)*100:.1f}%)")
if near_50_entries:
    near50_pnl = sum(float(p.get("realized_pnl", 0)) for p, _ in near_50_entries)
    near50_wins = sum(1 for p, _ in near_50_entries if float(p.get("realized_pnl", 0)) > 0.001)
    near50_losses = sum(1 for p, _ in near_50_entries if float(p.get("realized_pnl", 0)) < -0.001)
    out(f"  Near-50 Win/Loss: {near50_wins}/{near50_losses}")
    if near50_wins + near50_losses > 0:
        out(f"  Near-50 Win Rate: {near50_wins/(near50_wins+near50_losses)*100:.1f}%")
    out(f"  Near-50 Total P&L: ${near50_pnl:+.4f}")
    
    # When entry ~0.50 and exit ~0 vs exit ~1
    resolved_50 = [(p, ep) for p, ep in near_50_entries if float(p.get("exit_price", 0.5)) < 0.05 or float(p.get("exit_price", 0.5)) > 0.95]
    if resolved_50:
        out(f"  Resolved to 0/1: {len(resolved_50)}")
        res_wins_50 = sum(1 for p, ep in resolved_50 if float(p.get("exit_price", 0.5)) > 0.95)
        res_losses_50 = sum(1 for p, ep in resolved_50 if float(p.get("exit_price", 0.5)) < 0.05)
        out(f"    Won (→$1): {res_wins_50}, Lost (→$0): {res_losses_50}")
        out(f"    For entry ~$0.50: Win pays ~$0.50, Loss costs ~$0.50")
        out(f"    Need >50% win rate AFTER fees to be profitable")

# Cost analysis
out("\n" + "=" * 70)
out("COST / SIZE ANALYSIS")
out("=" * 70)
costs = [float(p["entry_price"]) * float(p.get("quantity", 0)) for p in closed]
out(f"  Total cost (all entries): ${sum(costs):.2f}")
out(f"  Avg cost per trade: ${sum(costs)/len(costs):.2f}")
out(f"  Min cost: ${min(costs):.2f}")
out(f"  Max cost: ${max(costs):.2f}")

# Outcome analysis - Up vs Down
out("\n" + "=" * 70)
out("DIRECTIONAL ANALYSIS (from condition_id diversity)")
out("=" * 70)
unique_conditions = set(p["condition_id"] for p in closed)
out(f"  Unique condition_ids: {len(unique_conditions)}")
out(f"  Total closed trades: {len(closed)}")
out(f"  Avg trades per condition: {len(closed)/len(unique_conditions):.1f}" if unique_conditions else "  N/A")

# Write output
output = "\n".join(lines)
with open(OUTPUT_FILE, "w") as f:
    f.write(output)

print(f"Analysis written to {OUTPUT_FILE}")
