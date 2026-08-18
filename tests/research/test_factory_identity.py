from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.config import validate_config
from research.factory_ledger import FactoryLedger, experiment_identity
import research.strategy_factory as factory_module


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

    @staticmethod
    def _factory_identity(config):
        """Capture the identity before orchestration mutates a ledger."""
        captured = []
        original = factory_module.experiment_identity

        def capture(**kwargs):
            identity = original(**kwargs)
            captured.append(identity)
            raise RuntimeError("stop after identity")

        with patch.object(factory_module, "_read_discovery_rows",
                          return_value=([], [], {}, [])), \
             patch.object(factory_module, "experiment_identity",
                          side_effect=capture):
            with unittest.TestCase().assertRaisesRegex(
                    RuntimeError, "stop after identity"):
                factory_module._run_factory(
                    [], runtime_config=config, _resources=[], strategies=1,
                    variants_per_strategy=2, workers=1)
        return captured[0]

    def test_runtime_risk_controls_are_bound_to_factory_identity(self):
        base = validate_config({
            "risk": {"stressed_cost_scenario_bps": 25.0,
                     "max_stressed_cost_to_risk_ratio": 1.0,
                     "max_position_notional_pct": 25.0},
        })
        changed_stress = validate_config({
            "risk": {"stressed_cost_scenario_bps": 50.0,
                     "max_stressed_cost_to_risk_ratio": 1.0,
                     "max_position_notional_pct": 25.0},
        })
        changed_ratio = validate_config({
            "risk": {"stressed_cost_scenario_bps": 25.0,
                     "max_stressed_cost_to_risk_ratio": .5,
                     "max_position_notional_pct": 25.0},
        })
        changed_sizing = validate_config({
            "risk": {"stressed_cost_scenario_bps": 25.0,
                     "max_stressed_cost_to_risk_ratio": 1.0,
                     "max_position_notional_pct": 10.0},
        })
        original = self._factory_identity(base)
        self.assertNotEqual(original["config_hash"],
                            self._factory_identity(changed_stress)["config_hash"])
        self.assertNotEqual(original["config_hash"],
                            self._factory_identity(changed_ratio)["config_hash"])
        self.assertNotEqual(original["config_hash"],
                            self._factory_identity(changed_sizing)["config_hash"])
        self.assertNotEqual(original["identity_hash"],
                            self._factory_identity(changed_stress)["identity_hash"])
        self.assertEqual(original["identity_hash"],
                         self._factory_identity(base)["identity_hash"])
        self.assertEqual(
            original["risk"]["stressed_cost_scenario_bps"], 25.0)


if __name__ == "__main__":
    unittest.main()
