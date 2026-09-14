"""Shared diagnostic cohort layout, health, and session acceptance tests."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from agent.contracts.rule import RULE_FAMILIES
from deploy import health
from deploy.session_acceptance import summarize_session
from research.diagnostic_cohort_contract import validate_cohort_layout
from tests.test_session_acceptance import CLOSE, OPEN, SESSION, SYMBOLS, _sample


def _layout(*, include_ibr: bool = False) -> dict:
    families = list(RULE_FAMILIES) + (["ibr"] if include_ibr else [])
    arms: list[dict] = []
    for family in families:
        roles = (["baseline", "variant"] if family != "ibr" else
                 ["baseline", *["variant"] * 6])
        for index, role in enumerate(roles):
            candidate_id = f"shadow:diagnostic:{family}:{role}:{index}"
            arms.append({
                "candidate_id": candidate_id,
                "family": family,
                "role": role,
                "variant_id": f"{family}.{role}.{index}",
                "code_identity": "code-1",
                "cohort_identity": "cohort-1",
            })
    return {
        "arms": arms,
        "candidate_identities": [arm["candidate_id"] for arm in arms],
        "families_total": len(families),
        "baseline_count": len(families),
        "variant_count": 18 if include_ibr else 12,
        "code_identity": "code-1",
        "cohort_identity": "cohort-1",
    }


def _full31_sample(ts: float) -> dict:
    sample = _sample(ts)
    diagnostic = sample["shadow"]["diagnostic_shadow"]
    extra = _layout(include_ibr=True)["arms"][-7:]
    diagnostic["arms"].extend(extra)
    diagnostic["candidate_identities"].extend(
        arm["candidate_id"] for arm in extra)
    diagnostic.update({
        "families_total": 13,
        "families_covered": 13,
        "families_observed": 13,
        "baseline_count": 13,
        "variant_count": 18,
    })
    first_cursor = next(iter(diagnostic["processed_event_cursors"].values()))
    cursor = dict(first_cursor)
    diagnostic["activation_event_watermark"]["last_inserted_at"] = OPEN - 20.0
    diagnostic["processed_event_cursors"].update({
        arm["candidate_id"]: dict(cursor) for arm in extra})
    diagnostic["processed_events"] = sum(
        int(value["processed_events"])
        for value in diagnostic["processed_event_cursors"].values())
    return sample


class DiagnosticCohortContractTests(unittest.TestCase):
    def test_both_supported_layouts_are_normalized_from_full_arms(self):
        for include_ibr, counts in ((False, (24, 12, 12, 12)),
                                    (True, (31, 13, 13, 18))):
            with self.subTest(include_ibr=include_ibr):
                contract = validate_cohort_layout(_layout(include_ibr=include_ibr))
                self.assertIsNotNone(contract)
                self.assertEqual(tuple(contract[key] for key in (
                    "arm_count", "family_count", "baseline_count",
                    "variant_count")), counts)
                self.assertEqual(contract["candidate_ids"], sorted(
                    arm["candidate_id"] for arm in contract["arms"]))

    def test_duplicate_missing_extra_roles_and_mismatches_reject(self):
        cases = []
        duplicate = _layout()
        duplicate["arms"][1]["candidate_id"] = duplicate["arms"][0]["candidate_id"]
        cases.append(duplicate)
        missing = _layout()
        missing["arms"].pop()
        missing["candidate_identities"].pop()
        cases.append(missing)
        extra_role = _layout(include_ibr=True)
        extra_role["arms"][-1]["role"] = "baseline"
        cases.append(extra_role)
        mismatched_ids = _layout()
        mismatched_ids["candidate_identities"][0] = "shadow:diagnostic:not-an-arm"
        cases.append(mismatched_ids)
        mismatched_counts = _layout()
        mismatched_counts["families_total"] = 31
        cases.append(mismatched_counts)
        unknown_family = _layout()
        unknown_family["arms"][0]["family"] = "operator-invented"
        cases.append(unknown_family)
        for value in cases:
            with self.subTest():
                self.assertIsNone(validate_cohort_layout(value))

    def test_full31_health_projection_is_ready_without_24_truncation(self):
        health_now = OPEN + 100.0
        diagnostic = _full31_sample(health_now)["shadow"]["diagnostic_shadow"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            path.write_text(json.dumps({
                "status": "running", "updated_ts": health_now,
                "candidate_errors": {}, "diagnostic_shadow": diagnostic,
            }), encoding="utf-8")
            result = health.shadow(path, 60.0, now=health_now)
        self.assertTrue(result["coverage_ready"], result["coverage_status"])
        projected = result["diagnostic_shadow"]
        self.assertEqual(projected["arm_count"], 31)
        self.assertEqual(projected["arms_total"], 31)
        self.assertEqual(projected["cursor_count"], 31)

    def test_full31_session_report_publishes_validated_contract(self):
        samples = [_full31_sample(float(ts)) for ts in range(
            int(OPEN), int(CLOSE) + 1, 5)]
        report = summarize_session(
            samples, session=SESSION, expected=SYMBOLS, now=CLOSE + 1.0,
            start_tolerance=5.0, end_tolerance=5.0, max_sample_gap=10.0)
        self.assertTrue(report["accepted"], report["reasons"])
        contract = report["cohort_contract"]
        self.assertEqual(contract["arm_count"], 31)
        self.assertEqual(contract["family_count"], 13)
        self.assertEqual(contract["baseline_count"], 13)
        self.assertEqual(contract["variant_count"], 18)
        self.assertEqual(report["post_activation_progress"]["arms"], 31)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
