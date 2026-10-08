"""Independent regressions for authorizing historical replay memoization."""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from research.live_shadow import ShadowRunner, ShadowStore, _digest
from tests.research import test_shadow_poll_metrics as poll_metrics


class ShadowReplayMemoRegressionTests(unittest.TestCase):
    """Exercise memo contracts without inheriting the shared TestCase."""

    def setUp(self) -> None:
        self.fixture = poll_metrics.ShadowPollMetricsTests(methodName="runTest")
        self.fixture.setUp()
        self.corpus = self.fixture.corpus
        self.edge = self.fixture.edge
        self.shadow = self.fixture.shadow

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _bar(self, stamp: datetime, index: int = 0) -> dict[str, str]:
        return self.fixture._bar(stamp, index)

    def _write(self, rows: list[dict[str, str]]) -> None:
        self.fixture._write(rows)

    def _runner(self) -> ShadowRunner:
        return self.fixture._runner()

    def _candidate(self) -> dict:
        return self.fixture._candidate()

    def _seed_replay_only(self, runner: ShadowRunner, candidate: dict,
                          session: str = "2026-01-02") -> dict[str, str]:
        closing = self._bar(
            datetime.fromisoformat(f"{session}T20:59:00+00:00"), 0)
        self._write([closing])
        store = runner.store
        assert store is not None
        store.upsert_candidate(candidate)
        store.ingest_event(closing, max_events=100)
        store.save_source_offsets({
            str(self.corpus.resolve()): self.corpus.stat().st_size})
        store.save_forward_event_floor(time.time() + 3600.0)
        store.decision(
            candidate_id=candidate["candidate_id"],
            event_key=closing["event_key"], session_date=session,
            symbol="SPY", kind="no_trade", reason="memo seed",
            payload={"session_date": session}, max_decisions=100)
        replay_digest = f"memo-replay-{session}"
        store.replay_diff(
            candidate_id=candidate["candidate_id"], session_date=session,
            source_digest=f"memo-source-{session}",
            shadow_digest=f"memo-shadow-{session}",
            replay_digest=replay_digest, status="match",
            details={"complete": True, "signature_match": True})
        store.record_replay_evidence(
            candidate_id=candidate["candidate_id"], session_date=session,
            replay_digest=replay_digest, vehicle="equity",
            starting_cash=100_000.0, ending_cash=100_000.0,
            realized_pnl=0.0,
            trades=[{"symbol": "SPY", "session_date": session,
                     "no_trade": True}], replay_status="match")
        return closing

    @staticmethod
    def _no_trade(candidate, event, bars, quotes, options):
        del bars, quotes, options
        return ("no_trade", "memo regression", {
            "session_date": str(event["timestamp"])[:10],
            "strategy_id": candidate.get("strategy_id"),
        }, None)

    def test_event_prefix_identity_normalizes_non_utc_bound(self):
        store = ShadowStore(self.shadow)
        event = self._bar(datetime(2026, 1, 2, 20, 59, tzinfo=timezone.utc))
        store.ingest_event(event, max_events=10)

        local_close = datetime(
            2026, 1, 2, 15, 59, tzinfo=ZoneInfo("America/New_York"))
        included = store.event_prefix_identity(local_close)
        self.assertEqual(included["through"], "2026-01-02T20:59:00+00:00")
        self.assertEqual(included["row_count"], 1)
        self.assertEqual(included["last_event_key"], event["event_key"])

        before_close = store.event_prefix_identity(
            local_close - timedelta(seconds=1))
        self.assertEqual(before_close["row_count"], 0)
        self.assertIsNone(before_close["last_event_key"])

    def test_account_identity_batches_respect_999_variable_limit(self):
        store = ShadowStore(self.shadow)
        pairs = [(f"candidate-{index}", "2026-01-02") for index in range(400)]
        with store._connection() as db:
            db.executemany("""INSERT INTO replay_diffs
                (diff_id,candidate_id,session_date,source_digest,shadow_digest,
                 replay_digest,status,details_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", [
                    (str(index), candidate, session, "source", "shadow",
                     "replay", "match", "{}", 1.0)
                    for index, (candidate, session) in enumerate(pairs)])
        original_connection = store._connection

        @contextmanager
        def limited_connection():
            with original_connection() as db:
                db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
                yield db

        with patch.object(store, "_connection", limited_connection):
            identities = store.replay_session_identities(pairs)
        self.assertEqual(set(identities), set(pairs))
        self.assertTrue(all(value["account"] is None
                            for value in identities.values()))

    def test_memo_key_changes_for_each_semantic_invalidation_input(self):
        runner = self._runner()
        candidate = self._candidate()
        candidate["config"] = json.loads(candidate["config_json"])
        session = "2026-01-02"
        close = datetime(2026, 1, 2, 21, 0, tzinfo=timezone.utc)
        replay_window = {
            "calendar_close": close,
            "calendar_source": "recorded",
            "calendar_bounds": (
                datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc), close),
        }
        causal_prefix = {
            "through": close.isoformat(), "row_count": 1,
            "max_inserted_at": 100.0, "last_event_key": "event-1",
            "last_digest": "source-1",
        }
        replay_identity = {
            "replay_code_hash": "code-1", "manifest_digest": "manifest-1",
            "candidate_set_digest": "candidates-1",
            "replay_epoch_identity": "epoch-1",
        }
        replay_state = {
            "replay": {"status": "match", "details_digest": "details-1"},
            "account": {"account_id": "account-1", "realized_pnl": 0.0,
                        "replay_status": "match"},
            "ambiguous_account": False,
        }
        book_state = [{"book_id": "book-1", "status": "closed_replay",
                       "symbol": "QQQ", "plan_json": "{}"}]

        def key(*, candidate_value=candidate, replay_window_value=replay_window,
                causal_value=causal_prefix, quarantine_value=None,
                replay_quarantine_value=None,
                replay_identity_value=replay_identity,
                replay_state_value=replay_state, book_state_value=book_state):
            return runner._authorizing_replay_memo_key(
                candidate_value, session, replay_window_value, causal_value,
                quarantine_value or {}, replay_quarantine_value or {},
                replay_identity_value, replay_state_value, book_state_value)

        baseline = key()
        mutations = {}

        changed_code = dict(replay_identity)
        changed_code["replay_code_hash"] = "code-2"
        mutations["replay code"] = key(replay_identity_value=changed_code)

        changed_config_candidate = deepcopy(candidate)
        changed_config_candidate["config"]["risk"]["risk_per_trade_pct"] = 2
        mutations["candidate config"] = key(
            candidate_value=changed_config_candidate)

        changed_proof_candidate = dict(candidate)
        changed_proof_candidate["dataset_hash"] = "dataset-2"
        mutations["candidate proof"] = key(candidate_value=changed_proof_candidate)

        original_config = runner.config
        runner.config = replace(
            runner.config, runtime_config={"runtime_epoch": "runtime-2"})
        try:
            mutations["runtime"] = key()
        finally:
            runner.config = original_config

        changed_cost_candidate = deepcopy(candidate)
        changed_cost_candidate["config"]["costs"] = {"spread_bps": 10.0}
        mutations["cost model"] = key(candidate_value=changed_cost_candidate)

        changed_source = dict(causal_prefix)
        changed_source["last_digest"] = "source-2"
        mutations["causal source prefix"] = key(causal_value=changed_source)

        mutations["source quarantine"] = key(
            quarantine_value={"event-1": {
                "session_date": session, "status": "quarantined"}})
        mutations["replay quarantine"] = key(
            replay_quarantine_value={
                f"{candidate['candidate_id']}:{session}": {
                    "candidate_id": candidate["candidate_id"],
                    "session_date": session, "status": "quarantined",
                    "reason": "mismatch"}})

        changed_book_state = deepcopy(book_state)
        changed_book_state[0]["status"] = "open_incomplete"
        mutations["virtual book"] = key(book_state_value=changed_book_state)

        changed_account = deepcopy(replay_state)
        changed_account["account"]["realized_pnl"] = 1.0
        mutations["account semantic state"] = key(
            replay_state_value=changed_account)

        self.assertEqual(len(mutations), 10)
        for label, value in mutations.items():
            with self.subTest(label=label):
                self.assertNotEqual(value, baseline)
        self.assertEqual(len(set(mutations.values())), len(mutations), mutations)

    def test_ambiguous_replay_account_retries_and_remains_blocked(self):
        runner = self._runner()
        candidate = self._candidate()
        session = "2026-01-02"
        self._seed_replay_only(runner, candidate, session)

        with patch.object(runner, "_evaluate", side_effect=self._no_trade), \
                patch.object(runner, "_replay", return_value=True):
            runner.run_once()
        prior_floor = runner.store.forward_event_floor()

        ambiguous = {
            (candidate["candidate_id"], session): {
                "replay": {
                    "source_digest": "memo-source-2026-01-02",
                    "shadow_digest": "memo-shadow-2026-01-02",
                    "replay_digest": "memo-replay-2026-01-02",
                    "status": "match", "details_digest": "details-1",
                },
                "account": None,
                "ambiguous_account": True,
            }
        }
        with patch.object(runner.store, "replay_session_identities",
                          return_value=ambiguous), \
                patch.object(runner, "_evaluate",
                             side_effect=self._no_trade), \
                patch.object(runner, "_replay", return_value=True) as replay:
            result = runner.run_once()

        replay.assert_called_once()
        self.assertEqual(result["forward_event_floor"], prior_floor)
        memo = runner._authorizing_replay_memo[(candidate["candidate_id"], session)]
        self.assertFalse(memo["ok"])

    def test_future_same_symbol_open_book_invalidates_historical_memo(self):
        runner = self._runner()
        candidate = self._candidate()
        session = "2026-01-02"
        self._seed_replay_only(runner, candidate, session)

        with patch.object(runner, "_evaluate", side_effect=self._no_trade), \
                patch.object(runner, "_replay", return_value=True):
            runner.run_once()

        future_event_key = "future-open-event"
        future_session = "2026-01-03"
        candidate_id = candidate["candidate_id"]
        runner.store.decision(
            candidate_id=candidate_id, event_key=future_event_key,
            session_date=future_session, symbol="SPY", kind="open_incomplete",
            reason="future book", payload={"session_date": future_session},
            max_decisions=100)
        runner.store.virtual_open(
            candidate_id=candidate_id,
            decision_id=_digest({"candidate_id": candidate_id,
                                 "event_key": future_event_key}),
            symbol="SPY",
            plan={"symbol": "SPY", "quantity": 1.0,
                  "entry_price": 100.0, "risk_usd": 10.0,
                  "notional": 100.0})
        self.assertTrue(runner.store.open_books(candidate_id))

        with patch.object(runner, "_evaluate",
                          side_effect=AssertionError(
                              "future open book should suppress historical entry")) as evaluate, \
                patch.object(runner, "_replay", return_value=True) as replay:
            runner.run_once()

        evaluate.assert_not_called()
        replay.assert_called_once()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
