# Polymarket Multi-Strategy Trading Bot 🚀

A sophisticated, production-ready Polymarket trading bot featuring multiple proven strategies for consistent profitability. Built to start with as little as $100 and scale through intelligent strategy orchestration.

## 🎯 Vision

This isn't just a trading bot—it's a comprehensive trading system designed with the "ultrathink" philosophy: elegant, modular, and powerful. Every component is crafted to be intuitive, scalable, and profitable.

## ✨ Features

### Multi-Strategy Architecture

The bot employs multiple battle-tested strategies that have generated significant profits for traders:

1. **Arbitrage Strategy** 🔄
   - Classic YES+NO hedge arbitrage
   - Targets markets where YES_ask + NO_ask < $1
   - Locks in guaranteed profit regardless of outcome
   - **Real trader**: "distinct-baguette" made $242k in 1.5 months

2. **Guaranteed Win Detection** 💎
   - Monitors resolved markets for mispriced winning shares
   - Buys winning shares trading below $1 for instant profit
   - Critical urgency execution (these disappear fast!)
   - **Example**: Football games complete, winning shares at $0.45

3. **Statistical Arbitrage** 📊
   - Identifies correlated market pairs that diverge
   - Example: "Trump wins" vs "GOP Senate control"
   - Long cheap market, short expensive market, profit on convergence
   - **Real trader**: "sharky6999" made $480k scanning 100+ markets/minute

4. **Spread Farming** (Coming Soon) 💰
   - Market making: buy at bid, sell at ask
   - High-frequency trading via CLOB API
   - **Real trader**: "cry.eth2" made $194k with 1M trades

5. **AI Probability Models** (Future Enhancement) 🤖
   - ML models estimate real odds from news/social data
   - Trade when model probability diverges from market price
   - **Real trader**: "ilovecircle" made $2.2M with 74% accuracy

### Architecture Highlights

- **Modular Strategy Framework**: Easy to add new strategies
- **Priority-Based Execution**: Urgent opportunities executed first
- **Risk Management**: Position limits, kill switches, paper trading
- **Real-Time Data**: WebSocket integration for live market feeds
- **Market Scanner**: Discovers opportunities across all Polymarket markets
- **Comprehensive Testing**: Full test suite for reliability

## 🚀 Quick Start

### 1. Installation

```bash
# Clone and setup
git clone <repo-url>
cd Polymarket_Trading_Bot

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -U pip
pip install -e ".[dev]"
```

### 2. Configuration

```bash
# Copy environment template
cp .env.example .env

# Edit .env with your settings
nano .env
```

Required settings for live trading:
- `POLY_PRIVATE_KEY`: Your Magic key from https://reveal.magic.link/polymarket
- `POLY_FUNDER_ADDRESS`: Your Polymarket proxy address (fund with USDC)
- `POLY_SIGNATURE_TYPE`: Set to `1` for Magic/proxy
- `TRADING_MODE`: Set to `live` for real trading (default: `paper`)

### 3. Run the Bot

```bash
# Paper trading (safe, no real trades)
python -m polymarket_bot.app_multi

# Live trading (after testing in paper mode)
# Set TRADING_MODE=live in .env first
python -m polymarket_bot.app_multi
```

**💡 Paper Mode Profitability Tracking**: Paper mode now tracks all simulated trades and shows comprehensive profitability statistics including total profit, ROI, and per-strategy breakdown. See [PAPER_MODE_PROFITABILITY.md](PAPER_MODE_PROFITABILITY.md) for details.

## 📊 Strategy Configuration

The orchestrator can be configured in `src/polymarket_bot/app_multi.py`:

```python
orch_config = OrchestratorConfig(
    scan_interval=2.0,              # Seconds between scans
    max_concurrent_trades=5,        # Max simultaneous positions
    enable_arbitrage=True,          # Enable YES+NO arbitrage
    enable_guaranteed_win=True,     # Enable resolved market scanning
    enable_stat_arb=False,          # Statistical arbitrage (advanced)
    min_volume=Decimal("5000"),     # Min market volume to consider
)
```

## 🛡️ Safety & Risk Management

### Built-in Safety Features

- **Kill Switch**: Set `KILL_SWITCH=1` to halt all trading instantly
- **Paper Mode**: Default mode simulates trades without real execution
- **Position Limits**: Configurable max concurrent positions
- **Order Size Caps**: `MAX_ORDER_USDC` limits per-order capital
- **Edge Buffers**: `MIN_EDGE_CENTS` ensures real profit after fees

### Risk Controls

```env
MAX_ORDER_USDC=20        # Max $ per order
MIN_EDGE_CENTS=1.5       # Min profit edge (cents)
EDGE_BUFFER_CENTS=0.5    # Extra buffer for fees/slippage
```

## 📁 Project Structure

```
src/polymarket_bot/
├── app_multi.py              # Main multi-strategy application
├── orchestrator.py           # Strategy coordination
├── scanner.py                # Market discovery & data fetching
├── strategy.py               # Strategy framework base classes
├── unified_executor.py       # Trade execution engine
├── strategies/
│   ├── arbitrage_strategy.py
│   ├── guaranteed_win_strategy.py
│   └── statistical_arbitrage_strategy.py
├── clob_client.py           # CLOB API client
├── wss.py                   # WebSocket feeds
├── config.py                # Configuration management
└── logging.py               # Logging setup

tests/
├── test_strategy.py
├── test_arbitrage_strategy.py
├── test_guaranteed_win_strategy.py
└── test_arbitrage.py        # Original tests
```

## 🧪 Testing

```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_arbitrage_strategy.py -v

# Run with coverage
pytest tests/ --cov=polymarket_bot --cov-report=html
```

## 📈 Performance Tracking

The bot logs detailed statistics:

```
⏱️  UPTIME: 15.2 minutes
📊 SIGNALS: seen=47 executed=3
📈 EXECUTIONS: total=3 success=3 failed=0
💼 ACTIVE POSITIONS: 2
🎯 STRATEGIES: 2 enabled
```

## 🔧 Development

### Adding a New Strategy

1. Create a new strategy class inheriting from `Strategy`
2. Implement `scan()` and `validate()` methods
3. Register in the orchestrator
4. Add tests

Example:

```python
class MyStrategy(Strategy):
    def scan(self, market_data):
        # Find opportunities
        signals = []
        # ... logic ...
        return signals
    
    def validate(self, signal):
        # Validate before execution
        return True, "ok"
```

### Code Quality

```bash
# Format code
ruff format src/ tests/

# Lint
ruff check src/ tests/

# Type check
mypy src/
```

## 🌟 Key Innovations

1. **Strategy Prioritization**: Urgent opportunities (guaranteed wins) execute before lower-priority ones
2. **Modular Design**: Easy to add/remove strategies without touching core logic
3. **Comprehensive Market Scanner**: Discovers opportunities across ALL Polymarket markets
4. **Real-Time & Historical**: Combines WebSocket feeds with API polling
5. **Production-Ready**: Logging, error handling, graceful shutdown

## 🎓 Learning Resources

- [Polymarket CLOB API Docs](https://docs.polymarket.com)
- [Gamma Markets API](https://gamma-api.polymarket.com)
- Trading Strategy Papers in `/docs/research/` (coming soon)

## ⚠️ Disclaimer

This is financial software operating in real markets. Key risks:

- Markets move quickly - prices can change before execution
- Partial fills can leave you exposed
- Fees and slippage can erode apparent arbitrage
- Smart contract and counterparty risks exist

**Use at your own risk. Start small. Test in paper mode first.**

## 🤝 Contributing

Contributions welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Ensure all tests pass
5. Submit a PR with clear description

## 📜 License

MIT License - see LICENSE file for details.

## 🙏 Acknowledgments

Built with:
- [py-clob-client](https://github.com/Polymarket/py-clob-client) - Official Polymarket Python SDK
- Real trader strategies that have generated millions in profits
- The "ultrathink" philosophy: elegant, powerful, inevitable

---

**Ready to start trading?** Review the safety features, test in paper mode, then deploy with confidence. 🚀

For questions or issues, open a GitHub issue or discussion.
