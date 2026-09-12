"""Focused checks for the bounded offline IBR diagnostic adapter."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.diagnostic_shadow import build_diagnostic_cohort
from research.ibr_diagnostic import run_offline_forward_ibr
from research.source_validation import source_content_hash


UTC = timezone.utc
OPEN = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 9, 8, 20, 0, tzinfo=UTC)


def _runtime() -> dict:
    return {
        "mode": "paper",
        "broker": {
            "provider": "alpaca", "data_feed": "iex",
            "options_feed": "opra", "paper": True, "allow_live": False,
        },
        "session": {
            "timezone": "America/New_York",
            "entries_regular_session_only": True,
            "allow_exits_outside_session": True,
            "require_exact_calendar": True,
            "force_flat_minutes_before_close": 10,
            "reject_new_entries_minutes_before_close": 5,
        },
        "universe": {
            "symbols": ["SPY"], "asset_classes": ["us_equity"],
            "min_price": 1.0, "max_symbols": 1, "denylist": [],
        },
        "strategy": {
            "id": "ibr", "version": "v1", "variant_id": "ibr.baseline",
            "execution_mode": "shares", "range_minutes": 15,
            "breakout_buffer_bps": 5.0, "min_relative_volume": 1.0,
            "target_r": 2.0, "max_entry_extension_r": 1.0,
            "min_ibr_width_atr": 0.25, "max_ibr_width_atr": 3.0,
            "atr_period": 14, "max_ibr_width_pct": 2.0,
            "stale_minutes": 0.5, "max_spread_bps": 25.0,
            "latest_entry_time": "15:00",
            "force_flat_minutes_before_close": 10,
        },
        "risk": {
            "risk_per_trade_pct": 0.5, "daily_loss_limit_pct": 2.0,
            "max_open_risk_pct": 2.0, "max_concurrent_positions": 3,
            "max_position_notional_pct": 25.0,
            "max_gross_exposure_pct": 50.0,
            "stressed_cost_scenario_bps": 25.0,
            "max_stressed_cost_to_risk_ratio": 0.30,
        },
        "execution": {
            "order_type": "market", "time_in_force": "day",
            "max_slippage_bps": 50.0,
            "max_market_data_age_seconds": 30.0,
            "max_spread_bps": 100.0, "strict_market_data": True,
        },
        "costs": {
            "spread_bps": 4.0, "slippage_bps": 6.0, "fee_bps": 0.5,
            "provenance": "ibr-offline-adapter-test",
        },
        "research": {"enabled": True, "require_validated_variant": True},
    }


def _calendar_fields() -> dict:
    return {
        "session_open": OPEN.isoformat(),
        "session_close": CLOSE.isoformat(),
    }


def _bar(index: int, *, opened: float = 100.0, high: float = 100.5,
         low: float = 99.5, close: float = 100.0,
         volume: float = 1000.0) -> dict:
    stamp = OPEN + timedelta(minutes=index)
    ended = stamp + timedelta(minutes=1)
    observed = ended + (timedelta(seconds=5) if index == 15 else timedelta())
    return {
        "kind": "bar", "symbol": "SPY", "timestamp": stamp.isoformat(),
        "as_of": ended.isoformat(), "observed_at": observed.isoformat(),
        "provider": "alpaca", "feed": "iex",
        "source_mode": "forward_observed", "open": opened,
        "high": high, "low": low, "close": close, "volume": volume,
        **_calendar_fields(),
    }


def _quote(at: datetime) -> dict:
    return {
        "kind": "quote", "symbol": "SPY", "timestamp": at.isoformat(),
        "as_of": at.isoformat(), "observed_at": at.isoformat(),
        "provider": "alpaca", "feed": "iex",
        "source_mode": "forward_observed", "bid": 101.2, "ask": 101.4,
        **_calendar_fields(),
    }


def _source() -> list[dict]:
    rows = [_bar(index) for index in range(15)]
    rows.append(_bar(
        15, opened=100.2, high=101.4, low=100.1,
        close=101.2, volume=2000.0))
    signal_at = datetime.fromisoformat(rows[-1]["observed_at"])
    rows.append(_quote(signal_at))
    rows.append(_bar(
        16, opened=101.5, high=101.8, low=101.0, close=101.6,
        volume=1200.0))
    rows.append(_bar(
        17, opened=101.6, high=106.0, low=101.2, close=105.0,
        volume=1200.0))
    return rows


def _complete_source() -> list[dict]:
    rows = _source()
    rows.append(_bar(
        389, opened=100.0, high=100.4, low=99.6, close=100.0,
        volume=1000.0))
    rows.append(_quote(CLOSE))
    return rows


def _continuous_source() -> list[dict]:
    close = OPEN + timedelta(minutes=60)
    rows: list[dict] = []
    for index in range(60):
        bar = _bar(index)
        bar["session_close"] = close.isoformat()
        rows.append(bar)
        quote = _quote(datetime.fromisoformat(bar["observed_at"]))
        quote["session_close"] = close.isoformat()
        rows.append(quote)
    return rows


class OfflineIbrDiagnosticAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = _runtime()
        cohort = build_diagnostic_cohort(
            self.runtime, code_identity="a" * 64, include_ibr=True)
        self.arms = [deepcopy(arm) for arm in cohort["arms"]
                     if arm["strategy_id"] == "ibr"]
        self.assertEqual(7, len(self.arms))

    def test_jsonl_replay_is_deterministic_ledger_free_and_reconciled(self):
        rows = _source()
        frozen_arms = deepcopy(self.arms)
        report_hint = {"content_hash": source_content_hash(rows), "rows": 999}
        with tempfile.TemporaryDirectory(prefix="ibr-offline-adapter-") as root:
            source = Path(root) / "forward.jsonl"
            source.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8")
            with patch("research.live_shadow.ShadowStore") as store, patch(
                    "research.live_shadow._read_factory_rule_roots") as roots:
                first = run_offline_forward_ibr(
                    source, runtime_config=self.runtime, arms=self.arms,
                    source_report=report_hint)
                second = run_offline_forward_ibr(
                    source, runtime_config=self.runtime, arms=self.arms,
                    source_report=report_hint)
            store.assert_not_called()
            roots.assert_not_called()

        self.assertEqual(first, second)
        self.assertEqual(frozen_arms, self.arms)
        self.assertEqual("offline-forward-ibr-diagnostic.v1", first["schema"])
        self.assertEqual("measured", first["status"])
        self.assertEqual(7, first["diagnostic"]["arms_measured"])
        self.assertEqual(len(rows), first["source"]["rows"])
        self.assertTrue(first["source"]["supplied_report_hash_verified"])
        self.assertFalse(first["broker_equivalence"])
        self.assertFalse(first["promotion_eligible"])
        baseline = first["arms"]["ibr.baseline"]
        self.assertEqual("measured", baseline["status"])
        self.assertEqual(1, len(baseline["rows"]))
        row = baseline["rows"][0]
        self.assertFalse(row["no_trade"], baseline)
        self.assertGreater(row["net_pnl"], 0.0)
        self.assertAlmostEqual(
            row["net_pnl"], baseline["account"]["realized_pnl"])
        self.assertEqual(1, baseline["diagnostic"]["executed_trades"])
        self.assertTrue(baseline["account"]["realized_pnl_reconciled"])
        self.assertFalse(baseline["broker_equivalence"])
        self.assertEqual([], baseline["proofs"])
        open_boundary = first["arms"]["ibr.target.3r"]
        self.assertEqual("missing_data", open_boundary["outcome"])
        self.assertTrue(open_boundary["rows"][0]["no_trade"])
        self.assertIsNone(open_boundary["rows"][0]["net_pnl"])

    def test_generated_signal_refusal_is_not_reported_as_no_signal(self):
        runtime = deepcopy(self.runtime)
        runtime["risk"]["max_stressed_cost_to_risk_ratio"] = 0.0
        cohort = build_diagnostic_cohort(
            runtime, code_identity="b" * 64, include_ibr=True)
        arms = [deepcopy(arm) for arm in cohort["arms"]
                if arm["strategy_id"] == "ibr"]
        result = run_offline_forward_ibr(
            _source(), runtime_config=runtime, arms=arms)
        baseline = result["arms"]["ibr.baseline"]
        self.assertEqual("execution_blocked", baseline["outcome"])
        self.assertEqual("refused", baseline["rows"][0][
            "execution_disposition"])
        self.assertTrue(baseline["rows"][0]["signal_opportunity"])
        self.assertIsNone(baseline["rows"][0]["net_pnl"])
        self.assertEqual({"SPY": "2026-09-08"},
                         baseline["account"]["signal_sessions"])

    def test_source_failures_are_unavailable_not_zero_measurements(self):
        cases: list[tuple[str, list[dict], dict]] = []
        no_quotes = [row for row in _source() if row["kind"] != "quote"]
        cases.append(("missing_quotes", no_quotes, {}))

        historical = deepcopy(_source())
        historical[0]["source_mode"] = "historical_backfill"
        cases.append(("historical_source", historical, {}))

        mixed = deepcopy(_source())
        mixed[-1]["source_mode"] = "historical_backfill"
        cases.append(("mixed_source_modes", mixed, {}))

        wrong_feed = deepcopy(_source())
        wrong_feed[-1]["feed"] = "sip"
        cases.append(("feed_mismatch", wrong_feed, {}))

        missing_calendar = deepcopy(_source())
        for row in missing_calendar:
            row.pop("session_open")
            row.pop("session_close")
        cases.append(("calendar_missing", missing_calendar, {}))

        conflicting_calendar = deepcopy(_source())
        conflicting_calendar[-1]["session_close"] = (
            CLOSE - timedelta(minutes=30)).isoformat()
        cases.append(("calendar_conflict", conflicting_calendar, {}))

        for reason, rows, kwargs in cases:
            with self.subTest(reason=reason), patch(
                    "research.live_shadow.ShadowStore") as store, patch(
                    "research.live_shadow._read_factory_rule_roots") as roots:
                result = run_offline_forward_ibr(
                    rows, runtime_config=self.runtime, arms=self.arms, **kwargs)
                self.assertEqual("unavailable", result["status"], result)
                self.assertIn(reason, result["reason_codes"], result)
                self.assertIsNone(result["diagnostic"])
                self.assertTrue(all(
                    arm["diagnostic"] is None and arm["rows"] == []
                    for arm in result["arms"].values()))
                store.assert_not_called()
                roots.assert_not_called()

    def test_no_signal_requires_a_complete_priced_session_boundary(self):
        partial = run_offline_forward_ibr(
            _source(), runtime_config=self.runtime, arms=self.arms)
        complete = run_offline_forward_ibr(
            _continuous_source(), runtime_config=self.runtime, arms=self.arms)
        self.assertEqual(
            "missing_data", partial["arms"]["ibr.range.45"]["outcome"])
        arm = complete["arms"]["ibr.range.45"]
        self.assertEqual("no_signal", arm["outcome"], arm)
        self.assertEqual("no_signal", arm["rows"][0]["execution_disposition"])
        self.assertIsNone(arm["rows"][0]["net_pnl"])

    def test_mutated_arm_config_is_rejected_even_with_matching_metadata(self):
        tampered = deepcopy(self.arms)
        tampered[0]["config"]["strategy"]["target_r"] = 99.0
        tampered[0]["config_identity"] = "f" * 64
        tampered[0]["config"]["diagnostic_shadow"]["config_identity"] = "f" * 64
        with patch(
                "research.ibr_diagnostic.ShadowRunner.for_offline_diagnostic",
                side_effect=AssertionError("unexpected shadow replay")) as worker:
            with self.assertRaisesRegex(ValueError, "frozen cohort"):
                run_offline_forward_ibr(
                    _source(), runtime_config=self.runtime, arms=tampered)
        worker.assert_not_called()

    def test_budget_and_source_report_hash_fail_before_shadow_replay(self):
        rows = _source()
        with patch(
                "research.ibr_diagnostic.ShadowRunner.for_offline_diagnostic",
                side_effect=AssertionError("unexpected shadow replay")) as worker:
            budget = run_offline_forward_ibr(
                rows, runtime_config=self.runtime, arms=self.arms,
                max_events=len(rows) - 1)
            mismatch = run_offline_forward_ibr(
                rows, runtime_config=self.runtime, arms=self.arms,
                source_report={"content_hash": "0" * 64})
        self.assertEqual("unavailable", budget["status"])
        self.assertIn("event_budget_exhausted", budget["reason_codes"])
        self.assertIsNone(budget["diagnostic"])
        self.assertEqual("unavailable", mismatch["status"])
        self.assertIn("source_report_hash_mismatch", mismatch["reason_codes"])
        self.assertIsNone(mismatch["diagnostic"])
        worker.assert_not_called()

    def test_missing_event_keys_are_content_addressed(self):
        from research.ibr_diagnostic import _project_events

        projected, wrappers = _project_events(_source())
        self.assertEqual(len(projected), len(wrappers))
        self.assertTrue(all(
            str(row["event_key"]).startswith("offline:")
            for row in projected))
        again, _ = _project_events(_source())
        self.assertEqual(
            [row["event_key"] for row in projected],
            [row["event_key"] for row in again])


if __name__ == "__main__":
    unittest.main()
