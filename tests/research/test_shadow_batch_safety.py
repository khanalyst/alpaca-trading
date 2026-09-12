"""Crash-safety and cached market-view parity regressions for shadow polls."""

from __future__ import annotations

import csv
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research import live_shadow as live_shadow_module
from research.diagnostic_shadow import build_diagnostic_cohort
from research.edge_ledger import EdgeLedger
from research.live_shadow import InputConflict, ShadowConfig, ShadowRunner


UTC = timezone.utc
FIELDS = [
    "event_key", "event_type", "symbol", "timestamp", "as_of",
    "observed_at", "provider", "feed", "source_mode", "open", "high",
    "low", "close", "volume", "bid", "ask",
]


def _runtime_config() -> dict:
    return {
        "mode": "paper",
        "broker": {"provider": "alpaca", "data_feed": "iex",
                   "options_feed": "opra", "paper": True,
                   "allow_live": False},
        "session": {"timezone": "America/New_York",
                    "entries_regular_session_only": True,
                    "allow_exits_outside_session": True,
                    "require_exact_calendar": False,
                    "force_flat_minutes_before_close": 10,
                    "reject_new_entries_minutes_before_close": 5},
        "universe": {"symbols": ["SPY"], "asset_classes": ["us_equity"],
                     "min_price": 1.0, "max_symbols": 1, "denylist": []},
        "risk": {"risk_per_trade_pct": 0.5, "daily_loss_limit_pct": 2.0,
                 "max_open_risk_pct": 2.0, "max_concurrent_positions": 3,
                 "max_position_notional_pct": 25.0,
                 "max_gross_exposure_pct": 50.0,
                 "stressed_cost_scenario_bps": 25.0,
                 "max_stressed_cost_to_risk_ratio": 0.30},
        "execution": {"order_type": "market", "time_in_force": "day",
                      "max_slippage_bps": 50,
                      "max_market_data_age_seconds": 30,
                      "max_spread_bps": 100,
                      "strict_market_data": True},
        "costs": {"spread_bps": 4.0, "slippage_bps": 6.0,
                  "fee_bps": 0.5,
                  "provenance": "shadow_batch_safety_test"},
        "research": {"enabled": True,
                     "require_validated_variant": True},
    }


def _bar(event_key: str, stamp: datetime, *, symbol: str = "SPY",
         observed_at: datetime | None = None) -> dict[str, str]:
    available = observed_at or stamp + timedelta(minutes=1)
    return {
        "event_key": event_key, "event_type": "bar_1m", "symbol": symbol,
        "timestamp": stamp.isoformat(),
        "as_of": (stamp + timedelta(minutes=1)).isoformat(),
        "observed_at": available.isoformat(), "provider": "alpaca",
        "feed": "iex", "source_mode": "forward_observed",
        "open": "100", "high": "101", "low": "99", "close": "100",
        "volume": "1000", "bid": "", "ask": "",
    }


def _quote(event_key: str, stamp: datetime, observed_at: datetime,
           bid: str, ask: str) -> dict[str, str]:
    return {
        "event_key": event_key, "event_type": "quote", "symbol": "SPY",
        "timestamp": stamp.isoformat(), "as_of": stamp.isoformat(),
        "observed_at": observed_at.isoformat(), "provider": "alpaca",
        "feed": "iex", "source_mode": "forward_observed",
        "open": "", "high": "", "low": "", "close": "",
        "volume": "", "bid": bid, "ask": ask,
    }


class ShadowBatchTransactionSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.corpus = self.root / "recorded.csv"
        self.edge = self.root / "edge.sqlite3"
        self.shadow = self.root / "shadow.sqlite3"
        EdgeLedger(self.edge)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, rows: list[dict[str, str]]) -> None:
        with self.corpus.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def _append(self, row: dict[str, str]) -> None:
        with self.corpus.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=FIELDS).writerow(row)

    def _runner(self) -> ShadowRunner:
        return ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, max_events=100,
            max_decisions=100, max_workers=1))

    def test_retry_after_event_commit_before_offset_commit_is_idempotent(self):
        start = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
        first = _bar("bar-first", start)
        second = _bar("bar-second", start + timedelta(minutes=1))
        third = _bar("bar-third", start + timedelta(minutes=2))
        self._write([first, second])
        runner = self._runner()
        runner.store.save_source_offsets({})

        with patch.object(
                runner.store, "save_source_offsets",
                side_effect=RuntimeError("injected offset commit failure")):
            with self.assertRaisesRegex(RuntimeError, "offset commit failure"):
                runner.run_once()

        self.assertEqual(runner.store.event_count(), 2)
        self.assertEqual(runner.store.source_offsets(), {})

        retry = runner.run_once()
        source = str(self.corpus.resolve())
        self.assertEqual(retry["ingested_events"], 0)
        self.assertEqual(runner.store.event_count(), 2)
        self.assertEqual(runner.store.source_offsets(),
                         {source: self.corpus.stat().st_size})

        self._append(third)
        appended = runner.run_once()
        self.assertEqual(appended["ingested_events"], 1)
        self.assertEqual(runner.store.event_count(), 3)
        self.assertEqual(
            {row["event_key"] for row in runner.store.events()},
            {"bar-first", "bar-second", "bar-third"})

    def test_input_conflict_rolls_back_entire_batch_and_retry_is_deterministic(self):
        start = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
        existing = _bar("bar-existing", start + timedelta(minutes=1))
        new = _bar("bar-new", start, symbol="QQQ")
        conflict = {**existing, "close": "100.5"}
        self._write([new, conflict])
        runner = self._runner()
        runner.store.ingest_event(existing, max_events=100)
        runner.store.save_source_offsets({})

        with self.assertRaises(InputConflict):
            runner.run_once()

        self.assertEqual(runner.store.event_count(), 1)
        self.assertEqual(runner.store.source_offsets(), {})
        self.assertEqual(
            {row["event_key"] for row in runner.store.events()},
            {"bar-existing"})

        self._write([new, existing])
        retry = runner.run_once()
        stable_rows = [
            (row["event_key"], row["digest"], row["event_json"])
            for row in runner.store.events()
        ]
        stable_offsets = runner.store.source_offsets()
        self.assertEqual(retry["ingested_events"], 1)
        self.assertEqual({key for key, _digest, _payload in stable_rows},
                         {"bar-existing", "bar-new"})
        self.assertEqual(stable_offsets,
                         {str(self.corpus.resolve()): self.corpus.stat().st_size})

        no_op = runner.run_once()
        self.assertEqual(no_op["ingested_events"], 0)
        self.assertEqual(runner.store.source_offsets(), stable_offsets)
        self.assertEqual([
            (row["event_key"], row["digest"], row["event_json"])
            for row in runner.store.events()
        ], stable_rows)


class ShadowDiagnosticMarketViewParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.corpus = self.root / "recorded.csv"
        self.edge = self.root / "edge.sqlite3"
        self.shadow = self.root / "shadow.sqlite3"
        self.corpus.write_text("event_key,event_type\n", encoding="utf-8")
        EdgeLedger(self.edge)
        self.runtime = _runtime_config()
        cohort = build_diagnostic_cohort(
            self.runtime, code_identity="a" * 64)
        self.arm = next(
            deepcopy(arm) for arm in cohort["arms"]
            if arm["family"] == "opening_range_breakout" and
            arm["role"] == "baseline")
        self.runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, diagnostic=True,
            runtime_config=deepcopy(self.runtime),
            runtime_config_path="/mounted/config.yaml",
            max_events=100, max_decisions=1000, max_workers=1))
        self.calendar = live_shadow_module._load_recorded_session_calendar(
            self.corpus)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _set_views(self, views: dict | None) -> None:
        if views is None:
            try:
                del self.runner._worker_state.diagnostic_market_views
            except AttributeError:
                pass
        else:
            self.runner._worker_state.diagnostic_market_views = views

    def _quote_snapshot(self, event: dict, bars: dict, quotes: dict,
                        views: dict | None) -> dict:
        self._set_views(views)
        signal = {
            "symbol": "SPY", "direction": "long",
            "setup_type": "rule_probe",
            "signal_ts": datetime.fromisoformat(event["timestamp"]).timestamp(),
            "entry_price": 100.1, "stop_price": 99.0,
            "target_price": 102.0, "stop_distance": 1.1,
            "target_r": 1.7, "execution_profile": "shares",
        }
        with patch("research.live_shadow.generate_rule_signal",
                   return_value=signal), patch(
                       "research.live_shadow.build_setup_plan",
                       return_value=(None, "probe complete")):
            result = self.runner._evaluate(
                self.arm, event, bars, quotes, {},
                calendar_snapshot=self.calendar)
        self.assertEqual(result[:2], ("reject", "probe complete"))
        return result[2]["snapshot"]

    def test_cached_and_uncached_use_latest_visible_quote_correction(self):
        start = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
        previous = _bar("bar-previous", start)
        event = _bar("bar-signal", start + timedelta(minutes=1))
        quote_at = start + timedelta(minutes=1, seconds=50)
        old = _quote("quote-old", quote_at,
                     quote_at + timedelta(seconds=1), "99.8", "100.0")
        corrected = _quote("quote-corrected", quote_at,
                           quote_at + timedelta(seconds=7), "99.9", "100.1")
        future = _quote("quote-future", quote_at + timedelta(seconds=5),
                        start + timedelta(minutes=2, seconds=1),
                        "109", "110")
        bars = {"SPY": [previous, event]}
        quotes = {"SPY": [old, corrected, future]}
        views = self.runner._build_diagnostic_market_views(
            self.arm, [event], bars, quotes, {}, self.calendar)

        uncached = self._quote_snapshot(event, bars, quotes, None)
        cached = self._quote_snapshot(event, bars, quotes, views)

        self.assertEqual(cached, uncached)
        self.assertEqual(cached["bid"], 99.9)
        self.assertEqual(cached["ask"], 100.1)
        self.assertEqual(cached["quote_observed_at"],
                         corrected["observed_at"])

    def test_cached_and_uncached_delay_bar_correction_until_observed(self):
        start = datetime(2026, 9, 8, 13, 28, tzinfo=UTC)
        previous = _bar("bar-previous", start)
        original = _bar("bar-original", start + timedelta(minutes=1))
        corrected = _bar(
            "bar-corrected", start + timedelta(minutes=1),
            observed_at=start + timedelta(minutes=3, seconds=30))
        early = _bar("bar-early", start + timedelta(minutes=2))
        later = _bar("bar-later", start + timedelta(minutes=3))
        bars = {"SPY": [previous, original, corrected, early, later]}
        views = self.runner._build_diagnostic_market_views(
            self.arm, [early, later], bars, {}, {}, self.calendar)

        def evaluate(event: dict, cached_views: dict | None) -> tuple[str, ...]:
            observed: list[tuple[str, ...]] = []

            def signal(_symbol, stream, **_kwargs):
                observed.append(tuple(row["event_key"] for row in stream))
                return None

            self._set_views(cached_views)
            with patch("research.live_shadow.generate_rule_signal",
                       side_effect=signal):
                result = self.runner._evaluate(
                    self.arm, event, bars, {}, {},
                    calendar_snapshot=self.calendar)
            self.assertEqual(result[:2], ("no_trade", "no signal"))
            self.assertEqual(len(observed), 1)
            return observed[0]

        early_uncached = evaluate(early, None)
        early_cached = evaluate(early, views)
        later_uncached = evaluate(later, None)
        later_cached = evaluate(later, views)

        self.assertEqual(early_cached, early_uncached)
        self.assertEqual(later_cached, later_uncached)
        self.assertNotIn("bar-corrected", early_cached)
        self.assertIn("bar-corrected", later_cached)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
