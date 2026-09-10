"""End-to-end regression coverage for exact shadow session deadlines."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import csv
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from deploy.recorder import INDEX_NAME
from deploy.recorder_market import _event_key
from research.diagnostic_shadow import build_diagnostic_cohort
from research.live_shadow import ShadowConfig, ShadowRunner


UTC = timezone.utc


def _runtime_config() -> dict:
    """Return the mounted policy used by one isolated diagnostic arm."""
    risk = {
        "risk_per_trade_pct": 0.5,
        "daily_loss_limit_pct": 2.0,
        "max_open_risk_pct": 2.0,
        "max_concurrent_positions": 3,
        "max_position_notional_pct": 25.0,
        "max_gross_exposure_pct": 50.0,
    }
    # Keep the shipped stressed-cost controls present in every path.  The
    # stress regression narrows the authored stop to the audited 30 bps floor,
    # while the deadline paths retain the wider deterministic range.
    risk.update({
        "stressed_cost_scenario_bps": 25.0,
        "max_stressed_cost_to_risk_ratio": 0.30,
    })
    return {
        "mode": "paper",
        "broker": {"provider": "alpaca", "data_feed": "iex",
                   "options_feed": "opra", "paper": True,
                   "allow_live": False},
        "session": {
            "timezone": "America/New_York",
            "entries_regular_session_only": True,
            "allow_exits_outside_session": True,
            "require_exact_calendar": True,
            # The strategy override below is intentional.  It must win over
            # this session default in ReplayPolicy and the shadow evaluator.
            "force_flat_minutes_before_close": 10,
            "reject_new_entries_minutes_before_close": 5,
        },
        "universe": {"symbols": ["SPY"], "asset_classes": ["us_equity"],
                     "min_price": 1.0, "max_symbols": 1, "denylist": []},
        "strategy": {
            "id": "rule", "version": "v1", "variant_id": "auto",
            "execution_mode": "shares",
            "force_flat_minutes_before_close": 12,
        },
        "risk": risk,
        "execution": {
            "order_type": "market", "time_in_force": "day",
            "max_slippage_bps": 50, "max_market_data_age_seconds": 30,
            "max_spread_bps": 100, "strict_market_data": True,
        },
        "costs": {"spread_bps": 4.0, "slippage_bps": 6.0,
                  "fee_bps": 0.5,
                  "provenance": "deadline-regression"},
        "research": {"enabled": True, "require_validated_variant": True},
    }


def _bar(stamp: datetime, close: float, *, high: float | None = None,
         low: float | None = None, observed_at: datetime | None = None,
         volume: float = 1000) -> dict:
    ended = stamp + timedelta(minutes=1)
    observed = observed_at or ended
    return {
        "event_key": _event_key("bar_1m", "SPY", stamp.isoformat()),
        "event_type": "bar_1m", "symbol": "SPY",
        "timestamp": stamp.isoformat(), "as_of": ended.isoformat(),
        "observed_at": observed.isoformat(), "provider": "alpaca",
        "feed": "iex", "source_mode": "forward_observed",
        "open": close, "high": high if high is not None else close + 0.5,
        "low": low if low is not None else close - 0.5,
        "close": close, "volume": volume,
    }


def _quote(stamp: datetime, bid: float, ask: float, *,
           as_of: datetime | None = None,
           observed_at: datetime | None = None) -> dict:
    available = as_of or stamp
    observed = observed_at or available
    return {
        "event_key": _event_key("quote", "SPY", stamp.isoformat()),
        "event_type": "quote", "symbol": "SPY",
        "timestamp": stamp.isoformat(), "as_of": available.isoformat(),
        "observed_at": observed.isoformat(), "provider": "alpaca",
        "feed": "iex", "source_mode": "forward_observed",
        "bid": bid, "ask": ask,
    }


class ShadowDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.corpus = self.root / "recorded.csv"
        self.edge = self.root / "edge.sqlite3"
        self.shadow = self.root / "shadow.sqlite3"
        fields = [
            "event_key", "event_type", "symbol", "timestamp", "as_of",
            "observed_at", "provider", "feed", "source_mode", "open",
            "high", "low", "close", "volume", "bid", "ask",
        ]
        with self.corpus.open("w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=fields).writeheader()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _arm(self) -> dict:
        cohort = build_diagnostic_cohort(
            _runtime_config(), code_identity="a" * 64)
        return next(
            arm for arm in cohort["arms"]
            if arm.get("family") == "momentum_continuation"
            and arm.get("role") == "baseline")

    def _calendar(self, *, close: datetime) -> None:
        session = close.astimezone(UTC).date().isoformat()
        opened = close.astimezone(UTC).replace(
            hour=14, minute=30, second=0, microsecond=0)
        (self.root / INDEX_NAME).write_text(json.dumps({
            "session_calendar": {session: {
                "open": opened.isoformat(),
                "close": close.isoformat(),
                "source": "alpaca_calendar",
            }},
        }), encoding="utf-8")

    def _signal_window(self, *, signal_at: datetime,
                       exit_at: datetime | None = None,
                       narrow: bool = False) -> tuple[
                           list[dict], list[dict], list[dict]]:
        """Build a causal prefix whose real momentum rule emits once."""
        first = signal_at - timedelta(minutes=49)
        bars = [
            _bar(
                first + timedelta(minutes=index), 100.0,
                high=100.025 if narrow else 100.5,
                low=99.975 if narrow else 99.5)
            for index in range(49)
        ]
        bars.append(_bar(signal_at, 102.0,
                         high=102.025 if narrow else 102.5,
                         low=101.975 if narrow else 99.5,
                         volume=1500))
        quotes = [_quote(signal_at + timedelta(seconds=30), 101.9, 102.1)]
        events = [*bars, *quotes]
        if exit_at is not None:
            # Make the quote executable exactly at the deadline while making
            # its observation event causal.  The later bar has hostile OHLC
            # so a deadline exit must win over any resting bracket inference.
            exit_quote = _quote(
                exit_at - timedelta(seconds=1), 103.1, 103.2,
                as_of=exit_at, observed_at=exit_at)
            exit_bar = _bar(
                exit_at, 150.0, high=999.0, low=1.0,
                observed_at=exit_at + timedelta(minutes=1))
            quotes.append(exit_quote)
            bars.append(exit_bar)
            events.extend((exit_quote, exit_bar))
        return bars, quotes, events

    def _runner(self) -> ShadowRunner:
        return ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow,
            max_events=1000, max_decisions=1000,
            diagnostic_session_max_events=1000,
            equity=100_000.0, runtime_config=_runtime_config(),
            runtime_config_path="/mounted/config.yaml"))

    def _evaluate(self, runner: ShadowRunner, arm: dict,
                  bars: list[dict], quotes: list[dict],
                  event: dict) -> tuple:
        return runner._evaluate(
            arm, event, {"SPY": bars}, {"SPY": quotes}, {})

    def _persist_diagnostic_session(
            self, *, close: datetime) -> tuple[dict, dict, list[dict]]:
        arm = self._arm()
        self._calendar(close=close)
        signal_at = close - timedelta(minutes=50)
        expected_deadline = close - timedelta(minutes=12)
        bars, quotes, events = self._signal_window(
            signal_at=signal_at, exit_at=expected_deadline)
        session = signal_at.astimezone(UTC).date().isoformat()
        runner = self._runner()
        candidate_id = str(arm["candidate_id"])
        cohort_identity = str(arm["cohort_identity"])
        runner.store.seed_diagnostic_accounts(
            cohort_identity=cohort_identity, candidate_ids=[candidate_id],
            starting_cash=runner.config.equity)
        initial = runner.store.diagnostic_account_snapshot(
            cohort_identity=cohort_identity, candidate_id=candidate_id)
        result = runner._evaluate_diagnostic_arm_snapshot(
            arm, {session: events}, {session: (bars, quotes, ())},
            {"SPY": bars}, {"SPY": quotes}, {}, initial)
        self.assertIsNone(result["error"], result)
        opened = [decision for decision in result["decisions"]
                  if decision.get("kind") == "open_incomplete"
                  and decision.get("plan") is not None]
        self.assertEqual(len(opened), 1, result["decisions"])
        self.assertEqual(opened[0]["plan"]["force_flat_at"],
                         expected_deadline.isoformat())
        self.assertAlmostEqual(opened[0]["plan"]["force_flat_ts"],
                               expected_deadline.timestamp())
        runner.store.record_diagnostic_batch(
            cohort_identity=cohort_identity, candidate_id=candidate_id,
            cursor_inserted_at=1.0,
            cursor_event_key=str(events[-1]["event_key"]),
            processed_events=len(events), rollups={session: {}},
            pending_sessions=[], decisions=result["decisions"],
            warmup_session=None, max_decisions=runner.config.max_decisions,
            account_batch=result["account_batch"])
        with closing(sqlite3.connect(self.shadow)) as db:
            db.row_factory = sqlite3.Row
            account = json.loads(db.execute(
                "SELECT state_json FROM diagnostic_accounts "
                "WHERE cohort_identity=? AND candidate_id=?",
                (cohort_identity, candidate_id)).fetchone()["state_json"])
            position_row = db.execute(
                "SELECT state_json FROM diagnostic_positions "
                "WHERE cohort_identity=? AND candidate_id=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (cohort_identity, candidate_id)).fetchone()
            self.assertIsNotNone(position_row)
            position = json.loads(position_row["state_json"])
            fills = [json.loads(row[0]) for row in db.execute(
                "SELECT fill_json FROM diagnostic_fills "
                "WHERE cohort_identity=? AND candidate_id=? ORDER BY created_at",
                (cohort_identity, candidate_id)).fetchall()]
        return account, position, fills

    def test_normal_close_deadline_survives_evaluate_setup_risk_and_book(self):
        close = datetime(2026, 1, 2, 21, 0, tzinfo=UTC)
        account, position, fills = self._persist_diagnostic_session(close=close)
        deadline = close - timedelta(minutes=12)
        self.assertEqual(account["closed_position_count"], 1)
        self.assertEqual(position["deadline"]["timestamp"], deadline.isoformat())
        self.assertEqual(position["exit_timestamp"], deadline.isoformat())
        self.assertEqual(position["exit_reason"], "session_force_flat")
        self.assertEqual(position["exit_reference"], 103.1)
        exit_fill = next(fill for fill in fills if fill["action"] == "exit_session_force_flat")
        self.assertEqual(exit_fill["evidence"]["boundary"], deadline.isoformat())
        self.assertEqual(exit_fill["evidence"]["bid"], 103.1)

    def test_early_close_clamps_deadline_and_uses_quote_at_exact_deadline(self):
        close = datetime(2026, 1, 2, 18, 0, tzinfo=UTC)
        account, position, fills = self._persist_diagnostic_session(close=close)
        deadline = close - timedelta(minutes=12)
        self.assertEqual(account["closed_position_count"], 1)
        self.assertEqual(position["deadline"]["timestamp"], deadline.isoformat())
        self.assertEqual(position["exit_timestamp"], deadline.isoformat())
        self.assertEqual(position["exit_reason"], "session_force_flat")
        self.assertEqual(position["exit_reference"], 103.1)
        exit_fill = next(fill for fill in fills if fill["action"] == "exit_session_force_flat")
        self.assertEqual(exit_fill["evidence"]["boundary"], deadline.isoformat())
        self.assertEqual(exit_fill["evidence"]["bid"], 103.1)

    def test_strict_missing_calendar_refuses_before_signal_evaluation(self):
        arm = self._arm()
        runner = self._runner()
        signal_at = datetime(2026, 1, 2, 15, 20, tzinfo=UTC)
        bars, quotes, _events = self._signal_window(signal_at=signal_at)
        kind, reason, payload, plan = self._evaluate(
            runner, arm, bars, quotes, bars[-1])
        self.assertEqual((kind, reason, plan), (
            "no_data", "exact broker calendar metadata unavailable", None))
        self.assertEqual(payload["calendar_source"],
                         "exact_calendar_metadata_missing")

    def test_stressed_cost_refusal_is_preserved_with_exact_calendar(self):
        arm = self._arm()
        runner = self._runner()
        close = datetime(2026, 1, 2, 21, 0, tzinfo=UTC)
        self._calendar(close=close)
        signal_at = datetime(2026, 1, 2, 15, 20, tzinfo=UTC)
        bars, quotes, _events = self._signal_window(
            signal_at=signal_at, narrow=True)
        kind, reason, _payload, plan = self._evaluate(
            runner, arm, bars, quotes, bars[-1])
        self.assertEqual((kind, reason, plan), (
            "reject", "stressed_cost_risk_limit", None))

    def test_force_flat_cutoff_blocks_entry_between_flat_and_latest_entry(self):
        arm = self._arm()
        runner = self._runner()
        close = datetime(2026, 1, 2, 18, 0, tzinfo=UTC)
        self._calendar(close=close)
        force_flat = close - timedelta(minutes=12)
        # Check both an event whose completed-bar availability is exactly the
        # force-flat boundary and one immediately after it.  Both precede the
        # close-relative latest-entry boundary (close - 5m).
        for signal_at in (
                force_flat - timedelta(minutes=1), force_flat):
            with self.subTest(signal_at=signal_at):
                bars, quotes, _events = self._signal_window(
                    signal_at=signal_at)
                kind, reason, payload, plan = self._evaluate(
                    runner, arm, bars, quotes, bars[-1])
                self.assertEqual((kind, reason, plan), (
                    "no_trade", "session force-flat cutoff reached", None))
                self.assertEqual(payload["force_flat_at"],
                                 force_flat.isoformat())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
