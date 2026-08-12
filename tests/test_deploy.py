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

# Several tests run the real ``deploy/research-cycle.sh`` with the repository
# as its working directory, and a real cycle archives its narrative and edge
# proofs under ``research/results``.  Redirecting both for the whole module
# keeps generated artifacts out of the working copy no matter which invocation
# a later test adds; every call site builds its environment from ``os.environ``.
_ARTIFACTS: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:                                     # noqa: N802
    global _ARTIFACTS
    _ARTIFACTS = tempfile.TemporaryDirectory(prefix="alpaca-test-artifacts.")
    root = Path(_ARTIFACTS.name)
    os.environ["ALPACA_RESEARCH_REPORT_DIR"] = str(root / "reports")
    os.environ["ALPACA_RESEARCH_PROOF_DIR"] = str(root / "proofs")


def tearDownModule() -> None:                                  # noqa: N802
    for name in ("ALPACA_RESEARCH_REPORT_DIR", "ALPACA_RESEARCH_PROOF_DIR"):
        os.environ.pop(name, None)
    if _ARTIFACTS is not None:
        _ARTIFACTS.cleanup()


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


class _CalendarFake:
    """Two regular sessions, one 13:00 early close and one missing holiday."""

    def __init__(self):
        self.calls = 0

    def calendar(self, start=None, end=None):
        self.calls += 1
        return [{"date": "2026-08-04", "open": "09:30", "close": "16:00"},
                {"date": "2026-08-06", "open": "09:30", "close": "16:00"},
                {"date": "2026-08-07", "open": "09:30", "close": "13:00"}]


def _corpus_rows(*, sessions: int, per_session: int, minute: int = 0) -> list[dict]:
    """Synthesize a durable corpus ending the session before the fakes' rows."""
    from deploy.recorder_market import _event_key
    rows = []
    for index in range(sessions):
        day = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc) - timedelta(
            days=sessions - index)
        for step in range(per_session):
            stamp = (day + timedelta(minutes=minute + step)).isoformat()
            rows.append({"event_key": _event_key("bar_1m", "SPY", stamp),
                         "observed_at": stamp, "provider": "alpaca", "feed": "iex",
                         "event_type": "bar_1m", "symbol": "SPY",
                         "timestamp": stamp, "as_of": stamp, "open": 100,
                         "high": 101, "low": 99, "close": 100.5, "volume": 10})
    return rows


def _write_flat_corpus(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=recorder.FIELDS)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in recorder.FIELDS}
                         for row in rows)


def _replay_corpus_csv(*, quotes: bool = True, sessions: int = 1) -> str:
    """One tradable IBR session per day, with quotes priced away from the bar."""
    from deploy.recorder_market import _event_key
    from zoneinfo import ZoneInfo
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=recorder.FIELDS)
    writer.writeheader()
    open_bell = datetime(2026, 8, 3, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    for session in range(sessions):
        day = open_bell + timedelta(days=session)
        for minute in range(60):
            stamp = (day + timedelta(minutes=minute)).astimezone(timezone.utc).isoformat()
            if minute < 15:
                values = (100.0, 100.5, 99.8, 100.2)
            elif minute == 15:
                values = (100.2, 101.5, 100.2, 101.4)
            else:
                base = 101.4 + minute * 0.02
                values = (base, base + 0.3, base - 0.1, base + 0.2)
            writer.writerow({
                "event_key": _event_key("bar_1m", "SPY", stamp), "observed_at": stamp,
                "provider": "alpaca", "feed": "iex", "event_type": "bar_1m",
                "symbol": "SPY", "timestamp": stamp, "as_of": stamp,
                "open": values[0], "high": values[1], "low": values[2],
                "close": values[3], "volume": 1000})
            if quotes:
                writer.writerow({
                    "event_key": _event_key("quote", "SPY", stamp), "observed_at": stamp,
                    "provider": "alpaca", "feed": "iex", "event_type": "quote",
                    "symbol": "SPY", "timestamp": stamp, "as_of": stamp,
                    "bid": values[0] - 0.5, "ask": values[0] + 0.5,
                    "bid_size": 10, "ask_size": 10})
    return buffer.getvalue()


def _run_research_cycle(dataset: Path | str, root: Path, **env):
    return subprocess.run(
        ["deploy/research-cycle.sh"],
        cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ, PYTHON=sys.executable,
                 ALPACA_RESEARCH_DATASET=str(dataset),
                 ALPACA_FACTORY_ENABLED="0",
                 ALPACA_EDGE_DB=str(root / "edge.sqlite3"), **env),
        capture_output=True, text=True, check=False)


def _cycle_payloads(text: str, key: str) -> list[dict]:
    payloads = []
    for line in text.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and key in payload:
            payloads.append(payload)
    return payloads


_corpus_iterator = recorder.iter_corpus_rows


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
            partitions = recorder.corpus_partitions(path)
            self.assertEqual([item.name for item in partitions],
                             ["market-2026-08-08.csv"])
            with partitions[0].open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(list(rows[0]), list(recorder.FIELDS))
            self.assertFalse(path.exists())
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

    def test_research_cycle_routes_recorded_quotes_into_the_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summaries = {}
            for label in ("quotes", "bars_only"):
                dataset = root / f"{label}.csv"
                dataset.write_text(_replay_corpus_csv(quotes=label == "quotes"),
                                   encoding="utf-8")
                result = _run_research_cycle(dataset, root / label)
                self.assertEqual(result.returncode, 0, result.stderr)
                views = _cycle_payloads(result.stderr, "schema")
                views = [item for item in views
                         if item["schema"] == "research-cycle-views.v1"][0]
                self.assertEqual(views["quotes"], 60 if label == "quotes" else 0)
                self.assertEqual(views["bars"], 60)
                summaries[label] = _cycle_payloads(result.stdout, "vehicle")[0]
            # The routed quotes are the executable price at the fill instant,
            # so a corpus carrying them must not replay like the bars alone.
            self.assertEqual(summaries["quotes"]["trades"], 1)
            self.assertEqual(summaries["bars_only"]["trades"], 1)
            self.assertNotEqual(summaries["quotes"]["net_pnl"],
                                summaries["bars_only"]["net_pnl"])

    def test_research_cycle_reads_a_partitioned_corpus_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "recorded"
            path = corpus / "market.csv"
            _write_flat_corpus(path, _corpus_rows(sessions=4, per_session=3))
            recorder.migrate_corpus(path)
            result = _run_research_cycle(
                "", root, ALPACA_RECORDED_DATASET_ROOT=str(corpus),
                ALPACA_RESEARCH_SESSION_WINDOW="2")
            self.assertEqual(result.returncode, 0, result.stderr)
            views = [item for item in _cycle_payloads(result.stderr, "schema")
                     if item["schema"] == "research-cycle-views.v1"][0]
            self.assertEqual(views["bars"], 6)  # two sessions, not twelve

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
            # The checked config trades shares, so the option lane runs only
            # when it is asked for; a trader cannot deploy option evidence.
            env = dict(os.environ, PYTHON=sys.executable,
                       ALPACA_RESEARCH_DATASET=str(dataset),
                       ALPACA_EDGE_DB=str(edge_db),
                       ALPACA_RESEARCH_VEHICLES="all")
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

    def test_research_cycle_studies_only_the_tradeable_vehicle_by_default(self):
        """Option evidence a shares trader can never deploy is not produced."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "market.jsonl"
            edge_db = root / "edge.sqlite3"
            dataset.write_text("\n".join(json.dumps(row) for row in (
                {"kind": "bar", "provider": "alpaca", "feed": "iex",
                 "symbol": "SPY", "timestamp": "2026-08-08T13:30:00+00:00",
                 "as_of": "2026-08-08T13:30:00+00:00",
                 "observed_at": "2026-08-08T13:31:00+00:00",
                 "open": 100, "high": 101, "low": 99, "close": 100.5,
                 "volume": 10},
                {"kind": "option_snapshot", "provider": "alpaca",
                 "feed": "indicative", "symbol": "SPY260918C00100000",
                 "contract": "SPY260918C00100000",
                 "timestamp": "2026-08-08T13:30:02+00:00",
                 "as_of": "2026-08-08T13:30:02+00:00",
                 "observed_at": "2026-08-08T13:31:00+00:00",
                 "underlying": "SPY", "expiration": "2026-09-18", "strike": 100,
                 "right": "call", "multiplier": 100, "bid": 1, "ask": 1.1,
                 "bid_size": 10, "ask_size": 11, "volume": 100,
                 "open_interest": 200, "underlying_price": 100},
            )) + "\n", encoding="utf-8")
            env = dict(os.environ, PYTHON=sys.executable,
                       ALPACA_RESEARCH_DATASET=str(dataset),
                       ALPACA_EDGE_DB=str(edge_db))
            env.pop("ALPACA_RESEARCH_VEHICLES", None)
            result = subprocess.run(
                ["deploy/research-cycle.sh"],
                cwd=Path(__file__).resolve().parents[1], env=env,
                capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            with closing(sqlite3.connect(edge_db)) as db:
                vehicles = dict(db.execute(
                    "SELECT vehicle, COUNT(*) FROM factory_hypotheses GROUP BY vehicle"
                ).fetchall())
            self.assertEqual(vehicles, {"equity": 7})

    def test_dashboard_tradeable_vehicle_matches_the_runtime_resolver(self):
        from agent.edge import runtime_vehicle
        for mode, expected in (("shares", "equity"), ("options", "option"),
                               ("option", "option"), ("", "equity")):
            config = {"strategy": {"execution_mode": mode},
                      "universe": {"asset_classes": ["us_equity", "us_option"]}}
            with self.subTest(mode=mode):
                self.assertEqual(dashboard._tradeable_vehicle(config), expected)
                self.assertEqual(runtime_vehicle(config), expected)

    def test_dashboard_counts_proved_edges_this_profile_cannot_trade(self):
        from research.edge_lab import EdgeLedger, init_ledger
        from tests.research.test_edge_discovery import _persist_gate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime" / "research").mkdir(parents=True)
            # An options profile holding a proved *equity* edge: real evidence
            # this trader can never act on, so it is reported, not counted as
            # deployable.
            (root / "config.yaml").write_text(
                "mode: paper\nstrategy:\n  id: ibr\n  version: v1\n"
                "  execution_mode: options\n", encoding="utf-8")
            ledger_path = root / "runtime" / "research" / "edge_lab.sqlite3"
            init_ledger(ledger_path)
            ledger = EdgeLedger(ledger_path)
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="an equity edge an options trader cannot deploy",
                config={"strategy": {"target_r": 1.5}})
            _persist_gate(ledger, candidate["candidate_id"], "shadow")
            with closing(sqlite3.connect(ledger_path)) as db, db:
                db.execute("UPDATE candidate_state SET status='validated' WHERE candidate_id=?",
                           (candidate["candidate_id"],))
            snapshot = dashboard.snapshot(root)
            self.assertEqual(snapshot["research"]["tradeable_vehicle"], "option")
            self.assertEqual(snapshot["edge"]["proved_edges"][0]["vehicle"], "equity")
            self.assertEqual(snapshot["research"]["untradeable_proved_edges"], 1)

    def test_recorder_passes_feed_and_deduplicates_overlapping_windows(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            self.assertEqual(recorder.record_once(fake, ["SPY"], path,
                                                  feed="sip"), 2)
            self.assertEqual(recorder.record_once(fake, ["SPY"], path,
                                                  feed="sip"), 0)
            rows = list(recorder.iter_corpus_rows(path))
            self.assertEqual(len(rows), 2)  # one bar and one quote
            self.assertEqual(fake.seen, [("bars", "sip"), ("quotes", "sip"),
                                         ("bars", "sip"), ("quotes", "sip")])
            self.assertEqual(len({row["event_key"] for row in rows}), 2)
            self.assertTrue(all(row["feed"] == "sip" for row in rows))

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
            rows = list(recorder.iter_corpus_rows(path))
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
            rows = list(recorder.iter_corpus_rows(path))
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

    def test_recorder_partitions_by_session_and_migrates_a_legacy_corpus(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            legacy = _corpus_rows(sessions=3, per_session=4)
            _write_flat_corpus(path, legacy)
            self.assertEqual(recorder.migrate_corpus(path), len(legacy))
            self.assertEqual(
                [item.name for item in recorder.corpus_partitions(path)],
                ["market-2026-08-05.csv", "market-2026-08-06.csv",
                 "market-2026-08-07.csv"])
            self.assertFalse(path.exists())
            self.assertTrue(path.with_name("market.csv.migrated").is_file())
            migrated = list(recorder.iter_corpus_rows(path))
            self.assertEqual([row["event_key"] for row in migrated],
                             [row["event_key"] for row in legacy])
            # The first cycle after the upgrade resumes from the migrated
            # watermark instead of rewriting anything.
            self.assertEqual(recorder.record_once(fake, ["SPY"], path), 2)
            self.assertEqual(len(recorder.corpus_partitions(path)), 4)

    def test_recorder_rejects_duplicate_keys_and_rebuilds_a_stale_index(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            rows = _corpus_rows(sessions=1, per_session=4)
            recorder._append_partitions(path, rows)
            recorder._save_index(path, recorder._scan_corpus(path))
            index = Path(directory) / recorder.INDEX_NAME
            self.assertTrue(index.is_file())
            # A cycle that appended rows and died before rewriting the index
            # leaves partition sizes disagreeing with it; that must rebuild.
            recorder._append_partitions(path, _corpus_rows(
                sessions=1, per_session=2, minute=90))
            self.assertIsNone(recorder._load_index(path))
            self.assertEqual(recorder.record_once(fake, ["SPY"], path), 2)
            keys = [row["event_key"] for row in recorder.iter_corpus_rows(path)]
            self.assertEqual(len(keys), len(set(keys)))
            recorder._append_partitions(path, rows[:1])
            with self.assertRaisesRegex(RuntimeError, "repeats event_key"):
                recorder._scan_corpus(path)

    def test_recorder_refuses_rows_older_than_the_dedup_window(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            self.assertEqual(recorder.record_once(fake, ["SPY"], path), 2)
            state = recorder._load_index(path)
            state["watermark"] = (
                datetime(2026, 8, 8, 13, 30, tzinfo=timezone.utc) +
                recorder.DEDUP_HORIZON + timedelta(minutes=5)).isoformat()
            recorder._save_index(path, state)
            with self.assertRaisesRegex(RuntimeError, "older than the dedup window"):
                recorder.record_once(fake, ["SPY"], path)

    def test_recorder_cycle_cost_stays_flat_as_the_corpus_grows(self):
        counts = []

        def counting(output):
            rows = 0
            for row in _corpus_iterator(output):
                rows += 1
                yield row
            counts.append(rows)

        with tempfile.TemporaryDirectory() as directory:
            measured = {}
            for label, sessions in (("small", 3), ("large", 30)):
                root = Path(directory) / label
                path = root / "market.csv"
                rows = _corpus_rows(sessions=sessions, per_session=200)
                recorder._append_partitions(path, rows)
                recorder._save_index(path, recorder._scan_corpus(path))
                self.assertEqual(len(recorder.corpus_partitions(path)), sessions)
                with patch.object(recorder, "iter_corpus_rows", counting):
                    counts.clear()
                    recorder.record_once(_MarketFake(), ["SPY"], path)
                    steady = sum(counts)
                    counts.clear()
                    recorder._scan_corpus(path)
                    measured[label] = (steady, sum(counts), len(rows))
            # A steady cycle reads no durable rows at either size, while the
            # recovery scan it replaced still grows with the corpus.
            self.assertEqual(measured["small"][0], 0)
            self.assertEqual(measured["large"][0], 0)
            self.assertGreaterEqual(
                measured["large"][1] / measured["small"][1],
                0.9 * measured["large"][2] / measured["small"][2])

    def test_recorder_keeps_calendar_holidays_and_early_closes_quiet(self):
        calendar = recorder.CalendarCache(_CalendarFake())
        zone = recorder.NEW_YORK
        gap = timedelta(hours=2)
        trading = datetime(2026, 8, 6, 10, 0, tzinfo=zone)
        self.assertTrue(recorder._regular_session_gap(
            trading, trading + gap, calendar))
        holiday = datetime(2026, 8, 5, 10, 0, tzinfo=zone)  # weekday, shut
        self.assertFalse(recorder._regular_session_gap(
            holiday, holiday + gap, calendar))
        self.assertTrue(recorder._regular_session_gap(
            holiday, holiday + gap))  # no calendar: heuristic still fails closed
        early = datetime(2026, 8, 7, 12, 55, tzinfo=zone)  # closes 13:00
        self.assertFalse(recorder._regular_session_gap(
            early, early + gap, calendar))
        self.assertEqual(calendar.provider.calls, 1)

    def test_recorder_keeps_pinned_option_contracts_in_the_sample(self):
        from deploy import recorder_market
        fake = _OptionFake()
        quotes = fake.quotes(["SPY"], start=None, end=None, feed="iex")
        now = datetime(2026, 8, 8, 13, 31, tzinfo=timezone.utc)
        expiry = datetime.now(timezone.utc).date() + timedelta(days=30)
        drifted = f"SPY{expiry:%y%m%d}C00105000"

        def sample(pinned=frozenset()):
            return [row["contract"] for row in recorder_market._option_rows(
                fake, ["SPY"], quotes, now, feed="iex", config=None, limit=2,
                pinned=pinned)]

        self.assertNotIn(drifted, sample())
        self.assertIn(drifted, sample(frozenset({drifted})))
        self.assertEqual(len(sample(frozenset({drifted}))), len(sample()) + 1)

    def test_recorder_pins_sampled_contracts_until_the_hold_expires(self):
        fake = _OptionFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            recorder.record_once(fake, ["SPY"], path, include_options=True,
                                 option_limit=1)
            pins = recorder._load_index(path)["option_pins"]
            self.assertEqual(len(pins), 2)
            self.assertTrue(all(value > datetime.now(timezone.utc).isoformat()
                                for value in pins.values()))
            recorder.record_once(fake, ["SPY"], path, include_options=True,
                                 option_limit=1,
                                 option_hold=timedelta(seconds=-1))
            expired = recorder._load_index(path)["option_pins"]
            self.assertEqual(set(expired), set(pins))
            self.assertTrue(all(value < datetime.now(timezone.utc).isoformat()
                                for value in expired.values()))
            self.assertEqual(recorder.record_once(
                fake, ["SPY"], path, include_options=True, option_limit=1), 0)

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
            rows = list(recorder.iter_corpus_rows(path))
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
            self.assertTrue(all(row["feed"] == "sip"
                                for row in recorder.iter_corpus_rows(path)))

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
            self.assertTrue(all(row["feed"] == "sip"
                                for row in recorder.iter_corpus_rows(path)))

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
        self.assertIn("d.edge.live_paper", dashboard.HTML)
        self.assertIn("Live paper results by edge", dashboard.HTML)
        self.assertIn("d.research.tradeable_vehicle", dashboard.HTML)
        self.assertIn("d.research.untradeable_proved_edges", dashboard.HTML)
        self.assertIn("cycle outcome", dashboard.HTML)
        self.assertIn("Execution journal", dashboard.HTML)
        self.assertNotIn("d.research_feed_version", dashboard.HTML)
        self.assertNotIn("d.research.optional", dashboard.HTML)
        self.assertNotIn("d.trader.heartbeat.research_available", dashboard.HTML)

    def test_dashboard_renders_the_detailed_reporting_panels(self):
        """Every question the operator asked has a surface that answers it."""
        for marker in (
                # Which edge is earning a promotion, and what to paste.
                "d.trial", "Demo trials", "Promotable", "config_snippet",
                # What was pinned, and whether it can actually trade.
                "d.promotions", "Pinned promotions", "Pinned but NOT trading",
                "notify only",
                # Which strategy and variant each real fill came from.
                "d.journal", "Trades by edge", "Recent trades, attributed",
                "variant_id",
                # Why research tried what it tried.
                "d.learning", "What research learned", "built_on",
                # Every configuration change, by id.
                "d.config_audit", "Configuration audit trail",
                "config_version_id"):
            with self.subTest(marker=marker):
                self.assertIn(marker, dashboard.HTML)

    def test_dashboard_attributes_each_trade_to_its_edge(self):
        import sqlite3 as sqlite
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.db"
            with closing(sqlite.connect(journal)) as db, db:
                db.execute("""CREATE TABLE trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, symbol TEXT,
                    side TEXT, action TEXT, qty REAL, price REAL, notional REAL,
                    realized_pnl_usd REAL, risk_usd REAL, pnl_pct REAL,
                    fill_status TEXT, setup_type TEXT, strategy_id TEXT,
                    strategy_version TEXT, variant_id TEXT, runtime_mode TEXT,
                    exit_policy TEXT, close_trigger TEXT)""")
                for index, (variant, pnl) in enumerate(
                        [("rule.a.1", 200.0), ("rule.a.1", -100.0),
                         ("rule.b.2", 50.0)]):
                    db.execute(
                        "INSERT INTO trades (ts,symbol,side,action,qty,price,"
                        "realized_pnl_usd,risk_usd,strategy_id,variant_id) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (1000.0 + index, "SPY", "buy", "close", 10, 100.0,
                         pnl, 100.0, "rule", variant))
            view = dashboard._journal_view(journal)
        self.assertTrue(view["available"])
        self.assertEqual(len(view["trades"]), 3)
        by_variant = {row["variant_id"]: row for row in view["by_variant"]}
        self.assertEqual(by_variant["rule.a.1"]["trades"], 2)
        self.assertEqual(by_variant["rule.a.1"]["total_r"], 1.0)
        self.assertEqual(by_variant["rule.a.1"]["win_rate"], 0.5)
        self.assertEqual(by_variant["rule.b.2"]["realized_pnl_usd"], 50.0)

    def test_dashboard_journal_view_degrades_without_a_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            view = dashboard._journal_view(Path(directory) / "absent.db")
        self.assertFalse(view["available"])
        self.assertEqual(view["trades"], [])

    def test_dashboard_surfaces_the_config_audit_trail(self):
        import copy as copier
        from agent.config import DEFAULT_CONFIG
        from agent.governance import record_config_version

        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.db"
            base = copier.deepcopy(DEFAULT_CONFIG)
            record_config_version(journal, base)
            base["risk"]["daily_loss_limit_pct"] = 3.0
            record_config_version(journal, base)
            audit = dashboard._config_audit(journal)
        self.assertTrue(audit["available"])
        self.assertEqual(len(audit["versions"]), 2)
        self.assertEqual(audit["current"], audit["versions"][0]["config_version_id"])
        self.assertIn("risk.daily_loss_limit_pct",
                      audit["versions"][0]["changed_paths"])

    def test_dashboard_reports_a_pin_that_cannot_trade(self):
        from research.edge_lab import init_ledger

        with tempfile.TemporaryDirectory() as directory:
            edge_db = Path(directory) / "edge.sqlite3"
            init_ledger(edge_db)
            from agent.config import DEFAULT_CONFIG as _DEFAULTS

            config = {**_DEFAULTS}
            config["strategy"] = {**config["strategy"],
                                  "selection_mode": "pinned",
                                  "pinned": [{"id": "pin-ghost",
                                              "variant_id": "rule.ghost.1",
                                              "vehicle": "equity",
                                              "strategy_id": "rule",
                                              "note": "", "promoted_at": ""}]}
            view = dashboard._promotions(config, edge_db)
        self.assertEqual(view["selection_mode"], "pinned")
        self.assertTrue(view["frozen"])
        self.assertEqual(len(view["pinned"]), 1)
        self.assertEqual(view["unresolved"][0]["id"], "pin-ghost")

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

    def test_dashboard_reports_live_paper_results_for_each_edge(self):
        """Proof confidence is what the evidence was; this is how it is doing."""
        from research.edge_lab import EdgeLedger, init_ledger
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "edge_lab.sqlite3"
            init_ledger(ledger_path)
            ledger = EdgeLedger(ledger_path)
            # No outcomes yet: the key exists so the card can render empty.
            self.assertEqual(dashboard._edge_status(ledger_path)["live_paper"], [])
            for variant, values in (("rule.mean-reversion.win", [1.0, 1.0, .5]),
                                    ("rule.trend-pullback.lose", [-1.0, -.5])):
                record = ledger.register_candidate(
                    variant, strategy_id="rule", vehicle="equity",
                    hypothesis="live paper visibility",
                    config={"strategy": {"rule_spec": {"family": "mean_reversion"}}})
                for index, value in enumerate(values):
                    ledger.ingest_paper_outcome(record["candidate_id"], {
                        "opportunity_id": f"{variant}:{index}",
                        "session_date": f"2026-06-{index + 1:02d}",
                        "net_pnl": value * 100.0, "risk_usd": 100.0})
            live = dashboard._edge_status(ledger_path)["live_paper"]
            self.assertEqual([row["variant_id"] for row in live],
                             ["rule.mean-reversion.win", "rule.trend-pullback.lose"])
            winner = live[0]
            self.assertEqual(winner["outcomes"], 3)
            self.assertEqual(winner["sessions"], 3)
            self.assertEqual(winner["last_session"], "2026-06-03")
            self.assertAlmostEqual(winner["total_r"], 2.5)
            self.assertAlmostEqual(winner["net_pnl"], 250.0)
            self.assertEqual(winner["guard"], "3/20")

    def test_dashboard_paper_guard_thresholds_track_the_ledger(self):
        """The dashboard restates the guard rather than importing it."""
        from research.edge_ledger import (PAPER_DEMOTION_MIN_OUTCOMES,
                                          PAPER_DEMOTION_R_FLOOR)
        self.assertEqual(dashboard.PAPER_ROLLING_WINDOW,
                         PAPER_DEMOTION_MIN_OUTCOMES)
        self.assertEqual(dashboard.PAPER_ROLLING_FLOOR, PAPER_DEMOTION_R_FLOOR)

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
