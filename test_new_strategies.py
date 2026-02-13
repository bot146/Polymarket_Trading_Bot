"""Quick import & config smoke test for the 4 new strategies."""
import logging
logging.disable(logging.CRITICAL)

import sys
ok = True

try:
    from polymarket_bot.strategies.conditional_arb_strategy import ConditionalArbStrategy
    print("[OK] conditional_arb_strategy")
except Exception as e:
    print(f"[FAIL] conditional_arb_strategy: {e}")
    ok = False

try:
    from polymarket_bot.strategies.liquidity_rewards_strategy import LiquidityRewardsStrategy
    print("[OK] liquidity_rewards_strategy")
except Exception as e:
    print(f"[FAIL] liquidity_rewards_strategy: {e}")
    ok = False

try:
    from polymarket_bot.strategies.near_resolution_strategy import NearResolutionStrategy
    print("[OK] near_resolution_strategy")
except Exception as e:
    print(f"[FAIL] near_resolution_strategy: {e}")
    ok = False

try:
    from polymarket_bot.orchestrator import OrchestratorConfig, StrategyOrchestrator
    print("[OK] orchestrator imports")
except Exception as e:
    print(f"[FAIL] orchestrator imports: {e}")
    ok = False

try:
    from polymarket_bot.config import load_settings
    s = load_settings()
    print(f"[OK] config: conditional_arb={s.enable_conditional_arb}, "
          f"liquidity_rewards={s.enable_liquidity_rewards}, "
          f"near_resolution={s.enable_near_resolution}, "
          f"arb_stacking={s.enable_arb_stacking}, "
          f"max_stacks={s.max_arb_stacks}")
except Exception as e:
    print(f"[FAIL] config: {e}")
    ok = False

try:
    from polymarket_bot.strategies import (
        ConditionalArbStrategy as C1,
        LiquidityRewardsStrategy as L1,
        NearResolutionStrategy as N1,
    )
    print("[OK] strategies __init__ exports")
except Exception as e:
    print(f"[FAIL] strategies __init__ exports: {e}")
    ok = False

# Quick functional test: ConditionalArbStrategy.scan with empty data
try:
    s1 = ConditionalArbStrategy()
    signals = s1.scan({"markets": []})
    assert signals == [], f"Expected empty signals, got {signals}"
    print("[OK] ConditionalArbStrategy.scan(empty)")
except Exception as e:
    print(f"[FAIL] ConditionalArbStrategy.scan(empty): {e}")
    ok = False

# Quick functional test: LiquidityRewardsStrategy.scan with empty data
try:
    s2 = LiquidityRewardsStrategy()
    signals = s2.scan({"markets": []})
    assert signals == [], f"Expected empty signals, got {signals}"
    print("[OK] LiquidityRewardsStrategy.scan(empty)")
except Exception as e:
    print(f"[FAIL] LiquidityRewardsStrategy.scan(empty): {e}")
    ok = False

# Quick functional test: NearResolutionStrategy.scan with empty data
try:
    s3 = NearResolutionStrategy()
    signals = s3.scan({"markets": []})
    assert signals == [], f"Expected empty signals, got {signals}"
    print("[OK] NearResolutionStrategy.scan(empty)")
except Exception as e:
    print(f"[FAIL] NearResolutionStrategy.scan(empty): {e}")
    ok = False

# Test stacking logic in filter_signals
try:
    from polymarket_bot.strategy import StrategyType
    cfg = OrchestratorConfig(
        enable_arb_stacking=True,
        max_arb_stacks=2,
        enable_multi_outcome_arb=False,
        enable_arbitrage=False,
        enable_guaranteed_win=False,
    )
    from polymarket_bot.config import Settings
    orch = StrategyOrchestrator(Settings(trading_mode="paper"), cfg)
    
    # Simulate having one active position
    orch.active_positions.append("group_123")
    
    from polymarket_bot.strategy import Opportunity, StrategySignal, Trade
    from decimal import Decimal
    sig = StrategySignal(
        opportunity=Opportunity(
            strategy_type=StrategyType.MULTI_OUTCOME_ARB,
            expected_profit=Decimal("0.10"),
            confidence=Decimal("0.95"),
            urgency=8,
            metadata={"condition_id": "group_123", "type": "multi_outcome_arb"},
        ),
        trades=[Trade(token_id="t1", side="BUY", size=Decimal("10"), price=Decimal("0.50"))],
        max_total_cost=Decimal("5"),
        min_expected_return=Decimal("10"),
    )
    
    # First stack should be allowed (count=1 < max=2)
    filtered = orch.filter_signals([sig])
    assert len(filtered) == 1, f"Stack 1 should be allowed, got {len(filtered)}"
    
    # Add second position — now at max
    orch.active_positions.append("group_123")
    filtered2 = orch.filter_signals([sig])
    assert len(filtered2) == 0, f"Stack 3 should be blocked, got {len(filtered2)}"
    
    print("[OK] Stacking filter_signals logic")
except Exception as e:
    print(f"[FAIL] Stacking filter_signals: {e}")
    import traceback; traceback.print_exc()
    ok = False

print()
if ok:
    print("=== ALL CHECKS PASSED ===")
else:
    print("=== SOME CHECKS FAILED ===")
    sys.exit(1)
