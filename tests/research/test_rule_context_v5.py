from copy import deepcopy
from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from agent.contracts.market_context import completed_context
from agent.contracts.rule import (
    RULE_SCHEMA_V4, RULE_SCHEMA_V5, RuleSpecError, causal_maturity_bars,
    evaluate_rule_signal, evaluate_rule_signal_trace, feature_window_bars,
    generate_rule_signal, rule_semantic_distance, rule_semantic_signature,
    rule_spec_json_schema, rule_variant_id, rule_vehicle_executable,
    validate_rule_spec,
)
from agent.engine_cycle import _rule_runtime_bars


def bars(prices):
    start = datetime(2026, 8, 3, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    return [{"timestamp": start + timedelta(minutes=i), "symbol": "SPY",
             "open": p - .01, "close": p, "high": p + .02,
             "low": p - .02, "volume": 1000.0} for i, p in enumerate(prices)]


def source(rows):
    return [{**row, "timestamp": row["timestamp"].timestamp()} for row in rows]


BASE = {"schema": RULE_SCHEMA_V5, "family": "momentum_continuation",
        "lookback": 3, "slow_lookback": 5, "atr_period": 3,
        "threshold_bps": 0.0}


class RuleContextTests(unittest.TestCase):
    def test_discovery_distinguishes_active_context_timeframe_and_interaction(self):
        from research.strategy_factory import _structurally_distinct, structure_signature
        prior = {**BASE, "family": "trend_pullback", "regime_mode": "trend"}
        slower = {**prior, "regime_timeframe_minutes": 15}
        self.assertIn("regime_timeframe_minutes", structure_signature(slower, prior)["active_axes"])
        self.assertTrue(_structurally_distinct(slower, [prior]))
        self.assertTrue(_structurally_distinct({**prior, "regime_lookback_bars": 8,
                       "regime_efficiency_threshold": .6}, [prior]))
        self.assertFalse(_structurally_distinct({**BASE, "regime_timeframe_minutes": 15,
                        "regime_lookback_bars": 8}, [BASE]))

    def test_neutral_upgrade_preserves_behavior_and_semantic_identity(self):
        old = {**BASE, "schema": RULE_SCHEMA_V4}
        rows = bars([100 + i * .1 for i in range(40)])
        a, b = evaluate_rule_signal(rows, old), evaluate_rule_signal(rows, BASE)
        for field in ("direction", "stop_price", "target_price", "signal_ts"):
            self.assertEqual(a[field], b[field])
        self.assertEqual(b["rule_schema"], RULE_SCHEMA_V5)
        self.assertEqual(rule_semantic_signature(old), rule_semantic_signature(BASE))
        self.assertEqual(rule_semantic_distance(old, BASE), 0)
        self.assertNotEqual(rule_variant_id(old), rule_variant_id(BASE))
        self.assertNotIn("regime_mode", validate_rule_spec(old))

    def test_completed_buckets_ignore_unfinished_minutes_and_old_sessions(self):
        rows = source(bars([100 + i * .1 for i in range(19)]))
        context, reason = completed_context(rows, minutes=5, lookback=3)
        self.assertEqual(reason, "passed")
        self.assertEqual(len(context["bars"]), 3)
        self.assertEqual(context["bars"][-1]["close"], rows[14]["close"])
        changed = deepcopy(rows)
        changed[-1]["close"] = 10000
        again, _ = completed_context(changed, minutes=5, lookback=3)
        self.assertEqual(context["snapshot_id"], again["snapshot_id"])
        changed[8]["close"] += .1
        revised, _ = completed_context(changed, minutes=5, lookback=3)
        self.assertNotEqual(context["snapshot_id"], revised["snapshot_id"])
        prior = [{**row, "timestamp": row["timestamp"] - 86400} for row in rows]
        with_prior, _ = completed_context(prior + rows, minutes=5, lookback=3)
        self.assertEqual(context["snapshot_id"], with_prior["snapshot_id"])

    def test_gaps_duplicates_and_incomplete_context_refuse(self):
        rows = source(bars([100 + i * .1 for i in range(49)]))
        self.assertIsNotNone(completed_context(rows, minutes=15, lookback=3)[0])
        for invalid in (rows[:8] + rows[9:], rows[:8] + [rows[8]] + rows[8:], rows[:44]):
            self.assertIsNone(completed_context(invalid, minutes=15, lookback=3)[0])

    def test_runtime_and_evaluator_share_snapshot_and_regime_veto(self):
        spec = {**BASE, "regime_mode": "trend", "regime_lookback_bars": 3}
        rows = bars([100 + i * .1 for i in range(19)])
        now = rows[-1]["timestamp"] + timedelta(seconds=65)
        prepared = _rule_runtime_bars(rows, spec, now, max_age_seconds=30)
        self.assertIsNotNone(prepared)
        direct = evaluate_rule_signal(rows, spec)
        runtime = generate_rule_signal("SPY", prepared[0], config={"strategy": {
            "rule_spec": spec, "execution_mode": "shares"}}, now=now)
        self.assertEqual(direct["intraday_context"], runtime["intraday_context"])
        self.assertEqual(direct["intraday_context"]["efficiency"], 1)
        trace = evaluate_rule_signal_trace(rows, {**spec, "regime_mode": "range"})
        self.assertIsNone(trace["signal"])
        self.assertEqual(trace["stages"][-1]["reason"], "regime_predicate_not_met")
        self.assertEqual(causal_maturity_bars(spec), feature_window_bars(spec))

    def test_pullback_requires_prior_impulse_retracement_and_close_reclaim(self):
        spec = {**BASE, "family": "trend_pullback", "entry_trigger": "reclaim",
                "pullback_bars": 2}
        valid = bars([100, 100.2, 100.4, 100.6, 100.8, 100.7, 100.6, 100.75])
        self.assertEqual(evaluate_rule_signal(valid, spec)["direction"], "long")
        unconfirmed = deepcopy(valid)
        unconfirmed[-1].update(close=100.61, open=100.60)
        self.assertIsNone(evaluate_rule_signal(unconfirmed, spec))
        no_retracement = bars([100, 100.2, 100.4, 100.6, 100.8, 100.9, 101, 101.1])
        self.assertIsNone(evaluate_rule_signal(no_retracement, spec))
        mirrored = [{**r, **{key: 201 - r[key] for key in ("open", "close")},
                     "high": 201-r["low"], "low": 201-r["high"]} for r in valid]
        self.assertEqual(evaluate_rule_signal(mirrored, spec)["direction"], "short")

    def test_context_fields_are_bounded_versioned_and_equity_only(self):
        for change in ({"regime_timeframe_minutes": 10}, {"regime_lookback_bars": True},
                       {"regime_efficiency_threshold": float("nan")},
                       {"entry_trigger": "reclaim"}, {"pullback_bars": 11}):
            with self.subTest(change=change), self.assertRaises(RuleSpecError):
                validate_rule_spec({**BASE, **change})
        with self.assertRaises(RuleSpecError):
            validate_rule_spec({**BASE, "schema": RULE_SCHEMA_V4, "regime_mode": "trend"})
        self.assertFalse(rule_vehicle_executable(BASE, "option"))
        for branch in rule_spec_json_schema(RULE_SCHEMA_V5)["oneOf"]:
            self.assertEqual(set(branch["required"]), set(branch["properties"]))

    def test_value_reclaim_needs_confirmation_and_room_to_frozen_anchor(self):
        for family in ("vwap_reversion", "mean_reversion"):
            spec = {**BASE, "family": family, "entry_trigger": "reclaim",
                    "pullback_bars": 2, "threshold_bps": 5, "zscore": 1}
            valid = bars([100, 100.1, 99.9, 100.1, 100, 99.5, 99.4, 99.6])
            with self.subTest(family=family):
                signal = evaluate_rule_signal(valid, spec)
                self.assertIsNotNone(signal)
                self.assertEqual(signal["direction"], "long")
                for close in (99.41, 100.2):
                    changed = deepcopy(valid)
                    changed[-1].update(open=close-.01, high=close+.02,
                                       low=close-.02, close=close)
                    self.assertIsNone(evaluate_rule_signal(changed, spec))


if __name__ == "__main__":
    unittest.main()
