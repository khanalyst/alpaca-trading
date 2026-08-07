import copy
import tempfile
import unittest
from pathlib import Path

from agent.staging import StagingStore
from research.staged_handoff import (HandoffError, build_registration_handoff,
                                     render_registration_handoff,
                                     validate_registration_handoff)
from research.staged_refinement import (RefinementPolicy, plan_refinement,
                                        register_refinement,
                                        stage_initial_neighborhood)


def proposal():
    return {
        "contract_id": "liquidation-carry",
        "mechanism": ("Crowded leveraged traders must keep paying carry and "
                      "are forced to exit when continuation stalls."),
        "payer": ("The leveraged trader pays us while financing a crowded "
                  "position they cannot unwind without moving price."),
        "falsifier": ("The held-out crowded cell has nonpositive expectancy "
                      "after costs and adequate opportunity coverage."),
        "direction": "both",
        "conditions": [{
            "field": "funding_percentile_30", "op": ">=", "value": 80.0,
        }],
    }


class StagedRefinementAndHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StagingStore(Path(self.tmp.name) / "staging.db")

    def test_initial_neighborhood_is_small_deterministic_and_one_dimensional(self):
        first = stage_initial_neighborhood(
            self.store, proposal(), generation=0, now=1)
        self.assertEqual(first["configuration_count"], 3)
        records = self.store.mechanism_records(first["mechanism_id"])
        self.assertEqual(len({row["mechanism_id"] for row in records}), 1)
        self.assertEqual(len({row["configuration_id"] for row in records}), 3)
        values = [row["proposal"]["conditions"][0]["value"] for row in records]
        self.assertEqual(values, [80.0, 70.0, 90.0])

    def test_failure_refinement_stops_at_attempt_and_family_budgets(self):
        family = stage_initial_neighborhood(
            self.store, proposal(), generation=0, now=1)
        root = family["root_contract_id"]
        self.store.retire(root, "NEGATIVE_EXPECTANCY: failed configuration", now=2)
        policy = RefinementPolicy(
            max_attempts=1, max_variants_per_attempt=2,
            max_configurations_per_mechanism=5)
        plan = plan_refinement(
            self.store, root, "NEGATIVE_EXPECTANCY", policy=policy)
        result = register_refinement(self.store, plan, generation=1, now=3)
        self.assertEqual(result["status"], "REGISTERED")
        self.assertEqual(len(result["registered_contract_ids"]), 2)
        child = result["registered_contract_ids"][0]
        self.store.retire(child, "NEGATIVE_EXPECTANCY: failed again", now=4)
        exhausted = plan_refinement(
            self.store, child, "NEGATIVE_EXPECTANCY", policy=policy)
        self.assertEqual(exhausted["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(exhausted["proposals"], [])

    def test_supported_handoff_is_content_addressed_and_non_authorizing(self):
        family = stage_initial_neighborhood(
            self.store, proposal(), generation=0, now=1)
        record = self.store.record(family["root_contract_id"])
        review = {
            "reviewed_ts": 20.0,
            "mechanisms": [{
                "mechanism_id": family["mechanism_id"],
                "mechanism": record["proposal"]["mechanism"],
                "payer": record["proposal"]["payer"],
                "code": "SUPPORTED",
                "supported_configuration_ids": [record["configuration_id"]],
            }],
            "verdicts": [{
                "contract_id": record["contract_id"],
                "mechanism_id": family["mechanism_id"],
                "configuration_id": record["configuration_id"],
                "variant_id": "staged.liquidation-carry", "scope_key": "s",
                "stage": "CONFIRM", "code": "SUPPORTED", "trades": 150,
                "mean_r": 0.25, "firing_rate": 0.1, "coverage_pct": 99,
                "pair_coverage_pct": 98, "explanation": "held out support",
            }],
        }
        handoff = build_registration_handoff(
            self.store, review, family["mechanism_id"])
        self.assertEqual(handoff["authority"]["approval_status"],
                         "PENDING_HUMAN_REVIEW")
        self.assertFalse(handoff["requested_action"]["registry_mutation_performed"])
        self.assertEqual(validate_registration_handoff(handoff), handoff)
        self.assertIn(handoff["handoff_id"], render_registration_handoff(handoff))
        forged = copy.deepcopy(handoff)
        forged["authority"]["live_eligible"] = True
        with self.assertRaises(HandoffError):
            validate_registration_handoff(forged)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

