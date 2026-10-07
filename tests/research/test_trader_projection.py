from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest

from deploy.trader_projection import (
    ProjectionBudgetError, ProjectionLimits, ProjectionError, compact_rows,
    project_corpus,
)
from research.live_shadow import _compact_shadow_rows


FIELDS = [
    "event_key", "event_type", "provider", "feed", "symbol", "timestamp",
    "observed_at", "as_of", "session_open", "session_close", "open", "high",
    "low", "close", "volume", "bid", "ask", "bid_size", "ask_size",
]


def _partition(path: Path, day: str, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    opening = f"{day}T14:30:00+00:00"
    closing = f"{day}T21:00:00+00:00"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({"session_open": opening, "session_close": closing,
                             "provider": "alpaca", "feed": "iex", **row})


def _bar(day: str, key: str, minute: int = 31) -> dict:
    stamp = f"{day}T14:{minute:02d}:00+00:00"
    return {"event_key": key, "event_type": "bar_1m", "symbol": "SPY",
            "timestamp": stamp, "observed_at": f"{day}T14:{minute:02d}:05+00:00",
            "as_of": stamp, "open": "100", "high": "101", "low": "99",
            "close": "100", "volume": "10"}


def _quote(day: str, key: str, second: int, *, minute: int = 30,
           observed_second: int | None = None) -> dict:
    stamp = f"{day}T14:{minute:02d}:{second:02d}+00:00"
    observed = observed_second if observed_second is not None else second + 1
    return {"event_key": key, "event_type": "quote", "symbol": "SPY",
            "timestamp": stamp, "observed_at": f"{day}T14:{minute:02d}:{observed:02d}+00:00",
            "as_of": stamp, "bid": "100", "ask": "100.10",
            "bid_size": "100", "ask_size": "100"}


class TraderProjectionTests(unittest.TestCase):
    def _config(self):
        return {"broker": {"provider": "alpaca", "data_feed": "iex"},
                "session": {"require_exact_calendar": True}}

    def test_two_cutoffs_keep_latest_quote_when_second_has_no_new_quote(self):
        delayed_last = _quote("2026-01-05", "q-last", 59,
                              observed_second=59)
        delayed_last["observed_at"] = "2026-01-05T14:31:06+00:00"
        rows = [
            _quote("2026-01-05", "q-first", 1),
            _bar("2026-01-05", "bar-first", 31),
            _quote("2026-01-05", "q-middle", 20),
            _bar("2026-01-05", "bar-second", 32),
            delayed_last,
        ]
        # q-middle is visible at both bar cutoffs but is neither minute edge.
        projected = compact_rows(rows)
        self.assertEqual({row["event_key"] for row in projected},
                         {"q-first", "q-middle", "q-last",
                          "bar-first", "bar-second"})

    def test_selection_preserves_bars_and_matches_shadow_visible_quote_subset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [_bar("2026-01-05", "bar-a"),
                    _quote("2026-01-05", "q-first", 1),
                    _quote("2026-01-05", "q-middle", 20, observed_second=40),
                    _quote("2026-01-05", "q-last", 59),
                    _bar("2026-01-05", "bar-revision", 32)]
            # The second session makes the chronological 70/30 split valid.
            _partition(root / "sessions/market-2026-01-05.csv", "2026-01-05", rows)
            _partition(root / "sessions/market-2026-01-06.csv", "2026-01-06",
                        [_bar("2026-01-06", "bar-next"),
                         _quote("2026-01-06", "q-next", 1)])
            output = root / "out"
            result = project_corpus(
                root, self._config(), output,
                limits=ProjectionLimits(disk_floor_bytes=1,
                                         max_retained_rows=1000))
            projected = [json.loads(line) for line in
                         (output / "compact.jsonl").read_text().splitlines()]
            self.assertEqual(result["status"], "complete")
            self.assertEqual({row["event_key"] for row in projected
                              if row.get("event_type") == "bar_1m"},
                             {"bar-a", "bar-revision", "bar-next"})
            self.assertEqual({row["event_key"] for row in projected
                              if row.get("event_type") == "quote"},
                             {"q-first", "q-middle", "q-last", "q-next"})
            shadow = _compact_shadow_rows([
                {**row, "kind": "quote" if row["event_type"] == "quote" else "bar"}
                for row in rows])
            shadow_ids = {row["event_key"] for row in shadow
                          if row.get("event_type") == "quote"}
            projected_ids = {row["event_key"] for row in projected
                             if row.get("event_type") == "quote"}
            self.assertTrue(shadow_ids <= projected_ids)
            self.assertEqual(projected[0]["observed_at"],
                             "2026-01-05T14:30:02+00:00")
            self.assertTrue((output / "manifest.json").is_file())
            self.assertGreater(result["compact_bytes"], 0)
            self.assertEqual(len(result["compact_sha256"]), 64)
            self.assertEqual(set(result["artifacts"]), {
                "rawcostsfit.json", "validation.json", "calibration.json"})

    def test_raw_cost_count_is_unthinned_and_same_feed_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for day in ("2026-01-05", "2026-01-06"):
                _partition(root / "sessions" / f"market-{day}.csv", day,
                            [_quote(day, f"{day}-q-{index}", index)
                             for index in (1, 2, 3, 4)])
            result = project_corpus(root, self._config(), root / "out",
                                    limits=ProjectionLimits(disk_floor_bytes=1,
                                                             max_retained_rows=1000))
            self.assertEqual(result["raw_quote_rows"], 8)
            self.assertEqual(result["raw_quote_rows_fit"], 4)
            self.assertEqual(result["raw_quote_rows_validation"], 4)
            self.assertEqual(json.loads((root / "out/rawcostsfit.json").read_text())[
                "measured"]["quote_rows_seen"], 4)

            bad = root / "bad"
            _partition(bad / "sessions/market-2026-01-05.csv", "2026-01-05",
                       [_quote("2026-01-05", "bad-q", 1)])
            row = _quote("2026-01-06", "bad-q2", 2)
            row["feed"] = "sip"
            _partition(bad / "sessions/market-2026-01-06.csv", "2026-01-06", [row])
            with self.assertRaises(ProjectionError):
                project_corpus(bad, self._config(), bad / "out",
                               limits=ProjectionLimits(disk_floor_bytes=1,
                                                        max_retained_rows=1000))

    def test_budget_guard_fails_before_retained_growth_and_leaves_no_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _partition(root / "sessions/market-2026-01-05.csv", "2026-01-05",
                        [_bar("2026-01-05", "bar")])
            _partition(root / "sessions/market-2026-01-06.csv", "2026-01-06",
                        [_bar("2026-01-06", "bar-next")])
            output = root / "out"
            with self.assertRaises(ProjectionBudgetError):
                project_corpus(root, self._config(), output,
                               limits=ProjectionLimits(max_retained_rows=1,
                                                        disk_floor_bytes=1))
            self.assertFalse((output / "manifest.json").exists())
            failure = json.loads((output / "failure.json").read_text())
            self.assertEqual(failure["status"], "failed_partial")

    def test_stable_selected_markers_exclude_live_status_and_support_marker_spelling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            day = "2026-01-05"
            partition = sessions / f"market-{day}.csv"
            # No per-row session boundaries: the selected marker supplies the
            # strict broker calendar and source mode.
            _partition(partition, day, [_bar(day, "bar")])
            for key in ("session_open", "session_close"):
                lines = partition.read_text(encoding="utf-8").splitlines()
                header = lines[0].split(",")
                index = header.index(key)
                for line_index in range(1, len(lines)):
                    fields = lines[line_index].split(",")
                    fields[index] = ""
                    lines[line_index] = ",".join(fields)
                partition.write_text("\n".join(lines) + "\n", encoding="utf-8")
            (sessions / f"market-{day}.calendar.json").write_text(json.dumps({
                "schema": "recorder-partition-calendar.v1",
                "partition": partition.name,
                "open": f"{day}T14:30:00+00:00",
                "close": f"{day}T21:00:00+00:00",
                "source": "alpaca_calendar",
            }), encoding="utf-8")
            (sessions / f"market-{day}.source.json").write_text(json.dumps({
                "schema": "recorder-partition-source.v1",
                "partition": partition.name,
                "source_mode": "historical_backfill",
            }), encoding="utf-8")
            (root / ".recorder-index.json").write_text(json.dumps({
                "session_calendar": {"2099-01-01": {"watermark": "ignored"}},
                "partition_sources": {}, "watermark": "mutable",
            }), encoding="utf-8")
            (root / ".recorder-status.json").write_text(
                json.dumps({"status": "recording", "heartbeat": 1}),
                encoding="utf-8")
            # Add a second selected session for the fit/validation split.
            _partition(sessions / "market-2026-01-06.csv", "2026-01-06",
                       [_bar("2026-01-06", "bar-next")])
            result = project_corpus(
                root, self._config(), root / "out",
                limits=ProjectionLimits(disk_floor_bytes=1,
                                         max_retained_rows=1000))
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["source_csv_binding"]["before"],
                             result["source_csv_binding"]["after"])
            bound_paths = [item["path"] for item in
                           result["source_csv_binding"]["after"]]
            self.assertNotIn(str((root / ".recorder-status.json").resolve()),
                             bound_paths)
            self.assertTrue(any(path.endswith("market-2026-01-05.calendar.json")
                                for path in bound_paths))

    def test_option_rows_are_filtered_before_equity_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            option = {"event_key": "option", "event_type": "option_snapshot",
                      "symbol": "SPY", "timestamp": "2026-01-05T14:30:00+00:00",
                      "observed_at": "2026-01-05T14:30:01+00:00",
                      "as_of": "2026-01-05T14:30:00+00:00"}
            _partition(root / "sessions/market-2026-01-05.csv", "2026-01-05",
                        [_bar("2026-01-05", "bar"), option])
            _partition(root / "sessions/market-2026-01-06.csv", "2026-01-06",
                        [_bar("2026-01-06", "bar-next")])
            result = project_corpus(root, self._config(), root / "out",
                                    limits=ProjectionLimits(disk_floor_bytes=1,
                                                             max_retained_rows=1000))
            self.assertEqual(result["option_rows_skipped"], 1)
            rows = [json.loads(line) for line in
                    (root / "out/compact.jsonl").read_text().splitlines()]
            self.assertNotIn("option", {row.get("event_key") for row in rows})


if __name__ == "__main__":
    unittest.main()
