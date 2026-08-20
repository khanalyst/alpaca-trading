"""Focused durable lifecycle regressions for the strategy factory."""

from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest

from agent.contracts.rule import rule_variant_id, validate_rule_spec
from research.factory_core import initial_hypotheses
from research.factory_ledger import FactoryError, FactoryLedger
from research.factory_report import build_report, render_markdown, render_text
from research.strategy_factory import _gate_classification, _recenter_successor


def _account_result(hypothesis_id: str, variant_id: str, *, cycle: str,
                    gate: dict) -> dict:
    return {
        "variant_id": variant_id,
        "vehicle": "equity",
        "worker_pid": 1,
        "gate": gate,
        "account": {
            "account_id": f"account:{cycle}:{variant_id}",
            "starting_cash": 100.0,
            "ending_equity": 99.0,
            "realized_pnl": -1.0,
            "max_drawdown": 1.0,
            "trades": 10,
        },
    }


def _variant_spec(root: dict, **changes: object) -> dict:
    return validate_rule_spec({**root, **changes})


class RecenterLifecycleTests(unittest.TestCase):
    def test_recenter_selects_fit_only_child_and_binds_lineage_ids(self):
        hypothesis = initial_hypotheses(1)[0]
        weaker = _variant_spec(hypothesis.rule_spec, threshold_bps=4.0)
        stronger = _variant_spec(hypothesis.rule_spec, threshold_bps=6.0)
        local = [
            ({"variant_id": rule_variant_id(weaker), "rule_spec": weaker},
             {"sample_adequate": True, "fit_test": {"mean_delta": 0.1},
              "test": {"mean_delta": 99.0}}),
            ({"variant_id": rule_variant_id(stronger), "rule_spec": stronger},
             {"sample_adequate": True, "fit_test": {"mean_delta": 0.8},
              "test": {"mean_delta": -999.0}}),
        ]
        child, payload = _recenter_successor(
            asdict(hypothesis), local, not_before="2026-01-02",
            existing_variant_ids=set(), generation_cap=5)
        self.assertIsNotNone(child)
        self.assertEqual(child.rule_spec, stronger)
        self.assertEqual(child.parent_hypothesis_id, hypothesis.hypothesis_id)
        self.assertEqual(payload["from_variant_id"], rule_variant_id(stronger))
        self.assertEqual(payload["to_variant_id"], rule_variant_id(stronger))
        self.assertEqual(payload["selected_variant_id"], rule_variant_id(stronger))
        self.assertEqual(payload["source_hypothesis_variant_id"],
                         rule_variant_id(hypothesis.rule_spec))
        self.assertEqual(payload["fit_score"], 0.8)
        self.assertEqual(payload["fit_score_source"], "fit_test.mean_delta")

    def test_recenter_generation_cap_is_durable_exhaustion(self):
        hypothesis = initial_hypotheses(1)[0]
        child_spec = _variant_spec(hypothesis.rule_spec, threshold_bps=6.0)
        local = [
            ({"variant_id": rule_variant_id(child_spec), "rule_spec": child_spec},
             {"sample_adequate": True, "fit_test": {"mean_delta": 0.5}}),
        ]
        child, payload = _recenter_successor(
            {**asdict(hypothesis), "generation": 4}, local,
            not_before=None, existing_variant_ids=set(), generation_cap=5)
        self.assertIsNone(child)
        self.assertTrue(payload["bounded_space_exhausted"])
        self.assertEqual(payload["generation_cap"], 5)


class VariantBudgetTests(unittest.TestCase):
    def test_underpowered_accounts_do_not_spend_confirmatory_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            hypothesis = initial_hypotheses(1)[0]
            ledger.register(hypothesis)
            variant_id = rule_variant_id(hypothesis.rule_spec)
            underpowered = {
                "passes": False,
                "sample_adequate": False,
                "heldout_sample_adequate": False,
            }
            for attempt in range(3):
                ledger.add_account(
                    f"thin-cycle-{attempt}", hypothesis.hypothesis_id,
                    _account_result(hypothesis.hypothesis_id, variant_id,
                                    cycle=f"thin-{attempt}", gate=underpowered))
            self.assertEqual(ledger.variant_attempts(
                hypothesis.hypothesis_id, variant_id), 0)
            self.assertEqual(ledger.account_attempts(
                hypothesis.hypothesis_id, variant_id), 3)
            self.assertNotIn(variant_id, ledger.closed_variant_ids(vehicle="equity"))

            adequate = {
                "passes": False,
                "sample_adequate": True,
                "heldout_sample_adequate": True,
            }
            ledger.add_account(
                "adequate-cycle-1", hypothesis.hypothesis_id,
                _account_result(hypothesis.hypothesis_id, variant_id,
                                cycle="adequate-1", gate=adequate))
            self.assertEqual(ledger.variant_attempts(
                hypothesis.hypothesis_id, variant_id), 1)
            self.assertNotIn(variant_id, ledger.closed_variant_ids(vehicle="equity"))
            with self.assertRaises(FactoryError):
                ledger.close_variant(
                    hypothesis.hypothesis_id, variant_id,
                    vehicle="equity", mode="budget",
                    reason="forged attempt count", attempts=3)

            for attempt in (2, 3):
                ledger.add_account(
                    f"adequate-cycle-{attempt}", hypothesis.hypothesis_id,
                    _account_result(hypothesis.hypothesis_id, variant_id,
                                    cycle=f"adequate-{attempt}", gate=adequate))
            self.assertEqual(ledger.variant_attempts(
                hypothesis.hypothesis_id, variant_id), 3)
            closure = ledger.close_variant(
                hypothesis.hypothesis_id, variant_id, vehicle="equity",
                mode="budget", reason="confirmatory budget exhausted",
                attempts=3)
            self.assertEqual(closure["attempts"], 3)
            self.assertEqual(closure["account_attempts_total"], 6)

    def test_adequate_inconclusive_variant_closes_only_at_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            hypothesis = initial_hypotheses(1)[0]
            ledger.register(hypothesis)
            variant_id = rule_variant_id(hypothesis.rule_spec)
            gate = {
                "passes": False,
                "sample_adequate": True,
                "heldout_sample_adequate": True,
                "heldout_net_pnl": -0.1,
                "heldout_expectancy": -0.1,
                "retirement_evidence": {
                    "rejects_minimum_useful_edge": False,
                    "multi_window_negative": False,
                },
            }
            self.assertEqual(_gate_classification(gate),
                             "adequate_negative_inconclusive")
            for attempt in range(1, 4):
                ledger.add_account(
                    f"cycle-{attempt}", hypothesis.hypothesis_id,
                    _account_result(hypothesis.hypothesis_id, variant_id,
                                    cycle=str(attempt), gate=gate))
                self.assertEqual(ledger.variant_attempts(
                    hypothesis.hypothesis_id, variant_id), attempt)
                if attempt < 3:
                    self.assertNotIn(variant_id,
                                     ledger.closed_variant_ids(vehicle="equity"))
            closure = ledger.close_variant(
                hypothesis.hypothesis_id, variant_id, vehicle="equity",
                mode="budget", reason="confirmatory budget exhausted",
                attempts=3, evidence={"classification": "adequate_negative_inconclusive"})
            self.assertEqual(closure["mode"], "budget")
            self.assertEqual(closure["attempts"], 3)
            self.assertIn(variant_id, ledger.failed_variant_ids(vehicle="equity"))
            restarted = FactoryLedger(Path(directory) / "edge.sqlite3")
            self.assertEqual(restarted.variant_closures(vehicle="equity")[0]["mode"],
                             "budget")

    def test_underpowered_sibling_stays_open_while_adequate_sibling_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            hypothesis = initial_hypotheses(1)[0]
            ledger.register(hypothesis)
            thin_id = "rule.thin"
            pass_id = "rule.pass"
            thin_gate = {
                "sample_adequate": False, "heldout_sample_adequate": False,
                "heldout_source": [{"session_date": "2026-01-01"}],
            }
            pass_gate = {
                "sample_adequate": True, "heldout_sample_adequate": True,
                "heldout_source": [{"session_date": "2026-01-02"}],
            }
            ledger.add_account("thin-cycle", hypothesis.hypothesis_id,
                               _account_result(hypothesis.hypothesis_id, thin_id,
                                               cycle="thin", gate=thin_gate))
            self.assertEqual(ledger.evidence_sessions("equity"), set())
            self.assertNotIn(thin_id, ledger.closed_variant_ids(vehicle="equity"))
            ledger.add_account("pass-cycle", hypothesis.hypothesis_id,
                               _account_result(hypothesis.hypothesis_id, pass_id,
                                               cycle="pass", gate=pass_gate))
            self.assertEqual(ledger.evidence_sessions("equity"), {"2026-01-02"})
            self.assertNotIn(thin_id, ledger.failed_variant_ids(vehicle="equity"))


class SearchStateReportTests(unittest.TestCase):
    def test_legacy_slot_without_search_state_is_not_exhausted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edge.sqlite3"
            ledger = FactoryLedger(path)
            hypothesis = initial_hypotheses(1)[0]
            ledger.register(hypothesis)

            report = build_report(path, vehicle="equity")
            vehicle = report["vehicles"][0]

            self.assertEqual(vehicle["search_state"], {})
            self.assertFalse(vehicle["search_exhausted"])

    def test_search_exhaustion_state_is_visible_in_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edge.sqlite3"
            ledger = FactoryLedger(path)
            hypothesis = initial_hypotheses(1)[0]
            ledger.register(hypothesis)
            state = {
                "state": "bounded_space_exhausted",
                "coordinate_total": 4, "coordinate_remaining": 0,
                "interaction_total": 2, "interaction_remaining": 0,
                "closed_count": 4, "open_count": 0,
                "eligible_confirmatory_attempts": 2,
                # Retain the old alias with a deliberately different value to
                # prove current renderers prefer the explicit field.
                "confirmatory_attempts": 99,
                "account_attempts_total": 5, "confirmatory_budget": 3,
            }
            ledger.event(hypothesis.hypothesis_id, "bounded_space_exhausted",
                         "bounded search exhausted", {"search_state": state})
            ledger.add_cycle("cycle", "dataset", "equity", 1, 1, 0,
                             {"status": "bounded_space_exhausted",
                              "search_state": {"0": state},
                              "search_exhausted": True})
            report = build_report(path, vehicle="equity")
            vehicle = report["vehicles"][0]
            self.assertTrue(vehicle["search_exhausted"])
            self.assertEqual(vehicle["search_state"]["0"]["state"],
                             "bounded_space_exhausted")
            text = render_text(report)
            self.assertIn("search exhausted: True", text)
            self.assertIn("eligible confirmatory attempts 2/3", text)
            self.assertIn("account attempts total 5", text)
            markdown = render_markdown(report)
            self.assertIn("eligible confirmatory attempts 2/3", markdown)
            self.assertIn("account attempts total 5", markdown)


if __name__ == "__main__":
    unittest.main()
