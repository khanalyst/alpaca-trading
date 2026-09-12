"""Independent evidence-contract checks for the offline IBR adapter."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import math
import socket
import sqlite3
import unittest
from unittest.mock import patch

from research.diagnostic_shadow import build_diagnostic_cohort
from research.ibr_diagnostic import run_offline_forward_ibr
from research.source_validation import source_content_hash
from tests.research.test_ibr_diagnostic_adapter import (
    CLOSE,
    OPEN,
    _bar,
    _complete_source,
    _quote,
    _runtime,
    _source,
)


class OfflineIbrDiagnosticEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = _runtime()
        cohort = build_diagnostic_cohort(
            self.runtime, code_identity="e" * 64, include_ibr=True)
        self.arms = [deepcopy(arm) for arm in cohort["arms"]
                     if arm["strategy_id"] == "ibr"]
        self.assertEqual(7, len(self.arms))

    def _run(self, rows: list[dict], **kwargs):
        return run_offline_forward_ibr(
            rows, runtime_config=self.runtime, arms=self.arms, **kwargs)

    def _assert_unavailable(self, report: dict, reason: str) -> None:
        self.assertEqual("unavailable", report["status"], report)
        self.assertIn(reason, report["reason_codes"], report)
        self.assertIsNone(report["diagnostic"])
        self.assertFalse(report["authorizing"])
        self.assertFalse(report["gate_eligible"])
        self.assertFalse(report["promotion_eligible"])
        self.assertEqual([], report["eligible"])
        self.assertEqual([], report["proofs"])
        for arm in report["arms"].values():
            self.assertEqual("unavailable", arm["status"])
            self.assertEqual("unavailable", arm["outcome"])
            self.assertEqual([], arm["rows"])
            self.assertIsNone(arm["diagnostic"])
            self.assertIsNone(arm["account"])
            self.assertIsNone(arm["decision_summary"])

    @staticmethod
    def _matching_but_misleading_report(rows: list[dict]) -> dict:
        return {
            "content_hash": source_content_hash(rows),
            "rows": len(rows),
            "source_mode_counts": {"forward_observed": len(rows)},
            "providers": ["alpaca"],
            "feeds": ["iex"],
            "errors": [],
            "authorizing": True,
            "diagnostic_only": False,
        }

    def test_raw_provenance_calendar_and_quotes_fail_closed_despite_report(self):
        historical = deepcopy(_source())
        historical[0]["source_mode"] = "historical_backfill"
        mixed = deepcopy(_source())
        mixed[-1]["source_mode"] = "historical_backfill"
        no_quotes = [row for row in _source() if row["kind"] != "quote"]
        missing_calendar = deepcopy(_source())
        for row in missing_calendar:
            row.pop("session_open")
            row.pop("session_close")
        conflicting_calendar = deepcopy(_source())
        conflicting_calendar[-1]["session_close"] = (
            CLOSE - timedelta(minutes=30)).isoformat()

        cases = (
            ("historical_source", historical),
            ("mixed_source_modes", mixed),
            ("missing_quotes", no_quotes),
            ("calendar_missing", missing_calendar),
            ("calendar_conflict", conflicting_calendar),
        )
        for reason, rows in cases:
            with self.subTest(reason=reason):
                report = self._run(
                    rows,
                    source_report=self._matching_but_misleading_report(rows))
                self._assert_unavailable(report, reason)

    def test_delayed_quote_and_market_events_are_only_used_when_observed(self):
        rows = _source()
        quote = next(row for row in rows if row["kind"] == "quote")
        quote_observed = OPEN + timedelta(minutes=16, seconds=20)
        quote.update({
            "timestamp": (OPEN + timedelta(minutes=16, seconds=10)).isoformat(),
            "as_of": (OPEN + timedelta(minutes=16, seconds=10)).isoformat(),
            "observed_at": quote_observed.isoformat(),
        })
        second_signal = next(
            row for row in rows
            if row["kind"] == "bar" and
            row["timestamp"] == (OPEN + timedelta(minutes=16)).isoformat())
        second_observed = OPEN + timedelta(minutes=16, seconds=25)
        second_signal["as_of"] = second_observed.isoformat()
        second_signal["observed_at"] = second_observed.isoformat()
        target = next(
            row for row in rows
            if row["kind"] == "bar" and
            row["timestamp"] == (OPEN + timedelta(minutes=17)).isoformat())
        target_observed = OPEN + timedelta(minutes=19, seconds=30)
        target["observed_at"] = target_observed.isoformat()
        earlier_observed_stop = _bar(
            18, opened=101.0, high=101.1, low=98.0, close=99.0,
            volume=1200.0)
        rows.append(earlier_observed_stop)
        rows.append(_quote(OPEN + timedelta(minutes=18, seconds=50)))

        report = self._run(rows)
        baseline = report["arms"]["ibr.baseline"]
        self.assertEqual("measured", report["status"], report)
        self.assertEqual(1, len(baseline["rows"]), baseline)
        terminal = baseline["rows"][0]
        self.assertFalse(terminal["no_trade"], baseline)
        self.assertEqual(second_observed.isoformat(), terminal["entry_timestamp"])
        self.assertGreaterEqual(
            datetime.fromisoformat(terminal["entry_timestamp"]), quote_observed)
        self.assertEqual("stop", terminal["canonical_exit_reason"])
        self.assertEqual(
            earlier_observed_stop["as_of"], terminal["exit_timestamp"])
        self.assertLess(
            datetime.fromisoformat(terminal["exit_timestamp"]),
            target_observed)

    def test_incomplete_and_open_positions_remain_missing_and_null(self):
        report = self._run(_source())

        open_arm = report["arms"]["ibr.target.3r"]
        self.assertEqual("missing_data", open_arm["outcome"], open_arm)
        self.assertEqual(1, open_arm["account"]["open_positions"])
        self.assertEqual(0, open_arm["account"]["closed_positions"])
        self.assertIsNone(open_arm["account"]["equity"])
        self.assertIsNone(open_arm["account"]["unrealized_pnl"])
        self.assertEqual(1, len(open_arm["rows"]))
        terminal = open_arm["rows"][0]
        self.assertTrue(terminal["no_trade"])
        self.assertEqual(
            "open_or_unpriced_position_at_replay_boundary",
            terminal["reject_reason"])
        self.assertIsNone(terminal["gross_pnl"])
        self.assertIsNone(terminal["net_pnl"])
        self.assertIsNone(terminal["return_value"])

        incomplete = report["arms"]["ibr.range.45"]
        self.assertEqual("missing_data", incomplete["outcome"], incomplete)
        self.assertNotEqual(
            "no_signal", incomplete["rows"][0]["execution_disposition"])
        self.assertIsNone(incomplete["rows"][0]["net_pnl"])

    def test_sparse_or_other_symbol_close_cannot_prove_no_signal(self):
        sparse = _complete_source()

        cross_symbol = _source()
        cross_symbol.append(_quote(OPEN + timedelta(minutes=17, seconds=50)))
        qqq_close = _bar(389)
        qqq_close["symbol"] = "QQQ"
        cross_symbol.append(qqq_close)
        qqq_quote = _quote(CLOSE)
        qqq_quote["symbol"] = "QQQ"
        cross_symbol.append(qqq_quote)

        for label, rows in (("sparse", sparse),
                            ("other_symbol", cross_symbol)):
            with self.subTest(label=label):
                report = self._run(rows)
                arm = report["arms"]["ibr.range.45"]
                spy = next(row for row in arm["rows"]
                           if row["symbol"] == "SPY")
                self.assertEqual("missing_data", arm["outcome"], arm)
                self.assertEqual("refused", spy["execution_disposition"])
                self.assertNotEqual("no_signal", spy["reject_reason"])
                self.assertIsNone(spy["net_pnl"])
                self.assertIn(
                    "insufficient bars",
                    arm["decision_summary"]["by_reason"])

    def test_continuous_priced_complete_session_can_report_no_signal(self):
        close = OPEN + timedelta(minutes=60)
        rows: list[dict] = []
        for index in range(60):
            bar = _bar(index)
            bar["session_close"] = close.isoformat()
            rows.append(bar)
            quote = _quote(datetime.fromisoformat(bar["observed_at"]))
            quote["session_close"] = close.isoformat()
            rows.append(quote)

        report = self._run(rows)
        arm = report["arms"]["ibr.range.45"]
        self.assertEqual("measured", report["status"], report)
        self.assertEqual("no_signal", arm["outcome"], arm)
        self.assertEqual(1, len(arm["rows"]))
        self.assertEqual("no_signal", arm["rows"][0]["execution_disposition"])
        self.assertNotIn("unpriced", arm["decision_summary"]["by_kind"])
        self.assertIsNone(arm["rows"][0]["net_pnl"])

    def test_deterministic_isolated_reconciliation_without_forbidden_io(self):
        rows = _source()
        frozen_rows = deepcopy(rows)
        frozen_arms = deepcopy(self.arms)
        forbidden = AssertionError("offline diagnostic attempted forbidden I/O")
        with patch("research.live_shadow.ShadowStore", side_effect=forbidden) as store, \
                patch("research.live_shadow._read_factory_rule_roots",
                      side_effect=forbidden) as roots, \
                patch("research.edge_ledger.EdgeLedger",
                      side_effect=forbidden) as ledger, \
                patch("agent.alpaca_provider.AlpacaProvider",
                      side_effect=forbidden) as broker, \
                patch.object(sqlite3, "connect", side_effect=forbidden) as connect, \
                patch.object(socket, "create_connection",
                             side_effect=forbidden) as network:
            first = self._run(rows)
            second = self._run(rows)

        self.assertEqual(first, second)
        self.assertEqual(frozen_rows, rows)
        self.assertEqual(frozen_arms, self.arms)
        store.assert_not_called()
        roots.assert_not_called()
        ledger.assert_not_called()
        broker.assert_not_called()
        connect.assert_not_called()
        network.assert_not_called()
        self.assertEqual(source_content_hash(rows), first["source"]["content_hash"])
        self.assertFalse(first["authorizing"])
        self.assertFalse(first["replay_scope"]["authorizing"])
        self.assertFalse(first["replay_scope"]["broker_equivalence"])
        self.assertEqual([], first["eligible"])
        self.assertEqual([], first["proofs"])

        for arm in first["arms"].values():
            keys = [(row["symbol"], row["session_date"])
                    for row in arm["rows"]]
            self.assertEqual(len(keys), len(set(keys)), arm)
            self.assertEqual(100_000.0, arm["account"]["starting_cash"])
            closed_net = sum(float(row["net_pnl"])
                             for row in arm["rows"] if not row["no_trade"])
            self.assertTrue(math.isclose(
                closed_net, arm["account"]["realized_pnl"],
                rel_tol=1e-9, abs_tol=1e-8), arm)
            self.assertTrue(math.isclose(
                arm["account"]["cash"],
                arm["account"]["starting_cash"] +
                arm["account"]["realized_pnl"],
                rel_tol=1e-9, abs_tol=1e-8), arm)
            self.assertFalse(arm["authorizing"])
            self.assertFalse(arm["eligible"])
            self.assertFalse(arm["promotion_eligible"])
            self.assertEqual([], arm["proofs"])
            for row in arm["rows"]:
                self.assertFalse(row["authorizing"])
                self.assertFalse(row["directional_authorizing"])
                self.assertFalse(row["actual_fill"])
                if not row["no_trade"]:
                    self.assertTrue(math.isclose(
                        row["gross_pnl"] - row["fees_after_fill_prices"],
                        row["net_pnl"], rel_tol=1e-9, abs_tol=1e-8), row)


if __name__ == "__main__":
    unittest.main()
