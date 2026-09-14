"""Metadata-only forward partition and acceptance report census tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.research_census import CensusError, census, load_current_epoch_context
from agent.config import load_config
from agent.contracts.rule import RULE_FAMILIES
from research.diagnostic_shadow import build_diagnostic_cohort
from research.live_shadow import ShadowStore, _replay_code_hash


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _cohort_contract() -> dict:
    arms = []
    for family in RULE_FAMILIES:
        for role in ("baseline", "variant"):
            arms.append({
                "candidate_id": f"shadow:diagnostic:{family}:{role}",
                "family": family, "role": role,
                "variant_id": f"rule.{family}.{role}",
                "code_identity": "code-1", "cohort_identity": "cohort-1",
            })
    candidates = [arm["candidate_id"] for arm in arms]
    return {
        "arm_count": 24, "family_count": 12,
        "baseline_count": 12, "variant_count": 12,
        "candidate_ids": candidates, "candidate_identities": candidates,
        "code_identity": "code-1", "cohort_identity": "cohort-1",
        "arms": arms,
    }


def _context() -> dict:
    return {
        "schema": "research-epoch-context.v1", "verified": True,
        "updated_ts": NOW.timestamp(),
        "identities": {"deployment": "deploy-1", "code": "code-1",
                        "cohort": "cohort-1", "activation": "activation-1"},
        "expected_symbols": ["AAPL", "MSFT"],
        "cohort_contract": _cohort_contract(),
        "config_identities": {"runtime": "runtime-1", "policy": "policy-1"},
        "include_ibr": False,
    }


def _calendar(day: str) -> dict:
    return {
        "status": "open",
        "source": "alpaca_calendar",
        "open": f"{day}T13:30:00+00:00",
        "close": f"{day}T20:00:00+00:00",
    }


def _partition(recorded: Path, day: str, *, historical: bool = False) -> Path:
    sessions = recorded / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    name = f"market-{day}.csv"
    path = sessions / name
    path.write_text("event_key,timestamp\nrow,1\n", encoding="utf-8")
    (sessions / f"{name}.calendar.json").write_text(json.dumps({
        "schema": "recorder-partition-calendar.v1",
        "partition": name,
        **_calendar(day),
    }), encoding="utf-8")
    if historical:
        (sessions / f"{name}.source.json").write_text(json.dumps({
            "schema": "recorder-partition-source.v1",
            "partition": name,
            "source_mode": "historical_backfill",
        }), encoding="utf-8")
    return path


def _report(root: Path, day: str, *, accepted: bool) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    calendar = _calendar(day)
    close_ts = datetime.fromisoformat(calendar["close"]).timestamp()
    path = root / f"session-{day}.report.json"
    path.write_text(json.dumps({
        "schema": "session-acceptance-report.v1",
        "session": {"date": day, "open": calendar["open"],
                    "close": calendar["close"], "source": "alpaca_calendar"},
        "accepted": accepted,
        "status": "accepted" if accepted else "rejected",
        "operational_only": True,
        "authorizing": False,
        "promotion_eligible": False,
        "reasons": [] if accepted else ["coverage_end_missing"],
        "reason_counts": {} if accepted else {"coverage_end_missing": 1},
        "sample_counts": ({"total": 390, "healthy": 390, "failed": 0,
                           "valid_timestamps": 390}
                          if accepted else
                          {"total": 389, "healthy": 388, "failed": 1,
                           "valid_timestamps": 389}),
        "expected_symbols": ["AAPL", "MSFT"],
        "identities": {"deployment": "deploy-1", "code": "code-1",
                       "cohort": "cohort-1", "activation": "activation-1"},
        "cohort_contract": _cohort_contract(),
        "coverage": {
            "first_sample_ts": datetime.fromisoformat(calendar["open"]).timestamp(),
            "last_sample_ts": close_ts,
            "open_ts": datetime.fromisoformat(calendar["open"]).timestamp(),
            "close_ts": close_ts,
            "start_tolerance_seconds": 60.0,
            "end_tolerance_seconds": 60.0,
            "max_sample_gap_seconds": 65.0,
            "max_observed_gap_seconds": 60.0,
            "closed_at_report": True,
        },
        "post_activation_progress": {
            "arms": 24, "snapshots": 390,
            "all_arms_progressed": True,
            "minimum_processed_events": 390,
            "minimum_session_delta": 389,
            "activation_watermark": {
                "last_inserted_at": (
                    datetime.fromisoformat(calendar["open"]).timestamp() - 3600),
                "last_event_key": "activation-event",
                "count": 0, "decision_event_count": 0,
            },
        },
        "freshness": {
            "strict_threshold_cap_seconds": 30.0,
            "quote_event_age_seconds": {"count": 780, "max": 1.0},
            "bar_event_age_seconds": {"count": 780, "max": 61.0},
            "bar_publication_deadline_lag_seconds": {
                "count": 780, "max": 30.0},
            "shadow_source_lag_seconds": {"count": 390, "max": 1.0},
        },
        "warmup_sessions": [(date.fromisoformat(day) - timedelta(days=1)).isoformat()],
        "finalized_ts": close_ts + 1,
    }), encoding="utf-8")
    return path


def _production_fixture(root: Path, *, include_ibr: bool) -> dict:
    deployment = "d" * 40
    code = _replay_code_hash()
    runtime_config = load_config("config.yaml")
    runtime_symbols = sorted(runtime_config["universe"]["symbols"])
    recorded = root / "recorded"
    recorded.mkdir(parents=True)
    (recorded / ".recorder-index.json").write_text(json.dumps({
        "schema": "recorder-index.v1",
        "configured_symbols": runtime_symbols,
    }), encoding="utf-8")
    _partition(recorded, "2026-09-08")
    acceptance = root / "acceptance"
    shadow_db = root / "shadow.sqlite3"
    store = ShadowStore(shadow_db)
    cohort = build_diagnostic_cohort(
        runtime_config, code_identity=code, include_ibr=include_ibr)
    activation = store.save_diagnostic_activation(
        cohort=cohort,
        activation_event_watermark={"last_inserted_at": 1.0},
        source_offsets={}, forward_event_floor=1.0)
    diagnostic = {
        "code_identity": code,
        "cohort_identity": cohort["cohort_identity"],
        "activation_identity": activation["activation_identity"],
        "candidate_identities": cohort["candidate_identities"],
        "cohort_contract": cohort["cohort_contract"],
        "arms": [{key: arm[key] for key in (
            "candidate_id", "family", "role", "variant_id",
            "code_identity", "cohort_identity")}
                 for arm in cohort["arms"]],
    }
    current = datetime.now(timezone.utc)
    health = root / "health.json"
    health.write_text(json.dumps({
        "schema": "shadow-health.v1", "status": "running",
        "updated_ts": current.timestamp(),
        "provenance": {"identity": deployment},
        "diagnostic_shadow": diagnostic,
    }), encoding="utf-8")
    return {
        "deployment": deployment, "code": code,
        "runtime_config": runtime_config, "runtime_symbols": runtime_symbols,
        "recorded": recorded, "acceptance": acceptance,
        "shadow_db": shadow_db, "health": health, "current": current,
        "cohort": cohort, "activation": activation,
    }


class ResearchCensusTests(unittest.TestCase):
    def test_production_epoch_context_rebuilds_supported_cohort_catalogs(self):
        # Keep this boundary test independent of a developer's ambient feed or
        # paper-mode environment while exercising the real mounted-config and
        # immutable-activation validators.
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"ALPACA_PAPER": "true"}, clear=True):
            root = Path(directory)
            for include_ibr in (False, True):
                fixture = _production_fixture(root / str(int(include_ibr)),
                                              include_ibr=include_ibr)
                current = fixture["current"]
                context = load_current_epoch_context(
                    shadow_health=fixture["health"],
                    shadow_db=fixture["shadow_db"],
                    recorded_root=fixture["recorded"],
                    runtime_config_path=Path("config.yaml"),
                    now=current,
                    expected_deployment=fixture["deployment"],
                    include_ibr=include_ibr)
                self.assertTrue(context["verified"])
                self.assertEqual(
                    context["cohort_contract"]["arm_count"],
                    31 if include_ibr else 24)
                self.assertEqual(context["expected_symbols"],
                                 fixture["runtime_symbols"])

                report = _report(fixture["acceptance"], "2026-09-08",
                                 accepted=True)
                payload = json.loads(report.read_text(encoding="utf-8"))
                payload["expected_symbols"] = fixture["runtime_symbols"]
                payload["identities"] = context["identities"]
                payload["cohort_contract"] = context["cohort_contract"]
                payload["post_activation_progress"]["arms"] = (
                    context["cohort_contract"]["arm_count"])
                observations = 390 * len(fixture["runtime_symbols"])
                for key in ("quote_event_age_seconds",
                            "bar_publication_deadline_lag_seconds",
                            "bar_event_age_seconds"):
                    payload["freshness"][key]["count"] = observations
                report.write_text(json.dumps(payload), encoding="utf-8")
                census_result = census(
                    partition_root=fixture["recorded"] / "sessions",
                    recorded_root=fixture["recorded"], trusted_recorder=True,
                    acceptance_root=fixture["acceptance"],
                    now=current,
                    backtest_minimum_sessions=1,
                    current_context=context)
                self.assertEqual(
                    census_result["accepted_forward_partition_count"], 1)

    def test_loader_rejects_untrusted_epoch_inputs(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"ALPACA_PAPER": "true"}, clear=True):
            root = Path(directory)
            fixture = _production_fixture(root, include_ibr=True)
            base_health = json.loads(fixture["health"].read_text())
            changed_config = root / "config-changed.yaml"
            changed_config.write_text(
                Path("config.yaml").read_text(encoding="utf-8").replace(
                    '"SPY"', '"ABC"', 1), encoding="utf-8")
            cases = {
                "missing_deployment": {
                    "expected_deployment": None},
                "unknown_deployment": {
                    "expected_deployment": "unknown"},
                "stale_health": {
                    "health": {"updated_ts": fixture["current"].timestamp() - 181}},
                "future_health": {
                    "health": {"updated_ts": fixture["current"].timestamp() + 10}},
                "wrong_code": {
                    "diagnostic": {"code_identity": "old-code"}},
                "wrong_cohort": {
                    "diagnostic": {"cohort_identity": "old-cohort"}},
                "wrong_activation": {
                    "diagnostic": {"activation_identity": "old-activation"}},
                "wrong_mode": {"include_ibr": False},
                "wrong_config": {"runtime_config_path": changed_config},
                "symbol_drift": {"symbols": ["ABC"]},
            }
            for name, overrides in cases.items():
                with self.subTest(name=name):
                    health = json.loads(json.dumps(base_health))
                    health.update(overrides.get("health", {}))
                    diagnostic = health["diagnostic_shadow"]
                    diagnostic.update(overrides.get("diagnostic", {}))
                    fixture["health"].write_text(
                        json.dumps(health), encoding="utf-8")
                    index = fixture["recorded"] / ".recorder-index.json"
                    original_index = index.read_text(encoding="utf-8")
                    if "symbols" in overrides:
                        index.write_text(json.dumps({
                            "schema": "recorder-index.v1",
                            "configured_symbols": overrides["symbols"],
                        }), encoding="utf-8")
                    try:
                        with self.assertRaises(CensusError):
                            load_current_epoch_context(
                                shadow_health=fixture["health"],
                                shadow_db=fixture["shadow_db"],
                                recorded_root=fixture["recorded"],
                                runtime_config_path=overrides.get(
                                    "runtime_config_path", Path("config.yaml")),
                                now=fixture["current"],
                                expected_deployment=overrides.get(
                                    "expected_deployment", fixture["deployment"]),
                                include_ibr=overrides.get("include_ibr", True))
                    finally:
                        fixture["health"].write_text(
                            json.dumps(base_health), encoding="utf-8")
                        index.write_text(original_index, encoding="utf-8")

    def test_absent_partition_root_is_zero_forward_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = census(
                partition_root=root / "recorded" / "sessions",
                recorded_root=root / "recorded", trusted_recorder=True,
                acceptance_root=root / "acceptance", now=NOW,
                backtest_minimum_sessions=30)

        self.assertEqual(result["complete_forward_partition_count"], 0)
        self.assertEqual(result["accepted_forward_partition_count"], 0)
        self.assertTrue(result["structurally_underpowered"])
        self.assertEqual(result["readiness"]["state"],
                         "waiting_for_forward_sessions")

    def test_historical_partition_never_counts_as_forward_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorded = root / "recorded"
            acceptance = root / "acceptance"
            _partition(recorded, "2026-09-08", historical=True)
            _report(acceptance, "2026-09-08", accepted=True)

            result = census(
                partition_root=recorded / "sessions", recorded_root=recorded,
                trusted_recorder=True, acceptance_root=acceptance, now=NOW,
                backtest_minimum_sessions=1, current_context=_context())

        self.assertEqual(result["complete_forward_partition_count"], 0)
        self.assertEqual(result["accepted_forward_partition_count"], 0)
        self.assertEqual(result["eligible_historical_partition_count"], 1)
        self.assertTrue(result["structurally_underpowered"])

    def test_missing_and_rejected_reports_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorded = root / "recorded"
            acceptance = root / "acceptance"
            _partition(recorded, "2026-09-07")
            _partition(recorded, "2026-09-08")
            _report(acceptance, "2026-09-08", accepted=False)

            result = census(
                partition_root=recorded / "sessions", recorded_root=recorded,
                trusted_recorder=True, acceptance_root=acceptance, now=NOW,
                backtest_minimum_sessions=1, current_context=_context())

        self.assertEqual(result["complete_forward_partition_count"], 2)
        self.assertEqual(result["accepted_forward_partition_count"], 0)
        self.assertEqual(result["missing_acceptance_report_count"], 1)
        self.assertEqual(result["rejected_acceptance_report_count"], 1)
        self.assertEqual(result["readiness"]["state"],
                         "waiting_for_forward_sessions")

    def test_only_exact_matching_report_is_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorded = root / "recorded"
            acceptance = root / "acceptance"
            _partition(recorded, "2026-09-08")
            arbitrary = _report(acceptance, "2026-09-08", accepted=True)
            arbitrary.rename(acceptance / "accepted.json")
            missing = census(
                partition_root=recorded / "sessions", recorded_root=recorded,
                trusted_recorder=True, acceptance_root=acceptance, now=NOW,
                backtest_minimum_sessions=1, current_context=_context())
            _report(acceptance, "2026-09-08", accepted=True)
            accepted = census(
                partition_root=recorded / "sessions", recorded_root=recorded,
                trusted_recorder=True, acceptance_root=acceptance, now=NOW,
                backtest_minimum_sessions=1, current_context=_context())

        self.assertEqual(missing["accepted_forward_partition_count"], 0)
        self.assertEqual(missing["missing_acceptance_report_count"], 1)
        self.assertEqual(accepted["accepted_forward_partition_count"], 1)
        self.assertFalse(accepted["structurally_underpowered"])

    def test_incomplete_accepted_report_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorded = root / "recorded"
            acceptance = root / "acceptance"
            _partition(recorded, "2026-09-08")
            report = _report(acceptance, "2026-09-08", accepted=True)
            payload = json.loads(report.read_text())
            payload.pop("coverage")
            report.write_text(json.dumps(payload), encoding="utf-8")

            result = census(
                partition_root=recorded / "sessions", recorded_root=recorded,
                trusted_recorder=True, acceptance_root=acceptance, now=NOW,
                backtest_minimum_sessions=1, current_context=_context())

        self.assertEqual(result["accepted_forward_partition_count"], 0)
        self.assertEqual(result["invalid_acceptance_report_count"], 1)

    def test_accepted_label_cannot_mask_missing_progress_or_stale_market_data(self):
        mutations = (
            lambda payload: payload.pop("post_activation_progress"),
            lambda payload: payload["freshness"][
                "bar_publication_deadline_lag_seconds"].update(max=31.0),
            lambda payload: payload.update(warmup_sessions=[payload["session"]["date"]]),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate.__code__.co_firstlineno), \
                 tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                recorded = root / "recorded"
                acceptance = root / "acceptance"
                _partition(recorded, "2026-09-08")
                report = _report(acceptance, "2026-09-08", accepted=True)
                payload = json.loads(report.read_text())
                mutate(payload)
                report.write_text(json.dumps(payload), encoding="utf-8")

                result = census(
                    partition_root=recorded / "sessions",
                    recorded_root=recorded, trusted_recorder=True,
                    acceptance_root=acceptance, now=NOW,
                    backtest_minimum_sessions=1, current_context=_context())

                self.assertEqual(result["accepted_forward_partition_count"], 0)
                self.assertEqual(result["invalid_acceptance_report_count"], 1)

    def test_acceptance_is_bound_to_current_epoch_symbols_and_layout(self):
        mutations = {
            "old_identity": lambda payload: payload["identities"].update(
                deployment="old-deploy", code="old-code",
                cohort="old-cohort", activation="old-activation"),
            "missing_identity": lambda payload: payload["identities"].pop("code"),
            "unknown_identity": lambda payload: payload["identities"].update(
                code="unknown"),
            "unknown_symbols": lambda payload: payload.update(
                expected_symbols=["AAPL", "NVDA"]),
            "changed_layout": lambda payload: payload["cohort_contract"].update(
                candidate_ids=payload["cohort_contract"]["candidate_ids"][:-1]),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                recorded = root / "recorded"
                acceptance = root / "acceptance"
                _partition(recorded, "2026-09-08")
                report = _report(acceptance, "2026-09-08", accepted=True)
                payload = json.loads(report.read_text())
                mutate(payload)
                report.write_text(json.dumps(payload), encoding="utf-8")
                result = census(
                    partition_root=recorded / "sessions",
                    recorded_root=recorded, trusted_recorder=True,
                    acceptance_root=acceptance, now=NOW,
                    backtest_minimum_sessions=1, current_context=_context())
                self.assertEqual(result["accepted_forward_partition_count"], 0)
                self.assertEqual(result["invalid_acceptance_report_count"], 1)

    def test_missing_or_stale_current_epoch_context_fails_closed(self):
        for context in (None, {**_context(), "verified": False},
                        {**_context(), "updated_ts": NOW.timestamp() - 181}):
            with self.subTest(context=context), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                recorded = root / "recorded"
                acceptance = root / "acceptance"
                _partition(recorded, "2026-09-08")
                _report(acceptance, "2026-09-08", accepted=True)
                result = census(
                    partition_root=recorded / "sessions",
                    recorded_root=recorded, trusted_recorder=True,
                    acceptance_root=acceptance, now=NOW,
                    backtest_minimum_sessions=1, current_context=context)
                self.assertEqual(result["accepted_forward_partition_count"], 0)
                self.assertEqual(result["invalid_acceptance_report_count"], 1)

    def test_omitted_acceptance_root_preserves_partition_upper_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            recorded = Path(directory) / "recorded"
            _partition(recorded, "2026-09-08")
            result = census(
                partition_root=recorded / "sessions", recorded_root=recorded,
                trusted_recorder=True, now=NOW, backtest_minimum_sessions=1)

        self.assertFalse(result["acceptance_required"])
        self.assertEqual(result["epoch_context"], {
            "status": "unknown", "reason": "acceptance root was not supplied"})
        self.assertFalse(result["structurally_underpowered"])
        self.assertEqual(result["accepted_forward_partition_count"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
