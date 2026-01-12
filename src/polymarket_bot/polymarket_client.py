"""
Polymarket API Client
Handles interaction with Polymarket's CLOB API
"""
import logging
import re
import time
import concurrent.futures
from decimal import Decimal, ROUND_DOWN
from typing import List, Dict, Optional, Any

import requests
import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType
from config import Config

logger = logging.getLogger(__name__)


class PolymarketClient:
    """Client for interacting with Polymarket API"""

    DATA_API_BASE_URL = "https://data-api.polymarket.com"
    
    def __init__(self, private_key: Optional[str] = None):
        """
        Initialize Polymarket client
        
        Args:
            private_key: Private key for authenticated operations. 
                        While optional, it is REQUIRED for trade monitoring.
                        Without it, you cannot fetch trades from any account.
        """
        self.api_url = Config.POLYMARKET_API_URL
        self.has_valid_creds = False
        # Circuit breaker for Cloudflare blocks (403 HTML).
        self._blocked_until_ts = 0.0
        self._last_block_reason = None

        # Ensure py-clob-client uses sane HTTP timeouts (prevents indefinite hangs).
        self._configure_clob_http_client_timeouts()
        
        if private_key:
            # Polymarket uses a signer (EOA) that may control a funded proxy wallet.
            # If configured, route order funding through the proxy wallet (funder) and
            # set the appropriate signature type so the exchange can validate signatures.
            signature_type: Optional[int] = None
            funder: Optional[str] = None

            derived_signer: Optional[str] = None
            try:
                from eth_account import Account

                derived_signer = Account.from_key(private_key).address
            except Exception:
                derived_signer = None

            # Explicit overrides take precedence.
            configured_sig_type = int(getattr(Config, "MIRROR_SIGNATURE_TYPE", -1))
            configured_funder = (getattr(Config, "MIRROR_FUNDER_ADDRESS", "") or "").strip()

            if configured_sig_type >= 0:
                signature_type = configured_sig_type

            if configured_funder:
                funder = configured_funder

            # Initialize authenticated client for trading
            self.client = ClobClient(
                self.api_url,
                key=private_key,
                chain_id=137,  # Polygon mainnet
                signature_type=signature_type,
                funder=funder,
            )

            if funder:
                logger.info(
                    "Using proxy/smart-wallet funding for orders (funder=%s, signature_type=%s)%s",
                    funder,
                    signature_type,
                    f" (signer={derived_signer})" if derived_signer else "",
                )
            # Create or derive API credentials for Level 2 authentication
            # This is REQUIRED to fetch trades from any account (including monitoring others)
            try:
                api_creds = self.client.create_or_derive_api_creds()
                self.client.set_api_creds(api_creds)
                self.has_valid_creds = True
                logger.info("Successfully initialized Polymarket client with API credentials")
                logger.info("   You can now execute trades on Polymarket")
            except Exception as e:
                logger.error("=" * 60)
                logger.error("CRITICAL: Failed to create API credentials")
                logger.error(f"   Error: {e}")
                logger.error("")
                logger.error("This bot requires valid API credentials to function.")
                logger.error("Without credentials, you CANNOT monitor or execute trades.")
                logger.error("")
                logger.error("To fix this:")
                logger.error("1. Check your MIRROR_ACCOUNT_PRIVATE_KEY in .env is correct")
                logger.error("2. Ensure you have network connectivity to Polymarket API")
                logger.error("3. Verify your account has API access enabled")
                logger.error("=" * 60)
                self.has_valid_creds = False
        else:
            # Initialize read-only client - but note this WON'T work for trade monitoring
            self.client = ClobClient(self.api_url, chain_id=137)
            self.has_valid_creds = False
            logger.warning("Initialized client without private key")
            logger.warning("   Trade monitoring will NOT work without authentication")

    @staticmethod
    def _configure_clob_http_client_timeouts() -> None:
        """Patch py-clob-client's module-level httpx client to include timeouts.

        py-clob-client uses a global `httpx.Client(http2=True)` without timeouts, which can hang
        indefinitely on some networks. This keeps the bot responsive and makes failures actionable.
        """
        try:
            from py_clob_client.http_helpers import helpers as clob_helpers

            timeout = httpx.Timeout(
                float(getattr(Config, "CLOB_HTTP_TIMEOUT_SECONDS", 20.0)),
                connect=float(getattr(Config, "CLOB_CONNECT_TIMEOUT_SECONDS", 10.0)),
            )
            clob_helpers._http_client = httpx.Client(http2=True, timeout=timeout)
        except Exception:
            # If patching fails, continue with py-clob-client defaults.
            pass
    
    def get_user_trades(self, address: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get recent trades for a user
        
        Fetches trades where the user was either the maker (posted a limit order)
        or the taker (took an existing order). This ensures all trades are captured,
        including buys made by taking existing sell orders.
        
        Args:
            address: User's wallet address
            limit: Maximum number of trades to fetch (note: actual results may vary
                   due to deduplication and pagination behavior)
            
        Returns:
            List of trade dictionaries (deduplicated by trade ID)
        """
        try:
            from py_clob_client.clob_types import TradeParams
            from py_clob_client.http_helpers.helpers import get
            from py_clob_client.endpoints import TRADES
            from py_clob_client.constants import END_CURSOR
            
            # Check if we have valid API credentials
            if not hasattr(self.client, 'creds') or self.client.creds is None:
                logger.error(
                    "Cannot fetch trades: API credentials are not set up. "
                    "Please ensure your POLY_PRIVATE_KEY is correctly configured in .env"
                )
                logger.error(
                    "To fetch account trades, you MUST have valid API credentials. "
                    "The Polymarket API requires Level 2 authentication."
                )
                return []
            
            all_trades = []
            seen_trade_ids = set()
            maker_trade_count = 0
            taker_trade_count = 0
            
            # Fetch trades where user was the MAKER
            logger.debug(f"Fetching maker trades for {address[:10]}...")
            try:
                maker_trades = self.client.get_trades(
                    TradeParams(maker_address=address)
                )
                
                # Handle response format - may be dict or list
                if isinstance(maker_trades, dict) and 'data' in maker_trades:
                    maker_trades = maker_trades['data']
                
                # Add maker trades to result
                if maker_trades:
                    for trade in maker_trades:
                        trade_id = trade.get("id") or trade.get("order_id")
                        if trade_id and trade_id not in seen_trade_ids:
                            seen_trade_ids.add(trade_id)
                            all_trades.append(trade)
                            maker_trade_count += 1
                    logger.debug(f"Found {maker_trade_count} unique maker trades")
            except Exception as e:
                logger.error(f"Error fetching maker trades: {e}")
                # Check if it's an authentication error
                if "401" in str(e) or "authentication" in str(e).lower() or "credentials" in str(e).lower():
                    logger.error(
                        "Authentication failed. Please verify:\n"
                        "1. Your MIRROR_ACCOUNT_PRIVATE_KEY is correct in .env\n"
                        "2. You have network connectivity to Polymarket API\n"
                        "3. Your account has proper API access enabled"
                    )
                    return []
            
            # Fetch trades where user was the TAKER
            # Note: py-clob-client doesn't support taker_address in TradeParams,
            # so we need to make a custom API call
            logger.debug(f"Fetching taker trades for {address[:10]}...")
            try:
                # Build URL manually with taker parameter
                # We need to handle authentication for Level 2 endpoints
                from py_clob_client.headers.headers import create_level_2_headers
                from py_clob_client.clob_types import RequestArgs
                
                # Verify we have a signer as well
                if not hasattr(self.client, 'signer') or self.client.signer is None:
                    logger.warning("No signer available, skipping taker trades")
                    logger.debug(f"Retrieved total {len(all_trades)} unique trades ({maker_trade_count} maker, 0 taker) for {address[:10]}...")
                    return all_trades
                
                # Create headers for authenticated request
                request_args = RequestArgs(method="GET", request_path=TRADES)
                headers = create_level_2_headers(self.client.signer, self.client.creds, request_args)
                
                # Fetch all pages of taker trades
                # Starting cursor for pagination (base64 encoded '0')
                next_cursor = "MA=="
                max_pages = 100  # Safety limit to prevent infinite loops
                page_count = 0
                
                while next_cursor != END_CURSOR and page_count < max_pages:
                    url = f"{self.api_url}{TRADES}?taker={address}&next_cursor={next_cursor}"
                    response = get(url, headers=headers)
                    next_cursor = response.get("next_cursor", END_CURSOR)
                    page_count += 1
                    
                    taker_trades = response.get("data", [])
                    if taker_trades:
                        for trade in taker_trades:
                            trade_id = trade.get("id") or trade.get("order_id")
                            if trade_id and trade_id not in seen_trade_ids:
                                seen_trade_ids.add(trade_id)
                                all_trades.append(trade)
                                taker_trade_count += 1
                
                if page_count >= max_pages:
                    logger.warning(f"Reached pagination limit ({max_pages} pages) for taker trades")
                
                logger.debug(f"Found {taker_trade_count} unique taker trades")
                
            except Exception as e:
                error_msg = str(e)
                if "401" in error_msg or "authentication" in error_msg.lower():
                    logger.error(f"Authentication failed when fetching taker trades: {e}")
                    logger.error("Your API credentials may be invalid or expired")
                else:
                    logger.warning(f"Could not fetch taker trades (may not be critical): {e}")
                # Continue with just maker trades if taker fetch fails
            
            logger.debug(f"Retrieved total {len(all_trades)} unique trades ({maker_trade_count} maker, {taker_trade_count} taker) for {address[:10]}...")
            return all_trades
            
        except Exception as e:
            logger.error(f"Error fetching trades for {address}: {e}")
            return []

    def get_user_trades_data_api(self, address: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent trades for a user from Polymarket's unauthenticated Data API.

        This endpoint has proven to return trade activity even when CLOB /data/trades
        returns empty for the target wallet.

        Args:
            address: User's wallet address
            limit: Best-effort cap on number of items to return (endpoint may return more)

        Returns:
            List of normalized trade dicts.
        """
        try:
            url = f"{self.DATA_API_BASE_URL}/trades"
            params = {"user": address, "limit": int(limit)}
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()

            data = resp.json()
            if not isinstance(data, list):
                logger.warning("Unexpected Data API /trades response shape")
                return []

            # Normalize into a shape compatible with TradeMonitor.parse_trade_details()
            normalized: List[Dict[str, Any]] = []
            for t in data:
                if not isinstance(t, dict):
                    continue

                normalized.append(
                    {
                        # Prefer tx hash so we can dedupe reliably
                        "id": t.get("transactionHash") or t.get("txHash") or t.get("id"),
                        "order_id": t.get("transactionHash") or t.get("txHash") or t.get("id"),
                        "transactionHash": t.get("transactionHash") or t.get("txHash"),
                        "asset_id": t.get("asset") or t.get("asset_id") or t.get("token_id"),
                        "token_id": t.get("asset") or t.get("asset_id") or t.get("token_id"),
                        "condition_id": t.get("conditionId") or t.get("condition_id"),
                        "market": t.get("conditionId") or t.get("condition_id"),
                        "side": (t.get("side") or "").upper() or None,  # CLOB requires uppercase BUY/SELL
                        "price": t.get("price"),
                        "size": t.get("size"),
                        "timestamp": t.get("timestamp"),
                        # Extra metadata (kept for logging/debugging)
                        "title": t.get("title"),
                        "slug": t.get("slug"),
                        "outcome": t.get("outcome"),
                        "proxyWallet": t.get("proxyWallet"),
                        "raw": t,
                        # Data API doesn't expose CLOB status; omit so monitor doesn't reject it
                        "status": None,
                    }
                )

            # Best-effort cap (Data API may return more than requested)
            if limit and len(normalized) > limit:
                normalized = normalized[:limit]

            return normalized
        except Exception as e:
            logger.error(f"Error fetching Data API trades for {address}: {e}")
            return []

    def get_user_trades_best_effort(self, address: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Best-effort trade fetch.

        Source order is configurable:
        - If Config.DATA_API_FIRST is true (default), try Data API first.
        - Otherwise, try CLOB first.
        - If Config.DISABLE_CLOB_TRADE_FETCH is true, never call CLOB for monitoring.
        """
        def _try_data_api() -> List[Dict[str, Any]]:
            trades = self.get_user_trades_data_api(address, limit=limit)
            if trades:
                logger.debug(f"Using Data API trades: {len(trades)}")
            return trades

        def _try_clob() -> List[Dict[str, Any]]:
            if Config.DISABLE_CLOB_TRADE_FETCH:
                return []
            timeout_s = float(getattr(Config, "CLOB_TRADE_FETCH_TIMEOUT_SECONDS", 8.0))
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(self.get_user_trades, address, limit)
                    trades = fut.result(timeout=timeout_s)
                if trades:
                    logger.debug(f"Using CLOB trades: {len(trades)}")
                return trades
            except concurrent.futures.TimeoutError:
                logger.warning("CLOB trade fetch timed out; skipping")
                return []
            except Exception:
                logger.warning("CLOB trade fetch failed")
                return []

        if Config.DATA_API_FIRST:
            trades = _try_data_api()
            if trades:
                return trades
            trades = _try_clob()
            if trades:
                return trades
            logger.info("No trades returned from Data API or CLOB")
            return []

        # CLOB-first mode
        trades = _try_clob()
        if trades:
            return trades
        trades = _try_data_api()
        if trades:
            return trades
        logger.info("No trades returned from CLOB or Data API")
        return []
    
    def get_market_info(self, condition_id: str) -> Optional[Dict[str, Any]]:
        """
        Get information about a specific market
        
        Args:
            condition_id: Market condition ID
            
        Returns:
            Market information dictionary or None if not found
        """
        try:
            market = self.client.get_market(condition_id)
            # Some client versions/stubs type this as Any|str; at runtime it's a dict.
            return market if isinstance(market, dict) else {"raw": market}
        except Exception as e:
            logger.error(f"Error fetching market {condition_id}: {e}")
            return None
    
    def place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        order_type: str = "GTC"
    ) -> Optional[Dict[str, Any]]:
        """
        Place an order on Polymarket
        
        Args:
            token_id: Token ID for the market outcome
            price: Price per share (0-1)
            size: Number of shares to buy/sell
            side: "BUY" or "SELL"
            order_type: Order type (default: GTC - Good Till Cancelled)
            
        Returns:
            Order result dictionary or None if failed
        """
        now = time.time()
        if now < self._blocked_until_ts:
            remaining = int(self._blocked_until_ts - now)
            logger.warning(
                "Order placement skipped: still in cooldown after Cloudflare block (%ss remaining).",
                remaining,
            )
            return None

        try:
            side_u = (side or "").upper()

            # Create order arguments
            # Map order type string to OrderType enum
            # NOTE: Some py-clob-client type stubs model OrderType values as Literals.
            # Keep this as Any to avoid type-checker conflicts while preserving runtime enum use.
            order_type_enum: Any
            if order_type == "GTC":
                order_type_enum = OrderType.GTC
            elif order_type == "FOK":
                order_type_enum = OrderType.FOK
            elif order_type in ["IOC", "FAK"]:
                order_type_enum = OrderType.FAK  # FAK is equivalent to IOC
            else:
                logger.warning(f"Unknown order type '{order_type}', defaulting to GTC")
                order_type_enum = OrderType.GTC

            # Polymarket validates marketable BUY order amounts with strict precision:
            # - maker amount (USD) max 2 decimals
            # - taker amount (shares) max 4 decimals
            # We observed 400s when sending BUY orders as share-sized limit orders in FOK/IOC.
            # For non-GTC BUY orders, use the market-order builder with amount=price*size.
            if side_u == "BUY" and order_type_enum != OrderType.GTC:
                notional = Decimal(str(price)) * Decimal(str(size))
                # Maker amount (USD) => 2 decimals, round down to be safe.
                notional = notional.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                amount = float(notional)
                if amount <= 0:
                    return {
                        "_ok": False,
                        "_error": "invalid_amount",
                        "_details": f"Computed BUY amount is non-positive after rounding: {amount}",
                        "_status_code": None,
                        "_payload": {"price": price, "size": size},
                    }

                market_args = MarketOrderArgs(
                    token_id=token_id,
                    amount=amount,
                    side=side_u,
                    price=price,
                    order_type=order_type_enum,
                )
                signed_order = self.client.create_market_order(market_args)
                posted = self.client.post_order(signed_order, orderType=order_type_enum)

                result: Dict[str, Any]
                if isinstance(posted, dict):
                    result = posted
                else:
                    result = {"raw": posted}
                    for attr in ["id", "order_id", "status"]:
                        if hasattr(posted, attr):
                            result[attr] = getattr(posted, attr)

                logger.info(f"Placed {side_u} order: ${amount:.2f} notional at ${price} ({order_type_enum})")
                return result

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side_u
            )

            # Create (sign) then post the order with the requested type.
            signed_order = self.client.create_order(order_args)
            posted = self.client.post_order(signed_order, orderType=order_type_enum)

            # Normalize return to a dict (some client versions return typed objects)
            result: Dict[str, Any]
            if isinstance(posted, dict):
                result = posted
            else:
                # Best-effort extraction
                result = {"raw": posted}
                for attr in ["id", "order_id", "status"]:
                    if hasattr(posted, attr):
                        result[attr] = getattr(posted, attr)

            logger.info(f"Placed {side} order: {size} shares at ${price} ({order_type_enum})")
            return result
        except Exception as e:
            # py-clob-client raises PolyApiException and may include a Cloudflare HTML page.
            msg = str(e)
            status_code: Optional[int] = None
            raw_payload: Any = None
            try:
                status_code = getattr(e, "status_code", None)
                raw_payload = getattr(e, "error_msg", None)
            except Exception:
                pass

            # If this is a PolyApiException, try to extract the underlying response payload
            # (often a dict like {"error":"insufficient_funds_or_approval", ...}).
            try:
                error_payload = getattr(e, "error_msg", None)
                if isinstance(error_payload, dict):
                    # Prefer canonical keys
                    for k in ["error", "message", "msg", "detail"]:
                        if k in error_payload and isinstance(error_payload[k], str):
                            msg = error_payload[k]
                            break
                    else:
                        # Fall back to full dict rendering
                        msg = str(error_payload)
            except Exception:
                pass

            if logger.isEnabledFor(logging.DEBUG) and raw_payload is not None:
                logger.debug("CLOB API error payload: %s", raw_payload)

            if "403" in msg and ("Cloudflare" in msg or "Sorry, you have been blocked" in msg):
                ray_id = self._extract_cloudflare_ray_id(msg)
                cooldown_seconds = int(getattr(Config, "CLOUDFLARE_BLOCK_COOLDOWN_SECONDS", 600))
                self._blocked_until_ts = time.time() + max(60, cooldown_seconds)
                self._last_block_reason = f"Cloudflare 403 blocked (Ray ID: {ray_id})" if ray_id else "Cloudflare 403 blocked"

                logger.error(
                    "CLOB order rejected by Cloudflare (HTTP 403). %s. Cooling down for %ss to avoid bans.",
                    (f"Ray ID: {ray_id}" if ray_id else "(Ray ID not found)"),
                    int(self._blocked_until_ts - time.time()),
                )
                logger.error(
                    "Likely causes: IP reputation (VPN/datacenter IP), too many rapid requests, or region/access restrictions. "
                    "Fix: run from a residential IP, disable VPN/proxy, slow polling/execution, or contact Polymarket support with the Ray ID."
                )
                return {
                    "_ok": False,
                    "_error": "cloudflare_blocked",
                    "_details": msg,
                    "_status_code": status_code,
                    "_payload": raw_payload,
                }

            logger.error("Error placing order")

            # Surface structured errors for common reject reasons so callers can react.
            lowered = msg.lower()
            if (
                "insufficient_funds_or_approval" in lowered
                or "not enough balance" in lowered
                or "insufficient funds" in lowered
                or "allowance" in lowered
            ):
                return {
                    "_ok": False,
                    "_error": "insufficient_funds_or_approval",
                    "_details": msg,
                    "_status_code": status_code,
                    "_payload": raw_payload,
                }

            # Example: "invalid amount for a marketable BUY order ($0.86), min size: $1"
            if "invalid amount" in lowered and "min size" in lowered:
                return {
                    "_ok": False,
                    "_error": "min_order_notional",
                    "_details": msg,
                    "_status_code": status_code,
                    "_payload": raw_payload,
                }

            return {
                "_ok": False,
                "_error": "order_failed",
                "_details": msg,
                "_status_code": status_code,
                "_payload": raw_payload,
            }

    def update_balance_allowance_best_effort(
        self,
        asset_type: str,
        token_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Best-effort call to CLOB's balance/allowance update endpoint.

        Polymarket accounts that trade via proxy/smart wallets often require approvals
        (ERC20 allowance + conditional token approvals) to be set before orders are accepted.

        Args:
            asset_type: "COLLATERAL" or "CONDITIONAL" (matches py-clob-client AssetType names)
            token_id: Conditional token id when asset_type is CONDITIONAL.
        """
        if not self.has_valid_creds:
            return None
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

            at_name = (asset_type or "").upper()
            if at_name == "COLLATERAL":
                at = AssetType.COLLATERAL
            elif at_name == "CONDITIONAL":
                at = AssetType.CONDITIONAL
            else:
                raise ValueError(f"Unknown asset_type: {asset_type}")
            params = BalanceAllowanceParams(asset_type=at, token_id=token_id, signature_type=-1)
            return self.client.update_balance_allowance(params)
        except Exception as e:
            logger.warning(f"Could not update balance/allowance ({asset_type}): {e}")
            return None

    def get_balance_allowance_best_effort(
        self,
        asset_type: str,
        token_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Best-effort call to fetch current balance/allowance for collateral/conditional."""
        if not self.has_valid_creds:
            return None
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

            at_name = (asset_type or "").upper()
            if at_name == "COLLATERAL":
                at = AssetType.COLLATERAL
            elif at_name == "CONDITIONAL":
                at = AssetType.CONDITIONAL
            else:
                raise ValueError(f"Unknown asset_type: {asset_type}")
            params = BalanceAllowanceParams(asset_type=at, token_id=token_id, signature_type=-1)
            return self.client.get_balance_allowance(params)
        except Exception as e:
            logger.warning(f"Could not fetch balance/allowance ({asset_type}): {e}")
            return None

    def get_wallet_mode(self) -> Dict[str, Any]:
        """Return the effective signer/funder/signature_type used by the underlying client."""
        out: Dict[str, Any] = {"has_valid_creds": bool(self.has_valid_creds)}
        try:
            if hasattr(self.client, "get_address"):
                out["signer_address"] = self.client.get_address()
        except Exception:
            pass
        try:
            builder = getattr(self.client, "builder", None)
            if builder is not None:
                out["signature_type"] = getattr(builder, "sig_type", None)
                out["funder_address"] = getattr(builder, "funder", None)
        except Exception:
            pass
        return out

    @staticmethod
    def _extract_cloudflare_ray_id(html_or_text: str) -> Optional[str]:
        """Extract Cloudflare Ray ID from a Cloudflare block HTML page."""
        # Example: Cloudflare Ray ID: <strong class="font-semibold">9bb2dcee08a91dd8</strong>
        m = re.search(r"Cloudflare Ray ID:\s*<strong[^>]*>([0-9a-fA-F]+)</strong>", html_or_text)
        if m:
            return m.group(1)
        # Fallback: sometimes appears as plain text
        m2 = re.search(r"Cloudflare Ray ID:\s*([0-9a-fA-F]+)", html_or_text)
        if m2:
            return m2.group(1)
        return None
    

    
    def get_open_orders(self, address: str) -> List[Dict[str, Any]]:
        """
        Get open orders for a user
        
        Args:
            address: User's wallet address
            
        Returns:
            List of open order dictionaries
        """
        try:
            # NOTE: This py-clob-client version's get_orders() only supports OpenOrderParams
            # (id/market/asset_id) and does not accept maker/status filters.
            # Returning raw response so callers can inspect if needed.
            orders = self.client.get_orders()

            # Response may be dict or list depending on client version
            if isinstance(orders, dict) and "data" in orders:
                data = orders.get("data") or []
                logger.debug(f"Retrieved {len(data)} open orders")
                return data
            if isinstance(orders, list):
                logger.debug(f"Retrieved {len(orders)} open orders")
                return orders

            logger.debug("Retrieved open orders (unexpected shape)")
            return []
        except Exception as e:
            logger.error(f"Error fetching open orders for {address}: {e}")
            return []

    def cancel_order_best_effort(self, order_id: str) -> bool:
        """Best-effort order cancel.

        Used to avoid leaving resting orders around when running in IOC/FOK style modes.
        """
        if not self.has_valid_creds:
            return False
        oid = (order_id or "").strip()
        if not oid:
            return False
        try:
            # py-clob-client cancel endpoint: DELETE /order
            self.client.cancel(oid)
            return True
        except Exception as e:
            logger.warning(f"Could not cancel order {oid}: {e}")
            return False
