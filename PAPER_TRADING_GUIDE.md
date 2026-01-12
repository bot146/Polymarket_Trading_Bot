# Paper Trading Guide: Realistic Offline Trading Simulation

## Overview

Paper trading mode in this bot is a **full-fidelity simulation** of live trading. It doesn't just log what would have happened—it actually:

✅ **Tracks Real Positions**: Opens, manages, and closes positions just like live trading  
✅ **Calculates Actual P&L**: Tracks realized and unrealized profit/loss  
✅ **Monitors Market Events**: Detects resolutions and marks positions as redeemable  
✅ **Follows Real Data**: Uses live market data for realistic simulation  
✅ **Scans ALL Markets**: No artificial limits on market scanning (configurable)

This allows you to **verify the bot can trade successfully** and measure **actual profitability** before risking real capital.

## How Paper Trading Works

### 1. Position Lifecycle

When the bot identifies a trading opportunity in paper mode:

```python
# Bot sees arbitrage opportunity
Signal: BUY 10 shares YES @ $0.48, BUY 10 shares NO @ $0.49

# Position Manager tracks this
Position 1: YES token, 10 shares @ $0.48 entry, cost basis = $4.80
Position 2: NO token, 10 shares @ $0.49 entry, cost basis = $4.90
Total investment: $9.70
Expected profit: $0.30 (since YES + NO = $0.97 < $1.00)
```

### 2. Position Tracking

All positions are stored in `~/.polymarket_bot/positions.json` and include:

- **Entry Details**: Price, quantity, timestamp, order ID
- **Status**: OPEN, CLOSING, CLOSED, REDEEMABLE
- **P&L Tracking**: Cost basis, unrealized P&L, realized P&L
- **Market Info**: Condition ID, token ID, outcome (YES/NO)
- **Strategy**: Which strategy opened the position

### 3. Market Resolution Monitoring

The bot continuously monitors markets for resolution:

```
Market resolves → Winning outcome detected → Position marked REDEEMABLE
Winning shares worth $1.00 each → Unrealized P&L updated
```

### 4. Position Closing

Positions can be closed through:

- **Manual close**: Sell at current market price
- **Auto-close**: On favorable price movement
- **Redemption**: Market resolved, redeem winning shares for $1.00

### 5. Profitability Calculation

The bot tracks two types of profitability:

**Expected (Theoretical)**:
- Based on signal edge at entry time
- Assumes perfect execution
- Shown as "paper_total_profit"

**Actual (Position-based)**:
- Based on real position tracking
- Includes slippage, market movement
- Shown as "realized_pnl" and "unrealized_pnl"

## Configuration for Maximum Market Coverage

### Scanning ALL Polymarket Markets

The bot is configured to scan all available markets by default:

```bash
# .env file
MARKET_FETCH_LIMIT=10000    # Very high limit (API may have lower cap)
MIN_MARKET_VOLUME=5000      # Minimum volume filter (in USDC)
```

**Important Notes**:

1. **No Artificial Limits**: The code uses `limit=None` everywhere, fetching up to `MARKET_FETCH_LIMIT`
2. **API Constraints**: Polymarket's API may have its own limits (typically 1000-3000 active markets)
3. **Filtering is Post-Fetch**: Markets are filtered AFTER fetching (by volume, resolution status, etc.)
4. **Comprehensive Logging**: Bot logs exactly how many markets were fetched and filtered

To scan even more markets (if API allows):

```bash
# Increase the limit
MARKET_FETCH_LIMIT=20000  # or higher

# Lower the volume threshold to include more markets
MIN_MARKET_VOLUME=1000    # or even 0 for all markets
```

### Monitoring Market Coverage

When the bot starts, it logs:

```
INFO: Market Fetch Limit: 10000 (0=unlimited within API constraints)
INFO: Min Market Volume: $5,000
INFO: Fetched 1,247 markets from Gamma API (requested_limit=10000, active_only=True, parse_errors=0)
INFO: High-volume market filter: 1,247 total markets -> 823 markets with volume >= $5,000
```

If you see a warning like:

```
WARNING: Retrieved 9,990 markets, close to limit of 10000. 
         There may be more markets available. Consider increasing MARKET_FETCH_LIMIT if needed.
```

Then increase `MARKET_FETCH_LIMIT` in your `.env` file.

## Running Paper Trading

### Quick Start

```bash
# 1. Install
pip install -e .

# 2. Configure (paper mode is default)
cp .env.example .env
# Edit .env: set KILL_SWITCH=0, adjust market limits if needed

# 3. Run
python -m polymarket_bot.app_multi
```

### What You'll See

```
╔══════════════════════════════════════════════════════════════╗
║     POLYMARKET MULTI-STRATEGY TRADING BOT                   ║
║     Full Trade Lifecycle: Entry → Monitoring → Exit         ║
╚══════════════════════════════════════════════════════════════╝

Mode: PAPER
Kill Switch: DISABLED
Market Fetch Limit: 10000 (0=unlimited within API constraints)
Min Market Volume: $5,000

✅ Position manager initialized (0 positions loaded)
✅ CLOB client initialized
✅ Resolution monitor initialized
✅ Position closer initialized
🚀 Bot initialized with full trade lifecycle. Starting main loop...

📊 Fetched 1,247 markets from Gamma API
📊 High-volume market filter: 1,247 total markets -> 823 markets
🔍 Found 3 signals across all strategies

📄 PAPER TRADE [arbitrage]: profit=$0.3000 cost=$9.70 confidence=95.00%
  Trade 1: BUY 10.00 @ $0.4800 token=yes123... type=FOK
  Trade 2: BUY 10.00 @ $0.4900 token=no456... type=FOK
  💰 Expected Profit Total: profit=$0.3000 cost=$9.70 ROI=3.09%
  💼 Actual Portfolio: realized=$0.00 unrealized=$0.00 total=$0.00

Opened position pos_1: YES 10 @ $0.48 (condition=abc12345...)
Opened position pos_2: NO 10 @ $0.49 (condition=abc12345...)
```

### Monitoring Performance

Every minute, you'll see comprehensive statistics:

```
======================================================================
⏱️  UPTIME: 15.2 minutes
📊 SIGNALS: seen=47 executed=12
📈 EXECUTIONS: total=12 success=12 failed=0
🎯 STRATEGIES: 2 enabled
──────────────────────────────────────────────────────────────────────
💼 PORTFOLIO:
   Open Positions: 8
   Closed Positions: 4
   Redeemable: 2
   Cost Basis: $116.40
──────────────────────────────────────────────────────────────────────
🎯 RESOLUTION MONITOR:
   Resolved Markets: 2
   Redeemable Value: $20.00
──────────────────────────────────────────────────────────────────────
💰 PROFITABILITY:
   Expected (theoretical):
     Profit: $8.4000
     Cost: $116.40
     ROI: 7.22%
   Actual (from positions):
     Realized P&L: $4.2000
     Unrealized P&L: $3.8000
     Total P&L: $8.0000
     Realized ROI: 3.61%
   Closed: 4 positions
   Redeemed: 2 positions
   Total Realized: $4.2000
──────────────────────────────────────────────────────────────────────
📊 STRATEGY BREAKDOWN:
   arbitrage: 8 trades, expected profit=$4.8000, ROI=5.50%
   guaranteed_win: 4 trades, expected profit=$3.6000, ROI=10.29%
======================================================================
```

## Validating Bot Performance

### Before Going Live

Run paper trading for **24-48 hours** minimum. Look for:

✅ **Positive ROI**: Actual ROI should be positive and growing
✅ **Regular Opportunities**: Bot should find multiple signals per hour
✅ **Position Management**: Positions should open, update, and close correctly
✅ **Market Coverage**: Logs should show thousands of markets scanned
✅ **Resolution Tracking**: Resolved markets should be detected and marked redeemable

### Red Flags

🚩 **Zero or negative actual P&L**: Strategy parameters need tuning
🚩 **Very few markets scanned** (< 100): Increase MARKET_FETCH_LIMIT or lower MIN_MARKET_VOLUME
🚩 **No signals found**: Lower MIN_EDGE_CENTS or enable more strategies
🚩 **Positions never close**: Check position_closer logic or market liquidity

### Success Criteria

Before going live, you should see:

- **Actual ROI > 3%** (after accounting for slippage)
- **Multiple successful trades** per day
- **Positions resolving correctly** when markets complete
- **Comprehensive market scanning** (1000+ markets)
- **Clean error logs** (no repeated failures)

## Understanding the Metrics

### Expected vs Actual Profit

**Expected (Theoretical)**:
- Calculated at signal generation time
- Assumes perfect execution at shown prices
- Upper bound estimate
- Useful for strategy comparison

**Actual (Position-based)**:
- Tracks real position lifecycle
- Includes all market dynamics
- More conservative estimate
- **Use this for go-live decisions**

### Why Actual < Expected

In paper trading, actual profit is often lower than expected because:

1. **Market Movement**: Prices change between signal and "execution"
2. **Simulated Slippage**: Paper mode may assume some slippage
3. **Partial Fills**: May simulate partial order fills
4. **Time Decay**: Positions held longer than optimal

**This is realistic and expected!** Live trading will have similar challenges.

## Transitioning to Live Trading

Once paper trading shows consistent profitability:

1. **Start Small**: Set `MAX_ORDER_USDC=10` for first live trades
2. **Monitor Closely**: Watch first few live trades carefully
3. **Compare Results**: Paper ROI should approximate live ROI
4. **Scale Gradually**: Increase position sizes as confidence grows

```bash
# Going live
# 1. Set credentials in .env
POLY_PRIVATE_KEY=your_key_here
POLY_FUNDER_ADDRESS=your_address_here

# 2. Enable live mode
TRADING_MODE=live

# 3. Start small
MAX_ORDER_USDC=10

# 4. Run
python -m polymarket_bot.app_multi
```

## Troubleshooting

### "Only seeing ~500 markets"

This is likely due to:

1. **Volume filtering**: Increase MARKET_FETCH_LIMIT or lower MIN_MARKET_VOLUME
2. **API pagination**: Polymarket API might paginate results (we request all at once)
3. **Active markets only**: Set `active_only=False` to see closed markets too

Check the logs for the exact number fetched:

```
INFO: Fetched 523 markets from Gamma API (requested_limit=10000, active_only=True)
```

If it's close to a round number (500, 1000), that's the API limit.

### "No positions are opening"

Check:
- `KILL_SWITCH=0` (not 1)
- Bot is finding signals (check "Found X signals" in logs)
- No validation errors (check for "validation_failed" messages)

### "P&L not updating"

Paper trading P&L updates require:
- Position manager is initialized
- Markets are being rescanned (for current prices)
- Resolution monitor is running (for market completions)

## Advanced: Custom Position Management

You can inspect and manage positions programmatically:

```python
from polymarket_bot.position_manager import PositionManager
from pathlib import Path

# Load positions
storage_path = Path.home() / ".polymarket_bot" / "positions.json"
manager = PositionManager(storage_path=str(storage_path))

# View all positions
for pos_id, pos in manager.positions.items():
    print(f"{pos_id}: {pos.outcome} {pos.quantity} shares @ ${pos.entry_price}")
    print(f"  Status: {pos.status}, Unrealized P&L: ${pos.unrealized_pnl}")

# Get portfolio stats
stats = manager.get_portfolio_stats()
print(f"Total P&L: ${stats['total_pnl']:.2f}")
print(f"Open positions: {stats['open_positions']}")
```

## Summary

Paper trading in this bot is **production-grade simulation** that:

- ✅ Tracks actual positions with full lifecycle
- ✅ Calculates real P&L (not just theoretical)
- ✅ Monitors market events and resolutions
- ✅ Scans all available markets (no artificial limits)
- ✅ Provides comprehensive profitability metrics

Use it to **validate your bot is profitable** before risking real money. The "actual" P&L from position tracking is your best indicator of live trading performance.

**Pro Tip**: Paper trade for at least 24-48 hours and aim for actual ROI > 3% before going live!
