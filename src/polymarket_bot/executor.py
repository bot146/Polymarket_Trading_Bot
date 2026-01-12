from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

from polymarket_bot.arbitrage import HedgeOpportunity
from polymarket_bot.config import Settings

log = logging.getLogger(__name__)


class TradeMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True)
class ExecutionResult:
    placed: bool
    reason: str
    yes_order_id: str | None = None
    no_order_id: str | None = None


def _cap_size_usdc(max_usdc: Decimal, price: Decimal) -> Decimal:
    # Buy order in shares such that cost <= max_usdc.
    # shares = usdc / price
    if price <= 0:
        return Decimal(0)
    return (max_usdc / price).quantize(Decimal("0.01"))


def should_execute(settings: Settings, opp: HedgeOpportunity) -> tuple[bool, str]:
    min_edge = (settings.min_edge_cents + settings.edge_buffer_cents) / Decimal(100)
    if opp.edge <= 0:
        return False, "no_edge"
    if opp.edge < min_edge:
        return False, f"edge_below_threshold(edge={opp.edge:.4f} < min={min_edge:.4f})"
    if settings.kill_switch:
        return False, "kill_switch_enabled"
    return True, "ok"


def execute_hedge(
    *,
    client: ClobClient,
    settings: Settings,
    opp: HedgeOpportunity,
) -> ExecutionResult:
    ok, reason = should_execute(settings, opp)
    if not ok:
        return ExecutionResult(placed=False, reason=reason)

    # Determine sizing (simple): spend at most MAX_ORDER_USDC on each leg.
    yes_size = _cap_size_usdc(settings.max_order_usdc, opp.yes_ask)
    no_size = _cap_size_usdc(settings.max_order_usdc, opp.no_ask)
    size = min(yes_size, no_size)

    if size <= 0:
        return ExecutionResult(placed=False, reason="size_too_small")

    if settings.trading_mode != TradeMode.LIVE.value:
        log.warning(
            "PAPER MODE: would place hedge orders. yes_token=%s no_token=%s size=%s yes_ask=%s no_ask=%s edge=%s",
            opp.yes_token_id,
            opp.no_token_id,
            size,
            opp.yes_ask,
            opp.no_ask,
            opp.edge,
        )
        return ExecutionResult(placed=False, reason="paper_mode")

    # Live mode: place immediate-or-cancel style orders to reduce leg risk.
    # NOTE: py_clob_client abstracts signing, but we must choose order type.
    # We'll use FOK to avoid partial fills (more conservative).
    try:
        yes_order = OrderArgs(
            price=float(opp.yes_ask),
            size=float(size),
            side=BUY,
            token_id=opp.yes_token_id,
        )
        no_order = OrderArgs(
            price=float(opp.no_ask),
            size=float(size),
            side=BUY,
            token_id=opp.no_token_id,
        )

        # Prefer a two-step flow that exists across more client versions.
        yes_signed = client.create_order(yes_order)
        no_signed = client.create_order(no_order)

        # Different client versions type this differently (enum vs string).
        yes_resp = client.post_order(yes_signed, orderType="FOK")  # type: ignore[arg-type]
        no_resp = client.post_order(no_signed, orderType="FOK")  # type: ignore[arg-type]

        def _order_id(resp: object) -> str | None:
            if isinstance(resp, dict):
                return resp.get("orderID") or resp.get("orderId")
            return getattr(resp, "orderID", None) or getattr(resp, "orderId", None)

        return ExecutionResult(
            placed=True,
            reason="live_placed",
            yes_order_id=_order_id(yes_resp),
            no_order_id=_order_id(no_resp),
        )
    except Exception as e:
        log.exception("Live execution failed")
        return ExecutionResult(placed=False, reason=f"live_error:{e}")
