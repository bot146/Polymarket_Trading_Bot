from __future__ import annotations

from decimal import Decimal

from polymarket_bot.config import Settings, load_settings
from polymarket_bot.orchestrator import OrchestratorConfig, StrategyOrchestrator
from polymarket_bot.paper_trading import PaperBlotter
from polymarket_bot.strategy import Opportunity, StrategySignal, StrategyType, Trade


def _mk_signal(condition_id: str = "c1") -> StrategySignal:
    opp = Opportunity(
        strategy_type=StrategyType.CONDITIONAL_ARB,
        expected_profit=Decimal("1.0"),
        confidence=Decimal("0.9"),
        urgency=8,
        metadata={"condition_id": condition_id},
    )
    # cost = 40 * 0.5 = 20
    trade = Trade(token_id="t1", side="BUY", size=Decimal("40"), price=Decimal("0.5"), order_type="FOK")
    return StrategySignal(
        opportunity=opp,
        trades=[trade],
        max_total_cost=Decimal("20"),
        min_expected_return=Decimal("40"),
    )


def test_load_settings_parses_strategy_toggles_and_seed(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "TRADING_MODE=paper",
                "KILL_SWITCH=0",
                "MAX_CONCURRENT_TRADES=7",
                "ENABLE_ARBITRAGE=false",
                "ENABLE_GUARANTEED_WIN=false",
                "ENABLE_MULTI_OUTCOME_ARB=false",
                "ENABLE_STAT_ARB=true",
                "ENABLE_SNIPING=true",
                "ENABLE_MARKET_MAKING=true",
                "ENABLE_VALUE_BETTING=true",
                "ENABLE_ORACLE_SNIPING_STRATEGY=true",
                "ENABLE_CONDITIONAL_ARB=true",
                "ENABLE_LIQUIDITY_REWARDS=true",
                "ENABLE_NEAR_RESOLUTION=true",
                "ENABLE_ARB_STACKING=true",
                "PAPER_RANDOM_SEED=123",
            ]
        ),
        encoding="utf-8",
    )

    s = load_settings(str(env))
    assert s.max_concurrent_trades == 7
    assert s.enable_arbitrage is False
    assert s.enable_guaranteed_win is False
    assert s.enable_multi_outcome_arb is False
    assert s.enable_stat_arb is True
    assert s.enable_sniping is True
    assert s.enable_market_making is True
    assert s.enable_value_betting is True
    assert s.enable_oracle_sniping_strategy is True
    assert s.enable_conditional_arb is True
    assert s.enable_liquidity_rewards is True
    assert s.enable_near_resolution is True
    assert s.enable_arb_stacking is True
    assert s.paper_random_seed == 123


def test_paper_blotter_seed_is_deterministic():
    def run_once(seed: int) -> bool:
        blotter = PaperBlotter(fill_probability=0.5, require_volume_cross=False, random_seed=seed)
        blotter.submit(
            token_id="t1",
            side="BUY",
            price=Decimal("0.40"),
            size=Decimal("1"),
            order_type="GTC",
        )
        fills = blotter.update_market(token_id="t1", best_bid=Decimal("0.39"), best_ask=Decimal("0.40"))
        return len(fills) == 1

    assert run_once(42) == run_once(42)


def test_orchestrator_active_position_tracks_stacks():
    settings = Settings(
        kill_switch=False,
        trading_mode="paper",
        max_order_usdc=Decimal("20"),
        min_order_usdc=Decimal("2"),
        initial_order_pct=Decimal("25"),
    )
    cfg = OrchestratorConfig(
        enable_arbitrage=False,
        enable_guaranteed_win=False,
        enable_multi_outcome_arb=False,
        enable_arb_stacking=True,
        max_arb_stacks=3,
    )
    orch = StrategyOrchestrator(settings, cfg)

    orch.mark_position_active("c1")
    orch.mark_position_active("c1")
    assert orch.active_positions.count("c1") == 2

    orch.mark_position_closed("c1")
    assert orch.active_positions.count("c1") == 1


def test_graduated_sizing_scales_by_stack_tier():
    settings = Settings(
        kill_switch=False,
        trading_mode="paper",
        max_order_usdc=Decimal("20"),
        min_order_usdc=Decimal("2"),
        initial_order_pct=Decimal("25"),
    )
    cfg = OrchestratorConfig(
        enable_arbitrage=False,
        enable_guaranteed_win=False,
        enable_multi_outcome_arb=False,
        enable_arb_stacking=True,
        max_arb_stacks=3,
    )
    orch = StrategyOrchestrator(settings, cfg)

    s = _mk_signal("stacked")

    # Tier 1 (0 active): 25% of $20 -> $5
    out1 = orch._apply_graduated_sizing([s])[0]
    assert out1.max_total_cost == Decimal("5.00")

    # Tier 2 (1 active): 62.5% of $20 -> $12.50
    orch.mark_position_active("stacked")
    out2 = orch._apply_graduated_sizing([s])[0]
    assert out2.max_total_cost == Decimal("12.50")

    # Tier 3 (2 active): 100% of $20 -> unchanged $20
    orch.mark_position_active("stacked")
    out3 = orch._apply_graduated_sizing([s])[0]
    assert out3.max_total_cost == Decimal("20")
