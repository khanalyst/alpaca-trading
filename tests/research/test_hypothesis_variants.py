import tempfile
import unittest

from agent.variants import (adaptive_hypothesis_variant, hypothesis_variants,
                            preregistered_variants)
from agent.brain import parse_decisions
from agent import shadow
from research.findings import FindingsStore, _connect
from tests.helpers import valid_config


class HypothesisVariantTests(unittest.TestCase):
    def test_in_prompt_settings_have_deterministic_first_class_identity(self):
        variants = hypothesis_variants("momentum", "phase1-v3")
        self.assertEqual(
            [v.variant_id for v in variants],
            sorted(v.variant_id for v in variants))
        self.assertTrue(all(v.hypothesis_id and v.hypothesis_params
                            for v in variants))

    def test_yaml_settings_materialize_without_promotion(self):
        variants = preregistered_variants("momentum", "phase1-v2")
        self.assertEqual(
            [v.variant_id for v in variants],
            ["momentum.prereg.registered", "momentum.prereg.target_2r",
             "momentum.prereg.wider_stop"])
        self.assertTrue(all(v.status == "candidate" for v in variants))

    def test_store_persists_exact_hypothesis_metadata_and_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FindingsStore(f"{directory}/findings.db")
            variant = hypothesis_variants("momentum", "phase1-v3")[0]
            store.register(variant)
            row = store.variant(variant.variant_id)
            self.assertEqual(row["hypothesis_id"], variant.hypothesis_id)
            self.assertIn("hypothesis_params_json", row)

            store.propose_numeric_setting(
                "momentum", "volume-thrust", "adaptive", 1.7, "run-1",
                minimum=1.0, maximum=3.0, now=10.0)
            with self.assertRaisesRegex(ValueError, "lock"):
                store.propose_numeric_setting(
                    "momentum", "volume-thrust", "adaptive", 1.8,
                    "run-2", minimum=1.0, maximum=3.0, now=11.0)
            with self.assertRaisesRegex(ValueError, "duplicate"):
                store.propose_numeric_setting(
                    "momentum", "volume-thrust", "adaptive", 1.7, "run-1",
                    minimum=1.0, maximum=3.0, now=20.0)
            with self.assertRaisesRegex(ValueError, "lock"):
                store.propose_numeric_setting(
                    "momentum", "volume-thrust", "another_setting", 2.1,
                    "run-1", minimum=1.0, maximum=3.0, now=21.0)

    def test_forward_axis_accepts_hypothesis_metadata_and_rejects_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FindingsStore(f"{directory}/findings.db")
            variants_for_hypothesis = hypothesis_variants(
                "momentum", "phase1-v3")
            selected = [variant for variant in variants_for_hypothesis
                        if variant.hypothesis_id == "volume-thrust"]
            baseline = next(variant for variant in selected
                            if variant.variant_id.endswith(".registered"))
            setting = next(variant for variant in selected
                           if variant.variant_id.endswith(".lower_participation"))
            for variant in (baseline, setting):
                store.register(variant)

            axis = ["hypothesis.volume-thrust.min_relative_volume"]
            with _connect(store.path) as conn:
                self.assertEqual(
                    FindingsStore._validate_forward_axis(
                        conn, "momentum", axis, baseline.variant_id,
                        [setting.variant_id], {}),
                    axis)
                with self.assertRaisesRegex(ValueError, "duplicates"):
                    FindingsStore._validate_forward_axis(
                        conn, "momentum", axis, baseline.variant_id,
                        [baseline.variant_id], {})

    def test_parser_accepts_only_registered_bounded_numeric_proposals(self):
        parsed = parse_decisions(
            '{"decisions":[],"proposal":{"hypothesis_id":"volume-thrust",'
            '"setting_id":"registered","value":1.7,'
            '"reasoning":"test the exact participation threshold"}}')
        self.assertEqual(parsed[-1]["action"], "research_proposal")
        self.assertEqual(parsed[-1]["value"], 1.7)
        self.assertEqual(parse_decisions(
            '{"decisions":[],"proposal":{"hypothesis_id":"nope",'
            '"setting_id":"registered","value":1.7}}'), [])
        self.assertEqual(parse_decisions(
            '{"decisions":[],"proposal":{"hypothesis_id":"volume-thrust",'
            '"setting_id":"registered","value":1001}}'), [])

    def test_shadow_build_enrolls_generated_variants_only_when_enabled(self):
        cfg = valid_config()
        cfg["research"] = {
            "shadow_enabled": True, "shadow_variants": ["*"],
            "shadow_budget_ms": 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            store = FindingsStore(f"{directory}/findings.db")
            evaluator = shadow.build(cfg, {}, store=store)
            self.assertTrue(any(".hyp." in item
                                for item in evaluator.variant_ids))
            cfg["research"]["shadow_enabled"] = False
            self.assertIsNone(shadow.build(cfg, {}, store=store))

    def test_adaptive_variant_identity_carries_exact_params_into_shadow(self):
        cfg = valid_config()
        cfg["research"] = {"shadow_enabled": True, "shadow_variants": [],
                            "shadow_budget_ms": 0}
        variant = adaptive_hypothesis_variant(
            "momentum", "phase1-v3", "volume-thrust", "registered", 2.25)
        self.assertEqual(variant.hypothesis_params["min_relative_volume"], 2.25)
        with tempfile.TemporaryDirectory() as directory:
            evaluator = shadow.ShadowEvaluator([variant], cfg,
                                                store=FindingsStore(
                                                    f"{directory}/f.db"))
            row = {"price": 100, "signal_ts": 1, "atr_1h_pct": 1,
                   "swing_low_pct": 1, "mom_1h_pct": .5,
                   "relative_volume_1h": 2.0}
            decision = {"action": "open", "symbol": "BTC/USDT:USDT",
                        "direction": "long", "setup_type": "other",
                        "hypothesis_id": "volume-thrust",
                        "invalidation_anchor": "structure",
                        "exit_policy": "fixed_rr"}
            plan, why = __import__("agent.strategy", fromlist=["build_setup_plan"]).build_setup_plan(
                decision, row, cfg, hypothesis_params=variant.hypothesis_params)
            self.assertIsNone(plan)
            self.assertIn("below 2.25", why)


if __name__ == "__main__":
    unittest.main()
