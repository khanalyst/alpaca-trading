"""Focused regressions for the immutable candidate/run assumptions identity."""

import unittest

from agent.config import validate_config
from agent.contracts.rule import validate_rule_spec, rule_variant_id
from agent.edge import _runtime_identity_matches
from research.costs import CostModel
from research.edge_discovery_core import _effective_ibr_config
from research.edge_identity import candidate_assumptions
from research.edge_ledger_store import content_hash


class CandidateIdentityTests(unittest.TestCase):
    def setUp(self):
        self.config = validate_config({
            "strategy": {"id": "ibr", "variant_id": "ibr.baseline",
                         "execution_mode": "shares"},
        })

    def test_ibr_backtest_and_shadow_replay_metadata_share_one_hash(self):
        _, effective = _effective_ibr_config(self.config, {})
        backtest = candidate_assumptions(
            {**effective, "replay_mode": "backtest",
             "replay_policy": {"quotes": "bar-fallback"}},
            vehicle="equity", strategy_id="ibr", variant_id="ibr.baseline")
        shadow = candidate_assumptions(
            {**effective, "replay_mode": "shadow",
             "replay_policy": {"quotes": "strict"}},
            vehicle="equity", strategy_id="ibr", variant_id="ibr.baseline")
        self.assertEqual(content_hash(backtest), content_hash(shadow))
        self.assertEqual(backtest["strategy"]["variant_id"], "ibr.baseline")
        self.assertEqual(backtest["broker"]["data_feed"], "sip")

    def test_factory_rule_identity_carries_explicit_model_and_is_lane_stable(self):
        model = CostModel(spread_bps=4, slippage_bps=6, fee_bps=.5,
                          provenance="test-calibration")
        spec = validate_rule_spec({"family": "opening_range_breakout"})
        variant_id = rule_variant_id(spec)
        source = {**self.config, "costs": model.as_dict()}
        candidate = candidate_assumptions(
            source, vehicle="equity", strategy_id="rule",
            variant_id=variant_id, rule_spec=spec)
        run = candidate_assumptions(
            {**source, "replay_mode": "shadow",
             "replay_policy": {"quotes": "strict"}},
            vehicle="equity", strategy_id="rule",
            variant_id=variant_id, rule_spec=spec)
        self.assertEqual(content_hash(candidate), content_hash(run))
        self.assertEqual(candidate["costs"]["provenance"], "test-calibration")
        self.assertIn("session", candidate)
        self.assertIn("risk", candidate)
        self.assertIn("execution", candidate)

    def test_explicit_vehicle_schedule_changes_candidate_identity(self):
        flat = validate_config({})
        scheduled = validate_config({"costs": {
            "vehicles": {"option": {"spread_bps": 9.0}},
        }})
        legacy = candidate_assumptions(
            flat, vehicle="option", strategy_id="ibr", variant_id="ibr.baseline")
        candidate = candidate_assumptions(
            scheduled, vehicle="option", strategy_id="ibr",
            variant_id="ibr.baseline")
        self.assertNotEqual(content_hash(legacy), content_hash(candidate))
        self.assertNotIn("vehicles", legacy["costs"])
        self.assertEqual(candidate["costs"]["vehicles"]["option"]["spread_bps"], 9.0)

    def test_runtime_llm_setting_is_hashed_and_mismatch_fails_closed(self):
        disabled = validate_config({
            "strategy": {"id": "ibr", "variant_id": "ibr.baseline",
                         "execution_mode": "shares"},
            "llm": {"enabled": False},
        })
        enabled = validate_config({
            "strategy": {"id": "ibr", "variant_id": "ibr.baseline",
                         "execution_mode": "shares"},
            "llm": {"enabled": True, "model": "paper-test"},
        })
        off = candidate_assumptions(
            disabled, vehicle="equity", strategy_id="ibr",
            variant_id="ibr.baseline")
        on = candidate_assumptions(
            enabled, vehicle="equity", strategy_id="ibr",
            variant_id="ibr.baseline")
        self.assertNotEqual(content_hash(off), content_hash(on))
        record = {"status": "validated", "strategy_id": "ibr",
                  "vehicle": "equity", "variant_id": "ibr.baseline",
                  "config": off, "config_hash": content_hash(off)}
        self.assertFalse(_runtime_identity_matches(record, enabled))


if __name__ == "__main__":
    unittest.main()
