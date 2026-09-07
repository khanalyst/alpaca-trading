from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.mechanism_cohort import COHORT_ID, mechanism_cohort
from research.strategy_factory import FactoryError, run_factory
from .test_factory_end_to_end import edge_corpus


class MechanismCohortTests(unittest.TestCase):
    def test_manifest_is_frozen_and_arms_are_distinct(self):
        manifest = mechanism_cohort()
        self.assertEqual(manifest["manifest_hash"],
                         "13c2b58b22cd5f669d26d9f7611c4067304f6390b9e1666bf83821aa0393abe6")
        arms = [arm for family in manifest["families"] for arm in family["arms"]]
        self.assertEqual(len(arms), 12)
        self.assertEqual(len({arm["variant_id"] for arm in arms}), 12)

    def test_frozen_comparison_runs_real_replay_without_llm_or_proofs(self):
        progress = []
        with tempfile.TemporaryDirectory() as directory, \
                patch("research.strategy_factory._tuned_variants", side_effect=AssertionError("no tuning")), \
                patch("research.strategy_factory._adapter", side_effect=AssertionError("no model")):
            ledger = Path(directory) / "no-ledger.sqlite3"
            result = run_factory(edge_corpus(2), db_path=ledger,
                diagnostic_only=True, cohort=COHORT_ID,
                strategy_llm={"enabled": True},
                progress_callback=lambda *event: progress.append(event))
            self.assertFalse(ledger.exists())
        self.assertEqual(result["strategies"], 3)
        self.assertEqual(result["variants"], 12)
        self.assertEqual(result["proofs"], [])
        self.assertFalse(result["authorizing"])
        self.assertFalse(result["strategy_llm"]["enabled"])
        self.assertEqual(progress[-1], ("evaluating", 12, 12))
        expected = {arm["variant_id"] for family in mechanism_cohort()["families"]
                    for arm in family["arms"]}
        self.assertEqual({arm["variant_id"] for report in result["reports"]
                          for arm in report["variants"]}, expected)
        self.assertTrue(all(arm["diagnostic"]["edge_proven"] is False
                            for report in result["reports"] for arm in report["variants"]))

    def test_cohort_cannot_skip_authorization_protocol_or_change_vehicle(self):
        with self.assertRaises(FactoryError):
            run_factory([], cohort=COHORT_ID)
        with self.assertRaises(FactoryError):
            run_factory(edge_corpus(1), cohort=COHORT_ID,
                        vehicle="option", diagnostic_only=True)


if __name__ == "__main__":
    unittest.main()
