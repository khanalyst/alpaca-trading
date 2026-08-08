import tempfile
import unittest
from pathlib import Path

from agent.staging import (StagingCapacityError, StagingNoveltyError,
                           StagingStore)
from research import shortlist, staged_review
from research.staged_lifecycle import (LifecyclePolicy,
                                       aggregate_mechanism_evidence)


def proposal(contract_id="crowded-carry", value=80.0):
    return {
        "contract_id": contract_id,
        "mechanism": ("Crowded leveraged traders must keep paying carry and "
                      "are forced to exit when continuation stalls."),
        "payer": ("The leveraged trader pays us while financing a crowded "
                  "position they cannot unwind without moving price."),
        "falsifier": ("The held-out crowded cell has nonpositive expectancy "
                      "after costs and adequate opportunity coverage."),
        "direction": "both",
        "conditions": [{
            "field": "funding_percentile_30", "op": ">=", "value": value,
        }],
    }


class BoundedStagingLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "staging.db"

    def test_active_contract_cap_applies_backpressure_and_reopens_after_retirement(self):
        store = StagingStore(self.path, max_active=2)
        store.register(proposal("one"), generation=0)
        store.register(proposal("two", 70), generation=0)
        with self.assertRaises(StagingCapacityError):
            store.register(proposal("three", 60), generation=0)
        self.assertTrue(store.capacity()["backpressured"])
        store.retire("one", "bounded lifecycle released the lane")
        store.register(proposal("three", 60), generation=1)
        self.assertEqual(store.capacity()["active"], 2)

    def test_strict_registration_rejects_cosmetic_id_and_threshold_novelty(self):
        store = StagingStore(self.path)
        root = store.register_bounded(proposal(), generation=0)
        conflicts = store.novelty_conflicts(proposal("renamed", 75))
        self.assertEqual(conflicts[0]["contract_id"], root.contract_id)
        with self.assertRaises(StagingNoveltyError):
            store.register_bounded(proposal("renamed", 75), generation=1)

    def test_register_batch_publishes_a_root_and_family_atomically(self):
        store = StagingStore(self.path, max_active=4)
        registered = store.register_batch(
            proposal("batch-root", 80),
            [proposal("batch-low", 70), proposal("batch-high", 60)],
            generation=3, now=10.0)

        self.assertEqual(
            [contract.contract_id for contract in registered],
            ["batch-root", "batch-low", "batch-high"])
        records = store.records()
        self.assertEqual(len(records), 3)
        root = next(record for record in records
                    if record["contract_id"] == "batch-root")
        self.assertIsNone(root["parent_contract_id"])
        self.assertEqual(
            {record["parent_contract_id"] for record in records
             if record["contract_id"] != "batch-root"},
            {"batch-root"})
        self.assertEqual(
            {record["mechanism_id"] for record in records},
            {root["mechanism_id"]})
        self.assertEqual(
            len({record["configuration_id"] for record in records}), 3)

    def test_register_batch_failure_leaves_no_partial_family(self):
        store = StagingStore(self.path, max_active=2)
        with self.assertRaises(StagingCapacityError):
            store.register_batch(
                proposal("batch-root", 80),
                [proposal("batch-child", 70), proposal("batch-child-two", 60)],
                generation=0)
        self.assertEqual(store.records(), [])

        store = StagingStore(self.path)
        with self.assertRaises(StagingNoveltyError):
            store.register_batch(
                proposal("batch-root", 80),
                [proposal("batch-a", 70), proposal("batch-b", 70)],
                generation=0)
        self.assertEqual(store.records(), [])

    def test_age_releases_an_unobserved_lane_without_claiming_falsification(self):
        store = StagingStore(self.path)
        store.register(proposal(), generation=0, now=0)

        class EmptyFindings:
            path = ":memory:"

            def paper_scopes(self):
                return []

        policy = LifecyclePolicy(
            no_observation_max_age_seconds=10,
            never_fired_max_age_seconds=20,
            starved_max_age_seconds=20)
        result = staged_review.review(
            store, EmptyFindings(), now=11, policy=policy)
        self.assertEqual(result["retired"], ["crowded-carry"])
        self.assertEqual(result["verdicts"][0]["code"],
                         staged_review.INACTIVE_EXPIRED)
        self.assertIn("without claiming", result["verdicts"][0]["explanation"])

    def test_starvation_expires_a_configuration_but_is_not_market_evidence(self):
        candidate = shortlist.Candidate(
            variant_id="staged.x", scope_key="s", trades=0, mean_r=0,
            ci_low=0, ci_high=0, label=shortlist.NO_EVIDENCE,
            starved_decisions=90, declined_decisions=10,
            fit_mean_r=None, confirmation_mean_r=None)
        policy = LifecyclePolicy(starved_max_age_seconds=10)
        code, why = staged_review.verdict_for(
            candidate, age_seconds=11, policy=policy)
        self.assertEqual(code, staged_review.STARVED_EXPIRED)
        self.assertIn("does not falsify", why)

    def test_mechanism_aggregation_does_not_equate_one_failed_configuration(self):
        aggregate = aggregate_mechanism_evidence([
            {"contract_id": "a", "configuration_id": "cfg-a",
             "code": staged_review.NEGATIVE_EXPECTANCY},
            {"contract_id": "b", "configuration_id": "cfg-b",
             "code": staged_review.COLLECTING},
        ])
        self.assertEqual(aggregate["code"], staged_review.COLLECTING)
        self.assertEqual(aggregate["failed_configuration_ids"], ["cfg-a"])

    def test_support_requires_all_scopes_for_that_configuration_to_agree(self):
        aggregate = aggregate_mechanism_evidence([
            {"contract_id": "a", "configuration_id": "cfg-a",
             "code": staged_review.SUPPORTED},
            {"contract_id": "a", "configuration_id": "cfg-a",
             "code": staged_review.NEGATIVE_EXPECTANCY},
        ])
        self.assertEqual(aggregate["code"], staged_review.COLLECTING)
        self.assertEqual(aggregate["supported_configuration_ids"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
