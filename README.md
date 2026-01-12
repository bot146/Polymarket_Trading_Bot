# Polymarket Trading Bot (Multi-Strategy)

A **production-grade** Polymarket trading bot with **multiple proven strategies** for consistent profitability. Built for reliability, featuring robust error handling, precise order execution, and graceful recovery from network issues.

## 🚀 What's New: Production-Ready Multi-Strategy System

This bot features a sophisticated multi-strategy architecture inspired by successful traders who've made hundreds of thousands to millions in profit, now enhanced with production-grade reliability:

### Available Strategies

1. **Arbitrage** 🔄 - YES+NO hedge arbitrage when sum < $1
   - Trader "distinct-baguette": **$242k in 1.5 months**
   - Lock guaranteed profit regardless of outcome

2. **Guaranteed Win Detection** 💎 - Buy resolved market winners below $1
   - Example: Football games complete, winning shares at $0.45
   - Critical urgency execution (these disappear fast!)

3. **Statistical Arbitrage** 📊 - Trade correlated market divergences
   - Trader "sharky6999": **$480k** scanning 100+ markets/minute
   - Long cheap, short expensive, profit on convergence

4. **Spread Farming** 💰 (Coming Soon) - High-frequency market making
   - Trader "cry.eth2": **$194k** with 1M trades

5. **AI Probability Models** 🤖 (Future) - ML-driven predictions
   - Trader "ilovecircle": **$2.2M** in 2 months with 74% accuracy

## ✨ Key Features

- **Modular Strategy Framework**: Easy to add/remove strategies
- **Priority-Based Execution**: Urgent opportunities first
- **Real-Time Market Scanner**: Discovers opportunities across ALL markets
- **Production-Grade Reliability**:
  - HTTP timeout configuration prevents indefinite hangs
  - Cloudflare block detection and automatic cooldown
  - Precise order quantization (avoids venue rejections)
  - Structured error handling with actionable feedback
  - WebSocket resilience (handles both list and dict message formats)
- **Risk Management**: Position limits, kill switches, paper trading
- **Comprehensive Testing**: 23 passing tests
- **Production-Ready**: Logging, error handling, graceful shutdown

## 📚 Documentation

- **[STRATEGY_GUIDE.md](STRATEGY_GUIDE.md)** - Comprehensive guide to all strategies, architecture, and development
- **[Original README](#original-bot)** - Basic setup and CLOB integration details below

## Fast start (Multi-Strategy Bot)

### 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your settings
```

### 3. Run

```bash
# Paper mode (safe, simulated trades)
python -m polymarket_bot.app_multi

# Live mode (set TRADING_MODE=live in .env first)
python -m polymarket_bot.app_multi
```

## 🎯 Strategy Configuration

In `src/polymarket_bot/app_multi.py`:

```python
orch_config = OrchestratorConfig(
    scan_interval=2.0,              # Scan every 2 seconds
    max_concurrent_trades=5,        # Max 5 positions
    enable_arbitrage=True,          # Classic arbitrage
    enable_guaranteed_win=True,     # Resolved markets
    enable_stat_arb=False,          # Advanced (disabled by default)
    min_volume=Decimal("5000"),     # Min $5k volume
)
```

## 🛡️ Safety Features

- **Kill Switch**: `KILL_SWITCH=1` stops all trading instantly
- **Paper Mode**: Default mode - no real trades
- **Position Limits**: Max concurrent positions configurable
- **Order Size Caps**: `MAX_ORDER_USDC=20` limits exposure
- **Edge Buffers**: `MIN_EDGE_CENTS` ensures profit after fees

## 🧪 Testing

```bash
# Run all tests
pytest tests/ -v

# All tests passing ✅
# 23 passed in 0.03s
```

## 📊 Performance Tracking

The bot logs real-time statistics:

```
⏱️  UPTIME: 15.2 minutes
📊 SIGNALS: seen=47 executed=3
📈 EXECUTIONS: total=3 success=3 failed=0
💼 ACTIVE POSITIONS: 2
🎯 STRATEGIES: 2 enabled
```

## 🏗️ Architecture

```
src/polymarket_bot/
├── app_multi.py              # Multi-strategy application ⭐
├── orchestrator.py           # Strategy coordination ⭐
├── scanner.py                # Market discovery ⭐
├── strategy.py               # Framework base classes ⭐
├── unified_executor.py       # Trade execution ⭐
├── strategies/               # Strategy implementations ⭐
│   ├── arbitrage_strategy.py
│   ├── guaranteed_win_strategy.py
│   └── statistical_arbitrage_strategy.py
├── app.py                    # Original simple arbitrage
├── clob_client.py           # CLOB API client
├── wss.py                   # WebSocket feeds
├── config.py                # Configuration
└── executor.py              # Legacy executor
```

⭐ = New multi-strategy components

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
