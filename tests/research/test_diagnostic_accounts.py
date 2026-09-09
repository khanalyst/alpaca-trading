"""Focused tests for persistent non-authorizing diagnostic account books."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from research.costs import ReplayPolicy
from research.diagnostic_accounts import (
    DiagnosticAccountBook, content_digest, new_account_state,
)
from research.live_shadow import InputConflict, ShadowStore


UTC = timezone.utc


def _config() -> dict:
    return {
        "broker": {"provider": "alpaca", "data_feed": "iex"},
        "execution": {"max_market_data_age_seconds": 30,
                      "max_spread_bps": 100, "max_slippage_bps": 50},
        "costs": {"spread_bps": 4.0, "slippage_bps": 6.0,
                  "fee_bps": 0.5, "provenance": "diagnostic-test"},
        "risk": {"risk_per_trade_pct": 0.5},
    }


def _quote(stamp: datetime, bid: float, ask: float, *,
           observed_at: datetime | None = None,
           provider: str = "alpaca", feed: str = "iex",
           source_mode: str = "forward_observed") -> dict:
    observed = observed_at or stamp
    return {
        "event_key": f"quote:{stamp.isoformat()}:{bid}:{ask}",
        "event_type": "quote", "symbol": "SPY",
        "timestamp": stamp.isoformat(), "as_of": stamp.isoformat(),
        "observed_at": observed.isoformat(), "provider": provider,
        "feed": feed, "source_mode": source_mode, "bid": bid, "ask": ask,
    }


def _bar(stamp: datetime, *, opened: float, high: float, low: float,
         close: float, key: str | None = None, provider: str = "alpaca",
         feed: str = "iex", source_mode: str = "forward_observed") -> dict:
    ended = stamp + timedelta(minutes=1)
    return {
        "event_key": key or f"bar:{stamp.isoformat()}",
        "event_type": "bar_1m", "symbol": "SPY",
        "timestamp": stamp.isoformat(), "as_of": ended.isoformat(),
        "observed_at": ended.isoformat(), "provider": provider,
        "feed": feed, "source_mode": source_mode,
        "open": opened, "high": high, "low": low, "close": close,
        "volume": 1000,
    }


def _plan(direction: str = "long", *, shares: int = 10,
          stop: float = 99.0, target: float = 102.0,
          breakeven_r: float | None = None,
          trailing_stop_r: float | None = None) -> dict:
    return {
        "symbol": "SPY", "direction": direction,
        "shares": shares, "contracts": shares,
        "entry_price": 100.0, "stop_price": stop,
        "target_price": target, "risk_usd": abs(100.0 - stop) * shares,
        "notional": 100.0 * shares, "max_hold_bars": 20,
        "force_flat_ts": None, "breakeven_r": breakeven_r,
        "trailing_stop_r": trailing_stop_r, "target_mode": "fixed_r",
        "target_lookback": None, "exit_before_ts": None,
    }


class DiagnosticAccountBookTests(unittest.TestCase):
    def _book(self, *, candidate: str = "candidate",
              rule_spec: dict | None = None) -> DiagnosticAccountBook:
        return DiagnosticAccountBook(
            account=new_account_state(
                cohort_identity="cohort", candidate_id=candidate,
                starting_cash=100_000.0),
            positions=[], config=_config(),
            policy=ReplayPolicy.from_config(_config()),
            rule_spec=rule_spec or {"max_hold_bars": 20},
        )

    def test_long_and_short_use_executable_sides_and_resting_targets(self):
        entry_bar = _bar(datetime(2026, 9, 10, 14, 30, tzinfo=UTC),
                         opened=100, high=100.2, low=99.8, close=100)
        entry_quote = _quote(datetime(2026, 9, 10, 14, 30, 59, tzinfo=UTC),
                             99.9, 100.1)
        late_same_timestamp = _quote(
            datetime(2026, 9, 10, 14, 30, 59, tzinfo=UTC), 50.0, 51.0,
            observed_at=datetime(2026, 9, 10, 14, 32, tzinfo=UTC))

        long_book = self._book(candidate="long")
        self.assertTrue(long_book.open_requested_position(
            event=entry_bar, plan=_plan(),
            quote_rows=[entry_quote, late_same_timestamp]))
        long_position = next(iter(long_book.positions.values()))
        self.assertEqual(long_position["entry_reference"], 100.1)
        self.assertEqual(long_position["entry_evidence"]["bid"], 99.9)
        self.assertEqual(long_position["entry_evidence"]["ask"], 100.1)
        self.assertEqual(long_position["entry_evidence"]["observed_at"],
                         entry_quote["observed_at"])
        target_bar = _bar(datetime(2026, 9, 10, 14, 31, tzinfo=UTC),
                          opened=100.5, high=102.5, low=100.4, close=102)
        long_book.advance_completed_bar(target_bar, quote_rows=[])
        self.assertEqual(long_position["status"], "closed")
        self.assertEqual(long_position["exit_reason"], "target")
        self.assertEqual(long_book.orders[-1]["evidence"]["source"],
                         "resting_bracket")
        self.assertFalse(long_book.orders[-1]["actual_fill"])

        short_book = self._book(candidate="short")
        self.assertTrue(short_book.open_requested_position(
            event=entry_bar,
            plan=_plan("short", stop=101.0, target=98.0),
            quote_rows=[entry_quote]))
        short_position = next(iter(short_book.positions.values()))
        self.assertEqual(short_position["entry_reference"], 99.9)
        short_target = _bar(datetime(2026, 9, 10, 14, 31, tzinfo=UTC),
                            opened=99.5, high=99.6, low=97.5, close=98)
        short_book.advance_completed_bar(short_target, quote_rows=[])
        self.assertEqual(short_position["status"], "closed")
        self.assertEqual(short_position["exit_reason"], "target")

    def test_same_bar_tie_prefers_stop(self):
        entry = _bar(datetime(2026, 9, 10, 14, 30, tzinfo=UTC),
                     opened=100, high=100.1, low=99.9, close=100)
        quote = _quote(datetime(2026, 9, 10, 14, 30, 59, tzinfo=UTC),
                       99.9, 100.1)
        book = self._book()
        book.open_requested_position(event=entry, plan=_plan(),
                                     quote_rows=[quote])
        tie = _bar(datetime(2026, 9, 10, 14, 31, tzinfo=UTC),
                   opened=100, high=103, low=98, close=101)
        book.advance_completed_bar(tie, quote_rows=[])
        position = next(iter(book.positions.values()))
        self.assertEqual(position["exit_reason"], "stop")
        self.assertTrue(position["tie_broken"])

    def test_breakeven_and_trailing_state_apply_on_the_next_bar(self):
        entry = _bar(datetime(2026, 9, 10, 14, 30, tzinfo=UTC),
                     opened=100, high=100.1, low=99.9, close=100)
        quote = _quote(datetime(2026, 9, 10, 14, 30, 59, tzinfo=UTC),
                       99.9, 100.1)
        book = self._book()
        book.open_requested_position(
            event=entry,
            plan=_plan(stop=99, target=105, breakeven_r=.5,
                       trailing_stop_r=1.0),
            quote_rows=[quote])
        first = _bar(datetime(2026, 9, 10, 14, 31, tzinfo=UTC),
                     opened=100.5, high=102.2, low=100.4, close=102)
        book.advance_completed_bar(first, quote_rows=[
            _quote(datetime(2026, 9, 10, 14, 31, 59, tzinfo=UTC),
                   101.9, 102.1)])
        position = next(iter(book.positions.values()))
        raised = float(position["exit_state"]["active_stop_price"])
        self.assertGreater(raised, float(position["entry_price"]))
        self.assertEqual(position["status"], "open")
        second = _bar(datetime(2026, 9, 10, 14, 32, tzinfo=UTC),
                      opened=102, high=102.1, low=raised - .01, close=raised)
        book.advance_completed_bar(second, quote_rows=[])
        self.assertEqual(position["status"], "closed")
        self.assertEqual(position["exit_reason"], "stop")

    def test_deadline_without_fresh_quote_stays_open_until_first_later_quote(self):
        entry_stamp = datetime(2026, 9, 10, 14, 30, tzinfo=UTC)
        entry = _bar(entry_stamp, opened=100, high=100.1, low=99.9, close=100)
        quote = _quote(entry_stamp + timedelta(seconds=59), 99.9, 100.1)
        book = self._book(rule_spec={"max_hold_bars": 1})
        book.open_requested_position(event=entry, plan=_plan(target=110),
                                     quote_rows=[quote])
        deadline_bar = _bar(entry_stamp + timedelta(minutes=2),
                            opened=100, high=100.2, low=99.8, close=100)
        book.advance_completed_bar(deadline_bar, quote_rows=[])
        position = next(iter(book.positions.values()))
        self.assertEqual(position["status"], "open")
        self.assertTrue(position["late_data_gap"])
        self.assertEqual(position["mark_status"], "unpriced")
        late_quote = _quote(entry_stamp + timedelta(minutes=3, seconds=10),
                            99.7, 99.9)
        later_bar = _bar(entry_stamp + timedelta(minutes=3),
                         opened=100, high=100.1, low=99.9, close=100)
        book.advance_completed_bar(later_bar, quote_rows=[late_quote])
        self.assertEqual(position["status"], "closed")
        self.assertEqual(position["exit_reason"], "max_hold")
        self.assertTrue(position["late_data_gap"])
        self.assertEqual(position["exit_reference"], 99.7)

    def test_overdue_deadline_precedes_later_target_ohlc(self):
        entry_stamp = datetime(2026, 9, 10, 14, 30, tzinfo=UTC)
        entry = _bar(entry_stamp, opened=100, high=100.1, low=99.9, close=100)
        book = self._book(rule_spec={"max_hold_bars": 1})
        book.open_requested_position(
            event=entry, plan=_plan(target=102),
            quote_rows=[_quote(entry_stamp + timedelta(seconds=59),
                               99.9, 100.1)])
        overdue = _bar(entry_stamp + timedelta(days=1), opened=103,
                       high=104, low=102.5, close=103.5)
        book.advance_completed_bar(overdue, quote_rows=[])
        position = next(iter(book.positions.values()))
        self.assertEqual(position["status"], "open")
        self.assertEqual(position["pending_exit"]["reason"], "max_hold")
        self.assertTrue(position["late_data_gap"])

        late = _quote(entry_stamp + timedelta(days=1, seconds=10),
                      102.9, 103.1)
        book.advance_quote_event(late, quote_rows=[late])
        self.assertEqual(position["status"], "closed")
        self.assertEqual(position["exit_reason"], "max_hold")
        self.assertNotEqual(position["exit_reason"], "target")
        self.assertEqual(position["exit_reference"], 102.9)

    def test_gap_exit_requires_forward_exact_quote(self):
        stamp = datetime(2026, 9, 10, 14, 30, tzinfo=UTC)
        entry = _bar(stamp, opened=100, high=100.1, low=99.9, close=100)
        quote = _quote(stamp + timedelta(seconds=59), 99.9, 100.1)
        book = self._book()
        book.open_requested_position(event=entry, plan=_plan(),
                                     quote_rows=[quote])
        gap = _bar(stamp + timedelta(minutes=1), opened=98.5,
                   high=99.0, low=98.0, close=98.7)
        invalid_exit_quotes = [
            _quote(stamp + timedelta(minutes=1), 98.4, 98.6,
                   provider="other"),
            _quote(stamp + timedelta(minutes=1), 98.4, 98.6,
                   source_mode="historical_backfill"),
            _quote(stamp + timedelta(minutes=1), 98.4, 98.6,
                   observed_at=stamp + timedelta(minutes=1, seconds=31)),
        ]
        book.advance_completed_bar(gap, quote_rows=invalid_exit_quotes)
        position = next(iter(book.positions.values()))
        self.assertEqual(position["status"], "open")
        self.assertTrue(position["late_data_gap"])
        later = _quote(stamp + timedelta(minutes=2), 98.2, 98.4)
        book.advance_quote_event(later, quote_rows=[*invalid_exit_quotes, later])
        self.assertEqual(position["status"], "closed")
        self.assertEqual(position["exit_reason"], "stop")
        self.assertEqual(position["exit_reference"], 98.2)

    def test_exit_bars_require_exact_forward_provenance(self):
        stamp = datetime(2026, 9, 10, 14, 30, tzinfo=UTC)
        entry = _bar(stamp, opened=100, high=100.1, low=99.9, close=100)
        quote = _quote(stamp + timedelta(seconds=59), 99.9, 100.1)
        book = self._book()
        book.open_requested_position(event=entry, plan=_plan(),
                                     quote_rows=[quote])
        position = next(iter(book.positions.values()))
        historical = _bar(
            stamp + timedelta(minutes=1), opened=100, high=103, low=99.5,
            close=102, source_mode="historical_backfill")
        book.advance_completed_bar(historical, quote_rows=[])
        self.assertEqual(position["status"], "open")
        wrong_provider = _bar(
            stamp + timedelta(minutes=2), opened=100, high=103, low=99.5,
            close=102, provider="other")
        book.advance_completed_bar(wrong_provider, quote_rows=[])
        self.assertEqual(position["status"], "open")
        forward = _bar(stamp + timedelta(minutes=3), opened=100, high=103,
                       low=99.5, close=102)
        book.advance_completed_bar(forward, quote_rows=[])
        self.assertEqual(position["status"], "closed")
        self.assertEqual(position["exit_reason"], "target")

    def test_wrong_provider_stale_and_backfill_quotes_never_fill(self):
        stamp = datetime(2026, 9, 10, 14, 30, tzinfo=UTC)
        event = _bar(stamp, opened=100, high=100.1, low=99.9, close=100)
        invalid = [
            _quote(stamp + timedelta(seconds=59), 99.9, 100.1,
                   provider="other"),
            _quote(stamp, 99.9, 100.1),
            _quote(stamp + timedelta(seconds=59), 99.9, 100.1,
                   source_mode="historical_backfill"),
        ]
        book = self._book()
        self.assertFalse(book.open_requested_position(
            event=event, plan=_plan(), quote_rows=invalid))
        self.assertEqual(len(book.positions), 0)
        self.assertEqual(book.orders[0]["status"], "unpriced_data_gap")
        self.assertEqual(book.account["fill_count"], 0)


class DiagnosticAccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "shadow.sqlite3"
        self.store = ShadowStore(self.path)
        self.store.seed_diagnostic_accounts(
            cohort_identity="cohort", candidate_ids=["arm-a", "arm-b"],
            starting_cash=100_000)

    def tearDown(self):
        self.tmp.cleanup()

    def _book(self, candidate: str) -> DiagnosticAccountBook:
        snapshot = self.store.diagnostic_account_snapshot(
            cohort_identity="cohort", candidate_id=candidate)
        return DiagnosticAccountBook(
            account=snapshot["account"], positions=snapshot["positions"],
            config=_config(), policy=ReplayPolicy.from_config(_config()),
            rule_spec={"max_hold_bars": 20})

    def _commit(self, candidate: str, cursor: int,
                book: DiagnosticAccountBook) -> None:
        self.store.record_diagnostic_batch(
            cohort_identity="cohort", candidate_id=candidate,
            cursor_inserted_at=float(cursor), cursor_event_key=f"event-{cursor}",
            processed_events=1, rollups={}, pending_sessions=[], decisions=[],
            warmup_session="2026-09-09", max_decisions=100,
            account_batch=book.batch())

    def test_restart_retry_pnl_once_and_arm_isolation(self):
        stamp = datetime(2026, 9, 10, 14, 30, tzinfo=UTC)
        entry = _bar(stamp, opened=100, high=100.1, low=99.9, close=100,
                     key="entry")
        quote = _quote(stamp + timedelta(seconds=59), 99.9, 100.1)
        first = self._book("arm-a")
        first.open_requested_position(event=entry, plan=_plan(),
                                      quote_rows=[quote])
        self._commit("arm-a", 1, first)

        restarted = self._book("arm-a")
        target = _bar(stamp + timedelta(days=1, minutes=1), opened=100.5,
                      high=102.5, low=100.4, close=102, key="target")
        late_quote = _quote(stamp + timedelta(days=1, minutes=1),
                            101.9, 102.1)
        restarted.advance_completed_bar(target, quote_rows=[late_quote])
        closed = next(iter(restarted.positions.values()))
        self.assertEqual(closed["exit_reason"], "max_hold")
        self.assertTrue(closed["late_data_gap"])
        batch = restarted.batch()
        self.store.record_diagnostic_batch(
            cohort_identity="cohort", candidate_id="arm-a",
            cursor_inserted_at=2.0, cursor_event_key="event-2",
            processed_events=1, rollups={}, pending_sessions=[], decisions=[],
            warmup_session="2026-09-09", max_decisions=100,
            account_batch=batch)
        cash = self.store.diagnostic_account_snapshot(
            cohort_identity="cohort", candidate_id="arm-a")["account"]["cash"]
        self.assertGreater(cash, 100_000)
        self.assertEqual(self.store.record_diagnostic_batch(
            cohort_identity="cohort", candidate_id="arm-a",
            cursor_inserted_at=2.0, cursor_event_key="event-2",
            processed_events=1, rollups={}, pending_sessions=[], decisions=[],
            warmup_session="2026-09-09", max_decisions=100,
            account_batch=batch), 0)
        self.assertEqual(self.store.diagnostic_account_snapshot(
            cohort_identity="cohort", candidate_id="arm-a")["account"]["cash"],
            cash)
        arm_b = self.store.diagnostic_account_snapshot(
            cohort_identity="cohort", candidate_id="arm-b")["account"]
        self.assertEqual(arm_b["cash"], 100_000)
        self.assertEqual(arm_b["fill_count"], 0)

        conflict = restarted.batch()
        conflict["account"]["state"]["cash"] += 1
        with self.assertRaises(InputConflict):
            self.store.record_diagnostic_batch(
                cohort_identity="cohort", candidate_id="arm-a",
                cursor_inserted_at=2.0, cursor_event_key="event-2",
                processed_events=2, rollups={}, pending_sessions=[],
                decisions=[], warmup_session="2026-09-09",
                max_decisions=100, account_batch=conflict)

        summary = self.store.diagnostic_account_summary(
            cohort_identity="cohort", candidate_ids=["arm-a", "arm-b"])
        self.assertEqual(summary["account_count"], 2)
        self.assertEqual(summary["modeled_fills"], 2)
        self.assertEqual(summary["actual_fills"], 0)
        self.assertIsNotNone(summary["by_candidate"][0]["last_event_at"])

        stale = self._book("arm-a").batch()
        changed = stale["account"]["state"]
        changed["cash"] += 1.0
        digest_body = dict(changed)
        digest_body.pop("state_digest")
        changed["state_digest"] = content_digest(digest_body)
        stale["account"]["previous_state_digest"] = "stale"
        with self.assertRaises(InputConflict):
            self.store.record_diagnostic_batch(
                cohort_identity="cohort", candidate_id="arm-a",
                cursor_inserted_at=3.0, cursor_event_key="event-3",
                processed_events=1, rollups={}, pending_sessions=[],
                decisions=[{
                    "event_key": "would-have-committed",
                    "session_date": "2026-09-11", "symbol": "SPY",
                    "kind": "reject", "reason": "probe", "payload": {},
                    "plan": None,
                }], warmup_session="2026-09-09", max_decisions=100,
                account_batch=stale)
        self.assertEqual([
            row for row in self.store.decisions("arm-a")
            if row["event_key"] == "would-have-committed"], [])

    def test_old_database_migration_is_additive_and_gate_rows_stay_empty(self):
        legacy = Path(self.tmp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as db, db:
            db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT INTO meta(key,value) VALUES('legacy','kept')")
        store = ShadowStore(legacy)
        with closing(sqlite3.connect(legacy)) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"diagnostic_accounts", "diagnostic_positions",
                             "diagnostic_orders", "diagnostic_fills"} <= tables)
            self.assertEqual(db.execute(
                "SELECT value FROM meta WHERE key='legacy'").fetchone()[0],
                "kept")
        store.seed_diagnostic_accounts(
            cohort_identity="cohort", candidate_ids=["shadow:diagnostic:test"],
            starting_cash=100_000)
        self.assertEqual(store.gate_rows("shadow:diagnostic:test"), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
