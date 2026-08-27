"""Focused regressions for the one-pass research corpus preprocessor."""

from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest

from deploy.research_dataset import build_views


HEADER = [
    "event_type", "provider", "feed", "symbol", "timestamp",
    "observed_at", "as_of", "open", "high", "low", "close", "volume",
    "bid", "ask", "bid_size", "ask_size",
]


def _write_partition(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)


def _outputs(root: Path) -> dict[str, Path]:
    return {name: root / f"{name}.jsonl"
            for name in ("normalized", "bars", "quotes", "options", "replay")}


class ResearchDatasetStreamingTests(unittest.TestCase):
    def test_partition_window_streams_in_order_with_global_source_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            partitions = root / "sessions"
            common = {
                "event_type": "bar_1m", "provider": "alpaca", "feed": "iex",
                "observed_at": "2026-01-02T14:30:00+00:00",
                "as_of": "2026-01-02T14:30:00+00:00",
                "open": 100, "high": 101, "low": 99, "close": 100,
                "volume": 10,
            }
            _write_partition(partitions / "market-2026-01-03.csv", [{
                **common, "symbol": "THIRD",
                "timestamp": "2026-01-03T14:30:00+00:00",
                "observed_at": "2026-01-03T14:30:00+00:00",
                # The second selected data row is quarantined. Its row number
                # must match the old one-header merged stream: 3, not 2.
                "as_of": "2026-01-03T14:31:00+00:00",
            }])
            _write_partition(partitions / "market-2026-01-01.csv", [{
                **common, "symbol": "FIRST",
                "timestamp": "2026-01-01T14:30:00+00:00",
            }])
            _write_partition(partitions / "market-2026-01-02.csv", [{
                **common, "symbol": "SECOND",
                "timestamp": "2026-01-02T14:30:00+00:00",
            }])
            outputs = _outputs(root)

            report = build_views(
                partition_root=partitions, session_window=2,
                input_format="csv", normalized=outputs["normalized"],
                bars=outputs["bars"], quotes=outputs["quotes"],
                options=outputs["options"], replay=outputs["replay"])

            rows = [json.loads(line) for line in
                    outputs["normalized"].read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["symbol"] for row in rows], ["SECOND"])
            self.assertEqual(report["first_source_row"], 3)
            self.assertEqual(report["last_source_row"], 3)
            self.assertEqual(report["kept_rows"], 1)
            self.assertEqual(report["view_counts"]["bars"], 1)

    def test_partition_header_mismatch_fails_before_writing_views(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            partitions = root / "sessions"
            _write_partition(partitions / "market-2026-01-01.csv", [])
            second = partitions / "market-2026-01-02.csv"
            second.write_text("event_type,symbol,unexpected\n", encoding="utf-8")
            outputs = _outputs(root)

            with self.assertRaisesRegex(ValueError, "headers do not match"):
                build_views(
                    partition_root=partitions, input_format="csv",
                    normalized=outputs["normalized"], bars=outputs["bars"],
                    quotes=outputs["quotes"], options=outputs["options"],
                    replay=outputs["replay"])
            self.assertFalse(outputs["normalized"].exists())

    def test_vehicle_calendar_and_projections_are_fused_without_quotes_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "market.jsonl"
            stamp = "2026-01-05T14:30:00+00:00"
            rows = [
                {"kind": "bar", "provider": "alpaca", "feed": "iex",
                 "symbol": "SPY", "timestamp": stamp, "observed_at": stamp,
                 "as_of": stamp, "open": 100, "high": 101, "low": 99,
                 "close": 100, "volume": 10},
                {"kind": "quote", "provider": "alpaca", "feed": "iex",
                 "symbol": "SPY", "timestamp": stamp, "observed_at": stamp,
                 "as_of": stamp, "bid": 99.9, "ask": 100.1},
                {"kind": "option_snapshot", "provider": "alpaca",
                 "feed": "indicative", "symbol": "SPY260116C00100000",
                 "timestamp": stamp, "observed_at": stamp, "as_of": stamp},
            ]
            source.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8")
            outputs = _outputs(root)

            report = build_views(
                source, input_format="jsonl", normalized=outputs["normalized"],
                bars=outputs["bars"], quotes=None, options=outputs["options"],
                replay=outputs["replay"], selected_vehicles="equity",
                agent_config={"session": {"require_exact_calendar": False}})

            self.assertFalse(outputs["quotes"].exists())
            self.assertEqual(report["kept_rows"], 3)
            self.assertEqual(report["vehicle_filter"]["excluded_option_rows"], 1)
            self.assertEqual(report["view_counts"], {
                "normalized": 2, "bars": 1, "quotes": 1,
                "options": 0, "replay": 1,
            })
            self.assertEqual(len(outputs["normalized"].read_text().splitlines()), 2)
            self.assertEqual(len(outputs["replay"].read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
