"""Multi-strategy orchestrator for Polymarket trading bot.

This module coordinates multiple strategies and manages their execution.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from polymarket_bot.config import Settings
from polymarket_bot.market_feed import EnhancedMarketFeed
from polymarket_bot.order_book_depth import OrderBookDepthChecker
from polymarket_bot.scanner import MarketScanner
from polymarket_bot.strategy import StrategyRegistry, StrategySignal
from polymarket_bot.strategies.arbitrage_strategy import ArbitrageStrategy
from polymarket_bot.strategies.copy_trading_strategy import CopyTradingStrategy
from polymarket_bot.strategies.guaranteed_win_strategy import GuaranteedWinStrategy
from polymarket_bot.strategies.market_making_strategy import MarketMakingConfig, MarketMakingStrategy
from polymarket_bot.strategies.multi_outcome_arb_strategy import MultiOutcomeArbStrategy
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
    enable_guaranteed_win: bool = True
    enable_stat_arb: bool = False  # Speculative — disabled
    enable_sniping: bool = False  # Speculative — disabled
    enable_market_making: bool = False  # Speculative — disabled
    enable_oracle_sniping: bool = False  # Speculative — disabled
    enable_copy_trading: bool = False
    enable_value_betting: bool = False  # Speculative (Kelly) — disabled
    enable_multi_outcome_arb: bool = True  # Buy all YES in a group for < $1
    
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
        self.active_positions: list[str] = []  # Track active condition_ids
        self.total_signals_seen = 0
        self.total_signals_executed = 0

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

    def scan_and_collect_signals(self) -> list[StrategySignal]:
        """Scan markets and collect signals from all strategies."""
        market_data = self._gather_market_data()
        
        # Run all strategies
        signals = self.registry.scan_all(market_data)
        self.total_signals_seen += len(signals)
        
        if signals:
            log.info(f"Found {len(signals)} total signals across all strategies")
        
        return signals

    def prioritize_signals(self, signals: list[StrategySignal]) -> list[StrategySignal]:
        """Prioritize signals by urgency and expected profit."""
        # Sort by urgency (descending), then expected profit (descending)
        sorted_signals = sorted(
            signals,
            key=lambda s: (s.opportunity.urgency, s.opportunity.expected_profit),
            reverse=True
        )
        return sorted_signals

    def filter_signals(self, signals: list[StrategySignal]) -> list[StrategySignal]:
        """Filter signals based on current state and constraints."""
        filtered = []
        
        for signal in signals:
            # Skip if we already have a position in this market
            condition_id = signal.opportunity.metadata.get("condition_id")
            if condition_id and condition_id in self.active_positions:
                log.debug(f"Skipping {condition_id[:8]}... - already have position")
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
                    clob_best_ask_map = self._fetch_clob_best_asks(
                        high_vol_markets, best_ask_map
                    )

                # Merge: WSS → CLOB → Gamma (priority order)
                def _resolve_best_ask(token_id: str, gamma_price: float) -> float:
                    tid = str(token_id)
                    # 1. WSS feed (most accurate)
                    if tid in best_ask_map and best_ask_map[tid] is not None:
                        return float(best_ask_map[tid])
                    # 2. CLOB order book (executable)
                    if tid in clob_best_ask_map and clob_best_ask_map[tid] is not None:
                        return float(clob_best_ask_map[tid])
                    # 3. Gamma mid-price (fallback)
                    return gamma_price

                # Convert to dict format for strategies
                for market in high_vol_markets:
                    market_dict = {
                        "condition_id": market.condition_id,
                        "question": market.question,
                        "volume": float(market.volume),
                        "active": market.active,
                        "neg_risk_market_id": market.neg_risk_market_id,
                        "group_item_title": market.group_item_title,
                        "tokens": [
                            {
                                "token_id": token.token_id,
                                "outcome": token.outcome,
                                "price": float(token.price),
                                "best_bid": (
                                    float(best_bid_map[str(token.token_id)])
                                    if str(token.token_id) in best_bid_map and best_bid_map[str(token.token_id)] is not None
                                    else float(token.price)
                                ),
                                "best_ask": _resolve_best_ask(token.token_id, float(token.price)),
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
        
        return signals

    def mark_position_active(self, condition_id: str) -> None:
        """Mark a market as having an active position."""
        if condition_id not in self.active_positions:
            self.active_positions.append(condition_id)

    def mark_position_closed(self, condition_id: str) -> None:
        """Mark a market position as closed."""
        if condition_id in self.active_positions:
            self.active_positions.remove(condition_id)

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
        already_have: dict[str, float],
    ) -> dict[str, float]:
        """Fetch best-ask prices from the CLOB order book for tokens that
        don't already have WSS data.

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
            # Return cached prices (excluding tokens the WSS already covers)
            return {k: v for k, v in self._clob_cache.items() if k not in already_have}

        # --- Build list of tokens that need CLOB data ----------------------
        tokens_to_fetch: list[str] = []

        for m in markets:
            # Only fetch for multi-outcome (negRisk) arb candidates.
            if not m.neg_risk_market_id:
                continue
            for t in m.tokens:
                tid = str(t.token_id)
                if tid and tid not in already_have:
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
