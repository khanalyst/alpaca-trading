"""Regression coverage for fail-open signal-quality prescreen semantics."""

import unittest
from unittest.mock import patch

from agent.contracts.rule import rule_variant_id, validate_rule_spec
from research import strategy_factory as factory
from research.signal_quality import DEFAULT_HORIZONS


def _eligibility(total_cells, *, status="actionable_signal"):
    return {
        "schema": "signal-quality-eligibility.v1",
        "scope": "fit_only",
        "authorizing": False,
        "diagnostic_only": True,
        "status": status,
        "total_cells": total_cells,
        "eligible_cells": total_cells,
        "mature_prefixes": total_cells,
        "evaluator_prefixes": total_cells,
        "data_ineligible_cells": 0,
        "data_incomplete_cells": 0,
        "truncated": False,
    }


def _actionable_quality(*, count, horizon, variant_id="variant",
                        delta=-0.000001, session_count=None):
    return {
        "schema": "signal-quality.v2",
        "scope": "fit_only",
        "authorizing": False,
        "diagnostic_only": True,
        "variant_id": variant_id,
        "event_count": count,
        "session_count": count if session_count is None else session_count,
        "event_rejection_counts": {},
        "eligibility_provenance": _eligibility(count),
        "horizon_metrics": {
            f"{horizon}m": {
                "candidate_count": count,
                "matched_count": count,
                "candidate_minus_control_bps": delta,
            },
        },
    }


class ScreenHorizonSafetyTests(unittest.TestCase):
    def test_worker_requests_and_selects_exact_authored_holds(self):
        base = validate_rule_spec({})
        self.assertEqual(base["max_hold_bars"], 90)
        specs = [validate_rule_spec({**base, "max_hold_bars": hold})
                 for hold in (90, 180, 240)]
        requested = {}

        def measure(_bars, spec, **kwargs):
            hold = spec["max_hold_bars"]
            requested[hold] = tuple(kwargs["horizons"])
            return _actionable_quality(
                count=1, horizon=hold, variant_id=rule_variant_id(spec))

        with patch.object(
                factory, "_fit_partition",
                return_value=(["fit-bar"], ["2026-01-05"], 1)), \
                patch.object(factory, "_screen_bar_cells", return_value=1), \
                patch.object(factory, "_fit_prefixes", return_value={
                    "first_signals": [],
                    "eligibility_provenance": _eligibility(1),
                }), \
                patch.object(factory, "measure_signal_quality",
                             side_effect=measure):
            result = factory._signal_quality_screen_worker({
                "hypothesis": {"hypothesis_id": "hypothesis"},
                "specs": specs,
                "bars": [],
                "snapshots": [],
                "quotes": [],
                "policy": None,
            })

        for spec in specs:
            hold = spec["max_hold_bars"]
            with self.subTest(hold=hold):
                self.assertEqual(
                    requested[hold],
                    tuple(sorted(set(DEFAULT_HORIZONS) | {hold})))
                record = result["screens"][rule_variant_id(spec)]
                self.assertEqual(
                    record["primary_horizon"]["horizon_minutes"], hold)

    def test_missing_exact_horizon_fails_open_instead_of_rounding(self):
        spec = validate_rule_spec({"max_hold_bars": 90})
        quality = _actionable_quality(count=30, horizon=60)
        quality["horizon_metrics"]["120m"] = dict(
            quality["horizon_metrics"]["60m"])

        primary = factory._screen_primary_horizon(spec, quality)
        record = factory._signal_quality_screen_record(
            quality, variant_id="variant", fit_cells=30,
            primary_horizon=primary)

        self.assertIsNone(primary)
        self.assertEqual(record["status"], "complete_actionable_signal")
        self.assertFalse(factory._screen_record_can_skip(
            record, variant_id="variant"))

    def test_nonpositive_point_estimates_never_skip_at_any_count(self):
        for count in (1, 29, 30, 100):
            with self.subTest(count=count):
                quality = _actionable_quality(
                    count=count, horizon=90, session_count=1)
                record = factory._signal_quality_screen_record(
                    quality, variant_id="variant", fit_cells=count,
                    primary_horizon=90)

                self.assertEqual(quality["session_count"], 1)
                self.assertLessEqual(
                    record["primary_horizon"][
                        "candidate_minus_control_bps"], 0.0)
                if count >= 30:
                    self.assertEqual(
                        record["status"], "complete_actionable_signal")
                self.assertFalse(factory._screen_record_can_skip(
                    record, variant_id="variant"))

    def test_legacy_sealed_nonpositive_record_fails_open(self):
        quality = _actionable_quality(count=32, horizon=60)
        legacy = factory._seal_signal_quality_screen_record({
            "schema": "signal-quality-screen.v2",
            "scope": "fit_only",
            "authorizing": False,
            "diagnostic_only": True,
            "variant_id": "variant",
            "status": "complete_nonpositive_control",
            "reason": "nonpositive_fit_control_delta",
            "event_count": 32,
            "fit_cells": 32,
            "event_rejection_counts": {},
            "primary_horizon": {
                "horizon_minutes": 60,
                "candidate_count": 32,
                "matched_count": 32,
                "matched_coverage": 1.0,
                "candidate_minus_control_bps": -1.0,
            },
            "digest": None,
        }, quality=quality)

        self.assertTrue(legacy["digest"])
        self.assertFalse(factory._screen_record_can_skip(
            legacy, variant_id="variant"))

    def test_complete_zero_can_skip_but_underpowered_and_malformed_continue(self):
        zero_quality = {
            "schema": "signal-quality.v2",
            "scope": "fit_only",
            "authorizing": False,
            "diagnostic_only": True,
            "variant_id": "variant",
            "event_count": 0,
            "event_rejection_counts": {"no_actionable_signal": 2},
            "eligibility_provenance": _eligibility(
                2, status="predicate_no_actionable_signal"),
        }
        complete = factory._signal_quality_screen_record(
            zero_quality, variant_id="variant", fit_cells=2)
        self.assertTrue(factory._screen_record_can_skip(
            complete, variant_id="variant"))

        underpowered = factory._signal_quality_screen_record(
            _actionable_quality(count=29, horizon=90),
            variant_id="variant", fit_cells=29, primary_horizon=90)
        self.assertEqual(underpowered["status"], "underpowered_control")
        self.assertFalse(factory._screen_record_can_skip(
            underpowered, variant_id="variant"))
        malformed = factory._signal_quality_screen_record(
            {"schema": "signal-quality.v2"},
            variant_id="variant", fit_cells=2)
        self.assertEqual(malformed["status"], "unknown")
        self.assertFalse(factory._screen_record_can_skip(
            malformed, variant_id="variant"))
        self.assertFalse(factory._screen_record_can_skip(
            {"schema": "signal-quality-screen.v2"}, variant_id="variant"))


if __name__ == "__main__":
    unittest.main()
