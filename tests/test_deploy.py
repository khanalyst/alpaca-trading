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
    from deploy import dashboard, health, recorder, scheduler
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


class _OptionFake(_MarketFake):
    def __init__(self):
        super().__init__(feed="iex")
        self.option_calls = []

    def option_candidates(self, symbol, *, now, underlying_price, feed,
                          min_dte, max_dte):
        self.option_calls.append((symbol, underlying_price, feed,
                                  min_dte, max_dte))
        timestamp = datetime(2026, 8, 8, 13, 30, 2, tzinfo=timezone.utc)
        rows = []
        for right in ("call", "put"):
            for index in range(6):
                strike = 100 + index if right == "call" else 100 - index
                rows.append({
                    "symbol": f"SPY260808{right[0].upper()}{index:03d}",
                    "underlying": "SPY", "expiration": (
                        datetime.now(timezone.utc).date() + timedelta(days=30)
                    ).isoformat(),
                    "strike": strike, "right": right, "multiplier": 100,
                    "timestamp": timestamp, "bid": 1 + index / 10,
                    "ask": 1.1 + index / 10, "bid_size": 10 + index,
                    "ask_size": 11 + index, "volume": 100 - index,
                    "open_interest": 200 - index,
                    "underlying_price": 100, "feed": feed,
                })
        return rows


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
        unit = Path("deploy/alpaca-research.service").read_text(encoding="utf-8")
        self.assertNotIn("ALPACA_AGENT_SECRETS_FILE", unit)
        self.assertNotIn("agent.env", unit)
        self.assertIn("research.env", unit)

    def test_scheduler_default_points_to_deploy_cycle(self):
        self.assertEqual(scheduler.build_parser().parse_args([]).script,
                         "deploy/research-cycle.sh")

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

    def test_research_cycle_sends_recorded_options_to_autonomous_discovery(self):
        expiry = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()
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
            "event_type": "option_snapshot", "symbol": "SPY260918C00100",
            "contract": "SPY260918C00100",
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
            self.assertNotIn("SPY260808C005", {row["symbol"] for row in options})
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
        self.assertNotIn("d.research_feed_version", dashboard.HTML)
        self.assertNotIn("d.research.optional", dashboard.HTML)
        self.assertNotIn("d.trader.heartbeat.research_available", dashboard.HTML)

    def test_dashboard_exposes_edge_ledger_status(self):
        from research.edge_lab import init_ledger
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime" / "research").mkdir(parents=True)
            (root / "config.yaml").write_text(
                "mode: paper\nstrategy:\n  id: ibr\n  version: v1\n",
                encoding="utf-8")
            init_ledger(root / "runtime" / "research" / "edge_lab.sqlite3")
            snapshot = dashboard.snapshot(root)
            self.assertTrue(snapshot["edge"]["available"])
            self.assertEqual(snapshot["edge"]["status"], "ready")

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
