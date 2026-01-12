from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from enum import Enum

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, MarketOrderArgs
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


def _quantize_order_size(side: str, price: float, size: float) -> float:
    """Quantize size to satisfy CLOB precision constraints.
    
    Observed venue constraint (marketable BUY orders):
    - maker amount max 2 decimals (collateral)
    - taker amount max 4 decimals (shares)
    
    We enforce:
    - size: max 4 decimals
    - for BUY: also ensure notional (price * size) has max 2 decimals
    """
    def _round_down(value: float, decimals: int) -> float:
        q = Decimal("1").scaleb(-int(decimals))
        return float(Decimal(str(value)).quantize(q, rounding=ROUND_DOWN))
    
    s = (side or "").upper()
    p = float(price)
    sz = float(size)

    if sz <= 0:
        return sz

    # Always cap share precision to 4 decimals.
    sz4 = _round_down(sz, 4)
    if p <= 0:
        return sz4

    if s == "BUY":
        # Ensure collateral notional has max 2 decimals.
        notional2 = _round_down(p * sz4, 2)
        if notional2 <= 0:
            return 0.0
        sz4 = _round_down(notional2 / p, 4)
    return sz4


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
    # Apply precision quantization to avoid CLOB rejections
    yes_price = float(opp.yes_ask)
    no_price = float(opp.no_ask)
    yes_size_f = _quantize_order_size("BUY", yes_price, float(size))
    no_size_f = _quantize_order_size("BUY", no_price, float(size))
    
    if yes_size_f <= 0 or no_size_f <= 0:
        return ExecutionResult(placed=False, reason="size_too_small_after_quantization")

    try:
        yes_order = OrderArgs(
            price=yes_price,
            size=yes_size_f,
            side=BUY,
            token_id=opp.yes_token_id,
        )
        no_order = OrderArgs(
            price=no_price,
            size=no_size_f,
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
        # Check for common error patterns
        err_msg = str(e).lower()
        if "cloudflare" in err_msg or "403" in err_msg:
            return ExecutionResult(placed=False, reason="cloudflare_blocked")
        elif "insufficient" in err_msg or "allowance" in err_msg:
            return ExecutionResult(placed=False, reason="insufficient_funds_or_approval")
        elif "invalid amount" in err_msg and "min size" in err_msg:
            return ExecutionResult(placed=False, reason="min_order_notional")
        return ExecutionResult(placed=False, reason=f"live_error:{e}")

