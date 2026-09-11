"""Bounded status projections keep experiments separate from verified proof."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from agent.config import load_config
from deploy import dashboard, health
from tests.test_parallel_runtime_status import _diagnostic_coverage


ROOT = Path(__file__).resolve().parents[1]


def trial_selection():
    return {
        "configured_strategy": "rule", "selection_mode": "paper_trial",
        "requested_variant": "rule.incumbent", "state": "paper_trial",
        "armed": True, "blocker_code": None,
        "resolved": {"candidate_id": "paper-candidate", "variant_id": "rule.incumbent",
                     "family": "opening_range", "proof": None},
        "paper_trial": {
            "schema": "paper-incumbent-trial.v1", "enabled": True,
            "trial_id": "trial-one", "candidate_id": "paper-candidate",
            "variant_id": "rule.incumbent", "incumbent_identity": "frozen-policy",
            "state": "running", "entry_eligible": True,
            "activation_confirmed": True,
            "valid_sessions": 7, "required_sessions": 20, "closed_outcomes": 5,
            "required_trades": 20, "max_review_sessions": 60,
            "verdict": "running", "started_on": "2026-09-09", "blockers": [],
            "authorizing": False, "proof_authority": False,
            "verdict_detail": {"state": "running", "net_pnl": -12.5,
                               "mean_r": -.1, "unbounded": "discard"},
            "report_identities": {"unbounded": "discard"},
        },
    }


class PaperShadowStatusTests(unittest.TestCase):
    def test_paper_experiment_roundtrips_without_invented_proof(self):
        value = trial_selection()
        projected = health.paper_selection_summary(value)
        self.assertEqual(projected["state"], "paper_trial")
        self.assertIsNone(projected["resolved"]["proof"])
        self.assertFalse(projected["resolved"]["proof_authority"])
        self.assertEqual(projected["paper_trial"]["valid_sessions"], 7)
        self.assertTrue(projected["paper_trial"]["activation_confirmed"])
        self.assertEqual(projected["paper_trial"]["verdict_detail"]["net_pnl"], -12.5)
        self.assertNotIn("unbounded", projected["paper_trial"]["verdict_detail"])
        self.assertNotIn("report_identities", projected["paper_trial"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat.json"
            path.write_text(json.dumps({"status": "running", "updated_ts": 100,
                                        "paper_selection": value}))
            self.assertEqual(health.trader(path, 60, now=100)["paper_selection"],
                             dashboard._safe_heartbeat(path)["paper_selection"])

    def test_experiment_cannot_be_labeled_qualified_or_carry_proof(self):
        for mutation in ("ready", "proof", "authority", "identity", "disarmed",
                         "unactivated"):
            with self.subTest(mutation=mutation):
                value = trial_selection()
                if mutation == "ready":
                    value["state"] = "ready"
                elif mutation == "proof":
                    value["resolved"]["proof"] = {
                        "run_id": "invented", "gate_hash": "invented",
                        "config_hash": "invented", "lane": "shadow"}
                elif mutation == "authority":
                    value["paper_trial"]["authorizing"] = True
                elif mutation == "identity":
                    value["resolved"]["variant_id"] = "different"
                elif mutation == "unactivated":
                    value["paper_trial"]["activation_confirmed"] = False
                else:
                    value["armed"] = False
                self.assertIsNone(health.paper_selection_summary(value))

    def test_safety_blocker_does_not_discard_incumbent_progress(self):
        value = trial_selection()
        value.update(state="blocked", armed=False, blocker_code="daily_risk_limit")
        projected = health.paper_selection_summary(value)
        self.assertFalse(projected["armed"])
        self.assertEqual(projected["paper_trial"]["trial_id"], "trial-one")
        self.assertEqual(projected["blocker_code"], "daily_risk_limit")

    def test_persistent_account_pnl_is_signed_and_missing_marks_stay_unknown(self):
        raw = _diagnostic_coverage()
        identity = raw["arms"][0]["candidate_id"]
        raw["forward_accounts"] = {
            "schema": "diagnostic-forward-accounts-summary.v1", "account_count": 1,
            "actual_fills": 0, "orders": 3, "modeled_fills": 3,
            "cash": 99987.5, "equity": None, "realized_pnl": -12.5,
            "unrealized_pnl": None, "unbounded": "discard",
            "by_candidate": [{"candidate_id": identity, "cash": 99987.5,
                              "equity": None, "realized_pnl": -12.5,
                              "unrealized_pnl": None, "fills": 3,
                              "mark_status": "unpriced", "unbounded": "discard"}],
        }
        raw["rejection_counts"]["by_reason"] = {"stressed_cost": 10, "bad_count": -1}
        projected = health._shadow_diagnostic_summary(raw)
        accounts = projected["forward_accounts"]
        self.assertFalse(accounts["authorizing"])
        self.assertEqual(accounts["realized_pnl"], -12.5)
        self.assertIsNone(accounts["equity"])
        self.assertEqual(accounts["by_candidate"][0]["variant_id"], raw["arms"][0]["variant_id"])
        self.assertNotIn("unbounded", accounts)
        self.assertNotIn("unbounded", accounts["by_candidate"][0])
        self.assertEqual(projected["rejection_counts"]["by_reason"], {"stressed_cost": 10})
        raw["forward_accounts"]["actual_fills"] = 1
        self.assertNotIn("forward_accounts", health._shadow_diagnostic_summary(raw))

    def test_default_policy_is_explicit_and_paper_experiment_remains_opt_in(self):
        config = load_config(ROOT / "config.yaml")
        self.assertEqual(config["research"]["trial"]["min_sessions"], 20)
        self.assertEqual(config["research"]["trial"]["min_trades"], 20)
        self.assertFalse(config["research"]["paper_trial"]["enabled"])
        self.assertTrue(config["research"]["require_validated_variant"])
        self.assertEqual(config["research"]["paper_trial"]["max_review_sessions"], 60)

    def test_compose_shares_acceptance_read_only_and_uses_existing_service(self):
        compose = (ROOT / "compose.yaml").read_text()
        trader = compose.split("\n  trader:\n", 1)[1].split("\n  watchdog:\n", 1)[0]
        self.assertIn("shadow-data:/app/shadow:ro", trader)
        self.assertIn("ALPACA_RESEARCH_ACCEPTANCE_ROOT:-/app/shadow/session-acceptance", compose)
        self.assertIn("${ALPACA_SHADOW_INTERVAL_SECONDS:-30}", compose)
        self.assertNotIn("session_acceptance.py monitor", compose)
        example = (ROOT / ".env.example").read_text()
        self.assertIn("ALPACA_RECORDER_INTERVAL_SECONDS=30\n", example)
        self.assertIn("ALPACA_SHADOW_INTERVAL_SECONDS=30\n", example)

    def test_dashboard_distinguishes_experiment_books_and_qualification(self):
        for marker in ("Frozen Alpaca paper experiment", "valid market sessions",
                       "Persistent shadow books — modeled, not broker returns",
                       "Why shadow signals did not trade", "Unknown marks stay unknown",
                       "Qualified-edge paper reviews — separate from the experiment"):
            self.assertIn(marker, dashboard.HTML)

    def test_expected_session_wait_is_alive_but_never_profit_evidence(self):
        readiness = {
            "schema": "research-readiness.v1",
            "state": "waiting_for_forward_sessions",
            "reason": "0 accepted full sessions; 30 are required",
            "recorded_sessions": 0, "required_sessions": 30,
            "sessions_remaining": 30, "authorizing": False,
        }
        for claimed_evidence in (False, True):
            with self.subTest(claimed_evidence=claimed_evidence):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "research.json"
                    path.write_text(json.dumps({
                        "status": "waiting_for_forward_sessions", "updated_ts": 100,
                        "last_exit_code": 0, "research_readiness": readiness,
                        "research_cycle": {
                            "status": "waiting_for_forward_sessions",
                            "evidence_available": claimed_evidence,
                        },
                    }))
                    projected = health.research(path, 60, now=100)
                self.assertTrue(projected["ok"])
                self.assertTrue(projected["scheduler_operational"])
                self.assertFalse(projected["previous_cycle_failed"])
                self.assertFalse(projected["evidence_available"])
                self.assertFalse(projected["readiness_ok"])
                self.assertEqual(projected["status"], "waiting_for_forward_sessions")
                self.assertEqual(projected["reason"], readiness["reason"])
                self.assertEqual(projected["research_readiness"]["sessions_remaining"], 30)


if __name__ == "__main__":
    unittest.main()
