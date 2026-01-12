"""
Trade Monitor Service
Monitors a target account for new trades
"""
import logging
import time
import math
from typing import List, Dict, Set, Any, Optional
from polymarket_client import PolymarketClient
from config import Config

logger = logging.getLogger(__name__)


class TradeMonitor:
    """Monitors a Polymarket account for new trades"""
    
    def __init__(self, target_address: str, private_key: Optional[str] = None):
        """
        Initialize trade monitor
        
        Args:
            target_address: Address of the account to monitor
            private_key: Optional private key for authenticated API access
        """
        self.target_address = target_address
        # Use authenticated client if private key provided (needed for get_trades)
        self.client = PolymarketClient(private_key=private_key)
        self.seen_trade_ids: Set[str] = set()
        logger.info(f"Initialized trade monitor for {target_address[:10]}...")
    
    def get_new_trades(self) -> List[Dict[str, Any]]:
        """
        Check for new trades since last check
        
        Returns:
            List of new trade dictionaries
        """
        try:
            # Fetch recent trades
            all_trades = self.client.get_user_trades_best_effort(
                self.target_address,
                limit=50
            )

            logger.debug(f"Fetched {len(all_trades)} trades from source")
            
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
                logger.info(f"Found {len(new_trades)} new trade(s)")
            else:
                logger.debug("No new trades detected this cycle")
            
            return new_trades
            
        except Exception as e:
            logger.error(f"Error checking for new trades: {e}")
            return []
    
    def initialize_seen_trades(self):
        """
        Initialize the set of seen trades to avoid re-processing old trades
        """
        try:
            logger.info("Initializing trade history...")
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
            
            logger.info(f"Initialized with {len(self.seen_trade_ids)} existing trades")
            
        except Exception as e:
            logger.error(f"Error initializing trade history: {e}")
    
    def parse_trade_details(self, trade: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse trade details into a standardized format
        
        Args:
            trade: Raw trade dictionary from API
            
        Returns:
            Parsed trade details
        """
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
            logger.error(f"Error parsing trade details: {e}")
            return {}
    
    def is_valid_trade_for_mirroring(self, trade_details: Dict[str, Any]) -> bool:
        """
        Check if a trade should be mirrored based on configured rules
        
        Args:
            trade_details: Parsed trade details
            
        Returns:
            True if trade should be mirrored, False otherwise
        """
        # Check if trade has required fields
        if not all(key in trade_details for key in ["token_id", "side", "price", "size"]):
            logger.warning("Trade missing required fields")
            return False

        price = float(trade_details.get("price") or 0)
        side = (trade_details.get("side") or "").upper()

        if price <= 0:
            logger.warning("Trade has invalid price - skipping")
            return False
        if side not in ["BUY", "SELL"]:
            logger.warning(f"Trade has invalid side '{trade_details.get('side')}' - skipping")
            return False

        # Simplified small-size mode (BUY only): force a fixed USD notional for buys.
        # Sells should mirror size (and are capped by available balance in the executor).
        if Config.FIXED_MIRROR_NOTIONAL_ENABLED and side == "BUY":
            target_notional = float(Config.FIXED_MIRROR_NOTIONAL_USD)
            if target_notional <= 0:
                logger.warning("FIXED_MIRROR_NOTIONAL_USD must be > 0 - skipping")
                return False

            # Compute a fractional share size that is as close as possible to the target notional,
            # but rounds UP to avoid falling below venue min-notional rules.
            # Use 0.001 share granularity as a conservative default.
            raw_size = target_notional / price
            size = math.ceil(raw_size * 1000.0) / 1000.0
            if size < 0.001:
                size = 0.001

            if size > float(Config.FIXED_MIRROR_MAX_SHARES):
                logger.warning(
                    f"Fixed-notional sizing would require {size:.3f} shares at ${price:.4f} to reach ${target_notional:.2f}; "
                    f"cap is {int(Config.FIXED_MIRROR_MAX_SHARES)}. Skipping."
                )
                return False

            trade_details["mirrored_size_override"] = float(size)
            return True

        # Alternate simplified mode (BUY only): force a fixed share size for buys.
        if Config.FIXED_MIRROR_SIZE_ENABLED and side == "BUY":
            trade_details["mirrored_size_override"] = float(int(Config.FIXED_MIRROR_SIZE_SHARES))
            return True
        
        # Check trade size limits using *mirrored* trade value (since mirroring can scale sizes).
        source_size = float(trade_details["size"])
        mirrored_size = source_size * Config.MIRROR_RATIO
        mirrored_trade_value = mirrored_size * price

        if mirrored_trade_value < Config.MIN_TRADE_SIZE_USD:
            logger.debug(
                f"Mirrored trade value ${mirrored_trade_value:.2f} below minimum ${Config.MIN_TRADE_SIZE_USD}"
            )
            return False

        if mirrored_trade_value > Config.MAX_TRADE_SIZE_USD:
            if Config.AUTO_DOWNSIZE_ENABLED and price > 0:
                max_affordable_by_limit = int(Config.MAX_TRADE_SIZE_USD / price)
                fallback_size = min(
                    Config.AUTO_DOWNSIZE_MAX_SHARES,
                    max(Config.AUTO_DOWNSIZE_MIN_SHARES, float(max_affordable_by_limit)),
                )
                fallback_size = float(int(fallback_size))  # force whole shares

                # Tell the executor to place a smaller mirrored order instead of skipping.
                trade_details["mirrored_size_override"] = fallback_size
                logger.warning(
                    f"Mirrored trade value ${mirrored_trade_value:.2f} exceeds maximum ${Config.MAX_TRADE_SIZE_USD}; "
                    f"auto-downsizing to {fallback_size:.0f} share(s) for testing"
                )
            else:
                logger.warning(
                    f"Mirrored trade value ${mirrored_trade_value:.2f} exceeds maximum ${Config.MAX_TRADE_SIZE_USD}"
                )
                return False
        
        # Check if trade is executed/filled
        # Only mirror trades that have been matched/filled, not pending orders
        status_raw = trade_details.get("status")
        status = (str(status_raw).upper() if status_raw else "")
        # Data API trades may omit status; treat missing status as already-executed.
        if status and status not in ["MATCHED", "FILLED"]:
            logger.debug(f"Trade status '{status}' not suitable for mirroring")
            return False
        
        return True
