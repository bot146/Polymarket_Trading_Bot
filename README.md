# Polymarket Trading Bot (CLOB)

A production-minded Polymarket trading bot focused on **real-time CLOB data** and **safe execution**.

This repo is intentionally built in layers:

- **Feeds**: real-time market data via CLOB WebSocket
- **Strategies**: start with *YES+NO hedge arbitrage* (sum of best asks < $1)
- **Execution**: place orders with hard risk caps, idempotency, and kill-switch
- **Modes**: paper trading by default, live trading when explicitly enabled

## Fast start (paper mode)

1. Create a virtualenv and install.

```zsh
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
```

1. Copy env template.

```zsh
cp .env.example .env
```

1. Run the bot (paper mode).

```zsh
python -m polymarket_bot.app
```

## Going live (Magic / Polymarket email) — recommended first

You will need:

- `POLY_PRIVATE_KEY` (Magic key) from <https://reveal.magic.link/polymarket>
- `POLY_FUNDER_ADDRESS` (your Polymarket proxy address you fund with USDC)
- `POLY_SIGNATURE_TYPE=1`

Then set:

- `TRADING_MODE=live`

The bot will **refuse** to trade live unless `TRADING_MODE=live` is set.

## Going live (EOA)

Set:

- `POLY_SIGNATURE_TYPE=0`

Fund your EOA with USDC and (depending on trading flow) MATIC for gas.

## Safety principles

- Start small: default `MAX_ORDER_USDC=20`
- Require real edge: `MIN_EDGE_CENTS` (buffer for fees/slippage/leg risk)
- Prefer immediate-or-cancel execution when hedging
- Kill switch via env var

## Repo layout

- `src/polymarket_bot/` – application code
- `tests/` – unit tests

## Disclaimer

This is financial software. Markets can move, fills can be partial, and fees/slippage can erase apparent arbitrage. Use at your own risk.
