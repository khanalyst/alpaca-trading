"""Funding provenance for the forward market-data recorder."""

import csv
import threading
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from research.record_flow import (
    MAX_COLLECTOR_WORKERS,
    FUNDING_FIELDS,
    FUNDING_FORECAST_SOURCE,
    FUNDING_REALIZED_SOURCE,
    LEGACY_FUNDING_FIELDS,
    Recorder,
    load_collector_config,
    load_collector_config_from_mapping,
)


INSTRUMENT = "BTC-USDT-SWAP"
SETTLEMENT = 1_800_000_000_000
NEXT_SETTLEMENT = SETTLEMENT + 8 * 3_600_000


def funding_rows(root: Path) -> tuple[list[str], list[dict[str, str]]]:
    files = list((root / "funding").glob("*.csv"))
    if not files:
        return [], []
    with files[0].open(newline="") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames or [], list(reader)


class FundingCaptureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_forecast_revisions_and_realized_rate_are_distinct_evidence(self):
        recorder = Recorder(self.root)
        forecast_calls = 0
        calls: list[tuple[str, dict]] = []

        def get(path: str, params: dict):
            nonlocal forecast_calls
            calls.append((path, params))
            if path == FUNDING_FORECAST_SOURCE:
                forecast_calls += 1
                revision = min(forecast_calls, 2)
                return [{
                    "instId": INSTRUMENT,
                    "fundingRate": f"0.000{revision}",
                    "fundingTime": str(SETTLEMENT),
                    "nextFundingTime": str(NEXT_SETTLEMENT),
                    # Upstream update time is deliberately constant: local
                    # observation time is the snapshot identity.
                    "ts": str(SETTLEMENT - 3_600_000),
                }]
            if path == FUNDING_REALIZED_SOURCE:
                return [{
                    "instId": INSTRUMENT,
                    "fundingRate": "0.00019",
                    "realizedRate": "0.00017",
                    "fundingTime": str(SETTLEMENT - 8 * 3_600_000),
                }]
            return []

        recorder.get = get
        observed_first = SETTLEMENT - 1_800_000
        observed_second = SETTLEMENT - 900_000
        with patch("research.record_flow.time.time", side_effect=[
                observed_first / 1000, (observed_first + 1) / 1000,
                observed_second / 1000, (observed_second + 1) / 1000,
                observed_second / 1000, (observed_second + 2) / 1000]):
            first = recorder.capture_hourly([INSTRUMENT], [])
            second = recorder.capture_hourly([INSTRUMENT], [])
            repeated = recorder.capture_hourly([INSTRUMENT], [])

        self.assertEqual(first["funding"], 2)
        self.assertEqual(second["funding"], 1)
        self.assertEqual(repeated["funding"], 0)
        fields, rows = funding_rows(self.root)
        self.assertEqual(fields, FUNDING_FIELDS)

        forecasts = [row for row in rows if row["status"] == "forecast"]
        realized = [row for row in rows if row["status"] == "realized"]
        self.assertEqual(len(forecasts), 2)
        self.assertEqual(
            {row["observed_at"] for row in forecasts},
            {str(observed_first), str(observed_second)},
        )
        self.assertEqual(
            {row["settlement_time"] for row in forecasts},
            {str(SETTLEMENT)},
        )
        self.assertEqual(
            {row["funding_rate"] for row in forecasts},
            {"0.0001", "0.0002"},
        )
        self.assertTrue(all(
            row["source"] == FUNDING_FORECAST_SOURCE for row in forecasts))

        self.assertEqual(len(realized), 1)
        self.assertEqual(realized[0]["source"], FUNDING_REALIZED_SOURCE)
        self.assertEqual(realized[0]["funding_rate"], "0.00017")
        self.assertEqual(realized[0]["realized_rate"], "0.00017")
        self.assertEqual(realized[0]["forecast_rate"], "")

        history_calls = [params for path, params in calls
                         if path == FUNDING_REALIZED_SOURCE]
        self.assertEqual(len(history_calls), 3)
        self.assertTrue(all(params == {"instId": INSTRUMENT, "limit": "100"}
                            for params in history_calls))

    def test_missing_realized_rate_is_not_replaced_with_forecast(self):
        recorder = Recorder(self.root)

        def get(path: str, params: dict):  # noqa: ARG001
            if path == FUNDING_REALIZED_SOURCE:
                return [{
                    "instId": INSTRUMENT,
                    "fundingRate": "0.123",
                    "realizedRate": "",
                    "fundingTime": str(SETTLEMENT),
                }]
            return []

        recorder.get = get
        written = recorder.capture_hourly([INSTRUMENT], [])

        self.assertEqual(written["funding"], 0)
        self.assertEqual(funding_rows(self.root), ([], []))

    def test_legacy_funding_csv_is_upgraded_without_losing_legacy_fields(self):
        recorder = Recorder(self.root)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.root / "funding" / f"{day}.csv"
        path.parent.mkdir(parents=True)
        legacy = {
            "ts": str(SETTLEMENT),
            "inst_id": INSTRUMENT,
            "funding_rate": "0.00009",
            "next_funding_time": str(NEXT_SETTLEMENT),
        }
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=LEGACY_FUNDING_FIELDS)
            writer.writeheader()
            writer.writerow(legacy)

        forecast = {
            **legacy,
            "observed_at": str(SETTLEMENT - 900_000),
            "settlement_time": str(SETTLEMENT),
            "source": FUNDING_FORECAST_SOURCE,
            "status": "forecast",
            "forecast_rate": legacy["funding_rate"],
            "realized_rate": "",
        }
        self.assertEqual(recorder.append("funding", FUNDING_FIELDS,
                                         [forecast]), 1)

        fields, rows = funding_rows(self.root)
        self.assertEqual(fields[:4], LEGACY_FUNDING_FIELDS)
        self.assertEqual(fields, FUNDING_FIELDS)
        self.assertEqual(len(rows), 2)
        for field in LEGACY_FUNDING_FIELDS:
            self.assertEqual(rows[0][field], legacy[field])
        self.assertEqual(rows[0]["source"], "")
        self.assertEqual(rows[0]["status"], "")
        self.assertEqual(rows[1]["status"], "forecast")

    def test_realized_rows_are_deduplicated_across_day_partitions(self):
        old_path = self.root / "funding" / "2026-01-01.csv"
        old_path.parent.mkdir(parents=True)
        realized = {
            "ts": str(SETTLEMENT),
            "inst_id": INSTRUMENT,
            "funding_rate": "0.00017",
            "next_funding_time": "",
            "observed_at": str(SETTLEMENT + 1),
            "settlement_time": str(SETTLEMENT),
            "source": FUNDING_REALIZED_SOURCE,
            "status": "realized",
            "forecast_rate": "0.00019",
            "realized_rate": "0.00017",
        }
        with old_path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FUNDING_FIELDS)
            writer.writeheader()
            writer.writerow(realized)

        recorder = Recorder(self.root)
        self.assertEqual(
            recorder.append("funding", FUNDING_FIELDS, [realized]), 0)
        self.assertEqual(len(list((self.root / "funding").glob("*.csv"))), 1)


class CollectorIsolationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_collector_settings_are_separate_from_active_universe(self):
        config = self.root / "config.yaml"
        config.write_text(
            "mode: demo\n"
            "universe:\n"
            "  top_n: 2\n"
            "research:\n"
            "  collector:\n"
            "    out: collected\n"
            "    top_n: 80\n"
            "    workers: 7\n",
            encoding="utf-8")

        settings = load_collector_config(config)

        self.assertEqual(settings["top_n"], 80)
        self.assertEqual(settings["workers"], 7)
        self.assertEqual(settings["out"], Path("collected"))

    def test_book_reads_can_parallelize_but_append_is_serialized(self):
        recorder = Recorder(self.root, workers=4)
        entered = threading.Barrier(4)
        worker_ids = set()
        append_ids = []

        def fake_book(inst_id):
            worker_ids.add(threading.get_ident())
            entered.wait(timeout=2)
            return {"ts": 1, "inst_id": inst_id}

        def fake_append(series, fields, rows):  # noqa: ARG001
            append_ids.append(threading.get_ident())
            return len(rows)

        recorder.order_book = fake_book
        recorder.append = fake_append
        written = recorder.capture_books(["A", "B", "C", "D"])

        self.assertEqual(written, 4)
        self.assertGreaterEqual(len(worker_ids), 2)
        self.assertEqual(append_ids, [threading.get_ident()])

    def test_worker_limit_is_bounded(self):
        recorder = Recorder(self.root, workers=MAX_COLLECTOR_WORKERS * 4)
        self.assertEqual(recorder.workers, MAX_COLLECTOR_WORKERS)

    def test_unknown_collector_setting_is_rejected(self):
        with self.assertRaises(ValueError):
            load_collector_config_from_mapping({"place_orders": True})


if __name__ == "__main__":
    unittest.main()
