"""Regression tests for the conservative shipped paper-edge default."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent.config import DEFAULT_CONFIG, load_config, validate_config
from agent.contracts.rule import rule_variant_id, validate_rule_spec
from agent.edge import resolve_validated_variant, resolve_validated_variants
from deploy import dashboard
from research.edge_identity import candidate_assumptions
from research.edge_lab import EdgeLedger


class DefaultPaperSelectionTests(unittest.TestCase):
    def test_shipped_and_code_defaults_select_one_auto_variant(self):
        self.assertEqual(DEFAULT_CONFIG["strategy"]["selection_mode"],
                         "specific")
        self.assertEqual(DEFAULT_CONFIG["strategy"]["variant_id"], "auto")

        validated = validate_config({})
        self.assertEqual(validated["strategy"]["selection_mode"], "specific")
        self.assertEqual(validated["strategy"]["variant_id"], "auto")
        self.assertEqual(
            validate_config({"strategy": {"selection_mode": "all_proved"}})
            ["strategy"]["selection_mode"], "all_proved")

        root = Path(__file__).resolve().parents[1]
        shipped = json.loads((root / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(shipped["strategy"]["selection_mode"], "specific")
        self.assertEqual(shipped["strategy"]["variant_id"], "auto")
        loaded = load_config(root / "config.yaml")
        self.assertEqual(loaded["strategy"]["selection_mode"], "specific")
        self.assertEqual(loaded["strategy"]["variant_id"], "auto")

    def test_specific_auto_runtime_path_returns_exactly_one_record(self):
        config = validate_config({})
        selected = {"candidate_id": "champion", "variant_id": "rule.winner"}
        with patch("agent.edge.resolve_validated_variant",
                   return_value=selected) as resolver:
            self.assertEqual(resolve_validated_variants(config), [selected])
        resolver.assert_called_once_with(config, vehicle=None, db_path=None)

    def test_dashboard_raw_config_fallback_matches_shipped_selection_mode(self):
        view = dashboard._promotions({}, Path("edge.sqlite3"))
        self.assertEqual(view["selection_mode"], "specific")

    def test_auto_runtime_uses_strongest_validated_candidate_and_ignores_corpus(self):
        """Only validated/champion records can enter the global auto lane."""
        with tempfile.TemporaryDirectory(prefix="default-paper-selection-") as directory:
            db_path = Path(directory) / "edges.sqlite3"
            ledger = EdgeLedger(db_path)
            base = validate_config({})
            candidates = {}
            for name, family, status in (
                    ("weak", "mean_reversion", "validated"),
                    ("strong", "volume_breakout", "validated"),
                    ("corpus-only", "opening_range_breakout", "candidate")):
                spec = validate_rule_spec({"family": family, "lookback": 10})
                variant_id = rule_variant_id(spec)
                assumptions = candidate_assumptions(
                    base, vehicle="equity", strategy_id="rule",
                    variant_id=variant_id, rule_spec=spec)
                record = ledger.register_candidate(
                    variant_id, strategy_id="rule", vehicle="equity",
                    hypothesis=name, config=assumptions)
                with closing(sqlite3.connect(db_path)) as db, db:
                    db.execute("UPDATE candidate_state SET status=? WHERE candidate_id=?",
                               (status, record["candidate_id"]))
                candidates[name] = ledger.candidate(record["candidate_id"])

            gates = {
                candidates["weak"]["candidate_id"]: self._gate(.10, 10),
                candidates["strong"]["candidate_id"]: self._gate(.25, 5),
                # This candidate has the most attractive-looking evidence, but
                # its corpus status must keep it out of automatic selection.
                candidates["corpus-only"]["candidate_id"]: self._gate(.90, 1),
            }

            def gate_for(candidate_id, *, lane=None):
                gate = gates[candidate_id]
                return ({"lane": "shadow"}, gate)

            def eligibility_for(candidate_id, *, lane="shadow"):
                record = ledger.candidate(candidate_id)
                proof = {"lane": "shadow",
                         "config_hash": record["config_hash"],
                         "verified_gate": gates[candidate_id]}
                return {"eligible": True, "latest_verified_run": proof}

            config = validate_config({"research": {"db_path": str(db_path)}})
            with patch.object(EdgeLedger, "_latest_verified_gate",
                              side_effect=gate_for), \
                    patch.object(EdgeLedger, "_live_shadow_authorized",
                                 return_value=True), \
                    patch.object(EdgeLedger, "eligibility",
                                 side_effect=eligibility_for):
                resolved = resolve_validated_variant(config, db_path=db_path)

            self.assertIsNotNone(resolved)
            self.assertEqual(resolved["variant_id"],
                             candidates["strong"]["variant_id"])
            self.assertEqual(resolved["status"], "champion")
            self.assertNotEqual(resolved["variant_id"],
                                candidates["corpus-only"]["variant_id"])

    @staticmethod
    def _gate(lower_bound, drawdown):
        return {
            "statistics": {"q_value": .01},
            "performance": {
                "heldout_delta": lower_bound + .05,
                "heldout_delta_lcb": lower_bound,
                "max_drawdown": drawdown,
            },
            "counts": {"heldout": {"trades": 100}},
            "passes": True,
        }


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
