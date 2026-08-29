"""Model-assisted historical diagnostics remain isolated from authorization."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.contracts.rule import RULE_SCHEMA_V2, rule_variant_id, validate_rule_spec
from research.llm_strategy import DISCOVERY_SCHEMA, TUNING_SCHEMA, ProposalResult
import research.strategy_factory as factory_module
from research.strategy_factory import TunedVariant, run_factory

from .test_factory_end_to_end import edge_corpus


class DiagnosticLLMEpochTests(unittest.TestCase):
    def test_root_variant_reuses_diagnostic_replay_and_fit(self):
        calls = {"simulate": [], "fit": []}

        real_simulate = factory_module.simulate_account
        real_measure = factory_module.measure_fit_diagnostics

        def tracked_simulate(*args, **kwargs):
            calls["simulate"].append({
                "account_id": kwargs["account_id"],
                "variant_id": rule_variant_id(args[2]),
            })
            return real_simulate(*args, **kwargs)

        def tracked_measure(*args, **kwargs):
            calls["fit"].append(rule_variant_id(args[1]))
            return real_measure(*args, **kwargs)

        def selected_root_and_variant(hypothesis, _diagnosis, **_kwargs):
            root = validate_rule_spec(hypothesis["rule_spec"])
            variant = validate_rule_spec({**root, "target_r": 2.5})
            return [
                TunedVariant(root, "retain root", "deterministic"),
                TunedVariant(variant, "test independent variant", "deterministic"),
            ], None

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(factory_module, "_tuned_variants",
                             side_effect=selected_root_and_variant), \
                patch.object(factory_module, "simulate_account",
                             side_effect=tracked_simulate), \
                patch.object(factory_module, "measure_fit_diagnostics",
                             side_effect=tracked_measure):
            result = run_factory(
                edge_corpus(2), db_path=Path(directory) / "unused.sqlite3",
                vehicle="equity", strategies=1, variants_per_strategy=2,
                workers=1, diagnostic_only=True)

        report = result["reports"][0]
        root_id = report["variant_id"]
        root_variant = next(item for item in report["variants"]
                            if item["variant_id"] == root_id)
        other_variants = [item for item in report["variants"]
                          if item["variant_id"] != root_id]
        self.assertEqual(len(other_variants), 1)
        other_id = other_variants[0]["variant_id"]

        # One context replay, one selected-root replay, and one independent
        # non-root replay: the selected root itself is not replayed a third
        # time by the variant loop.
        self.assertEqual(len(calls["simulate"]), 3)
        self.assertEqual(len(calls["fit"]), 3)
        self.assertEqual(
            [item["variant_id"] for item in calls["simulate"]].count(root_id), 2)
        self.assertEqual(
            [item["variant_id"] for item in calls["simulate"]].count(other_id), 1)
        self.assertEqual(calls["fit"].count(root_id), 2)
        self.assertEqual(calls["fit"].count(other_id), 1)
        expected_variant_account = f"diagnostic:{report['hypothesis_id']}:{root_id}"
        self.assertNotIn(
            expected_variant_account,
            [item["account_id"] for item in calls["simulate"]])

        # The reused root preserves every diagnostic metric and row while its
        # copied envelope keeps the established per-variant account identity.
        root_metrics = {
            key: value for key, value in report["root_diagnostic"].items()
            if key not in {"authorizing", "diagnostic_only"}
        }
        self.assertEqual(root_variant["diagnostic"], root_metrics)
        self.assertEqual(root_variant["account"]["rows"],
                         report["account"]["rows"])
        self.assertEqual(root_variant["account"]["account_id"],
                         expected_variant_account)
        self.assertEqual(report["account"]["account_id"],
                         f"diagnostic-root:{report['hypothesis_id']}")
        self.assertFalse(result["authorizing"])
        self.assertTrue(result["diagnostic_only"])

    def test_model_discovery_and_tuning_run_without_opening_ledgers(self):
        discovered = validate_rule_spec({
            "schema": RULE_SCHEMA_V2,
            "family": "volatility_breakout",
            "entry_after_minutes": 90,
            "entry_before_minutes": 300,
            "confirmations": ["volume"],
            "max_atr_bps": 75.0,
        })

        class Adapter:
            def __init__(self):
                self.calls = []

            def discover(self, *, vehicle, slot, context):
                self.calls.append(("discovery", vehicle, slot, context))
                return ProposalResult(
                    True, schema=DISCOVERY_SCHEMA, rule_spec=discovered,
                    variant_id=rule_variant_id(discovered), spec_id="diagnostic",
                    thesis="A diagnostic compression breakout may be reachable.",
                    evidence={"model": "gpt-5.6-terra"})

            def tune(self, **kwargs):
                self.calls.append(("tuning", kwargs["vehicle"], kwargs["slot"],
                                   kwargs["diagnosis"]))
                return ProposalResult(
                    False, schema=TUNING_SCHEMA,
                    error="test adapter requests deterministic fallback",
                    evidence={"model": "gpt-5.6-terra"})

            def _budget_evidence(self):
                return {"calls_used": len(self.calls), "max_total_calls": 8}

        adapter = Adapter()
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "must-not-exist.sqlite3"
            result = run_factory(
                edge_corpus(2), db_path=ledger, vehicle="equity",
                strategies=1, variants_per_strategy=2, workers=1,
                strategy_llm={
                    "enabled": True, "provider": "openai",
                    "model": "gpt-5.6-terra", "max_total_calls": 8,
                },
                proposal_adapter=adapter, diagnostic_only=True)

        self.assertFalse(ledger.exists())
        self.assertTrue(result["diagnostic_only"])
        self.assertFalse(result["authorizing"])
        self.assertEqual(result["bar_coverage"]["schema"], "bar-coverage.v1")
        self.assertIn("by_symbol_session", result["bar_coverage"])
        self.assertEqual(result["strategy_llm"]["model"], "gpt-5.6-terra")
        self.assertEqual(result["strategy_llm"]["calls_used"], 2)
        self.assertEqual(result["llm_call_evidence"]["attempted"], 2)
        self.assertEqual(result["llm_call_evidence"]["succeeded"], 1)
        self.assertEqual(result["reports"][0]["hypothesis_source"],
                         "llm_discovery")
        self.assertEqual(result["reports"][0]["rule_spec"], discovered)
        self.assertEqual(len(result["reports"][0]["variants"]), 2)
        self.assertNotIn("bar_coverage", result["reports"][0]["account"])
        self.assertNotIn(
            "bar_coverage",
            result["reports"][0]["diagnostic"]["fit_diagnostics"])
        self.assertTrue(all(
            report["diagnostic_only"] and not report["authorizing"]
            for report in result["reports"]))
        self.assertEqual(result["authorization"]["eligible"], [])
        self.assertFalse(result["authorization"]["paper_promotion_allowed"])


if __name__ == "__main__":
    unittest.main()
