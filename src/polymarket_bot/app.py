from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal

from polymarket_bot.arbitrage import compute_hedge_opportunity
from polymarket_bot.clob_client import build_clob_client
from polymarket_bot.config import load_settings
from polymarket_bot.executor import execute_hedge
from polymarket_bot.logging import setup_logging
from polymarket_bot.wss import MarketWssClient

log = logging.getLogger(__name__)


# For MVP, we run against a single known pair of token_ids.
# You can replace these with any YES/NO pair from Gamma API.
DEFAULT_YES_TOKEN_ID = "109681959945973300464568698402968596289258214226684818748321941747028805721376"
DEFAULT_NO_TOKEN_ID = "109681959945973300464568698402968596289258214226684818748321941747028805721377"


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)

    log.info("Starting polymarket bot. mode=%s kill_switch=%s", settings.trading_mode, settings.kill_switch)

    client = None
    creds = None
    if settings.poly_private_key:
        try:
            client, creds = build_clob_client(settings)
        except Exception as e:
            log.warning("CLOB client init failed (will run in data-only mode): %s", e)

    wss = MarketWssClient(asset_ids=[DEFAULT_YES_TOKEN_ID, DEFAULT_NO_TOKEN_ID])

    thr = threading.Thread(target=wss.run_forever, daemon=True)
    thr.start()

    last_action = 0.0
    while True:
        time.sleep(0.25)

        yes_ask = wss.best_ask.get(DEFAULT_YES_TOKEN_ID)
        no_ask = wss.best_ask.get(DEFAULT_NO_TOKEN_ID)
        if yes_ask is None or no_ask is None:
            continue

        opp = compute_hedge_opportunity(
            yes_token_id=DEFAULT_YES_TOKEN_ID,
            no_token_id=DEFAULT_NO_TOKEN_ID,
            yes_ask=Decimal(str(yes_ask)),
            no_ask=Decimal(str(no_ask)),
        )

        # Print status periodically.
        now = time.time()
        if now - last_action > 2.0:
            log.info("best_asks yes=%.4f no=%.4f total=%.4f edge=%.4f", yes_ask, no_ask, float(opp.total_cost), float(opp.edge))
            last_action = now

        if client is None:
            continue

        # Try execution when edge positive.
        res = execute_hedge(client=client, settings=settings, opp=opp)
        if res.placed:
            log.warning("Placed hedge orders yes=%s no=%s", res.yes_order_id, res.no_order_id)
            time.sleep(1.0)


if __name__ == "__main__":
    main()
