# Polymarket Trading Bot — Project Context

## Overview
Automated trading bot for Polymarket prediction markets. Scans for structural
mispricings (arbitrage, conditional probability violations, near-resolution
opportunities) and executes paper or live trades via the CLOB API.

## Architecture

### Entrypoints
- **`python -m polymarket_bot.app_multi`** — Main bot (multi-strategy orchestrator). This is the ONLY entrypoint to use.
- `app.py` is a legacy single-pair scanner — do NOT use.

### Core modules (`src/polymarket_bot/`)
| Module | Purpose |
|---|---|
| `config.py` | `Settings` dataclass + `load_settings()` from `.env` |
| `orchestrator.py` | `StrategyOrchestrator` — scans markets, collects signals, prioritizes, dispatches |
| `unified_executor.py` | `UnifiedExecutor` — paper & live order execution, FOK/GTC handling |
| `scanner.py` | `MarketScanner` — Gamma API market fetching, resolution-time filtering |
| `position_manager.py` | `PositionManager` — position tracking, P&L, persistence to `~/.polymarket_bot/positions.json` |
| `position_closer.py` | `PositionCloser` — exit rules (stop-loss, profit-target, age-based, resolution) |
| `paper_trading.py` | `PaperBlotter` — in-memory order blotter for paper-mode fill simulation |
| `paper_wallet.py` | `PaperWalletController` — virtual wallet with tier-based sizing |
| `dashboard.py` | Dash web UI at `http://127.0.0.1:8050/` |
| `circuit_breaker.py` | Daily loss / drawdown / consecutive-loss protection |
| `resolution_monitor.py` | Polls Gamma API for market resolution events |
| `clob_client.py` | `build_clob_client()` wrapper for py-clob-client |

### Strategies (`src/polymarket_bot/strategies/`)
| Strategy | Key | Description |
|---|---|---|
| `conditional_arb` | `ENABLE_CONDITIONAL_ARB` | Cumulative bracket probability violations in neg-risk groups |
| `multi_outcome_arb` | `ENABLE_MULTI_OUTCOME_ARB` | Buy all brackets when sum < $1 (guaranteed $1 payout) |
| `arbitrage` | `ENABLE_ARBITRAGE` | YES+NO on same market sums > $1 |
| `guaranteed_win` | `ENABLE_GUARANTEED_WIN` | Riskless YES+NO when sum < $1 |
| `near_resolution` | `ENABLE_NEAR_RESOLUTION` | Markets about to resolve with mispriced outcomes |
| `liquidity_rewards` | `ENABLE_LIQUIDITY_REWARDS` | Maker orders to earn liquidity rewards |
| `sniping` | `ENABLE_SNIPING` | Large / fast price moves |
| `market_making` | `ENABLE_MARKET_MAKING` | Spread capture on stable markets |

### Data flow
1. `MarketScanner` fetches high-volume markets from Gamma API
2. `MarketScanner.get_short_duration_markets()` fetches recurring/5-min markets by creation time
3. Resolution-time filter narrows to configured window (paper: 72h, live: 30d)
4. `StrategyOrchestrator.scan_and_collect_signals()` feeds markets to each enabled strategy
5. `prioritize_signals()` scores by composite (edge × weight + time × weight)
6. `filter_actionable_signals()` deduplicates, checks stacking limits
7. `UnifiedExecutor.execute_signal()` places paper/live orders
8. `PositionCloser` checks exits every 15s; `ResolutionMonitor` checks resolutions every 60s

## Key Design Decisions

### P&L Calculation
- **Arb strategies** (`multi_outcome_arb`, `conditional_arb`): Use group-level P&L.
  Positions are held to resolution, NOT marked to market via mid-prices.
  Group value = $1.00 × qty per execution (one bracket wins).
  This prevents misleading unrealized losses caused by wide bid-ask spreads on illiquid brackets.
- **All other strategies**: Standard mark-to-market using WSS mid-prices.

### Paper Mode Clean Restart
When `PAPER_RESET_ON_START=true` (default), every restart:
- Deletes `positions.json` and writes empty state
- Resets wallet to `PAPER_START_BALANCE`
- Clears orchestrator's `active_positions` list
This prevents stale positions from blocking new signals via "max stacks reached".

### Paper vs Live Resolution Window
- Paper mode uses `PAPER_RESOLUTION_MAX_HOURS` (default 72h) to focus on
  markets that resolve quickly, so we capture actual realized P&L for testing.
- Live mode uses `RESOLUTION_MAX_DAYS` (default 30d) for broader opportunity set.

### Short-Duration Markets (5-min crypto, sports, esports)
- `get_short_duration_markets()` fetches by `createdAt` desc, bypassing volume filter
- These markets have $0 volume at creation but $9k-$18k AMM liquidity
- Identified by series slug (`-5m`, `-15m`), question pattern ("up or down"),
  or crypto fee type (`crypto_15_min`)
- **10% taker fee** (1000 bps) vs standard 2% — important for edge calculation
- `endDate` field preferred over `endDateIso` for precise resolution timing
- `MarketInfo` now includes: `series_ticker`, `event_start_time`, `fee_type`
- Active series: BTC, ETH, SOL, XRP (4 × ~39 markets/day = ~155 total)
- Sports markets (`sports_fees`) and esports also visible but not matched by scanner

## Wallet & Credentials
- **Proxy wallet**: `0x0df18f2e85aa500635ec19504f3713fdbe0754cc`
- **EOA signer**: `0x759f7E384C1B8c2edc49599E5e16eD8594E91b16`
- **Signature type**: 1 (POLY_PROXY)
- **Chain**: Polygon (137)
- Private key in `.env` — starts with `0xe8f909d3ca`
- Live USDC balance: ~$32 (proxy wallet)
- Paper starting balance: $40

## State Files
All persisted under `~/.polymarket_bot/`:
- `positions.json` — open/closed positions
- `paper_wallet.json` — wallet config (balance, tiers)
- `bot.log` / `bot_err.log` — logs

## Testing
```bash
.venv\Scripts\python.exe -m pytest tests/ -v
```
140 tests covering: strategies, position management, paper fills, P&L accuracy,
resolution filtering, priority scoring, clean restart, risk rails,
short-duration market scanning.

## Common Pitfalls
1. **Don't use `app.py`** — it's the old single-pair entrypoint
2. **Kill stale Python processes** before restarting (port 8050 conflict)
3. **`.env` is NOT in `.gitignore`** — contains private key (fix pending)
4. **Conditional arb buys bracket subsets** — positions must be grouped by
   `condition_id` for P&L, not individually marked to market
5. **WSS feed** only starts after first scan cycle — first iteration may use Gamma prices
6. **`max_arb_stacks`** limits how many times the bot can re-enter the same condition_id

## .env Key Settings
| Setting | Current | Purpose |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `PAPER_START_BALANCE` | `40` | Virtual starting balance |
| `PAPER_RESOLUTION_MAX_HOURS` | `72` | Paper-only: max hours to resolution |
| `RESOLUTION_MAX_DAYS` | `30` | Live: max days to resolution |
| `MAX_CONCURRENT_TRADES` | `20` | Max simultaneous positions |
| `MAX_ARB_STACKS` | `3` | Max stacked entries per condition |
| `MAX_ORDER_USDC` | `20` | Max order size |
| `MIN_EDGE_CENTS` | `0.5` | Minimum edge to act on |
| `PAPER_RESET_ON_START` | `true` | Clean restart each time |
| `ENABLE_SHORT_DURATION_SCAN` | `true` | Scan for 5-min crypto markets |
| `SHORT_DURATION_MIN_LIQUIDITY` | `500` | Min liquidity for short-duration markets |
