from pathlib import Path
import tempfile
import unittest

from research.factory_ledger import FactoryLedger, experiment_identity


class FactoryExperimentIdentityTests(unittest.TestCase):
    def test_changed_assumptions_do_not_reuse_a_prior_cycle(self):
        base = {
            "dataset_hash": "dataset",
            "vehicle": "equity",
            "code_hash": "code",
            "config_hash": "config",
            "cost": {"fee_bps": 0.5},
            "risk": {"risk_per_trade_pct": 0.5},
            "gate": {"alpha": 0.05},
            "provenance": {"dataset_hash": "dataset"},
        }
        original = experiment_identity(**base)
        changed_cost = experiment_identity(
            **{**base, "cost": {"fee_bps": 1.0}})
        self.assertNotEqual(original["identity_hash"],
                            changed_cost["identity_hash"])

        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            ledger.add_cycle(
                "cycle-1", "dataset", "equity", 1, 1, 2,
                {"cycle_id": "cycle-1", "marker": "original"},
                identity=original)

            reused = ledger.existing_cycle("dataset", "equity", original)
            self.assertEqual(reused["marker"], "original")
            self.assertIsNone(ledger.existing_cycle(
                "dataset", "equity", changed_cost))


if __name__ == "__main__":
    unittest.main()
