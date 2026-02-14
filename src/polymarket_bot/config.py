from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import os
from pathlib import Path

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
    max_concurrent_trades: int = 5

    # Strategy toggles
    enable_arbitrage: bool = True
    enable_guaranteed_win: bool = False
    enable_multi_outcome_arb: bool = True
    enable_stat_arb: bool = False
    enable_sniping: bool = False
    enable_market_making: bool = False
    enable_value_betting: bool = False
    enable_oracle_sniping_strategy: bool = False

    # Risk
    max_order_usdc: Decimal = Decimal("20")
    min_order_usdc: Decimal = Decimal("2")       # Minimum order size per trade
    initial_order_pct: Decimal = Decimal("25")    # First entry = 25% of max_order_usdc
    min_edge_cents: Decimal = Decimal("0.5")

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
    edge_buffer_cents: Decimal = Decimal("0.2")

    # Data API settings
    data_api_first: bool = True
    disable_clob_trade_fetch: bool = False
    clob_trade_fetch_timeout_seconds: float = 8.0
    clob_http_timeout_seconds: float = 20.0
    clob_connect_timeout_seconds: float = 10.0
    cloudflare_block_cooldown_seconds: int = 600
    
    # Market scanning settings
    market_fetch_limit: int = 10000  # Max markets to fetch from API (0 = use DEFAULT_FETCH_LIMIT)
    min_market_volume: Decimal = Decimal("1000")  # Minimum volume threshold in USDC

    # Fee modeling
    taker_fee_rate: Decimal = Decimal("0.02")    # 2% taker fee
    maker_fee_rate: Decimal = Decimal("0.005")   # 0.5% maker fee (rebate on some venues)

    # Circuit breaker / risk limits
    max_daily_loss_usdc: Decimal = Decimal("50")   # Stop trading after this daily loss
    max_drawdown_pct: Decimal = Decimal("10")       # Stop if portfolio drops X% from peak
    max_consecutive_losses: int = 5                  # Pause after N consecutive losses
    circuit_breaker_cooldown_minutes: int = 30       # How long to pause after breaker trips

    # Position exit rules
    profit_target_pct: Decimal = Decimal("5")        # Close at 5% profit
    stop_loss_pct: Decimal = Decimal("3")             # Close at 3% loss
    max_position_age_hours: float = 24.0              # Close after 24 hours
    exit_check_interval_seconds: float = 15.0         # How often to check exits

    # Order book depth
    min_book_depth_usdc: Decimal = Decimal("10")     # Min liquidity to trade
    verify_book_depth: bool = True                    # Enable depth checks

    # Event-driven oracle
    enable_oracle_sniping: bool = True
    oracle_check_interval_seconds: float = 10.0
    oracle_min_confidence: Decimal = Decimal("0.95")  # Only trade when very confident

    # Dashboard
    enable_dashboard: bool = False
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8050

    # Copy / whale trading
    enable_copy_trading: bool = False
    whale_min_trade_usdc: Decimal = Decimal("1000")  # Min trade size to follow
    whale_addresses: str = ""  # Comma-separated addresses

    # New strategy flags
    enable_conditional_arb: bool = False      # Cumulative bracket arb — disabled by default
    enable_liquidity_rewards: bool = False    # Liquidity reward harvesting — disabled by default
    enable_near_resolution: bool = False      # Near-resolution sniping — disabled by default
    enable_arb_stacking: bool = False         # Allow multiple executions on same arb group
    max_arb_stacks: int = 3                   # Max stacked positions per arb group
    near_resolution_max_hours: float = 48.0   # Max hours to end for near-resolution
    liquidity_rewards_max_position: Decimal = Decimal("50")  # Max capital per reward market

    # Paper fill realism
    paper_fill_probability: Decimal = Decimal("0.5")  # 50% fill chance for maker
    paper_require_volume_cross: bool = True            # Require volume to cross price level
    paper_random_seed: int | None = None               # Deterministic paper fills when set
    paper_start_balance: Decimal = Decimal("100")      # Starting virtual bankroll in paper mode
    paper_wallet_path: str = ""                        # Optional runtime wallet config file path
    paper_sizing_tiers: str = "100:1.00,1000:1.10,5000:1.20,10000:1.30"
    paper_wallet_refresh_seconds: float = 5.0
    paper_reset_on_start: bool = True                   # Reset positions/wallet each paper restart

    # Runtime config reload
    runtime_reload_env: bool = True
    runtime_reload_seconds: float = 5.0


def load_settings(env_file: str | None = None) -> Settings:
    """Load settings from .env and environment variables.

    Safe defaults:
    - trading_mode defaults to paper
    - kill_switch defaults to ON unless explicitly disabled
    """

    if env_file:
        load_dotenv(env_file, override=True)
    else:
        load_dotenv()

    trading_mode = os.getenv("TRADING_MODE", "paper").strip().lower()
    kill_switch = os.getenv("KILL_SWITCH", "1").strip() not in {"0", "false", "no"}

    # Helper to parse bool env vars
    def parse_bool(val: str | None, default: bool) -> bool:
        if val is None:
            return default
        return val.strip().lower() in {"1", "true", "yes"}

    def parse_opt_int(val: str | None) -> int | None:
        if val is None:
            return None
        v = val.strip()
        if v == "":
            return None
        return int(v)

    return Settings(
        poly_host=os.getenv("POLY_HOST", "https://clob.polymarket.com").strip(),
        poly_chain_id=int(os.getenv("POLY_CHAIN_ID", "137")),
        poly_private_key=os.getenv("POLY_PRIVATE_KEY") or None,
        poly_funder_address=os.getenv("POLY_FUNDER_ADDRESS") or None,
        poly_signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "1")),
        trading_mode=trading_mode,
        kill_switch=kill_switch,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        max_concurrent_trades=int(os.getenv("MAX_CONCURRENT_TRADES", "5")),

        enable_arbitrage=parse_bool(os.getenv("ENABLE_ARBITRAGE"), True),
        enable_guaranteed_win=parse_bool(os.getenv("ENABLE_GUARANTEED_WIN"), False),
        enable_multi_outcome_arb=parse_bool(os.getenv("ENABLE_MULTI_OUTCOME_ARB"), True),
        enable_stat_arb=parse_bool(os.getenv("ENABLE_STAT_ARB"), False),
        enable_sniping=parse_bool(os.getenv("ENABLE_SNIPING"), False),
        enable_market_making=parse_bool(os.getenv("ENABLE_MARKET_MAKING"), False),
        enable_value_betting=parse_bool(os.getenv("ENABLE_VALUE_BETTING"), False),
        enable_oracle_sniping_strategy=parse_bool(os.getenv("ENABLE_ORACLE_SNIPING_STRATEGY"), False),
        max_order_usdc=Decimal(os.getenv("MAX_ORDER_USDC", "20")),
        min_order_usdc=Decimal(os.getenv("MIN_ORDER_USDC", "2")),
        initial_order_pct=Decimal(os.getenv("INITIAL_ORDER_PCT", "25")),
        min_edge_cents=Decimal(os.getenv("MIN_EDGE_CENTS", "0.5")),
        edge_buffer_cents=Decimal(os.getenv("EDGE_BUFFER_CENTS", "0.2")),

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
        min_market_volume=Decimal(os.getenv("MIN_MARKET_VOLUME", "1000")),

        # Fee modeling
        taker_fee_rate=Decimal(os.getenv("TAKER_FEE_RATE", "0.02")),
        maker_fee_rate=Decimal(os.getenv("MAKER_FEE_RATE", "0.005")),

        # Circuit breaker
        max_daily_loss_usdc=Decimal(os.getenv("MAX_DAILY_LOSS_USDC", "50")),
        max_drawdown_pct=Decimal(os.getenv("MAX_DRAWDOWN_PCT", "10")),
        max_consecutive_losses=int(os.getenv("MAX_CONSECUTIVE_LOSSES", "5")),
        circuit_breaker_cooldown_minutes=int(os.getenv("CIRCUIT_BREAKER_COOLDOWN_MINUTES", "30")),

        # Exit rules
        profit_target_pct=Decimal(os.getenv("PROFIT_TARGET_PCT", "5")),
        stop_loss_pct=Decimal(os.getenv("STOP_LOSS_PCT", "3")),
        max_position_age_hours=float(os.getenv("MAX_POSITION_AGE_HOURS", "24.0")),
        exit_check_interval_seconds=float(os.getenv("EXIT_CHECK_INTERVAL_SECONDS", "15.0")),

        # Order book depth
        min_book_depth_usdc=Decimal(os.getenv("MIN_BOOK_DEPTH_USDC", "10")),
        verify_book_depth=parse_bool(os.getenv("VERIFY_BOOK_DEPTH"), True),

        # Oracle
        enable_oracle_sniping=parse_bool(os.getenv("ENABLE_ORACLE_SNIPING"), True),
        oracle_check_interval_seconds=float(os.getenv("ORACLE_CHECK_INTERVAL_SECONDS", "10.0")),
        oracle_min_confidence=Decimal(os.getenv("ORACLE_MIN_CONFIDENCE", "0.95")),

        # Dashboard
        enable_dashboard=parse_bool(os.getenv("ENABLE_DASHBOARD"), False),
        dashboard_host=os.getenv("DASHBOARD_HOST", "127.0.0.1").strip(),
        dashboard_port=int(os.getenv("DASHBOARD_PORT", "8050")),

        # Copy trading
        enable_copy_trading=parse_bool(os.getenv("ENABLE_COPY_TRADING"), False),
        whale_min_trade_usdc=Decimal(os.getenv("WHALE_MIN_TRADE_USDC", "1000")),
        whale_addresses=os.getenv("WHALE_ADDRESSES", ""),

        # New strategy flags
        enable_conditional_arb=parse_bool(os.getenv("ENABLE_CONDITIONAL_ARB"), False),
        enable_liquidity_rewards=parse_bool(os.getenv("ENABLE_LIQUIDITY_REWARDS"), False),
        enable_near_resolution=parse_bool(os.getenv("ENABLE_NEAR_RESOLUTION"), False),
        enable_arb_stacking=parse_bool(os.getenv("ENABLE_ARB_STACKING"), False),
        max_arb_stacks=int(os.getenv("MAX_ARB_STACKS", "3")),
        near_resolution_max_hours=float(os.getenv("NEAR_RESOLUTION_MAX_HOURS", "48.0")),
        liquidity_rewards_max_position=Decimal(os.getenv("LIQUIDITY_REWARDS_MAX_POSITION", "50")),

        # Paper fill realism
        paper_fill_probability=Decimal(os.getenv("PAPER_FILL_PROBABILITY", "0.5")),
        paper_require_volume_cross=parse_bool(os.getenv("PAPER_REQUIRE_VOLUME_CROSS"), True),
        paper_random_seed=parse_opt_int(os.getenv("PAPER_RANDOM_SEED")),
        paper_start_balance=Decimal(os.getenv("PAPER_START_BALANCE", "100")),
        paper_wallet_path=(os.getenv("PAPER_WALLET_PATH") or "").strip() or str(Path.home() / ".polymarket_bot" / "paper_wallet.json"),
        paper_sizing_tiers=os.getenv("PAPER_SIZING_TIERS", "100:1.00,1000:1.10,5000:1.20,10000:1.30").strip(),
        paper_wallet_refresh_seconds=float(os.getenv("PAPER_WALLET_REFRESH_SECONDS", "5.0")),
        paper_reset_on_start=parse_bool(os.getenv("PAPER_RESET_ON_START"), True),

        runtime_reload_env=parse_bool(os.getenv("RUNTIME_RELOAD_ENV"), True),
        runtime_reload_seconds=float(os.getenv("RUNTIME_RELOAD_SECONDS", "5.0")),
    )


def is_live(settings: Settings) -> bool:
    return settings.trading_mode == "live" and not settings.kill_switch

