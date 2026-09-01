"""The v2 rule grammar: wider hypotheses, identical v1 identities.

v2 exists so autonomous discovery can express a *conditional* edge — this
family, only in this part of the session, only in this volatility regime, with
these confirmations — instead of only retuned numbers.  Two properties make
that safe, and both are pinned here:

1. A v1 spec keeps its exact content hash, so every candidate already in a
   ledger still resolves to the same ``variant_id`` and stays deployable.
2. Every v2 extension is an entry-side predicate over the same completed-bar
   prefix, so research and runtime remain one evaluator and no extension can
   reach sizing, exits, or order placement.
"""

from datetime import datetime, timedelta
import random
import unittest
from zoneinfo import ZoneInfo

from agent.contracts.rule import (
    DEFAULT_RULE_SPEC, MAX_CONFIRMATIONS, RULE_FAMILIES, RULE_SCHEMA_V1,
    RULE_SCHEMA_V2, RULE_SCHEMA_V3, RULE_SCHEMA_V4, SIDES, CONFIRMATIONS, RuleSpecError,
    evaluate_rule_signal, rule_spec_hash, rule_spec_json_schema,
    rule_variant_id, setup_evidence,
    validate_rule_spec,
)

NEW_YORK = ZoneInfo("America/New_York")

# The seven shipped templates, spelled out rather than imported, so a change to
# the catalog cannot silently change what "the v1 identities" means here.
SHIPPED_V1_SPECS = (
    {"family": "opening_range_breakout", "range_minutes": 15,
     "threshold_bps": 5.0, "confirmation": "volume"},
    {"family": "opening_range_fade", "range_minutes": 20,
     "threshold_bps": 8.0, "target_r": 1.5, "confirmation": "none"},
    {"family": "momentum_continuation", "lookback": 12,
     "threshold_bps": 18.0, "confirmation": "volume"},
    {"family": "mean_reversion", "lookback": 20,
     "zscore": 1.5, "target_r": 1.5, "confirmation": "volatility"},
    {"family": "trend_pullback", "lookback": 10,
     "slow_lookback": 35, "threshold_bps": 15.0, "confirmation": "trend"},
    {"family": "volatility_breakout", "lookback": 12,
     "compression_bps": 55.0, "threshold_bps": 5.0, "confirmation": "volume"},
    {"family": "volume_breakout", "lookback": 15,
     "volume_multiplier": 1.5, "threshold_bps": 5.0, "confirmation": "trend"},
)

# Captured from the pre-v2 implementation.  If a v2 change ever perturbs the v1
# canonical form, these stop matching and the ledger-compatibility guarantee
# fails loudly instead of orphaning deployed edges.
FROZEN_V1_VARIANT_IDS = {
    "opening_range_breakout": "rule.opening-range-breakout.64608f6cec110cc5",
    "opening_range_fade": "rule.opening-range-fade.8f2aec682f9cd1a1",
    "momentum_continuation": "rule.momentum-continuation.ccff5e35c2474684",
    "mean_reversion": "rule.mean-reversion.3f42bf47eda6ff36",
    "trend_pullback": "rule.trend-pullback.23953367754d637c",
    "volatility_breakout": "rule.volatility-breakout.624a0c225124b81e",
    "volume_breakout": "rule.volume-breakout.2c1ded392b0e05da",
}


def rising_bars(count, start_minute, *, drift=.001, volume=10_000):
    """Steadily rising one-minute bars offset from the 09:30 New York open."""
    base = datetime(2026, 3, 10, 9, 30, tzinfo=NEW_YORK) + timedelta(
        minutes=start_minute)
    rows, price = [], 100.0
    for index in range(count):
        price *= (1 + drift)
        rows.append({"timestamp": base + timedelta(minutes=index), "symbol": "SPY",
                     "open": price, "high": price * 1.004, "low": price * .996,
                     "close": price, "volume": volume})
    return rows


BASE = {"family": "momentum_continuation", "lookback": 5, "slow_lookback": 20,
        "atr_period": 5, "threshold_bps": 10.0, "confirmation": "none"}


class V1IdentityTests(unittest.TestCase):
    """v2 must be additive: nothing about a v1 spec may move."""

    def test_default_spec_is_still_v1_and_carries_no_extension_fields(self):
        self.assertEqual(DEFAULT_RULE_SPEC["schema"], RULE_SCHEMA_V1)
        spec = validate_rule_spec({"family": "mean_reversion"})
        self.assertEqual(spec["schema"], RULE_SCHEMA_V1)
        for name in ("confirmations", "entry_after_minutes",
                     "entry_before_minutes", "min_atr_bps", "max_atr_bps"):
            self.assertNotIn(name, spec)

    def test_shipped_template_variant_ids_are_frozen(self):
        for raw in SHIPPED_V1_SPECS:
            with self.subTest(family=raw["family"]):
                self.assertEqual(rule_variant_id(raw),
                                 FROZEN_V1_VARIANT_IDS[raw["family"]])

    def test_v1_identity_is_stable_across_the_whole_v1_field_space(self):
        """A broad random sweep, not just the seven templates."""
        generator = random.Random(20260811)
        for _ in range(400):
            lookback = generator.randint(3, 110)
            raw = {
                "family": generator.choice(RULE_FAMILIES),
                "side": generator.choice(SIDES),
                "confirmation": generator.choice(CONFIRMATIONS),
                "lookback": lookback,
                "slow_lookback": lookback + generator.randint(1, 100),
                "range_minutes": generator.randint(3, 120),
                "threshold_bps": round(generator.uniform(0, 500), 3),
                "target_r": round(generator.uniform(.25, 10), 3),
                "max_hold_bars": generator.randint(1, 390),
            }
            spec = validate_rule_spec(raw)
            # The normalized form must contain exactly the v1 field set.
            self.assertEqual(set(spec), set(DEFAULT_RULE_SPEC))
            self.assertEqual(spec["schema"], RULE_SCHEMA_V1)

    def test_v1_rejects_extension_fields_with_a_version_error(self):
        with self.assertRaises(RuleSpecError) as caught:
            validate_rule_spec({"family": "mean_reversion",
                                "confirmations": ["trend"]})
        self.assertIn(RULE_SCHEMA_V2, str(caught.exception))

    def test_unknown_schema_is_refused(self):
        with self.assertRaises(RuleSpecError):
            validate_rule_spec({"schema": "rule-strategy.v5",
                                "family": "mean_reversion"})

    def test_v2_content_hash_is_frozen_when_v3_is_added(self):
        expected = {
            RULE_SCHEMA_V1:
                "825f856a71714f2fc2e1f3efd955e3088a90c8a27da0f4211b8d3b1f97b49008",
            RULE_SCHEMA_V2:
                "f7091bc68676e2e603e1a3796f83c593e36b3593cf8fa12cb1cc1d36665c23c3",
        }
        for schema, digest in expected.items():
            with self.subTest(schema=schema):
                spec = validate_rule_spec({**BASE, "schema": schema})
                self.assertEqual(rule_spec_hash(spec), digest)

    def test_v1_and_v2_signal_contracts_do_not_gain_v3_fields(self):
        bars = rising_bars(25, 0)
        for schema in (RULE_SCHEMA_V1, RULE_SCHEMA_V2):
            with self.subTest(schema=schema):
                signal = evaluate_rule_signal(
                    bars, validate_rule_spec({**BASE, "schema": schema}))
                self.assertIsNotNone(signal)
                self.assertNotIn("breakeven_r", signal)
                self.assertNotIn("rule_schema", signal)


class EligibilitySchemaTests(unittest.TestCase):
    def test_provider_schema_exposes_allowlist_only_on_cross_sectional_branch(self):
        for schema_name in (RULE_SCHEMA_V1, RULE_SCHEMA_V2, RULE_SCHEMA_V3):
            with self.subTest(schema=schema_name):
                schema = rule_spec_json_schema(schema_name)
                branches = schema["oneOf"]
                cross = next(branch for branch in branches
                             if branch["properties"]["family"].get("const") ==
                             "cross_sectional_residual")
                non_cross = next(branch for branch in branches
                                 if "enum" in branch["properties"]["family"])
                self.assertIn("eligible_symbols", cross["properties"])
                self.assertNotIn("eligible_symbols", non_cross["properties"])

        accepted = validate_rule_spec({
            "family": "cross_sectional_residual",
            "eligible_symbols": ["QQQ"],
        })
        self.assertEqual(accepted["eligible_symbols"], ["QQQ"])
        with self.assertRaisesRegex(RuleSpecError, "only valid"):
            validate_rule_spec({
                "family": "momentum_continuation",
                "eligible_symbols": ["QQQ"],
            })


class V3ValidationTests(unittest.TestCase):
    def test_v3_default_is_nullable_and_inherits_v2_entry_filters(self):
        spec = validate_rule_spec({"schema": RULE_SCHEMA_V3,
                                   "family": "mean_reversion"})
        self.assertIsNone(spec["breakeven_r"])
        self.assertEqual(spec["confirmations"], [])
        self.assertEqual(spec["entry_before_minutes"], 390)

    def test_breakeven_is_v3_only_bounded_and_below_target(self):
        for schema in (RULE_SCHEMA_V1, RULE_SCHEMA_V2):
            with self.subTest(schema=schema), self.assertRaisesRegex(
                    RuleSpecError, RULE_SCHEMA_V3):
                validate_rule_spec({"schema": schema,
                                    "family": "mean_reversion",
                                    "breakeven_r": 1.0})
        for value, target in ((True, 2.0), (-0.01, 2.0),
                              (10.1, 11.0), (1.0, 1.0), (2.0, 1.0)):
            with self.subTest(value=value, target=target), self.assertRaises(RuleSpecError):
                validate_rule_spec({"schema": RULE_SCHEMA_V3,
                                    "family": "mean_reversion",
                                    "target_r": min(target, 10.0),
                                    "breakeven_r": value})
        self.assertEqual(validate_rule_spec({
            "schema": RULE_SCHEMA_V3, "family": "mean_reversion",
            "target_r": 2.0, "breakeven_r": 1.0})["breakeven_r"], 1.0)


class V4ValidationTests(unittest.TestCase):
    def test_v4_defaults_are_explicit_and_fixed_r_is_backward_compatible(self):
        spec = validate_rule_spec({"schema": RULE_SCHEMA_V4,
                                   "family": "mean_reversion"})
        self.assertEqual(spec["target_mode"], "fixed_r")
        self.assertEqual(spec["target_lookback"], 20)
        self.assertIsNone(spec["trailing_stop_r"])
        self.assertIsNone(spec["exit_before_minutes"])
        v3 = validate_rule_spec({"schema": RULE_SCHEMA_V3,
                                 "family": "mean_reversion"})
        self.assertNotIn("target_mode", v3)

    def test_target_mode_and_rolling_mean_lookback_are_bounded(self):
        for mode in ("nope", "fixed", "vwap"):
            with self.assertRaises(RuleSpecError):
                validate_rule_spec({"schema": RULE_SCHEMA_V4,
                                    "family": "mean_reversion",
                                    "target_mode": mode})
        for lookback in (True, 1, 121, 2.5):
            with self.assertRaises(RuleSpecError):
                validate_rule_spec({"schema": RULE_SCHEMA_V4,
                                    "family": "mean_reversion",
                                    "target_mode": "rolling_mean",
                                    "target_lookback": lookback})
        for minutes in (True, 0, 391, 2.5):
            with self.assertRaises(RuleSpecError):
                validate_rule_spec({"schema": RULE_SCHEMA_V4,
                                    "family": "mean_reversion",
                                    "exit_before_minutes": minutes})

    def test_v4_extensions_are_rejected_by_older_schemas(self):
        for schema in (RULE_SCHEMA_V1, RULE_SCHEMA_V2, RULE_SCHEMA_V3):
            with self.subTest(schema=schema), self.assertRaisesRegex(
                    RuleSpecError, RULE_SCHEMA_V4):
                validate_rule_spec({"schema": schema, "family": "mean_reversion",
                                    "target_mode": "fixed_r"})


class V2ValidationTests(unittest.TestCase):
    def test_defaults_are_neutral_and_complete(self):
        spec = validate_rule_spec({"schema": RULE_SCHEMA_V2,
                                   "family": "mean_reversion"})
        self.assertEqual(spec["confirmations"], [])
        self.assertEqual(spec["entry_after_minutes"], 0)
        self.assertEqual(spec["entry_before_minutes"], 390)
        self.assertEqual(spec["min_atr_bps"], 0.0)
        self.assertEqual(spec["max_atr_bps"], 5_000.0)

    def test_confirmations_canonicalize_so_one_rule_is_one_variant(self):
        first = validate_rule_spec({"schema": RULE_SCHEMA_V2,
                                    "family": "mean_reversion",
                                    "confirmations": ["volume", "trend"]})
        second = validate_rule_spec({"schema": RULE_SCHEMA_V2,
                                     "family": "mean_reversion",
                                     "confirmations": ["trend", "volume", "trend"]})
        self.assertEqual(first["confirmations"], ["trend", "volume"])
        self.assertEqual(rule_variant_id(first), rule_variant_id(second))

    def test_confirmations_reject_unknown_names_strings_and_overflow(self):
        for value in ("trend", ["nope"], ["none"], [1],
                      ["trend", "volume", "volatility", "trend", "volume"][:MAX_CONFIRMATIONS + 1]):
            with self.subTest(value=value), self.assertRaises(RuleSpecError):
                validate_rule_spec({"schema": RULE_SCHEMA_V2,
                                    "family": "mean_reversion",
                                    "confirmations": value})

    def test_window_and_band_must_be_ordered_and_in_range(self):
        for override in ({"entry_after_minutes": 400},
                         {"entry_after_minutes": 200, "entry_before_minutes": 100},
                         {"entry_after_minutes": 60, "entry_before_minutes": 60},
                         {"min_atr_bps": 90.0, "max_atr_bps": 30.0},
                         {"min_atr_bps": 90.0, "max_atr_bps": 90.0},
                         {"max_atr_bps": 9_999.0},
                         {"entry_after_minutes": True}):
            with self.subTest(override=override), self.assertRaises(RuleSpecError):
                validate_rule_spec({"schema": RULE_SCHEMA_V2,
                                    "family": "mean_reversion", **override})

    def test_setup_evidence_reports_the_spec_schema_not_a_constant(self):
        v2 = validate_rule_spec({"schema": RULE_SCHEMA_V2, "family": "mean_reversion"})
        evidence = setup_evidence({}, {"strategy": {"rule_spec": v2}})
        self.assertEqual(evidence["schema"], RULE_SCHEMA_V2)


def ohlc_bars(count, step, *, start_minute=0, volume=lambda index: 3_000,
              last_body=None):
    """One-minute bars with real bodies, offset from the 09:30 open."""
    base = datetime(2026, 3, 10, 9, 30, tzinfo=NEW_YORK) + timedelta(
        minutes=start_minute)
    rows, price = [], 100.0
    for index in range(count):
        opened = price
        move = last_body if (last_body is not None and index == count - 1) else step(index)
        price *= (1 + move)
        high, low = max(opened, price) * 1.0005, min(opened, price) * .9995
        rows.append({"timestamp": base + timedelta(minutes=index), "symbol": "SPY",
                     "open": opened, "high": high, "low": low, "close": price,
                     "volume": volume(index)})
    return rows


SURGE = lambda index: 20_000 if index >= 75 else 3_000  # noqa: E731


class SessionAnchoredFamilyTests(unittest.TestCase):
    """The families added so discovery is not confined to seven primitives."""

    CASES = {
        # family: (rising-market bars, expected direction when rising)
        "vwap_trend": (
            lambda: ohlc_bars(80, lambda i: .0006, volume=SURGE), "long"),
        "opening_drive": (
            lambda: ohlc_bars(80, lambda i: .0008 if i < 30 else .0004,
                              volume=SURGE), "long"),
        "range_expansion": (
            lambda: ohlc_bars(60, lambda i: .0002, last_body=.004), "long"),
        # A fade: stretched above the session average, so it sells.
        "vwap_reversion": (
            lambda: ohlc_bars(80, lambda i: .0012 if i > 55 else 0.0), "short"),
    }

    @staticmethod
    def _template(family):
        from research.factory_core import family_template
        return validate_rule_spec(family_template(family))

    def test_every_new_family_is_in_the_catalog_with_a_template(self):
        from research.factory_core import FAMILY_TEMPLATES
        self.assertEqual([item["family"] for item in FAMILY_TEMPLATES],
                         list(RULE_FAMILIES))
        for family in self.CASES:
            self.assertIn(family, RULE_FAMILIES)

    def test_each_new_family_fires_on_its_own_shape(self):
        for family, (build, expected) in self.CASES.items():
            with self.subTest(family=family):
                signal = evaluate_rule_signal(build(), self._template(family))
                self.assertIsNotNone(signal, f"{family} never signalled")
                self.assertEqual(signal["direction"], expected)
                self.assertEqual(signal["setup_type"], f"rule_{family}")
                self.assertEqual(signal["family"], family)

    def test_each_new_family_is_symmetric(self):
        """A mirrored market must produce the mirrored side."""
        mirrored = {
            "vwap_trend": (ohlc_bars(80, lambda i: -.0006, volume=SURGE), "short"),
            "opening_drive": (ohlc_bars(
                80, lambda i: -.0008 if i < 30 else -.0004, volume=SURGE), "short"),
            "range_expansion": (ohlc_bars(
                60, lambda i: -.0002, last_body=-.004), "short"),
            "vwap_reversion": (ohlc_bars(
                80, lambda i: -.0012 if i > 55 else 0.0), "long"),
        }
        for family, (rows, expected) in mirrored.items():
            with self.subTest(family=family):
                signal = evaluate_rule_signal(rows, self._template(family))
                self.assertIsNotNone(signal, f"{family} never signalled")
                self.assertEqual(signal["direction"], expected)

    def test_a_flat_session_signals_nothing(self):
        flat = ohlc_bars(80, lambda index: 0.0)
        for family in self.CASES:
            with self.subTest(family=family):
                self.assertIsNone(
                    evaluate_rule_signal(flat, self._template(family)))

    def test_opening_families_require_every_minute_from_the_calendar_open(self):
        rows = ohlc_bars(80, lambda i: .0008 if i < 30 else .0004,
                         volume=SURGE)
        missing_open = rows[8:]
        for family in ("opening_range_breakout", "opening_range_fade",
                       "opening_drive"):
            with self.subTest(family=family):
                self.assertIsNone(evaluate_rule_signal(
                    missing_open, self._template(family)))

    def test_session_statistics_cannot_leak_across_days(self):
        """A longer history must not change today's VWAP or opening drive."""
        today = ohlc_bars(80, lambda index: .0006, volume=SURGE)
        yesterday = [{**row, "timestamp": row["timestamp"] - timedelta(days=1),
                      "open": row["open"] * 3, "high": row["high"] * 3,
                      "low": row["low"] * 3, "close": row["close"] * 3}
                     for row in ohlc_bars(80, lambda index: -.002)]
        for family in ("vwap_trend", "vwap_reversion", "opening_drive"):
            with self.subTest(family=family):
                spec = self._template(family)
                self.assertEqual(
                    evaluate_rule_signal(today, spec),
                    evaluate_rule_signal(yesterday + today, spec))

    def test_every_family_evaluates_cleanly_under_both_schemas(self):
        rows = ohlc_bars(80, lambda index: .0006, volume=SURGE)
        for family in RULE_FAMILIES:
            with self.subTest(family=family):
                evaluate_rule_signal(rows, self._template(family))
                widened = validate_rule_spec({
                    **self._template(family), "schema": RULE_SCHEMA_V2,
                    "entry_after_minutes": 15, "entry_before_minutes": 300,
                    "confirmations": ["trend"], "max_atr_bps": 900.0})
                evaluate_rule_signal(rows, widened)


class V2EvaluationTests(unittest.TestCase):
    """Each extension must actually gate, and only on the entry side."""

    def test_baseline_v1_signals_in_both_halves_of_the_session(self):
        spec = validate_rule_spec(BASE)
        self.assertIsNotNone(evaluate_rule_signal(rising_bars(40, 5), spec))
        self.assertIsNotNone(evaluate_rule_signal(rising_bars(40, 200), spec))

    def test_entry_window_suppresses_signals_outside_it(self):
        spec = validate_rule_spec({**BASE, "schema": RULE_SCHEMA_V2,
                                   "entry_after_minutes": 0,
                                   "entry_before_minutes": 60})
        self.assertIsNotNone(evaluate_rule_signal(rising_bars(40, 5), spec))
        self.assertIsNone(evaluate_rule_signal(rising_bars(40, 200), spec))

    def test_entry_window_is_inclusive_of_its_start_and_exclusive_of_its_end(self):
        bars = rising_bars(40, 5)
        signal = evaluate_rule_signal(bars, validate_rule_spec(BASE))
        minutes = int((datetime.fromtimestamp(signal["signal_ts"], NEW_YORK) -
                       datetime(2026, 3, 10, 9, 30, tzinfo=NEW_YORK)).total_seconds() // 60)
        exact = validate_rule_spec({**BASE, "schema": RULE_SCHEMA_V2,
                                    "entry_after_minutes": minutes,
                                    "entry_before_minutes": minutes + 1})
        self.assertIsNotNone(evaluate_rule_signal(bars, exact))
        past = validate_rule_spec({**BASE, "schema": RULE_SCHEMA_V2,
                                   "entry_after_minutes": minutes + 1,
                                   "entry_before_minutes": minutes + 2})
        self.assertIsNone(evaluate_rule_signal(bars, past))

    def test_volatility_band_gates_both_ends(self):
        bars = rising_bars(40, 5)
        wide = validate_rule_spec({**BASE, "schema": RULE_SCHEMA_V2})
        self.assertIsNotNone(evaluate_rule_signal(bars, wide))
        too_calm = validate_rule_spec({**BASE, "schema": RULE_SCHEMA_V2,
                                       "min_atr_bps": 500.0})
        self.assertIsNone(evaluate_rule_signal(bars, too_calm))
        too_wild = validate_rule_spec({**BASE, "schema": RULE_SCHEMA_V2,
                                       "max_atr_bps": 2.0})
        self.assertIsNone(evaluate_rule_signal(bars, too_wild))

    def test_volatility_confirmation_uses_compression_as_an_upper_bound(self):
        low_atr = validate_rule_spec({
            **BASE, "schema": RULE_SCHEMA_V2, "confirmation": "volatility",
            "compression_bps": 100.0,
        })
        high_atr = rising_bars(40, 5, drift=.01)
        low_atr_rows = rising_bars(40, 5, drift=.001)
        self.assertIsNotNone(evaluate_rule_signal(low_atr_rows, low_atr))
        self.assertIsNone(evaluate_rule_signal(high_atr, low_atr))

    def test_volatility_breakout_and_confirmation_share_the_same_polarity(self):
        rows = ohlc_bars(30, lambda _index: 0.0, last_body=.002)
        spec = validate_rule_spec({
            "schema": RULE_SCHEMA_V2, "family": "volatility_breakout",
            "lookback": 12, "atr_period": 3, "threshold_bps": 1.0,
            "compression_bps": 20.0, "confirmation": "volatility",
        })
        signal = evaluate_rule_signal(rows, spec)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["direction"], "long")

    def test_every_listed_confirmation_must_hold(self):
        quiet = rising_bars(40, 5, volume=10_000)
        passing = validate_rule_spec({**BASE, "schema": RULE_SCHEMA_V2,
                                      "confirmations": ["trend"]})
        self.assertIsNotNone(evaluate_rule_signal(quiet, passing))
        # Flat volume cannot clear a 3x relative-volume filter, so adding it to
        # a passing rule must suppress the signal rather than be ignored.
        blocked = validate_rule_spec({**BASE, "schema": RULE_SCHEMA_V2,
                                      "confirmations": ["trend", "volume"],
                                      "volume_multiplier": 3.0})
        self.assertIsNone(evaluate_rule_signal(quiet, blocked))

    def test_extensions_never_change_the_resulting_plan(self):
        """A v2 spec that admits a signal must produce the v1 plan exactly."""
        bars = rising_bars(40, 5)
        plain = evaluate_rule_signal(bars, validate_rule_spec(BASE))
        widened = evaluate_rule_signal(bars, validate_rule_spec({
            **BASE, "schema": RULE_SCHEMA_V2, "entry_after_minutes": 0,
            "entry_before_minutes": 390, "confirmations": ["trend"]}))
        self.assertIsNotNone(plain)
        self.assertIsNotNone(widened)
        for field in ("direction", "entry_price", "stop_price", "target_price",
                      "stop_distance", "target_r", "max_hold_bars", "signal_ts"):
            self.assertEqual(plain[field], widened[field], field)


if __name__ == "__main__":
    unittest.main()
