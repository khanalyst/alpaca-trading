"""Cross-lane regression for the runtime stressed-cost boundary.

The runtime owns the validated policy, while factory and null-control replay
must consume that policy rather than carrying a second cost veto.  These tests
keep the integration narrow: a real factory replay row is converted to the
same sized plan the runtime would submit, and a randomized-entry null is
checked at the same boundary.  They do not exercise an EdgeLedger transition.
"""

from dataclasses import replace
from datetime import datetime, timezone
import unittest

from agent.config import validate_config
from agent.risk import RiskEngine
from agent.contracts.rule import validate_rule_spec
from research.costs import (ReplayPolicy, STRESSED_COST_BASIS,
                            STRESSED_COST_SCHEMA)
from research.factory_core import simulate_account
from research.strategy_factory import null_control_account

from tests.research.test_costs import FLAT, RISING, SPEC, _bars, _quote


class RuntimeStressedCostParityTests(unittest.TestCase):
    """The candidate and null lanes share the runtime's stressed-cost veto."""

    @classmethod
    def setUpClass(cls):
        cls.config = validate_config({})
        cls.policy = ReplayPolicy.from_config(cls.config)
        cls.risk = RiskEngine(cls.config)

        # The shipped policy is calendar-authoritative.  Add exact session
        # metadata to the existing compact replay fixture rather than relaxing
        # the policy or inventing another bar generator here.
        session_open = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
        session_close = datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc)
        cls.bars = [replace(bar, session_open=session_open,
                            session_close=session_close)
                    for bar in _bars(RISING + FLAT)]
        # Keep the executable quote on the boundary bar instead of pinning it
        # below the already-authored long stop.  A through-stop quote is a
        # legitimate runtime rejection and is covered by the gap tests; this
        # fixture is specifically about stressed-cost geometry parity.
        cls.quotes = [_quote(index, float(bar.open), float(bar.open))
                      for index, bar in enumerate(cls.bars)]

        # stop_atr=3 is naturally above the effective floor.  stop_atr=1
        # authors the grammar's ~30bp minimum and exercises the policy veto.
        cls.accepted_spec = validate_rule_spec({**SPEC, "stop_atr": 3.0})
        cls.floor_spec = validate_rule_spec({**SPEC, "stop_atr": 1.0})
        cls.no_stress_policy = replace(
            cls.policy, stressed_cost_scenario_bps=None,
            max_stressed_cost_to_risk_ratio=None)

    @staticmethod
    def _plan(row):
        """Project a replay row into the runtime's sized plan shape."""
        return {
            "execution_profile": "shares",
            "shares": row["quantity"],
            "notional": row["entry_notional"],
            "risk_usd": row.get("nominal_risk_usd", row.get("risk_usd")),
        }

    def _factory(self, spec, account_id, *, policy=None):
        return simulate_account(
            self.bars, [], spec, vehicle="equity", account_id=account_id,
            quotes=self.quotes, policy=self.policy if policy is None else policy)

    def test_factory_candidate_matches_runtime_decision_and_telemetry(self):
        book = self._factory(self.accepted_spec, "runtime-parity-candidate")
        self.assertEqual(book["trades"], 1)
        row = book["rows"][0]
        self.assertFalse(row["no_trade"])

        plan = self._plan(row)
        runtime, runtime_reason = self.risk.check_stressed_cost(
            plan, cfg=self.config)
        self.assertIsNotNone(runtime)
        self.assertIsNone(runtime_reason)
        self.assertLessEqual(
            row["stressed_cost_to_risk_ratio"],
            self.policy.max_stressed_cost_to_risk_ratio)

        # The factory row carries the canonical runtime telemetry, not a local
        # description or an independently rounded stress calculation.
        for key in (
                "stressed_cost_vehicle", "stressed_cost_schema",
                "stressed_cost_basis", "stressed_cost_entry_notional",
                "stressed_cost_scenario_bps", "stressed_cost_usd",
                "stressed_cost_to_risk_ratio",
                "max_stressed_cost_to_risk_ratio"):
            with self.subTest(key=key):
                if isinstance(runtime[key], float):
                    self.assertAlmostEqual(row[key], runtime[key], places=12)
                else:
                    self.assertEqual(row[key], runtime[key])
        self.assertEqual(row["stressed_cost_schema"], STRESSED_COST_SCHEMA)
        self.assertEqual(row["stressed_cost_basis"], STRESSED_COST_BASIS)

    def test_thirty_bp_floor_is_vetoed_by_factory_null_and_runtime(self):
        # First replay without stress to retain the authored geometry.  Every
        # authorizing lane must reject it at max(grammar, scenario/ratio)
        # before sizing, rather than changing the authored candidate.
        reference = self._factory(
            self.floor_spec, "runtime-parity-floor-reference",
            policy=self.no_stress_policy)
        candidate = self._factory(self.floor_spec, "runtime-parity-floor")
        candidate_row = candidate["rows"][0]
        self.assertEqual(candidate["trades"], 0)
        self.assertTrue(candidate_row["no_trade"])
        self.assertEqual(candidate_row["reject_reason"],
                         "stressed_cost_risk_limit")
        self.assertTrue(candidate_row["stress_floor_binding"])
        self.assertAlmostEqual(candidate_row["authored_stop_distance"], .3024)
        self.assertAlmostEqual(candidate_row["effective_stop_floor_bps"],
                               25.0 / .30)
        self.assertEqual(candidate_row["reject_stage"], "risk_geometry")

        reference_row = reference["rows"][0]
        runtime, runtime_reason = self.risk.vet_open(
            {"symbol": "SPY", "direction": reference_row["direction"],
             "execution_profile": "shares",
             "entry_price": reference_row["plan_entry"],
             "stop_price": reference_row["stop_price"],
             "target_price": reference_row["target_price"],
             "target_r": self.floor_spec["target_r"]},
            100_000, [],
            {"SPY": {"price": reference_row["plan_entry"]}}, {}, 0, now=0)
        self.assertIsNone(runtime)
        self.assertEqual(runtime_reason, "stressed_cost_risk_limit")

        # The compact fixture has no quote at the final force-flat instant, so
        # keep this null-control check focused on geometry with bar fallback.
        null = null_control_account(
            self.bars, [], self.floor_spec, vehicle="equity",
            reference_rows=reference["rows"], account_id="runtime-parity-floor-null",
            quotes=self.quotes,
            policy=replace(self.policy, strict_market_data=False))
        self.assertEqual(null["trades"], 0)
        null_row = null["rows"][0]
        self.assertTrue(null_row["no_trade"])
        self.assertEqual(null_row["reject_reason"],
                         "stressed_cost_risk_limit")
        self.assertTrue(null_row["stress_floor_binding"])
        # The null receives the candidate reference row after broker-tick
        # normalization; it must preserve that executable 31-cent geometry
        # before applying the stress veto.
        self.assertAlmostEqual(null_row["authored_stop_distance"], .31)
        self.assertAlmostEqual(null_row["effective_stop_floor_bps"],
                               candidate_row["effective_stop_floor_bps"])


if __name__ == "__main__":
    unittest.main()
