"""Differential checks for immutable diagnostic quote snapshots."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research import diagnostic_accounts
from research.costs import ReplayPolicy
from research.diagnostic_accounts import (
    DiagnosticAccountBook,
    DiagnosticAccountError,
    new_account_state,
    prepare_diagnostic_quote_index,
)
from research.diagnostic_shadow import build_diagnostic_cohort
from research.live_shadow import ShadowConfig, ShadowRunner
from tests.research.test_diagnostic_accounts import (
    _bar,
    _config,
    _plan,
    _quote,
)
from tests.research.test_ibr_runtime_parity import (
    SESSION,
    SESSION_OPEN,
    _bar as _ibr_bar,
    _opening_breakout,
    _quote as _ibr_quote,
    _runtime_config,
)


UTC = timezone.utc


class DiagnosticQuoteIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = _config()
        self.policy = ReplayPolicy.from_config(self.config)

    def _book(self, *, max_hold_bars: int = 20) -> DiagnosticAccountBook:
        return self._book_with_policy(
            self.policy, max_hold_bars=max_hold_bars)

    def _book_with_policy(
            self, policy: ReplayPolicy, *,
            max_hold_bars: int = 20) -> DiagnosticAccountBook:
        return DiagnosticAccountBook(
            account=new_account_state(
                cohort_identity="cohort", candidate_id="candidate",
                starting_cash=100_000.0),
            positions=[], config=self.config, policy=policy,
            rule_spec={"max_hold_bars": max_hold_bars},
        )

    def _prepared(self, rows: list[dict]):
        return prepare_diagnostic_quote_index(
            {"SPY": tuple(rows)}, policy=self.policy)

    def assertLookupParity(self, rows: list[dict], *, at: datetime,
                           side: str = "buy") -> dict | None:
        raw = self._book().executable_quote(
            rows, symbol="SPY", at=at, side=side)
        prepared = self._book().executable_quote(
            self._prepared(rows), symbol="SPY", at=at, side=side)
        self.assertEqual(prepared, raw)
        return raw

    def test_lookup_preserves_visibility_ties_provenance_and_session_barriers(self):
        at = datetime(2026, 9, 10, 14, 31, tzinfo=UTC)
        stamp = at - timedelta(seconds=5)
        first = _quote(stamp, 99.9, 100.1)
        tied_later_source = _quote(stamp, 100.0, 100.2)
        delayed_correction = _quote(
            stamp, 100.5, 100.7,
            observed_at=at + timedelta(seconds=10))
        malformed = _quote(stamp, 101.0, 100.0)
        rows = [
            _quote(at - timedelta(seconds=31), 90.0, 90.2),
            first,
            tied_later_source,
            delayed_correction,
            malformed,
            _quote(stamp, 80.0, 80.2, provider="other"),
            _quote(stamp, 70.0, 70.2,
                   source_mode="historical_backfill"),
        ]

        before_correction = self.assertLookupParity(rows, at=at)
        self.assertIsNotNone(before_correction)
        self.assertEqual(before_correction["price"], 100.2)
        visible_correction = self.assertLookupParity(
            rows, at=at + timedelta(seconds=10))
        self.assertIsNotNone(visible_correction)
        self.assertEqual(visible_correction["price"], 100.7)
        self.assertIsNone(self.assertLookupParity(
            rows, at=at + timedelta(seconds=40)))

        inclusive = [_quote(at - timedelta(seconds=30), 101.0, 101.2)]
        boundary = self.assertLookupParity(inclusive, at=at)
        self.assertIsNotNone(boundary)
        self.assertEqual(boundary["price"], 101.2)

        prior_session = datetime(2026, 9, 11, 3, 59, 50, tzinfo=UTC)
        next_session = prior_session + timedelta(seconds=20)
        self.assertIsNone(self.assertLookupParity(
            [_quote(prior_session, 102.0, 102.2)], at=next_session))

    def test_snapshot_normalizes_each_eligible_row_once_and_never_reindexes(self):
        at = datetime(2026, 9, 10, 14, 31, tzinfo=UTC)
        rows = [
            _quote(at - timedelta(seconds=5), 99.9, 100.1),
            _quote(at - timedelta(seconds=4), 100.0, 100.2),
            _quote(at - timedelta(seconds=3), 101.0, 100.0),
            _quote(at - timedelta(seconds=2), 80.0, 80.2,
                   provider="other"),
            _quote(at - timedelta(seconds=1), 70.0, 70.2,
                   source_mode="historical_backfill"),
        ]
        original = diagnostic_accounts.normalize_quote
        with patch.object(
                diagnostic_accounts, "normalize_quote", wraps=original) as normalize:
            prepared = self._prepared(rows)
            self.assertEqual(normalize.call_count, 3)
            rows[1]["ask"] = 999.0
            book = self._book()
            with patch.object(
                    diagnostic_accounts, "index_quotes",
                    side_effect=AssertionError("prepared quotes were reindexed")):
                first = book.executable_quote(
                    prepared, symbol="SPY", at=at, side="buy")
                second = book.executable_quote(
                    prepared, symbol="SPY", at=at, side="sell")
                after = book.first_executable_quote_after(
                    prepared, symbol="SPY", after=at - timedelta(seconds=5),
                    visible_by=at, side="buy")
            self.assertEqual(normalize.call_count, 3)
        self.assertEqual(first["price"], 100.2)
        self.assertEqual(second["price"], 100.0)
        self.assertIsNotNone(after)

    def test_snapshot_rejects_cross_policy_reuse_on_both_fast_paths(self):
        at = datetime(2026, 9, 10, 14, 31, tzinfo=UTC)
        prepared = self._prepared([
            _quote(at - timedelta(seconds=5), 99.9, 100.1),
        ])
        mismatches = (
            replace(self.policy, max_market_data_age_seconds=29.0),
            replace(self.policy, equity_feed="sip"),
            replace(self.policy, equity_provider="other"),
        )
        for policy in mismatches:
            with self.subTest(policy=policy):
                book = self._book_with_policy(policy)
                with self.assertRaisesRegex(
                        DiagnosticAccountError, "quote policy mismatch"):
                    book.executable_quote(
                        prepared, symbol="SPY", at=at, side="buy")
                with self.assertRaisesRegex(
                        DiagnosticAccountError, "quote policy mismatch"):
                    book.first_executable_quote_after(
                        prepared, symbol="SPY",
                        after=at - timedelta(seconds=10),
                        visible_by=at, side="buy")

    def test_resting_close_account_batch_matches_raw_fallback(self):
        stamp = datetime(2026, 9, 10, 14, 30, tzinfo=UTC)
        entry = _bar(
            stamp, opened=100.0, high=100.1, low=99.9, close=100.0)
        rows = [_quote(stamp + timedelta(seconds=59), 99.9, 100.1)]
        prepared = self._prepared(rows)
        raw_book = self._book()
        indexed_book = self._book()
        self.assertTrue(raw_book.open_requested_position(
            event=entry, plan=_plan(), quote_rows=rows))
        self.assertTrue(indexed_book.open_requested_position(
            event=entry, plan=_plan(), quote_rows=prepared))

        target = _bar(
            stamp + timedelta(minutes=1), opened=100.5, high=102.5,
            low=100.4, close=102.0)
        raw_book.advance_completed_bar(target, quote_rows=rows)
        indexed_book.advance_completed_bar(target, quote_rows=prepared)
        self.assertEqual(indexed_book.batch(), raw_book.batch())
        position = next(iter(indexed_book.positions.values()))
        self.assertEqual((position["status"], position["exit_reason"]),
                         ("closed", "target"))

    def test_pending_gap_and_deadline_account_batches_match_raw_fallback(self):
        stamp = datetime(2026, 9, 10, 14, 30, tzinfo=UTC)
        entry = _bar(
            stamp, opened=100.0, high=100.1, low=99.9, close=100.0)
        entry_quote = _quote(
            stamp + timedelta(seconds=59), 99.9, 100.1)
        late_quote = _quote(
            stamp + timedelta(minutes=3, seconds=10), 99.7, 99.9)
        rows = [entry_quote, late_quote]
        prepared = self._prepared(rows)
        raw_book = self._book(max_hold_bars=1)
        indexed_book = self._book(max_hold_bars=1)
        for book, quote_rows in ((raw_book, rows),
                                 (indexed_book, prepared)):
            self.assertTrue(book.open_requested_position(
                event=entry, plan=_plan(target=110.0),
                quote_rows=quote_rows))

        deadline = _bar(
            stamp + timedelta(minutes=2), opened=100.0, high=100.2,
            low=99.8, close=100.0)
        raw_book.advance_completed_bar(deadline, quote_rows=rows)
        indexed_book.advance_completed_bar(deadline, quote_rows=prepared)
        self.assertEqual(indexed_book.batch(), raw_book.batch())
        pending = next(iter(indexed_book.positions.values()))
        self.assertEqual(pending["status"], "open")
        self.assertTrue(pending["late_data_gap"])
        self.assertEqual(pending["mark_status"], "unpriced")

        raw_book.advance_quote_event(late_quote, quote_rows=rows)
        indexed_book.advance_quote_event(late_quote, quote_rows=prepared)
        self.assertEqual(indexed_book.batch(), raw_book.batch())
        closed = next(iter(indexed_book.positions.values()))
        self.assertEqual((closed["status"], closed["exit_reason"]),
                         ("closed", "max_hold"))

    def test_position_producing_arm_matches_raw_decisions_and_account_batch(self):
        runtime = _runtime_config()
        cohort = build_diagnostic_cohort(
            runtime, code_identity="a" * 64, include_ibr=True)
        arm = next(
            deepcopy(item) for item in cohort["arms"]
            if item.get("strategy_id") == "ibr" and
            item.get("variant_id") == "ibr.baseline")
        bars = _opening_breakout()
        signal = bars[-1]
        signal_at = datetime.fromisoformat(signal["observed_at"])
        entry_quote = _ibr_quote(
            signal_at, bid=101.2, ask=101.4, key="quote:entry")
        middle = _ibr_bar(
            SESSION_OPEN + timedelta(minutes=16), opened=101.4,
            high=101.8, low=101.0, close=101.6, key="bar:middle")
        middle_at = datetime.fromisoformat(middle["observed_at"])
        middle_quote = _ibr_quote(
            middle_at, bid=101.5, ask=101.7, key="quote:middle")
        target = _ibr_bar(
            SESSION_OPEN + timedelta(minutes=17), opened=101.6,
            high=110.0, low=101.1, close=109.0, key="bar:target")
        target_at = datetime.fromisoformat(target["observed_at"])
        target_quote = _ibr_quote(
            target_at, bid=108.9, ask=109.1, key="quote:target")
        all_bars = [*bars, middle, target]
        quotes = [entry_quote, middle_quote, target_quote]
        events = [signal, entry_quote, middle, middle_quote,
                  target, target_quote]
        initial = {
            "account": new_account_state(
                cohort_identity=str(arm["cohort_identity"]),
                candidate_id=str(arm["candidate_id"]),
                starting_cash=100_000.0),
            "positions": [],
        }

        with tempfile.TemporaryDirectory(prefix="diagnostic-quote-index-") as root:
            root_path = Path(root)
            corpus = root_path / "recorded.csv"
            corpus.write_text("event_key,event_type\n", encoding="utf-8")
            runner = ShadowRunner(ShadowConfig(
                corpus, root_path / "edge.sqlite3",
                root_path / "shadow.sqlite3", diagnostic=True,
                diagnostic_include_ibr=True,
                runtime_config=deepcopy(runtime),
                runtime_config_path="/mounted/config.yaml",
                max_events=500, max_decisions=5000, max_workers=1,
                diagnostic_session_max_events=500))
            inputs = {SESSION: (all_bars, quotes, ())}
            grouped_bars = {"SPY": all_bars}
            grouped_quotes = {"SPY": quotes}

            def raw_snapshot(mapping, *, policy):
                del policy
                return tuple(
                    row for symbol in sorted(mapping)
                    for row in mapping[symbol])

            with patch(
                    "research.live_shadow.prepare_diagnostic_quote_index",
                    side_effect=raw_snapshot):
                raw = runner._evaluate_diagnostic_arm_snapshot(
                    arm, {SESSION: events}, inputs, grouped_bars,
                    grouped_quotes, {}, deepcopy(initial))

            original = diagnostic_accounts.normalize_quote
            with patch.object(
                    diagnostic_accounts, "normalize_quote",
                    wraps=original) as normalize:
                prepared = runner._evaluate_diagnostic_arm_snapshot(
                    arm, {SESSION: events}, inputs, grouped_bars,
                    grouped_quotes, {}, deepcopy(initial))
                self.assertEqual(normalize.call_count, len(quotes))

        self.assertIsNone(raw["error"], raw)
        self.assertIsNone(prepared["error"], prepared)
        self.assertEqual(prepared["decisions"], raw["decisions"])
        self.assertEqual(prepared["account_batch"], raw["account_batch"])
        self.assertGreaterEqual(len(prepared["decisions"]), 3)
        account = prepared["account_batch"]["account"]["state"]
        self.assertGreaterEqual(account["fill_count"], 2)
        self.assertEqual(
            prepared["account_batch"]["positions"][0]["state"]["status"],
            "closed")

    def test_no_position_arm_does_not_normalize_account_quotes(self):
        runtime = _runtime_config()
        cohort = build_diagnostic_cohort(
            runtime, code_identity="a" * 64, include_ibr=True)
        arm = next(
            deepcopy(item) for item in cohort["arms"]
            if item.get("strategy_id") == "ibr" and
            item.get("variant_id") == "ibr.baseline")
        bars = _opening_breakout()
        signal = bars[-1]
        signal_at = datetime.fromisoformat(signal["observed_at"])
        stale_quote = _ibr_quote(
            signal_at - timedelta(seconds=31), bid=101.2, ask=101.4,
            key="quote:stale")
        initial = {
            "account": new_account_state(
                cohort_identity=str(arm["cohort_identity"]),
                candidate_id=str(arm["candidate_id"]),
                starting_cash=100_000.0),
            "positions": [],
        }
        with tempfile.TemporaryDirectory(
                prefix="diagnostic-quote-index-empty-") as root:
            root_path = Path(root)
            corpus = root_path / "recorded.csv"
            corpus.write_text("event_key,event_type\n", encoding="utf-8")
            runner = ShadowRunner(ShadowConfig(
                corpus, root_path / "edge.sqlite3",
                root_path / "shadow.sqlite3", diagnostic=True,
                diagnostic_include_ibr=True,
                runtime_config=deepcopy(runtime),
                runtime_config_path="/mounted/config.yaml",
                max_events=500, max_decisions=5000, max_workers=1,
                diagnostic_session_max_events=500))
            original = diagnostic_accounts.normalize_quote
            with patch.object(
                    diagnostic_accounts, "normalize_quote",
                    wraps=original) as normalize:
                result = runner._evaluate_diagnostic_arm_snapshot(
                    arm, {SESSION: [signal]},
                    {SESSION: (bars, [stale_quote], ())},
                    {"SPY": bars}, {"SPY": [stale_quote]}, {},
                    deepcopy(initial))
                self.assertEqual(normalize.call_count, 0)
        self.assertIsNone(result["error"], result)
        self.assertEqual(
            result["account_batch"]["account"]["state"]["fill_count"], 0)


if __name__ == "__main__":
    unittest.main()
