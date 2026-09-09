"""Focused coverage for the explicit non-authorizing paper incumbent."""

from __future__ import annotations

from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from agent import state
from agent.alpaca_domain import Account, Order, OrderRequest, Position, Quote
from agent.alpaca_provider import AlpacaError
from agent.config import ConfigError, DEFAULT_CONFIG, validate_config
from agent.engine import Engine
from agent.paper_trial import REPLACEMENT_LOCAL_BOOK_ERROR, merge_outcomes
from research.diagnostic_shadow import _logical_arms


VARIANT = _logical_arms()[0]["variant_id"]
SECOND_VARIANT = _logical_arms()[1]["variant_id"]
START = "2026-08-01"


class PaperProvider:
    paper = True
    data_feed = "iex"
    options_feed = "indicative"
    endpoint = "https://paper-api.alpaca.markets"

    class Session:
        api_key = "paper-key"
        secret_key = "paper-secret"

    session = Session()

    def __init__(self):
        self.orders_by_id: dict[str, Order] = {}
        self.positions_live: list[Position] = []
        self.close_requests: list[OrderRequest] = []
        self.reconcile_calls = 0
        self._next_id = 1
        self.quote = Quote(
            "SPY", datetime(2026, 8, 10, 14, tzinfo=timezone.utc),
            bid=Decimal("98"), ask=Decimal("99"))

    def submit_order(self, request: OrderRequest) -> Order:
        order_id = f"order-{self._next_id}"
        self._next_id += 1
        order = Order(
            order_id, request.symbol, request.qty, request.side, "accepted",
            request.type, request.time_in_force,
            client_order_id=request.client_order_id)
        self.orders_by_id[order_id] = order
        return order

    def set_order(self, order_id: str, *, status: str, filled_qty: float,
                  filled_avg_price: float) -> None:
        prior = self.orders_by_id[order_id]
        from dataclasses import replace
        self.orders_by_id[order_id] = replace(
            prior, status=status, filled_qty=Decimal(str(filled_qty)),
            filled_avg_price=Decimal(str(filled_avg_price)))

    def reconcile(self):
        self.reconcile_calls += 1
        return {"positions": list(self.positions_live),
                "orders": list(self.orders_by_id.values())}

    def positions(self):
        return list(self.positions_live)

    def orders(self, **_):
        return list(self.orders_by_id.values())

    def quotes(self, symbols, **_):
        return {str(symbol).upper(): [self.quote] for symbol in symbols}

    def close_position(self, symbol, qty=None, *, client_order_id=None,
                       order_type="market", time_in_force="day"):
        held = next(item for item in self.positions_live
                    if item.symbol == str(symbol).upper())
        request = OrderRequest(
            held.symbol, qty or abs(held.qty), "sell", type=order_type,
            time_in_force=time_in_force, client_order_id=client_order_id)
        self.close_requests.append(request)
        return self.submit_order(request)

    def account(self):
        return Account("paper-account", "active", Decimal("100000"),
                       Decimal("100000"), Decimal("100000"))


def trial_config(root: Path, *, trial_id: str = "paper-a",
                 variant_id: str = VARIANT, min_sessions: int = 20,
                 min_trades: int = 20, max_sessions: int = 60,
                 risk_pct: float = .5) -> dict:
    return {
        "mode": "paper",
        "broker": {
            "paper": True, "allow_live": False, "provider": "alpaca",
            "data_feed": "iex", "options_feed": "indicative",
        },
        "universe": {"symbols": ["SPY"], "asset_classes": ["us_equity"]},
        "strategy": {"execution_mode": "shares"},
        "risk": {"risk_per_trade_pct": risk_pct},
        "llm": {"enabled": False},
        "research": {
            "enabled": True,
            "require_validated_variant": True,
            "trial": {
                "enabled": True,
                "min_sessions": min_sessions,
                "min_trades": min_trades,
                "min_mean_r": 0.0,
                "min_total_r": 0.0,
            },
            "paper_trial": {
                "enabled": True,
                "trial_id": trial_id,
                "variant_id": variant_id,
                "accepted_session_report_root": str(root),
                "max_review_sessions": max_sessions,
            },
        },
    }


class PaperTrialRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paper-trial-")
        self.root = Path(self.tmp.name)
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.provider = PaperProvider()
        self.original_runtime_base = state.RUNTIME_BASE
        state.RUNTIME_BASE = self.root / "runtime"
        self.code_patch = mock.patch(
            "agent.paper_trial.runtime_code_identity", return_value="c" * 64)
        self.today_patch = mock.patch("agent.paper_trial._today", return_value=START)
        self.code_patch.start()
        self.today_patch.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.today_patch.stop()
        self.code_patch.stop()
        state.RUNTIME_BASE = self.original_runtime_base
        state.configure_runtime("paper")
        self.tmp.cleanup()

    def _bind_account(self, engine: Engine) -> None:
        engine._runtime_state = state.update_state(lambda current: {
            **current,
            "account_fingerprint": state.account_fingerprint(
                "paper", "paper-key\0paper-account"),
        })

    def _activate(self, engine: Engine) -> None:
        self._bind_account(engine)
        engine.reconcile()
        self.assertTrue(engine._refresh_edge())

    def _engine(self, *, activate: bool = True, **kwargs) -> Engine:
        engine = Engine(trial_config(self.reports, **kwargs), light=True,
                        provider=self.provider)
        self.addCleanup(engine.close)
        if activate:
            self._activate(engine)
        return engine

    def _report(self, engine: Engine, day: str, *, accepted: bool = True,
                source: str = "alpaca_calendar", source_mode: str | None = None,
                deployment: str = "deploy-a", activation: str = "activation-a"):
        descriptor = engine._paper_trial_runtime.descriptor
        opened = datetime.fromisoformat(f"{day}T13:30:00+00:00")
        closed = datetime.fromisoformat(f"{day}T20:00:00+00:00")
        candidates = descriptor["diagnostic_candidate_ids"]
        payload = {
            "schema": "session-acceptance-report.v1",
            "session": {
                "date": day,
                "open": opened.isoformat(),
                "close": closed.isoformat(),
                "source": source,
            },
            "accepted": accepted,
            "status": "accepted" if accepted else "rejected",
            "operational_only": True,
            "authorizing": False,
            "promotion_eligible": False,
            "expected_symbols": ["SPY"],
            "reasons": [] if accepted else ["fixture_rejected"],
            "reason_counts": {} if accepted else {"fixture_rejected": 1},
            "sample_counts": {
                "total": 391,
                "healthy": 391 if accepted else 390,
                "failed": 0 if accepted else 1,
                "valid_timestamps": 391,
            },
            "freshness": {
                "strict_threshold_cap_seconds": 30.0,
                "quote_event_age_seconds": {
                    "count": 391, "p50": 1.0, "p95": 1.0, "max": 1.0,
                },
                "bar_event_age_seconds": {
                    "count": 390, "p50": 30.0, "p95": 60.0, "max": 60.0,
                },
                "bar_publication_deadline_lag_seconds": {
                    "count": 391, "p50": 0.0, "p95": 0.0, "max": 0.0,
                },
                "shadow_source_lag_seconds": {
                    "count": 391, "p50": 1.0, "p95": 1.0, "max": 1.0,
                },
            },
            "symbol_failure_counts": {},
            "coverage": {
                "first_sample_ts": opened.timestamp(),
                "last_sample_ts": closed.timestamp(),
                "open_ts": opened.timestamp(),
                "close_ts": closed.timestamp(),
                "start_tolerance_seconds": 60.0,
                "end_tolerance_seconds": 60.0,
                "max_sample_gap_seconds": 65.0,
                "closed_at_report": True,
                "max_observed_gap_seconds": 60.0,
            },
            "identities": {
                "deployment": deployment,
                "code": descriptor["code_identity"],
                "cohort": descriptor["cohort_identity"],
                "activation": activation,
            },
            "warmup_sessions": [START],
            "arm_progress": {
                "arms": len(candidates),
                "cursors": {
                    candidate: {
                        "last_inserted_at": closed.timestamp() - 1,
                        "last_event_key": "market-event-final",
                        "processed_events": 100,
                    }
                    for candidate in candidates
                },
            },
            "post_activation_progress": {
                "arms": len(candidates),
                "snapshots": 391,
                "all_arms_progressed": True,
                "minimum_processed_events": 100,
                "minimum_session_delta": 99,
                "activation_watermark": {
                    "last_inserted_at": opened.timestamp() - 60,
                    "last_event_key": "activation-event",
                    "count": 1,
                    "decision_event_count": 0,
                },
            },
            "finalized_ts": closed.timestamp() + 1,
        }
        if source_mode is not None:
            payload["source_mode"] = source_mode
        (self.reports / f"session-{day}.report.json").write_text(
            json.dumps(payload), encoding="utf-8")
        return self.reports / f"session-{day}.report.json"

    def _accepted_reports(self, engine: Engine, count: int) -> None:
        start = datetime.fromisoformat(START).date()
        for offset in range(1, count + 1):
            self._report(engine, (start + timedelta(days=offset)).isoformat())

    def _append_outcomes(self, engine: Engine, values: list[float]) -> None:
        identity = engine._paper_trial_runtime.entry_identity()
        start = datetime.fromisoformat(START).replace(tzinfo=timezone.utc)
        offset = len(engine._runtime_state["paper_trial"].get("outcomes", []))
        pending = []
        for index, value in enumerate(values, offset + 1):
            opened = (start + timedelta(days=index, hours=14)).timestamp()
            pending.append({
                **identity,
                "opportunity_id": f"setup-{index}",
                "order_id": f"entry-{index}",
                "symbol": "SPY",
                "opened_at": opened,
                "session_date": (start + timedelta(days=index)).date().isoformat(),
                "net_pnl": value * 100.0,
                "gross_pnl": value * 100.0,
                "fees": 0.0,
                "slippage": 0.0,
                "risk_usd": 100.0,
                "planned_risk_usd": 100.0,
                "r_multiple": value,
            })

        def update(current):
            current["paper_trial"] = merge_outcomes(
                current["paper_trial"], pending)
            return current

        engine._runtime_state = state.update_state(update)
        engine._paper_trial_runtime.current = engine._runtime_state["paper_trial"]

    def test_disabled_default_and_normal_engine_path_are_unchanged(self):
        self.assertFalse(DEFAULT_CONFIG["research"]["paper_trial"]["enabled"])
        config = validate_config({})
        self.assertFalse(config["research"]["paper_trial"]["enabled"])
        engine = Engine({
            "mode": "paper",
            "broker": {"paper": True, "allow_live": False},
            "research": {"enabled": False},
        }, light=True, provider=self.provider)
        self.addCleanup(engine.close)
        self.assertIsNone(engine._paper_trial_runtime)
        self.assertNotIn("paper_trial", engine._paper_selection_status())

    def test_enabled_trial_rejects_live_raw_config_and_runtime_llm(self):
        live = trial_config(self.reports)
        live.update(mode="live")
        live["broker"].update(paper=False, allow_live=True)
        live["strategy"].update(
            selection_mode="specific", variant_id=VARIANT)
        with mock.patch.dict(os.environ, {
                "ALPACA_LIVE_ENABLE": "true", "ALPACA_PAPER": "false"},
                clear=False):
            with self.assertRaisesRegex(ConfigError, "paper_trial requires mode=paper"):
                validate_config(live)

            class LiveProvider(PaperProvider):
                paper = False
                endpoint = "https://api.alpaca.markets"

            with self.assertRaisesRegex(AlpacaError, "paper_trial requires mode=paper"):
                Engine(live, light=True, provider=LiveProvider())

        llm = trial_config(self.reports)
        llm["llm"] = {"enabled": True, "provider": "openai", "model": "x"}
        with self.assertRaisesRegex(ConfigError, "llm.enabled=false"):
            validate_config(llm)
        with self.assertRaisesRegex(AlpacaError, "injected brains"):
            Engine(trial_config(self.reports), light=True,
                   provider=self.provider, brain=object())

        delayed = trial_config(self.reports)
        delayed["broker"]["data_feed"] = "delayed_sip"
        with self.assertRaisesRegex(ConfigError, "real-time equity feed"):
            validate_config(delayed)

        options = trial_config(self.reports)
        options["strategy"]["execution_mode"] = "options"
        options["universe"]["asset_classes"] = ["us_option"]
        options["broker"]["options_feed"] = "opra"
        with self.assertRaisesRegex(ConfigError, "shares/us_equity"):
            validate_config(options)

        weakened = trial_config(self.reports)
        weakened["research"]["require_validated_variant"] = False
        with self.assertRaisesRegex(ConfigError, "edge gate"):
            validate_config(weakened)

    def test_exact_catalog_variant_is_the_only_runtime_strategy(self):
        bad = trial_config(self.reports, variant_id="rule.not-in-catalog")
        with self.assertRaisesRegex(ConfigError, "diagnostic catalog arm"):
            validate_config(bad)
        engine = self._engine()
        self.assertEqual(len(engine._edge_configs), 1)
        self.assertIsNone(engine._edge_configs[0][0])
        strategy = engine._edge_configs[0][1]["strategy"]
        self.assertEqual(strategy["id"], "rule")
        self.assertEqual(strategy["variant_id"], VARIANT)
        self.assertEqual(strategy["rule_spec"], _logical_arms()[0]["rule_spec"])
        status = engine._paper_selection_status()["paper_trial"]
        self.assertFalse(status["authorizing"])
        self.assertFalse(status["proof_authority"])
        self.assertTrue(status["entry_eligible"])
        selection = engine._paper_selection_status()
        self.assertEqual(selection["state"], "paper_trial")
        self.assertEqual(selection["resolved"]["candidate_id"],
                         status["candidate_id"])
        self.assertIsNone(selection["resolved"]["proof"])
        self.assertFalse(selection["resolved"]["authorizing"])

    def test_restart_is_sticky_and_active_config_drift_is_rejected(self):
        first = self._engine()
        original = state.load_state()["paper_trial"]
        first.close()
        second = self._engine()
        restarted = state.load_state()["paper_trial"]
        self.assertEqual(restarted["incumbent_identity"],
                         original["incumbent_identity"])
        self.assertEqual(restarted["started_on"], START)
        second.close()
        with self.assertRaisesRegex(AlpacaError, "identity/config changed"):
            Engine(trial_config(self.reports, risk_pct=.75), light=True,
                   provider=self.provider)
        threshold_drift = trial_config(self.reports, min_sessions=21)
        with self.assertRaisesRegex(AlpacaError, "identity/config changed"):
            Engine(threshold_drift, light=True, provider=self.provider)
        with self.assertRaisesRegex(AlpacaError, "cannot be disabled or bypassed"):
            Engine(validate_config({}), light=True, provider=self.provider)

    def test_first_activation_requires_account_bound_broker_and_local_flatness(self):
        engine = self._engine(activate=False, trial_id="first-activation")
        status = engine._paper_selection_status()["paper_trial"]
        self.assertFalse(status["activation_confirmed"])
        self.assertFalse(status["entry_eligible"])
        self.assertIsNone(status["started_on"])
        self.assertIn(
            "initial_activation_requires_account_bound_flat_book",
            status["blockers"])

        self._bind_account(engine)
        state.update_state(lambda current: {
            **current, "active_trades": {"SPY": {"symbol": "SPY"}}})
        engine._last_reconcile_snapshot = {
            "positions": [Position("SPY", Decimal("1"), "long")],
            "orders": [],
        }
        self.assertFalse(engine._refresh_edge())

        pending = Order(
            "pending", "SPY", Decimal("1"), "buy", "accepted",
            "market", "day")
        state.update_state(lambda current: {
            **current,
            "active_trades": {}, "protection": {},
            "orders": {"pending": {"order_id": "pending",
                                      "status": "accepted"}},
        })
        engine._last_reconcile_snapshot = {"positions": [], "orders": [pending]}
        self.assertFalse(engine._refresh_edge())

        state.update_state(lambda current: {
            **current, "orders": {}, "active_trades": {}, "protection": {}})
        engine._last_reconcile_snapshot = {"positions": [], "orders": []}
        self.assertTrue(engine._refresh_edge())
        status = engine._paper_selection_status()["paper_trial"]
        self.assertTrue(status["activation_confirmed"])
        self.assertTrue(status["entry_eligible"])
        self.assertEqual(status["started_on"], START)

    def test_trial_pause_never_overwrites_hard_runtime_safety_state(self):
        engine = self._engine(trial_id="hard-stop")

        def stop(current):
            trial = dict(current["paper_trial"])
            trial["state"] = "review_required"
            current["paper_trial"] = trial
            current["state"] = state.DAY_STOPPED
            current["kill_reason"] = "operator_kill"
            current["risk_day"] = {"limit_hit": True}
            return current

        state.update_state(stop)
        self.assertFalse(engine._refresh_edge())
        runtime = state.load_state()
        self.assertEqual(runtime["state"], state.DAY_STOPPED)
        self.assertEqual(runtime["kill_reason"], "operator_kill")
        self.assertTrue(runtime["risk_day"]["limit_hit"])

    def test_counts_only_full_accepted_post_start_forward_reports(self):
        engine = self._engine()
        self._report(engine, START)
        self._report(engine, "2026-08-02")
        self._report(engine, "2026-08-03", accepted=False)
        self._report(engine, "2026-08-04", source_mode="historical_backfill")
        self._report(engine, "2026-08-05")
        self._report(engine, "2026-08-06", source_mode="replay")
        self.assertTrue(engine._refresh_edge())
        status = engine._paper_selection_status()["paper_trial"]
        self.assertEqual(status["valid_sessions"], 2)
        self.assertEqual(status["report_identities"]["deployment"], "deploy-a")
        self.assertEqual(status["report_identities"]["activation"], "activation-a")
        (self.reports / "session-2026-08-02.report.json").unlink()
        self.assertFalse(engine._refresh_edge())
        status = engine._paper_selection_status()["paper_trial"]
        self.assertEqual(status["valid_sessions"], 1)
        self.assertIn(
            "counted_report_missing:session-2026-08-02.report.json",
            status["blockers"])
        selection = engine._paper_selection_status()
        self.assertEqual(selection["state"], "blocked")
        self.assertIsNone(selection["resolved"]["proof"])

    def test_incomplete_or_inconsistent_accepted_reports_never_count(self):
        engine = self._engine(trial_id="strict-reports")
        path = self._report(engine, "2026-08-02")
        valid = json.loads(path.read_text(encoding="utf-8"))

        cases = (
            ("missing_status", lambda item: item.pop("status"),
             "report_contract_invalid"),
            ("reported_reason", lambda item: item["reasons"].append("gap"),
             "report_contract_invalid"),
            ("late_first_sample", lambda item: item["coverage"].update(
                first_sample_ts=item["coverage"]["open_ts"] + 120),
             "report_coverage_invalid"),
            ("preclose_finalization", lambda item: item.update(
                finalized_ts=item["coverage"]["close_ts"] - 1),
             "report_finalization_invalid"),
            ("failed_sample", lambda item: item["sample_counts"].update(
                healthy=390, failed=1), "report_samples_invalid"),
            ("future_warmup", lambda item: item.update(
                warmup_sessions=["2026-08-02"]), "report_warmup_invalid"),
            ("universe_drift", lambda item: item.update(
                expected_symbols=["AAPL"]), "report_universe_mismatch"),
            ("missing_publication_freshness", lambda item: item[
                "freshness"].pop("bar_publication_deadline_lag_seconds"),
             "report_freshness_invalid"),
            ("stale_publication_with_fresh_ingestion", lambda item: (
                item["freshness"][
                    "bar_publication_deadline_lag_seconds"].update(
                        p50=0.0, p95=31.0, max=31.0),
                item["freshness"].update(bar_ingestion_age_seconds={
                    "count": 391, "p50": 0.0, "p95": 0.0, "max": 0.0,
                })), "report_freshness_invalid"),
            ("stale_shadow_source", lambda item: item["freshness"][
                "shadow_source_lag_seconds"].update(
                    p50=1.0, p95=31.0, max=31.0),
             "report_freshness_invalid"),
            ("zero_progress", lambda item: next(iter(
                item["arm_progress"]["cursors"].values())).update(
                    processed_events=0), "report_progress_invalid"),
            ("stale_progress", lambda item: next(iter(
                item["arm_progress"]["cursors"].values())).update(
                    last_inserted_at=item["coverage"]["open_ts"] - 1),
             "report_progress_invalid"),
            ("missing_session_progress", lambda item: item.pop(
                "post_activation_progress"), "report_progress_invalid"),
            ("no_session_delta", lambda item: item[
                "post_activation_progress"].update(minimum_session_delta=0),
             "report_progress_invalid"),
            ("future_activation_watermark", lambda item: item[
                "post_activation_progress"]["activation_watermark"].update(
                    last_inserted_at=item["coverage"]["open_ts"]),
             "report_progress_invalid"),
        )
        for label, mutate, blocker in cases:
            with self.subTest(label=label):
                payload = deepcopy(valid)
                mutate(payload)
                path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertFalse(engine._refresh_edge())
                status = engine._paper_selection_status()["paper_trial"]
                self.assertEqual(status["valid_sessions"], 0)
                self.assertTrue(any(
                    str(item).startswith(blocker)
                    for item in status["blockers"]))

    def test_uncertain_negative_continues_but_hard_negative_stops_after_floors(self):
        uncertain = self._engine(
            trial_id="uncertain", min_sessions=5, min_trades=5,
            max_sessions=10)
        self._accepted_reports(uncertain, 5)
        self._append_outcomes(uncertain, [-1.0, 1.0, -1.0, 1.0, -.1])
        self.assertTrue(uncertain._refresh_edge())
        uncertain_status = uncertain._paper_selection_status()["paper_trial"]
        self.assertEqual(uncertain_status["verdict"], "inconclusive")
        self.assertEqual(uncertain_status["state"], "running")

        state.RUNTIME_BASE = self.root / "negative-runtime"
        negative_reports = self.root / "negative-reports"
        negative_reports.mkdir()
        negative = Engine(trial_config(
            negative_reports, trial_id="negative", min_sessions=5,
            min_trades=5, max_sessions=10), light=True,
            provider=self.provider)
        self.addCleanup(negative.close)
        self._activate(negative)
        for offset in range(1, 5):
            self._report_to(negative, negative_reports,
                            f"2026-08-{offset + 1:02d}")
        self._append_outcomes(negative, [-1.0] * 4)
        self.assertTrue(negative._refresh_edge())
        self.assertEqual(
            negative._paper_selection_status()["paper_trial"]["verdict"],
            "running")
        self._report_to(negative, negative_reports, "2026-08-06")
        self._append_outcomes(negative, [-1.0])
        self.assertFalse(negative._refresh_edge())
        status = negative._paper_selection_status()["paper_trial"]
        self.assertEqual(status["verdict"], "failed")
        self.assertEqual(status["state"], "failed")

    def _report_to(self, engine: Engine, root: Path, day: str, **kwargs):
        original = self.reports
        try:
            self.reports = root
            self._report(engine, day, **kwargs)
        finally:
            self.reports = original

    def test_max_horizon_inconclusive_requires_review_without_hard_negative(self):
        engine = self._engine(
            trial_id="horizon", min_sessions=5, min_trades=5,
            max_sessions=5)
        self._accepted_reports(engine, 5)
        self.assertFalse(engine._refresh_edge())
        status = engine._paper_selection_status()["paper_trial"]
        self.assertEqual(status["state"], "review_required")
        self.assertEqual(status["verdict"], "running")
        self.assertIn("paper_trial_review_required", status["blockers"])

    def test_daily_risk_stop_overrides_trial_tenure(self):
        engine = self._engine()
        state.update_state(lambda current: {
            **current,
            "risk_day": {"limit_hit": True},
        })
        selection = engine._paper_selection_status()
        self.assertEqual(selection["blocker_code"], "daily_risk_limit")
        self.assertFalse(selection["armed"])

    def test_terminal_replacement_requires_account_bound_flat_snapshot(self):
        first = self._engine(trial_id="first")
        self._report(first, "2026-08-02")
        self._append_outcomes(first, [.5])
        self.assertTrue(first._refresh_edge())

        def terminal(current):
            trial = dict(current["paper_trial"])
            trial["state"] = "failed"
            trial["verdict"] = {**trial["verdict"], "state": "failed"}
            current["paper_trial"] = trial
            current["account_fingerprint"] = state.account_fingerprint(
                "paper", "paper-key\0paper-account")
            return current

        state.update_state(terminal)
        first.close()
        second = self._engine(
            activate=False, trial_id="second", variant_id=SECOND_VARIANT)
        pending = second._paper_selection_status()["paper_trial"]
        self.assertFalse(pending["entry_eligible"])
        self.assertEqual(pending["trial_id"], "first")
        second.reconcile()
        with mock.patch("agent.paper_trial._today", return_value="2026-08-03"):
            self.assertTrue(second._refresh_edge(), second._edge_error)
        replaced = state.load_state()["paper_trial"]
        self.assertEqual(replaced["trial_id"], "second")
        self.assertEqual(replaced["variant_id"], SECOND_VARIANT)
        self.assertTrue(second._refresh_edge(), second._edge_error)
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            rows = db.execute(
                "SELECT payload FROM events "
                "WHERE kind='paper_trial_terminal_snapshot'").fetchall()
        self.assertEqual(len(rows), 1)
        audit = json.loads(rows[0][0])
        self.assertEqual(audit["terminal"]["trial_id"], "first")
        self.assertEqual(audit["terminal"]["state"], "failed")
        self.assertEqual(audit["terminal"]["verdict"]["state"], "failed")
        self.assertEqual(audit["valid_sessions"], 1)
        self.assertEqual(audit["closed_outcomes"], 1)
        self.assertEqual(len(audit["terminal"]["outcomes"]), 1)
        self.assertFalse(audit["authorizing"])
        self.assertFalse(audit["proof_authority"])

    def test_terminal_replacement_is_blocked_when_audit_journal_fails(self):
        first = self._engine(trial_id="audit-old")

        def terminal(current):
            trial = dict(current["paper_trial"])
            trial["state"] = "review_required"
            current["paper_trial"] = trial
            return current

        state.update_state(terminal)
        first.close()
        second = self._engine(
            activate=False, trial_id="audit-new", variant_id=SECOND_VARIANT)
        second.reconcile()
        with mock.patch.object(
                state, "log_event", side_effect=OSError("journal unavailable")):
            self.assertFalse(second._refresh_edge())
        self.assertIn("journal unavailable", second._edge_error)
        self.assertEqual(state.load_state()["paper_trial"]["trial_id"],
                         "audit-old")

    def test_pending_replacement_never_applies_new_policy_to_old_exposure(self):
        first = self._engine(trial_id="old-policy")

        def terminal(current):
            trial = dict(current["paper_trial"])
            trial["state"] = "failed"
            current["paper_trial"] = trial
            return current

        state.update_state(terminal)
        first.close()
        held = Position(
            "SPY", Decimal("1"), "long", avg_entry_price=Decimal("100"),
            current_price=Decimal("99"), market_value=Decimal("99"))
        self.provider.positions_live = [held]
        second = Engine(trial_config(
            self.reports, trial_id="new-policy", variant_id=SECOND_VARIANT,
            risk_pct=.9), light=True, provider=self.provider)
        self.addCleanup(second.close)
        self.assertTrue(second._paper_trial_runtime.pending_replacement)
        self.assertEqual(second._edge_configs, [])
        second._preflight = {}
        second._reconciled = True
        second._last_reconcile_snapshot = {
            "positions": [held], "orders": [],
        }
        with mock.patch.object(second, "_monitor_positions") as monitor:
            result = second.run_once({})
        self.assertEqual(result["action"], "hold")
        self.assertIn("cannot manage old exposure", result["reason"])
        monitor.assert_not_called()
        self.assertEqual(len(self.provider.orders_by_id), 0)

    def test_replacement_refuses_before_settling_old_close_with_new_costs(self):
        old_config = trial_config(self.reports, trial_id="settling-old")
        old_config["costs"] = {
            "spread_bps": 0.0,
            "slippage_bps": 0.0,
            "fee_bps": 0.0,
            "option_fee_per_contract_side": 0.0,
        }
        first = Engine(old_config, light=True, provider=self.provider)
        self.addCleanup(first.close)
        self._activate(first)

        request = OrderRequest(
            "SPY", Decimal("10"), "buy", client_order_id="settling-entry")
        entry = self.provider.submit_order(request)
        plan = first._paper_trial_runtime.annotate({
            "execution_profile": "shares", "direction": "long",
            "entry_price": 100, "stop_price": 99, "target_price": 105,
            "underlying_stop_price": 99, "underlying_target_price": 105,
            "underlying_symbol": "SPY", "contract_multiplier": Decimal("1"),
            "setup_id": "settling-parent", "setup_type": "rule",
            "risk_usd": 10, "planned_risk_usd": 10, "notional": 1000,
        })
        first._record_open_order(request, entry, plan)
        self.provider.set_order(
            entry.id, status="filled", filled_qty=10, filled_avg_price=100)
        position = Position(
            "SPY", Decimal("10"), "long", avg_entry_price=Decimal("100"),
            current_price=Decimal("98"))
        self.provider.positions_live = [position]
        first.reconcile()
        first._monitor_positions(
            datetime(2026, 8, 10, 14, tzinfo=timezone.utc), [position])
        close_request = self.provider.close_requests[-1]
        close_order = next(
            order for order in self.provider.orders_by_id.values()
            if order.client_order_id == close_request.client_order_id)
        self.provider.set_order(
            close_order.id, status="filled", filled_qty=10,
            filled_avg_price=98)
        self.provider.positions_live = []

        def terminal(current):
            trial = dict(current["paper_trial"])
            trial["state"] = "failed"
            trial["verdict"] = {**trial["verdict"], "state": "failed"}
            current["paper_trial"] = trial
            return current

        state.update_state(terminal)
        first.close()
        durable_before = state.load_state()
        self.assertIn("SPY", durable_before["active_trades"])
        self.assertEqual(
            durable_before["orders"][close_order.id]["status"], "accepted")
        self.assertEqual(durable_before["paper_trial"]["outcomes"], [])
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            journal_before = {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("events", "orders", "trades")
            }
        broker_before = deepcopy(self.provider.orders_by_id)
        close_requests_before = list(self.provider.close_requests)
        next_id_before = self.provider._next_id
        self.provider.reconcile_calls = 0

        new_config = trial_config(
            self.reports, trial_id="settling-new", variant_id=SECOND_VARIANT)
        new_config["costs"] = {
            "spread_bps": 0.0,
            "slippage_bps": 0.0,
            "fee_bps": 10.0,
            "option_fee_per_contract_side": 0.0,
        }
        with mock.patch.object(
                Engine, "preflight", return_value={"clock": object()}) as preflight, \
             mock.patch.object(
                 Engine, "_enforce_intraday_cleanup",
                 return_value=True) as cleanup:
            replacement = Engine(
                new_config, light=False, provider=self.provider)
        self.addCleanup(replacement.close)

        self.assertTrue(replacement._paper_trial_runtime.pending_replacement)
        self.assertEqual(replacement._preflight_error,
                         REPLACEMENT_LOCAL_BOOK_ERROR)
        self.assertEqual(replacement._edge_error,
                         REPLACEMENT_LOCAL_BOOK_ERROR)
        self.assertEqual(replacement._edge_configs, [])
        preflight.assert_not_called()
        cleanup.assert_not_called()
        self.assertEqual(self.provider.reconcile_calls, 0)
        with self.assertRaisesRegex(
                AlpacaError, "restart with the frozen incumbent"):
            replacement.reconcile()
        self.assertEqual(self.provider.reconcile_calls, 0)
        self.assertEqual(self.provider.orders_by_id, broker_before)
        self.assertEqual(self.provider.close_requests, close_requests_before)
        self.assertEqual(self.provider._next_id, next_id_before)
        self.assertEqual(state.load_state(), durable_before)
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            journal_after = {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("events", "orders", "trades")
            }
            replacement_audits = db.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE kind='paper_trial_terminal_snapshot'").fetchone()[0]
        self.assertEqual(journal_after, journal_before)
        self.assertEqual(replacement_audits, 0)
        replacement.close()

        # The unchanged frozen incumbent is still allowed to reconcile the
        # already-settled close, and must attribute it under its original
        # zero-fee policy before any later replacement attempt.
        self.provider.reconcile_calls = 0
        with mock.patch.object(
                Engine, "preflight", return_value={"clock": object()}) as preflight, \
             mock.patch.object(
                 Engine, "_enforce_intraday_cleanup",
                 return_value=True) as cleanup:
            resumed = Engine(old_config, light=False, provider=self.provider)
        self.addCleanup(resumed.close)
        preflight.assert_called_once()
        cleanup.assert_called_once()
        self.assertEqual(self.provider.reconcile_calls, 1)
        settled = state.load_state()
        self.assertEqual(settled["paper_trial"]["trial_id"], "settling-old")
        self.assertEqual(settled["active_trades"], {})
        self.assertEqual(len(settled["paper_trial"]["outcomes"]), 1)
        outcome = settled["paper_trial"]["outcomes"][0]
        self.assertEqual(outcome["gross_pnl"], -20.0)
        self.assertEqual(outcome["fees"], 0.0)
        self.assertEqual(outcome["net_pnl"], -20.0)
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            replacement_audits = db.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE kind='paper_trial_terminal_snapshot'").fetchone()[0]
        self.assertEqual(replacement_audits, 0)

    def test_run_once_executes_only_the_activated_exact_incumbent(self):
        now = datetime(2026, 8, 10, 13, 46, tzinfo=timezone.utc)
        opened = datetime(2026, 8, 10, 13, 30, tzinfo=timezone.utc)
        closed = datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc)

        class Market:
            def refresh_calendar(self):
                return None

            def clock(self):
                return type("Clock", (), {
                    "timestamp": now, "is_open": True,
                })()

            def session(self, _now):
                return type("Session", (), {
                    "date": opened.date(), "open": opened, "close": closed,
                })()

            def should_force_flat(self, _now):
                return False

            def can_enter(self, _now):
                return True

        config = trial_config(self.reports, trial_id="full-cycle")
        # This regression exercises the complete signal -> risk -> broker
        # route, so give the catalog arm enough stressed-cost headroom for its
        # authored opening-range stop without bypassing the real RiskEngine.
        config["risk"]["max_stressed_cost_to_risk_ratio"] = 1.0
        engine = Engine(config, light=True, provider=self.provider,
                        market_data=Market())
        self.addCleanup(engine.close)
        self._activate(engine)
        engine._preflight = {}
        engine._startup_cleanup_checked = True
        engine._wall_clock = lambda: now
        bars = []
        for index in range(16):
            timestamp = opened + timedelta(minutes=index)
            close_price = 100.0 if index < 15 else 100.3
            bars.append({
                "timestamp": timestamp.isoformat(),
                "open": 100.0,
                "high": max(100.0, close_price) + .05,
                "low": min(100.0, close_price) - .05,
                "close": close_price,
                "volume": 1000 if index < 15 else 2000,
                "feed": "iex", "provider": "alpaca",
            })
        row = {
            "symbol": "SPY", "bars": bars, "price": 100.3,
            "quote": {
                "timestamp": now.isoformat(), "bid": 100.29, "ask": 100.3,
                "feed": "iex", "provider": "alpaca",
            },
            "spread_bps": 1.0, "stale": False, "quote_stale": False,
            "quote_age_seconds": 0.0,
        }
        from agent.contracts.rule import generate_rule_signal as real_signal
        seen_variants: list[str] = []
        events: list[tuple[str, dict]] = []
        engine._event = lambda kind, payload: events.append((kind, payload))

        def exact_signal(*args, **kwargs):
            seen_variants.append(kwargs["config"]["strategy"]["variant_id"])
            return real_signal(*args, **kwargs)

        with mock.patch.object(engine, "_collect", return_value={"SPY": row}), \
             mock.patch("agent.engine_cycle.generate_rule_signal",
                        side_effect=exact_signal):
            result = engine.run_once()

        self.assertEqual(result["action"], "decide", result)
        self.assertEqual(len(result["orders"]), 1, {"result": result,
                                                    "events": events})
        self.assertEqual(seen_variants, [VARIANT])
        runtime = state.load_state()
        order = next(iter(runtime["orders"].values()))
        risk_plan = order["risk_plan"]
        self.assertEqual(risk_plan["paper_trial_variant_id"], VARIANT)
        self.assertEqual(
            risk_plan["paper_trial_candidate_id"],
            engine._paper_trial_runtime.current["candidate_id"])
        self.assertIsNone(risk_plan["proof_run_id"])
        self.assertFalse(risk_plan["paper_trial_authorizing"])

    def test_close_outcome_is_atomic_idempotent_and_never_enters_edge_ledger(self):
        engine = self._engine(trial_id="close")
        request = OrderRequest(
            "SPY", Decimal("10"), "buy", client_order_id="entry-trial")
        entry = self.provider.submit_order(request)
        plan = engine._paper_trial_runtime.annotate({
            "execution_profile": "shares", "direction": "long",
            "entry_price": 100, "stop_price": 99, "target_price": 105,
            "underlying_stop_price": 99, "underlying_target_price": 105,
            "underlying_symbol": "SPY", "contract_multiplier": Decimal("1"),
            "setup_id": "trial-parent", "setup_type": "rule",
            "risk_usd": 10, "planned_risk_usd": 10, "notional": 1000,
        })
        engine._record_open_order(request, entry, plan)
        self.provider.set_order(entry.id, status="filled", filled_qty=10,
                                filled_avg_price=100)
        position = Position(
            "SPY", Decimal("10"), "long", avg_entry_price=Decimal("100"),
            current_price=Decimal("98"))
        self.provider.positions_live = [position]
        engine.reconcile()
        trade = state.load_state()["active_trades"]["SPY"]
        self.assertEqual(trade["paper_trial_id"], "close")
        self.assertEqual(trade["candidate_id"],
                         trade["paper_trial_candidate_id"])
        self.assertIsNone(trade["proof_run_id"])

        engine._monitor_positions(
            datetime(2026, 8, 10, 14, tzinfo=timezone.utc), [position])
        first_close = self.provider.close_requests[-1]
        first_order = next(
            order for order in self.provider.orders_by_id.values()
            if order.client_order_id == first_close.client_order_id)
        self.provider.set_order(first_order.id, status="canceled", filled_qty=4,
                                filled_avg_price=99)
        self.provider.positions_live = [Position(
            "SPY", Decimal("6"), "long", avg_entry_price=Decimal("100"),
            current_price=Decimal("98"))]
        engine.reconcile()
        self.assertEqual(state.load_state()["paper_trial"]["outcomes"], [])

        engine._monitor_positions(
            datetime(2026, 8, 10, 14, tzinfo=timezone.utc),
            list(self.provider.positions_live))
        second_close = self.provider.close_requests[-1]
        second_order = next(
            order for order in self.provider.orders_by_id.values()
            if order.client_order_id == second_close.client_order_id)
        self.provider.set_order(second_order.id, status="filled", filled_qty=6,
                                filled_avg_price=98)
        self.provider.positions_live = []
        with mock.patch("agent.edge.record_paper_outcome") as authorizing_write:
            engine.reconcile()
            engine.reconcile()
        runtime = state.load_state()
        self.assertEqual(len(runtime["paper_trial"]["outcomes"]), 1)
        self.assertEqual(runtime["edge_outbox"], [])
        self.assertFalse(runtime["paper_trial"]["outcomes"][0]["authorizing"])
        authorizing_write.assert_not_called()

    def test_unmeasurable_trial_close_is_retained_as_inconclusive(self):
        engine = self._engine(trial_id="unmeasurable")
        identity = engine._paper_trial_runtime.entry_identity()
        engine._record_edge_outcome({
            **identity,
            "symbol": "SPY",
            "order_id": "entry-unmeasurable",
            "setup_id": "setup-unmeasurable",
            "opened_at": datetime(
                2026, 8, 10, 14, tzinfo=timezone.utc).timestamp(),
            "qty": "1",
            "entry_price": None,
            "planned_risk_usd": 10,
        }, None, None, None)

        engine._runtime_state = state.update_state(lambda latest: {
            **latest,
            "paper_trial": engine._queued_paper_trial_state(latest),
        })
        outcome = engine._runtime_state["paper_trial"]["outcomes"][0]
        self.assertIsNone(outcome["net_pnl"])
        self.assertIsNone(outcome["r_multiple"])
        self.assertFalse(outcome["authorizing"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
