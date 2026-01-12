from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    # Polymarket
    poly_host: str = "https://clob.polymarket.com"
    poly_chain_id: int = 137
    poly_private_key: str | None = None
    poly_funder_address: str | None = None
    poly_signature_type: int = 1  # Magic/proxy by default

    # Runtime
    trading_mode: str = "paper"  # paper|live
    kill_switch: bool = True
    log_level: str = "INFO"

    # Risk
    max_order_usdc: Decimal = Decimal("20")
    min_edge_cents: Decimal = Decimal("1.5")

    # Arbitrage scanning
    # Extra cushion for fees/slippage/leg risk.
    edge_buffer_cents: Decimal = Decimal("0.5")

    # Mirror Trading Configuration
    target_account_address: str | None = None
    mirror_account_address: str | None = None
    mirror_proxy_address: str | None = None
    mirror_ratio: float = 1.0
    poll_interval_seconds: int = 10
    min_trade_size_usd: float = 1.0
    max_trade_size_usd: float = 10000.0
    
    # Fast polling for near-real-time mirroring
    use_fast_polling: bool = False
    fast_poll_interval_seconds: float = 1.0
    order_execution_mode: str = "GTC"  # GTC, FOK, IOC, FAK
    
    # Simplified small-size modes
    fixed_mirror_notional_enabled: bool = True
    fixed_mirror_notional_usd: float = 3.0
    fixed_mirror_max_shares: int = 50
    fixed_mirror_size_enabled: bool = False
    fixed_mirror_size_shares: float = 2.0
    
    # Risk management for mirror trading
    dry_run_mode: bool = False
    only_buy_at_lower_price: bool = False
    reserve_balance_usd: float = 10.0
    max_position_size_usd: float = 50.0
    auto_downsize_enabled: bool = True
    auto_downsize_max_shares: float = 2.0
    auto_downsize_min_shares: float = 1.0
    min_marketable_order_notional_usd: float = 3.0
    
    # Data API settings
    data_api_first: bool = True
    disable_clob_trade_fetch: bool = False
    clob_trade_fetch_timeout_seconds: float = 8.0
    clob_http_timeout_seconds: float = 20.0
    clob_connect_timeout_seconds: float = 10.0
    cloudflare_block_cooldown_seconds: int = 600


def load_settings(env_file: str | None = None) -> Settings:
    """Load settings from .env and environment variables.

    Safe defaults:
    - trading_mode defaults to paper
    - kill_switch defaults to ON unless explicitly disabled
    """

    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()

    trading_mode = os.getenv("TRADING_MODE", "paper").strip().lower()
    kill_switch = os.getenv("KILL_SWITCH", "1").strip() not in {"0", "false", "no"}

    # Helper to parse bool env vars
    def parse_bool(val: str | None, default: bool) -> bool:
        if val is None:
            return default
        return val.strip().lower() in {"1", "true", "yes"}

    return Settings(
        poly_host=os.getenv("POLY_HOST", "https://clob.polymarket.com").strip(),
        poly_chain_id=int(os.getenv("POLY_CHAIN_ID", "137")),
        poly_private_key=os.getenv("POLY_PRIVATE_KEY") or None,
        poly_funder_address=os.getenv("POLY_FUNDER_ADDRESS") or None,
        poly_signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "1")),
        trading_mode=trading_mode,
        kill_switch=kill_switch,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        max_order_usdc=Decimal(os.getenv("MAX_ORDER_USDC", "20")),
        min_edge_cents=Decimal(os.getenv("MIN_EDGE_CENTS", "1.5")),
        edge_buffer_cents=Decimal(os.getenv("EDGE_BUFFER_CENTS", "0.5")),
        
        # Mirror Trading settings
        target_account_address=os.getenv("TARGET_ACCOUNT_ADDRESS") or None,
        mirror_account_address=os.getenv("MIRROR_ACCOUNT_ADDRESS") or None,
        mirror_proxy_address=os.getenv("MIRROR_PROXY_ADDRESS") or None,
        mirror_ratio=float(os.getenv("MIRROR_RATIO", "1.0")),
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "10")),
        min_trade_size_usd=float(os.getenv("MIN_TRADE_SIZE_USD", "1.0")),
        max_trade_size_usd=float(os.getenv("MAX_TRADE_SIZE_USD", "10000.0")),
        
        use_fast_polling=parse_bool(os.getenv("USE_FAST_POLLING"), False),
        fast_poll_interval_seconds=float(os.getenv("FAST_POLL_INTERVAL_SECONDS", "1.0")),
        order_execution_mode=os.getenv("ORDER_EXECUTION_MODE", "GTC").strip().upper(),
        
        fixed_mirror_notional_enabled=parse_bool(os.getenv("FIXED_MIRROR_NOTIONAL_ENABLED"), True),
        fixed_mirror_notional_usd=float(os.getenv("FIXED_MIRROR_NOTIONAL_USD", "3.0")),
        fixed_mirror_max_shares=int(os.getenv("FIXED_MIRROR_MAX_SHARES", "50")),
        fixed_mirror_size_enabled=parse_bool(os.getenv("FIXED_MIRROR_SIZE_ENABLED"), False),
        fixed_mirror_size_shares=float(os.getenv("FIXED_MIRROR_SIZE_SHARES", "2.0")),
        
        dry_run_mode=parse_bool(os.getenv("DRY_RUN_MODE"), False),
        only_buy_at_lower_price=parse_bool(os.getenv("ONLY_BUY_AT_LOWER_PRICE"), False),
        reserve_balance_usd=float(os.getenv("RESERVE_BALANCE_USD", "10.0")),
        max_position_size_usd=float(os.getenv("MAX_POSITION_SIZE_USD", "50.0")),
        auto_downsize_enabled=parse_bool(os.getenv("AUTO_DOWNSIZE_ENABLED"), True),
        auto_downsize_max_shares=float(os.getenv("AUTO_DOWNSIZE_MAX_SHARES", "2.0")),
        auto_downsize_min_shares=float(os.getenv("AUTO_DOWNSIZE_MIN_SHARES", "1.0")),
        min_marketable_order_notional_usd=float(os.getenv("MIN_MARKETABLE_ORDER_NOTIONAL_USD", "3.0")),
        
        data_api_first=parse_bool(os.getenv("DATA_API_FIRST"), True),
        disable_clob_trade_fetch=parse_bool(os.getenv("DISABLE_CLOB_TRADE_FETCH"), False),
        clob_trade_fetch_timeout_seconds=float(os.getenv("CLOB_TRADE_FETCH_TIMEOUT_SECONDS", "8.0")),
        clob_http_timeout_seconds=float(os.getenv("CLOB_HTTP_TIMEOUT_SECONDS", "20.0")),
        clob_connect_timeout_seconds=float(os.getenv("CLOB_CONNECT_TIMEOUT_SECONDS", "10.0")),
        cloudflare_block_cooldown_seconds=int(os.getenv("CLOUDFLARE_BLOCK_COOLDOWN_SECONDS", "600")),
    )


def is_live(settings: Settings) -> bool:
    return settings.trading_mode == "live" and not settings.kill_switch

