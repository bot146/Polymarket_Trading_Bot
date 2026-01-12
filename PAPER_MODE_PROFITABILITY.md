# Paper Mode Profitability Tracking

## Overview

Paper mode now includes **comprehensive profitability tracking** so you can validate that your strategies are working and profitable before risking real capital.

## What's Tracked

### Per-Trade Information
- Expected profit for each trade
- Cost (capital deployed)
- Real-time running totals

### Cumulative Statistics
- **Total Profit**: Sum of all expected profits
- **Total Cost**: Sum of all capital deployed
- **ROI**: Return on Investment percentage
- **By Strategy**: Breakdown showing which strategies are most profitable

## Example Output

When you execute a paper trade, you'll see:

```
📄 PAPER TRADE [arbitrage]: profit=$0.6000 cost=$9.70 confidence=95.00% trades=2
  Trade 1: BUY 10.00 @ $0.4800 token=yes1... type=FOK
  Trade 2: BUY 10.00 @ $0.4900 token=no1... type=FOK
  💰 Running Total: profit=$0.6000 cost=$9.70 ROI=6.19%
```

### Periodic Statistics Report

Every minute, the bot prints comprehensive statistics:

```
======================================================================
⏱️  UPTIME: 15.2 minutes
📊 SIGNALS: seen=47 executed=12
📈 EXECUTIONS: total=12 success=12 failed=0
💼 ACTIVE POSITIONS: 3
🎯 STRATEGIES: 2 enabled
──────────────────────────────────────────────────────────────────────
💰 PAPER TRADING PROFITABILITY:
   Total Profit: $8.4000
   Total Cost: $116.40
   ROI: 7.22%
   Strategy Breakdown:
     arbitrage: 8 trades, profit=$4.8000, ROI=5.50%
     guaranteed_win: 4 trades, profit=$3.6000, ROI=10.29%
======================================================================
```

## How It Works

### Expected Profit Calculation

For each strategy:

1. **Arbitrage**: `profit = 1 - (yes_ask + no_ask)` × shares
   - Example: YES at $0.48, NO at $0.49 = $0.03 edge × 10 shares = $0.30 profit

2. **Guaranteed Win**: `profit = 1 - winning_share_price` × shares
   - Example: Winner at $0.85 = $0.15 edge × 20 shares = $3.00 profit

3. **Statistical Arbitrage**: Based on divergence convergence expectation

### Cost Tracking

Cost represents the total capital deployed:
- For arbitrage: `(yes_price + no_price) × shares`
- For guaranteed win: `price × shares`

### ROI Calculation

`ROI = (Total Profit / Total Cost) × 100%`

This shows the percentage return on capital deployed.

## Using Profitability Data

### Decision Making

1. **Run in Paper Mode First** (24-48 hours recommended)
   ```bash
   TRADING_MODE=paper python -m polymarket_bot.app_multi
   ```

2. **Monitor Key Metrics**:
   - Overall ROI should be positive (>0%)
   - Individual strategy performance
   - Number of opportunities found vs executed

3. **Evaluate Performance**:
   - **Good**: ROI >5%, consistent opportunities
   - **Acceptable**: ROI >2%, growing over time
   - **Needs Adjustment**: ROI <1% or negative

4. **Go Live When Confident**:
   ```bash
   TRADING_MODE=live python -m polymarket_bot.app_multi
   ```

### Understanding the Numbers

**Expected vs Actual Profit**

⚠️ **Important**: Paper mode shows *expected* profit assuming:
- Orders fill at the prices shown
- No slippage
- No partial fills
- Market doesn't move between signal and execution

In live trading:
- Actual profit may be lower due to fees (~0.2-0.5%)
- Slippage on large orders
- Leg risk (one side fills, other doesn't)
- Market movement

**Use paper mode ROI as an upper bound.** A good rule of thumb:
- Paper ROI of 5-10% → Live ROI of 3-7%
- Paper ROI of 10-20% → Live ROI of 7-15%

## Demo Script

Run the included demo to see profitability tracking in action:

```bash
python demo_profitability.py
```

This simulates several trades and shows how profitability is tracked and reported.

## Strategy-Specific Notes

### Arbitrage
- Most consistent, predictable profits
- Lower ROI (typically 3-7%) but high confidence
- Paper mode very close to live performance

### Guaranteed Win
- Highest ROI (10-20%+) when opportunities exist
- Time-sensitive (disappear quickly)
- Paper mode may overestimate if execution is slow

### Statistical Arbitrage
- Medium ROI (5-15%) but requires convergence
- Higher risk - correlations can break
- Paper mode shows potential, actual depends on timing

## Best Practices

1. **Start Conservative**:
   - Set `MAX_ORDER_USDC=10` initially
   - Enable only arbitrage and guaranteed_win
   - Monitor for 24+ hours

2. **Look for Patterns**:
   - What times of day have most opportunities?
   - Which strategies perform best?
   - Are there consistent markets to target?

3. **Validate Profitability**:
   - ROI should be positive and stable
   - Should see regular opportunities (not just 1-2)
   - Individual trades should make sense

4. **Scale Gradually**:
   - Start live with small capital ($100-$500)
   - Increase position sizes as confident
   - Add more strategies once comfortable

## Troubleshooting

### "No trades executed"
- Check `KILL_SWITCH` is set to 0
- Verify `MIN_EDGE_CENTS` isn't too high
- Ensure strategies are enabled

### "ROI is negative"
- Check if `MIN_EDGE_CENTS` is accounting for fees
- May need to increase edge threshold
- Review individual trades for patterns

### "Very few opportunities"
- Scanner now fetches ALL markets (no limit) - check if markets are being filtered out
- Lower `min_volume` threshold in orchestrator config
- Enable more strategies (arbitrage, guaranteed_win, stat_arb)
- Run during high-activity times (US market hours)
- Verify network connectivity to Polymarket APIs

## Conclusion

Paper mode profitability tracking gives you **confidence** before going live:

✅ Validates strategies are finding opportunities  
✅ Shows expected profitability  
✅ Identifies best-performing strategies  
✅ Helps tune parameters  
✅ Risk-free learning and testing  

Use it to ensure your bot is working and profitable before deploying real capital!
