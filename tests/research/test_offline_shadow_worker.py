"""Focused contract checks for the ledger-free diagnostic evaluator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.diagnostic_accounts import (
    DiagnosticAccountError, content_digest, new_account_state,
    validate_account_state,
)
from research.diagnostic_shadow import build_diagnostic_cohort
from research.live_shadow import (
    ShadowConfig, ShadowError, ShadowRunner,
    _RecordedSessionCalendarSnapshot,
)


UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[2]
SESSION = "2026-09-08"
OPEN = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)


def _runtime() -> dict:
    config = json.loads((ROOT / "config.yaml").read_text(encoding="utf-8"))
    config["session"]["require_exact_calendar"] = True
    config["universe"]["symbols"] = ["SPY"]
    config["universe"]["max_symbols"] = 1
    return config


def _bar(index: int, *, close: float = 100.0,
         volume: float = 1000.0) -> dict:
    stamp = OPEN + timedelta(minutes=index)
    ended = stamp + timedelta(minutes=1)
    observed = ended + (timedelta(seconds=5) if index == 15 else timedelta())
    return {
        "event_key": f"bar:{index}", "event_type": "bar_1m",
        "symbol": "SPY", "timestamp": stamp.isoformat(),
        "as_of": ended.isoformat(), "observed_at": observed.isoformat(),
        "provider": "alpaca", "feed": "iex",
        "source_mode": "forward_observed", "open": 100.0,
        "high": max(100.5, close + 0.2), "low": min(99.5, close - 0.2),
        "close": close, "volume": volume,
    }


def _quote(at: datetime) -> dict:
    return {
        "event_key": "quote:signal", "event_type": "quote",
        "symbol": "SPY", "timestamp": at.isoformat(),
        "as_of": at.isoformat(), "observed_at": at.isoformat(),
        "provider": "alpaca", "feed": "iex",
        "source_mode": "forward_observed", "bid": 101.2, "ask": 101.4,
    }


class OfflineShadowWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="offline-shadow-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = ShadowConfig(
            root / "absent.csv", root / "absent-edge.sqlite3",
            root / "absent-shadow.sqlite3", diagnostic=True,
            diagnostic_include_ibr=True, runtime_config=_runtime(),
            runtime_config_path="/verified/config.yaml")

    def test_constructor_and_run_once_are_ledger_free_and_fail_closed(self):
        with patch("research.live_shadow.ShadowStore") as store, patch(
                "research.live_shadow._read_factory_rule_roots") as roots, patch(
                    "research.live_shadow._load_recorded_session_calendar") as calendar:
            worker = ShadowRunner.for_offline_diagnostic(self.config)
            self.assertIsNone(worker.store)
            self.assertEqual(worker._factory_roots, {})
            store.assert_not_called()
            roots.assert_not_called()
            with self.assertRaisesRegex(
                    ShadowError, "offline diagnostic worker"):
                worker.run_once()
            calendar.assert_not_called()

    def test_non_diagnostic_config_is_rejected_before_ledger_access(self):
        config = ShadowConfig(
            self.config.corpus_path, self.config.edge_db,
            self.config.shadow_db)
        with patch("research.live_shadow.ShadowStore") as store, patch(
                "research.live_shadow._read_factory_rule_roots") as roots:
            with self.assertRaisesRegex(ValueError, "requires diagnostic mode"):
                ShadowRunner.for_offline_diagnostic(config)
            store.assert_not_called()
            roots.assert_not_called()

    def test_supplied_fixture_evaluates_without_store_or_calendar_io(self):
        cohort = build_diagnostic_cohort(
            self.config.runtime_config or {}, code_identity="a" * 64,
            include_ibr=True)
        arm = next(
            item for item in cohort["arms"]
            if item["variant_id"] == "ibr.baseline")
        bars = [_bar(index) for index in range(15)]
        bars.append(_bar(15, close=101.2, volume=2000.0))
        event = bars[-1]
        available = datetime.fromisoformat(event["observed_at"])
        quote = _quote(available)
        raw_rows = [
            {"event_type": row["event_type"], "symbol": row["symbol"],
             "event_json": json.dumps(row)}
            for row in [*bars, quote]
        ]
        calendar = _RecordedSessionCalendarSnapshot(
            sessions=(SESSION,), bounds=((
                OPEN, datetime(2026, 9, 8, 20, 0, tzinfo=UTC)),))
        with patch("research.live_shadow.ShadowStore") as store, patch(
                "research.live_shadow._read_factory_rule_roots") as roots, patch(
                    "research.live_shadow._load_recorded_session_calendar",
                    side_effect=AssertionError("unexpected calendar IO")):
            worker = ShadowRunner.for_offline_diagnostic(self.config)
            grouped_bars, grouped_quotes, grouped_options = (
                worker._group_event_rows(raw_rows))
            views = worker._build_diagnostic_market_views(
                arm, [event], grouped_bars, grouped_quotes, grouped_options,
                calendar)
            account = new_account_state(
                cohort_identity=cohort["cohort_identity"],
                candidate_id=arm["candidate_id"], starting_cash=100_000.0)
            result = worker._evaluate_diagnostic_arm_snapshot(
                arm, {SESSION: [event]},
                {SESSION: (grouped_bars["SPY"], grouped_quotes["SPY"], ())},
                grouped_bars, grouped_quotes, grouped_options,
                {"account": account, "positions": []},
                calendar_snapshot=calendar, diagnostic_market_views=views)
            self.assertIsNone(result["error"], result)
            self.assertEqual(len(result["decisions"]), 1)
            self.assertEqual(result["account_batch"]["account"][
                "state"]["signal_sessions"], {"SPY": SESSION})
            store.assert_not_called()
            roots.assert_not_called()

    def test_signal_sessions_require_real_iso_dates_without_old_state_change(self):
        state = new_account_state(
            cohort_identity="cohort", candidate_id="candidate",
            starting_cash=100_000.0)
        self.assertNotIn("signal_sessions", state)
        self.assertEqual(validate_account_state(
            state, cohort_identity="cohort", candidate_id="candidate"), state)

        malformed = dict(state)
        malformed["signal_sessions"] = {"SPY": "2026-02-30"}
        body = dict(malformed)
        body.pop("state_digest", None)
        malformed["state_digest"] = content_digest(body)
        with self.assertRaisesRegex(
                DiagnosticAccountError, "signal sessions are invalid"):
            validate_account_state(
                malformed, cohort_identity="cohort",
                candidate_id="candidate")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
