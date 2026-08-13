"""Production-path control resolution checks for the live shadow worker."""

from pathlib import Path
import tempfile
import unittest

from agent.contracts.rule import rule_variant_id, validate_rule_spec
from research.edge_ledger import EdgeLedger
from research.factory_core import template_hypothesis
from research.factory_ledger import FactoryLedger
from research.live_shadow import ShadowConfig, ShadowRunner, _read_candidates


class LiveShadowControlTests(unittest.TestCase):
    def test_rule_descendant_uses_factory_root_synthetic_control(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            edge_path = root / "edge.sqlite3"
            shadow_path = root / "shadow.sqlite3"
            hypothesis = template_hypothesis(0)
            FactoryLedger(edge_path).register(hypothesis)
            root_spec = validate_rule_spec(hypothesis.rule_spec)
            descendant = validate_rule_spec({**root_spec, "target_r": 2.5})
            ledger = EdgeLedger(edge_path)
            candidate = ledger.register_candidate(
                rule_variant_id(descendant), strategy_id="rule", vehicle="equity",
                hypothesis="descendant", config={
                    "strategy": {"id": "rule", "rule_spec": descendant}},
                axes={"hypothesis_id": hypothesis.hypothesis_id})
            control = ShadowRunner(ShadowConfig(root / "empty.csv", edge_path,
                                                shadow_path))._rule_root_control(candidate)
            self.assertIsNotNone(control)
            self.assertEqual(control["candidate_id"],
                             f"shadow:baseline:{candidate['candidate_id']}")
            self.assertEqual(control["config"]["strategy"]["rule_spec"], root_spec)
            self.assertIsNone(ShadowRunner(ShadowConfig(
                root / "empty.csv", edge_path, root / "shadow2.sqlite"
            ))._rule_root_control(ledger.register_candidate(
                rule_variant_id(root_spec), strategy_id="rule", vehicle="equity",
                hypothesis="root", config={
                    "strategy": {"id": "rule", "rule_spec": root_spec}},
                axes={"hypothesis_id": hypothesis.hypothesis_id})))

    def test_ibr_baseline_is_read_even_while_candidate_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            edge_path = root / "edge.sqlite3"
            ledger = EdgeLedger(edge_path)
            baseline = ledger.register_candidate(
                "ibr.baseline", strategy_id="ibr", vehicle="equity",
                hypothesis="baseline", config={"strategy": {"id": "ibr"}})
            candidate = ledger.register_candidate(
                "ibr.range.30", strategy_id="ibr", vehicle="equity",
                hypothesis="candidate", config={"strategy": {"id": "ibr"}})
            rows = _read_candidates(edge_path, max_candidates=10)
            ids = {row["candidate_id"] for row in rows}
            self.assertIn(baseline["candidate_id"], ids)
            self.assertNotIn(candidate["candidate_id"], ids)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
