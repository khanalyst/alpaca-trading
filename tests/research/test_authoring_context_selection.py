import unittest

from research.authoring_context import build_evidence_context


class AuthoringContextSelectionTests(unittest.TestCase):
    def test_negative_and_starved_staged_results_are_both_visible(self):
        context = build_evidence_context(None, history={
            "falsified": [{
                "contract_id": "lost", "code": "NEGATIVE_EXPECTANCY",
                "mechanism": "a losing forced-flow mechanism",
                "reason": "NEGATIVE_EXPECTANCY: lost after costs",
            }],
            "inconclusive": [{
                "contract_id": "starved", "code": "STARVED_OF_DATA",
                "mechanism": "an untested forced-flow mechanism",
                "reason": "STARVED_OF_DATA: inputs absent",
            }],
        })
        self.assertEqual(
            {row["code"] for row in context["staged_results"]},
            {"NEGATIVE_EXPECTANCY", "STARVED_OF_DATA"})

    def test_representative_outcomes_keep_an_older_negative_class(self):
        class OutcomeStore:
            def paper_scopes(self):
                return []

            def variants(self):
                return []

            def experiment_outcomes(self, scope=None):
                rows = [{
                    "outcome_id": "old-negative", "scope_key": "s",
                    "strategy_id": "momentum", "candidate_variant_id": "v-old",
                    "verdict": "FAILED", "created_ts": 1,
                    "payload": {"reasons": [{
                        "code": "NONPOSITIVE_AFTER_COST_EXPECTANCY",
                    }]},
                }]
                rows.extend({
                    "outcome_id": f"recent-{index}", "scope_key": "s",
                    "strategy_id": "momentum", "candidate_variant_id": "v-new",
                    "verdict": "INCONCLUSIVE", "created_ts": 100 + index,
                    "payload": {"reasons": [{
                        "code": "BASELINE_DELTA_NOT_ESTABLISHED",
                    }]},
                } for index in range(40))
                return rows

        context = build_evidence_context(OutcomeStore())
        self.assertTrue(any(
            row["outcome_id"] == "old-negative"
            for row in context["null_context"]))
        self.assertTrue(context["near_miss_context"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

