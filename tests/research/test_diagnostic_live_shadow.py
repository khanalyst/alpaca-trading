"""Non-authorizing coverage and epoch rollover tests for live diagnostics."""

from __future__ import annotations

import csv
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from deploy.recorder_market import _event_key
from agent.registry import validate_contract_config
from research.diagnostic_shadow import (
    DIAGNOSTIC_CANDIDATE_PREFIX, build_diagnostic_cohort,
    is_diagnostic_candidate,
)
from research.edge_ledger import EdgeLedger
from research.live_shadow import (
    ShadowConfig, ShadowError, ShadowRunner, ShadowStore, _digest,
    _manifest_replay_identity, _read_candidates,
)


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
        "universe": {"symbols": ["SPY", "QQQ"], "asset_classes": ["us_equity"],
                     "min_price": 1.0, "max_symbols": 2, "denylist": []},
        "risk": {"risk_per_trade_pct": 0.5, "daily_loss_limit_pct": 2.0,
                 "max_open_risk_pct": 2.0, "max_concurrent_positions": 3,
                 "max_position_notional_pct": 25.0,
                 "max_gross_exposure_pct": 50.0,
                 "stressed_cost_scenario_bps": 25.0,
                 "max_stressed_cost_to_risk_ratio": 0.30},
        "execution": {"order_type": "market", "time_in_force": "day",
                      "max_slippage_bps": 50,
                      "max_market_data_age_seconds": 30,
                      "max_spread_bps": 100, "strict_market_data": True},
        "costs": {"spread_bps": 4.0, "slippage_bps": 6.0,
                  "fee_bps": 0.5,
                  "provenance": "test_unchanged_runtime_costs"},
        "research": {"enabled": True, "require_validated_variant": True},
    }


class DiagnosticCohortTests(unittest.TestCase):
    def test_fixed_cohort_has_24_distinct_deterministic_one_factor_arms(self):
        mounted = _runtime_config()
        mounted["broker"].update(api_key="do-not-persist",
                                  secret_key="do-not-persist")
        first = build_diagnostic_cohort(mounted, code_identity="a" * 64)
        second = build_diagnostic_cohort(mounted, code_identity="a" * 64)
        self.assertEqual(first, second)
        self.assertEqual(first["families_total"], 12)
        self.assertEqual(first["registered_arms"], 24)
        self.assertEqual(len(set(first["candidate_identities"])), 24)
        self.assertTrue(all(candidate.startswith(DIAGNOSTIC_CANDIDATE_PREFIX)
                            for candidate in first["candidate_identities"]))
        by_family: dict[str, dict[str, dict]] = {}
        for arm in first["arms"]:
            by_family.setdefault(arm["family"], {})[arm["role"]] = arm
            marker = arm["config"]["diagnostic_shadow"]
            self.assertTrue(marker["diagnostic_only"])
            self.assertFalse(marker["authorizing"])
            self.assertEqual(marker["config_identity"],
                             arm["config_identity"])
            self.assertNotIn("api_key", arm["config"]["broker"])
            self.assertNotIn("secret_key", arm["config"]["broker"])
            validate_contract_config(arm["config"])
        self.assertEqual(len(by_family), 12)
        for roles in by_family.values():
            self.assertEqual(set(roles), {"baseline", "variant"})
            baseline = roles["baseline"]["rule_spec"]
            variant = roles["variant"]["rule_spec"]
            changed = [key for key in sorted(set(baseline) | set(variant))
                       if baseline.get(key) != variant.get(key)]
            self.assertEqual(changed, [roles["variant"]["variant_axis"]])

        changed_config = _runtime_config()
        changed_config["risk"]["risk_per_trade_pct"] = 0.75
        next_epoch = build_diagnostic_cohort(
            changed_config, code_identity="a" * 64)
        self.assertNotEqual(first["cohort_identity"], next_epoch["cohort_identity"])
        self.assertTrue(set(first["candidate_identities"]).isdisjoint(
            next_epoch["candidate_identities"]))


class DiagnosticLiveShadowTests(unittest.TestCase):
    FIELDS = [
        "event_key", "event_type", "symbol", "timestamp", "as_of",
        "observed_at", "provider", "feed", "open", "high", "low",
        "close", "volume", "bid", "ask",
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.corpus = self.root / "recorded.csv"
        self.edge = self.root / "edge.sqlite3"
        self.shadow = self.root / "shadow.sqlite3"
        with self.corpus.open("w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=self.FIELDS).writeheader()

    def tearDown(self):
        self.tmp.cleanup()

    def _config(self) -> ShadowConfig:
        return ShadowConfig(
            self.corpus, self.edge, self.shadow, diagnostic=True,
            runtime_config=_runtime_config(), runtime_config_path="/mounted/config.yaml",
            max_events=200, max_decisions=10_000, max_workers=4)

    def _append_bar(self, minute: int, *,
                    observed_at: datetime | None = None) -> None:
        stamp = datetime(2026, 9, 8, 14, minute, tzinfo=timezone.utc)
        observed = observed_at or (stamp + timedelta(minutes=1))
        row = {
            "event_key": _event_key("bar_1m", "SPY", stamp.isoformat()),
            "event_type": "bar_1m", "symbol": "SPY",
            "timestamp": stamp.isoformat(),
            "as_of": (stamp + timedelta(minutes=1)).isoformat(),
            "observed_at": observed.isoformat(),
            "provider": "alpaca", "feed": "iex", "open": "100",
            "high": "101", "low": "99", "close": "100",
            "volume": "1000", "bid": "", "ask": "",
        }
        with self.corpus.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=self.FIELDS).writerow(row)

    def test_preregistration_rejects_preactivation_events_and_restart_is_stable(self):
        self._append_bar(30)
        with patch.object(
                ShadowRunner, "_evaluate",
                side_effect=AssertionError("preactivation event evaluated")):
            first = ShadowRunner(self._config()).run_once()
        coverage = first["diagnostic_shadow"]
        self.assertEqual(coverage["families_covered"], 12)
        self.assertEqual(coverage["baseline_count"], 12)
        self.assertEqual(coverage["variant_count"], 12)
        self.assertEqual(coverage["decision_counts"]["total"], 0)
        self.assertEqual(coverage["rejection_counts"]["preactivation"], 24)
        self.assertEqual(coverage["observation_status"],
                         "no_post_activation_events")

        restarted = ShadowRunner(self._config()).run_once()["diagnostic_shadow"]
        self.assertEqual(coverage["cohort_identity"], restarted["cohort_identity"])
        self.assertEqual(coverage["activation_identity"],
                         restarted["activation_identity"])
        self.assertEqual(coverage["candidate_identities"],
                         restarted["candidate_identities"])

        self._append_bar(
            31, observed_at=datetime.now(timezone.utc) + timedelta(seconds=1))

        def unpriced(_runner, _candidate, _event, _bars, _quotes, _options):
            return "unpriced", "stale or unavailable quote", {"signal": {
                "direction": "long"}}, None

        with patch.object(ShadowRunner, "_evaluate", new=unpriced), \
                patch.object(ShadowRunner, "_replay", return_value=True):
            forward = ShadowRunner(self._config()).run_once()
        coverage = forward["diagnostic_shadow"]
        self.assertEqual(coverage["decision_counts"]["this_poll"], 24)
        self.assertEqual(coverage["quoteable_virtual_opens"], 0)
        self.assertEqual(coverage["unpriced_virtual_opens"], 24)
        self.assertEqual(coverage["replay_modeled_fills"], 0)
        self.assertEqual(coverage["actual_fills"], 0)
        self.assertFalse(coverage["actual_fill_claims"])
        payload = json.loads(ShadowStore(self.shadow).decisions()[0][
            "payload_json"])["diagnostic_shadow"]
        self.assertEqual(payload["activation_event_watermark"],
                         coverage["activation_event_watermark"])
        self.assertEqual(payload["cohort_identity"],
                         coverage["cohort_identity"])
        self.assertEqual(coverage["observation_status"],
                         "signals_unpriced_no_virtual_fill_claim")

    def test_equal_cursor_retry_is_exactly_once(self):
        runner = ShadowRunner(self._config())
        activated = runner.run_once()
        coverage = activated["diagnostic_shadow"]
        cohort_identity = coverage["cohort_identity"]
        candidate_id = coverage["candidate_identities"][0]
        activation = runner.store.diagnostic_activation(cohort_identity)
        kwargs = {
            "cohort_identity": cohort_identity,
            "candidate_id": candidate_id,
            "cursor_inserted_at": 1.0,
            "cursor_event_key": "event-1",
            "processed_events": 1,
            "rollups": {"2026-09-08": {"no_trade": 1}},
            "pending_sessions": [],
            "decisions": [],
            "warmup_session": activation["warmup_session"],
            "max_decisions": 100,
        }
        self.assertEqual(runner.store.record_diagnostic_batch(**kwargs), 0)
        self.assertEqual(runner.store.record_diagnostic_batch(**kwargs), 0)
        progress = runner.store.diagnostic_progress(
            cohort_identity, [candidate_id], activation)[candidate_id]
        self.assertEqual(progress["processed_events"], 1)
        self.assertEqual(
            progress["rollups"]["cumulative"]["no_trade"], 1)

        conflicting = dict(kwargs)
        conflicting["processed_events"] = 2
        with self.assertRaisesRegex(ShadowError, "equal-cursor batch conflicts"):
            runner.store.record_diagnostic_batch(**conflicting)

    def test_diagnostic_candidates_never_write_edge_or_expose_gate_rows(self):
        EdgeLedger(self.edge)
        edge_before = self.edge.read_bytes()
        result = ShadowRunner(self._config()).run_once()
        self.assertEqual(edge_before, self.edge.read_bytes())
        store = ShadowStore(self.shadow)
        candidates = store.candidates()
        self.assertEqual(len(candidates), 24)
        for candidate in candidates:
            proof = json.loads(candidate["proof_json"])
            self.assertEqual(proof, {
                "authorizing": False,
                "diagnostic_only": True,
                "gate_eligible": False,
                "promotion_eligible": False,
            })
            store.replay_diff(
                candidate_id=candidate["candidate_id"],
                session_date="2026-09-08", source_digest="source",
                shadow_digest="shadow", replay_digest="replay",
                status="match", details={"complete": True})
            store.record_replay_evidence(
                candidate_id=candidate["candidate_id"],
                session_date="2026-09-08", replay_digest="replay",
                vehicle="equity", starting_cash=100_000,
                ending_cash=100_001, realized_pnl=1, trades=[{"net_pnl": 1}],
                replay_status="match")
            self.assertEqual(store.gate_rows(candidate["candidate_id"]), [])
        self.assertEqual(store.gate_sessions(), [])
        self.assertFalse(result["diagnostic_shadow"]["authorizing"])

        with closing(sqlite3.connect(self.shadow)) as db, db:
            db.execute(
                "UPDATE replay_diffs SET created_at=0 WHERE candidate_id LIKE ?",
                (f"{DIAGNOSTIC_CANDIDATE_PREFIX}%",))
        self.assertEqual(store.prune()["pruned_replay_diffs"], 0)
        self.assertEqual(len(store.replay_metadata()), 24)

    def test_diagnostic_replay_is_modeled_only_and_skips_null_proof_lane(self):
        runner = ShadowRunner(self._config())
        arm = build_diagnostic_cohort(
            _runtime_config(), code_identity="a" * 64)["arms"][0]
        bar = {
            "event_key": "closing-bar", "event_type": "bar_1m",
            "symbol": "SPY", "timestamp": "2026-01-02T20:59:00+00:00",
            "as_of": "2026-01-02T21:00:00+00:00",
            "observed_at": "2026-01-02T21:00:00+00:00",
            "provider": "alpaca", "feed": "iex", "open": 100,
            "high": 101, "low": 99, "close": 100, "volume": 1000,
        }
        account = {
            "starting_cash": 100_000, "ending_equity": 100_001,
            "realized_pnl": 1,
            "rows": [{"symbol": "SPY", "session_date": "2026-01-02",
                      "net_pnl": 1, "return_value": .00001,
                      "no_trade": False}],
        }
        manifest_digest = runner.store.save_manifest({
            "candidate_set": [{"candidate_id": arm["candidate_id"]}],
            "candidate_set_digest": _digest([
                {"candidate_id": arm["candidate_id"]}]),
            "event_watermark": {"count": 1},
        })
        manifest = runner.store.manifest(manifest_digest)
        self.assertIsNotNone(manifest)
        activation = {
            "warmup_session": "2026-01-01",
            "activation_identity": "shadow:diagnostic:activation:test",
        }
        with patch("research.live_shadow.simulate_account",
                   return_value=account), patch(
                       "research.live_shadow.null_control_account",
                       side_effect=AssertionError("diagnostic null proof invoked")):
            self.assertTrue(runner._replay(
                arm, "2026-01-02", [bar], [], [], [],
                replay_identity=_manifest_replay_identity(manifest),
                diagnostic_activation=activation))
        metadata = runner.store.replay_metadata(arm["candidate_id"])
        self.assertEqual(len(metadata), 1)
        self.assertTrue(metadata[0]["status"].startswith("diagnostic_"))
        self.assertEqual(metadata[0]["trade_count"], 1)
        self.assertFalse(metadata[0]["details"]["authorizing"])
        self.assertEqual(metadata[0]["details"]["null_reason"],
                         "diagnostic_non_authorizing")
        self.assertEqual(runner.store.gate_rows(arm["candidate_id"]), [])
        self.assertEqual(runner.store.replay_quarantine(), {})

    def test_activation_session_signals_never_create_books_or_modeled_pnl(self):
        runner = ShadowRunner(self._config())
        first = runner.run_once()
        cohort_id = first["diagnostic_shadow"]["cohort_identity"]
        activation = runner.store.diagnostic_activation(cohort_id)
        self.assertIsNotNone(activation)
        activated_at = datetime.fromisoformat(activation["activated_at"])
        session = activated_at.astimezone(
            ZoneInfo("America/New_York")).date().isoformat()
        close = datetime.combine(
            datetime.fromisoformat(session).date(),
            datetime.min.time(), tzinfo=ZoneInfo("America/New_York"))
        close = close.replace(hour=16).astimezone(timezone.utc)
        observed = max(activated_at + timedelta(seconds=1),
                       close + timedelta(seconds=1))
        row = {
            "event_key": "warmup-close", "event_type": "bar_1m",
            "symbol": "SPY",
            "timestamp": (close - timedelta(minutes=1)).isoformat(),
            "as_of": close.isoformat(),
            "observed_at": observed.isoformat(),
            "provider": "alpaca", "feed": "iex", "open": 100,
            "high": 101, "low": 99, "close": 100, "volume": 1000,
        }
        runner.store.ingest_event(
            row, max_events=200, source_path="synthetic", source_offset_start=0,
            source_offset_end=1)

        def signal(_runner, _candidate, event, _bars, _quotes, _options):
            return ("open_incomplete", "probe", {"session_date": session},
                    {"symbol": event["symbol"], "shares": 1,
                     "entry_price": 100.0})

        with patch.object(ShadowRunner, "_evaluate", new=signal), patch(
                "research.live_shadow._session_close",
                return_value=(close, "probe")), patch(
                "research.live_shadow.simulate_account",
                side_effect=AssertionError("warmup replay evaluated")):
            result = runner.run_once()
        candidate_ids = set(result["diagnostic_shadow"]["candidate_identities"])
        self.assertEqual(result["diagnostic_shadow"]["decision_counts"]["total"], 24)
        self.assertEqual(result["diagnostic_shadow"]["quoteable_virtual_opens"], 0)
        self.assertEqual(result["diagnostic_shadow"]["warmup_quoteable_signals"], 24)
        self.assertEqual(result["diagnostic_shadow"]["observation_status"],
                         "warmup_signals_not_evaluated")
        with closing(sqlite3.connect(self.shadow)) as db:
            self.assertEqual(db.execute(
                "SELECT count(*) FROM virtual_books").fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT count(*) FROM shadow_accounts").fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT count(*) FROM shadow_trades").fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT count(*) FROM diagnostic_accounts").fetchone()[0], 24)
            self.assertEqual(db.execute(
                "SELECT count(*) FROM diagnostic_positions").fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT count(*) FROM diagnostic_fills").fetchone()[0], 0)
        forward_accounts = result["diagnostic_shadow"]["forward_accounts"]
        self.assertEqual(forward_accounts["account_count"], 24)
        self.assertEqual(forward_accounts["cash"], 2_400_000.0)
        self.assertEqual(forward_accounts["modeled_fills"], 0)
        self.assertEqual(forward_accounts["realized_pnl"], 0.0)
        metadata = [row for row in runner.store.replay_metadata()
                    if row["candidate_id"] in candidate_ids]
        self.assertEqual(len(metadata), 24)
        self.assertTrue(all(row["status"] == "warmup_not_evaluated"
                            for row in metadata))

    def test_postactivation_historical_append_is_rejected_once(self):
        runner = ShadowRunner(self._config())
        runner.run_once()
        old = {
            "event_key": "late-historical", "event_type": "bar_1m",
            "symbol": "SPY", "timestamp": "2026-09-04T19:59:00+00:00",
            "as_of": "2026-09-04T20:00:00+00:00",
            "observed_at": "2026-09-04T20:00:00+00:00",
            "provider": "alpaca", "feed": "iex", "open": 100,
            "high": 101, "low": 99, "close": 100, "volume": 1000,
        }
        runner.store.ingest_event(
            old, max_events=200, source_path="historical", source_offset_start=0,
            source_offset_end=1)
        with patch.object(
                ShadowRunner, "_evaluate",
                side_effect=AssertionError("historical append evaluated")):
            rejected = runner.run_once()["diagnostic_shadow"]
            repeated = runner.run_once()["diagnostic_shadow"]
        self.assertEqual(rejected["rejection_counts"][
            "preactivation_this_poll"], 24)
        self.assertEqual(repeated["rejection_counts"][
            "preactivation_this_poll"], 0)
        self.assertEqual(repeated["decision_counts"]["total"], 0)

    def test_partition_source_marker_rejects_postactivation_backfill(self):
        runner = ShadowRunner(self._config())
        first = runner.run_once()
        activation = runner.store.diagnostic_activation(
            first["diagnostic_shadow"]["cohort_identity"])
        warmup = datetime.fromisoformat(
            str(activation["warmup_session"])).date()
        day = warmup + timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        stamp = datetime.combine(
            day, datetime.min.time(),
            tzinfo=ZoneInfo("America/New_York")).replace(
                hour=9, minute=30).astimezone(timezone.utc)
        source = self.root / "partitions" / f"market-{day.isoformat()}.csv"
        source.parent.mkdir(parents=True)
        marker = source.with_name(source.name + ".source.json")
        marker.write_text(json.dumps({
            "schema": "recorder-partition-source.v1",
            "partition": source.name,
            "source_mode": "historical_backfill",
        }), encoding="utf-8")
        runner.store.ingest_event({
            "event_key": "postactivation-backfill",
            "event_type": "bar_1m", "symbol": "SPY",
            "timestamp": stamp.isoformat(),
            "as_of": (stamp + timedelta(minutes=1)).isoformat(),
            "observed_at": (stamp + timedelta(minutes=1)).isoformat(),
            "provider": "alpaca", "feed": "iex", "open": 100,
            "high": 101, "low": 99, "close": 100, "volume": 1000,
        }, max_events=200, source_path=str(source), source_offset_start=0,
            source_offset_end=1)
        with patch.object(
                ShadowRunner, "_evaluate",
                side_effect=AssertionError("backfill evaluated")):
            coverage = runner.run_once()["diagnostic_shadow"]
        self.assertEqual(coverage["rejection_counts"]["forward_provenance"], 24)
        self.assertEqual(coverage["forward_accounts"]["modeled_fills"], 0)

    def test_old_sep4_wal_with_ibr_never_retroactively_enters_diagnostics(self):
        ledger = EdgeLedger(self.edge)
        ledger.register_candidate(
            "ibr.baseline", strategy_id="ibr", vehicle="equity",
            hypothesis="production baseline",
            config={"strategy": {"id": "ibr"}})
        old_stamp = datetime(2026, 9, 4, 19, 59, tzinfo=timezone.utc)
        old_row = {
            "event_key": _event_key("bar_1m", "SPY", old_stamp.isoformat()),
            "event_type": "bar_1m", "symbol": "SPY",
            "timestamp": old_stamp.isoformat(),
            "as_of": (old_stamp + timedelta(minutes=1)).isoformat(),
            "observed_at": (old_stamp + timedelta(minutes=1)).isoformat(),
            "provider": "alpaca", "feed": "iex", "open": "100",
            "high": "101", "low": "99", "close": "100",
            "volume": "1000", "bid": "", "ask": "",
        }
        with self.corpus.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=self.FIELDS).writerow(old_row)

        diagnostic_events: list[str] = []

        def no_trade(_runner, candidate, event, _bars, _quotes, _options):
            if is_diagnostic_candidate(candidate):
                diagnostic_events.append(str(event.get("event_key") or ""))
            return "no_trade", "probe", {}, None

        class ActivationDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 9, 8, 14, 30, 30,
                            tzinfo=timezone.utc)
                return value if tz is None else value.astimezone(tz)

        runner = ShadowRunner(self._config())
        with patch("research.live_shadow.datetime", ActivationDatetime), \
                patch.object(ShadowRunner, "_evaluate", new=no_trade), \
                patch.object(ShadowRunner, "_replay", return_value=True):
            first = runner.run_once()
        activation = runner.store.diagnostic_activation(
            first["diagnostic_shadow"]["cohort_identity"])
        self.assertEqual(activation["warmup_session"], "2026-09-08")
        self.assertEqual(diagnostic_events, [])

        self._append_bar(31)
        with patch.object(ShadowRunner, "_evaluate", new=no_trade), \
                patch.object(ShadowRunner, "_replay", return_value=True):
            runner.run_once()
        self.assertEqual(len(diagnostic_events), 24)

        historical_append = dict(old_row)
        historical_append["event_key"] = "sep4-postactivation-historical"
        with self.corpus.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=self.FIELDS).writerow(
                historical_append)
        with patch.object(ShadowRunner, "_evaluate", new=no_trade), \
                patch.object(ShadowRunner, "_replay", return_value=True):
            rejected = runner.run_once()["diagnostic_shadow"]
        self.assertEqual(len(diagnostic_events), 24)
        self.assertEqual(rejected["rejection_counts"][
            "preactivation_this_poll"], 24)

    def test_full_24_symbol_bar_and_quote_session_is_incremental_and_bounded(self):
        runtime = _runtime_config()
        symbols = [f"S{index:02d}" for index in range(24)]
        runtime["universe"]["symbols"] = symbols
        runtime["universe"]["max_symbols"] = 24
        config = ShadowConfig(
            self.corpus, self.edge, self.shadow, diagnostic=True,
            runtime_config=runtime, runtime_config_path="/mounted/config.yaml",
            max_events=20_000, diagnostic_session_max_events=200_000,
            max_decisions=24, max_workers=4)
        runner = ShadowRunner(config)
        first = runner.run_once()
        activation = runner.store.diagnostic_activation(
            first["diagnostic_shadow"]["cohort_identity"])
        self.assertIsNotNone(activation)
        day = datetime.fromisoformat(activation["activated_at"]).astimezone(
            ZoneInfo("America/New_York")).date() + timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        start = datetime.combine(
            day, datetime.min.time(), tzinfo=ZoneInfo("America/New_York"))
        start = start.replace(hour=9, minute=30).astimezone(timezone.utc)

        def append_slice(first_minute: int, last_minute: int) -> None:
            with self.corpus.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=self.FIELDS)
                for minute in range(first_minute, last_minute):
                    stamp = start + timedelta(minutes=minute)
                    for symbol in symbols:
                        for second, bid, ask in (
                                (10, "99.95", "100.05"),
                                (50, "99.96", "100.06")):
                            quote_stamp = stamp + timedelta(seconds=second)
                            writer.writerow({
                                "event_key": _event_key(
                                    "quote", symbol, quote_stamp.isoformat()),
                                "event_type": "quote", "symbol": symbol,
                                "timestamp": quote_stamp.isoformat(),
                                "as_of": quote_stamp.isoformat(),
                                "observed_at": quote_stamp.isoformat(),
                                "provider": "alpaca", "feed": "iex",
                                "open": "", "high": "", "low": "",
                                "close": "", "volume": "",
                                "bid": bid, "ask": ask,
                            })
                        writer.writerow({
                            "event_key": _event_key(
                                "bar_1m", symbol, stamp.isoformat()),
                            "event_type": "bar_1m", "symbol": symbol,
                            "timestamp": stamp.isoformat(),
                            "as_of": (stamp + timedelta(minutes=1)).isoformat(),
                            "observed_at": (
                                stamp + timedelta(minutes=1)).isoformat(),
                            "provider": "alpaca", "feed": "iex",
                            "open": "100", "high": "101", "low": "99",
                            "close": "100", "volume": "1000",
                            "bid": "", "ask": "",
                        })

        calls = {"count": 0}

        def reject(_runner, _candidate, _event, _bars, _quotes, _options):
            calls["count"] += 1
            return "reject", "probe rejection", {}, None

        append_slice(0, 195)
        with patch.object(ShadowRunner, "_evaluate", new=reject), \
                patch.object(ShadowRunner, "_replay", return_value=True):
            midpoint = runner.run_once()
            append_slice(195, 390)
            completed = runner.run_once()
            repeated = runner.run_once()
        expected_decisions = 24 * 24 * 390
        expected_context = 24 * 24 * 390 * 3
        self.assertEqual(calls["count"], expected_decisions)
        self.assertEqual(runner.store.event_count(), 24 * 390 * 3)
        self.assertEqual(midpoint["diagnostic_shadow"][
            "session_context_max_events"], 200_000)
        self.assertEqual(midpoint["diagnostic_shadow"][
            "incremental_max_events"], 20_000)
        self.assertEqual(completed["decisions"], 24)
        self.assertEqual(completed["diagnostic_shadow"]["decision_counts"][
            "trace_representatives"], 24)
        self.assertEqual(completed["diagnostic_shadow"]["rejection_counts"][
            "reject"], expected_decisions)
        self.assertEqual(completed["diagnostic_shadow"]["rejection_counts"][
            "by_reason"], {"probe rejection": expected_decisions})
        self.assertEqual(completed["diagnostic_shadow"]["processed_events"],
                         expected_context)
        self.assertEqual(completed["diagnostic_shadow"]["observation_status"],
                         "observed_no_quoteable_virtual_opens")
        self.assertEqual(repeated["decisions"], 24)
        self.assertEqual(repeated["diagnostic_shadow"]["processed_events"],
                         expected_context)

        next_day = day + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        next_stamp = datetime.combine(
            next_day, datetime.min.time(),
            tzinfo=ZoneInfo("America/New_York")).replace(
                hour=9, minute=30).astimezone(timezone.utc)
        with self.corpus.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.FIELDS)
            for symbol in symbols:
                writer.writerow({
                    "event_key": _event_key(
                        "bar_1m", symbol, next_stamp.isoformat()),
                    "event_type": "bar_1m", "symbol": symbol,
                    "timestamp": next_stamp.isoformat(),
                    "as_of": (next_stamp + timedelta(minutes=1)).isoformat(),
                    "observed_at": (
                        next_stamp + timedelta(minutes=1)).isoformat(),
                    "provider": "alpaca", "feed": "iex", "open": "100",
                    "high": "101", "low": "99", "close": "100",
                    "volume": "1000", "bid": "", "ask": "",
                })
        with patch.object(ShadowRunner, "_evaluate", new=reject), \
                patch.object(ShadowRunner, "_replay", return_value=True):
            next_session = runner.run_once()
        expected_next = expected_decisions + 24 * 24
        self.assertEqual(calls["count"], expected_next)
        self.assertEqual(next_session["decisions"], 24)
        self.assertEqual(next_session["diagnostic_shadow"][
            "rejection_counts"]["reject"], expected_next)
        self.assertEqual(next_session["diagnostic_shadow"][
            "rejection_counts"]["by_reason"], {
                "probe rejection": expected_next})

    def test_diagnostic_context_and_incremental_bounds_fail_closed(self):
        with self.assertRaisesRegex(
                ValueError,
                "diagnostic_session_max_events must be a positive integer"):
            ShadowConfig(
                self.corpus, self.edge, self.shadow,
                diagnostic_session_max_events=0)
        with self.assertRaisesRegex(
                ValueError, "diagnostic_session_max_events must be <="):
            ShadowConfig(
                self.corpus, self.edge, self.shadow,
                diagnostic_session_max_events=1_000_001)

        self._append_bar(30)
        self._append_bar(31)
        runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, diagnostic=True,
            runtime_config=_runtime_config(),
            runtime_config_path="/mounted/config.yaml",
            max_events=1, diagnostic_session_max_events=200))
        with self.assertRaisesRegex(
                ShadowError, "shadow event batch bound 1 exceeded"):
            runner.run_once()

        context_runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, diagnostic=True,
            runtime_config=_runtime_config(),
            runtime_config_path="/mounted/config.yaml",
            max_events=2, diagnostic_session_max_events=2))
        context_runner.run_once()
        self._append_bar(
            32, observed_at=datetime.now(timezone.utc) + timedelta(seconds=1))
        with patch.object(
                ShadowRunner, "_evaluate",
                return_value=("no_trade", "probe", {}, None)), \
                self.assertRaisesRegex(
                    ShadowError,
                    "shadow replay validation event bound 2 exceeded"):
            context_runner.run_once()

    def test_account_events_are_globally_causal_across_sessions_and_restarts(self):
        runner = ShadowRunner(self._config())
        activated = runner.run_once()["diagnostic_shadow"]
        cohort_identity = activated["cohort_identity"]
        activation = runner.store.diagnostic_activation(cohort_identity)
        first_day = datetime.fromisoformat(
            activation["warmup_session"]).date() + timedelta(days=1)
        while first_day.weekday() >= 5:
            first_day += timedelta(days=1)
        second_day = first_day + timedelta(days=1)
        while second_day.weekday() >= 5:
            second_day += timedelta(days=1)
        market = ZoneInfo("America/New_York")
        first_stamp = datetime.combine(
            first_day, datetime.min.time(), tzinfo=market).replace(
                hour=9, minute=30).astimezone(timezone.utc)
        second_stamp = datetime.combine(
            second_day, datetime.min.time(), tzinfo=market).replace(
                hour=9, minute=30).astimezone(timezone.utc)
        newer_stamp = second_stamp + timedelta(seconds=10)
        delayed_stamp = first_stamp + timedelta(seconds=10)
        delayed_available = second_stamp + timedelta(hours=1)
        newer_key = _event_key("quote", "SPY", newer_stamp.isoformat())
        delayed_key = _event_key("quote", "SPY", delayed_stamp.isoformat())
        with self.corpus.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.FIELDS)
            for key, stamp, observed, bid, ask in (
                    (newer_key, newer_stamp, newer_stamp, "100", "100.1"),
                    (delayed_key, delayed_stamp, delayed_available,
                     "101", "101.1")):
                writer.writerow({
                    "event_key": key, "event_type": "quote",
                    "symbol": "SPY", "timestamp": stamp.isoformat(),
                    "as_of": stamp.isoformat(),
                    "observed_at": observed.isoformat(),
                    "provider": "alpaca", "feed": "iex", "open": "",
                    "high": "", "low": "", "close": "", "volume": "",
                    "bid": bid, "ask": ask,
                })
        runner.run_once()
        summary = runner.store.diagnostic_account_summary(
            cohort_identity=cohort_identity,
            candidate_ids=activated["candidate_identities"])
        self.assertTrue(all(
            item["last_event_at"] == delayed_available.isoformat()
            for item in summary["by_candidate"]))

        backward_stamp = first_stamp + timedelta(minutes=1)
        backward_available = second_stamp - timedelta(minutes=1)
        backward_key = _event_key(
            "bar_1m", "SPY", backward_stamp.isoformat())
        with self.corpus.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=self.FIELDS).writerow({
                "event_key": backward_key, "event_type": "bar_1m",
                "symbol": "SPY", "timestamp": backward_stamp.isoformat(),
                "as_of": (backward_stamp + timedelta(minutes=1)).isoformat(),
                "observed_at": backward_available.isoformat(),
                "provider": "alpaca", "feed": "iex", "open": "100",
                "high": "101", "low": "99", "close": "100",
                "volume": "1000", "bid": "", "ask": "",
            })
        cash_before = summary["cash"]
        fills_before = summary["modeled_fills"]
        with patch.object(
                ShadowRunner, "_evaluate",
                side_effect=AssertionError("backward event evaluated")):
            runner.run_once()
        after = runner.store.diagnostic_account_summary(
            cohort_identity=cohort_identity,
            candidate_ids=activated["candidate_identities"])
        self.assertEqual(after["cash"], cash_before)
        self.assertEqual(after["modeled_fills"], fills_before)
        self.assertTrue(all(
            item["last_event_at"] == delayed_available.isoformat()
            for item in after["by_candidate"]))
        candidate_id = activated["candidate_identities"][0]
        snapshot = runner.store.diagnostic_account_snapshot(
            cohort_identity=cohort_identity, candidate_id=candidate_id)
        self.assertEqual(snapshot["account"]["last_event_key"], delayed_key)
        progress = runner.store.diagnostic_progress(
            cohort_identity, [candidate_id], activation)[candidate_id]
        self.assertEqual(progress["last_event_key"], backward_key)

    def test_marker_alias_is_excluded_from_all_gate_surfaces(self):
        store = ShadowStore(self.shadow)
        candidate_id = "ordinary-looking-diagnostic"
        store.upsert_candidate({
            "candidate_id": candidate_id, "variant_id": "v",
            "strategy_id": "rule", "vehicle": "equity",
            "status": "diagnostic",
            "config": {"diagnostic_shadow": {"diagnostic_only": True}},
        })
        store.replay_diff(
            candidate_id=candidate_id, session_date="2026-09-08",
            source_digest="source", shadow_digest="shadow",
            replay_digest="replay", status="match",
            details={"diagnostic_only": True})
        store.record_replay_evidence(
            candidate_id=candidate_id, session_date="2026-09-08",
            replay_digest="replay", vehicle="equity", starting_cash=100,
            ending_cash=101, realized_pnl=1,
            trades=[{"symbol": "SPY", "net_pnl": 1}],
            replay_status="match")
        self.assertEqual(store.gate_rows(candidate_id), [])
        self.assertNotIn((candidate_id, "2026-09-08"), store.gate_sessions())

    def test_edge_candidate_ingestion_rejects_diagnostic_marker(self):
        EdgeLedger(self.edge).register_candidate(
            "ibr.range.30", strategy_id="ibr", vehicle="equity",
            hypothesis="forbidden diagnostic ingestion",
            config={"strategy": {"id": "ibr"},
                    "diagnostic_shadow": {"diagnostic_only": True}})
        with self.assertRaisesRegex(ShadowError, "cannot be ingested"):
            _read_candidates(self.edge, max_candidates=10)

    def test_old_code_latest_manifest_can_roll_forward_but_corruption_fails(self):
        store = ShadowStore(self.shadow)
        with patch("research.live_shadow._replay_code_hash",
                   return_value="a" * 64):
            old_digest = store.save_manifest({"event_watermark": {"count": 0}})
        with patch("research.live_shadow._replay_code_hash",
                   return_value="b" * 64):
            rollover = store.latest_manifest_for_rollover()
            self.assertEqual(rollover["replay_code_hash"], "a" * 64)
            with self.assertRaisesRegex(ShadowError, "code identity"):
                store.manifest(old_digest)
            new_digest = ShadowRunner(self._config()).run_once()["manifest_digest"]
            self.assertNotEqual(old_digest, new_digest)
            self.assertEqual(store.manifest(new_digest)["replay_code_hash"],
                             "b" * 64)

            with closing(sqlite3.connect(self.shadow)) as db, db:
                payload = json.loads(db.execute(
                    "SELECT value FROM meta WHERE key='shadow-manifest.v1:latest'"
                ).fetchone()[0])
                payload["event_watermark"]["count"] = 1
                db.execute("UPDATE meta SET value=? WHERE key=?",
                           (json.dumps(payload), "shadow-manifest.v1:latest"))
            with self.assertRaisesRegex(ShadowError, "digest mismatch"):
                store.latest_manifest_for_rollover()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
