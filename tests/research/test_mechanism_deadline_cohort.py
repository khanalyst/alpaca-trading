import unittest
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from agent.contracts.rule import (
    RULE_SCHEMA_V7,
    SESSION_MINUTES,
    rule_vehicle_executable,
    thesis_exit_deadline,
)
from research.mechanism_cost_cohort import mechanism_cost_cohort
from research.mechanism_deadline_cohort import (
    COHORT_ID,
    mechanism_deadline_cohort,
)


FROZEN_COST_MANIFEST_HASH = (
    "f3f7cb105125541d52a5f9184ede43af5f6e0e3a69dff9eb9fc91685b9f0ab00")
FROZEN_COST_PARENT_IDS = [
    ("opening_range_breakout", "rule.opening-range-breakout.2aa6eecce57d9c56"),
    ("opening_range_fade", "rule.opening-range-fade.8a6efdff14783e2b"),
    ("momentum_continuation", "rule.momentum-continuation.bdc87e262eb6fb8a"),
    ("mean_reversion", "rule.mean-reversion.264ff151b4541339"),
    ("trend_pullback", "rule.trend-pullback.203b7d4417ea9bda"),
    ("volatility_breakout", "rule.volatility-breakout.0a3e27f02d6c32e2"),
    ("volume_breakout", "rule.volume-breakout.3f366a2a931587ad"),
    ("vwap_reversion", "rule.vwap-reversion.6907a109eaf5f882"),
    ("vwap_trend", "rule.vwap-trend.23428d749bda8288"),
    ("range_expansion", "rule.range-expansion.4b22b60b28b8c96d"),
    ("opening_drive", "rule.opening-drive.0d82e82b09466fa9"),
    ("cross_sectional_residual", "rule.cross-sectional-residual.a4c9876c220419b0"),
]


class DeadlineCompatibleCohortTests(unittest.TestCase):
    def test_manifest_is_deterministic_and_has_the_frozen_parent_identity(self):
        parent_before = mechanism_cost_cohort()
        first = mechanism_deadline_cohort()
        second = mechanism_deadline_cohort()
        parent_after = mechanism_cost_cohort()

        self.assertEqual(first, second)
        self.assertEqual(first["cohort_id"], COHORT_ID)
        self.assertEqual(parent_before, parent_after)
        self.assertEqual(parent_before["manifest_hash"], FROZEN_COST_MANIFEST_HASH)
        self.assertEqual(
            [(family["name"], family["arms"][0]["variant_id"])
             for family in parent_before["families"]],
            FROZEN_COST_PARENT_IDS,
        )
        self.assertEqual(first["parent_cohort_id"], parent_before["cohort_id"])
        self.assertEqual(first["parent_manifest_hash"], FROZEN_COST_MANIFEST_HASH)
        self.assertEqual(first["registered_arms"], 3)
        self.assertTrue(first["diagnostic_only"])
        self.assertFalse(first["authorizing"])
        self.assertFalse(first["runtime_config_changed"])
        self.assertEqual(
            [family["name"] for family in first["families"]],
            ["opening_range_fade", "mean_reversion", "vwap_reversion"],
        )

    def test_only_deadline_changes_from_each_v7_cost_parent(self):
        parent = mechanism_cost_cohort()
        child = mechanism_deadline_cohort()
        parent_by_family = {family["name"]: family for family in parent["families"]}

        child_ids = set()
        for family in child["families"]:
            arm = family["arms"][0]
            parent_arm = parent_by_family[family["name"]]["arms"][0]
            spec = dict(arm["rule_spec"])
            parent_spec = dict(parent_arm["rule_spec"])
            self.assertEqual(spec.pop("exit_before_minutes"), 30)
            self.assertEqual(parent_spec.pop("exit_before_minutes"), 360)
            self.assertEqual(spec, parent_spec)
            self.assertEqual(arm["parent_variant_id"], parent_arm["variant_id"])
            self.assertEqual(arm["timing_delta"], {
                "exit_before_minutes": {"before": 360, "after": 30},
            })
            self.assertEqual(arm["inherited_max_hold_bars"], spec["max_hold_bars"])
            self.assertNotEqual(arm["variant_id"], parent_arm["variant_id"])
            self.assertEqual(spec["schema"], RULE_SCHEMA_V7)
            self.assertEqual(spec["min_stop_distance_bps"], 100.0)
            self.assertFalse(rule_vehicle_executable(spec, "equity"))
            child_ids.add(arm["variant_id"])
        self.assertEqual(len(child_ids), 3)

    def test_entry_windows_fit_before_the_new_york_thesis_deadline(self):
        manifest = mechanism_deadline_cohort()
        ny = ZoneInfo("America/New_York")
        session_date = date(2026, 10, 8)
        entry_timestamp = datetime.combine(
            session_date, time(9, 30), tzinfo=ny).timestamp()
        actual_deadline = datetime.combine(
            session_date, time(15, 30), tzinfo=ny).timestamp()
        frozen_deadline = datetime.combine(
            session_date, time(10, 0), tzinfo=ny).timestamp()
        parent_by_family = {
            family["name"]: family
            for family in mechanism_cost_cohort()["families"]
        }
        for family in manifest["families"]:
            spec = family["arms"][0]["rule_spec"]
            deadline_minutes = SESSION_MINUTES - spec["exit_before_minutes"]
            self.assertEqual(deadline_minutes, 360)
            self.assertEqual(
                thesis_exit_deadline(entry_timestamp, spec), actual_deadline)
            parent_spec = parent_by_family[family["name"]]["arms"][0]["rule_spec"]
            self.assertEqual(
                thesis_exit_deadline(entry_timestamp, parent_spec), frozen_deadline)
            latest_entry_plus_hold = (
                spec["entry_before_minutes"] + spec["max_hold_bars"] + 1)
            self.assertLessEqual(latest_entry_plus_hold, deadline_minutes)
            self.assertGreater(
                latest_entry_plus_hold,
                SESSION_MINUTES - parent_spec["exit_before_minutes"],
            )

    def test_timing_deltas_are_isolated_from_callers_and_future_manifests(self):
        first = mechanism_deadline_cohort()
        deltas = [family["arms"][0]["timing_delta"] for family in first["families"]]
        self.assertEqual(len({id(delta) for delta in deltas}), 3)
        self.assertEqual(len({id(delta["exit_before_minutes"]) for delta in deltas}), 3)

        deltas[0]["exit_before_minutes"]["after"] = 999
        self.assertEqual(deltas[1]["exit_before_minutes"]["after"], 30)
        self.assertEqual(deltas[2]["exit_before_minutes"]["after"], 30)
        fresh = mechanism_deadline_cohort()
        self.assertEqual(
            fresh["families"][0]["arms"][0]["timing_delta"],
            {"exit_before_minutes": {"before": 360, "after": 30}},
        )

    def test_targets_and_max_hold_horizons_remain_inherited(self):
        parent = mechanism_cost_cohort()
        child = mechanism_deadline_cohort()
        parent_by_family = {family["name"]: family for family in parent["families"]}
        for family in child["families"]:
            spec = family["arms"][0]["rule_spec"]
            parent_spec = parent_by_family[family["name"]]["arms"][0]["rule_spec"]
            for field in ("target_r", "target_mode", "target_lookback", "max_hold_bars",
                          "entry_after_minutes", "entry_before_minutes"):
                self.assertEqual(spec[field], parent_spec[field], field)


if __name__ == "__main__":
    unittest.main()
