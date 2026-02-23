"""
Polymarket Mirror Trading Bot
Main orchestrator that coordinates monitoring and executing trades
"""
import logging
import threading
import time
import sys
from typing import Dict, Any

from polymarket_bot.config import load_settings, Settings
from polymarket_bot.log_config import setup_logging
from polymarket_bot.polymarket_client import PolymarketClient

log = logging.getLogger(__name__)


class TradeMonitor:
    """Monitors a Polymarket account for new trades"""
    
    def __init__(self, target_address: str, settings: Settings, client: PolymarketClient):
        """
        Initialize trade monitor
        
        Args:
            target_address: Address of the account to monitor
            settings: Bot settings
            client: Polymarket client instance
        """
        self.target_address = target_address
        self.settings = settings
        self.client = client
        self.seen_trade_ids: set[str] = set()
        log.info("Initialized trade monitor for %s...", target_address[:10])
    
    def get_new_trades(self) -> list[Dict[str, Any]]:
        """Check for new trades since last check"""
        try:
            # Fetch recent trades
            all_trades = self.client.get_user_trades_best_effort(
                self.target_address,
                limit=50
            )

            log.debug("Fetched %d trades from source", len(all_trades))
            
            # Filter to only new trades
            new_trades = []
            for trade in all_trades:
                trade_id = (
                    trade.get("transactionHash")
                    or trade.get("txHash")
                    or trade.get("id")
                    or trade.get("order_id")
                )

                if not trade_id:
                    continue

                trade_id = str(trade_id)
                
                # Skip if we've seen this trade before
                if trade_id in self.seen_trade_ids:
                    continue
                
                # Mark as seen and add to new trades
                self.seen_trade_ids.add(trade_id)
                new_trades.append(trade)
            
            if new_trades:
                log.info("Found %d new trade(s)", len(new_trades))
            else:
                log.debug("No new trades detected this cycle")
            
            return new_trades
            
        except Exception as e:
            log.error("Error checking for new trades: %s", e)
            return []
    
    def initialize_seen_trades(self):
        """Initialize the set of seen trades to avoid re-processing old trades"""
        try:
            log.info("Initializing trade history...")
            initial_trades = self.client.get_user_trades_best_effort(
                self.target_address,
                limit=100
            )
            
            for trade in initial_trades:
                trade_id = (
                    trade.get("transactionHash")
                    or trade.get("txHash")
                    or trade.get("id")
                    or trade.get("order_id")
                )
                if trade_id:
                    self.seen_trade_ids.add(str(trade_id))
            
            log.info("Initialized with %d existing trades", len(self.seen_trade_ids))
            
        except Exception as e:
            log.error("Error initializing trade history: %s", e)
    
    def parse_trade_details(self, trade: Dict[str, Any]) -> Dict[str, Any]:
        """Parse trade details into a standardized format"""
        try:
            return {
                "id": trade.get("id") or trade.get("order_id"),
                "token_id": trade.get("asset_id") or trade.get("token_id"),
                "market_id": trade.get("market") or trade.get("condition_id"),
                "side": trade.get("side"),
                "price": float(trade.get("price", 0)),
                "size": float(trade.get("size", 0) or trade.get("original_size", 0)),
                "timestamp": trade.get("timestamp") or trade.get("created_at"),
                "status": trade.get("status"),
                "raw_trade": trade
            }
        except Exception as e:
            log.error("Error parsing trade details: %s", e)
            return {}
    
    def is_valid_trade_for_mirroring(self, trade_details: Dict[str, Any]) -> bool:
        """Check if a trade should be mirrored based on configured rules"""
        # Check if trade has required fields
        if not all(key in trade_details for key in ["token_id", "side", "price", "size"]):
            log.warning("Trade missing required fields")
            return False

        price = float(trade_details.get("price") or 0)
        side = (trade_details.get("side") or "").upper()

        if price <= 0:
            log.warning("Trade has invalid price - skipping")
            return False
        if side not in ["BUY", "SELL"]:
            log.warning("Trade has invalid side '%s' - skipping", trade_details.get('side'))
            return False

        # Simplified small-size mode (BUY only): force a fixed USD notional for buys.
        if self.settings.fixed_mirror_notional_enabled and side == "BUY":
            import math
            target_notional = float(self.settings.fixed_mirror_notional_usd)
            if target_notional <= 0:
                log.warning("FIXED_MIRROR_NOTIONAL_USD must be > 0 - skipping")
                return False

            raw_size = target_notional / price
            size = math.ceil(raw_size * 1000.0) / 1000.0
            if size < 0.001:
                size = 0.001

            if size > float(self.settings.fixed_mirror_max_shares):
                log.warning(
                    "Fixed-notional sizing would require %.3f shares at $%.4f to reach $%.2f; "
                    "cap is %d. Skipping.",
                    size, price, target_notional, int(self.settings.fixed_mirror_max_shares)
                )
                return False

            trade_details["mirrored_size_override"] = float(size)
            return True

        # Alternate simplified mode (BUY only): force a fixed share size for buys.
        if self.settings.fixed_mirror_size_enabled and side == "BUY":
            trade_details["mirrored_size_override"] = float(int(self.settings.fixed_mirror_size_shares))
            return True
        
        # Check trade size limits using *mirrored* trade value
        source_size = float(trade_details["size"])
        mirrored_size = source_size * self.settings.mirror_ratio
        mirrored_trade_value = mirrored_size * price

        if mirrored_trade_value < self.settings.min_trade_size_usd:
            log.debug(
                "Mirrored trade value $%.2f below minimum $%.2f",
                mirrored_trade_value, self.settings.min_trade_size_usd
            )
            return False

        if mirrored_trade_value > self.settings.max_trade_size_usd:
            if self.settings.auto_downsize_enabled and price > 0:
                max_affordable_by_limit = int(self.settings.max_trade_size_usd / price)
                fallback_size = min(
                    self.settings.auto_downsize_max_shares,
                    max(self.settings.auto_downsize_min_shares, float(max_affordable_by_limit)),
                )
                fallback_size = float(int(fallback_size))

                trade_details["mirrored_size_override"] = fallback_size
                log.warning(
                    "Mirrored trade value $%.2f exceeds maximum $%.2f; "
                    "auto-downsizing to %.0f share(s) for testing",
                    mirrored_trade_value, self.settings.max_trade_size_usd, fallback_size
                )
            else:
                log.warning(
                    "Mirrored trade value $%.2f exceeds maximum $%.2f",
                    mirrored_trade_value, self.settings.max_trade_size_usd
                )
                return False
        
        # Check if trade is executed/filled
        status_raw = trade_details.get("status")
        status = (str(status_raw).upper() if status_raw else "")
        if status and status not in ["MATCHED", "FILLED"]:
            log.debug("Trade status '%s' not suitable for mirroring", status)
            return False
        
        return True


class TradeExecutor:
    """Executes mirror trades on Polymarket"""

    def __init__(self, settings: Settings, client: PolymarketClient, account_address: str):
        """Initialize trade executor"""
        self.settings = settings
        self.client = client
        self.account_address = account_address
        self.position_tracker: Dict[str, float] = {}
        log.info("Initialized trade executor for %s...", account_address[:10])
        
        if settings.dry_run_mode:
            log.warning("=" * 60)
            log.warning("DRY RUN MODE ENABLED - No real trades will be executed")
            log.warning("=" * 60)
    
    def execute_mirror_trade(self, trade_details: Dict[str, Any]) -> bool:
        """Execute a mirror trade based on the original trade details"""
        try:
            original_size = trade_details["size"]
            mirrored_size = original_size * self.settings.mirror_ratio

            override = trade_details.get("mirrored_size_override")
            if override is not None:
                try:
                    mirrored_size = float(override)
                except Exception:
                    pass
            
            token_id = trade_details["token_id"]
            price = trade_details["price"]
            side = trade_details["side"]
            execution_mode = self.settings.order_execution_mode
            
            # Check position management rules
            if self.settings.only_buy_at_lower_price and side == "BUY":
                if not self._should_buy_at_price(token_id, price):
                    log.info(
                        "Skipping BUY - price $%.4f not lower than existing position ($%.4f)",
                        price, self.position_tracker.get(token_id, 0)
                    )
                    return False
            
            trade_value = mirrored_size * price
            if not self._check_position_limits(token_id, trade_value):
                log.warning("Trade exceeds position limits - skipping")
                return False
            
            if self.settings.dry_run_mode:
                log.info(
                    "[DRY RUN] Executing mirror trade: %s %.2f shares at $%.4f (original: %.2f)",
                    side, mirrored_size, price, original_size
                )
                if side == "BUY":
                    self._update_position_tracker(token_id, price)
                return True
            
            # Place the mirror order
            result = self.client.place_order(
                token_id=token_id,
                price=price,
                size=mirrored_size,
                side=side,
                order_type=execution_mode,
            )
            
            if result and not (isinstance(result, dict) and result.get("_ok") is False):
                order_id = result.get("orderID") or result.get("order_id") or result.get("id")
                log.info(
                    "Mirror trade executed: %s %.2f shares at $%.4f (original: %.2f) - Order ID: %s",
                    side, mirrored_size, price, original_size, order_id
                )
                
                if side == "BUY":
                    self._update_position_tracker(token_id, price)
                
                return True
            else:
                if isinstance(result, dict) and result.get("_ok") is False:
                    err = result.get("_error")
                    details = result.get("_details")
                    log.error("Mirror trade rejected: %s - %s", err, details)
                else:
                    log.error("Mirror trade failed - no result returned")
                return False
                
        except Exception as e:
            log.error("Error executing mirror trade: %s", e)
            return False
    
    def _should_buy_at_price(self, token_id: str, price: float) -> bool:
        """Check if we should buy at this price based on existing positions"""
        if token_id not in self.position_tracker:
            return True
        return price < self.position_tracker[token_id]
    
    def _update_position_tracker(self, token_id: str, price: float):
        """Update the position tracker with the lowest price for this token"""
        if token_id not in self.position_tracker or price < self.position_tracker[token_id]:
            self.position_tracker[token_id] = price
            log.debug("Updated position tracker for %s: $%.4f", token_id, price)
    
    def _check_position_limits(self, token_id: str, trade_value: float) -> bool:
        """Check if trade respects position size limits"""
        if trade_value > self.settings.max_position_size_usd:
            log.warning(
                "Trade value $%.2f exceeds max position size $%.2f",
                trade_value, self.settings.max_position_size_usd
            )
            return False
        return True


class MirrorTradingBot:
    """Main bot class that orchestrates mirror trading"""
    
    def __init__(self, settings: Settings):
        """Initialize the mirror trading bot"""
        log.info("Initializing Polymarket Mirror Trading Bot")
        
        self.settings = settings
        
        if not settings.target_account_address:
            raise ValueError("TARGET_ACCOUNT_ADDRESS is required in environment")
        
        if not settings.poly_private_key:
            raise ValueError("POLY_PRIVATE_KEY is required in environment")
        
        # Initialize client
        self.client = PolymarketClient(private_key=settings.poly_private_key)
        
        if not self.client.has_valid_creds:
            log.error("=" * 60)
            log.error("INITIALIZATION FAILED")
            log.error("=" * 60)
            log.error("The bot cannot start because API credentials are not available.")
            log.error("Please check the error messages above and fix the configuration.")
            log.error("=" * 60)
            raise ValueError("Failed to initialize API credentials - bot cannot function")
        
        # Initialize components
        self.monitor = TradeMonitor(
            settings.target_account_address,
            settings,
            self.client
        )
        self.executor = TradeExecutor(
            settings,
            self.client,
            settings.mirror_account_address or "unknown"
        )
        
        # Statistics
        self.stats = {
            "trades_detected": 0,
            "trades_mirrored": 0,
            "trades_failed": 0,
            "trades_skipped": 0,
            "start_time": time.time()
        }

        self._last_no_trades_log_ts = 0.0
        
        log.info("Bot initialized successfully")
    
    def process_trade(self, trade: Dict[str, Any]) -> bool:
        """Process a single trade from the monitored account"""
        try:
            trade_details = self.monitor.parse_trade_details(trade)
            
            if not trade_details:
                log.warning("Could not parse trade details")
                self.stats["trades_skipped"] += 1
                return False
            
            if not self.monitor.is_valid_trade_for_mirroring(trade_details):
                log.info("Trade does not meet mirroring criteria - skipping")
                self.stats["trades_skipped"] += 1
                return False
            
            success = self.executor.execute_mirror_trade(trade_details)
            
            if success:
                self.stats["trades_mirrored"] += 1
            else:
                self.stats["trades_failed"] += 1
                log.error("Failed to mirror trade")
            
            return success
            
        except Exception as e:
            log.error("Error processing trade: %s", e)
            self.stats["trades_failed"] += 1
            return False
    
    def run_monitoring_cycle(self):
        """Run a single monitoring cycle"""
        try:
            new_trades = self.monitor.get_new_trades()
            
            if not new_trades:
                now = time.time()
                if now - self._last_no_trades_log_ts >= 60:
                    log.info("No new trades detected (still monitoring)")
                    self._last_no_trades_log_ts = now
                return
            
            self.stats["trades_detected"] += len(new_trades)
            log.info("Detected %d new trade(s)", len(new_trades))
            
            for trade in new_trades:
                self.process_trade(trade)
                
        except Exception as e:
            log.error("Error in monitoring cycle: %s", e)
    
    def print_statistics(self):
        """Print current bot statistics"""
        runtime = time.time() - self.stats["start_time"]
        runtime_hours = runtime / 3600
        
        log.info("=" * 60)
        log.info("Bot Statistics:")
        log.info("  Runtime: %.2f hours", runtime_hours)
        log.info("  Trades Detected: %d", self.stats['trades_detected'])
        log.info("  Trades Mirrored: %d", self.stats['trades_mirrored'])
        log.info("  Trades Failed: %d", self.stats['trades_failed'])
        log.info("  Trades Skipped: %d", self.stats['trades_skipped'])
        log.info("=" * 60)
    
    def run(self):
        """Main bot loop"""
        log.info("Starting mirror trading bot...")
        
        self.monitor.initialize_seen_trades()
        
        log.info("Monitoring %s... for new trades", self.settings.target_account_address[:10])
        
        poll_interval = (
            self.settings.fast_poll_interval_seconds 
            if self.settings.use_fast_polling 
            else self.settings.poll_interval_seconds
        )
        
        if self.settings.use_fast_polling:
            log.info("FAST POLLING MODE ENABLED - Poll interval: %ss (near-real-time)", poll_interval)
            log.info("Order execution mode: %s", self.settings.order_execution_mode)
        else:
            log.info("Poll interval: %d seconds", poll_interval)
        
        try:
            cycle_count = 0
            while True:
                cycle_count += 1
                
                self.run_monitoring_cycle()

                if cycle_count % 60 == 0:
                    log.info("Heartbeat: monitoring loop is running")
                
                cycles_per_10_min = max(1, int(600 / poll_interval))
                if cycle_count % cycles_per_10_min == 0:
                    self.print_statistics()
                
                time.sleep(poll_interval)
                
        except KeyboardInterrupt:
            log.info("\nShutting down bot...")
            self.print_statistics()
            log.info("Bot stopped")
        except Exception as e:
            log.error("Fatal error in bot loop: %s", e)
            self.print_statistics()
            raise


def main() -> None:
    """Main entry point"""
    try:
        settings = load_settings()
        setup_logging(settings.log_level)
        
        bot = MirrorTradingBot(settings)
        bot.run()
    except Exception as e:
        log.error("Failed to start bot: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
