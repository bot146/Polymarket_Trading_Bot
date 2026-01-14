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

    # Execution profile
    # - hard_guarantee: hedge immediately on any imbalance; prefer taker/atomic style
    # - aggressive_maker: allow brief opportunistic window before forced hedge
    execution_profile: str = "aggressive_maker"  # hard_guarantee|aggressive_maker
    hedge_timeout_ms: int = 1200
    max_inventory_usdc_per_condition: Decimal = Decimal("25")
    max_open_gtc_orders_per_condition: int = 8

    # Paper quoting maintenance (paper mode only)
    enable_paper_requote: bool = True
    requote_max_age_seconds: float = 20.0
    requote_max_distance: Decimal = Decimal("0.02")
    requote_cooldown_ms: int = 750

    # Arbitrage scanning
    # Extra cushion for fees/slippage/leg risk.
    edge_buffer_cents: Decimal = Decimal("0.5")

    # Data API settings
    data_api_first: bool = True
    disable_clob_trade_fetch: bool = False
    clob_trade_fetch_timeout_seconds: float = 8.0
    clob_http_timeout_seconds: float = 20.0
    clob_connect_timeout_seconds: float = 10.0
    cloudflare_block_cooldown_seconds: int = 600
    
    # Market scanning settings
    market_fetch_limit: int = 10000  # Max markets to fetch from API (0 = use DEFAULT_FETCH_LIMIT)
    min_market_volume: Decimal = Decimal("5000")  # Minimum volume threshold in USDC


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

        execution_profile=os.getenv("EXECUTION_PROFILE", "aggressive_maker").strip().lower(),
        hedge_timeout_ms=int(os.getenv("HEDGE_TIMEOUT_MS", "1200")),
        max_inventory_usdc_per_condition=Decimal(os.getenv("MAX_INVENTORY_USDC_PER_CONDITION", "25")),
        max_open_gtc_orders_per_condition=int(os.getenv("MAX_OPEN_GTC_ORDERS_PER_CONDITION", "8")),

        enable_paper_requote=parse_bool(os.getenv("ENABLE_PAPER_REQUOTE"), True),
        requote_max_age_seconds=float(os.getenv("REQUOTE_MAX_AGE_SECONDS", "20.0")),
        requote_max_distance=Decimal(os.getenv("REQUOTE_MAX_DISTANCE", "0.02")),
        requote_cooldown_ms=int(os.getenv("REQUOTE_COOLDOWN_MS", "750")),
        
        data_api_first=parse_bool(os.getenv("DATA_API_FIRST"), True),
        disable_clob_trade_fetch=parse_bool(os.getenv("DISABLE_CLOB_TRADE_FETCH"), False),
        clob_trade_fetch_timeout_seconds=float(os.getenv("CLOB_TRADE_FETCH_TIMEOUT_SECONDS", "8.0")),
        clob_http_timeout_seconds=float(os.getenv("CLOB_HTTP_TIMEOUT_SECONDS", "20.0")),
        clob_connect_timeout_seconds=float(os.getenv("CLOB_CONNECT_TIMEOUT_SECONDS", "10.0")),
        cloudflare_block_cooldown_seconds=int(os.getenv("CLOUDFLARE_BLOCK_COOLDOWN_SECONDS", "600")),
        
        market_fetch_limit=int(os.getenv("MARKET_FETCH_LIMIT", "10000")),
        min_market_volume=Decimal(os.getenv("MIN_MARKET_VOLUME", "5000")),
    )


def is_live(settings: Settings) -> bool:
    return settings.trading_mode == "live" and not settings.kill_switch

