"""Multi-strategy orchestrator for Polymarket trading bot.

This module coordinates multiple strategies and manages their execution.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from polymarket_bot.config import Settings
from polymarket_bot.market_feed import EnhancedMarketFeed
from polymarket_bot.order_book_depth import OrderBookDepthChecker
from polymarket_bot.scanner import MarketScanner
from polymarket_bot.strategy import StrategyRegistry, StrategySignal, StrategyType
from polymarket_bot.strategies.arbitrage_strategy import ArbitrageStrategy
from polymarket_bot.strategies.conditional_arb_strategy import ConditionalArbStrategy
from polymarket_bot.strategies.copy_trading_strategy import CopyTradingStrategy
from polymarket_bot.strategies.guaranteed_win_strategy import GuaranteedWinStrategy
from polymarket_bot.strategies.liquidity_rewards_strategy import LiquidityRewardsStrategy
from polymarket_bot.strategies.market_making_strategy import MarketMakingConfig, MarketMakingStrategy
from polymarket_bot.strategies.multi_outcome_arb_strategy import MultiOutcomeArbStrategy
from polymarket_bot.strategies.near_resolution_strategy import NearResolutionStrategy
from polymarket_bot.strategies.oracle_sniping_strategy import OracleSnipingStrategy
from polymarket_bot.strategies.sniping_strategy import SnipingConfig, SnipingStrategy
from polymarket_bot.strategies.statistical_arbitrage_strategy import StatisticalArbitrageStrategy
from polymarket_bot.strategies.value_betting_strategy import ValueBettingConfig, ValueBettingStrategy

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrchestratorConfig:
    """Configuration for the strategy orchestrator."""
    scan_interval: float = 2.0  # Seconds between scans
    max_concurrent_trades: int = 5  # Max number of simultaneous positions
    enable_arbitrage: bool = True
    enable_guaranteed_win: bool = False
    enable_stat_arb: bool = False  # Speculative — disabled
    enable_sniping: bool = False  # Speculative — disabled
    enable_market_making: bool = False  # Speculative — disabled
    enable_oracle_sniping: bool = False  # Speculative — disabled
    enable_copy_trading: bool = False
    enable_value_betting: bool = False  # Speculative (Kelly) — disabled
    enable_multi_outcome_arb: bool = True  # Buy all YES in a group for < $1
    enable_conditional_arb: bool = False     # Cumulative bracket arb
    enable_liquidity_rewards: bool = False   # Liquidity reward harvesting
    enable_near_resolution: bool = False     # Near-resolution sniping
    enable_arb_stacking: bool = False        # Allow N stacked positions on same arb group
    max_arb_stacks: int = 3                  # Max stacked positions per group
    
    # Market scanning
    scan_high_volume: bool = True
    scan_resolved: bool = True
    # min_volume will be taken from Settings.min_market_volume
    
    def __post_init__(self):
        """Validate configuration values."""
        if self.scan_interval <= 0:
            raise ValueError("scan_interval must be positive")
        if self.max_concurrent_trades < 0:
            raise ValueError("max_concurrent_trades must be non-negative")


class StrategyOrchestrator:
    """Orchestrates multiple trading strategies."""

    def __init__(self, settings: Settings, config: OrchestratorConfig | None = None):
        self.settings = settings
        self.config = config or OrchestratorConfig()
        
        # Initialize components with settings
        self.scanner = MarketScanner(fetch_limit=settings.market_fetch_limit)
        self.registry = StrategyRegistry()
        
        # Initialize strategies
        self._init_strategies()

        # Realtime top-of-book feed (best bid/ask). This is optional: if it can't
        # start or doesn't have data yet, we fall back to Gamma prices.
        self._feed: EnhancedMarketFeed | None = None
        self._feed_started = False
        
        # State tracking
        self.active_positions: list[str] = []  # Track active condition_ids (duplicates = stacked entries)
        self.total_signals_seen = 0
        self.total_signals_executed = 0
        self._dynamic_max_order_usdc: Decimal | None = None
        self._dynamic_min_order_usdc: Decimal | None = None
        self._dynamic_initial_order_pct: Decimal | None = None

    def _init_strategies(self) -> None:
        """Initialize and register trading strategies."""
        if self.config.enable_arbitrage:
            # Strict arb (used for BOTH paper and live):
            # - include edge buffer for fees/leg risk
            # - require top-of-book (avoid Gamma fallback)
            # Paper mode exists to test production logic with simulated fills,
            # so the signal-generation rules should match live mode.
            strict_arb = True
            arb_strategy = ArbitrageStrategy(
                min_edge_cents=self.settings.min_edge_cents,
                edge_buffer_cents=self.settings.edge_buffer_cents,
                max_order_usdc=self.settings.max_order_usdc,
                strict=strict_arb,
                require_top_of_book=strict_arb,
                taker_fee_rate=self.settings.taker_fee_rate,
                enabled=True,
            )
            self.registry.register(arb_strategy)
            log.info("Registered: ArbitrageStrategy (taker_fee=%.2f%%)", float(self.settings.taker_fee_rate * 100))

        if self.config.enable_guaranteed_win:
            gw_strategy = GuaranteedWinStrategy(
                min_discount_cents=Decimal("5.0"),
                max_order_usdc=self.settings.max_order_usdc * Decimal("2"),  # More capital for guaranteed wins
                taker_fee_rate=self.settings.taker_fee_rate,
                enabled=True,
            )
            self.registry.register(gw_strategy)
            log.info("Registered: GuaranteedWinStrategy")

        if self.config.enable_stat_arb:
            stat_arb_strategy = StatisticalArbitrageStrategy(
                max_order_usdc=self.settings.max_order_usdc,
                maker_fee_rate=self.settings.maker_fee_rate,
                enabled=True,
            )
            self.registry.register(stat_arb_strategy)
            log.info("Registered: StatisticalArbitrageStrategy")

        if self.config.enable_sniping:
            snipe_config = SnipingConfig(maker_fee_rate=self.settings.maker_fee_rate)
            snipe_strategy = SnipingStrategy(config=snipe_config, enabled=True)
            self.registry.register(snipe_strategy)
            log.info("Registered: SnipingStrategy")

        if self.config.enable_market_making:
            mm_config = MarketMakingConfig(maker_fee_rate=self.settings.maker_fee_rate)
            mm_strategy = MarketMakingStrategy(config=mm_config, enabled=True)
            self.registry.register(mm_strategy)
            log.info("Registered: MarketMakingStrategy")

        if self.config.enable_oracle_sniping and self.settings.enable_oracle_sniping:
            oracle_strategy = OracleSnipingStrategy(
                taker_fee_rate=self.settings.taker_fee_rate,
                max_order_usdc=self.settings.max_order_usdc,
                min_confidence=self.settings.oracle_min_confidence,
                enabled=True,
            )
            self.registry.register(oracle_strategy)
            log.info("Registered: OracleSnipingStrategy (via CoinGecko)")

        if self.config.enable_copy_trading and self.settings.enable_copy_trading:
            whale_addrs = set(
                a.strip() for a in self.settings.whale_addresses.split(",") if a.strip()
            )
            copy_strategy = CopyTradingStrategy(
                min_trade_usdc=self.settings.whale_min_trade_usdc,
                max_order_usdc=self.settings.max_order_usdc,
                whale_addresses=whale_addrs if whale_addrs else None,
                taker_fee_rate=self.settings.taker_fee_rate,
                enabled=True,
            )
            self.registry.register(copy_strategy)
            log.info("Registered: CopyTradingStrategy (whale_addrs=%d)", len(whale_addrs))

        if self.config.enable_value_betting:
            vb_config = ValueBettingConfig(maker_fee_rate=self.settings.maker_fee_rate)
            vb_strategy = ValueBettingStrategy(config=vb_config, enabled=True)
            self.registry.register(vb_strategy)
            log.info("Registered: ValueBettingStrategy (ensemble edge detection)")

        if self.config.enable_multi_outcome_arb:
            mo_arb_strategy = MultiOutcomeArbStrategy(
                min_edge_cents=self.settings.min_edge_cents,
                max_order_usdc=self.settings.max_order_usdc,
                taker_fee_rate=self.settings.taker_fee_rate,
                enabled=True,
            )
            self.registry.register(mo_arb_strategy)
            log.info("Registered: MultiOutcomeArbStrategy")

        if self.config.enable_conditional_arb:
            cond_arb_strategy = ConditionalArbStrategy(
                min_edge_cents=self.settings.min_edge_cents,
                max_order_usdc=self.settings.max_order_usdc,
                taker_fee_rate=self.settings.taker_fee_rate,
                enabled=True,
            )
            self.registry.register(cond_arb_strategy)
            log.info("Registered: ConditionalArbStrategy")

        if self.config.enable_liquidity_rewards:
            liq_strategy = LiquidityRewardsStrategy(
                max_order_usdc=self.settings.max_order_usdc,
                maker_fee_rate=self.settings.maker_fee_rate,
                max_position_usdc=self.settings.liquidity_rewards_max_position,
                enabled=True,
            )
            self.registry.register(liq_strategy)
            log.info("Registered: LiquidityRewardsStrategy")

        if self.config.enable_near_resolution:
            nr_strategy = NearResolutionStrategy(
                min_edge_cents=self.settings.min_edge_cents,
                max_order_usdc=self.settings.max_order_usdc,
                taker_fee_rate=self.settings.taker_fee_rate,
                max_hours_to_end=self.settings.near_resolution_max_hours,
                enabled=True,
            )
            self.registry.register(nr_strategy)
            log.info("Registered: NearResolutionStrategy")

    def scan_and_collect_signals(self) -> list[StrategySignal]:
        """Scan markets and collect signals from all strategies."""
        market_data = self._gather_market_data()
        
        # Build a condition_id → end_date lookup from market data so we can
        # inject resolution info into every signal's metadata centrally,
        # regardless of whether individual strategies include it.
        end_date_by_condition: dict[str, str | None] = {}
        for m in market_data.get("markets", []):
            cid = m.get("condition_id")
            if cid:
                end_date_by_condition[cid] = m.get("end_date")

        # Run all strategies
        signals = self.registry.scan_all(market_data)

        # Enrich signals: ensure end_date is in metadata for priority scoring
        enriched: list[StrategySignal] = []
        for sig in signals:
            meta = sig.opportunity.metadata
            cid = meta.get("condition_id", "")
            if "end_date" not in meta and cid in end_date_by_condition:
                new_meta = dict(meta)
                new_meta["end_date"] = end_date_by_condition[cid]
                new_opp = replace(sig.opportunity, metadata=new_meta)
                sig = replace(sig, opportunity=new_opp)
            enriched.append(sig)

        signals = enriched
        self.total_signals_seen += len(signals)
        
        if signals:
            log.info(f"Found {len(signals)} total signals across all strategies")
        
        return signals

    def prioritize_signals(self, signals: list[StrategySignal]) -> list[StrategySignal]:
        """Prioritize signals by resolution time, edge, and urgency.

        Each signal receives a composite priority score:
            score = edge_weight * edge_score + resolution_weight * time_score

        Where:
        - **edge_score** is normalized expected profit (0-1 within the batch)
        - **time_score** rewards markets closer to resolution:
            • Markets within ``resolution_sweet_spot_hours`` get score 1.0
            • Score decays linearly toward 0 at ``resolution_max_days``
            • Markets with unknown end_date get score 0.1 (low but nonzero)

        Urgency is used as a tiebreaker (higher urgency wins).
        """
        if not signals:
            return signals

        sweet_spot_hours = self.settings.resolution_sweet_spot_hours
        max_hours = self.settings.resolution_max_days * 24.0
        edge_weight = self.settings.edge_priority_weight
        time_weight = self.settings.resolution_priority_weight

        # Normalize weights so they sum to 1.0
        total_weight = edge_weight + time_weight
        if total_weight > 0:
            edge_weight /= total_weight
            time_weight /= total_weight
        else:
            edge_weight = 0.5
            time_weight = 0.5

        # Compute edge scores (normalize to [0, 1])
        profits = [float(s.opportunity.expected_profit) for s in signals]
        max_profit = max(profits) if profits else 1.0
        if max_profit <= 0:
            max_profit = 1.0

        def _time_score(signal: StrategySignal) -> float:
            """Score from 0.0 to 1.0 based on time to resolution."""
            end_date = signal.opportunity.metadata.get("end_date")
            hours = MarketScanner.hours_to_resolution(end_date)
            if hours is None:
                return 0.1  # Unknown resolution → low priority
            if hours < 0:
                return 0.0  # Already past due

            if hours <= sweet_spot_hours:
                return 1.0  # Sweet spot → max priority
            if max_hours > 0 and hours >= max_hours:
                return 0.0  # Beyond window

            # Linear decay from 1.0 at sweet_spot to 0.0 at max_hours
            remaining_range = max_hours - sweet_spot_hours
            if remaining_range <= 0:
                return 0.5
            return max(0.0, 1.0 - (hours - sweet_spot_hours) / remaining_range)

        def _composite_score(signal: StrategySignal, idx: int) -> float:
            edge_score = profits[idx] / max_profit
            t_score = _time_score(signal)
            return edge_weight * edge_score + time_weight * t_score

        scored = [
            (_composite_score(s, i), s.opportunity.urgency, s)
            for i, s in enumerate(signals)
        ]
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

        # Log the top 5 for visibility
        for rank, (score, urgency, sig) in enumerate(scored[:5], 1):
            end_date = sig.opportunity.metadata.get("end_date", "?")
            hours = MarketScanner.hours_to_resolution(end_date)
            hours_str = f"{hours:.0f}h" if hours is not None else "?"
            log.debug(
                "Priority #%d: score=%.3f edge=$%.4f time=%s urgency=%d | %s",
                rank, score,
                float(sig.opportunity.expected_profit),
                hours_str, urgency,
                sig.opportunity.metadata.get("condition_id", "?")[:16],
            )

        return [s for _, _, s in scored]

    def filter_signals(self, signals: list[StrategySignal]) -> list[StrategySignal]:
        """Filter signals based on current state and constraints."""
        filtered = []
        
        for signal in signals:
            # Skip if we already have a position in this market
            condition_id = signal.opportunity.metadata.get("condition_id")
            if condition_id and condition_id in self.active_positions:
                # Same-group stacking: allow re-execution up to max_stacks
                strategy_type = signal.opportunity.strategy_type
                is_stackable = strategy_type in (
                    StrategyType.MULTI_OUTCOME_ARB,
                    StrategyType.CONDITIONAL_ARB,
                )
                if self.config.enable_arb_stacking and is_stackable:
                    count = self.active_positions.count(condition_id)
                    if count < self.config.max_arb_stacks:
                        log.info(
                            "📚 Stacking %s on %s (%d/%d)",
                            strategy_type.value,
                            condition_id[:12],
                            count + 1,
                            self.config.max_arb_stacks,
                        )
                        # Allow through — don't skip
                    else:
                        log.info(
                            "⏭️  Skipping %s — max stacks reached (%d/%d)",
                            condition_id[:12], count, self.config.max_arb_stacks,
                        )
                        continue
                else:
                    log.info(f"⏭️  Skipping {condition_id[:12]}... — already have active position")
                    continue
            
            # Check concurrent trade limit
            if len(self.active_positions) >= self.config.max_concurrent_trades:
                log.info(f"Max concurrent trades reached ({self.config.max_concurrent_trades})")
                break
            
            filtered.append(signal)
        
        return filtered

    def _gather_market_data(self) -> dict[str, Any]:
        """Gather market data from scanner."""
        market_data: dict[str, Any] = {"markets": [], "resolved_markets": []}

        # Lazy-start websocket feed once we know what assets to subscribe to.
        # We do this here (instead of in __init__) because the asset universe is
        # derived from the scanner results.
        def _maybe_start_feed(token_ids: list[str]) -> None:
            if self._feed_started:
                return
            if not token_ids:
                return

            try:
                self._feed = EnhancedMarketFeed(asset_ids=token_ids)
                self._feed.start()
                self._feed_started = True
                log.info("✅ Top-of-book feed started (assets=%d)", len(token_ids))
            except Exception as e:
                # Data-only / restricted networks should still allow the bot to run.
                self._feed_started = True
                self._feed = None
                log.warning("⚠️  Could not start top-of-book feed; using Gamma price fallback: %s", e)
        
        try:
            # Scan high-volume active markets
            if self.config.scan_high_volume:
                high_vol_markets = self.scanner.get_high_volume_markets(
                    min_volume=self.settings.min_market_volume,  # Use settings
                    limit=None  # Scan all markets above volume threshold
                )

                # Apply resolution-time window filter.
                # In paper mode, use the tighter paper-specific window (hours → days)
                # so markets resolve quickly and we capture actual P&L.
                res_min = self.settings.resolution_min_days
                res_max = self.settings.resolution_max_days
                if (
                    self.settings.trading_mode == "paper"
                    and getattr(self.settings, "paper_resolution_max_hours", 0) > 0
                ):
                    res_max = self.settings.paper_resolution_max_hours / 24.0
                    res_min = 0.0  # Start from now

                if res_max > 0 or res_min > 0:
                    pre_filter_count = len(high_vol_markets)
                    high_vol_markets = self.scanner.filter_by_resolution_window(
                        high_vol_markets,
                        min_days=res_min,
                        max_days=res_max,
                    )
                    if pre_filter_count != len(high_vol_markets):
                        label = f"{res_max * 24:.0f}h" if res_max < 1 else f"{res_max:.0f}d"
                        log.info(
                            "🕐 Resolution window [%s]: %d → %d markets",
                            label,
                            pre_filter_count,
                            len(high_vol_markets),
                        )

                # Gather token ids and start feed (once).
                token_ids: list[str] = []
                for m in high_vol_markets:
                    for t in m.tokens:
                        if t.token_id:
                            token_ids.append(str(t.token_id))
                _maybe_start_feed(list(dict.fromkeys(token_ids)))

                # Snapshot to avoid locking per-token.
                feed_snapshot: dict[str, Any] = {}
                if self._feed is not None:
                    try:
                        feed_snapshot = self._feed.get_market_data()
                    except Exception:
                        feed_snapshot = {}
                best_bid_map: dict[str, float] = feed_snapshot.get("best_bid", {}) if isinstance(feed_snapshot, dict) else {}
                best_ask_map: dict[str, float] = feed_snapshot.get("best_ask", {}) if isinstance(feed_snapshot, dict) else {}

                # When the WSS feed isn't connected, best_ask falls back to the
                # Gamma mid-price.  For arbitrage strategies (binary and multi-
                # outcome) we need *executable* ask prices from the CLOB order
                # book.  Fetch books for tokens that don't have WSS data.
                clob_best_ask_map: dict[str, float] = {}
                if self.config.enable_arbitrage or self.config.enable_multi_outcome_arb:
                    clob_best_ask_map = self._fetch_clob_best_asks(high_vol_markets)

                # Merge: CLOB → WSS → Gamma (priority order for best_ask)
                # For negRisk tokens the CLOB book gives the real executable ask,
                # which is what multi-outcome arb needs. WSS mid-prices are useful
                # for speculative strategies but can mask arb edges.
                def _resolve_best_ask(token_id: str) -> float | None:
                    tid = str(token_id)
                    # 1. CLOB order book (most accurate for execution)
                    if tid in clob_best_ask_map and clob_best_ask_map[tid] is not None:
                        return float(clob_best_ask_map[tid])
                    # 2. WSS feed (real-time but may differ from executable ask)
                    if tid in best_ask_map and best_ask_map[tid] is not None:
                        return float(best_ask_map[tid])
                    # No executable ask available.
                    return None

                # Convert to dict format for strategies
                for market in high_vol_markets:
                    market_dict = {
                        "condition_id": market.condition_id,
                        "question": market.question,
                        "volume": float(market.volume),
                        "active": market.active,
                        "neg_risk_market_id": market.neg_risk_market_id,
                        "group_item_title": market.group_item_title,
                        "end_date": market.end_date,
                        "liquidity": float(market.liquidity),
                        "spread": float(market.spread) if market.spread is not None else None,
                        "one_day_price_change": market.one_day_price_change,
                        "rewards_min_size": float(market.rewards_min_size) if market.rewards_min_size is not None else None,
                        "rewards_max_spread": float(market.rewards_max_spread) if market.rewards_max_spread is not None else None,
                        "rewards_daily_rate": float(market.rewards_daily_rate) if market.rewards_daily_rate is not None else None,
                        "tokens": [
                            {
                                "token_id": token.token_id,
                                "outcome": token.outcome,
                                "price": float(token.price),
                                "best_bid": (
                                    float(best_bid_map[str(token.token_id)])
                                    if str(token.token_id) in best_bid_map and best_bid_map[str(token.token_id)] is not None
                                    else None
                                ),
                                "best_ask": _resolve_best_ask(token.token_id),
                                "volume": float(token.volume),
                            }
                            for token in market.tokens
                        ],
                    }
                    market_data["markets"].append(market_dict)
            
            # Scan resolved markets for guaranteed wins
            if self.config.scan_resolved:
                resolved_markets = self.scanner.get_resolved_markets(limit=None)  # Scan all resolved markets

                # Reuse feed where possible (resolved markets should share token ids)
                feed_snapshot: dict[str, Any] = {}
                if self._feed is not None:
                    try:
                        feed_snapshot = self._feed.get_market_data()
                    except Exception:
                        feed_snapshot = {}
                best_ask_map: dict[str, float] = feed_snapshot.get("best_ask", {}) if isinstance(feed_snapshot, dict) else {}
                
                for market in resolved_markets:
                    market_dict = {
                        "condition_id": market.condition_id,
                        "question": market.question,
                        "resolved": market.resolved,
                        "winning_outcome": market.winning_outcome,
                        "tokens": [
                            {
                                "token_id": token.token_id,
                                "outcome": token.outcome,
                                "price": float(token.price),
                                "best_ask": (
                                    float(best_ask_map[str(token.token_id)])
                                    if str(token.token_id) in best_ask_map and best_ask_map[str(token.token_id)] is not None
                                    else float(token.price)
                                ),
                            }
                            for token in market.tokens
                        ],
                    }
                    market_data["resolved_markets"].append(market_dict)
                    
            log.debug(
                f"Gathered {len(market_data['markets'])} active markets, "
                f"{len(market_data['resolved_markets'])} resolved markets"
            )
            
        except Exception as e:
            log.error(f"Failed to gather market data: {e}")
        
        return market_data

    def run_once(self) -> list[StrategySignal]:
        """Run one iteration of strategy scanning.
        
        Returns:
            List of prioritized, filtered signals ready for execution.
        """
        # Collect signals from all strategies
        signals = self.scan_and_collect_signals()
        
        # Prioritize by urgency and profit
        signals = self.prioritize_signals(signals)
        
        # Filter based on constraints
        signals = self.filter_signals(signals)

        # Apply graduated sizing — scale trade sizes based on stack depth
        signals = self._apply_graduated_sizing(signals)
        
        return signals

    # ------------------------------------------------------------------
    # Graduated position sizing
    # ------------------------------------------------------------------

    def _apply_graduated_sizing(
        self, signals: list[StrategySignal]
    ) -> list[StrategySignal]:
        """Scale trade sizes to build positions gradually.

        First entry into a market uses ``initial_order_pct`` of max_order_usdc.
        Each subsequent stack (if arb stacking is enabled) increases linearly
        toward max_order_usdc.  This lets the bot probe with a small amount
        first and commit more capital only when the edge persists.

        Tier schedule (with initial_order_pct=25, max_stacks=3):
            Stack 1: 25% of max  → $5
            Stack 2: 62% of max  → $12.50
            Stack 3: 100% of max → $20

        Strategies that already sized below the tier target are left as-is
        (we never *increase* a signal's size beyond what the strategy chose).
        """
        initial_pct_raw = self._dynamic_initial_order_pct or self.settings.initial_order_pct
        initial_pct = initial_pct_raw / Decimal("100")
        min_usdc = self._dynamic_min_order_usdc or self.settings.min_order_usdc
        max_usdc = self._dynamic_max_order_usdc or self.settings.max_order_usdc
        max_stacks = self.config.max_arb_stacks

        sized: list[StrategySignal] = []
        for signal in signals:
            condition_id = signal.opportunity.metadata.get("condition_id", "")
            stack_count = self.active_positions.count(condition_id)  # 0 = first entry

            # Compute tier fraction: lerp from initial_pct (stack 0) to 1.0 (stack max-1)
            if max_stacks <= 1:
                tier_frac = initial_pct
            else:
                tier_frac = initial_pct + (Decimal("1") - initial_pct) * Decimal(str(stack_count)) / Decimal(str(max_stacks - 1))
            tier_frac = min(tier_frac, Decimal("1"))

            tier_budget = max(min_usdc, (max_usdc * tier_frac).quantize(Decimal("0.01")))

            # Only scale down — never inflate above what the strategy calculated
            original_cost = signal.max_total_cost
            if original_cost <= Decimal("0"):
                sized.append(signal)
                continue

            if tier_budget >= original_cost:
                # Strategy already sized within / below the tier budget
                sized.append(signal)
                continue

            # Scale factor to shrink all trades proportionally
            scale = (tier_budget / original_cost).quantize(Decimal("0.0001"))
            if scale <= Decimal("0"):
                continue

            scaled_trades = [
                replace(t, size=(t.size * scale).quantize(Decimal("0.01")))
                for t in signal.trades
            ]

            # Drop if any trade size falls below minimum viable (Polymarket min ≈ 1 share)
            if any(t.size < Decimal("1") for t in scaled_trades):
                log.debug(
                    "Dropping signal %s — scaled size < 1 share",
                    condition_id[:12],
                )
                continue

            new_cost = sum(t.size * t.price for t in scaled_trades)
            new_return = signal.min_expected_return * scale
            new_profit = signal.opportunity.expected_profit * scale

            new_opp = replace(signal.opportunity, expected_profit=new_profit)
            new_signal = replace(
                signal,
                opportunity=new_opp,
                trades=scaled_trades,
                max_total_cost=new_cost,
                min_expected_return=new_return,
            )

            log.info(
                "📐 Sized %s tier %d/%d: $%.2f → $%.2f (%.0f%%)",
                condition_id[:12],
                stack_count + 1,
                max_stacks,
                float(original_cost),
                float(new_cost),
                float(tier_frac * 100),
            )
            sized.append(new_signal)

        return sized

    def mark_position_active(self, condition_id: str) -> None:
        """Mark a market as having an active position."""
        # Keep duplicates to represent stacked entries in the same condition.
        self.active_positions.append(condition_id)

    def mark_position_closed(self, condition_id: str) -> None:
        """Mark a market position as closed."""
        # Remove one entry at a time so stacked accounting stays accurate.
        if condition_id in self.active_positions:
            self.active_positions.remove(condition_id)

    def set_dynamic_max_order_usdc(self, max_order_usdc: Decimal | None) -> None:
        """Set runtime max-order override used by graduated sizing.

        Passing None restores static settings.max_order_usdc behavior.
        """
        if max_order_usdc is None:
            self._dynamic_max_order_usdc = None
            return
        if max_order_usdc <= 0:
            return
        self._dynamic_max_order_usdc = max_order_usdc

    def set_dynamic_sizing_params(
        self,
        *,
        max_order_usdc: Decimal | None = None,
        min_order_usdc: Decimal | None = None,
        initial_order_pct: Decimal | None = None,
    ) -> None:
        """Set runtime sizing overrides used by graduated sizing."""
        if max_order_usdc is not None and max_order_usdc > 0:
            self._dynamic_max_order_usdc = max_order_usdc
        if min_order_usdc is not None and min_order_usdc > 0:
            self._dynamic_min_order_usdc = min_order_usdc
        if initial_order_pct is not None and initial_order_pct > 0:
            self._dynamic_initial_order_pct = initial_order_pct

    def get_stats(self) -> dict[str, Any]:
        """Get orchestrator statistics."""
        return {
            "total_signals_seen": self.total_signals_seen,
            "total_signals_executed": self.total_signals_executed,
            "active_positions": len(self.active_positions),
            "enabled_strategies": len(self.registry.get_enabled()),
        }

    def get_top_of_book_snapshot(self) -> dict[str, dict[str, float]]:
        """Return the latest top-of-book snapshot.

        Returns:
            {"best_bid": {token_id: price}, "best_ask": {token_id: price}}

        Notes:
            If the websocket feed isn't running or isn't ready, returns empty maps.
        """
        if self._feed is None:
            return {"best_bid": {}, "best_ask": {}}
        try:
            snap = self._feed.get_market_data()
            if not isinstance(snap, dict):
                return {"best_bid": {}, "best_ask": {}}
            best_bid = snap.get("best_bid", {})
            best_ask = snap.get("best_ask", {})
            if not isinstance(best_bid, dict) or not isinstance(best_ask, dict):
                return {"best_bid": {}, "best_ask": {}}
            # Ensure str keys.
            return {
                "best_bid": {str(k): float(v) for k, v in best_bid.items() if v is not None},
                "best_ask": {str(k): float(v) for k, v in best_ask.items() if v is not None},
            }
        except Exception:
            return {"best_bid": {}, "best_ask": {}}

    # ------------------------------------------------------------------
    # CLOB order-book helpers
    # ------------------------------------------------------------------

    _CLOB_API_BASE = "https://clob.polymarket.com"
    _clob_cache: dict[str, float] = {}       # token_id → best_ask price
    _clob_cache_ts: float = 0.0              # epoch of last fetch
    _CLOB_CACHE_TTL: float = 60.0            # re-fetch every 60 s

    def _fetch_clob_best_asks(
        self,
        markets: list,
    ) -> dict[str, float]:
        """Fetch best-ask prices from the CLOB order book for negRisk tokens.

        CLOB books give the real executable ask price, which is authoritative
        for multi-outcome arb edge detection.  We always fetch these
        regardless of whether WSS has data for the token.

        Only fetches books for **negRisk** markets (multi-outcome arb
        candidates).  Binary YES+NO arb is empirically dead after the 2%
        taker fee, so we skip those to avoid hundreds of unnecessary HTTP
        calls on every scan cycle.

        Results are cached for ``_CLOB_CACHE_TTL`` seconds to avoid
        hammering the API every 2-second scan cycle.  Uses a
        ``ThreadPoolExecutor`` for parallel fetching.

        CLOB book asks are sorted **descending** — the best (lowest) ask
        is the *last* element in the list.

        Returns:
            {token_id: best_ask_price} for successfully fetched tokens.
        """
        import time as _time
        import requests as _requests
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # --- Cache check: return cached data if still fresh ----------------
        now = _time.time()
        if self._clob_cache and (now - self._clob_cache_ts) < self._CLOB_CACHE_TTL:
            # Return all cached CLOB prices (CLOB is authoritative for
            # negRisk executable asks — do NOT filter out WSS-covered tokens).
            return dict(self._clob_cache)

        # --- Build list of tokens that need CLOB data ----------------------
        tokens_to_fetch: list[str] = []

        for m in markets:
            # Only fetch for multi-outcome (negRisk) arb candidates.
            if not m.neg_risk_market_id:
                continue
            for t in m.tokens:
                tid = str(t.token_id)
                if tid:
                    tokens_to_fetch.append(tid)

        # Deduplicate, preserve order
        tokens_to_fetch = list(dict.fromkeys(tokens_to_fetch))

        if not tokens_to_fetch:
            return {}

        log.info("📖 Fetching CLOB books for %d negRisk tokens (concurrent, TTL=%ds)...",
                 len(tokens_to_fetch), int(self._CLOB_CACHE_TTL))

        errors: dict[str, int] = {}  # error type → count
        adapter = _requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
        sess = _requests.Session()
        sess.mount("https://", adapter)

        def _fetch_one(tid: str) -> tuple[str, float | None]:
            try:
                resp = sess.get(
                    f"{self._CLOB_API_BASE}/book",
                    params={"token_id": tid},
                    timeout=5.0,
                )
                resp.raise_for_status()
                book = resp.json()
                asks = book.get("asks", [])
                if asks:
                    # Asks are sorted descending — best (lowest) is last
                    best_ask_raw = asks[-1]
                    price = float(best_ask_raw.get("price", 0) if isinstance(best_ask_raw, dict) else best_ask_raw[0])
                    if price > 0:
                        return (tid, price)
            except _requests.exceptions.HTTPError as e:
                err_key = f"HTTP {e.response.status_code}" if e.response is not None else "HTTP ?"
                errors[err_key] = errors.get(err_key, 0) + 1
            except _requests.exceptions.Timeout:
                errors["timeout"] = errors.get("timeout", 0) + 1
            except Exception as e:
                err_key = type(e).__name__
                errors[err_key] = errors.get(err_key, 0) + 1
            return (tid, None)

        result: dict[str, float] = {}
        fetched = 0
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = {pool.submit(_fetch_one, tid): tid for tid in tokens_to_fetch}
            for future in as_completed(futures):
                tid, price = future.result()
                if price is not None:
                    result[tid] = price
                    fetched += 1

        # Update cache
        self._clob_cache = dict(result)
        self._clob_cache_ts = _time.time()

        log.info("📖 CLOB books: fetched %d/%d best-ask prices (%.1fs)",
                 fetched, len(tokens_to_fetch), self._clob_cache_ts - now)
        if errors:
            log.warning("📖 CLOB fetch errors: %s", errors)

        return result
