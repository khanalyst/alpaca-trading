"""Model-assisted historical diagnostics remain isolated from authorization."""

from pathlib import Path
import tempfile
import unittest

from agent.contracts.rule import RULE_SCHEMA_V2, rule_variant_id, validate_rule_spec
from research.llm_strategy import DISCOVERY_SCHEMA, TUNING_SCHEMA, ProposalResult
from research.strategy_factory import run_factory

from .test_factory_end_to_end import edge_corpus


class DiagnosticLLMEpochTests(unittest.TestCase):
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
