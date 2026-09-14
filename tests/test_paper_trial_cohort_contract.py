"""Contract coverage for paper-trial diagnostic cohort mode freezing."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent.paper_trial import (PaperTrialError, PaperTrialRuntime,
                               build_descriptor, new_state, refresh_state,
                               resolve_catalog_arm, validate_state)
from deploy.session_acceptance import summarize_session
from research.diagnostic_shadow import (_logical_arms,
                                        build_diagnostic_cohort)
from tests.test_cohort_integration_regression import _raw_diagnostic
from tests.test_session_acceptance import CLOSE, OPEN, SESSION, SYMBOLS, _sample


START = "2026-08-01"
NOW = datetime(2026, 8, 3, 21, tzinfo=timezone.utc)
VARIANT = _logical_arms()[0]["variant_id"]


def _config(root: Path, *, variant_id: str = VARIANT) -> dict:
    return {
        "mode": "paper",
        "broker": {
            "paper": True,
            "allow_live": False,
            "provider": "alpaca",
            "data_feed": "iex",
            "options_feed": "indicative",
        },
        "universe": {"symbols": ["SPY"], "asset_classes": ["us_equity"]},
        "strategy": {"execution_mode": "shares"},
        "risk": {"risk_per_trade_pct": 0.5},
        "research": {
            "enabled": True,
            "require_validated_variant": True,
            "trial": {
                "enabled": True,
                "min_sessions": 20,
                "min_trades": 20,
                "min_mean_r": 0.0,
                "min_total_r": 0.0,
            },
            "paper_trial": {
                "enabled": True,
                "trial_id": "paper-cohort-contract",
                "variant_id": variant_id,
                "accepted_session_report_root": str(root),
                "max_review_sessions": 60,
            },
        },
    }


def _activated_state(descriptor: dict, config: dict,
                     *, started_on: str = START) -> dict:
    state = new_state(descriptor, config)
    state.update({
        "activation_confirmed": True,
        "activation_account_fingerprint": "paper-account-fingerprint",
        "started_on": started_on,
    })
    return validate_state(state)


def _accepted_report(path: Path, descriptor: dict, *, day: str,
                     identity_descriptor: dict | None = None) -> None:
    """Write the producer-shaped accepted report consumed by paper trials."""
    source = identity_descriptor or descriptor
    opened = datetime.fromisoformat(f"{day}T13:30:00+00:00")
    closed = datetime.fromisoformat(f"{day}T20:00:00+00:00")
    count = 391
    candidates = descriptor["diagnostic_candidate_ids"]
    payload = {
        "schema": "session-acceptance-report.v1",
        "session": {
            "date": day,
            "open": opened.isoformat(),
            "close": closed.isoformat(),
            "source": "alpaca_calendar",
            "source_mode": "forward_observed",
        },
        "accepted": True,
        "status": "accepted",
        "operational_only": True,
        "authorizing": False,
        "promotion_eligible": False,
        "expected_symbols": ["SPY"],
        "reasons": [],
        "reason_counts": {},
        "symbol_failure_counts": {},
        "sample_counts": {
            "total": count,
            "healthy": count,
            "failed": 0,
            "valid_timestamps": count,
        },
        "freshness": {
            "strict_threshold_cap_seconds": 30.0,
            "quote_event_age_seconds": {
                "count": count, "p50": 1.0, "p95": 1.0, "max": 1.0,
            },
            "bar_publication_deadline_lag_seconds": {
                "count": count, "p50": 0.0, "p95": 0.0, "max": 0.0,
            },
            "shadow_source_lag_seconds": {
                "count": count, "p50": 1.0, "p95": 1.0, "max": 1.0,
            },
        },
        "coverage": {
            "first_sample_ts": opened.timestamp(),
            "last_sample_ts": closed.timestamp(),
            "open_ts": opened.timestamp(),
            "close_ts": closed.timestamp(),
            "start_tolerance_seconds": 60.0,
            "end_tolerance_seconds": 60.0,
            "max_sample_gap_seconds": 65.0,
            "max_observed_gap_seconds": 60.0,
            "closed_at_report": True,
        },
        "identities": {
            "deployment": "deploy-a",
            "code": source["code_identity"],
            "cohort": source["cohort_identity"],
            "activation": "activation-a",
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
            "snapshots": count,
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
    path.write_text(json.dumps(payload), encoding="utf-8")


class PaperTrialCohortContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paper-trial-cohort-")
        self.root = Path(self.tmp.name)
        self.config = _config(self.root)
        self.addCleanup(self.tmp.cleanup)

    def _descriptor(self, value: str | None) -> dict:
        if value is None:
            return build_descriptor(self.config)
        with mock.patch.dict(os.environ,
                             {"ALPACA_SHADOW_INCLUDE_IBR": value},
                             clear=False):
            return build_descriptor(self.config)

    def test_real_producer_descriptors_match_24_and_31_cohorts(self):
        for mode, expected_count in (("0", 24), ("1", 31)):
            with self.subTest(mode=mode), mock.patch.dict(
                    os.environ, {"ALPACA_SHADOW_INCLUDE_IBR": mode},
                    clear=False):
                descriptor = build_descriptor(self.config)
                expected = build_diagnostic_cohort(
                    self.config,
                    code_identity=descriptor["code_identity"],
                    include_ibr=mode == "1")
                candidate = next(
                    arm for arm in expected["arms"]
                    if arm["variant_id"] == descriptor["variant_id"])
                self.assertEqual(descriptor["include_ibr"], mode == "1")
                self.assertEqual(len(descriptor["diagnostic_candidate_ids"]),
                                 expected_count)
                self.assertEqual(descriptor["cohort_identity"],
                                 expected["cohort_identity"])
                self.assertEqual(descriptor["candidate_id"],
                                 candidate["candidate_id"])
                self.assertEqual(
                    descriptor["diagnostic_candidate_ids"],
                    sorted(expected["candidate_identities"]))

    def test_full_31_report_advances_tenure_and_opposite_report_fails_closed(self):
        with mock.patch.dict(os.environ,
                             {"ALPACA_SHADOW_INCLUDE_IBR": "1"},
                             clear=False):
            descriptor31 = build_descriptor(self.config)
            state31 = _activated_state(descriptor31, self.config)
            path = self.root / "session-2026-08-02.report.json"
            _accepted_report(path, descriptor31, day="2026-08-02")
            refreshed = refresh_state(
                state31, descriptor31, self.config, now=NOW)
            self.assertEqual(len(refreshed["accepted_sessions"]), 1)
            self.assertEqual(refreshed["accepted_sessions"][0]["date"],
                             "2026-08-02")
            self.assertEqual(refreshed["verdict"]["sessions"], 1)

            descriptor24 = self._descriptor("0")
            _accepted_report(path, descriptor31, day="2026-08-02",
                             identity_descriptor=descriptor24)
            rejected = refresh_state(
                state31, descriptor31, self.config, now=NOW)
            self.assertEqual(rejected["accepted_sessions"], [])
            self.assertIn(
                "report_identity_mismatch:session-2026-08-02.report.json",
                rejected["blockers"])

    def test_real_31_session_acceptance_report_is_counted(self):
        """Synthetic telemetry exercises the actual acceptance producer."""
        config = _config(self.root)
        config["universe"]["symbols"] = list(SYMBOLS)
        with mock.patch.dict(os.environ,
                             {"ALPACA_SHADOW_INCLUDE_IBR": "1"},
                             clear=False):
            descriptor = build_descriptor(config)
            cohort = build_diagnostic_cohort(
                config, code_identity=descriptor["code_identity"],
                include_ibr=True)
            samples = []
            for timestamp in (OPEN, min(OPEN + 60.0, CLOSE), CLOSE):
                sample = _sample(float(timestamp))
                diagnostic = _raw_diagnostic(cohort, float(timestamp))
                sample["shadow"]["diagnostic_shadow"] = diagnostic
                sample["shadow"]["cohort_contract"] = deepcopy(
                    cohort["cohort_contract"])
                sample["shadow"]["candidate_errors"] = {}
                sample["shadow"]["candidate_errors_present"] = True
                sample["identities"].update(
                    code=descriptor["code_identity"],
                    cohort=descriptor["cohort_identity"],
                    activation="activation-1")
                samples.append(sample)
            report = summarize_session(
                samples, session=SESSION, expected=SYMBOLS, now=CLOSE + 1.0,
                start_tolerance=5.0, end_tolerance=5.0, max_sample_gap=65.0)
            self.assertTrue(report["accepted"], report["reasons"])
            self.assertEqual(report["cohort_contract"],
                             cohort["cohort_contract"])
            self.assertEqual(report["post_activation_progress"]["arms"], 31)
            # ``finalize_session`` adds this publication timestamp after the
            # summarize step; mirror that producer-owned metadata while
            # keeping all telemetry synthetic and in-memory.
            report["finalized_ts"] = CLOSE + 1.0
            report_path = self.root / f"session-{SESSION['date']}.report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")

            state = _activated_state(
                descriptor, config, started_on="2026-09-07")
            refreshed = refresh_state(
                state, descriptor, config,
                now=datetime.fromtimestamp(CLOSE + 1.0, timezone.utc))
            self.assertEqual(len(refreshed["accepted_sessions"]), 1)
            self.assertEqual(refreshed["accepted_sessions"][0]["date"],
                             SESSION["date"])
            self.assertEqual(refreshed["verdict"]["sessions"], 1)

    def test_mode_is_strict_and_frozen_active_identity_rejects_switch(self):
        for malformed in ("2", "true", " 1", "1 "):
            with self.subTest(value=malformed), mock.patch.dict(
                    os.environ, {"ALPACA_SHADOW_INCLUDE_IBR": malformed},
                    clear=False):
                with self.assertRaisesRegex(PaperTrialError,
                                             "must be exactly 0 or 1"):
                    build_descriptor(self.config)

        with mock.patch.dict(os.environ,
                             {"ALPACA_SHADOW_INCLUDE_IBR": "0"},
                             clear=False):
            descriptor24 = build_descriptor(self.config)
        active = _activated_state(descriptor24, self.config)
        with mock.patch.dict(os.environ,
                             {"ALPACA_SHADOW_INCLUDE_IBR": "1"},
                             clear=False):
            descriptor31 = build_descriptor(self.config)
            self.assertNotEqual(descriptor24["incumbent_identity"],
                                descriptor31["incumbent_identity"])
            runtime = PaperTrialRuntime(self.config)
            with self.assertRaisesRegex(PaperTrialError,
                                         "identity/config changed"):
                runtime.update_runtime({"paper_trial": active})

    def test_omitted_mode_is_legacy_24_and_ibr_arm_is_unselectable(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            descriptor = build_descriptor(self.config)
            self.assertFalse(descriptor["include_ibr"])
            self.assertEqual(len(descriptor["diagnostic_candidate_ids"]), 24)

            cohort31 = build_diagnostic_cohort(
                self.config, code_identity=descriptor["code_identity"],
                include_ibr=True)
            ibr_variant = next(
                arm["variant_id"] for arm in cohort31["arms"]
                if arm["family"] == "ibr")
            with self.assertRaises(PaperTrialError):
                resolve_catalog_arm(ibr_variant)

        legacy = _activated_state(descriptor, self.config)
        legacy.pop("include_ibr")
        self.assertFalse(validate_state(legacy)["include_ibr"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
