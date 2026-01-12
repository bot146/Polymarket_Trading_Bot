"""
Trade Executor Service
Executes mirror trades on the configured account
"""
import logging
import math
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Any, Optional
from polymarket_client import PolymarketClient
from config import Config

logger = logging.getLogger(__name__)


class TradeExecutor:
    """Executes mirror trades on Polymarket"""

    @staticmethod
    def _round_down(value: float, decimals: int) -> float:
        q = Decimal("1").scaleb(-int(decimals))
        return float(Decimal(str(value)).quantize(q, rounding=ROUND_DOWN))

    def _quantize_order_size(self, side: str, price: float, size: float) -> float:
        """Quantize size to satisfy CLOB precision constraints.

        Observed venue constraint (marketable BUY orders):
        - maker amount max 2 decimals (collateral)
        - taker amount max 4 decimals (shares)

        We enforce:
        - size: max 4 decimals
        - for BUY: also ensure notional (price * size) has max 2 decimals by
          snapping size to the nearest value (rounded down) that yields a 2dp notional.
        """
        s = (side or "").upper()
        p = float(price)
        sz = float(size)

        if sz <= 0:
            return sz

        # Always cap share precision to 4 decimals.
        sz4 = self._round_down(sz, 4)
        if p <= 0:
            return sz4

        if s == "BUY":
            # Ensure collateral notional has max 2 decimals.
            notional2 = self._round_down(p * sz4, 2)
            if notional2 <= 0:
                return 0.0
            sz4 = self._round_down(notional2 / p, 4)
        return sz4

    @staticmethod
    def _parse_balance_to_shares(balance_value: Any) -> Optional[float]:
        """Best-effort parse of a conditional token balance into share units.

        CLOB balance/allowance endpoints commonly return amounts in 6-decimal fixed units
        (micro-units) as ints/strings. This normalizes to shares.
        """
        if balance_value is None:
            return None

        # Nested shapes like {"amount": "123"} or {"balance": "..."}
        if isinstance(balance_value, dict):
            for k in ["balance", "available", "amount", "value"]:
                if k in balance_value:
                    return TradeExecutor._parse_balance_to_shares(balance_value.get(k))
            return None

        # Integers/strings are usually micro-units (1e6)
        try:
            if isinstance(balance_value, int):
                return float(balance_value) / 1_000_000.0
            if isinstance(balance_value, str):
                s = balance_value.strip()
                if s.isdigit():
                    return float(int(s)) / 1_000_000.0
                return float(s)
            if isinstance(balance_value, float):
                return float(balance_value)
        except Exception:
            return None
        return None

    def _get_available_shares_best_effort(self, token_id: str) -> Optional[float]:
        """Fetch available conditional token balance (shares) for SELL capping."""
        try:
            # Best-effort refresh (proxy wallets often need this endpoint to reflect current balances)
            self.client.update_balance_allowance_best_effort("CONDITIONAL", token_id=str(token_id))
        except Exception:
            pass

        try:
            snap = self.client.get_balance_allowance_best_effort("CONDITIONAL", token_id=str(token_id))
        except Exception:
            snap = None

        if not isinstance(snap, dict):
            return None

        # Try a few common shapes/keys
        for key in ["balance", "available", "amount"]:
            if key in snap:
                out = self._parse_balance_to_shares(snap.get(key))
                if out is not None:
                    return out

        # Sometimes nested under data/result
        for container_key in ["data", "result"]:
            if container_key in snap and isinstance(snap.get(container_key), dict):
                out = self._parse_balance_to_shares(snap[container_key])
                if out is not None:
                    return out

        return None
    
    def __init__(self, private_key: str, account_address: str):
        """
        Initialize trade executor
        
        Args:
            private_key: Private key for the mirror account
            account_address: Address of the mirror account
        """
        self.account_address = account_address
        self.client = PolymarketClient(private_key)
        self.position_tracker: Dict[str, float] = {}  # Track lowest price per token_id
        logger.info(f"Initialized trade executor for {account_address[:10]}...")
        
        if Config.DRY_RUN_MODE:
            logger.warning("=" * 60)
            logger.warning("DRY RUN MODE ENABLED - No real trades will be executed")
            logger.warning("=" * 60)
    
    def execute_mirror_trade(self, trade_details: Dict[str, Any]) -> bool:
        """
        Execute a mirror trade based on the original trade details
        
        Args:
            trade_details: Parsed trade details from the monitored account
            
        Returns:
            True if trade executed successfully, False otherwise
        """
        try:
            # Apply mirror ratio to trade size
            original_size = trade_details["size"]
            mirrored_size = original_size * Config.MIRROR_RATIO

            # Allow earlier validation steps to override mirrored size (e.g., downsize for testing).
            override = trade_details.get("mirrored_size_override")
            if override is not None:
                try:
                    mirrored_size = float(override)
                except Exception:
                    pass
            
            # Get trade parameters
            token_id = trade_details["token_id"]
            price = trade_details["price"]
            side = trade_details["side"]

            # Use the configured order execution mode (e.g., GTC/FOK/IOC/FAK).
            execution_mode = Config.ORDER_EXECUTION_MODE
            
            # Check if we should execute based on position management rules
            if Config.ONLY_BUY_AT_LOWER_PRICE and side == "BUY":
                if not self._should_buy_at_price(token_id, price):
                    logger.info(
                        f"Skipping BUY - price ${price:.4f} not lower than "
                        f"existing position (${self.position_tracker.get(token_id, 0):.4f})"
                    )
                    return False

                # SELL sizing safety: if we don't have enough shares to mirror, sell max available.
                if (side or "").upper() == "SELL" and not Config.DRY_RUN_MODE:
                    available = self._get_available_shares_best_effort(str(token_id))
                    if available is not None:
                        if available <= 0:
                            logger.warning("No available balance to SELL for token_id=%s; skipping", token_id)
                            return False
                        if float(mirrored_size) > float(available):
                            logger.warning(
                                "Capping SELL size to available balance: requested %.6f, available %.6f",
                                float(mirrored_size),
                                float(available),
                            )
                            mirrored_size = float(available)
            
            # Check position size limits
            trade_value = mirrored_size * price
            if not self._check_position_limits(token_id, trade_value):
                logger.warning(f"Trade exceeds position limits - skipping")
                return False
            
            # In dry run mode, simulate success
            if Config.DRY_RUN_MODE:
                logger.info(
                    f"[DRY RUN] Executing mirror trade: "
                    f"{side} {mirrored_size:.2f} shares at ${price:.4f} (original: {original_size:.2f})"
                )
                logger.info(f"[DRY RUN] Would place {side} order for {mirrored_size:.2f} shares at ${price:.4f}")
                logger.info(f"[DRY RUN] Trade value: ${trade_value:.2f}")
                
                # Update position tracker even in dry run
                if side == "BUY":
                    self._update_position_tracker(token_id, price)
                
                return True
            
            # Place the mirror order immediately to minimize execution latency
            def _try_place(size_to_try: float, order_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
                size_to_try = self._quantize_order_size(side=side, price=price, size=size_to_try)
                return self.client.place_order(
                    token_id=token_id,
                    price=price,
                    size=size_to_try,
                    side=side,
                    order_type=(order_type or execution_mode),
                )

            result = _try_place(mirrored_size)

            # If rejected due to insufficient funds / missing approval, auto-downsize and retry for testing.
            if (
                Config.AUTO_DOWNSIZE_ENABLED
                and isinstance(result, dict)
                and result.get("_ok") is False
                and result.get("_error") == "insufficient_funds_or_approval"
            ):
                # Try smaller whole-share sizes. Prefer 2 then 1.
                fallback_sizes = [
                    float(int(Config.AUTO_DOWNSIZE_MAX_SHARES)),
                    float(int(Config.AUTO_DOWNSIZE_MIN_SHARES)),
                ]
                # Remove duplicates and any fallbacks >= current attempt.
                fallback_sizes = [s for s in dict.fromkeys(fallback_sizes) if s > 0 and s < float(mirrored_size)]

                for fallback in fallback_sizes:
                    logger.warning(
                        f"Order rejected; retrying with {fallback:.0f} share(s)"
                    )
                    trade_details["mirrored_size_override"] = fallback
                    result = _try_place(fallback)
                    # Success is a truthy dict without an explicit failure marker.
                    if result and not (isinstance(result, dict) and result.get("_ok") is False):
                        mirrored_size = fallback
                        break

            # If rejected due to minimum notional ($1 min), retry with the smallest whole-share
            # size that meets the minimum, but only if it fits within our test max shares.
            if (
                Config.AUTO_DOWNSIZE_ENABLED
                and isinstance(result, dict)
                and result.get("_ok") is False
                and result.get("_error") == "min_order_notional"
                and side == "BUY"
                and price > 0
            ):
                # In real-time modes (FOK/IOC/FAK), do NOT fall back to resting GTC orders.
                if execution_mode != "GTC":
                    logger.warning(
                        "Order rejected due to min-notional and execution_mode is %s; skipping (no resting orders in real-time mode).",
                        execution_mode,
                    )
                    return False

                # In non-real-time mode, GTC is acceptable.
                logger.warning(
                    "Order below min notional for marketable BUY; retrying as GTC (may fill later)"
                )
                result = _try_place(mirrored_size, order_type="GTC")

                # Second: if still failing and we're allowed to adjust size, try to meet min notional.
                if isinstance(result, dict) and result.get("_ok") is False:
                    min_notional = float(getattr(Config, "MIN_MARKETABLE_ORDER_NOTIONAL_USD", 1.0))
                    needed_shares = float(int(math.ceil(min_notional / float(price))))
                    # Respect the configured test cap.
                    if needed_shares <= float(int(Config.AUTO_DOWNSIZE_MAX_SHARES)) and needed_shares > 0:
                        logger.warning(
                            f"Retrying with {needed_shares:.0f} share(s) to satisfy min notional (${min_notional:.2f})"
                        )
                        trade_details["mirrored_size_override"] = needed_shares
                        result = _try_place(needed_shares)
                        if result and not (isinstance(result, dict) and result.get("_ok") is False):
                            mirrored_size = needed_shares
                    else:
                        logger.warning(
                            f"Min-notional would require {needed_shares:.0f} share(s); cap is {float(int(Config.AUTO_DOWNSIZE_MAX_SHARES)):.0f}."
                        )
            
            # Log after trade is placed
            if result and not (isinstance(result, dict) and result.get("_ok") is False):
                # Normalize order id + status shapes
                order_id = None
                try:
                    order_id = result.get("orderID") or result.get("order_id") or result.get("id")
                except Exception:
                    order_id = None
                status = None
                try:
                    status = result.get("status")
                except Exception:
                    status = None

                # If we are running in a real-time mode and the venue returned a LIVE order,
                # cancel it immediately to avoid leaving large resting orders.
                if execution_mode != "GTC" and isinstance(status, str) and status.upper() == "LIVE":
                    if order_id:
                        self.client.cancel_order_best_effort(str(order_id))
                    logger.warning(
                        "Mirror order returned LIVE while in %s mode; cancelled to enforce fill-or-kill behavior.",
                        execution_mode,
                    )
                    return False

                logger.info(
                    f"Mirror trade executed: {side} {mirrored_size:.2f} shares at ${price:.4f} "
                    f"(original: {original_size:.2f}) - Order ID: {order_id}"
                )
                
                # Update position tracker
                if side == "BUY":
                    self._update_position_tracker(token_id, price)
                
                return True
            else:
                if isinstance(result, dict) and result.get("_ok") is False:
                    err = result.get("_error")
                    details = result.get("_details")
                    status_code = result.get("_status_code")
                    if status_code is not None and details:
                        logger.error("Mirror trade rejected: %s (HTTP %s) - %s", err, status_code, details)
                    elif status_code is not None:
                        logger.error("Mirror trade rejected: %s (HTTP %s)", err, status_code)
                    elif details:
                        logger.error("Mirror trade rejected: %s - %s", err, details)
                    else:
                        logger.error("Mirror trade rejected: %s", err)
                    return False
                logger.error("Mirror trade failed - no result returned")
                return False
                
        except Exception as e:
            logger.error(f"Error executing mirror trade: {e}")
            return False
    
    def _should_buy_at_price(self, token_id: str, price: float) -> bool:
        """
        Check if we should buy at this price based on existing positions
        
        Args:
            token_id: Token ID of the market
            price: Proposed buy price
            
        Returns:
            True if we should buy (price is lower than existing), False otherwise
        """
        if token_id not in self.position_tracker:
            # No existing position, allow buy
            return True
        
        existing_price = self.position_tracker[token_id]
        # Only buy if new price is lower than existing
        return price < existing_price
    
    def _update_position_tracker(self, token_id: str, price: float):
        """
        Update the position tracker with the lowest price for this token
        
        Args:
            token_id: Token ID of the market
            price: Price at which we bought
        """
        if token_id not in self.position_tracker or price < self.position_tracker[token_id]:
            self.position_tracker[token_id] = price
            logger.debug(f"Updated position tracker for {token_id}: ${price:.4f}")
    
    def _check_position_limits(self, token_id: str, trade_value: float) -> bool:
        """
        Check if trade respects position size limits
        
        Args:
            token_id: Token ID of the market
            trade_value: Dollar value of the proposed trade
            
        Returns:
            True if within limits, False otherwise
        """
        if trade_value > Config.MAX_POSITION_SIZE_USD:
            logger.warning(
                f"Trade value ${trade_value:.2f} exceeds max position size "
                f"${Config.MAX_POSITION_SIZE_USD}"
            )
            return False
        
        return True
    
    
    def get_open_orders(self) -> list:
        """
        Get open orders for the mirror account
        
        Returns:
            List of open orders
        """
        try:
            orders = self.client.get_open_orders(self.account_address)
            return orders
        except Exception as e:
            logger.error(f"Error fetching open orders: {e}")
            return []
    

