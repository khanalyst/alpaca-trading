"""Exact session candidates must satisfy main's real execution evidence checks."""

from copy import deepcopy
from datetime import date, timedelta
import unittest

from research.session_candidate_review import (
    CandidateReviewError, EVIDENCE_SCHEMA, _hash, candidate_request,
    review_discovery, review_execution_evidence,
)


def experiment():
    inputs = {"minute:SPY": "1" * 64, "daily:SPY": "2" * 64}
    code = {"strategies.py": "3" * 64, "grammar.py": "4" * 64}
    manifest = {"schema": "session-edges-experiment.v2", "input_digests": inputs,
                "source_provenance": {"mode": "historical_backfill"},
                "dataset_digest": _hash(inputs), "context_digest": "5" * 64,
                "code_digest": _hash(code), "code_files": code,
                "cost_digest": "6" * 64, "protocol_digest": "7" * 64}
    return {"id": _hash(manifest), "manifest": manifest}


def promoted(family="close_weakness_reversion"):
    from research.session_candidate_review import _EVIDENCE_FIELDS
    row = {"spec": {"family": family, "symbol": "SPY", "params": {}},
           "source": "grid", "cost_bps": 1.0, "design": {"sessions": 1000},
           "holdout": {"sessions": 1000, "round_trips": 1000}, "recent": {},
           "neighbour_share": 1.0, "data_last_session": "2024-12-31",
           "promoted_at": "2025-01-01T00:00:00+00:00", "experiment_name": "research-v2",
           "experiment_id": experiment()["id"], "tier": "historical_supported",
           "authorizing": False, "requires_main_qualification": True}
    row["evidence_digest"] = _hash({k: row.get(k) for k in _EVIDENCE_FIELDS})
    return row


def execution_rows(request, *, sessions=100, start=date(2025, 1, 1), control=False):
    rows = []
    for number in range(sessions):
        day = (start + timedelta(days=number)).isoformat()
        for trade in range(3):
            rows.append({"vehicle": "equity", "symbol": "SPY", "session_date": day,
                         "opportunity_id": f"{day}-{trade}",
                         "comparison_id": f"SPY:{day}-{trade}",
                         "entry_timestamp": f"{day}T14:{35 + trade:02d}:00+00:00",
                         "exit_timestamp": f"{day}T14:{36 + trade:02d}:00+00:00",
                         "entry_price": 100.0, "exit_price": 101.0, "quantity": 1.0,
                         "risk_usd": 10.0, "net_pnl": 0.0 if control else 1.0,
                         "entry_fill_source": "quote", "exit_fill_source": "quote",
                         "entry_feed": "sip", "exit_feed": "sip",
                         "entry_provider": "alpaca", "exit_provider": "alpaca",
                         "entry_quote_age_seconds": 0.0, "exit_quote_age_seconds": 0.0,
                         "session_candidate_request_id": request["request_id"],
                         **({"session_control_identity": "8" * 64} if control else
                            {"session_execution_identity": request["execution_identity"]})})
    return rows


def execution_evidence(request, *, sessions=100, qualification=False):
    payload = {"schema": EVIDENCE_SCHEMA, "request_id": request["request_id"],
               "control_identity": "8" * 64,
               "candidate_rows": execution_rows(request, sessions=sessions),
               "baseline_rows": execution_rows(request, sessions=sessions, control=True)}
    if qualification:
        start = date(2025, 5, 1)
        payload.update({
            "qualification_rows": execution_rows(request, sessions=40, start=start),
            "qualification_baseline_rows": execution_rows(request, sessions=40, start=start, control=True),
            "qualification_sessions": [(start + timedelta(days=n)).isoformat() for n in range(40)]})
    return payload


class SessionCandidateReviewTests(unittest.TestCase):
    def test_daily_and_minute_reversion_have_different_exact_execution_identity(self):
        minute = candidate_request(promoted(), experiment())
        daily = candidate_request(promoted("close_weakness_reversion_daily"), experiment())
        self.assertNotEqual(minute["execution_identity"], daily["execution_identity"])
        self.assertEqual(daily["spec"]["family"], "close_weakness_reversion_daily")
        self.assertFalse(daily["kernel"]["runner_translation_permitted"])
        self.assertIn("next-session-open", daily["kernel"]["execution_form"])

    def test_historical_sessions_and_trips_are_not_manufactured_as_executions(self):
        request = candidate_request(promoted(), experiment())
        report = review_execution_evidence(request)
        self.assertEqual(report["floors"]["heldout"]["trades"], 0)
        self.assertFalse(report["floors"]["heldout"]["adequate"])
        self.assertFalse(report["qualified"])
        self.assertIn("trade_level_execution_observations_required", report["blockers"])

    def test_real_bound_quote_rows_use_unchanged_main_protocol_floors(self):
        request = candidate_request(promoted(), experiment())
        report = review_execution_evidence(request, execution_evidence(request, qualification=True))
        self.assertEqual(report["floors"]["fit"]["trades"], 180)
        self.assertEqual(report["floors"]["heldout"]["trades"], 120)
        self.assertTrue(report["floors"]["heldout"]["adequate"])
        self.assertTrue(report["paired_control"]["adequate"])
        self.assertEqual(report["qualification"]["trades"], 120)
        self.assertTrue(report["qualification"]["adequate"])
        self.assertFalse(report["qualification"]["post_selection"]["preselected"])
        self.assertFalse(report["qualified"])
        self.assertFalse(report["authorizing"])

    def test_bar_fallbacks_are_excluded_by_strict_main_projection(self):
        request = candidate_request(promoted(), experiment())
        payload = execution_evidence(request, sessions=1)
        for row in payload["candidate_rows"]:
            row.update(entry_fill_source="bar", exit_fill_source="bar")
        report = review_execution_evidence(request, payload)
        self.assertEqual(report["projection"]["candidate"]["counts"]["eligible"], 0)
        self.assertEqual(report["floors"]["fit"]["trades"], 0)

    def test_different_candidate_or_edited_request_is_refused(self):
        request = candidate_request(promoted(), experiment())
        payload = execution_evidence(request, sessions=1)
        payload["candidate_rows"][0]["session_execution_identity"] = "0" * 64
        with self.assertRaisesRegex(CandidateReviewError, "different rule"):
            review_execution_evidence(request, payload)
        edited = deepcopy(request)
        edited["spec"]["params"]["max_sessions"] = 10
        with self.assertRaisesRegex(CandidateReviewError, "request identity"):
            review_execution_evidence(edited)

    def test_used_historical_or_development_sessions_cannot_be_resealed(self):
        request = candidate_request(promoted(), experiment())
        payload = execution_evidence(request, sessions=1)
        payload["qualification_sessions"] = ["2025-01-01"]
        with self.assertRaisesRegex(CandidateReviewError, "reuses historical/development"):
            review_execution_evidence(request, payload)

    def test_qualification_seal_has_one_candidate_owner_within_batch(self):
        request = candidate_request(promoted(), experiment())
        payload = execution_evidence(request, sessions=1, qualification=True)
        used = {_hash(payload["qualification_sessions"])}
        with self.assertRaisesRegex(CandidateReviewError, "already consumed"):
            review_execution_evidence(request, payload, used_seals=used)

    def test_mismatched_manifest_and_counterfeit_authority_are_refused(self):
        row = promoted()
        changed = deepcopy(experiment())
        changed["manifest"]["cost_digest"] = "0" * 64
        with self.assertRaisesRegex(CandidateReviewError, "experiment identity"):
            candidate_request(row, changed)
        row["authorizing"] = True
        with self.assertRaisesRegex(CandidateReviewError, "nonauthorizing"):
            candidate_request(row, experiment())

    def test_v2_discovery_import_retains_main_authority_and_batch_correction(self):
        record = {"schema": "session-edges-discovery.v2", "authorizing": False,
                  "experiments": {"research-v2": experiment()}, "promoted": {"source-1": promoted()}}
        result = review_discovery(record)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["qualified_count"], 0)
        self.assertEqual(result["candidates"][0]["main_gate_readiness"]["diagnostic_batch_q_value"], 1.0)
        record["schema"] = "session-edges-discovery.v1"
        with self.assertRaisesRegex(CandidateReviewError, "v2"):
            review_discovery(record)


if __name__ == "__main__":
    unittest.main()
