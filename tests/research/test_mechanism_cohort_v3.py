"""The corrected mechanism cohort v3 and the errata on the frozen cohorts."""

from pathlib import Path
from statistics import mean
import unittest

from agent.config import load_config
from agent.contracts.rule import (
    RULE_FAMILIES, RULE_SCHEMA_V7, evaluate_rule_signal, frozen_target_reference,
    rule_variant_id, rule_vehicle_executable, thesis_deadline_conflict,
    validate_rule_spec,
)
from research.mechanism_cohort_v2 import mechanism_cohort as mechanism_cohort_v2
from research.mechanism_cohort_v3 import COHORT_ID, mechanism_cohort
from research.mechanism_cost_cohort import mechanism_cost_cohort
from research.mechanism_deadline_cohort import mechanism_deadline_cohort
from research.mechanism_refresh_cohort import mechanism_refresh_cohort
from tests.research import rule_golden_harness as harness


V3_MANIFEST_HASH = (
    "c417f90fccb1bdfc1c735e50c57cd8a97743814a868f668732020504fd20797d")
FROZEN_MANIFEST_HASHES = {
    "v2": "41cb0233046d280c32e38f9af0af846debb4015baa52f4940a2822eb6ae59748",
    "cost": "f3f7cb105125541d52a5f9184ede43af5f6e0e3a69dff9eb9fc91685b9f0ab00",
    "deadline": "fb4b6667c80661d494d27ea64e639c8a92ba09cb74d190f1625a436e260c242d",
    "refresh": "67525c166888c5d10c2294ec1223200245d8ea89335cbaed1a75cd2418cbef76",
}


def _arms(manifest):
    return {family["name"]: family["arms"][0] for family in manifest["families"]}


def _market_context(market, symbol, day):
    """Prior sessions plus the current one for the subject and for SPY."""
    def upto(rows):
        return [row for row in rows
                if row["timestamp"].astimezone(harness.NY).date() <= day]
    return {symbol: upto(market[symbol]), "SPY": upto(market["SPY"])}


class MechanismCohortV3Tests(unittest.TestCase):
    def test_manifest_is_frozen_diagnostic_and_one_arm_per_family(self):
        manifest = mechanism_cohort()
        self.assertEqual(manifest, mechanism_cohort())
        self.assertEqual(manifest["cohort_id"], COHORT_ID)
        self.assertEqual(manifest["manifest_hash"], V3_MANIFEST_HASH)
        self.assertEqual([family["name"] for family in manifest["families"]],
                         list(RULE_FAMILIES))
        self.assertTrue(manifest["diagnostic_only"])
        self.assertFalse(manifest["authorizing"])
        self.assertEqual(manifest["parent_manifest_hash"],
                         FROZEN_MANIFEST_HASHES["v2"])
        ids = set()
        for family, arm in _arms(manifest).items():
            spec = validate_rule_spec(arm["rule_spec"])
            self.assertEqual(spec, arm["rule_spec"])
            self.assertEqual(spec["schema"], RULE_SCHEMA_V7)
            self.assertEqual(spec["min_stop_distance_bps"], 30.0)
            self.assertEqual(rule_variant_id(spec), arm["variant_id"])
            self.assertFalse(rule_vehicle_executable(spec, "equity"))
            ids.add(arm["variant_id"])
        self.assertEqual(len(ids), 12)

    def test_each_arm_is_its_v2_parent_plus_declared_corrections(self):
        parents = _arms(mechanism_cohort_v2())
        for family, arm in _arms(mechanism_cohort()).items():
            with self.subTest(family=family):
                parent = parents[family]
                self.assertEqual(arm["parent_variant_id"], parent["variant_id"])
                declared = {field for item in arm["corrections"]
                            for field in item["fields"]}
                changed = {key for key in set(parent["rule_spec"]) |
                           set(arm["rule_spec"])
                           if parent["rule_spec"].get(key) !=
                           arm["rule_spec"].get(key)}
                self.assertEqual(changed, declared | {"schema",
                                                      "min_stop_distance_bps"})
                self.assertEqual(sorted(changed),
                                 arm["fields_changed_from_parent"])
                self.assertEqual(arm["rule_spec"]["trailing_stop_r"], 1.5)

    def test_every_finding_is_applied_where_it_belongs(self):
        specs = {family: arm["rule_spec"]
                 for family, arm in _arms(mechanism_cohort()).items()}
        for family, spec in specs.items():
            with self.subTest(family=family):
                # A-01: the deadline, if any, fits the window and the hold.
                self.assertIsNone(thesis_deadline_conflict(
                    spec, require_full_window=True))
                # A-02 / A-05: no arm uses the inert floor-only stop.
                self.assertEqual(spec["stop_mode"],
                                 "structure" if family == "opening_range_fade"
                                 else "horizon_atr")
                # A-14: no trend confirmation on trend_pullback.
                if family == "trend_pullback":
                    self.assertNotIn("trend", {spec["confirmation"],
                                               *spec["confirmations"]})
        # A-03: value targets on the reversion arms, with a 15:30 deadline.
        for family, mode in (("opening_range_fade", "session_vwap"),
                             ("mean_reversion", "rolling_mean"),
                             ("vwap_reversion", "session_vwap")):
            self.assertEqual(specs[family]["target_mode"], mode)
            self.assertEqual(specs[family]["exit_before_minutes"], 30)
        # A-04: first-cross events on the state predicates the review named.
        self.assertEqual(
            {family for family, spec in specs.items()
             if spec.get("signal_event") == "first_cross"},
            {"opening_range_breakout", "opening_range_fade",
             "momentum_continuation", "volatility_breakout",
             "volume_breakout", "opening_drive"})
        # A-09, A-10, A-15.
        self.assertEqual(specs["cross_sectional_residual"]["residual_mode"], "beta")
        self.assertEqual(specs["momentum_continuation"]["volume_confirmation_mode"],
                         "same_clock")
        self.assertEqual(specs["volume_breakout"]["confirmation"], "none")
        self.assertEqual(specs["volume_breakout"]["relative_volume_mode"],
                         "same_clock")
        self.assertEqual(specs["range_expansion"]["range_multiplier"], 2.0)

    def test_every_corrected_arm_trades_on_the_synthetic_market(self):
        market = harness.synthetic_market()
        day = harness.SESSION_DATES[-1]
        for family, arm in _arms(mechanism_cohort()).items():
            with self.subTest(family=family):
                fired = None
                for symbol in harness.SYMBOLS:
                    rows = harness._session_rows(market[symbol], day)
                    context = _market_context(market, symbol, day)
                    fired = next((signal for index in range(len(rows))
                                  if (signal := evaluate_rule_signal(
                                      rows[:index + 1], arm["rule_spec"],
                                      bars_by_symbol=context, symbol=symbol))),
                                 None)
                    if fired is not None:
                        break
                self.assertIsNotNone(fired)
                self.assertIn(fired["stop_mode"], {"horizon_atr", "structure"})


class ReversionValueTargetTests(unittest.TestCase):
    """A-03: fixed-R reversion targets overshoot the anchor; v3 targets it."""

    def setUp(self):
        self.market = harness.synthetic_market()
        self.day = harness.SESSION_DATES[-1]

    def _signals(self, spec, *, context=False):
        out = []
        for symbol in harness.SYMBOLS:
            rows = harness._session_rows(self.market[symbol], self.day)
            kwargs = ({"bars_by_symbol": _market_context(
                self.market, symbol, self.day), "symbol": symbol}
                if context else {})
            for index in range(len(rows)):
                signal = evaluate_rule_signal(rows[:index + 1], spec, **kwargs)
                if signal:
                    out.append((rows[:index + 1], signal))
        return out

    def test_diagnostic_mean_reversion_target_overshoots_the_mean(self):
        from research.diagnostic_shadow import _logical_arms
        spec = next(arm["rule_spec"] for arm in _logical_arms()
                    if arm["variant_id"] ==
                    "rule.mean-reversion.b4e231565af56f03")
        signals = self._signals(spec)
        self.assertGreater(len(signals), 0)
        for prefix, signal in signals:
            anchor = mean(row["close"] for row in prefix[-spec["lookback"]:])
            beyond = (signal["target_price"] > anchor
                      if signal["direction"] == "long"
                      else signal["target_price"] < anchor)
            self.assertTrue(beyond)

    def test_v3_reversion_targets_are_the_frozen_value_anchor(self):
        arms = _arms(mechanism_cohort())
        checked = 0
        for family in ("mean_reversion", "vwap_reversion"):
            spec = arms[family]["rule_spec"]
            for prefix, signal in self._signals(spec, context=True):
                anchor = frozen_target_reference(prefix, spec)
                self.assertEqual(signal["target_price"], anchor)
                self.assertEqual(signal["target_reference"], anchor)
                checked += 1
        self.assertGreater(checked, 0)


class FrozenCohortErrataTests(unittest.TestCase):
    """A-01 and A-12 recorded beside, not inside, the frozen manifests."""

    def test_frozen_manifest_hashes_are_unchanged(self):
        self.assertEqual(
            {"v2": mechanism_cohort_v2()["manifest_hash"],
             "cost": mechanism_cost_cohort()["manifest_hash"],
             "deadline": mechanism_deadline_cohort()["manifest_hash"],
             "refresh": mechanism_refresh_cohort()["manifest_hash"]},
            FROZEN_MANIFEST_HASHES)

    def test_v2_and_cost_reversion_arms_are_marked_invalid(self):
        expected = {
            "opening_range_fade": ("09:50-12:30", "entry_window_and_hold_"
                                   "extend_past_thesis_deadline"),
            "mean_reversion": ("09:45-12:30", "entry_window_and_hold_"
                               "extend_past_thesis_deadline"),
            "vwap_reversion": ("10:00-14:30",
                               "thesis_deadline_at_or_before_entry_window"),
        }
        for manifest in (mechanism_cohort_v2(), mechanism_cost_cohort()):
            errata = manifest["errata"]
            self.assertFalse(errata["covered_by_manifest_hash"])
            arms = _arms(manifest)
            self.assertEqual(
                {item["family"]: (item["entry_window_new_york"], item["reason"])
                 for item in errata["invalid_arms"]}, expected)
            for item in errata["invalid_arms"]:
                self.assertEqual(item["thesis_deadline_new_york"], "10:00")
                self.assertEqual(item["variant_id"],
                                 arms[item["family"]]["variant_id"])
        self.assertEqual(mechanism_deadline_cohort()["errata"]["corrects"][0]
                         ["finding"], "A-01")

    def test_stress_label_correction_matches_the_shipped_configuration(self):
        risk = load_config(Path("config.yaml"))["risk"]
        self.assertEqual(float(risk["stressed_cost_scenario_bps"]), 9.0)
        self.assertEqual(float(risk["max_stressed_cost_to_risk_ratio"]), 0.30)
        for manifest in (mechanism_cost_cohort(), mechanism_deadline_cohort()):
            correction = manifest["errata"]["label_corrections"][0]
            self.assertEqual(correction["finding"], "A-12")
            self.assertEqual(correction["recorded_value"],
                             manifest["risk_policy"])
            self.assertIn("25bps", correction["recorded_value"])
            self.assertIn("9 / 0.30 = 30 bps", correction["statement"])



class BetaContextRefusalTests(unittest.TestCase):
    """Review of A-09: a beta arm without prior sessions is a context refusal."""

    def test_factory_reports_missing_beta_history_not_a_quiet_rule(self):
        from collections import Counter
        from datetime import timedelta
        from research.costs import ReplayPolicy
        from research.factory_core import simulate_account
        from research.market_data import normalize_underlying_bar

        market = harness.synthetic_market()
        bars = []
        for symbol in ("SPY", "QQQ"):
            for row in market[symbol]:
                end = row["timestamp"] + timedelta(minutes=1)
                bars.append(normalize_underlying_bar({
                    **row, "timestamp": row["timestamp"].isoformat(),
                    "provider": "alpaca", "source_mode": "forward_observed",
                    "as_of": end.isoformat(), "observed_at": end.isoformat(),
                    "session_date": row["timestamp"].astimezone(
                        harness.NY).date().isoformat()}))
        bars.sort(key=lambda bar: (bar.timestamp, bar.symbol))
        spec = next(family["arms"][0]["rule_spec"]
                    for family in mechanism_cohort()["families"]
                    if family["name"] == "cross_sectional_residual")
        rows = simulate_account(bars, [], spec, vehicle="equity", account_id="beta",
                                policy=ReplayPolicy(strict_market_data=False))["rows"]
        reasons = Counter(row.get("no_signal_reason") for row in rows
                          if row.get("symbol") == "QQQ" and
                          row.get("prefix_status") == "valid_prefix_no_signal")
        self.assertTrue(reasons)
        self.assertNotIn("rule_not_triggered", reasons)
        self.assertEqual(set(reasons), {"residual_beta_unavailable"})


if __name__ == "__main__":
    unittest.main()
