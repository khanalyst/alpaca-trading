"""Dependency-light checks for the paper deployment boundary."""

from __future__ import annotations

import json
import csv
import io
from contextlib import closing
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

try:
    from deploy import dashboard, health, recorder, scheduler, scheduler_output
except ModuleNotFoundError as exc:  # dependency-independent local test runs
    if exc.name == "yaml":
        raise unittest.SkipTest("PyYAML is supplied by the deployment lockfile")
    raise
import report
from agent.alpaca_provider import AlpacaProvider, AlpacaSession


class _MarketFake:
    def __init__(self, feed=None):
        self.seen = []
        self.data_feed = feed

    def bars(self, symbols, *, start, end, feed, **kwargs):
        self.seen.append(("bars", feed))
        return {"SPY": [SimpleNamespace(
            timestamp=datetime(2026, 8, 8, 13, 30, tzinfo=timezone.utc),
            open=100, high=101, low=99, close=100.5, volume=10)]}

    def quotes(self, symbols, *, start, end, feed):
        self.seen.append(("quotes", feed))
        return {"SPY": [SimpleNamespace(
            timestamp=datetime(2026, 8, 8, 13, 30, 1, tzinfo=timezone.utc),
            bid=100, ask=101, last=100.5)]}


class _WindowFake(_MarketFake):
    def __init__(self):
        super().__init__()
        self.starts = []

    def bars(self, symbols, *, start, end, feed, **kwargs):
        self.starts.append(start)
        return super().bars(symbols, start=start, end=end, feed=feed, **kwargs)

    def quotes(self, symbols, *, start, end, feed):
        self.starts.append(start)
        return super().quotes(symbols, start=start, end=end, feed=feed)


class _MissingQuoteTimestampFake(_MarketFake):
    def quotes(self, symbols, *, start, end, feed):
        self.seen.append(("quotes", feed))
        return {"SPY": [SimpleNamespace(
            timestamp=None, bid=100, ask=101, last=100.5)]}


class _FutureBarFake(_MarketFake):
    def bars(self, symbols, *, start, end, feed, **kwargs):
        self.seen.append(("bars", feed))
        return {"SPY": [SimpleNamespace(
            timestamp=end + timedelta(minutes=1),
            open=100, high=101, low=99, close=100.5, volume=10)]}


class _OptionFake(_MarketFake):
    def __init__(self):
        super().__init__(feed="iex")
        self.option_calls = []

    def option_candidates(self, symbol, *, now, underlying_price, feed,
                          min_dte, max_dte):
        self.option_calls.append((symbol, underlying_price, feed,
                                  min_dte, max_dte))
        timestamp = datetime(2026, 8, 8, 13, 30, 2, tzinfo=timezone.utc)
        expiration = datetime.now(timezone.utc).date() + timedelta(days=30)
        rows = []
        for right in ("call", "put"):
            for index in range(6):
                strike = 100 + index if right == "call" else 100 - index
                rows.append({
                    "symbol": f"SPY{expiration:%y%m%d}{right[0].upper()}{int(strike * 1000):08d}",
                    "underlying": "SPY", "expiration": expiration.isoformat(),
                    "strike": strike, "right": right, "multiplier": 100,
                    "timestamp": timestamp, "bid": 1 + index / 10,
                    "ask": 1.1 + index / 10, "bid_size": 10 + index,
                    "ask_size": 11 + index, "volume": 100 - index,
                    "open_interest": 200 - index,
                    "underlying_price": 100, "feed": feed,
                })
        return rows


class _MismatchedOptionFake(_OptionFake):
    def option_candidates(self, *args, **kwargs):
        rows = super().option_candidates(*args, **kwargs)
        return [{**row, "underlying": "BTC"} for row in rows]


class _StockDataFake:
    def __init__(self):
        self.bar_request = None
        self.quote_request = None

    @staticmethod
    def _response():
        return SimpleNamespace(data={"SPY": [SimpleNamespace(
            timestamp=datetime(2026, 8, 8, 13, 30, tzinfo=timezone.utc),
            open=100, high=101, low=99, close=100.5, volume=10,
            bid_price=100, ask_price=101, last_price=100.5)]})

    def get_stock_bars(self, request):
        self.bar_request = request
        return self._response()

    def get_stock_quotes(self, request):
        self.quote_request = request
        return self._response()


class DeployTests(unittest.TestCase):
    def test_direct_health_probe_bootstraps_repo_import(self):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, "deploy/health.py", "dashboard",
             "--url", "http://127.0.0.1:1"],
            cwd=Path(__file__).resolve().parents[1], env=env,
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("ModuleNotFoundError", result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["component"], "dashboard")

    def test_recorder_compose_service_receives_credentials_secret(self):
        text = Path("compose.yaml").read_text(encoding="utf-8")
        recorder = text.split("  trader:", 1)[0]
        self.assertIn("ALPACA_AGENT_SECRETS_FILE: /run/secrets/agent_credentials",
                      recorder)
        self.assertIn("source: agent_credentials", recorder)

    def test_research_service_has_no_broker_credentials(self):
        text = Path("compose.yaml").read_text(encoding="utf-8")
        research = text.split("  research:", 1)[1].split("  dashboard:", 1)[0]
        self.assertNotIn("ALPACA_AGENT_SECRETS_FILE", research)
        self.assertNotIn("agent_credentials", research)
        self.assertIn(
            "ALPACA_RESEARCH_LLM_SECRETS_FILE: /run/secrets/research_llm_credentials",
            research)
        self.assertIn("source: research_llm_credentials", research)
        self.assertNotIn("OPENAI_API_KEY:", research)
        self.assertNotIn("ANTHROPIC_API_KEY:", research)
        unit = Path("deploy/alpaca-research.service").read_text(encoding="utf-8")
        self.assertNotIn("ALPACA_AGENT_SECRETS_FILE", unit)
        self.assertNotIn("agent.env", unit)
        self.assertIn("research.env", unit)

    def test_scheduler_default_points_to_deploy_cycle(self):
        self.assertEqual(scheduler.build_parser().parse_args([]).script,
                         "deploy/research-cycle.sh")

    def test_scheduler_output_facade_reexports_moved_symbols(self):
        names = (
            "_BoundedCapture", "structured_failure",
            "structured_research_cycle", "_drain", "_start_capture",
            "_capture_detail",
        )
        for name in names:
            self.assertIs(getattr(scheduler, name),
                          getattr(scheduler_output, name))

    def test_scheduler_output_bounded_capture_and_structured_results(self):
        capture = scheduler_output._BoundedCapture(8)
        capture.feed("prefix\n")
        capture.feed(json.dumps({
            "generation": 2, "accepted_count": 0, "status": "FAILED",
            "attempt_id": "attempt-1", "error": "author failed",
        }) + "\n")
        cycle_line = json.dumps({
            "schema": "research-cycle.v1", "status": "completed_no_edge",
            "reason": "no eligible edge", "exit_code": 0,
            "outcomes": ["equity", "option"], "proofs": True,
        }) + "\n"
        capture.feed(cycle_line)
        self.assertEqual(capture.tail, cycle_line[-8:])
        self.assertTrue(capture.truncated)
        self.assertEqual(capture.structured_failures, [{
            "component": "authoring", "status": "FAILED",
            "attempt_id": "attempt-1", "error": "author failed",
        }])
        self.assertEqual(capture.research_cycles, [{
            "schema": "research-cycle.v1", "status": "completed_no_edge",
            "reason": "no eligible edge", "exit_code": 0,
            "outcomes": ["equity", "option"], "proofs": True,
            "no_edge": False,
        }])

    def test_scheduler_output_capture_detail_combines_streams(self):
        stdout = scheduler_output._BoundedCapture(4)
        stderr = scheduler_output._BoundedCapture(200)
        stdout.feed("abcdef")
        stderr.feed(json.dumps({
            "max_reviews": 2, "reviewed": 2, "retry_pending": 1,
            "failed": 0, "status": "RETRY_PENDING",
        }) + "\n")
        detail = scheduler_output._capture_detail(stdout, stderr)
        self.assertEqual(detail["stdout_tail"], "cdef")
        self.assertEqual(detail["stdout_chars"], 6)
        self.assertTrue(detail["stdout_truncated"])
        self.assertEqual(detail["stderr_chars"], len(stderr.tail))
        self.assertFalse(detail["stderr_truncated"])
        self.assertEqual(detail["structured_failures"], [{
            "component": "review", "status": "RETRY_PENDING",
            "reviewed": 2, "retry_pending": 1, "failed": 0,
        }])

    def test_scheduler_output_start_capture_drains_text_stream(self):
        stream = io.StringIO("line one\nline two\n")
        result = scheduler_output._start_capture(stream, 100)
        self.assertIsNotNone(result)
        capture, thread = result
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(capture.total_chars, len("line one\nline two\n"))
        self.assertEqual(capture.tail, "line one\nline two\n")

    def test_recorder_facade_reexports_market_symbols(self):
        from deploy import recorder_market
        names = (
            "FIELDS", "_value", "_timeframe", "_feed", "_options_feed",
            "_call_market_data", "_call_quotes", "_call_options", "_event_key",
            "_number", "_iso", "_point_in_time", "_timestamp",
            "_underlying_price", "_option_right", "_option_rank", "_option_rows",
            "_rows",
        )
        for name in names:
            self.assertIs(getattr(recorder, name), getattr(recorder_market, name))

    def test_recorder_migrates_legacy_header_before_deduplicating(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            path.write_text(
                "event_type,symbol,timestamp\n"
                "bar_1m,SPY,2026-08-08T13:30:00+00:00\n",
                encoding="utf-8")
            self.assertEqual(recorder.record_once(fake, ["SPY"], path), 1)
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(list(rows[0]), list(recorder.FIELDS))
            self.assertEqual(recorder.record_once(fake, ["SPY"], path), 0)

    def test_research_cycle_uses_recorded_csv_and_initializes_edge_ledger(self):
        csv_text = (
            "event_key,observed_at,provider,feed,event_type,symbol,timestamp,"
            "open,high,low,close,volume,bid,ask,last\n"
            "k,2026-08-08T13:31:00+00:00,alpaca,iex,bar_1m,SPY,"
            "2026-08-08T13:30:00+00:00,100,101,99,100.5,10,,,,\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "market.csv"
            edge_db = root / "edge.sqlite3"
            dataset.write_text(csv_text, encoding="utf-8")
            env = dict(os.environ, PYTHON=sys.executable,
                       ALPACA_RESEARCH_DATASET=str(dataset),
                       ALPACA_EDGE_DB=str(edge_db))
            result = subprocess.run(
                ["deploy/research-cycle.sh"],
                cwd=Path(__file__).resolve().parents[1], env=env,
                capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn('"valid": true', result.stdout)
            self.assertTrue(edge_db.is_file())
            with closing(sqlite3.connect(edge_db)) as db:
                self.assertGreater(db.execute(
                    "SELECT COUNT(*) FROM candidates").fetchone()[0], 0)

    def test_research_cycle_reports_no_data_as_structured_nonzero_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "empty.jsonl"
            dataset.write_text("\n", encoding="utf-8")
            result = subprocess.run(
                ["deploy/research-cycle.sh"],
                cwd=Path(__file__).resolve().parents[1],
                env=dict(os.environ, PYTHON=sys.executable,
                         ALPACA_RESEARCH_DATASET=str(dataset)),
                capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["schema"], "research-cycle.v1")
            self.assertEqual(payload["status"], "no_data")
            self.assertEqual(payload["exit_code"], 2)

    def test_scheduler_preserves_research_cycle_statuses_in_health_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text("mode: paper\nbroker:\n  paper: true\n",
                              encoding="utf-8")
            script = root / "cycle.sh"
            script.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$CYCLE_RESULT\"\n"
                "exit \"${CYCLE_EXIT:-0}\"\n", encoding="utf-8")
            script.chmod(0o755)
            status = root / "health.json"
            for expected, exit_code in (("completed_no_edge", 0),
                                        ("completed_no_edge", 2),
                                        ("no_data", 2)):
                scheduler._running = True
                env = dict(os.environ, CYCLE_RESULT=json.dumps({
                    "schema": "research-cycle.v1", "status": expected,
                    "reason": "test", "exit_code": exit_code,
                }), CYCLE_EXIT=str(exit_code))
                with patch.dict(os.environ, env, clear=True):
                    args = SimpleNamespace(
                        status_file=str(status), config=str(config),
                        script=str(script), root=str(root), hour=3, minute=0,
                        once=True, timeout_seconds=10,
                        output_limit_chars=4096)
                    expected_exit = (0 if expected == "completed_no_edge"
                                     else exit_code)
                    self.assertEqual(scheduler.run_scheduler(args), expected_exit)
                payload = json.loads(status.read_text(encoding="utf-8"))
                self.assertEqual(payload["status"], expected)
                self.assertEqual(payload["cycle_status"], expected)
                self.assertEqual(payload["research_cycle"]["status"], expected)

    def test_research_health_distinguishes_no_edge_from_no_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.json"
            now = datetime.now(timezone.utc).timestamp()
            path.write_text(json.dumps({
                "status": "completed_no_edge", "updated_ts": now,
                "last_exit_code": 0}), encoding="utf-8")
            self.assertTrue(health.research(path, 60, now=now)["ok"])
            path.write_text(json.dumps({
                "status": "no_data", "updated_ts": now,
                "last_exit_code": 2}), encoding="utf-8")
            self.assertFalse(health.research(path, 60, now=now)["ok"])

    def test_research_cycle_sends_recorded_options_to_autonomous_discovery(self):
        expiry = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()
        contract_symbol = f"SPY{datetime.fromisoformat(expiry):%y%m%d}C00100000"
        fields = list(recorder.FIELDS)
        csv_buffer = io.StringIO()
        writer = csv.DictWriter(csv_buffer, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "event_key": "bar", "observed_at": "2026-08-08T13:31:00+00:00",
            "provider": "alpaca", "feed": "iex", "event_type": "bar_1m",
            "symbol": "SPY", "timestamp": "2026-08-08T13:30:00+00:00",
            "as_of": "2026-08-08T13:30:00+00:00", "open": 100, "high": 101,
            "low": 99, "close": 100.5, "volume": 10,
        })
        writer.writerow({
            "event_key": "option", "observed_at": "2026-08-08T13:31:00+00:00",
            "provider": "alpaca", "feed": "indicative",
            "event_type": "option_snapshot", "symbol": contract_symbol,
            "contract": contract_symbol,
            "timestamp": "2026-08-08T13:30:02+00:00",
            "as_of": "2026-08-08T13:30:02+00:00", "volume": 100, "bid": 1,
            "ask": 1.1, "underlying": "SPY", "expiration": expiry,
            "strike": 100, "right": "call", "multiplier": 100,
            "bid_size": 10, "ask_size": 11, "open_interest": 200,
            "underlying_price": 100,
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "market.csv"
            edge_db = root / "edge.sqlite3"
            dataset.write_text(csv_buffer.getvalue(), encoding="utf-8")
            env = dict(os.environ, PYTHON=sys.executable,
                       ALPACA_RESEARCH_DATASET=str(dataset),
                       ALPACA_EDGE_DB=str(edge_db))
            result = subprocess.run(
                ["deploy/research-cycle.sh"],
                cwd=Path(__file__).resolve().parents[1], env=env,
                capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn('"vehicle": "option"', result.stdout)
            self.assertTrue(edge_db.is_file())
            with closing(sqlite3.connect(edge_db)) as db:
                vehicles = dict(db.execute(
                    "SELECT vehicle, COUNT(*) FROM factory_hypotheses GROUP BY vehicle"
                ).fetchall())
            self.assertEqual(vehicles, {"equity": 7, "option": 7})

    def test_recorder_passes_feed_and_deduplicates_overlapping_windows(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            self.assertEqual(recorder.record_once(fake, ["SPY"], path,
                                                  feed="sip"), 2)
            self.assertEqual(recorder.record_once(fake, ["SPY"], path,
                                                  feed="sip"), 0)
            rows = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 3)  # header plus one bar and quote
            self.assertEqual(fake.seen, [("bars", "sip"), ("quotes", "sip"),
                                         ("bars", "sip"), ("quotes", "sip")])
            self.assertEqual(len({row.split(",", 1)[0] for row in rows[1:]}), 2)

    def test_recorder_resumes_from_durable_watermark_after_long_outage(self):
        fake = _WindowFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            self.assertEqual(recorder.record_once(fake, ["SPY"], path,
                                                  feed="iex"), 2)
            self.assertEqual(recorder.record_once(fake, ["SPY"], path,
                                                  feed="iex"), 0)
            expected = datetime(2026, 8, 8, 13, 29, 1,
                                tzinfo=timezone.utc)
            self.assertEqual(fake.starts[-2:], [expected, expected])

    def test_recorder_skips_quotes_without_point_in_time_timestamp(self):
        fake = _MissingQuoteTimestampFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            self.assertEqual(recorder.record_once(fake, ["SPY"], path), 1)
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["event_type"] for row in rows], ["bar_1m"])

    def test_recorder_fails_closed_on_corrupt_existing_csv(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            path.write_text(
                "event_key,event_type,symbol,timestamp\n"
                "x,bar_1m,SPY,2026-08-08T13:30:00+00:00,unexpected\n",
                encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "malformed CSV"):
                recorder.record_once(fake, ["SPY"], path)

    def test_recorder_rejects_crypto_symbols_before_provider_calls(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            with self.assertRaisesRegex(ValueError, "slash pair"):
                recorder.record_once(fake, ["BTC/USD"], path)
            self.assertEqual(fake.seen, [])

    def test_recorder_rejects_future_provider_events(self):
        fake = _FutureBarFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            with self.assertRaisesRegex(RuntimeError, "future"):
                recorder.record_once(fake, ["SPY"], path)
            self.assertFalse(path.exists())

    def test_recorder_does_not_mutate_invalid_legacy_csv(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            original = ("event_type,symbol,timestamp\n"
                        "bar_1m,SPY,not-a-timestamp\n")
            path.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "invalid timestamp"):
                recorder.record_once(fake, ["SPY"], path)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_recorder_skips_option_rows_with_mismatched_underlying(self):
        fake = _MismatchedOptionFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            self.assertEqual(recorder.record_once(
                fake, ["SPY"], path, include_options=True), 2)
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual({row["event_type"] for row in rows},
                             {"bar_1m", "quote"})

    def test_recorder_rejects_invalid_existing_row_semantics(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            writer_buffer = io.StringIO()
            writer = csv.DictWriter(writer_buffer, fieldnames=recorder.FIELDS)
            writer.writeheader()
            writer.writerow({
                "event_key": "bad", "event_type": "evil", "symbol": "BTC/USD",
                "timestamp": "2026-08-08T13:30:00+00:00"})
            path.write_text(writer_buffer.getvalue(), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "invalid recorder dataset row"):
                recorder.record_once(fake, ["SPY"], path)
            self.assertEqual(fake.seen, [])

    def test_recorder_detects_intraday_bar_continuity_gap(self):
        previous = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)
        current = previous + timedelta(hours=2)
        rows = [{"event_type": "bar_1m", "symbol": "SPY",
                 "timestamp": current.isoformat()}]
        with self.assertRaisesRegex(RuntimeError, "continuity gap"):
            recorder._verify_bar_continuity(
                rows, {"SPY": previous}, current, ["SPY"])
        recorder._verify_bar_continuity(
            [{"event_type": "bar_1m", "symbol": "SPY",
              "timestamp": (previous + timedelta(minutes=1)).isoformat()}],
            {"SPY": previous}, previous + timedelta(minutes=1), ["SPY"])

    def test_recorder_records_bounded_lossless_option_snapshots(self):
        from research.market_data import normalize_option_snapshot
        fake = _OptionFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            config = {
                "universe": {"asset_classes": ["us_equity", "us_option"]},
                "risk": {"options_min_dte": 7, "options_max_dte": 60},
            }
            self.assertEqual(recorder.record_once(
                fake, ["SPY"], path, config=config, include_options=True), 12)
            self.assertEqual(recorder.record_once(
                fake, ["SPY"], path, config=config, include_options=True), 0)
            self.assertEqual(fake.option_calls,
                             [("SPY", 100.5, "indicative", 7, 60),
                              ("SPY", 100.5, "indicative", 7, 60)])
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            options = [row for row in rows if row["event_type"] == "option_snapshot"]
            self.assertEqual(len(options), 10)
            self.assertEqual({row["right"] for row in options}, {"call", "put"})
            expiry = datetime.now(timezone.utc).date() + timedelta(days=30)
            self.assertNotIn(
                f"SPY{expiry:%y%m%d}C00105000",
                {row["symbol"] for row in options})
            required = {"contract", "underlying", "expiration", "strike", "right",
                        "multiplier", "bid", "ask", "bid_size", "ask_size",
                        "volume", "open_interest", "observed_at", "timestamp"}
            self.assertTrue(required.issubset(options[0]))
            for row in options:
                payload = {key: (None if value == "" else value)
                           for key, value in row.items()}
                normalized = normalize_option_snapshot(
                    payload, provider=row["provider"], feed=row["feed"])
                self.assertEqual(normalized.contract.underlying, "SPY")
                self.assertEqual(normalized.contract.right, row["right"])

    def test_recorder_uses_provider_feed_over_config_and_records_it(self):
        fake = _MarketFake(feed="sip")
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                "os.environ", {"ALPACA_DATA_FEED": "iex"}, clear=False):
            path = Path(directory) / "market.csv"
            self.assertEqual(recorder.record_once(fake, ["SPY"], path), 2)
            self.assertEqual(fake.seen, [("bars", "sip"), ("quotes", "sip")])
            self.assertTrue(all(",sip," in row for row in path.read_text(
                encoding="utf-8").splitlines()[1:]))

    def test_provider_env_feed_wins_over_iex_config_and_is_recorded(self):
        sdk = _StockDataFake()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                "os.environ", {"ALPACA_DATA_FEED": "sip"}, clear=False):
            provider = AlpacaProvider(
                {"mode": "paper", "broker": {"paper": True,
                                               "data_feed": "iex"}},
                session=AlpacaSession(api_key="key", secret_key="secret",
                                      paper=True, stock_data_client=sdk))
            path = Path(directory) / "market.csv"
            self.assertEqual(recorder.record_once(provider, ["SPY"], path), 2)
            self.assertEqual(provider.data_feed, "sip")
            for request in (sdk.bar_request, sdk.quote_request):
                value = getattr(request, "feed", None)
                if value is None and isinstance(request, dict):
                    value = request.get("feed")
                self.assertIn("sip", str(value).lower())
            self.assertTrue(all(",sip," in row for row in path.read_text(
                encoding="utf-8").splitlines()[1:]))

    def test_health_uses_running_paper_heartbeat_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat.json"
            path.write_text(json.dumps({
                "status": "running", "updated_ts": 100,
                "research_expected": False,
            }), encoding="utf-8")
            result = health.trader(path, max_age=30, now=100)
            self.assertTrue(result["ok"])

    def test_scheduler_accepts_paper_config_without_secret_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("mode: paper\nbroker:\n  paper: true\n",
                              encoding="utf-8")
            self.assertEqual(scheduler.configured_mode(config), "paper")
        self.assertNotIn("load_secrets", Path(scheduler.__file__).read_text())

    def test_dashboard_does_not_claim_promotion_when_research_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime").mkdir()
            (root / "config.yaml").write_text(
                "mode: paper\nstrategy:\n  id: ibr\n  version: v1\n",
                encoding="utf-8")
            snapshot = dashboard.snapshot(root)
            self.assertTrue(snapshot["research"]["service_optional"])
            self.assertTrue(snapshot["research"]["entry_gate_required"])
            self.assertNotIn("gates", snapshot)

    def test_dashboard_html_renders_the_current_snapshot_contract(self):
        self.assertIn("d.strategy.execution_mode", dashboard.HTML)
        self.assertIn("d.research.entry_gate_required", dashboard.HTML)
        self.assertIn("d.research.service_optional", dashboard.HTML)
        self.assertIn("d.edge.proved_edges", dashboard.HTML)
        self.assertIn("cycle outcome", dashboard.HTML)
        self.assertIn("Execution journal", dashboard.HTML)
        self.assertNotIn("d.research_feed_version", dashboard.HTML)
        self.assertNotIn("d.research.optional", dashboard.HTML)
        self.assertNotIn("d.trader.heartbeat.research_available", dashboard.HTML)

    def test_dashboard_exposes_edge_ledger_status(self):
        from research.edge_lab import EdgeLedger, init_ledger
        from tests.research.test_edge_discovery import _persist_gate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime" / "research").mkdir(parents=True)
            (root / "config.yaml").write_text(
                "mode: paper\nstrategy:\n  id: ibr\n  version: v1\n",
                encoding="utf-8")
            ledger_path = root / "runtime" / "research" / "edge_lab.sqlite3"
            init_ledger(ledger_path)
            ledger = EdgeLedger(ledger_path)
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="dashboard proof visibility",
                config={"strategy": {"target_r": 1.5}})
            _persist_gate(ledger, candidate["candidate_id"], "shadow")
            with closing(sqlite3.connect(ledger_path)) as db, db:
                db.execute("UPDATE candidate_state SET status='validated' WHERE candidate_id=?",
                           (candidate["candidate_id"],))
            result = dashboard._edge_status(ledger_path)
            self.assertTrue(result["available"])
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["proved_edges"][0]["variant_id"],
                             "ibr.target.1_5r")
            _persist_gate(ledger, candidate["candidate_id"], "shadow",
                          passes=False)
            self.assertEqual(dashboard._edge_status(ledger_path)["proved_edges"], [])

    def test_report_is_a_small_usd_paper_summary(self):
        with closing(sqlite3.connect(":memory:")) as db:
            db.executescript("""
                CREATE TABLE trades (ts REAL, action TEXT, symbol TEXT,
                    realized_pnl_usd REAL, pnl_pct REAL);
                CREATE TABLE equity (ts REAL, equity REAL, state TEXT);
                INSERT INTO trades VALUES (1, 'open', 'SPY', NULL, NULL);
                INSERT INTO trades VALUES (2, 'close', 'SPY', 12.5, 0.25);
                INSERT INTO equity VALUES (1, 1000, 'running');
                INSERT INTO equity VALUES (2, 1012.5, 'running');
            """)
            summary = report.json_report(db)
            self.assertEqual(summary["scope"], "alpaca-paper")
            self.assertEqual(summary["closed_trades"], 1)
            self.assertEqual(summary["realized_pnl_usd"], 12.5)
            self.assertNotIn("promotion", json.dumps(summary))

    def test_compose_is_paper_only_and_research_profile_is_optional(self):
        text = Path("compose.yaml").read_text(encoding="utf-8")
        self.assertIn("ALPACA_PAPER: \"true\"", text)
        self.assertIn("profiles: [research]", text)
        self.assertIn("ALPACA_RESEARCH_DATASET", text)


if __name__ == "__main__":
    unittest.main()
