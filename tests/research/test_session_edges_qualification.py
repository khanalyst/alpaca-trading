"""Regression checks for the study/main authority boundary and research history."""

from __future__ import annotations

import copy
import json
import math
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from studies.session_edges import discover, grammar, stats
from studies.session_edges.data import DailyBar
from studies.session_edges.strategies import DayResult


ROOT = Path(__file__).resolve().parents[2]


def corpus(count: int = 3600) -> tuple[list[DailyBar], list[DayResult]]:
    price = 100.0
    bars, results = [], []
    for index in range(count):
        day = date(2012, 1, 2) + timedelta(days=index)
        price *= 1.0 + (0.002 if index % 2 else -0.001)
        bars.append(DailyBar(day, price, price * 1.02, price * 0.98, price))
        results.append(DayResult(day, (0.0015, 0.0005, 0.001)[index % 3], 1))
    return bars, results


class HistoricalQualificationTests(unittest.TestCase):
    def test_reported_positive_returns_do_not_pass_corrected_alpha(self):
        path = ROOT / "studies/session_edges/results/discovery-public-2026-11-12/discovery.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        rows = original["promoted"]
        self.assertEqual(len(rows), 124)
        original_holm = discover.holm({key: row["holdout"]["p_one_sided"] for key, row in rows.items()})
        self.assertEqual(len(original_holm), 9)
        tested, holm, by = discover.historical_corrections(rows)
        self.assertEqual(len(tested), 124)
        self.assertEqual(holm, set())
        self.assertEqual(by, set())
        # This saved-artifact sensitivity check is not a new causal backtest.
        self.assertEqual(original["schema"], discover.LEGACY_SCHEMA)

    def test_missing_and_underpowered_alpha_remain_in_denominator(self):
        rows = {"good": {"holdout": {"sessions": 1000, "t": 3.0, "alpha_t": 3.0}},
                "short": {"holdout": {"sessions": 12, "t": 20.0, "alpha_t": 20.0}},
                "beta_only": {"holdout": {"sessions": 1000, "t": 20.0}}}
        tested, _, _ = discover.historical_corrections(rows, prior_trials=7)
        self.assertEqual(len(tested), 10)
        self.assertEqual(tested["short"], 1.0)
        self.assertEqual(tested["beta_only"], 1.0)
        self.assertGreater(tested["good"], 0)

    def test_hac_alpha_handles_fitted_exposure_and_degenerate_evidence(self):
        market = [0.03 if index % 2 else -0.01 for index in range(300)]
        net = [0.5 * value + (0.002, 0.0, 0.001)[index % 3] for index, value in enumerate(market)]
        alpha = stats.regression_alpha(net, market)
        self.assertAlmostEqual(alpha["beta"], 0.5)
        self.assertAlmostEqual(alpha["alpha_bps_per_session"], 10.0)
        self.assertGreater(alpha["alpha_t"], 10)
        pure_beta = stats.regression_alpha(market, market)
        self.assertAlmostEqual(pure_beta["alpha_bps_per_session"], 0.0)
        self.assertIsNone(pure_beta["alpha_t"])
        self.assertEqual(pure_beta["alpha_p_one_sided"], 1.0)
        self.assertEqual(stats.regression_alpha(net, [0.01] * 300)["alpha_p_one_sided"], 1.0)
        for invalid in (float("nan"), float("inf"), -0.1, 1.1):
            with self.assertRaises(ValueError):
                discover.holm({"invalid": invalid})

    def test_open_swing_position_pays_executed_entry_side(self):
        entry = DayResult(date(2026, 1, 2), 0.01, 0, cost_round_trips=0.5)
        exit = DayResult(date(2026, 1, 5), -0.02, 1, cost_round_trips=0.5)
        self.assertAlmostEqual(stats.net_series([entry], 4.0)[0], 0.0098)
        self.assertAlmostEqual(sum(stats.net_series([entry, exit], 4.0)), -0.0104)
        for invalid in (float("nan"), -1.0):
            with self.assertRaises(ValueError):
                stats.net_series([entry], invalid)

    def test_supported_daily_or_proxy_candidates_never_gain_order_authority(self):
        bars, results = corpus(5400)  # includes the explicitly required 2024+ diagnostic window
        with patch.object(grammar, "simulator", return_value=lambda _: results):
            record = discover.discover(minute={}, daily={"SYNTHETIC": bars},
                                       families=["close_weakness_reversion_daily"])
        self.assertTrue(record["promoted"])
        self.assertTrue(any(row["tier"] == discover.CONFIRMED for row in record["promoted"].values()))
        registered = {"schema": "session-edges-study.v1", "minute": {"QQQ (index proxy)": {
            "noise_band_momentum": {"verdict": "paper_candidate", "evidence": {"holdout": {}}}}}}
        book = discover.build_book(record, registered)
        self.assertFalse(book["authorizing"])
        self.assertTrue(book["requires_main_qualification"])
        self.assertTrue(book["candidates"])
        self.assertFalse(any(leg["trade"] for leg in book["legs"]))
        self.assertTrue(all(leg["weight"] == 0 for leg in book["legs"]))
        self.assertFalse(any(leg["rule"] == "close_weakness_reversion" for leg in book["legs"]))
        self.assertIn("legacy", book["legs"][0]["note"])
        with self.assertRaises(discover.LedgerError):
            discover.build_book({"schema": discover.LEGACY_SCHEMA, "authorizing": False})


class ImmutableHistoryTests(unittest.TestCase):
    def test_missing_incompatible_and_corrupt_ledgers_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "global.json"
            with self.assertRaises(discover.LedgerError):
                discover.load_ledger(path)
            ledger = discover.load_ledger(path, create=True)
            discover.write_ledger(path, ledger)
            self.assertEqual(discover.load_ledger(path), ledger)
            with self.assertRaises(discover.LedgerError):
                discover.load_ledger(path, create=True)
            path.write_text(json.dumps({"schema": discover.LEGACY_SCHEMA, "promoted": {}}))
            with self.assertRaises(discover.LedgerError):
                discover.load_ledger(path)
            corrupt = copy.deepcopy(ledger)
            corrupt["holdout_start"] = "2021-01-01"
            path.write_text(json.dumps(corrupt))
            with self.assertRaises(discover.LedgerError):
                discover.load_ledger(path)

    def test_experiment_inputs_code_and_costs_are_immutable_and_prior_trials_count(self):
        bars, results = corpus()
        ledger = discover.new_ledger()
        with patch.object(grammar, "simulator", return_value=lambda _: results):
            first = discover.discover(minute={}, daily={"TEST": bars}, ledger=ledger,
                                      families=["close_weakness_reversion_daily"], experiment_name="frozen")
            self.assertEqual(first["promoted_ever"], 1)
            before = copy.deepcopy(ledger)
            changed = [replace(bars[0], close=bars[0].close * 1.001), *bars[1:]]
            for changed_bars, costs in ((changed, None), (bars, {"TEST": 8.0})):
                with self.assertRaises(discover.LedgerError):
                    discover.discover(minute={}, daily={"TEST": changed_bars}, ledger=ledger,
                                      families=["close_weakness_reversion_daily"], costs=costs,
                                      experiment_name="frozen")
                self.assertEqual(ledger, before)
            with self.assertRaises(discover.LedgerError):
                discover.discover(minute={}, daily={"TEST": changed}, ledger=ledger,
                                  families=["close_weakness_reversion_daily"], experiment_name="replacement")
            second = discover.discover(minute={}, daily={"TEST": changed}, ledger=ledger,
                                       families=["close_weakness_reversion_daily"], experiment_name="replacement",
                                       allow_new_experiment=True)
            self.assertEqual(second["promoted_ever"], 2)
            self.assertEqual(second["global_correction"]["trials"], 2)
            self.assertNotEqual(first["experiment_id"], second["experiment_id"])
            self.assertEqual(len(second["promoted"]), 1)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "global.json"
                discover.write_ledger(path, ledger)
                self.assertEqual(discover.load_ledger(path), ledger)
            tampered = copy.deepcopy(ledger)
            next(iter(tampered["promoted"].values()))["holdout"]["alpha_t"] = 100.0
            with self.assertRaises(discover.LedgerError):
                discover.discover(minute={}, daily={"TEST": bars}, ledger=tampered,
                                  families=["close_weakness_reversion_daily"], experiment_name="frozen")

    def test_legacy_migration_preserves_artifact_without_upgrading_evidence(self):
        payload = {"schema": discover.LEGACY_SCHEMA, "holdout_start": "2020-01-01", "promoted": {
            "old": {"tier": "confirmed", "holdout": {"sessions": 1000, "t": 100, "alpha_t": 100}}}}
        original = copy.deepcopy(payload)
        ledger = discover.migrate_legacy_ledger(payload)
        self.assertEqual(payload, original)
        self.assertEqual(ledger["promoted"], {})
        historical = next(iter(ledger["historical"].values()))
        self.assertEqual(historical["artifact"], payload)
        self.assertEqual(historical["trials"], 1)
        self.assertFalse(historical["authorizing"])
        bars, results = corpus()
        with patch.object(grammar, "simulator", return_value=lambda _: results):
            record = discover.discover(minute={}, daily={"TEST": bars}, ledger=ledger,
                                       families=["close_weakness_reversion_daily"])
        self.assertEqual(record["historical_trials"], 1)
        self.assertEqual(record["global_correction"]["trials"], 2)
        self.assertNotIn("old", record["promoted"])

    def test_source_declaration_and_code_digest_change_experiment_identity(self):
        bars, _ = corpus(5)
        datasets = {(grammar.DAILY, "TEST"): bars}
        first = discover.experiment_manifest(datasets, context={}, costs=None,
                                            source_provenance={"feed": "public"})
        second = discover.experiment_manifest(datasets, context={}, costs=None,
                                             source_provenance={"feed": "sip"})
        self.assertNotEqual(discover._digest(first), discover._digest(second))
        changed = copy.deepcopy(first)
        changed["code_digest"] = "0" * 64
        ledger = discover.new_ledger()
        discover._bind_experiment(ledger, "frozen", first, allow_new=False)
        with self.assertRaises(discover.LedgerError):
            discover._bind_experiment(ledger, "frozen", changed, allow_new=False)


if __name__ == "__main__":
    unittest.main()
