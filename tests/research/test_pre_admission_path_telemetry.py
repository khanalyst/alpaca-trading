"""Fit-only telemetry for opportunities rejected before cost admission."""

from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
import unittest
from unittest.mock import patch

from agent.contracts.rule import validate_rule_spec
import research.fit_diagnostics as fit_diagnostics_module
from research.costs import ReplayPolicy
from research.factory_core import simulate_account
from research.fit_diagnostics import measure_fit_diagnostics
from research.path_telemetry import (
    aggregate_pre_admission_path_telemetry,
    compute_pre_admission_path_telemetry,
)
from tests.research.test_costs import FLAT, RISING, SPEC, _bars, _quote


BASE = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)


def _bar(index, opened, high, low, close, **updates):
    stamp = BASE + timedelta(minutes=index)
    row = {
        "symbol": "SPY", "session_date": "2026-01-05",
        "timestamp": stamp.isoformat(), "interval_seconds": 60,
        "open": opened, "high": high, "low": low, "close": close,
    }
    row.update(updates)
    return row


def _plan(**updates):
    result = {
        "schema": "pre-admission-plan.v1", "symbol": "SPY",
        "session_date": "2026-01-05", "direction": "long",
        "entry_timestamp": BASE.isoformat(), "entry_reference": 100.0,
        "authored_entry_reference": 99.0,
        "authored_stop_price": 98.0, "authored_target_price": 104.0,
        "authored_stop_distance": 2.0, "authored_target_r": 2.0,
        "authored_max_hold_bars": 2,
        "tick_normalized_stop_price": 98.0,
        "tick_normalized_target_price": 104.0,
        "tick_normalized_stop_distance": 2.0,
        "exit_deadline_timestamp": (BASE + timedelta(minutes=3)).isoformat(),
        "exit_deadline_reason": "max_hold",
        "stress_scenario_bps": 25.0,
        "stress_max_cost_to_risk_ratio": .3,
        "required_stop_floor_bps": 25.0 / .3,
        "required_tick_rounded_stop_distance": .84,
        "required_tick_rounded_stop_distance_bps": 84.0,
    }
    result.update(updates)
    return result


class PreAdmissionPathTelemetryTests(unittest.TestCase):
    def test_long_short_and_entry_reference_not_signal_price(self):
        long = compute_pre_admission_path_telemetry(_plan(), [
            _bar(0, 100, 101, 99, 100),
            _bar(1, 100, 104.5, 99.5, 104),
            _bar(2, 104, 105, 103, 104),
        ])
        self.assertEqual(long["status"], "usable")
        self.assertAlmostEqual(long["mfe_bps"], 500.0)
        self.assertAlmostEqual(long["mae_bps"], -100.0)
        self.assertAlmostEqual(long["mfe_authored_r"], 2.5)
        self.assertEqual(long["fixed_barrier_first_reached"], "target")
        self.assertEqual(long["entry_reference"], 100.0)
        self.assertNotEqual(long["entry_reference"],
                            long["authored_entry_reference"])

        short = compute_pre_admission_path_telemetry(_plan(
            direction="short", authored_stop_price=102.0,
            authored_target_price=96.0, tick_normalized_stop_price=102.0,
            tick_normalized_target_price=96.0), [
                _bar(0, 100, 101, 99, 100),
                _bar(1, 100, 101, 95.5, 96),
                _bar(2, 96, 97, 95, 96),
            ])
        self.assertEqual(short["status"], "usable")
        self.assertAlmostEqual(short["mfe_bps"], 500.0)
        self.assertAlmostEqual(short["mae_bps"], -100.0)
        self.assertEqual(short["fixed_barrier_first_reached"], "target")

    def test_exact_deadline_caps_path_for_early_close(self):
        plan = _plan(
            exit_deadline_timestamp=(BASE + timedelta(minutes=2)).isoformat(),
            exit_deadline_reason="session_force_flat")
        result = compute_pre_admission_path_telemetry(plan, [
            _bar(0, 100, 101, 99, 100),
            _bar(1, 100, 102, 99, 101),
            _bar(2, 101, 150, 50, 100),
        ])
        self.assertEqual(result["status"], "usable")
        self.assertEqual(result["observed_bars"], 2)
        self.assertEqual(result["horizon_timestamp"],
                         plan["exit_deadline_timestamp"])
        self.assertAlmostEqual(result["mfe_bps"], 200.0)

    def test_one_sided_entry_gaps_clamp_excursions_at_zero(self):
        long = compute_pre_admission_path_telemetry(
            _plan(exit_deadline_timestamp=(BASE + timedelta(minutes=1)).isoformat()),
            [_bar(0, 97, 99, 95, 96)])
        self.assertEqual(long["mfe_bps"], 0.0)
        self.assertAlmostEqual(long["mae_bps"], -500.0)

        short = compute_pre_admission_path_telemetry(
            _plan(direction="short", authored_stop_price=102.0,
                  authored_target_price=96.0, tick_normalized_stop_price=102.0,
                  tick_normalized_target_price=96.0,
                  exit_deadline_timestamp=(BASE + timedelta(minutes=1)).isoformat()),
            [_bar(0, 103, 105, 101, 104)])
        self.assertEqual(short["mfe_bps"], 0.0)
        self.assertAlmostEqual(short["mae_bps"], -500.0)

    def test_entry_failures_and_path_censoring_are_explicit(self):
        cases = {
            "entry_bar_missing": [_bar(1, 100, 101, 99, 100)],
            "duplicate_entry_bar": [
                _bar(0, 100, 101, 99, 100),
                _bar(0, 100, 101, 99, 100),
            ],
            "malformed_entry_bar": [
                {**_bar(0, 100, 101, 99, 100), "low": None},
            ],
        }
        for reason, rows in cases.items():
            with self.subTest(reason=reason):
                result = compute_pre_admission_path_telemetry(_plan(), rows)
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["unavailable_reason"], reason)

        partial = compute_pre_admission_path_telemetry(
            _plan(entry_timestamp=(BASE + timedelta(seconds=30)).isoformat()),
            [_bar(0, 100, 101, 99, 100)])
        self.assertEqual(partial["status"], "censored")
        self.assertEqual(partial["censor_reason"], "partial_entry_bar")

        ended = compute_pre_admission_path_telemetry(
            _plan(), [_bar(0, 100, 101, 99, 100)])
        self.assertEqual(ended["status"], "censored")
        self.assertEqual(ended["censor_reason"], "observed_data_end")

        gapped = compute_pre_admission_path_telemetry(_plan(), [
            _bar(0, 100, 101, 99, 100),
            _bar(2, 100, 101, 99, 100),
        ])
        self.assertEqual(gapped["status"], "censored")
        self.assertEqual(gapped["censor_reason"], "internal_gap")

    def test_same_bar_tie_is_stop_first_but_ambiguous(self):
        result = compute_pre_admission_path_telemetry(_plan(), [
            _bar(0, 100, 105, 97, 100),
            _bar(1, 100, 101, 99, 100),
            _bar(2, 100, 101, 99, 100),
        ])
        self.assertEqual(result["fixed_barrier_first_reached"], "stop")
        self.assertTrue(result["fixed_barrier_same_bar_ambiguous"])
        self.assertFalse(result["unambiguous_usable"])
        self.assertEqual(result["fixed_barrier_model"],
                         "fixed_authored_tick_normalized")

    def test_duplicate_timestamp_group_is_never_partially_evaluated(self):
        entry = _bar(0, 100, 101, 99, 100)
        target_only = _bar(1, 100, 105, 99, 104)
        stop_only = _bar(1, 100, 101, 97, 98)
        outcomes = []
        for rows in ([entry, target_only, stop_only],
                     [entry, stop_only, target_only]):
            result = compute_pre_admission_path_telemetry(_plan(), rows)
            summary = aggregate_pre_admission_path_telemetry([result])
            self.assertEqual(result["status"], "censored")
            self.assertEqual(result["censor_reason"], "duplicate_bar")
            self.assertIsNone(result["fixed_barrier_first_reached"])
            self.assertEqual(summary["fixed_barrier_first_reached"],
                             {"unknown": 1})
            self.assertEqual(summary["mfe_bps"]["count"], 0)
            outcomes.append(result)
        self.assertEqual(outcomes[0], outcomes[1])

        earlier_target = _bar(0, 100, 105, 99, 104)
        preserved = compute_pre_admission_path_telemetry(
            _plan(), [earlier_target, target_only, stop_only])
        self.assertEqual(preserved["censor_reason"], "duplicate_bar")
        self.assertEqual(preserved["fixed_barrier_first_reached"], "target")
        self.assertEqual(
            aggregate_pre_admission_path_telemetry([preserved])
            ["fixed_barrier_first_reached"], {"target": 1})

    def test_nonpositive_ohlc_is_malformed_at_entry_or_later(self):
        normalized = _bars(RISING + FLAT)
        entry_cases = [
            _bar(0, 0, 0, -1, 0),
            _bar(0, -2, -1, -3, -2),
            replace(normalized[0], open=0, high=0, low=-1, close=0),
        ]
        for row in entry_cases:
            with self.subTest(entry=row):
                result = compute_pre_admission_path_telemetry(_plan(), [row])
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["unavailable_reason"],
                                 "malformed_entry_bar")

        later_cases = [
            _bar(1, 0, 0, -1, 0),
            _bar(1, -2, -1, -3, -2),
            replace(normalized[1], open=0, high=0, low=-1, close=0),
        ]
        for row in later_cases:
            with self.subTest(later=row):
                result = compute_pre_admission_path_telemetry(
                    _plan(exit_deadline_timestamp=(
                        BASE + timedelta(minutes=2)).isoformat()),
                    [normalized[0], row])
                self.assertEqual(result["status"], "censored")
                self.assertEqual(result["censor_reason"], "malformed_bar")
                self.assertEqual(result["observed_bars"], 1)
                self.assertIsNone(result["fixed_barrier_first_reached"])

    def test_aggregate_keeps_unambiguous_use_and_cost_geometry(self):
        usable = compute_pre_admission_path_telemetry(_plan(), [
            _bar(0, 100, 101, 99, 100), _bar(1, 100, 104, 99, 103),
            _bar(2, 103, 104, 102, 103),
        ])
        tie = compute_pre_admission_path_telemetry(_plan(), [
            _bar(0, 100, 105, 97, 100), _bar(1, 100, 101, 99, 100),
            _bar(2, 100, 101, 99, 100),
        ])
        censored = compute_pre_admission_path_telemetry(
            _plan(), [_bar(0, 100, 101, 99, 100)])
        summary = aggregate_pre_admission_path_telemetry(
            [usable, tie, censored])
        self.assertEqual(summary["plan_count"], 3)
        self.assertEqual(summary["status_counts"], {
            "usable": 2, "censored": 1, "unavailable": 0})
        self.assertEqual(summary["unambiguous_usable"], 1)
        self.assertEqual(summary["same_bar_ambiguous"], 1)
        self.assertEqual(summary["metric_scope"],
                         "uncensored_full_horizon_only")
        self.assertEqual(summary["mfe_bps"]["count"], 2)
        self.assertEqual(summary["fixed_barrier_first_reached"], {
            "stop": 1, "target": 1, "unknown": 1})
        self.assertEqual(summary["cost_geometry"]
                         ["required_tick_rounded_stop_distance_bps"]["count"], 3)

        prefix_hit = compute_pre_admission_path_telemetry(_plan(), [
            _bar(0, 100, 105, 99, 104),
        ])
        prefix_summary = aggregate_pre_admission_path_telemetry([prefix_hit])
        self.assertEqual(prefix_hit["status"], "censored")
        self.assertEqual(prefix_summary["fixed_barrier_first_reached"],
                         {"target": 1})
        self.assertEqual(prefix_summary["mfe_bps"]["count"], 0)

    def test_factory_attaches_static_plan_without_changing_rejection(self):
        session_open = BASE
        # Exact-calendar replay resolves the shipped ten-minute safety offset
        # from this early close, yielding a 09:36 ET force-flat boundary.
        session_close = BASE + timedelta(minutes=16)
        bars = [replace(row, session_open=session_open,
                        session_close=session_close)
                for row in _bars(RISING + FLAT)]
        quotes = [_quote(index, float(row.open), float(row.open) + .02)
                  for index, row in enumerate(bars)]
        policy = ReplayPolicy(
            strict_market_data=True, force_flat_time=time(9, 36),
            stressed_cost_scenario_bps=25.0,
            max_stressed_cost_to_risk_ratio=.30)
        floor_spec = validate_rule_spec({**SPEC, "stop_atr": 1.0})
        baseline = simulate_account(
            bars, [], floor_spec, vehicle="equity", account_id="plan",
            quotes=quotes, policy=policy)
        row = baseline["rows"][0]
        self.assertTrue(row["no_trade"])
        self.assertEqual(row["reject_reason"], "stressed_cost_risk_limit")
        self.assertEqual((row["net_pnl"], row["return_value"]), (0.0, 0.0))
        plan = row["pre_admission_plan"]
        self.assertEqual(plan["entry_timestamp"],
                         (BASE + timedelta(minutes=4)).isoformat())
        self.assertNotEqual(plan["entry_reference"],
                            plan["authored_entry_reference"])
        self.assertEqual(plan["entry_source"], "quote")
        self.assertEqual(plan["entry_provenance"]["feed"], "sip")
        self.assertEqual(plan["exit_deadline_reason"], "session_force_flat")
        self.assertEqual(plan["exit_deadline_timestamp"],
                         (BASE + timedelta(minutes=6)).isoformat())
        self.assertGreater(plan["required_tick_rounded_stop_distance"],
                           plan["tick_normalized_stop_distance"])

        # Bars after the rejected boundary are not part of static plan creation.
        changed = list(bars)
        changed[-1] = replace(changed[-1], open=900, high=999, low=1, close=500)
        altered = simulate_account(
            changed, [], floor_spec, vehicle="equity", account_id="plan-2",
            quotes=quotes, policy=policy)["rows"][0]
        self.assertEqual(altered["pre_admission_plan"], plan)

    def test_fit_diagnostics_separate_executed_and_rejected_paths(self):
        bars = _bars(RISING + FLAT)
        executed_path = {
            "available": True, "target_r": 2.0, "max_hold_bars": 3,
            "exit_reason": "target", "right_censored": False,
            "gap_detected": False, "observed_bars": 2,
            "mfe_bps": 100.0, "mae_bps": -20.0,
            "mfe_r": 1.0, "mae_r": -.2,
        }
        rejected = {
            "vehicle": "equity", "no_trade": True,
            "entry_timestamp": BASE.isoformat(),
            "reject_reason": "stressed_cost_risk_limit",
            # This legacy nested value must never leak into executed telemetry.
            "path_telemetry": executed_path,
            "pre_admission_plan": _plan(),
        }
        result = measure_fit_diagnostics(
            bars, SPEC,
            account_rows=[
                {"vehicle": "equity", "no_trade": False,
                 "path_telemetry": executed_path},
                rejected,
            ])
        self.assertEqual(result["path_telemetry"]["trade_count"], 1)
        pre = result["pre_admission_path_telemetry"]
        self.assertEqual(pre["plan_count"], 1)
        self.assertEqual(pre["status_counts"]["usable"], 1)
        self.assertFalse(pre["authorizing"])
        self.assertTrue(pre["diagnostic_only"])
        self.assertEqual(pre["scope"], "fit_only")

        # The diagnostic sees only the caller-provided fit slice; a future,
        # different-session extreme cannot affect its rejected-path aggregate.
        future = replace(bars[-1],
                         timestamp=bars[-1].timestamp + timedelta(days=1),
                         identity=replace(
                             bars[-1].identity,
                             as_of=bars[-1].identity.as_of + timedelta(days=1),
                             observed_at=(bars[-1].identity.observed_at +
                                          timedelta(days=1)),
                             session_date=(bars[-1].session_date +
                                           timedelta(days=1))),
                         open=1, high=10_000, low=.01, close=9_000)
        isolated = measure_fit_diagnostics(
            bars, SPEC, account_rows=[rejected])
        still_isolated = measure_fit_diagnostics(
            [row for row in [*bars, future]
             if row.session_date == bars[0].session_date],
            SPEC, account_rows=[rejected])
        self.assertEqual(isolated["pre_admission_path_telemetry"],
                         still_isolated["pre_admission_path_telemetry"])

    def test_fit_diagnostics_indexes_each_plan_to_its_local_partition(self):
        bars = _bars(RISING + FLAT)
        other_symbol = [replace(row, symbol="QQQ") for row in bars]
        future_day = [replace(
            row, timestamp=row.timestamp + timedelta(days=1),
            identity=replace(
                row.identity, as_of=row.identity.as_of + timedelta(days=1),
                observed_at=row.identity.observed_at + timedelta(days=1),
                session_date=row.session_date + timedelta(days=1)))
            for row in bars]
        rejected = {"vehicle": "equity", "no_trade": True,
                    "pre_admission_plan": _plan()}
        direct = aggregate_pre_admission_path_telemetry([
            compute_pre_admission_path_telemetry(
                rejected["pre_admission_plan"],
                [*bars, *other_symbol, *future_day])])
        with patch.object(
                fit_diagnostics_module,
                "compute_pre_admission_path_telemetry",
                wraps=compute_pre_admission_path_telemetry) as measured:
            indexed = measure_fit_diagnostics(
                [*bars, *other_symbol, *future_day], SPEC,
                account_rows=[rejected])["pre_admission_path_telemetry"]
        self.assertEqual(indexed, direct)
        self.assertEqual(len(measured.call_args.args[1]), len(bars))


if __name__ == "__main__":
    unittest.main()
