"""Focused fail-closed tests for empirical entry stress calibration."""

import unittest
import json
import tempfile
from unittest.mock import patch
from datetime import date, timedelta

from agent.config import validate_config
from agent.risk import RiskEngine
from research.costs import ReplayPolicy, check_stressed_cost_plan
from research.stressed_cost_calibration import (
    StressCalibrationError, calibrate_stressed_cost, resolve_stress_scenario,
    verify_stress_calibration_artifact)
from research.edge_ledger import content_hash
from research.cost_rerun import main as cost_rerun_main


def _session_ids(start: int, count: int) -> list[str]:
    origin = date(2026, 1, 1) + timedelta(days=int(start))
    return [(origin + timedelta(days=index)).isoformat() for index in range(count)]


def _section(*, quotes=100, sessions=4, spread=4.0, depth=10_000.0,
             session_start=0):
    return {
        "quote_count": quotes, "session_count": sessions,
        "sessions": _session_ids(session_start, sessions),
        "spread_bps": {"p95": spread},
        "touch_shares": {"p25": depth},
    }


def _schedule(*, spread=4.0, depth=10_000.0, quotes=100, sessions=5,
              provider="alpaca", feed="iex", buckets=True, sparse=0,
              digest="fit", session_start=0):
    session_ids = _session_ids(session_start, sessions)
    entry = {
        "quote_count": quotes, "session_count": sessions,
        "sessions": session_ids,
        "spread_bps": {"p95": spread},
        "touch_shares": {"p25": depth},
        "buckets": ({"m000_030": _section(
            quotes=quotes, sessions=sessions, spread=spread, depth=depth,
            session_start=session_start)}
                     if buckets else {}),
        "sparse_buckets": sparse,
    }
    schedule = {
        "schema": "quote-cost-schedule.v1",
        "measured": {
            "providers": [provider] if provider else [],
            "feeds": [feed] if feed else [], "min_quotes_per_cell": 50,
            "fixture_identity": digest,
        },
        "symbols": {"SPY": entry},
    }
    schedule["schedule_hash"] = content_hash(schedule)
    return schedule


def _rehash(artifact):
    result = json.loads(json.dumps(artifact))
    result.pop("content_hash", None)
    result["content_hash"] = content_hash(result)
    return result


def _rehash_schedule(schedule):
    schedule.pop("schedule_hash", None)
    schedule["schedule_hash"] = content_hash(schedule)
    return schedule


class StressCalibrationTests(unittest.TestCase):
    def test_liquid_cells_select_upward_ladder_rungs(self):
        low = calibrate_stressed_cost(
            _schedule(spread=4.0), expected_provider="alpaca",
            expected_feed="iex")
        high = calibrate_stressed_cost(
            _schedule(spread=20.0), expected_provider="alpaca",
            expected_feed="iex")
        self.assertEqual(low["cells"][0]["selected_scenario_bps"], 9.0)
        self.assertEqual(high["cells"][0]["selected_scenario_bps"], 15.0)

    def test_sparse_cell_falls_back_to_configured_25(self):
        result = calibrate_stressed_cost(
            _schedule(buckets=False, sparse=1), expected_feed="iex")
        cell = result["cells"][0]
        self.assertFalse(cell["usable"])
        self.assertEqual(cell["selected_scenario_bps"], 25.0)
        self.assertIn("sparse", cell["fallback_reason"])

    def test_validation_can_widen_but_never_narrow_fit(self):
        # 16 -> 8.5 bps (rung 9); 20 -> 10.5 bps (rung 15), while the
        # validation increase remains inside the materiality tolerance.
        fit = _schedule(spread=16.0, digest="fit")
        validation_wide = _schedule(spread=20.0, digest="validation")
        validation_tight = _schedule(spread=1.0, digest="validation-tight")
        widened = calibrate_stressed_cost(
            fit, validation_schedule=validation_wide, expected_feed="iex")
        retained = calibrate_stressed_cost(
            fit, validation_schedule=validation_tight, expected_feed="iex")
        self.assertEqual(widened["cells"][0]["selected_scenario_bps"], 15.0)
        self.assertEqual(retained["cells"][0]["selected_scenario_bps"], 9.0)

    def test_provider_mismatch_fails_closed(self):
        result = calibrate_stressed_cost(
            _schedule(provider="other"), expected_provider="alpaca",
            expected_feed="iex")
        cell = result["cells"][0]
        self.assertFalse(cell["usable"])
        self.assertEqual(cell["selected_scenario_bps"], 25.0)
        self.assertEqual(cell["fallback_reason"], "provider_mismatch")

    def test_present_but_sparse_validation_cell_fails_closed(self):
        fit = _schedule(spread=16.0, digest="fit")
        validation = _schedule(spread=16.0, quotes=10, digest="validation")
        result = calibrate_stressed_cost(
            fit, validation_schedule=validation, expected_feed="iex")
        cell = result["cells"][0]
        self.assertFalse(cell["usable"])
        self.assertEqual(cell["selected_scenario_bps"], 25.0)
        self.assertEqual(cell["fallback_reason"], "validation_cell_coverage")

    def test_failed_validation_window_is_retained_as_fail_closed_reason(self):
        result = calibrate_stressed_cost(
            _schedule(), expected_feed="iex",
            validation_failure_reason="validation_quote_window_below_measurement_floor")
        cell = result["cells"][0]
        self.assertFalse(cell["usable"])
        self.assertEqual(cell["fallback_reason"],
                         "validation_quote_window_below_measurement_floor")

    def test_hash_is_deterministic_and_validation_hash_is_persisted(self):
        kwargs = dict(expected_provider="alpaca", expected_feed="iex")
        fit = _schedule(digest="fit")
        validation = _schedule(spread=8.0, digest="validation")
        first = calibrate_stressed_cost(
            fit, validation_schedule=validation, **kwargs)
        second = calibrate_stressed_cost(
            fit, validation_schedule=validation, **kwargs)
        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertEqual(first["fit_schedule_hash"], fit["schedule_hash"])
        self.assertEqual(first["validation_schedule_hash"],
                         validation["schedule_hash"])

    def test_tampered_fit_or_validation_schedule_is_rejected(self):
        fit = _schedule(digest="fit")
        validation = _schedule(digest="validation", session_start=6)
        fit["symbols"]["SPY"]["spread_bps"]["p95"] = 40.0
        with self.assertRaisesRegex(StressCalibrationError, "hash"):
            calibrate_stressed_cost(
                fit, validation_schedule=validation,
                expected_provider="alpaca", expected_feed="iex")

        fit = _schedule(digest="fit")
        validation["symbols"]["SPY"]["spread_bps"]["p95"] = 40.0
        with self.assertRaisesRegex(StressCalibrationError, "hash"):
            calibrate_stressed_cost(
                fit, validation_schedule=validation,
                expected_provider="alpaca", expected_feed="iex")

    def test_cost_above_50_bps_falls_closed(self):
        result = calibrate_stressed_cost(
            _schedule(spread=110.0), expected_feed="iex")
        self.assertEqual(result["cells"][0]["selected_scenario_bps"], 25.0)
        self.assertEqual(result["cells"][0]["fallback_reason"],
                         "measured_cost_exceeds_ladder_max")

    def test_fit_only_artifact_cannot_activate(self):
        artifact = calibrate_stressed_cost(
            _schedule(), expected_provider="alpaca", expected_feed="iex")
        self.assertEqual(artifact["validation_schedule_hash"], None)
        self.assertEqual(
            resolve_stress_scenario(artifact, symbol="SPY", bucket="m000_030",
                                    operator_enabled=True,
                                    expected_provider="alpaca", expected_feed="iex"),
            (25.0, "validation_evidence_missing"))

    def test_validated_artifact_selects_symbol_bucket_and_missing_falls_back(self):
        artifact = calibrate_stressed_cost(
            _schedule(spread=16.0, digest="fit"),
            validation_schedule=_schedule(spread=20.0, digest="validation",
                                           session_start=6),
            expected_provider="alpaca", expected_feed="iex")
        self.assertEqual(
            resolve_stress_scenario(artifact, symbol="SPY", bucket="m000_030",
                                    operator_enabled=True,
                                    expected_provider="alpaca", expected_feed="iex",
                                    observation_session="2026-01-12"),
            (15.0, None))
        self.assertEqual(
            resolve_stress_scenario(artifact, symbol="QQQ", bucket="m000_030",
                                    operator_enabled=True,
                                    expected_provider="alpaca", expected_feed="iex",
                                    observation_session="2026-01-12"),
            (25.0, "cell_missing"))

    def test_tampered_hash_and_feed_mismatch_fail_closed(self):
        artifact = calibrate_stressed_cost(
            _schedule(digest="fit"),
            validation_schedule=_schedule(digest="validation", session_start=6),
            expected_provider="alpaca", expected_feed="iex")
        tampered = dict(artifact)
        tampered["provider"] = "other"
        self.assertEqual(verify_stress_calibration_artifact(
            tampered, expected_provider="alpaca", expected_feed="iex"),
            (False, "artifact_content_hash_invalid"))
        self.assertEqual(resolve_stress_scenario(
            artifact, symbol="SPY", bucket="m000_030", operator_enabled=True,
            expected_provider="alpaca", expected_feed="sip",
            observation_session="2026-01-12"),
            (25.0, "feed_mismatch"))

    def test_runtime_and_replay_use_the_same_activated_cell(self):
        artifact = calibrate_stressed_cost(
            _schedule(digest="fit"),
            validation_schedule=_schedule(digest="validation", session_start=6),
            expected_provider="alpaca", expected_feed="iex")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
            json.dump(artifact, handle)
            handle.flush()
            config = validate_config({"risk": {
                "stressed_cost_calibration_enabled": True,
                "stressed_cost_calibration_path": handle.name,
            }})
            plan = {"symbol": "SPY", "entry_timestamp": "2026-01-12T14:45:00+00:00",
                    "execution_profile": "shares", "shares": 10,
                    "notional": 1_000.0, "risk_usd": 10.0}
            runtime, runtime_reason = RiskEngine(config).check_stressed_cost(plan, cfg=config)
            replay, replay_reason = check_stressed_cost_plan(
                plan, scenario_bps=25.0, max_ratio=.30, config=config)
            self.assertIsNone(runtime_reason)
            self.assertIsNone(replay_reason)
            self.assertEqual(runtime["stressed_cost_scenario_bps"], 9.0)
            self.assertEqual(replay["stressed_cost_scenario_bps"], 9.0)
            option_plan = {**plan, "execution_profile": "options",
                           "contracts": 1, "shares": None, "risk_usd": 20.0}
            option_runtime, option_runtime_reason = RiskEngine(config).check_stressed_cost(
                option_plan, cfg=config)
            option_replay, option_replay_reason = check_stressed_cost_plan(
                option_plan, scenario_bps=25.0, max_ratio=.30, config=config)
            self.assertIsNone(option_runtime_reason)
            self.assertIsNone(option_replay_reason)
            self.assertEqual(option_runtime["stressed_cost_scenario_bps"], 25.0)
            self.assertEqual(option_replay["stressed_cost_scenario_bps"], 25.0)
            self.assertEqual(option_runtime["stressed_cost_activation_reason"],
                             "calibration_equity_only")
            self.assertEqual(option_replay["stressed_cost_activation_reason"],
                             "calibration_equity_only")

    def test_vet_open_carries_market_timestamp_for_runtime_activation(self):
        artifact = self._validated_artifact()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
            json.dump(artifact, handle)
            handle.flush()
            config = validate_config({"risk": {
                "stressed_cost_calibration_enabled": True,
                "stressed_cost_calibration_path": handle.name,
            }})
            decision = {"symbol": "SPY", "direction": "long",
                        "entry_price": 100.0, "stop_price": 99.0,
                        "target_price": 102.0}
            post = {"price": 100.0,
                    "quote": {"timestamp": "2026-01-12T14:45:00+00:00"}}
            plan, reason = RiskEngine(config).vet_open(
                decision, 100_000.0, [], {"SPY": post}, {}, 0.0,
                now=1768229100.0, cost_cfg=config)
            self.assertIsNone(reason)
            self.assertEqual(plan["entry_timestamp"],
                             "2026-01-12T14:45:00+00:00")
            self.assertEqual(plan["stressed_cost_scenario_bps"], 15.0)
            for timestamp, expected_reason in (
                    ("2026-01-11T14:45:00+00:00",
                     "observation_before_effective_after_session"),
                    ("2026-01-12T14:46:00+00:00",
                     "observation_session_missing"),
                    (None, "observation_session_missing")):
                snapshot = {"price": 100.0,
                            "quote": ({"timestamp": timestamp}
                                      if timestamp is not None else {})}
                plan, reason = RiskEngine(config).vet_open(
                    decision, 100_000.0, [], {"SPY": snapshot}, {}, 0.0,
                    now=1768229100.0, cost_cfg=config)
                self.assertIsNone(reason)
                self.assertEqual(plan["stressed_cost_scenario_bps"], 25.0)
                self.assertEqual(plan["stressed_cost_activation_reason"],
                                 expected_reason)

    def test_pre_validation_and_missing_observation_fall_back(self):
        artifact = self._validated_artifact()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
            json.dump(artifact, handle)
            handle.flush()
            config = validate_config({"risk": {
                "stressed_cost_calibration_enabled": True,
                "stressed_cost_calibration_path": handle.name,
            }})
            for timestamp, expected_reason in (
                    ("2026-01-11T14:45:00+00:00",
                     "observation_before_effective_after_session"),
                    (None, "observation_session_missing")):
                plan = {"symbol": "SPY", "entry_timestamp": timestamp,
                        "execution_profile": "shares", "shares": 10,
                        "notional": 1_000.0, "risk_usd": 10.0}
                checked, reason = RiskEngine(config).check_stressed_cost(
                    plan, cfg=config)
                self.assertIsNone(reason)
                self.assertEqual(checked["stressed_cost_scenario_bps"], 25.0)
                self.assertEqual(checked["stressed_cost_activation_reason"],
                                 expected_reason)

    def test_explicit_bucket_without_observation_session_falls_back(self):
        artifact = self._validated_artifact()
        self.assertEqual(resolve_stress_scenario(
            artifact, symbol="SPY", bucket="m000_030", operator_enabled=True,
            expected_provider="alpaca", expected_feed="iex"),
            (25.0, "observation_session_missing"))

    def test_multi_cell_uses_artifact_level_latest_validation_boundary(self):
        fit = _schedule(spread=16.0, digest="fit", session_start=0)
        validation = _schedule(spread=20.0, digest="validation", session_start=6)
        later_fit = _schedule(spread=16.0, digest="fit", session_start=0)
        later_validation = _schedule(
            spread=20.0, digest="validation", session_start=10)
        fit["symbols"]["QQQ"] = later_fit["symbols"]["SPY"]
        validation["symbols"]["QQQ"] = later_validation["symbols"]["SPY"]
        _rehash_schedule(fit)
        _rehash_schedule(validation)
        artifact = calibrate_stressed_cost(
            fit, validation_schedule=validation,
            expected_provider="alpaca", expected_feed="iex")
        self.assertEqual(artifact["effective_after_session"], "2026-01-15")
        self.assertEqual(resolve_stress_scenario(
            artifact, symbol="SPY", bucket="m000_030", operator_enabled=True,
            expected_provider="alpaca", expected_feed="iex",
            observation_session="2026-01-12"),
            (25.0, "observation_before_effective_after_session"))
        self.assertEqual(resolve_stress_scenario(
            artifact, symbol="SPY", bucket="m000_030", operator_enabled=True,
            expected_provider="alpaca", expected_feed="iex",
            observation_session="2026-01-16"),
            (15.0, None))

    def _validated_artifact(self):
        return calibrate_stressed_cost(
            _schedule(spread=16.0, digest="fit", session_start=0),
            validation_schedule=_schedule(
                spread=20.0, digest="validation", session_start=6),
            expected_provider="alpaca", expected_feed="iex")

    def test_overlapping_validation_sessions_fail_closed(self):
        artifact = self._validated_artifact()
        cell = dict(artifact["cells"][0])
        cell["validation_sessions"] = list(cell["fit_sessions"])
        artifact["cells"] = [cell]
        self.assertEqual(verify_stress_calibration_artifact(
            _rehash(artifact), expected_provider="alpaca", expected_feed="iex"),
            (False, "cell_sessions_overlap"))

    def test_reversed_fit_validation_sessions_fail_closed(self):
        artifact = self._validated_artifact()
        cell = dict(artifact["cells"][0])
        cell["fit_sessions"] = _session_ids(12, 5)
        artifact["cells"] = [cell]
        self.assertEqual(verify_stress_calibration_artifact(
            _rehash(artifact), expected_provider="alpaca", expected_feed="iex"),
            (False, "cell_sessions_not_chronological"))

    def test_reversed_mixed_date_datetime_sessions_fail_closed(self):
        artifact = self._validated_artifact()
        cell = dict(artifact["cells"][0])
        # The fit window is actually later than validation, and it uses
        # datetime IDs while validation retains date-only IDs. Mixed kinds
        # must be rejected before tuple ordering can authorize the artifact.
        cell["fit_sessions"] = [
            f"2026-01-{12 + index:02d}T00:00:00+00:00"
            for index in range(5)]
        artifact["cells"] = [cell]
        self.assertEqual(verify_stress_calibration_artifact(
            _rehash(artifact), expected_provider="alpaca", expected_feed="iex"),
            (False, "cell_sessions_mixed_kinds"))

    def test_missing_session_ids_fail_closed(self):
        artifact = self._validated_artifact()
        cell = dict(artifact["cells"][0])
        cell.pop("validation_sessions", None)
        artifact["cells"] = [cell]
        self.assertEqual(verify_stress_calibration_artifact(
            _rehash(artifact), expected_provider="alpaca", expected_feed="iex"),
            (False, "cell_sessions_missing"))

    def test_calibration_only_out_is_a_runtime_artifact(self):
        artifact = self._validated_artifact()
        wrapper = {
            "schema": "stressed-cost-calibration-run.v1",
            "diagnostic_only": True, "authorizing": False,
            "stress_calibration": artifact,
            "activation": {"ready": True, "reasons": []},
        }
        with tempfile.TemporaryDirectory() as directory:
            config_path = f"{directory}/config.json"
            out_path = f"{directory}/artifact.json"
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump({}, handle)
            with patch("research.cost_rerun.run_cost_calibration",
                       return_value=wrapper):
                self.assertEqual(cost_rerun_main([
                    "--calibration-only", "--corpus", "unused",
                    "--config", config_path, "--out", out_path]), 0)
            with open(out_path, encoding="utf-8") as handle:
                persisted = json.load(handle)
            self.assertEqual(persisted["schema"], "stressed-cost-calibration.v1")
            self.assertEqual(verify_stress_calibration_artifact(
                persisted, expected_provider="alpaca", expected_feed="iex"),
                (True, None))


if __name__ == "__main__":
    unittest.main()
