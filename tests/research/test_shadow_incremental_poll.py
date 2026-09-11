"""Focused regressions for incremental authorizing shadow polls."""

from __future__ import annotations

import csv
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy.recorder_market import _event_key
from research.edge_ledger import EdgeLedger
from research.live_shadow import ShadowConfig, ShadowRunner


SESSION = "2026-01-02"
FIELDS = [
    "event_key", "event_type", "symbol", "timestamp", "as_of",
    "observed_at", "provider", "feed", "open", "high", "low",
    "close", "volume", "bid", "ask",
]


class ShadowIncrementalPollTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.corpus = self.root / "recorded.csv"
        self.edge = self.root / "edge.sqlite3"
        self.shadow = self.root / "shadow.sqlite3"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _candidate(self, variant: str = "ibr.baseline") -> dict:
        candidate = EdgeLedger(self.edge).register_candidate(
            variant, strategy_id="ibr", vehicle="equity", hypothesis=variant,
            config={
                "strategy": {"id": "ibr", "version": "v1",
                             "variant_id": variant},
                "risk": {"risk_per_trade_pct": 1},
                "execution": {}, "session": {},
            })
        with closing(sqlite3.connect(self.edge)) as db, db:
            db.execute(
                "UPDATE candidate_state SET status='backtest_passed' "
                "WHERE candidate_id=?", (candidate["candidate_id"],))
        return candidate

    @staticmethod
    def _bar(timestamp: str, as_of: str) -> dict[str, str]:
        return {
            "event_key": _event_key("bar_1m", "SPY", timestamp),
            "event_type": "bar_1m", "symbol": "SPY",
            "timestamp": timestamp, "as_of": as_of,
            "observed_at": as_of, "provider": "alpaca", "feed": "iex",
            "open": "100", "high": "101", "low": "99", "close": "100",
            "volume": "1000", "bid": "", "ask": "",
        }

    def _write_rows(self, rows: list[dict[str, str]]) -> None:
        with self.corpus.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def _append_row(self, row: dict[str, str]) -> None:
        with self.corpus.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=FIELDS).writerow(row)

    def _runner(self) -> ShadowRunner:
        return ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, max_events=100,
            max_decisions=100, max_workers=2))

    @staticmethod
    def _no_trade(candidate, event, bars, quotes, options):
        del bars, quotes, options
        session = (datetime.fromisoformat(str(event["as_of"]))
                   .astimezone(timezone.utc).date().isoformat())
        return ("no_trade", "no signal", {
            "session_date": session,
            "strategy_id": candidate.get("strategy_id"),
            "variant_id": candidate.get("variant_id"),
        }, None)

    @staticmethod
    def _open(candidate, event, bars, quotes, options):
        del candidate, bars, quotes, options
        signal_ts = datetime.fromisoformat(str(event["timestamp"])).timestamp()
        setup = {
            "symbol": "SPY", "direction": "long",
            "setup_type": "ibr_breakout", "signal_ts": signal_ts,
            "stop_price": 99, "target_price": 102, "stop_distance": 1,
            "target_r": 2, "execution_profile": "shares",
        }
        payload = {
            "equity_feed": "iex", "equity_provider": "alpaca",
            "signal": dict(setup), "setup_plan": dict(setup),
            "risk_plan": dict(setup),
        }
        plan = {**setup, "entry_price": 100, "shares": 1,
                "risk_usd": 1, "notional": 100}
        return "open_incomplete", "changed code", payload, plan

    def test_unchanged_incomplete_poll_skips_evaluation_and_replay(self):
        candidate = self._candidate()
        opening = self._bar(
            "2026-01-02T15:00:00+00:00",
            "2026-01-02T15:01:00+00:00")
        self._write_rows([opening])
        runner = self._runner()

        with patch.object(runner, "_evaluate", side_effect=self._no_trade) as evaluate, \
                patch.object(runner, "_replay", wraps=runner._replay) as replay:
            first = runner.run_once()
            self.assertEqual(evaluate.call_count, 1)
            replay.assert_not_called()
            evaluate.reset_mock()
            replay.reset_mock()

            second = runner.run_once()

        evaluate.assert_not_called()
        replay.assert_not_called()
        self.assertEqual(second["ingested_events"], 0)
        self.assertEqual(second["manifest_digest"], first["manifest_digest"])
        self.assertEqual(second["forward_event_floor"],
                         first["forward_event_floor"])
        self.assertEqual(len(runner.store.decisions(candidate["candidate_id"])), 1)

    def test_one_new_incomplete_event_is_evaluated_once_per_arm(self):
        first = self._candidate("ibr.baseline")
        second = self._candidate("ibr.range.30")
        opening = self._bar(
            "2026-01-02T15:00:00+00:00",
            "2026-01-02T15:01:00+00:00")
        update = self._bar(
            "2026-01-02T15:01:00+00:00",
            "2026-01-02T15:02:00+00:00")
        self._write_rows([opening])
        runner = self._runner()

        with patch.object(runner, "_evaluate", side_effect=self._no_trade):
            runner.run_once()
        self._append_row(update)
        with patch.object(runner, "_evaluate", side_effect=self._no_trade) as evaluate, \
                patch.object(runner, "_replay", wraps=runner._replay) as replay:
            result = runner.run_once()

        calls = [(str(call.args[0]["candidate_id"]),
                  str(call.args[1]["event_key"]))
                 for call in evaluate.call_args_list]
        self.assertCountEqual(calls, [
            (first["candidate_id"], update["event_key"]),
            (second["candidate_id"], update["event_key"]),
        ])
        replay.assert_not_called()
        self.assertEqual(result["ingested_events"], 1)

    def test_closing_event_replays_all_durable_decisions_and_advances_floor(self):
        candidate = self._candidate()
        opening = self._bar(
            "2026-01-02T15:00:00+00:00",
            "2026-01-02T15:01:00+00:00")
        closing = self._bar(
            "2026-01-02T20:59:00+00:00",
            "2026-01-02T21:00:00+00:00")
        self._write_rows([opening])
        runner = self._runner()
        with patch.object(runner, "_evaluate", side_effect=self._no_trade):
            first = runner.run_once()

        self._append_row(closing)
        with patch.object(runner, "_evaluate", side_effect=self._no_trade) as evaluate, \
                patch.object(runner, "_replay", return_value=True) as replay:
            second = runner.run_once()

        self.assertEqual(evaluate.call_count, 1)
        replay.assert_called_once()
        replay_rows = replay.call_args.args[4]
        self.assertEqual(
            {str(row["event_key"]) for row in replay_rows},
            {opening["event_key"], closing["event_key"]})
        self.assertGreater(second["forward_event_floor"],
                           first["forward_event_floor"])
        self.assertEqual(len(runner.store.decisions(candidate["candidate_id"])), 2)

    def test_replay_only_session_fully_reevaluates_and_quarantines_mismatch(self):
        candidate = self._candidate()
        closing = self._bar(
            "2026-01-02T20:59:00+00:00",
            "2026-01-02T21:00:00+00:00")
        self._write_rows([closing])
        runner = self._runner()
        store = runner.store
        store.upsert_candidate(candidate)
        store.ingest_event(closing, max_events=100)
        store.save_source_offsets({str(self.corpus.resolve()): self.corpus.stat().st_size})
        store.save_forward_event_floor(time.time() + 3600)
        store.decision(
            candidate_id=candidate["candidate_id"],
            event_key=closing["event_key"], session_date=SESSION,
            symbol="SPY", kind="no_trade", reason="old code",
            payload={"session_date": SESSION}, max_decisions=100)
        seed_digest = "seed-replay"
        store.replay_diff(
            candidate_id=candidate["candidate_id"], session_date=SESSION,
            source_digest="seed-source", shadow_digest="seed-shadow",
            replay_digest=seed_digest, status="match",
            details={"complete": True, "signature_match": True})
        store.record_replay_evidence(
            candidate_id=candidate["candidate_id"], session_date=SESSION,
            replay_digest=seed_digest, vehicle="equity",
            starting_cash=100_000, ending_cash=100_000, realized_pnl=0,
            trades=[{"symbol": "SPY", "session_date": SESSION}],
            replay_status="match")

        with patch.object(runner, "_evaluate", side_effect=self._open) as evaluate, \
                patch("research.live_shadow.replay_ibr",
                      return_value=SimpleNamespace(trades=[], refusals=[])):
            runner.run_once()

        self.assertEqual(evaluate.call_count, 1)
        metadata = store.replay_metadata(candidate["candidate_id"])[0]
        self.assertEqual(metadata["status"], "mismatch")
        quarantine = store.replay_quarantine()[
            f"{candidate['candidate_id']}:{SESSION}"]
        self.assertEqual(quarantine["status"], "quarantined")
        self.assertEqual(
            store.decisions(candidate["candidate_id"])[0]["kind"], "no_trade")

    def test_failed_arm_retries_without_reevaluating_successful_sibling(self):
        failed = self._candidate("ibr.range.30")
        healthy = self._candidate("ibr.baseline")
        opening = self._bar(
            "2026-01-02T15:00:00+00:00",
            "2026-01-02T15:01:00+00:00")
        self._write_rows([opening])
        runner = self._runner()

        def fail_one(candidate, event, bars, quotes, options):
            if str(candidate["candidate_id"]) == str(failed["candidate_id"]):
                raise RuntimeError("candidate-only failure")
            return self._no_trade(candidate, event, bars, quotes, options)

        with patch.object(runner, "_evaluate", side_effect=fail_one):
            first = runner.run_once()
        self.assertIn(failed["candidate_id"], first["candidate_errors"])
        self.assertEqual(len(runner.store.decisions(failed["candidate_id"])), 0)
        self.assertEqual(len(runner.store.decisions(healthy["candidate_id"])), 1)

        with patch.object(runner, "_evaluate", side_effect=self._no_trade) as evaluate:
            second = runner.run_once()

        self.assertEqual(
            [str(call.args[0]["candidate_id"])
             for call in evaluate.call_args_list],
            [failed["candidate_id"]])
        self.assertNotIn(failed["candidate_id"], second["candidate_errors"])
        self.assertEqual(len(runner.store.decisions(failed["candidate_id"])), 1)
        self.assertEqual(len(runner.store.decisions(healthy["candidate_id"])), 1)

    def test_skip_seen_restores_prior_virtual_open_for_new_event(self):
        candidate = self._candidate()
        opening = self._bar(
            "2026-01-02T15:00:00+00:00",
            "2026-01-02T15:01:00+00:00")
        update = self._bar(
            "2026-01-02T15:01:00+00:00",
            "2026-01-02T15:02:00+00:00")
        self._write_rows([opening])
        runner = self._runner()
        with patch.object(runner, "_evaluate", side_effect=self._open):
            runner.run_once()
        self.assertTrue(runner.store.has_open(candidate["candidate_id"], "SPY"))

        self._append_row(update)
        with patch.object(
                runner, "_evaluate",
                side_effect=AssertionError("persisted open was forgotten")):
            runner.run_once()

        decisions = runner.store.decisions(candidate["candidate_id"])
        self.assertEqual(len(decisions), 2)
        latest = next(row for row in decisions
                      if row["event_key"] == update["event_key"])
        self.assertEqual(latest["kind"], "no_trade")
        self.assertEqual(latest["reason"],
                         "virtual book has an incomplete open")
        self.assertTrue(runner.store.has_open(candidate["candidate_id"], "SPY"))

    def test_warm_restart_uses_persisted_event_identities(self):
        candidate = self._candidate()
        opening = self._bar(
            "2026-01-02T15:00:00+00:00",
            "2026-01-02T15:01:00+00:00")
        self._write_rows([opening])
        first_runner = self._runner()
        with patch.object(first_runner, "_evaluate", side_effect=self._no_trade):
            first = first_runner.run_once()

        restarted = self._runner()
        with patch.object(restarted, "_evaluate", side_effect=self._no_trade) as evaluate, \
                patch.object(restarted, "_replay", wraps=restarted._replay) as replay:
            second = restarted.run_once()

        evaluate.assert_not_called()
        replay.assert_not_called()
        self.assertEqual(first["manifest_digest"], second["manifest_digest"])
        self.assertEqual(first["forward_event_floor"],
                         second["forward_event_floor"])
        self.assertEqual(
            len(restarted.store.decisions(candidate["candidate_id"])), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
