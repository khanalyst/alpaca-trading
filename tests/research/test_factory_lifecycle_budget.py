"""Focused durable lifecycle regressions for the strategy factory."""

from dataclasses import asdict
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.contracts.rule import rule_variant_id, validate_rule_spec
from research.factory_core import initial_hypotheses
from research.factory_ledger import FactoryError, FactoryLedger
from research.factory_report import build_report, render_markdown, render_text
from research.strategy_factory import (
    _execution_blocked_exhausted, _gate, _gate_classification,
    _recenter_successor, run_factory,
)
from .test_factory_end_to_end import edge_corpus


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
    def test_diagnostic_factory_is_non_authorizing_and_does_not_create_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "diagnostic.sqlite3"
            result = run_factory(
                edge_corpus(1), db_path=db_path, vehicle="equity",
                strategies=1, variants_per_strategy=2, workers=1,
                diagnostic_only=True)
            self.assertTrue(result["diagnostic_only"])
            self.assertFalse(result["authorizing"])
            self.assertEqual(result["results"], [])
            self.assertEqual(result["proofs"], [])
            self.assertFalse(result["authorization"]["fdr"])
            self.assertFalse(db_path.exists())
            report = result["reports"][0]["diagnostic"]
            self.assertTrue(report["diagnostic_only"])
            self.assertFalse(report["authorizing"])

    def test_diagnostic_cost_rerun_keeps_in_memory_delta_without_default_artifact(self):
        # The cost rerun remains useful telemetry when enabled, but an
        # explicitly diagnostic factory must not create the helper's default
        # runtime/research/diagnostics artifact directory.
        fake_report = {
            "cost_models": {
                "configured_round_trip_bps": 4.0,
                "measured_round_trip_bps": 5.5,
            },
            "results": [{
                "configured": {"net_r": 0.2},
                "measured": {"net_r": 0.35},
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            previous_cwd = Path.cwd()
            os.chdir(directory)
            try:
                with patch.dict(
                        os.environ,
                        {"ALPACA_RESEARCH_COST_RERUN_ENABLED": "1"},
                        clear=False), \
                        patch("research.cost_rerun.run_cost_rerun",
                              return_value=fake_report), \
                        patch("research.cost_rerun.write_immutable_evidence") as writer:
                    result = run_factory(
                        edge_corpus(1),
                        db_path=Path(directory) / "diagnostic.sqlite3",
                        vehicle="equity", strategies=1,
                        variants_per_strategy=2, workers=1,
                        diagnostic_only=True)
            finally:
                os.chdir(previous_cwd)

            diagnostic = result["cost_diagnostic"]
            self.assertEqual(diagnostic["status"], "completed")
            self.assertEqual(diagnostic["delta"]["round_trip_bps_delta"], 1.5)
            self.assertAlmostEqual(diagnostic["delta"]["mean_net_r_delta"], .15)
            self.assertIsNone(diagnostic["path"])
            self.assertIsNone(diagnostic["report_path"])
            writer.assert_not_called()
            self.assertFalse(
                Path(directory, "runtime", "research", "diagnostics").exists())

    def test_all_execution_blocked_exhaustion_is_rotation_eligible_not_statistical(self):
        blocked = {
            "fit_execution_blocked": True,
            "sample_adequate": False,
            "heldout_sample_adequate": False,
        }
        worker = {"expected_variants": 2}
        refinement = {
            "phase": "interaction",
            "interaction_remaining_before": 2,
        }
        local = [({"variant_id": "a"}, blocked),
                 ({"variant_id": "b"}, blocked)]
        self.assertTrue(_execution_blocked_exhausted(
            local, worker, refinement, coordinate_remaining=0))
        self.assertFalse(_execution_blocked_exhausted(
            local, worker, refinement, coordinate_remaining=1))
        statistical = {**blocked, "fit_execution_blocked": False,
                       "sample_adequate": True,
                       "heldout_sample_adequate": True}
        self.assertFalse(_execution_blocked_exhausted(
            [({"variant_id": "a"}, statistical),
             ({"variant_id": "b"}, blocked)],
            worker, refinement, coordinate_remaining=0))

    def test_gate_execution_blocked_survives_fit_row_persistence_boundary(self):
        rows = [
            {
                "vehicle": "equity", "symbol": "SPY",
                "session_date": f"2026-01-{index:02d}",
                "opportunity_id": f"blocked-{index}",
                "net_pnl": 0.0, "no_trade": True,
                "reject_reason": "entry_slippage_exceeds_limit",
            }
            for index in range(1, 6)
        ]
        gate = _gate(
            rows, rows, vehicle="equity", mode="backtest",
            min_trades=1, min_sessions=1, alpha=1.0, null_rows=rows,
        )
        self.assertEqual(_gate_classification(gate), "execution_blocked")
        # Persistence removes raw partitions before lifecycle grading.  The
        # compact verdict must remain available after that boundary.
        gate.pop("_fit_raw_rows", None)
        self.assertEqual(_gate_classification(gate), "execution_blocked")

    def test_popped_fit_rows_retain_execution_blocked_classification(self):
        gate = {
            "fit_execution_blocked": True,
            "passes": False,
            "sample_adequate": False,
            "heldout_sample_adequate": False,
        }
        self.assertEqual(_gate_classification(gate), "execution_blocked")

    def test_unavailable_qualification_is_not_called_a_failure(self):
        gate = {
            "passes": False,
            "sample_adequate": True,
            "heldout_sample_adequate": True,
            "heldout_net_pnl": 1.0,
            "heldout_expectancy": .1,
            "development_passes_without_family": True,
            "qualification": {"available": False},
        }
        self.assertEqual(_gate_classification(gate), "qualification_unavailable")

    def test_execution_blocked_budget_uses_account_attempt_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            hypothesis = initial_hypotheses(1)[0]
            ledger.register(hypothesis)
            variant_id = rule_variant_id(hypothesis.rule_spec)
            blocked_gate = {
                "passes": False, "sample_adequate": False,
                "heldout_sample_adequate": False,
            }
            for attempt in range(3):
                ledger.add_account(
                    f"blocked-{attempt}", hypothesis.hypothesis_id,
                    _account_result(hypothesis.hypothesis_id, variant_id,
                                    cycle=f"blocked-{attempt}", gate=blocked_gate))
            # Execution-blocked accounts have zero eligible confirmatory
            # attempts, but their durable account rows still bound retries.
            closure = ledger.close_variant(
                hypothesis.hypothesis_id, variant_id, vehicle="equity",
                mode="budget", reason="execution blocked", attempts=0,
                evidence={"classification": "execution_blocked",
                          "account_attempts_total": 3})
            self.assertEqual(closure["mode"], "budget")
            self.assertEqual(closure["attempts"], 0)
            self.assertEqual(closure["account_attempts_total"], 3)

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
