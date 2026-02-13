"""Enhanced unified executor for multi-strategy trading.

This module handles execution of trading signals from multiple strategies
with proper validation, risk management, error handling, and position tracking.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs

from polymarket_bot.circuit_breaker import CircuitBreaker
from polymarket_bot.config import Settings, is_live
from polymarket_bot.hedge_scheduler import HedgeScheduler
from polymarket_bot.inventory_hedger import InventoryHedger
from polymarket_bot.order_book_depth import OrderBookDepthChecker
from polymarket_bot.paper_trading import PaperBlotter, PaperFill
from polymarket_bot.position_manager import PositionManager
from polymarket_bot.strategy import Strategy, StrategySignal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionResult:
    """Result of executing a strategy signal."""
    success: bool
    reason: str
    signal: StrategySignal
    order_ids: list[str] | None = None
    position_ids: list[str] | None = None  # Track opened positions
    error: str | None = None


class UnifiedExecutor:
    """Unified executor for all trading strategies."""

    def __init__(
        self,
        client: ClobClient | None,
        settings: Settings,
        position_manager: PositionManager | None = None,
    ):
        self.client = client
        self.settings = settings
        self.position_manager = position_manager
        self.execution_count = 0
        self.success_count = 0
        self.failure_count = 0
        
        # Paper trading profitability tracking (expected/theoretical)
        self.paper_total_profit = Decimal("0")
        self.paper_total_cost = Decimal("0")
        self.paper_trades_by_strategy: dict[str, dict] = {}

        # Paper-mode order blotter for maker orders.
        self.paper_blotter = PaperBlotter(
            fill_probability=float(self.settings.paper_fill_probability),
            require_volume_cross=self.settings.paper_require_volume_cross,
            random_seed=self.settings.paper_random_seed,
        )

        # Track fills/placements for basic realism metrics.
        self.paper_orders_placed = 0
        self.paper_orders_filled = 0
        self.paper_orders_canceled = 0
        self.paper_orders_requoted = 0
        self._last_requote_ms_by_token: dict[str, int] = {}
        self._last_requote_ms_by_condition: dict[str, int] = {}

        # Runtime bankroll/equity cap (set by app loop from wallet snapshot/live wallet).
        self._equity_cap: Decimal | None = settings.paper_start_balance if settings.trading_mode == "paper" else None
        self._wallet_snapshot: dict[str, float | str | None] | None = None

        # Hedging metrics
        self.hedge_events = 0
        self.forced_hedge_events = 0

        # Inventory control
        self.hedger = InventoryHedger(
            min_imbalance_shares=Decimal("1"),
            max_hedge_usdc=min(Decimal("10"), self.settings.max_order_usdc),
        )
        self.hedge_scheduler = HedgeScheduler(hedge_timeout_ms=self.settings.hedge_timeout_ms)

        # Circuit breaker — halts trading after excessive losses.
        self.circuit_breaker = CircuitBreaker(
            max_daily_loss_usdc=self.settings.max_daily_loss_usdc,
            max_drawdown_pct=self.settings.max_drawdown_pct,
            max_consecutive_losses=self.settings.max_consecutive_losses,
            cooldown_minutes=self.settings.circuit_breaker_cooldown_minutes,
        )

        # Order book depth checker — verify liquidity before executing.
        self.depth_checker = OrderBookDepthChecker(
            api_base=self.settings.poly_host,
            min_depth_usdc=self.settings.min_book_depth_usdc,
        )

    def set_equity_cap(self, cap: Decimal | None) -> None:
        """Set current bankroll cap used by pre-trade risk checks."""
        self._equity_cap = cap

    def set_paper_equity_cap(self, cap: Decimal | None) -> None:
        """Backward-compatible alias for set_equity_cap."""
        self.set_equity_cap(cap)

    def set_wallet_snapshot(self, snapshot: dict[str, float | str | None] | None) -> None:
        """Set wallet snapshot for telemetry/dashboard output."""
        self._wallet_snapshot = snapshot

    def execute_signal(
        self,
        signal: StrategySignal,
        strategy: Strategy,
    ) -> ExecutionResult:
        """Execute a trading signal.
        
        Args:
            signal: The trading signal to execute.
            strategy: The strategy that generated the signal.
            
        Returns:
            ExecutionResult with success status and details.
        """
        self.execution_count += 1

        # Pre-execution validation
        if self.settings.kill_switch:
            return ExecutionResult(
                success=False,
                reason="kill_switch_enabled",
                signal=signal,
            )

        # Circuit breaker check
        if not self.circuit_breaker.allow_trading():
            log.warning("🔴 Circuit breaker TRIPPED — blocking trade")
            return ExecutionResult(
                success=False,
                reason="circuit_breaker_tripped",
                signal=signal,
            )

        # Validate with strategy
        valid, reason = strategy.validate(signal)
        if not valid:
            log.warning(f"Signal validation failed: {reason}")
            return ExecutionResult(
                success=False,
                reason=f"validation_failed_{reason}",
                signal=signal,
            )

        # Global risk rails (paper + live). These are conservative guardrails
        # to keep the bot from dying via runaway inventory.
        guard_ok, guard_reason = self._risk_check_signal(signal)
        if not guard_ok:
            return ExecutionResult(
                success=False,
                reason=f"risk_check_failed_{guard_reason}",
                signal=signal,
            )

        # Check if we have a client for live trading
        if not self.client:
            return self._paper_trade(signal)

        # Execute based on trading mode
        if is_live(self.settings):
            return self._live_trade(signal)
        else:
            return self._paper_trade(signal)

    def _paper_trade(self, signal: StrategySignal) -> ExecutionResult:
        """Simulate trade execution in paper mode."""
        strategy_type = signal.opportunity.strategy_type.value
        profit = signal.opportunity.expected_profit
        confidence = signal.opportunity.confidence
        cost = signal.max_total_cost
        
        # Track profitability (theoretical expected profit)
        self.paper_total_profit += profit
        self.paper_total_cost += cost
        
        # Track by strategy
        if strategy_type not in self.paper_trades_by_strategy:
            self.paper_trades_by_strategy[strategy_type] = {
                "count": 0,
                "total_profit": Decimal("0"),
                "total_cost": Decimal("0"),
            }
        
        self.paper_trades_by_strategy[strategy_type]["count"] += 1
        self.paper_trades_by_strategy[strategy_type]["total_profit"] += profit
        self.paper_trades_by_strategy[strategy_type]["total_cost"] += cost
        
    # In paper mode we distinguish between:
    # - taker-like orders (FOK/IOC): fill only if marketable against the last
    #   observed top-of-book (best_bid/best_ask). FOK is atomic across all legs.
    # - maker orders (GTC): rest in blotter and only fill on a future market cross
        order_ids: list[str] = []

        condition_id = signal.opportunity.metadata.get("condition_id")

        # Pre-check atomicity for FOK: all legs must be fillable at current book.
        fok_trades = [t for t in signal.trades if t.order_type == "FOK"]
        if fok_trades:
            for t in fok_trades:
                best_bid, best_ask = self.paper_blotter.get_last_top_of_book(token_id=t.token_id)
                if t.side == "BUY":
                    if best_ask is None or best_ask > t.price:
                        log.warning(
                            "📄 FOK not marketable: token=%s side=%s limit=%.4f best_ask=%s",
                            t.token_id[:12], t.side, t.price,
                            f"{best_ask:.4f}" if best_ask is not None else "None",
                        )
                        return ExecutionResult(success=False, reason="paper_fok_not_marketable", signal=signal)
                else:  # SELL
                    if best_bid is None or best_bid < t.price:
                        log.warning(
                            "📄 FOK not marketable: token=%s side=SELL limit=%.4f best_bid=%s",
                            t.token_id[:12], t.price,
                            f"{best_bid:.4f}" if best_bid is not None else "None",
                        )
                        return ExecutionResult(success=False, reason="paper_fok_not_marketable", signal=signal)

        for trade in signal.trades:
            # Record the order in the blotter (even if filled immediately) so that
            # we have stable order IDs for logs/metrics.
            paper_order = self.paper_blotter.submit(
                token_id=trade.token_id,
                side=trade.side,
                price=trade.price,
                size=trade.size,
                order_type=trade.order_type,
                condition_id=str(condition_id) if condition_id else None,
            )
            self.paper_orders_placed += 1
            order_ids.append(paper_order.order_id)

            if trade.order_type == "GTC":
                # GTC rests; no fill now.
                continue

            if trade.order_type in {"FOK", "IOC"}:
                # Book-aware taker simulation.
                best_bid, best_ask = self.paper_blotter.get_last_top_of_book(token_id=trade.token_id)

                fill_price: Decimal | None = None
                if trade.side == "BUY":
                    # Marketable if best_ask <= limit.
                    if best_ask is not None and best_ask <= trade.price:
                        fill_price = best_ask
                else:  # SELL
                    # Marketable if best_bid >= limit.
                    if best_bid is not None and best_bid >= trade.price:
                        fill_price = best_bid

                if fill_price is None:
                    # IOC: no fill. FOK atomicity was checked above.
                    continue

                fill = PaperFill(
                    order_id=paper_order.order_id,
                    token_id=paper_order.token_id,
                    side=paper_order.side,
                    fill_price=fill_price,
                    fill_size=trade.size,
                )
                self._apply_paper_fill(fill, signal=signal)
                self.paper_orders_filled += 1
                continue

            # Unknown order type: do nothing.
            continue

        # Record theoretical PnL for circuit breaker.
        self.circuit_breaker.record_trade_result(pnl=profit)

        log.warning(
            f"📄 PAPER TRADE [{strategy_type}]: "
            f"profit=${profit:.4f} cost=${cost:.2f} confidence={confidence:.2%} "
            f"trades={len(signal.trades)}"
        )

        for i, trade in enumerate(signal.trades):
            log.info(
                f"  Trade {i+1}: {trade.side} {trade.size:.2f} @ ${trade.price:.4f} "
                f"token={trade.token_id[:8]}... type={trade.order_type}"
            )
        
        # Show running total (theoretical)
        roi = (self.paper_total_profit / self.paper_total_cost * 100) if self.paper_total_cost > 0 else Decimal("0")
        log.info(
            f"  💰 Expected Profit Total: profit=${self.paper_total_profit:.4f} "
            f"cost=${self.paper_total_cost:.2f} ROI={roi:.2f}% (theoretical)"
        )
        
        # Show actual portfolio P&L if position manager available
        if self.position_manager:
            portfolio_stats = self.position_manager.get_portfolio_stats()
            log.info(
                f"  💼 Actual Portfolio: realized=${portfolio_stats['total_realized_pnl']:.4f} "
                f"unrealized=${portfolio_stats['total_unrealized_pnl']:.4f} "
                f"total=${portfolio_stats['total_pnl']:.4f}"
            )
        
        return ExecutionResult(
            success=True,
            reason="paper_mode_simulated",
            signal=signal,
            order_ids=order_ids if order_ids else None,
        )

    def on_market_update(
        self,
        *,
        token_id: str,
        best_bid: Decimal | None,
        best_ask: Decimal | None,
        best_ask_by_token: dict[str, Decimal] | None = None,
    ) -> list[PaperFill]:
        """Process a top-of-book update and fill any marketable resting maker orders."""
        fills = self.paper_blotter.update_market(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
        )
        for fill in fills:
            self._apply_paper_fill(fill, signal=None)
            self.paper_orders_filled += 1

        # Opportunistic hedging after any fills (paper mode only). We compute
        # imbalances per condition and submit IOC hedges.
        if fills:
            ask_map = best_ask_by_token or ({token_id: best_ask} if best_ask is not None else {})
            self._maybe_hedge_inventory(best_ask_by_token=ask_map)

        # Quote maintenance (paper): cancel stale maker (GTC) orders that are too
        # far from the current top-of-book so we don't accumulate dead quotes.
        # This is intentionally conservative; real live cancel/replace is added
        # separately behind a flag.
        if (best_bid is not None or best_ask is not None) and self.settings.trading_mode == "paper":
            # Cancel stale and, optionally, re-quote near the current book.
            # Paired quoting is enforced by condition_id when available.
            canceled = self.paper_blotter.cancel_stale_gtc_orders(
                token_id=token_id,
                max_price_distance=self.settings.requote_max_distance,
                max_age_seconds=self.settings.requote_max_age_seconds,
            )
            if canceled:
                self.paper_orders_canceled += len(canceled)

            if canceled and self.settings.enable_paper_requote:
                # Attempt condition-level paired requote if we can infer condition_id
                # from historical maker orders for this token.
                cond_id: str | None = self.paper_blotter.get_reference_gtc_condition_id(token_id=token_id)

                now_ms = int(time.time() * 1000)

                if cond_id:
                    last_ms = self._last_requote_ms_by_condition.get(cond_id, 0)
                    if (now_ms - last_ms) >= int(self.settings.requote_cooldown_ms):
                        self._last_requote_ms_by_condition[cond_id] = now_ms
                        self._paper_requote_condition(condition_id=cond_id)
                else:
                    # Fallback: per-token requote (legacy behavior for orders that
                    # did not include condition_id).
                    last_ms = self._last_requote_ms_by_token.get(token_id, 0)
                    if (now_ms - last_ms) >= int(self.settings.requote_cooldown_ms):
                        self._last_requote_ms_by_token[token_id] = now_ms
                        self._paper_requote_token(token_id=token_id)
        return fills

    def _paper_requote_condition(self, *, condition_id: str) -> None:
        """Cancel+replace policy for paper mode quotes, paired by condition.

        We re-place one BUY and one SELL quote near current top-of-book for each
        token that currently has maker orders under this condition.

        Notes:
        - Token universe is inferred from existing open maker orders.
        - This keeps YES/NO maintenance in sync and reduces directional drift.
        """
        token_ids = self.paper_blotter.known_gtc_token_ids_for_condition(condition_id)
        if not token_ids:
            return

        ref_sizes = self.paper_blotter.get_reference_gtc_size_for_condition(condition_id=condition_id)

        total_placed = 0
        for token_id in token_ids:
            best_bid, best_ask = self.paper_blotter.get_last_top_of_book(token_id=token_id)
            if best_bid is None and best_ask is None:
                continue

            ref = ref_sizes.get(token_id) or Decimal("1")

            placed = 0
            if best_bid is not None:
                self.paper_blotter.submit(
                    token_id=token_id,
                    side="BUY",
                    price=best_bid,
                    size=ref,
                    order_type="GTC",
                    condition_id=condition_id,
                )
                self.paper_orders_placed += 1
                placed += 1

            if best_ask is not None:
                self.paper_blotter.submit(
                    token_id=token_id,
                    side="SELL",
                    price=best_ask,
                    size=ref,
                    order_type="GTC",
                    condition_id=condition_id,
                )
                self.paper_orders_placed += 1
                placed += 1

            total_placed += placed

        if total_placed:
            self.paper_orders_requoted += total_placed

    def _paper_requote_token(self, *, token_id: str) -> None:
        """Cancel+replace policy for paper mode quotes.

        We re-place one BUY and one SELL quote near current top-of-book.
        - BUY: quote at best_bid (no improvement; queue realism not modeled)
        - SELL: quote at best_ask

        Size policy is intentionally conservative: reuse the size of the most
        recent open/canceled order if we can find one; otherwise small default.
        """
        best_bid, best_ask = self.paper_blotter.get_last_top_of_book(token_id=token_id)
        if best_bid is None and best_ask is None:
            return

        # Preserve condition_id where possible so future requotes can be paired.
        cond_id: str | None = self.paper_blotter.get_reference_gtc_condition_id(token_id=token_id)

        # Find a reference size from any historical order for this token.
        ref_size: Decimal | None = self.paper_blotter.get_reference_gtc_size(token_id=token_id)
        if ref_size is None:
            ref_size = Decimal("1")

        # Place refreshed quotes.
        placed = 0
        if best_bid is not None:
            self.paper_blotter.submit(
                token_id=token_id,
                side="BUY",
                price=best_bid,
                size=ref_size,
                order_type="GTC",
                condition_id=cond_id,
            )
            self.paper_orders_placed += 1
            placed += 1
        if best_ask is not None:
            self.paper_blotter.submit(
                token_id=token_id,
                side="SELL",
                price=best_ask,
                size=ref_size,
                order_type="GTC",
                condition_id=cond_id,
            )
            self.paper_orders_placed += 1
            placed += 1

        if placed:
            self.paper_orders_requoted += placed

    def _maybe_hedge_inventory(self, *, best_ask_by_token: dict[str, Decimal]) -> None:
        if not self.position_manager:
            return

        # Group open positions by condition.
        by_condition: dict[str, list] = {}
        for p in self.position_manager.get_open_positions():
            by_condition.setdefault(p.condition_id, []).append(p)

        for condition_id, positions in by_condition.items():
            # Best-effort mapping for yes/no token IDs comes from position metadata.
            meta = {}
            for p in positions:
                if p.metadata:
                    meta = p.metadata
                    break

            yes_token_id = meta.get("yes_token_id")
            no_token_id = meta.get("no_token_id")
            if not yes_token_id or not no_token_id:
                continue

            # Expand asks map if we have them in metadata.
            # Caller may pass partial; we only hedge if we have ask for hedge token.
            decision = self.hedger.decide(
                positions=positions,
                yes_token_id=str(yes_token_id),
                no_token_id=str(no_token_id),
                best_ask=best_ask_by_token,
            )
            if decision is None:
                # Balanced; clear any pending forced hedge.
                self.hedge_scheduler.clear(condition_id)
                continue

            # Any non-None decision means we detected an imbalance.
            self.hedge_events += 1

            # If we're in aggressive maker mode, allow a brief opportunistic
            # window before forcing the hedge (to let the other leg fill).
            profile = (self.settings.execution_profile or "").lower()
            if profile == "aggressive_maker":
                self.hedge_scheduler.note_imbalance(condition_id)
                if not self.hedge_scheduler.due(condition_id):
                    continue

            # If we had to wait for the scheduler (or we're in hard guarantee),
            # count this as a forced hedge.
            self.forced_hedge_events += 1

            # Hard guarantee mode (or timeout expired): execute hedge now.
            self.hedge_scheduler.clear(condition_id)

            # Execute hedges as immediate paper fills.
            for trade in decision.trades:
                paper_order = self.paper_blotter.submit(
                    token_id=trade.token_id,
                    side=trade.side,
                    price=trade.price,
                    size=trade.size,
                    order_type=trade.order_type,
                )
                self.paper_orders_placed += 1
                self.paper_orders_filled += 1
                self._apply_paper_fill(
                    PaperFill(
                        order_id=paper_order.order_id,
                        token_id=paper_order.token_id,
                        side=paper_order.side,
                        fill_price=trade.price,
                        fill_size=trade.size,
                    ),
                    signal=None,
                )

    def _apply_paper_fill(self, fill: PaperFill, *, signal: StrategySignal | None) -> None:
        """Apply a fill to the position ledger in paper mode.

        For BUY fills: open a Position.
        For SELL fills: close the oldest open Position for that token_id.

        Notes:
        - This is a conservative heuristic to avoid inventing a full FIFO/LIFO
          accounting model. It’s “good enough” for maker realism + P&L tracking.
        - If no position manager is configured, we do nothing.
        """
        if not self.position_manager:
            return

        # Infer metadata and strategy/outcome best-effort (safe defaults).
        condition_id = "unknown"
        strategy_type = "unknown"
        metadata: dict = {}
        if signal is not None:
            condition_id = signal.opportunity.metadata.get("condition_id", "unknown")
            strategy_type = signal.opportunity.strategy_type.value
            metadata = dict(signal.opportunity.metadata)

        # Determine outcome.
        outcome = metadata.get("outcome", "UNKNOWN")
        if "yes_token_id" in metadata and fill.token_id == metadata["yes_token_id"]:
            outcome = "YES"
        elif "no_token_id" in metadata and fill.token_id == metadata["no_token_id"]:
            outcome = "NO"

        if fill.side == "BUY":
            self.position_manager.open_position(
                condition_id=condition_id,
                token_id=fill.token_id,
                outcome=outcome,
                strategy=strategy_type,
                entry_price=fill.fill_price,
                quantity=fill.fill_size,
                entry_order_id=fill.order_id,
                metadata=metadata,
            )
            return

        # SELL: close an open position for this token (oldest-first).
        open_positions = [p for p in self.position_manager.get_open_positions() if p.token_id == fill.token_id]
        if not open_positions:
            return
        pos = sorted(open_positions, key=lambda p: p.entry_time)[0]
        self.position_manager.close_position(pos.position_id, exit_price=fill.fill_price, exit_order_id=fill.order_id)

    def _risk_check_signal(self, signal: StrategySignal) -> tuple[bool, str]:
        """Risk checks that must hold before we place *any* orders."""
        # Global bankroll cap (paper + live): do not allow open exposure to exceed
        # current account equity cap.
        if self.position_manager and self._equity_cap is not None:
            try:
                portfolio = self.position_manager.get_portfolio_stats()
                current_open_cost = Decimal(str(portfolio.get("total_cost_basis", 0)))
            except Exception:
                current_open_cost = Decimal("0")

            incoming_buy_cost = sum(
                (trade.price * trade.size for trade in signal.trades if trade.side == "BUY"),
                Decimal("0"),
            )

            if (current_open_cost + incoming_buy_cost) > self._equity_cap:
                return False, "wallet_exposure_limit"

        # Inventory cap per condition (cost basis).
        if self.position_manager:
            condition_id = signal.opportunity.metadata.get("condition_id")
            if condition_id:
                positions = self.position_manager.get_positions_by_condition(str(condition_id))
                open_cost = sum((p.cost_basis for p in positions if p.is_open), Decimal("0"))
                # Conservative: once we are *over* the cap, block new orders.
                # (Equality is allowed so a user can set an exact cap.)
                if open_cost > self.settings.max_inventory_usdc_per_condition:
                    return False, "max_inventory_usdc_per_condition"

        # Order book depth check (skip for paper-only to avoid API calls).
        if self.settings.verify_book_depth and is_live(self.settings):
            for trade in signal.trades:
                depth_ok = self.depth_checker.check_depth(
                    token_id=trade.token_id,
                    side=trade.side,
                    required_size=trade.size,
                    limit_price=trade.price,
                )
                if not depth_ok.sufficient:
                    log.warning(
                        "📉 Insufficient book depth for %s %s (have $%.2f, need $%.2f)",
                        trade.side, trade.token_id[:8],
                        float(depth_ok.available_notional),
                        float(self.settings.min_book_depth_usdc),
                    )
                    return False, "insufficient_book_depth"

        # Maker open order cap (paper mode only).
        # We treat any signal that submits GTC as "maker".
        condition_id = signal.opportunity.metadata.get("condition_id")
        if (
            condition_id
            and not is_live(self.settings)
            and any(t.order_type == "GTC" for t in signal.trades)
        ):
            existing = self.paper_blotter.open_gtc_orders_for_condition(str(condition_id))
            new_gtc = sum((1 for t in signal.trades if t.order_type == "GTC"), 0)
            if (len(existing) + new_gtc) > self.settings.max_open_gtc_orders_per_condition:
                return False, "max_open_gtc_orders_per_condition"

        return True, "ok"

    def _live_trade(self, signal: StrategySignal) -> ExecutionResult:
        """Execute trades in live mode."""
        if not self.client:
            return ExecutionResult(
                success=False,
                reason="no_client",
                signal=signal,
                error="Client not initialized",
            )

        order_ids = []
        position_ids = []
        strategy_type = signal.opportunity.strategy_type.value
        condition_id = signal.opportunity.metadata.get("condition_id", "unknown")
        
        try:
            for trade in signal.trades:
                # Create order
                order_args = OrderArgs(
                    price=float(trade.price),
                    size=float(trade.size),
                    side=trade.side,
                    token_id=trade.token_id,
                )
                
                # Sign order
                signed_order = self.client.create_order(order_args)
                
                # Post order
                response = self.client.post_order(signed_order, orderType=trade.order_type)  # type: ignore[arg-type]
                
                # Extract order ID
                order_id = self._extract_order_id(response)
                if order_id:
                    order_ids.append(order_id)
                
                log.info(
                    f"✅ LIVE ORDER: {trade.side} {trade.size:.2f} @ ${trade.price:.4f} "
                    f"order_id={order_id}"
                )
                
                # Track position if buy order and position manager available
                if trade.side == "BUY" and self.position_manager:
                    outcome = signal.opportunity.metadata.get("outcome", "UNKNOWN")
                    if "yes_token_id" in signal.opportunity.metadata and trade.token_id == signal.opportunity.metadata["yes_token_id"]:
                        outcome = "YES"
                    elif "no_token_id" in signal.opportunity.metadata and trade.token_id == signal.opportunity.metadata["no_token_id"]:
                        outcome = "NO"
                    
                    position = self.position_manager.open_position(
                        condition_id=condition_id,
                        token_id=trade.token_id,
                        outcome=outcome,
                        strategy=strategy_type,
                        entry_price=trade.price,
                        quantity=trade.size,
                        entry_order_id=order_id,
                        metadata=signal.opportunity.metadata,
                    )
                    position_ids.append(position.position_id)
            
            self.success_count += 1
            
            return ExecutionResult(
                success=True,
                reason="live_executed",
                signal=signal,
                order_ids=order_ids,
                position_ids=position_ids if position_ids else None,
            )
            
        except Exception as e:
            self.failure_count += 1
            log.exception(f"Live execution failed: {e}")
            
            return ExecutionResult(
                success=False,
                reason="live_execution_error",
                signal=signal,
                error=str(e),
            )

    def _extract_order_id(self, response: object) -> str | None:
        """Extract order ID from various response formats.
        
        The py-clob-client library may return different formats:
        - dict with "orderID" or "orderId" key
        - object with orderID or orderId attribute
        """
        try:
            if isinstance(response, dict):
                return response.get("orderID") or response.get("orderId")
            return getattr(response, "orderID", None) or getattr(response, "orderId", None)
        except Exception as e:
            log.warning(f"Failed to extract order ID from response: {e}")
            return None

    def get_stats(self) -> dict:
        """Get executor statistics including profitability and portfolio."""
        open_gtc = list(self.paper_blotter.iter_open_orders())
        open_gtc_by_condition: dict[str, int] = {}
        for o in open_gtc:
            if o.order_type != "GTC":
                continue
            if not o.condition_id:
                continue
            open_gtc_by_condition[o.condition_id] = open_gtc_by_condition.get(o.condition_id, 0) + 1

        stats = {
            "total_executions": self.execution_count,
            "successful": self.success_count,
            "failed": self.failure_count,
            "execution_profile": (self.settings.execution_profile or "").lower(),
            "hedge": {
                "events": self.hedge_events,
                "forced_events": self.forced_hedge_events,
            },
            "paper_total_profit": float(self.paper_total_profit),
            "paper_total_cost": float(self.paper_total_cost),
            "paper_roi": float((self.paper_total_profit / self.paper_total_cost * 100) if self.paper_total_cost > 0 else Decimal("0")),
            "paper_trades_by_strategy": {
                strategy: {
                    "count": data["count"],
                    "total_profit": float(data["total_profit"]),
                    "total_cost": float(data["total_cost"]),
                    "roi": float((data["total_profit"] / data["total_cost"] * 100) if data["total_cost"] > 0 else Decimal("0")),
                }
                for strategy, data in self.paper_trades_by_strategy.items()
            },
            "paper_orders": {
                "placed": self.paper_orders_placed,
                "filled": self.paper_orders_filled,
                "canceled": self.paper_orders_canceled,
                "requoted": self.paper_orders_requoted,
                "fill_rate": float(
                    (Decimal(self.paper_orders_filled) / Decimal(self.paper_orders_placed) * 100)
                    if self.paper_orders_placed > 0
                    else Decimal("0")
                ),
            },
            "paper_open_orders": {
                "open_gtc_total": sum(open_gtc_by_condition.values()),
                "open_gtc_by_condition": open_gtc_by_condition,
            },
            "wallet": self._wallet_snapshot or {},
            "circuit_breaker": self.circuit_breaker.get_stats(),
        }
        
        # Add portfolio stats if position manager available
        if self.position_manager:
            portfolio_stats = self.position_manager.get_portfolio_stats()
            stats["portfolio"] = portfolio_stats
        
        return stats
