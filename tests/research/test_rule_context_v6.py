"""Causal mechanism predicates for the diagnostic v6 rule grammar."""

from copy import deepcopy
from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from agent.contracts.rule import (
    RULE_SCHEMA_V5, RULE_SCHEMA_V6, RuleSpecError, causal_maturity_bars,
    evaluate_rule_signal, feature_window_bars, rule_semantic_distance,
    rule_semantic_signature, rule_spec_json_schema, rule_variant_id,
    rule_vehicle_executable, validate_rule_spec,
)


NY = ZoneInfo("America/New_York")


def bars(prices, *, wick=.02):
    start = datetime(2026, 8, 3, 9, 30, tzinfo=NY)
    rows = []
    for index, price in enumerate(prices):
        opened = prices[index - 1] if index else price - .01
        rows.append({
            "timestamp": start + timedelta(minutes=index), "symbol": "SPY",
            "open": opened, "high": max(opened, price) + wick,
            "low": min(opened, price) - wick, "close": price,
            "volume": 1_000.0,
        })
    return rows


BASE = {
    "schema": RULE_SCHEMA_V6, "family": "momentum_continuation",
    "lookback": 3, "slow_lookback": 5, "atr_period": 3,
    "threshold_bps": 0.0,
}


class V6GrammarTests(unittest.TestCase):
    def test_neutral_v6_aliases_v5_semantics_but_has_a_new_content_id(self):
        v5 = validate_rule_spec({**BASE, "schema": RULE_SCHEMA_V5})
        v6 = validate_rule_spec(BASE)
        self.assertEqual(rule_semantic_signature(v5), rule_semantic_signature(v6))
        self.assertEqual(rule_semantic_distance(v5, v6), 0.0)
        self.assertNotEqual(rule_variant_id(v5), rule_variant_id(v6))
        self.assertFalse(rule_vehicle_executable(v6, "equity"))

    def test_v5_rejects_every_v6_field_and_non_applicable_fields_are_strict(self):
        fields = (
            {"compression_mode": "atr", "compression_atr_multiple": 3.0},
            {"opening_hold_bars": 2},
            {"body_fraction_min": .5},
        )
        for change in fields:
            with self.subTest(change=change), self.assertRaises(RuleSpecError):
                validate_rule_spec({**BASE, "schema": RULE_SCHEMA_V5, **change})
        with self.assertRaises(RuleSpecError):
            validate_rule_spec({**BASE, "family": "mean_reversion",
                                "body_fraction_min": .5})
        with self.assertRaises(RuleSpecError):
            validate_rule_spec({**BASE, "family": "volatility_breakout",
                                "compression_mode": "atr"})

    def test_json_schema_exposes_v6_fields_and_strict_required_properties(self):
        schema = rule_spec_json_schema(RULE_SCHEMA_V6)
        for branch in schema["oneOf"]:
            self.assertEqual(set(branch["required"]), set(branch["properties"]))
            for name in ("compression_mode", "compression_atr_multiple",
                         "opening_hold_bars", "body_fraction_min",
                         "close_location_min"):
                self.assertIn(name, branch["properties"])

    def test_prior_bar_atr_does_not_look_into_the_signal_bar(self):
        spec = validate_rule_spec(BASE)
        rows = bars([100 + i * .2 for i in range(20)])
        changed = deepcopy(rows)
        changed[-1]["high"] = 150.0
        changed[-1]["low"] = 50.0
        first = evaluate_rule_signal(rows, spec)
        second = evaluate_rule_signal(changed, spec)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first["stop_distance"], second["stop_distance"])
        self.assertEqual(first["stop_price"], second["stop_price"])
        self.assertGreaterEqual(causal_maturity_bars(spec), spec["atr_period"] + 2)
        self.assertEqual(causal_maturity_bars(spec), feature_window_bars(spec))

    def test_opening_hold_requires_consecutive_post_window_closes(self):
        spec = validate_rule_spec({
            "schema": RULE_SCHEMA_V6, "family": "opening_range_breakout",
            "range_minutes": 3, "threshold_bps": 0.0,
            "lookback": 3, "slow_lookback": 5, "atr_period": 3,
            "opening_hold_bars": 2,
        })
        rows = bars([100.0, 100.0, 100.0, 100.5, 100.0, 100.6, 100.7])
        self.assertIsNone(evaluate_rule_signal(rows[:5], spec))
        self.assertIsNotNone(evaluate_rule_signal(rows, spec))

    def test_range_expansion_body_and_close_location_are_directional(self):
        common = {
            "schema": RULE_SCHEMA_V6, "family": "range_expansion",
            "lookback": 3, "slow_lookback": 5, "atr_period": 3,
            "threshold_bps": 0.0, "volume_multiplier": 1.25,
        }
        rows = bars([100.0, 100.0, 100.0, 100.0, 100.0, 100.5], wick=.02)
        # Make the expansion bar wide and close at the upper 75% boundary,
        # while its body occupies only one third of the total range.
        rows[-1].update(open=100.0, high=101.5, low=99.0, close=100.5)
        neutral = validate_rule_spec(common)
        strict = validate_rule_spec({**common, "body_fraction_min": .5,
                                     "close_location_min": .75})
        self.assertIsNotNone(evaluate_rule_signal(rows, neutral))
        self.assertIsNone(evaluate_rule_signal(rows, strict))

    def test_rolling_vwap_slope_uses_two_equal_completed_windows(self):
        spec = validate_rule_spec({
            "schema": RULE_SCHEMA_V6, "family": "vwap_trend",
            "lookback": 3, "slow_lookback": 5, "atr_period": 3,
            "threshold_bps": 0.0, "vwap_slope_mode": "rolling",
            "vwap_slope_window": 5,
        })
        rows = bars([100.0 + index * .5 for index in range(11)])
        signal = evaluate_rule_signal(rows, spec)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["vwap_slope_mode"], "rolling")
        self.assertEqual(signal["vwap_slope_window"], 5)
        self.assertGreaterEqual(causal_maturity_bars(spec), 10)
        self.assertEqual(feature_window_bars(spec), causal_maturity_bars(spec))

        cumulative = validate_rule_spec({
            **spec, "vwap_slope_mode": "cumulative",
        })
        self.assertIsNone(feature_window_bars(cumulative))
        session_target = validate_rule_spec({
            **spec, "target_mode": "session_vwap",
        })
        self.assertIsNone(feature_window_bars(session_target))

        # A future context row cannot alter the completed prefix used by the
        # signal.  Keep the same current prefix while changing only a row
        # after it, which catches accidental full-session lookahead.
        future = deepcopy(rows[-1])
        future["timestamp"] = future["timestamp"] + timedelta(minutes=1)
        future.update(open=1.0, high=500.0, low=1.0, close=2.0, volume=1_000_000)
        self.assertEqual(signal, evaluate_rule_signal(
            rows, spec, bars_by_symbol={"SPY": [*rows, future]}, symbol="SPY"))

    def test_same_clock_rvol_requires_five_prior_feed_sessions_and_is_causal(self):
        spec = validate_rule_spec({
            "schema": RULE_SCHEMA_V6, "family": "volume_breakout",
            "lookback": 3, "slow_lookback": 5, "atr_period": 3,
            "threshold_bps": 0.0, "volume_multiplier": 1.25,
            "relative_volume_mode": "same_clock",
            "relative_volume_sessions": 5,
        })
        current_day = datetime(2026, 8, 10, 9, 30, tzinfo=NY)
        current = []
        for index in range(31):
            stamp = current_day + timedelta(minutes=index)
            close = 100.0 + index * .01
            current.append({
                "timestamp": stamp, "symbol": "SPY", "feed": "iex",
                "provider": "test", "open": close - .01,
                "high": close + .02, "low": close - .02,
                "close": close, "volume": 100.0,
            })
        # The signal bar at 10:00 breaks the preceding three-bar high and
        # carries twice the five-session same-clock baseline volume.
        current[-1].update(open=101.0, high=102.0, low=100.9,
                           close=101.5, volume=200.0)
        prior = []
        for day_offset in range(1, 7):
            stamp = current_day - timedelta(days=day_offset)
            prior.append({
                "timestamp": stamp + timedelta(minutes=30), "symbol": "SPY",
                "feed": "iex", "provider": "test", "open": 100.0,
                "high": 100.1, "low": 99.9, "close": 100.0,
                "volume": 100.0,
            })
        context = {"SPY": [*prior, *current]}
        signal = evaluate_rule_signal(
            current, spec, bars_by_symbol=context, symbol="SPY")
        self.assertIsNotNone(signal)
        self.assertEqual(signal["relative_volume_mode"], "same_clock")
        self.assertEqual(signal["relative_volume_sessions"], 5)
        self.assertAlmostEqual(signal["relative_volume"], 2.0)

        # The sixth, newer prior session is part of the selected five.  Its
        # missing sample fails closed instead of shrinking the baseline.
        missing_rows = [row for row in context["SPY"] if row is not prior[0]]
        # Retain another completed bar from that session so the context still
        # proves the session exists; only the exact same-clock sample is gone.
        missing_rows.append({**prior[0],
                             "timestamp": prior[0]["timestamp"] - timedelta(minutes=1)})
        missing = {"SPY": missing_rows}
        self.assertIsNone(evaluate_rule_signal(
            current, spec, bars_by_symbol=missing, symbol="SPY"))

        # A future session is excluded by local session date and cannot change
        # the historical baseline or the emitted signal.
        future = dict(prior[0])
        future["timestamp"] = current_day + timedelta(days=1, minutes=30)
        future["volume"] = 9_999_999.0
        with_future = {"SPY": [*context["SPY"], future]}
        self.assertEqual(signal, evaluate_rule_signal(
            current, spec, bars_by_symbol=with_future, symbol="SPY"))

        # Same-clock means same feed; one mismatched historical feed removes
        # that session from the eligible set and therefore fails closed.
        wrong_feed = [dict(row) for row in prior]
        wrong_feed[0]["feed"] = "sip"
        wrong_feed[1]["feed"] = "sip"
        mismatch = {"SPY": [*wrong_feed, *current]}
        self.assertIsNone(evaluate_rule_signal(
            current, spec, bars_by_symbol=mismatch, symbol="SPY"))


if __name__ == "__main__":
    unittest.main()
