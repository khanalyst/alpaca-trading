"""Focused regressions for poll-local shadow calendar and duration snapshots."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import pickle
import tempfile
import threading
import unittest
from unittest.mock import patch

from deploy.recorder import INDEX_NAME
from deploy.recorder_market import _event_key
from research import live_shadow
from research.live_shadow import ShadowConfig, ShadowRunner


UTC = timezone.utc
SESSION = "2026-01-02"


class ShadowCalendarPollTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.corpus = self.root / "recorded.csv"
        self.edge = self.root / "edge.sqlite3"
        self.shadow = self.root / "shadow.sqlite3"
        self.index = self.root / INDEX_NAME

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _candidate() -> dict:
        return {
            "candidate_id": "calendar-candidate",
            "variant_id": "ibr.baseline",
            "strategy_id": "ibr",
            "vehicle": "equity",
            "status": "shadow",
            "config": {
                "broker": {"provider": "alpaca", "data_feed": "iex"},
                "session": {"require_exact_calendar": True},
                "strategy": {"id": "ibr"},
            },
        }

    @staticmethod
    def _bar() -> dict:
        stamp = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
        ended = datetime(2026, 1, 2, 15, 1, tzinfo=UTC)
        return {
            "event_key": _event_key("bar_1m", "SPY", stamp.isoformat()),
            "event_type": "bar_1m",
            "symbol": "SPY",
            "timestamp": stamp.isoformat(),
            "as_of": ended.isoformat(),
            "observed_at": ended.isoformat(),
            "provider": "alpaca",
            "feed": "iex",
            "source_mode": "forward_observed",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000,
        }

    def _runner(self, *, shadow: Path | None = None) -> ShadowRunner:
        return ShadowRunner(ShadowConfig(
            self.corpus, self.edge, shadow or self.shadow,
            max_events=100, max_decisions=100, max_workers=2))

    def _write_calendar(
            self, close: str = "2026-01-02T21:00:00+00:00", *,
            size: int | None = None) -> None:
        payload = json.dumps({
            "session_calendar": {
                SESSION: {
                    "open": "2026-01-02T14:30:00+00:00",
                    "close": close,
                    "source": "alpaca_calendar",
                },
                "2026-01-03": {
                    "status": "closed", "source": "alpaca_calendar",
                },
            },
        })
        if size is not None:
            if len(payload.encode("utf-8")) > size:
                raise ValueError("calendar fixture exceeds requested size")
            payload += " " * (size - len(payload.encode("utf-8")))
        self.index.write_text(payload, encoding="utf-8")

    def _write_bar(self) -> None:
        row = self._bar()
        with self.corpus.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)

    def test_poll_reads_index_once_across_catalog_worker_and_replay_window(self):
        self._write_calendar()
        self._write_bar()
        runner = self._runner()
        reads = 0
        original_read_text = Path.read_text

        def counted_read_text(path: Path, *args, **kwargs):
            nonlocal reads
            if path == self.index:
                reads += 1
            return original_read_text(path, *args, **kwargs)

        with patch.object(
                live_shadow, "_read_candidates",
                return_value=[self._candidate()]), \
                patch.object(
                    runner, "_evaluate",
                    return_value=("no_trade", "no signal", {
                        "session_date": SESSION}, None)) as evaluate, \
                patch.object(Path, "read_text", new=counted_read_text):
            result = runner.run_once()

        self.assertEqual(reads, 1)
        self.assertEqual(result["events"], 1)
        evaluate.assert_called_once()
        self.assertIn(SESSION, runner.store.session_catalog())

    def test_one_snapshot_serves_repeated_direct_consumers(self):
        self._write_calendar(size=1_048_704)
        runner = self._runner()
        candidate = self._candidate()
        bar = self._bar()
        closing_bar = dict(bar, timestamp="2026-01-02T20:59:00+00:00",
                           as_of="2026-01-02T21:00:00+00:00",
                           observed_at="2026-01-02T21:00:00+00:00")
        reads = 0
        bytes_read = 0
        original_read_text = Path.read_text

        def counted_read_text(path: Path, *args, **kwargs):
            nonlocal reads, bytes_read
            payload = original_read_text(path, *args, **kwargs)
            if path == self.index:
                reads += 1
                bytes_read += len(payload.encode("utf-8"))
            return payload

        with patch.object(Path, "read_text", new=counted_read_text):
            snapshot = live_shadow._load_recorded_session_calendar(self.corpus)
            for _ in range(96):
                runner._evaluate(
                    candidate, bar, {"SPY": [bar]}, {}, {},
                    calendar_snapshot=snapshot)
            for _ in range(24):
                self.assertTrue(runner._diagnostic_session_complete(
                    candidate, SESSION, [closing_bar],
                    calendar_snapshot=snapshot))
            window = runner._replay_session_window(
                candidate, SESSION, [closing_bar], (), (),
                calendar_snapshot=snapshot)
            catalog = live_shadow._recorded_session_calendar(
                self.corpus, calendar_snapshot=snapshot)

        self.assertEqual(reads, 1)
        self.assertEqual(bytes_read, 1_048_704)
        self.assertEqual(window["calendar_bounds"], snapshot.get(SESSION))
        self.assertEqual(catalog[SESSION], snapshot.get(SESSION))

    def test_each_poll_reloads_a_corrected_calendar(self):
        self._write_calendar()
        self._write_bar()
        runner = self._runner()
        candidate = self._candidate()
        reads = 0
        original_read_text = Path.read_text

        def counted_read_text(path: Path, *args, **kwargs):
            nonlocal reads
            if path == self.index:
                reads += 1
            return original_read_text(path, *args, **kwargs)

        with patch.object(live_shadow, "_read_candidates", return_value=[candidate]), \
                patch.object(
                    runner, "_evaluate",
                    return_value=("no_trade", "no signal", {
                        "session_date": SESSION}, None)), \
                patch.object(runner, "_replay", return_value=True) as replay, \
                patch.object(Path, "read_text", new=counted_read_text):
            runner.run_once()
            replay.assert_not_called()
            self._write_calendar("2026-01-02T15:01:00+00:00")
            runner.run_once()

        self.assertEqual(reads, 2)
        replay.assert_called_once()
        window = replay.call_args.kwargs["replay_window"]
        self.assertEqual(
            window["calendar_close"],
            datetime(2026, 1, 2, 15, 1, tzinfo=UTC))

    def test_pickled_parallel_workers_keep_one_calendar_after_sidecar_change(self):
        self._write_calendar()
        snapshot = pickle.loads(pickle.dumps(
            live_shadow._load_recorded_session_calendar(self.corpus)))
        ready = threading.Barrier(3)
        release = threading.Event()

        def consume() -> tuple[datetime | None, str]:
            ready.wait(timeout=5)
            self.assertTrue(release.wait(timeout=5))
            return live_shadow._session_close(
                self.corpus, SESSION, require_exact_calendar=True,
                calendar_snapshot=snapshot)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(consume) for _ in range(2)]
            ready.wait(timeout=5)
            self._write_calendar("2026-01-02T18:00:00+00:00")
            release.set()
            observed = [future.result(timeout=5) for future in futures]

        self.assertEqual(observed, [
            (datetime(2026, 1, 2, 21, 0, tzinfo=UTC),
             "recorder_alpaca_calendar"),
        ] * 2)
        self.assertEqual(
            live_shadow._session_close(
                self.corpus, SESSION, require_exact_calendar=True)[0],
            datetime(2026, 1, 2, 18, 0, tzinfo=UTC))

    def test_missing_malformed_and_contradictory_exact_calendar_fail_closed(self):
        runner = self._runner()
        candidate = self._candidate()
        bar = self._bar()
        cases = {
            "missing": None,
            "malformed": "{not-json",
            "contradictory": json.dumps({
                "session_calendar": {SESSION: {
                    "open": "2026-01-02T14:30:00+00:00",
                    "close": "2026-01-02T14:00:00+00:00",
                    "source": "alpaca_calendar",
                }},
            }),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                self.index.unlink(missing_ok=True)
                if payload is not None:
                    self.index.write_text(payload, encoding="utf-8")
                snapshot = live_shadow._load_recorded_session_calendar(
                    self.corpus)
                close, source = live_shadow._session_close(
                    self.corpus, SESSION, require_exact_calendar=True,
                    calendar_snapshot=snapshot)
                self.assertIsNone(close)
                self.assertEqual(source, "exact_calendar_metadata_missing")
                kind, reason, decision_payload, plan = runner._evaluate(
                    candidate, bar, {"SPY": [bar]}, {}, {},
                    calendar_snapshot=snapshot)
                self.assertEqual((kind, reason, plan), (
                    "no_data", "exact broker calendar metadata unavailable",
                    None))
                self.assertEqual(
                    decision_payload["calendar_source"],
                    "exact_calendar_metadata_missing")

    def test_worker_calendar_context_is_removed_after_exception(self):
        self._write_calendar()
        snapshot = live_shadow._load_recorded_session_calendar(self.corpus)
        runner = self._runner()
        candidate = self._candidate()
        bar = self._bar()
        with patch.object(runner, "_evaluate", side_effect=RuntimeError("boom")):
            result = runner._evaluate_arm_snapshot(
                candidate, {SESSION: (bar,)},
                {SESSION: ((bar,), (), ())}, {"SPY": (bar,)}, {}, {},
                ([], {}, 0.0), snapshot)

        self.assertIn("RuntimeError: boom", result["error"])
        self.assertFalse(hasattr(runner._worker_state, "calendar_snapshot"))
        self.assertFalse(hasattr(runner._worker_state, "portfolios"))

    def test_duration_includes_coverage_reads_on_both_return_paths(self):
        for candidates in ([], [self._candidate()]):
            with self.subTest(candidates=bool(candidates)):
                shadow = self.root / (
                    "with-candidate.sqlite3" if candidates else
                    "without-candidate.sqlite3")
                runner = self._runner(shadow=shadow)
                clock = {"now": 100.0}
                original_coverage = runner._diagnostic_coverage

                def delayed_coverage(*args, **kwargs):
                    clock["now"] += 4.25
                    return original_coverage(*args, **kwargs)

                with patch.object(
                        live_shadow, "_read_candidates",
                        return_value=candidates), \
                        patch.object(
                            live_shadow.time, "monotonic",
                            side_effect=lambda: clock["now"]), \
                        patch.object(
                            runner, "_diagnostic_coverage",
                            side_effect=delayed_coverage):
                    result = runner.run_once()

                self.assertEqual(result["poll_duration_seconds"], 4.25)
                self.assertEqual(
                    result["diagnostic_shadow"]["poll_duration_seconds"],
                    4.25)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
