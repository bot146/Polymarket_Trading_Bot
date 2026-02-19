# Polymarket Trading Bot (Multi-Strategy)

## Agent Excerpt (Quick Understanding)

This repository runs a multi-strategy Polymarket trading system with a shared orchestrator, unified executor, and strict risk rails. It supports both live trading and an offline paper mode that mirrors fills into a paper wallet and tracks full trade lifecycle P&L (open, unrealized, resolved, redeemed).

This bot features a sophisticated multi-strategy architecture inspired by successful traders who've made hundreds of thousands to millions in profit, now enhanced with production-grade reliability:

- Multiple strategy modules under one execution/risk framework
- Paper + live modes with consistent position tracking
- Resolution monitoring and redemption flow for Actual P&L
- Circuit breaker, kill switch, inventory/risk limits, and requote controls

## Core Features

- **Strategy Engine**: Arbitrage, guaranteed-win, statistical, market-making, and additional optional strategies
- **Execution Layer**: Unified paper/live execution with order lifecycle tracking
- **Paper Wallet**: Offline mirrored trading with realized/unrealized P&L and resolution-aware redemption
- **Reliability**: Structured logging, retry/timeouts, graceful shutdown, and defensive parsing
- **Safety Rails**: Kill switch, max exposure rules, position limits, and circuit breaker

## Fast Start

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -U pip
pip install -e ".[dev]"
copy .env.example .env
python -m polymarket_bot.app_multi
```

## Docs

- [STRATEGY_GUIDE.md](STRATEGY_GUIDE.md)
- [PAPER_TRADING_GUIDE.md](PAPER_TRADING_GUIDE.md)
- [PAPER_MODE_PROFITABILITY.md](PAPER_MODE_PROFITABILITY.md)
- [Original README](#original-bot)

---

## Original Bot

Below is the documentation for the original single-strategy arbitrage bot.

<a name="original-bot"></a>

### Original Features

This repo is intentionally built in layers:

- **Feeds**: real-time market data via CLOB WebSocket
- **Strategies**: start with *YES+NO hedge arbitrage* (sum of best asks < $1)
- **Execution**: place orders with hard risk caps, idempotency, and kill-switch
- **Modes**: paper trading by default, live trading when explicitly enabled

### Fast start (paper mode)

1. Create a virtualenv and install.

```zsh
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
```

2. Copy env template.

```zsh
cp .env.example .env
```

3. Run the bot (paper mode).

```zsh
python -m polymarket_bot.app
```

### Going live (Magic / Polymarket email) — recommended first

You will need:

- `POLY_PRIVATE_KEY` (Magic key) from <https://reveal.magic.link/polymarket>
- `POLY_FUNDER_ADDRESS` (your Polymarket proxy address you fund with USDC)
- `POLY_SIGNATURE_TYPE=1`

Then set:

- `TRADING_MODE=live`

The bot will **refuse** to trade live unless `TRADING_MODE=live` is set.

### Going live (EOA)

Set:

- `POLY_SIGNATURE_TYPE=0`

Fund your EOA with USDC and (depending on trading flow) MATIC for gas.

### Safety principles

- Start small: default `MAX_ORDER_USDC=20`
- Require real edge: `MIN_EDGE_CENTS` (buffer for fees/slippage/leg risk)
- Prefer immediate-or-cancel execution when hedging
- Kill switch via env var

### Repo layout

- `src/polymarket_bot/` – application code
- `tests/` – unit tests

## Disclaimer

This is financial software. Markets can move, fills can be partial, and fees/slippage can erase apparent arbitrage. Use at your own risk.
