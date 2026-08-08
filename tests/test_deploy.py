"""Dependency-light checks for the paper deployment boundary."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
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
            self.assertTrue(snapshot["research"]["optional"])
            self.assertNotIn("gates", snapshot)

    def test_report_is_a_small_usd_paper_summary(self):
        with sqlite3.connect(":memory:") as db:
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
