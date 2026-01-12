# Mirror Trading Archive

This directory contains archived code from the mirror trading functionality that was removed from the main codebase.

## Archived Files

- **mirror_bot.py** - Main mirror trading bot that monitors and copies trades from target accounts
- **trade_executor.py** - Trade execution service for mirror trades with precision handling
- **trade_monitor.py** - Monitors target account trades and validates them for mirroring

## Why Archived?

These files were part of a mirror trading feature that allowed copying trades from other Polymarket accounts. The functionality was removed to focus the bot on:

1. **Autonomous Trading Strategies** - Arbitrage, guaranteed wins, statistical arbitrage
2. **All-Market Scanning** - Scanning all Polymarket markets (not just following specific accounts)
3. **Bot-Inspired Patterns** - Implementing patterns used by successful high-volume bots

The mirror trading configuration was shared for configuration purposes but is not the core focus of this trading bot.

## Reference Only

These files are kept for reference purposes only and are not maintained or tested. They may contain outdated code or dependencies.

If you need mirror trading functionality, you would need to:
1. Restore the mirror configuration fields in `config.py` and `.env.example`
2. Move these files back to `src/polymarket_bot/`
3. Update dependencies and test thoroughly
4. Re-integrate with the current codebase

---

Archived: January 12, 2026
