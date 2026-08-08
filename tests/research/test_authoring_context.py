import json
import unittest

from research.authoring import build_request
from research.authoring_context import build_evidence_context


class EvidenceStore:
    def paper_scopes(self):
        return ["scope-a"]

    def variants(self):
        return [{"variant_id": "candidate-1"}, {"variant_id": "baseline-1"}]

    def paper_decisions_for(self, scope, variant):
        if variant != "candidate-1":
            return []
        return [
            {"decision_id": "d1", "proposal_id": "p1", "paper_trade_id": "t1",
             "symbol": "BTC", "direction": "long", "setup_type": "reversal",
             "decision_outcome": "PROPOSED", "reason": None},
            {"decision_id": "d2", "proposal_id": "p2", "paper_trade_id": None,
             "symbol": "BTC", "direction": "short", "setup_type": "reversal",
             "decision_outcome": "VETOED", "reason": "data missing for candle"},
            {"decision_id": "d3", "proposal_id": "p3", "paper_trade_id": None,
             "symbol": "BTC", "direction": "long", "setup_type": "reversal",
             "decision_outcome": "VETOED", "reason": "confidence below floor"},
            {"decision_id": "d4", "proposal_id": "p4", "paper_trade_id": None,
             "symbol": "BTC", "direction": "short", "setup_type": "reversal",
             "decision_outcome": "VETOED", "reason": "data missing",
             "inference_eligible": False},
            {"decision_id": "d5", "proposal_id": "p5", "paper_trade_id": "t2",
             "symbol": "BTC", "direction": "long", "setup_type": "reversal",
             "decision_outcome": "PROPOSED", "reason": None,
             "inference_eligible": False},
        ]

    def paper_trades_for(self, scope, variant):
        if variant != "candidate-1":
            return []
        return [{
            "trade_id": "t1", "proposal_id": "p1", "status": "CLOSED",
            "result": "target", "valid_for_inference": 1,
            "r_multiple": 0.5, "net_pnl_usd": 12.0,
        }, {
            "trade_id": "t2", "proposal_id": "p5", "status": "CLOSED",
            "result": "target", "valid_for_inference": 1,
            "r_multiple": 99.0, "net_pnl_usd": 999.0,
        }]

    def experiment_outcomes(self, scope=None):
        return [{
            "outcome_id": "o1", "scope_key": "scope-a",
            "strategy_id": "momentum", "candidate_variant_id": "candidate-1",
            "baseline_variant_id": "baseline-1", "verdict": "INCONCLUSIVE",
            "created_ts": 10.0,
            "payload": {
                "reasons": [
                    {"code": "BASELINE_DELTA_NOT_ESTABLISHED"},
                    {"code": "TWO_SEGMENTS_UNAVAILABLE"},
                ],
                "candidate": {"variant_id": "candidate-1", "decision_count": 3,
                              "proposed_decisions": 1, "vetoed_decisions": 2,
                              "closes": 1, "trade_r_metrics": {"expectancy_r": 0.5},
                              "decisions": [
                                  {"decision_ts": 1, "decision_outcome": "PROPOSED",
                                   "trade_status": "CLOSED", "trade_valid_for_inference": 1,
                                   "trade_r_multiple": 0.5},
                                  {"decision_ts": 6, "decision_outcome": "VETOED"},
                                  {"decision_ts": 2, "decision_outcome": "PROPOSED",
                                   "trade_status": "CLOSED",
                                   "trade_valid_for_inference": 1,
                                   "trade_r_multiple": 99.0,
                                   "inference_eligible": False},
                                  {"decision_ts": 7, "decision_outcome": "VETOED",
                                   "inference_eligible": False},
                              ]},
                "baseline": {"variant_id": "baseline-1", "decision_count": 3,
                              "trade_r_metrics": {"expectancy_r": 0.1}},
                "data_window": {"split_cutoff_ts": 5.0},
                "paired": {
                    "fit": {"paired_n": 2, "pair_coverage_pct": 100,
                            "interval": {"point": 0.2, "low": -0.1, "high": 0.4}},
                    "confirm": {"paired_n": 1, "pair_coverage_pct": 100,
                                "interval": {"point": 0.1, "low": -0.2, "high": 0.3}},
                },
            },
        }]

    def qualification_events(self, variant_id):
        if variant_id != "candidate-1":
            return []
        return [{
            "event_id": "q1", "scope_key": "scope-a", "status": "QUALIFIED",
            "source_analysis_id": "a1",
            "detail": {"family": {"family_n": 3, "alpha": 0.05,
                                   "p_adjusted": 0.02, "significant": True,
                                   "axes": ["risk", "exit"]}},
        }]

    def analysis(self, analysis_id):
        return {"analysis_id": analysis_id, "payload": {
            "evidence": {"split": {
                "survives": True,
                "reason": "confirm window is compatible",
                "fit": {"n": 2, "expectancy_r": 0.2, "ci_low": -0.1,
                        "ci_high": 0.4},
                "confirm": {"n": 1, "expectancy_r": 0.1, "ci_low": -0.2,
                            "ci_high": 0.3},
                "fit_regime": {"median_vol_ratio": 0.9, "comparable": True},
                "confirm_regime": {"median_vol_ratio": 1.0, "comparable": True},
            }},
        }}


class AuthoringEvidenceContextTests(unittest.TestCase):
    def test_context_is_derived_from_persisted_rows_and_bounded(self):
        context = build_evidence_context(EvidenceStore())
        # JSON-safe and deterministic are prompt invariants.
        encoded = json.dumps(context, sort_keys=True, allow_nan=False)
        self.assertIn("conditional_returns", context)
        self.assertIn("firing_rates", context)
        self.assertIn("out_of_sample", context)
        self.assertIn("family_context", context)
        self.assertNotIn("feature_summary", context)
        self.assertNotIn("regime_summary", context)

        rates = context["firing_rates"][0]
        self.assertEqual(rates["fired"], 1)
        self.assertEqual(rates["declined"], 2)
        self.assertEqual(rates["missing_data"], 1)
        self.assertAlmostEqual(rates["missing_data_rate_pct"], 100 / 3)
        conditional = [row for row in context["conditional_returns"]
                       if row["dimension"] == "direction" and row["value"] == "long"][0]
        self.assertEqual(conditional["closed"], 1)
        self.assertEqual(conditional["mean_r"], 0.5)
        persisted_oos = [row for row in context["out_of_sample"]
                         if row.get("outcome_id") == "o1"][0]
        self.assertEqual(persisted_oos["fit"]["paired_n"], 2)
        segments = [row for row in context["segment_context"]
                    if row["arm"] == "candidate"]
        self.assertEqual({row["segment"] for row in segments}, {"fit", "confirm"})
        fit = [row for row in segments if row["segment"] == "fit"][0]
        confirm = [row for row in segments if row["segment"] == "confirm"][0]
        self.assertEqual(
            (fit["opportunities"], fit["fired"], fit["closed"], fit["mean_r"]),
            (1, 1, 1, 0.5))
        self.assertEqual(
            (confirm["opportunities"], confirm["declined"], confirm["closed"]),
            (1, 1, 0))
        self.assertEqual(len(context["regime_context"]), 2)
        self.assertTrue(any(row["analysis_id"] == "a1"
                            for row in context["out_of_sample"]))
        self.assertEqual(context["family_context"][0]["family"]["family_n"], 3)

    def test_request_carries_evidence_without_changing_registration_inputs(self):
        class EmptyStore:
            def active(self):
                return []

            def generation(self):
                return 4

        evidence = {"schema": "authoring_evidence.v1", "firing_rates": [{"n": 2}]}
        request = build_request(EmptyStore(), evidence=evidence)
        self.assertEqual(request["generation"], 5)
        self.assertEqual(request["evidence"], evidence)
        self.assertEqual(request["observable_fields"], sorted(request["observable_fields"]))

    def test_supplied_context_is_still_bounded_and_json_safe(self):
        supplied = {
            "schema": "authoring_evidence.v1",
            "conditional_returns": [
                {"scope_key": str(i), "value": "x" * 10_000}
                for i in range(500)
            ],
        }
        context = build_evidence_context(None, history={"evidence": supplied})
        self.assertLessEqual(len(context["conditional_returns"]), 96)
        self.assertLessEqual(len(context["conditional_returns"][0]["value"]), 512)
        json.dumps(context, sort_keys=True, allow_nan=False)

    def test_immutable_outcome_rows_supply_rates_when_paper_projection_is_absent(self):
        class OutcomeOnlyStore:
            def experiment_outcomes(self, scope=None):
                return [{
                    "outcome_id": "o-only", "scope_key": "scope-b",
                    "candidate_variant_id": "candidate-only", "verdict": "FAILED",
                    "payload": {"candidate": {
                        "variant_id": "candidate-only",
                        "decisions": [
                            {"symbol": "ETH", "direction": "short",
                             "setup_type": "carry", "decision_outcome": "PROPOSED",
                             "trade_status": "CLOSED", "trade_valid_for_inference": 1,
                             "trade_r_multiple": -0.25},
                            {"symbol": "ETH", "direction": "short",
                             "setup_type": "carry", "decision_outcome": "VETOED",
                             "reason": "data missing"},
                            {"symbol": "ETH", "direction": "short",
                             "setup_type": "carry", "decision_outcome": "PROPOSED",
                             "trade_status": "CLOSED", "trade_valid_for_inference": 1,
                             "trade_r_multiple": 99.0,
                             "inference_eligible": False},
                            {"symbol": "ETH", "direction": "short",
                             "setup_type": "carry", "decision_outcome": "VETOED",
                             "reason": "data missing", "inference_eligible": False},
                        ]}, "reasons": []},
                }]

            def variants(self):
                return []

        context = build_evidence_context(OutcomeOnlyStore())
        self.assertEqual(context["firing_rates"][0]["fired"], 1)
        self.assertEqual(context["firing_rates"][0]["missing_data"], 1)
        self.assertEqual(context["conditional_returns"][0]["closed"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
