"""Safety and determinism checks for the broker-free live shadow lane."""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy.recorder import INDEX_NAME, iter_corpus_rows
from deploy.recorder_market import _event_key
from agent.contracts.rule import rule_variant_id, validate_rule_spec
from research.edge_ledger import EdgeLedger
from research.factory_core import simulate_account as factory_simulate_account
from research.live_shadow import (InputConflict, ShadowConfig, ShadowRunner,
                                   ShadowError, ShadowStore, _compact_shadow_rows,
                                   _replay_signature,
                                   _shadow_signature, _signature_diffs,
                                   run_shadow_once)


class LiveShadowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.corpus = self.root / "recorded.csv"
        self.edge = self.root / "edge.sqlite3"
        self.shadow = self.root / "shadow.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def _candidate(self, *, variant="ibr.baseline", strategy="ibr", vehicle="equity",
                   status="backtest_passed", config=None):
        ledger = EdgeLedger(self.edge)
        row = ledger.register_candidate(
            variant, strategy_id=strategy, vehicle=vehicle, hypothesis=variant,
            config=config or {"strategy": {"id": strategy, "version": "v1",
                                             "variant_id": variant},
                              "risk": {"risk_per_trade_pct": 1},
                              "execution": {}, "session": {}})
        with sqlite3.connect(self.edge) as db:
            db.execute("UPDATE candidate_state SET status=? WHERE candidate_id=?",
                       (status, row["candidate_id"]))
        return row

    def _write_rows(self, *, include_quote=False):
        start = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
        fields = ["event_key", "event_type", "symbol", "timestamp", "as_of",
                  "observed_at", "provider", "feed", "open", "high", "low",
                  "close", "volume", "bid", "ask"]
        rows = []
        for i in range(3):
            stamp = start + timedelta(minutes=i)
            rows.append({
                "event_key": _event_key("bar_1m", "SPY", stamp.isoformat()),
                "event_type": "bar_1m", "symbol": "SPY",
                "timestamp": stamp.isoformat(),
                "as_of": (stamp + timedelta(minutes=1)).isoformat(),
                "observed_at": (stamp + timedelta(minutes=2)).isoformat(),
                "provider": "recorded", "feed": "iex", "open": "100",
                "high": "103", "low": "99", "close": str(100 + i),
                "volume": "1000", "bid": "", "ask": "",
            })
        if include_quote:
            stamp = start + timedelta(minutes=2)
            rows.append({
                "event_key": _event_key("quote", "SPY", stamp.isoformat()),
                "event_type": "quote", "symbol": "SPY",
                "timestamp": stamp.isoformat(), "as_of": stamp.isoformat(),
                "observed_at": (stamp + timedelta(minutes=1)).isoformat(),
                "provider": "recorded", "feed": "iex", "open": "",
                "high": "", "low": "", "close": "", "volume": "",
                "bid": "99.9", "ask": "100.1",
            })
        with self.corpus.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def _write_session_closing_rows(self):
        """Write one completed bar in each of two sessions for batching tests."""
        fields = ["event_key", "event_type", "symbol", "timestamp", "as_of",
                  "observed_at", "provider", "feed", "open", "high", "low",
                  "close", "volume", "bid", "ask"]
        rows = []
        for day in (2, 3):
            stamp = datetime(2026, 1, day, 20, 59, tzinfo=timezone.utc)
            rows.append({
                "event_key": _event_key("bar_1m", "SPY", stamp.isoformat()),
                "event_type": "bar_1m", "symbol": "SPY",
                "timestamp": stamp.isoformat(),
                "as_of": (stamp + timedelta(minutes=1)).isoformat(),
                "observed_at": (stamp + timedelta(minutes=2)).isoformat(),
                "provider": "recorded", "feed": "iex", "open": "100",
                "high": "103", "low": "99", "close": "100",
                "volume": "1000", "bid": "", "ask": "",
            })
        with self.corpus.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def _run(self, **kwargs):
        return run_shadow_once(ShadowConfig(self.corpus, self.edge, self.shadow, **kwargs))

    def test_ingest_is_idempotent_and_stale_data_is_explicit(self):
        candidate = self._candidate()
        self._write_rows()
        first = self._run(max_events=20)
        second = self._run(max_events=20)
        self.assertEqual(first["ingested_events"], 3)
        self.assertEqual(second["ingested_events"], 0)
        rows = ShadowStore(self.shadow).decisions(candidate["candidate_id"])
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["kind"] in {"no_data", "unpriced", "no_trade", "reject"} for row in rows))

    def test_changed_digest_is_a_hard_conflict(self):
        self._candidate()
        self._write_rows()
        self._run(max_events=20)
        text = self.corpus.read_text(encoding="utf-8").replace(",100,1000,,", ",100.5,1000,,", 1)
        self.corpus.write_text(text, encoding="utf-8")
        with self.assertRaises((InputConflict, ShadowError)):
            self._run(max_events=20)

    def test_quote_burst_preserves_first_and_last_quote_per_symbol_minute(self):
        start = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
        rows = [{
            "event_key": f"quote-{index}", "event_type": "quote",
            "symbol": "SPY", "timestamp": (start + timedelta(seconds=index)).isoformat(),
            "bid": str(100 + index / 100), "ask": str(101 + index / 100),
        } for index in range(30)]
        compacted = _compact_shadow_rows(rows)
        self.assertEqual(len(compacted), 2)
        self.assertEqual([row["event_key"] for row in compacted],
                         ["quote-0", "quote-29"])

    def test_quote_compaction_preserves_exact_delayed_decision_boundary(self):
        start = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
        quotes = [{
            "event_key": f"quote-{second}", "event_type": "quote",
            "symbol": "SPY", "timestamp": (start + timedelta(seconds=second)).isoformat(),
            "as_of": (start + timedelta(seconds=second)).isoformat(),
            "observed_at": (start + timedelta(seconds=second)).isoformat(),
            "bid": "100", "ask": "100.1",
        } for second in (1, 5, 59)]
        bar = {
            "event_key": "bar", "event_type": "bar_1m", "symbol": "SPY",
            "timestamp": (start - timedelta(minutes=1)).isoformat(),
            "as_of": start.isoformat(),
            "observed_at": (start + timedelta(seconds=6)).isoformat(),
        }
        compacted = _compact_shadow_rows([*quotes, bar])
        self.assertEqual(
            [row["event_key"] for row in compacted if row["event_type"] == "quote"],
            ["quote-1", "quote-5", "quote-59"])

    def test_quote_visibility_requires_as_of_and_observation_before_decision(self):
        at = datetime(2026, 1, 2, 14, 30, 10, tzinfo=timezone.utc)
        future_observation = {
            "symbol": "SPY", "timestamp": "2026-01-02T14:30:00+00:00",
            "as_of": "2026-01-02T14:30:00+00:00",
            "observed_at": "2026-01-02T14:30:11+00:00",
            "bid": 100, "ask": 100.1,
        }
        self.assertIsNone(ShadowRunner._latest_quote([future_observation], at))
        visible = {**future_observation,
                   "observed_at": "2026-01-02T14:30:09+00:00"}
        self.assertEqual(ShadowRunner._latest_quote([visible], at), visible)

    def test_saturated_legacy_wal_baselines_then_ingests_only_appends(self):
        self._candidate()
        self._write_rows()
        original = list(iter_corpus_rows(self.corpus))
        store = ShadowStore(self.shadow)
        for row in original[:2]:
            store.ingest_event(row, max_events=2)
        first = self._run(max_events=2)
        self.assertEqual(first["ingested_events"], 0)
        appended = dict(original[-1])
        stamp = datetime.fromisoformat(appended["timestamp"]) + timedelta(minutes=1)
        appended["timestamp"] = stamp.isoformat()
        appended["as_of"] = (stamp + timedelta(minutes=1)).isoformat()
        appended["event_key"] = _event_key("bar_1m", "SPY", stamp.isoformat())
        with self.corpus.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=list(original[0])).writerow(appended)
        second = self._run(max_events=2)
        self.assertEqual(second["ingested_events"], 1)

    def test_candidate_books_are_isolated_and_edge_ledger_is_read_only(self):
        first = self._candidate()
        second = self._candidate(variant="rule.invalid", strategy="rule",
                                config={"strategy": {"id": "rule", "version": "v1",
                                                     "variant_id": "rule.invalid"},
                                        "risk": {}, "execution": {}, "session": {}})
        self._write_rows(include_quote=True)
        before = self.edge.read_bytes()
        self._run(max_events=20)
        self.assertEqual(before, self.edge.read_bytes())
        books = sqlite3.connect(self.shadow).execute(
            "SELECT candidate_id FROM virtual_books").fetchall()
        self.assertTrue({row[0] for row in books}.issubset({first["candidate_id"], second["candidate_id"]}))

    def test_incomplete_replay_is_diagnostic_and_does_not_touch_edge(self):
        candidate = self._candidate(status="demoted")
        self._write_rows(include_quote=True)
        before = self.edge.read_bytes()
        self._run(max_events=20)
        self.assertEqual(before, self.edge.read_bytes())
        row = sqlite3.connect(self.shadow).execute(
            "SELECT status,details_json FROM replay_diffs WHERE candidate_id=?",
            (candidate["candidate_id"],)).fetchone()
        self.assertIsNotNone(row)
        self.assertIn(row[0], {"incomplete", "mismatch", "match"})
        json.loads(row[1])

    def test_bounds_are_enforced(self):
        self._candidate()
        self._write_rows()
        with self.assertRaises(Exception):
            self._run(max_events=2)

    def test_known_event_conflict_check_skips_repeat_normalization(self):
        self._candidate()
        self._write_rows()
        row = next(iter_corpus_rows(self.corpus))
        store = ShadowStore(self.shadow)
        store.ingest_event(row, max_events=20)
        with patch("research.live_shadow._normalize_row",
                   side_effect=AssertionError("known rows must not normalize")):
            self.assertEqual(store.ingest_event(row, max_events=20), (row["event_key"], False))
            changed = dict(row, close="100.5")
            with self.assertRaises(InputConflict):
                store.ingest_event(changed, max_events=20)

    def _replay_row(self, *, as_of: str, timestamp: str = "2026-01-02T20:59:00+00:00"):
        return {
            "event_key": _event_key("bar_1m", "SPY", timestamp),
            "event_type": "bar_1m", "symbol": "SPY", "timestamp": timestamp,
            "as_of": as_of, "observed_at": "2026-01-02T21:01:00+00:00",
            "provider": "recorded", "feed": "iex", "open": "100",
            "high": "103", "low": "99", "close": "100", "volume": "1000",
        }

    def _seed_open_book(self, runner, candidate, symbol, *, risk_usd, notional):
        timestamp = "2026-01-02T20:59:00+00:00"
        event_key = _event_key("bar_1m", symbol, timestamp)
        runner.store.decision(candidate_id=candidate["candidate_id"],
                              event_key=event_key, session_date="2026-01-02",
                              symbol=symbol, kind="open_incomplete", reason="test",
                              payload={"signal": {}}, max_decisions=20)
        decision = runner.store.decisions(candidate["candidate_id"])[-1]
        runner.store.virtual_open(
            candidate_id=candidate["candidate_id"],
            decision_id=decision["decision_id"], symbol=symbol,
            plan={"symbol": symbol, "entry_price": 100, "shares": 1,
                  "risk_usd": risk_usd, "notional": notional})

    def _evaluate_with_context(self, runner, candidate, symbol="QQQ"):
        event = self._replay_row(
            timestamp="2026-01-02T16:00:00+00:00",
            as_of="2026-01-02T16:01:00+00:00")
        event["observed_at"] = "2026-01-02T16:01:00+00:00"
        event["symbol"] = symbol
        event["event_key"] = _event_key("bar_1m", symbol, event["timestamp"])
        previous = dict(event, timestamp="2026-01-02T15:59:00+00:00",
                        as_of="2026-01-02T16:00:00+00:00",
                        observed_at="2026-01-02T16:00:00+00:00",
                        event_key=_event_key("bar_1m", symbol,
                                             "2026-01-02T15:59:00+00:00"))
        quote = {"symbol": symbol, "timestamp": event["observed_at"],
                 "as_of": event["observed_at"],
                 "observed_at": event["observed_at"],
                 "provider": "recorded", "feed": "sip",
                 "bid": "99.9", "ask": "100.1"}
        signal_ts = datetime.fromisoformat(event["timestamp"]).timestamp()
        signal = {"symbol": symbol, "direction": "long",
                  "setup_type": "ibr_breakout", "signal_ts": signal_ts,
                  "entry_price": 100, "stop_price": 99,
                  "target_price": 102, "stop_distance": 1,
                  "target_r": 2}
        plan = dict(signal, execution_profile="shares")
        with patch("research.live_shadow.generate_ibr_signal",
                   return_value=signal), patch(
                       "research.live_shadow.build_setup_plan",
                       return_value=(plan, None)):
            return runner._evaluate(candidate, event, {symbol: [previous, event]},
                                    {symbol: [quote]}, {})

    def test_open_books_enforce_candidate_local_portfolio_caps(self):
        cases = (
            ({"max_concurrent_positions": 1, "max_open_risk_pct": 100,
              "max_gross_exposure_pct": 100}, "max concurrent positions"),
            ({"max_concurrent_positions": 5, "max_open_risk_pct": 1,
              "max_gross_exposure_pct": 100}, "max open risk cap"),
            ({"max_concurrent_positions": 5, "max_open_risk_pct": 100,
              "max_gross_exposure_pct": 1}, "max gross exposure cap"),
        )
        for limits, expected in cases:
            with self.subTest(expected=expected):
                config = {"strategy": {"id": "ibr", "version": "v1",
                                        "variant_id": "ibr.baseline"},
                          "risk": {"risk_per_trade_pct": .5,
                                   **limits}, "execution": {}, "session": {}}
                candidate = {"candidate_id": f"candidate-{expected}",
                             "strategy_id": "ibr", "vehicle": "equity",
                             "variant_id": "ibr.baseline", "config": config}
                runner = ShadowRunner(ShadowConfig(self.corpus, self.edge, self.shadow))
                self._seed_open_book(runner, candidate, "SPY", risk_usd=600,
                                     notional=600)
                kind, reason, _, _ = self._evaluate_with_context(runner, candidate)
                self.assertEqual(kind, "reject")
                self.assertIn(expected, reason or "")

    def test_open_books_do_not_create_cross_candidate_contention(self):
        limits = {"max_concurrent_positions": 1, "max_open_risk_pct": 1,
                  "max_gross_exposure_pct": 1}
        config = {"strategy": {"id": "ibr", "version": "v1",
                                "variant_id": "ibr.baseline"},
                  "risk": {"risk_per_trade_pct": .5, **limits},
                  "execution": {}, "session": {}}
        first = {"candidate_id": "candidate-first", "strategy_id": "ibr",
                 "vehicle": "equity", "variant_id": "ibr.baseline",
                 "config": config}
        second_config = {**config, "risk": {"risk_per_trade_pct": .5,
                                             "max_concurrent_positions": 5,
                                             "max_open_risk_pct": 100,
                                             "max_gross_exposure_pct": 100}}
        second = {"candidate_id": "candidate-second", "strategy_id": "ibr",
                  "vehicle": "equity", "variant_id": "ibr.baseline",
                  "config": second_config}
        runner = ShadowRunner(ShadowConfig(self.corpus, self.edge, self.shadow))
        self._seed_open_book(runner, first, "SPY", risk_usd=600, notional=600)
        kind, reason, _, plan = self._evaluate_with_context(runner, second)
        self.assertEqual(kind, "open_incomplete", reason)
        self.assertIsNotNone(plan)

    def test_open_book_duplicate_symbol_is_rejected(self):
        config = {"strategy": {"id": "ibr", "version": "v1",
                                "variant_id": "ibr.baseline"},
                  "risk": {"risk_per_trade_pct": .5,
                           "max_concurrent_positions": 5,
                           "max_open_risk_pct": 100,
                           "max_gross_exposure_pct": 100},
                  "execution": {}, "session": {}}
        candidate = {"candidate_id": "candidate-duplicate", "strategy_id": "ibr",
                     "vehicle": "equity", "variant_id": "ibr.baseline",
                     "config": config}
        runner = ShadowRunner(ShadowConfig(self.corpus, self.edge, self.shadow))
        self._seed_open_book(runner, candidate, "SPY", risk_usd=600, notional=600)
        kind, reason, _, _ = self._evaluate_with_context(runner, candidate, "SPY")
        self.assertEqual(kind, "reject")
        self.assertEqual(reason, "already holding this symbol")

    def test_completed_replay_uses_as_of_and_real_ibr_config(self):
        candidate = self._candidate()
        runner = ShadowRunner(ShadowConfig(self.corpus, self.edge, self.shadow))
        row = self._replay_row(as_of="2026-01-02T21:00:00+00:00")
        runner._replay(candidate, "2026-01-02", [row], [], [])
        stored = sqlite3.connect(self.shadow).execute(
            "SELECT status,details_json FROM replay_diffs WHERE candidate_id=?",
            (candidate["candidate_id"],)).fetchone()
        self.assertIsNotNone(stored)
        self.assertNotEqual(stored[0], "incomplete")
        details = json.loads(stored[1])
        self.assertTrue(details["complete"])
        self.assertNotIn("from_mapping", details.get("error", ""))

    def test_option_replay_receives_every_normalized_snapshot(self):
        candidate = {**self._candidate(), "vehicle": "option"}
        runner = ShadowRunner(ShadowConfig(self.corpus, self.edge, self.shadow))
        row = self._replay_row(as_of="2026-01-02T21:00:00+00:00")
        base = {
            "event_type": "option_snapshot", "underlying": "SPY",
            "expiration": "2026-01-16", "strike": "100", "right": "call",
            "multiplier": "100", "bid": "1.0", "ask": "1.1",
            "bid_size": "5", "ask_size": "5", "volume": "20",
            "open_interest": "100", "provider": "alpaca", "feed": "opra",
        }
        options = []
        for index, contract in enumerate(
                ("SPY260116C00100000", "SPY260116C00101000")):
            stamp = datetime(2026, 1, 2, 20, 59, index, tzinfo=timezone.utc)
            options.append({
                **base, "event_key": f"option-{index}", "symbol": contract,
                "contract": contract, "strike": str(100 + index),
                "timestamp": stamp.isoformat(), "as_of": stamp.isoformat(),
                "observed_at": stamp.isoformat(),
            })
        with patch("research.live_shadow.replay_ibr",
                   return_value=SimpleNamespace(trades=[], refusals=[])) as replay:
            runner._replay(candidate, "2026-01-02", [row], [], [], options)
        snapshots = replay.call_args.kwargs["option_snapshots"]
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(
            {snapshot.contract.symbol for snapshot in snapshots.values()},
            {"SPY260116C00100000", "SPY260116C00101000"})

    def test_recorder_calendar_marks_an_early_close_complete(self):
        candidate = self._candidate()
        runner = ShadowRunner(ShadowConfig(self.corpus, self.edge, self.shadow))
        (self.corpus.parent / INDEX_NAME).write_text(json.dumps({
            "session_calendar": {"2026-01-02": {
                "open": "2026-01-02T14:30:00+00:00",
                "close": "2026-01-02T18:00:00+00:00",
                "source": "alpaca_calendar",
            }},
        }), encoding="utf-8")
        row = self._replay_row(
            timestamp="2026-01-02T17:59:00+00:00",
            as_of="2026-01-02T18:00:00+00:00")
        row["observed_at"] = "2026-01-02T18:00:00+00:00"
        extended = self._replay_row(
            timestamp="2026-01-02T18:01:00+00:00",
            as_of="2026-01-02T18:02:00+00:00")
        extended["observed_at"] = "2026-01-02T18:02:00+00:00"
        with patch("research.live_shadow.replay_ibr",
                   return_value=SimpleNamespace(trades=[], refusals=[])) as replay:
            self.assertTrue(runner._replay(
                candidate, "2026-01-02", [row, extended], [], []))
        replay_bars = replay.call_args.args[0]
        replay_config = replay.call_args.kwargs["config"]
        self.assertEqual(len(replay_bars), 1)
        self.assertEqual(replay_config.policy.force_flat_time.isoformat(), "12:50:00")
        self.assertEqual(replay_config.policy.latest_entry_time.isoformat(), "12:55:00")
        metadata = runner.store.replay_metadata(candidate["candidate_id"])[0]
        self.assertTrue(metadata["details"]["complete"])
        self.assertEqual(metadata["details"]["calendar_source"],
                         "recorder_alpaca_calendar")

    def test_early_close_cutoff_prevents_shadow_entry(self):
        candidate = self._candidate()
        runner = ShadowRunner(ShadowConfig(self.corpus, self.edge, self.shadow))
        (self.corpus.parent / INDEX_NAME).write_text(json.dumps({
            "session_calendar": {"2026-01-02": {
                "open": "2026-01-02T14:30:00+00:00",
                "close": "2026-01-02T18:00:00+00:00",
                "source": "alpaca_calendar",
            }},
        }), encoding="utf-8")
        event = self._replay_row(
            timestamp="2026-01-02T17:59:00+00:00",
            as_of="2026-01-02T18:00:00+00:00")
        event["observed_at"] = "2026-01-02T18:00:00+00:00"
        previous = dict(
            event, timestamp="2026-01-02T17:58:00+00:00",
            as_of="2026-01-02T17:59:00+00:00",
            observed_at="2026-01-02T17:59:00+00:00")
        kind, reason, payload, plan = runner._evaluate(
            candidate, event, {"SPY": [previous, event]}, {"SPY": []}, {})
        self.assertEqual((kind, reason, plan),
                         ("no_trade", "session entry cutoff reached", None))
        self.assertEqual(payload["calendar_source"], "recorder_alpaca_calendar")

    def test_no_trade_opportunity_is_persisted_for_gate_denominators(self):
        store = ShadowStore(self.shadow)
        replay = "replay-no-trade"
        store.replay_diff(
            candidate_id="candidate", session_date="2026-01-02",
            source_digest="source", shadow_digest="shadow",
            replay_digest=replay, status="match",
            details={"complete": True, "signature_match": True})
        opportunity = {
            "vehicle": "equity", "symbol": "SPY",
            "session_date": "2026-01-02",
            "opportunity_id": "equity:SPY:2026-01-02",
            "net_pnl": 0.0, "return_value": 0.0, "no_trade": True,
        }
        store.record_replay_evidence(
            candidate_id="candidate", session_date="2026-01-02",
            replay_digest=replay, vehicle="equity", starting_cash=100_000,
            ending_cash=100_000, realized_pnl=0.0, trades=[opportunity],
            replay_status="match")
        self.assertEqual(store.gate_rows("candidate", "2026-01-02"),
                         [opportunity])
        self.assertEqual(store.replay_accounts("candidate")[0]["trade_count"], 0)

    def test_completed_replay_replaces_earlier_incomplete_window(self):
        candidate = self._candidate()
        runner = ShadowRunner(ShadowConfig(self.corpus, self.edge, self.shadow))
        partial = self._replay_row(as_of="2026-01-02T20:00:00+00:00")
        complete = self._replay_row(as_of="2026-01-02T21:00:00+00:00")
        runner._replay(candidate, "2026-01-02", [partial], [], [])
        runner._replay(candidate, "2026-01-02", [complete], [], [])
        rows = sqlite3.connect(self.shadow).execute(
            "SELECT status,details_json FROM replay_diffs WHERE candidate_id=?",
            (candidate["candidate_id"],)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0][0], "incomplete")
        self.assertTrue(json.loads(rows[0][1])["complete"])

    def test_semantic_signature_can_match_and_reports_field_mismatch(self):
        payload = {"signal": {"direction": "long", "setup_type": "rule_opening_range_breakout",
                               "signal_ts": 1767369600.0,
                               "decision_timestamp": "2026-01-02T16:00:00+00:00",
                               "entry_timestamp": "2026-01-02T16:01:00+00:00"},
                   "setup_plan": {"direction": "long", "setup_type": "rule_opening_range_breakout",
                                  "signal_ts": 1767369600.0, "stop_price": 99,
                                  "target_price": 102, "stop_distance": 1,
                                  "target_r": 2, "execution_profile": "shares",
                                  "decision_timestamp": "2026-01-02T16:00:00+00:00",
                                  "entry_timestamp": "2026-01-02T16:01:00+00:00"}}
        runtime = _shadow_signature({"kind": "open_incomplete", "symbol": "SPY",
                                     "session_date": "2026-01-02", "payload_json": json.dumps(payload)})
        replay = _replay_signature({"symbol": "SPY", "session_date": "2026-01-02",
                                    "direction": "long", "signal_timestamp": "2026-01-02T16:00:00+00:00",
                                    "decision_timestamp": "2026-01-02T16:00:00+00:00",
                                    "entry_timestamp": "2026-01-02T16:01:00+00:00",
                                    "stop_price": 99, "target_price": 102,
                                    "stop_distance": 1}, vehicle="equity", strategy_id="rule",
                                   target_r=2, setup_type="rule_opening_range_breakout")
        self.assertEqual(_signature_diffs([runtime], [replay]), [])
        mismatch = {**replay, "target_price": 103}
        differences = _signature_diffs([runtime], [mismatch])
        self.assertEqual([item["field"] for item in differences], ["target_price"])

    def test_completed_replay_closes_virtual_book_for_the_next_session(self):
        store = ShadowStore(self.shadow)
        decision_id = "decision-1"
        store.decision(candidate_id="candidate", event_key="event-1",
                       session_date="2026-01-02", symbol="SPY",
                       kind="open_incomplete", reason="test",
                       payload={"signal": {}}, max_decisions=10)
        store.virtual_open(candidate_id="candidate", decision_id=decision_id,
                           symbol="SPY", plan={"entry_price": 100, "shares": 1})
        # The production ID is content-derived; use the row's actual ID for
        # the close query just as run_once does.
        actual = store.decisions("candidate")[0]["decision_id"]
        with sqlite3.connect(self.shadow) as db:
            db.execute("UPDATE virtual_books SET decision_id=? WHERE decision_id=?",
                       (actual, decision_id))
        self.assertTrue(store.has_open("candidate", "SPY"))
        self.assertEqual(store.close_session_books("candidate", "2026-01-02"), 1)
        self.assertFalse(store.has_open("candidate", "SPY"))

    def test_completed_replay_aggregates_symbols_and_preserves_both_signatures(self):
        candidate = self._candidate()
        runner = ShadowRunner(ShadowConfig(self.corpus, self.edge, self.shadow))
        store = runner.store
        signal_ts = datetime(2026, 1, 2, 16, 0, tzinfo=timezone.utc).timestamp()

        def payload():
            return {"signal": {"direction": "long", "setup_type": "ibr_breakout",
                                "signal_ts": signal_ts},
                    "setup_plan": {"direction": "long", "setup_type": "ibr_breakout",
                                   "signal_ts": signal_ts, "stop_price": 99,
                                   "target_price": 102, "stop_distance": 1,
                                   "target_r": 2, "execution_profile": "shares"}}

        decisions = []
        for symbol in ("SPY", "QQQ"):
            timestamp = "2026-01-02T20:59:00+00:00"
            event_key = _event_key("bar_1m", symbol, timestamp)
            store.decision(candidate_id=candidate["candidate_id"], event_key=event_key,
                           session_date="2026-01-02", symbol=symbol,
                           kind="open_incomplete", reason="test", payload=payload(),
                           max_decisions=10)
            decision = store.decisions(candidate["candidate_id"])[-1]
            store.virtual_open(candidate_id=candidate["candidate_id"],
                               decision_id=decision["decision_id"], symbol=symbol,
                               plan={"entry_price": 100, "shares": 1})
            decisions.append(decision)

        rows = []
        for symbol in ("SPY", "QQQ"):
            row = self._replay_row(as_of="2026-01-02T21:00:00+00:00")
            row["symbol"] = symbol
            row["event_key"] = _event_key("bar_1m", symbol, row["timestamp"])
            rows.append(row)
        trades = [{"symbol": symbol, "session_date": "2026-01-02",
                   "direction": "long", "signal_timestamp": "2026-01-02T16:00:00+00:00",
                   "entry_timestamp": "2026-01-02T16:01:00+00:00",
                   "stop_price": 99, "target_price": 102, "stop_distance": 1,
                   "net_pnl": 0.0}
                  for symbol in ("SPY", "QQQ")]
        result_trades = [SimpleNamespace(
            **{**trade, "session_date": date.fromisoformat(trade["session_date"])})
            for trade in trades]
        self.assertTrue(store.has_open(candidate["candidate_id"], "SPY"))
        self.assertTrue(store.has_open(candidate["candidate_id"], "QQQ"))
        with patch("research.live_shadow.replay_ibr",
                   return_value=SimpleNamespace(trades=result_trades, refusals=[])) as replay:
            self.assertTrue(runner._replay(candidate, "2026-01-02", rows, [], decisions))
            self.assertEqual(replay.call_count, 1)
            self.assertEqual({bar.symbol for bar in replay.call_args.args[0]}, {"SPY", "QQQ"})

        stored = sqlite3.connect(self.shadow).execute(
            "SELECT status,details_json FROM replay_diffs WHERE candidate_id=?",
            (candidate["candidate_id"],)).fetchone()
        self.assertIsNotNone(stored)
        self.assertEqual(stored[0], "match", stored[1])
        details = json.loads(stored[1])
        self.assertEqual(len(details["shadow_signatures"]), 2)
        self.assertEqual(len(details["replay_signatures"]), 2)
        self.assertTrue(details["signature_match"])
        self.assertEqual(len(store.gate_rows(candidate["candidate_id"])), 2)
        account = store.replay_accounts(candidate["candidate_id"])[0]
        self.assertEqual(account["starting_cash"], 100_000.0)
        self.assertEqual(account["ending_cash"], 100_000.0)
        self.assertEqual(account["trade_count"], 2)
        books = sqlite3.connect(self.shadow).execute(
            "SELECT symbol,status FROM virtual_books WHERE candidate_id=? ORDER BY symbol",
            (candidate["candidate_id"],)).fetchall()
        self.assertEqual(books, [("QQQ", "closed_replay"), ("SPY", "closed_replay")])

        mismatch_trades = [SimpleNamespace(
            **{**trade, "session_date": date.fromisoformat(trade["session_date"]),
               "target_price": 103}) for trade in trades]
        with patch("research.live_shadow.replay_ibr",
                   return_value=SimpleNamespace(trades=mismatch_trades, refusals=[])):
            runner._replay(candidate, "2026-01-02", rows, [], decisions)
        self.assertEqual(store.gate_rows(candidate["candidate_id"]), [])

    def test_single_run_session_batches_close_before_next_session(self):
        candidate = self._candidate()
        self._write_session_closing_rows()
        runner = ShadowRunner(ShadowConfig(self.corpus, self.edge, self.shadow))
        calls = []

        def evaluate(*args):
            event = args[1]
            session = (datetime.fromisoformat(event["as_of"])
                       .astimezone(timezone.utc).date().isoformat())
            calls.append(session)
            return ("open_incomplete", "test open", {
                "signal": {}, "setup_plan": {}, "risk_plan": {},
            }, {"entry_price": 100, "shares": 1})

        with patch.object(runner, "_evaluate", side_effect=evaluate):
            runner.run_once()
        self.assertEqual(calls, ["2026-01-02", "2026-01-03"])
        decisions = runner.store.decisions(candidate["candidate_id"])
        self.assertEqual([row["kind"] for row in decisions],
                         ["open_incomplete", "open_incomplete"])

    def test_rule_replay_uses_factory_account(self):
        spec = validate_rule_spec({"schema": "rule-strategy.v2",
                                   "family": "opening_range_breakout"})
        variant = rule_variant_id(spec)
        candidate = self._candidate(
            variant=variant, strategy="rule",
            config={"strategy": {"id": "rule", "version": "v1",
                                  "variant_id": variant, "rule_spec": spec},
                    "risk": {"risk_per_trade_pct": 1},
                    "execution": {}, "session": {}})
        runner = ShadowRunner(ShadowConfig(self.corpus, self.edge, self.shadow))
        row = self._replay_row(as_of="2026-01-02T21:00:00+00:00")
        with patch("research.live_shadow.simulate_account",
                   wraps=factory_simulate_account) as factory:
            runner._replay(candidate, "2026-01-02", [row], [], [])
        self.assertTrue(factory.called)
        stored = sqlite3.connect(self.shadow).execute(
            "SELECT status,details_json FROM replay_diffs WHERE candidate_id=?",
            (candidate["candidate_id"],)).fetchone()
        self.assertIsNotNone(stored)
        details = json.loads(stored[1])
        self.assertNotIn("rule replay factory unavailable", details.get("reason", ""))
        self.assertIn("account", details)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
