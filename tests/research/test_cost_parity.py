"""B2: the replay cost model and the live risk engine must agree exactly.

This is the test that makes replayed R and demo R the same unit. Sizing
reserves entry slippage, stop slippage, spread and two sides of fee inside
``estimated_loss_pct``, and ``risk_usd`` is derived from it - so that figure,
not the bare stop distance, is the denominator the live agent's R was
measured against.

If the replay divided by the stop distance instead, every replayed variant
would look roughly a third better than the live record for no reason other
than arithmetic, and the comparison the whole programme rests on would be
between two different units wearing the same name.

Rather than restating the formula, these tests run the real ``RiskEngine``
over a shared fixture and require the two numbers to match.
"""

import unittest

from agent.risk import RiskEngine
from research.outcomes import CostModel, SetupPlan
from tests.helpers import valid_config


SIGNAL_TS = 1_760_000_000_000


def snapshot(**overrides) -> dict:
    row = {
        "price": 100.0,
        "spread_pct": 0.02,
        "funding_rate_pct": 0.0,
        "funding_interval_hours": 8,
        "next_funding_minutes": 240,
        "taker_fee_pct_per_side": 0.05,
        "atr_1h_pct": 0.8,
        "trend_15m": "up", "trend_1h": "up", "trend_4h": "up",
        "swing_low_pct": 2.0, "swing_high_pct": 2.0,
        "signal_ts": SIGNAL_TS,
    }
    row.update(overrides)
    return {"BTC/USDT:USDT": row}


def plan_from(row: dict, direction: str = "long",
              stop_pct: float = 2.0) -> SetupPlan:
    return SetupPlan(
        symbol="BTC/USDT:USDT", direction=direction, entry_price=100.0,
        stop_pct=stop_pct, take_pct=stop_pct * 2, signal_ts=SIGNAL_TS,
        spread_pct=row["spread_pct"],
        funding_rate_pct=row["funding_rate_pct"],
        funding_interval_hours=row["funding_interval_hours"] or 0.0,
        taker_fee_pct_per_side=row["taker_fee_pct_per_side"])


def live_estimated_loss_pct(cfg: dict, snap: dict, direction: str,
                            stop_pct: float) -> float:
    """Run the real engine and read back the figure it sized from."""
    engine = RiskEngine(cfg)
    decision = {
        "symbol": "BTC/USDT:USDT", "action": "open", "direction": direction,
        "setup_type": "trend_continuation", "confidence": 0.9,
        "stop_loss_pct": stop_pct, "take_profit_pct": stop_pct * 2,
        "invalidation_anchor": "structure", "exit_policy": "fixed_rr",
    }
    plan, why = engine.vet_open(
        decision, equity=10_000.0, positions=[], snapshot=snap,
        cooldowns={}, gross_notional=0.0)
    if plan is None:
        raise AssertionError(f"fixture was vetoed by the risk engine: {why}")
    return float(plan["estimated_loss_pct"])


def replay_risk_pct(cfg: dict, snap: dict, direction: str,
                    stop_pct: float) -> float:
    row = snap["BTC/USDT:USDT"]
    costs = CostModel(
        expected_stop_slippage_pct=float(
            cfg["trading_costs"]["expected_stop_slippage_pct"]),
        max_order_book_slippage_pct=float(
            cfg["execution"]["max_order_book_slippage_pct"]))
    plan = plan_from(row, direction, stop_pct)

    # Mirror the engine's own funding-interval derivation: the count it
    # reserves depends on the schedule and the expected hold, not on how long
    # the trade actually lasted.
    import math
    hold_hours = float(cfg["trading_costs"]["expected_hold_hours"])
    interval = float(row["funding_interval_hours"] or 0)
    intervals = float(cfg["trading_costs"]["expected_funding_intervals_held"])
    if interval > 0:
        next_minutes = row.get("next_funding_minutes")
        if next_minutes in (None, ""):
            dynamic = math.ceil(hold_hours / interval)
        else:
            next_hours = max(0.0, float(next_minutes) / 60)
            dynamic = (0 if next_hours > hold_hours else
                       1 + math.floor(
                           max(0.0, hold_hours - next_hours) / interval))
        intervals = max(intervals, dynamic)
    return costs.risk_pct(plan, intervals)


class CostParityTests(unittest.TestCase):
    """Same fixture, both tiers, same number."""

    def _assert_parity(self, snap: dict, direction: str = "long",
                       stop_pct: float = 2.0):
        cfg = valid_config()
        live = live_estimated_loss_pct(cfg, snap, direction, stop_pct)
        replay = replay_risk_pct(cfg, snap, direction, stop_pct)
        self.assertAlmostEqual(
            replay, live, 9,
            f"replay risk {replay} != live estimated_loss_pct {live}; "
            "the two tiers are not comparable")

    def test_zero_funding_long(self):
        self._assert_parity(snapshot())

    def test_zero_funding_short(self):
        self._assert_parity(snapshot(), direction="short")

    def test_positive_funding_costs_the_long(self):
        self._assert_parity(snapshot(funding_rate_pct=0.01))

    def test_positive_funding_is_free_for_the_short(self):
        self._assert_parity(snapshot(funding_rate_pct=0.01),
                            direction="short")

    def test_negative_funding_costs_the_short(self):
        self._assert_parity(snapshot(funding_rate_pct=-0.012),
                            direction="short")

    def test_a_wider_spread_moves_both_the_same_way(self):
        self._assert_parity(snapshot(spread_pct=0.25))

    def test_a_wider_stop_moves_both_the_same_way(self):
        self._assert_parity(snapshot(), stop_pct=3.5)

    def test_an_unknown_funding_schedule_matches(self):
        self._assert_parity(snapshot(next_funding_minutes=None))

    def test_a_distant_settlement_matches(self):
        self._assert_parity(snapshot(next_funding_minutes=2000))

    def test_a_non_default_account_fee_matches(self):
        self._assert_parity(snapshot(taker_fee_pct_per_side=0.02))


class DenominatorTests(unittest.TestCase):
    def test_risk_pct_is_strictly_greater_than_the_bare_stop(self):
        cfg = valid_config()
        stop = 2.0
        self.assertGreater(
            replay_risk_pct(cfg, snapshot(), "long", stop), stop,
            "costs must be inside the denominator, as they are live")

    def test_the_gap_is_the_cost_ratio_findings_warns_about(self):
        """~30% on a typical 2% stop, per findings.md section 9.1."""
        cfg = valid_config()
        risk = replay_risk_pct(cfg, snapshot(), "long", 2.0)
        self.assertGreater(risk / 2.0, 1.2)
        self.assertLess(risk / 2.0, 1.45)


if __name__ == "__main__":
    unittest.main()
