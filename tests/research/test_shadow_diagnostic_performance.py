"""Complexity regressions for the bounded live-shadow diagnostic cadence."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from deploy.recorder_market import _event_key
from research import live_shadow as live_shadow_module
from research.diagnostic_accounts import DiagnosticAccountBook
from research.diagnostic_shadow import build_diagnostic_cohort
from research.edge_ledger import EdgeLedger
from research.live_shadow import ShadowConfig, ShadowRunner, ShadowStore


FIELDS = [
    "event_key", "event_type", "symbol", "timestamp", "as_of",
    "observed_at", "provider", "feed", "open", "high", "low",
    "close", "volume", "bid", "ask",
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
        "universe": {"symbols": ["SPY", "QQQ"],
                     "asset_classes": ["us_equity"],
                     "min_price": 1.0, "max_symbols": 2,
                     "denylist": []},
        "risk": {"risk_per_trade_pct": 0.5,
                 "daily_loss_limit_pct": 2.0,
                 "max_open_risk_pct": 2.0,
                 "max_concurrent_positions": 3,
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
                  "provenance": "test_shadow_diagnostic_performance"},
        "research": {"enabled": True,
                     "require_validated_variant": True},
    }


class _CountingConnection:
    def __init__(self, connection, counter: dict[str, int]):
        self._connection = connection
        self._counter = counter

    def execute(self, *args, **kwargs):
        self._counter["execute"] += 1
        return self._connection.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class ShadowDiagnosticPerformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.corpus = self.root / "recorded.csv"
        self.edge = self.root / "edge.sqlite3"
        self.shadow = self.root / "shadow.sqlite3"
        EdgeLedger(self.edge)
        with self.corpus.open("w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=FIELDS).writeheader()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _bar(symbol: str, stamp: datetime) -> dict[str, str]:
        available = stamp + timedelta(minutes=1)
        return {
            "event_key": _event_key("bar_1m", symbol, stamp.isoformat()),
            "event_type": "bar_1m", "symbol": symbol,
            "timestamp": stamp.isoformat(), "as_of": available.isoformat(),
            "observed_at": available.isoformat(), "provider": "alpaca",
            "feed": "iex", "open": "100", "high": "101", "low": "99",
            "close": "100", "volume": "1000", "bid": "", "ask": "",
        }

    @staticmethod
    def _quote(symbol: str, stamp: datetime) -> dict[str, str]:
        return {
            "event_key": _event_key("quote", symbol, stamp.isoformat()),
            "event_type": "quote", "symbol": symbol,
            "timestamp": stamp.isoformat(), "as_of": stamp.isoformat(),
            "observed_at": stamp.isoformat(), "provider": "alpaca",
            "feed": "iex", "open": "", "high": "", "low": "",
            "close": "", "volume": "", "bid": "99.95", "ask": "100.05",
        }

    def _append(self, rows: list[dict[str, str]]) -> None:
        with self.corpus.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=FIELDS).writerows(rows)

    def test_poll_ingestion_uses_constant_database_connections(self):
        start = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)
        rows = [self._bar("SPY", start + timedelta(minutes=index))
                for index in range(240)]
        self._append(rows)
        runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, diagnostic=False,
            max_events=500, max_decisions=500))

        with patch.object(
                runner.store, "_connect", wraps=runner.store._connect) as connect:
            result = runner.run_once()

        self.assertEqual(result["ingested_events"], len(rows))
        self.assertLess(connect.call_count, len(rows) // 4)

    def test_repeated_trace_batch_deduplicates_before_sql(self):
        store = ShadowStore(self.shadow)
        cohort_identity = "shadow:diagnostic:cohort:test"
        candidate_id = "shadow:diagnostic:candidate:test"
        store.upsert_candidate({
            "candidate_id": candidate_id, "variant_id": "rule.test",
            "strategy_id": "rule", "vehicle": "equity",
            "status": "diagnostic",
            "config": {"diagnostic_shadow": {
                "diagnostic_only": True,
                "cohort_identity": cohort_identity,
            }},
        })
        decisions = [{
            "candidate_id": candidate_id,
            "event_key": f"event-{index}",
            "session_date": "2026-09-08", "symbol": "SPY",
            "kind": "reject", "reason": "same rejection",
            "payload": {}, "plan": None,
        } for index in range(2_000)]
        counter = {"execute": 0}
        original_connect = store._connect

        def connect():
            return _CountingConnection(original_connect(), counter)

        with patch.object(store, "_connect", side_effect=connect):
            inserted = store.record_diagnostic_batch(
                cohort_identity=cohort_identity,
                candidate_id=candidate_id,
                cursor_inserted_at=2_000.0,
                cursor_event_key="event-1999",
                processed_events=2_000,
                rollups={"2026-09-08": {"reject": 2_000}},
                pending_sessions=[], decisions=decisions,
                warmup_session=None, max_decisions=100,
                cohort_candidate_ids=[candidate_id])

        self.assertEqual(inserted, 1)
        self.assertLess(counter["execute"], 10)
        self.assertEqual(store.decision_count(), 1)

    def test_diagnostic_poll_prepares_rows_once_and_fast_paths_empty_books(self):
        runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, diagnostic=True,
            runtime_config=_runtime_config(),
            runtime_config_path="/mounted/config.yaml",
            max_events=500, diagnostic_session_max_events=1_000,
            max_decisions=1_000, max_workers=4))
        first = runner.run_once()
        activation = runner.store.diagnostic_activation(
            first["diagnostic_shadow"]["cohort_identity"])
        day = datetime.fromisoformat(activation["activated_at"]).astimezone(
            ZoneInfo("America/New_York")).date() + timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        start = datetime.combine(
            day, datetime.min.time(),
            tzinfo=ZoneInfo("America/New_York")).replace(
                hour=9, minute=30).astimezone(timezone.utc)
        rows = []
        for index in range(4):
            stamp = start + timedelta(minutes=index)
            for symbol in ("SPY", "QQQ"):
                rows.append(self._quote(symbol, stamp + timedelta(seconds=30)))
                rows.append(self._bar(symbol, stamp))
        self._append(rows)

        with patch.object(
                live_shadow_module, "_diagnostic_projected_payload",
                wraps=live_shadow_module._diagnostic_projected_payload) as prepare, \
                patch.object(
                    runner, "_build_diagnostic_market_views",
                    wraps=runner._build_diagnostic_market_views) as market_views, \
                patch.object(
                    ShadowRunner, "_evaluate",
                    return_value=("reject", "probe rejection", {}, None)) as evaluate, \
                patch.object(
                    DiagnosticAccountBook, "advance_quote_event",
                    side_effect=AssertionError("empty book scanned quote history")), \
                patch.object(
                    DiagnosticAccountBook, "advance_completed_bar",
                    side_effect=AssertionError("empty book scanned bar history")):
            result = runner.run_once()

        self.assertEqual(result["candidate_errors"], {})
        self.assertEqual(prepare.call_count, len(rows) * 2)
        self.assertEqual(market_views.call_count, 1)
        self.assertEqual(evaluate.call_count, 24 * 8)
        self.assertEqual(
            result["diagnostic_shadow"]["processed_events"], 24 * len(rows))

    def test_coverage_uses_filtered_rollups_not_full_table_materialization(self):
        runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, diagnostic=True,
            runtime_config=_runtime_config(),
            runtime_config_path="/mounted/config.yaml"))
        runner.run_once()
        cohort, _arms, activation = runner._prepare_diagnostic_cohort()

        with patch.object(
                runner.store, "decisions",
                side_effect=AssertionError("full decisions table read")), \
                patch.object(
                    runner.store, "replay_accounts",
                    side_effect=AssertionError("full replay account table read")):
            coverage = runner._diagnostic_coverage(cohort, activation)

        self.assertTrue(coverage["enabled"])
        self.assertEqual(coverage["candidate_identities"],
                         sorted(cohort["candidate_identities"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
