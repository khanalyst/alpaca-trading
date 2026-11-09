"""Research lanes can price with per_symbol_v2 instead of static_v1's 17 bps."""

from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from agent.config import ConfigError, validate_config
from agent.contracts.rule import validate_rule_spec
from research.costs import (CostError, PerSymbolCostModel, RESEARCH_COST_MODEL_ENV,
                            apply_research_cost_model)
from research.factory_core import simulate_account
from research.quote_costs import cost_resolver_setup
from tests.research import test_costs_versions as fixtures

ROOT = Path(__file__).resolve().parents[2]


def shipped():
    return yaml.safe_load((ROOT / "config.yaml").read_text())


class ResearchCostModelTests(unittest.TestCase):
    def test_absent_key_leaves_the_normalized_costs_unchanged(self):
        base = validate_config(shipped())
        self.assertNotIn("research_model", base["costs"])
        cfg = shipped()
        cfg["costs"]["research_model"] = "per_symbol_v2"
        chosen = validate_config(cfg)
        self.assertEqual(chosen["costs"]["research_model"], "per_symbol_v2")
        self.assertEqual({k: v for k, v in chosen["costs"].items() if k != "research_model"},
                         base["costs"])

    def test_an_unknown_model_is_refused(self):
        cfg = shipped()
        cfg["costs"]["research_model"] = "free"
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_the_environment_sets_it_and_an_explicit_value_wins(self):
        base = validate_config(shipped())
        self.assertEqual(apply_research_cost_model(base, {}), base)
        applied = apply_research_cost_model(base, {RESEARCH_COST_MODEL_ENV: "per_symbol_v2"})
        self.assertEqual(applied["costs"]["research_model"], "per_symbol_v2")
        explicit = {**base, "costs": {**base["costs"], "research_model": "static_v1"}}
        kept = apply_research_cost_model(explicit, {RESEARCH_COST_MODEL_ENV: "per_symbol_v2"})
        self.assertEqual(kept["costs"]["research_model"], "static_v1")
        with self.assertRaises(CostError):
            apply_research_cost_model(base, {RESEARCH_COST_MODEL_ENV: "zero"})

    def test_the_resolver_is_per_symbol_for_equity_only(self):
        cfg = validate_config(shipped())
        self.assertIsNone(cost_resolver_setup(cfg).resolver)
        cfg = apply_research_cost_model(cfg, {RESEARCH_COST_MODEL_ENV: "per_symbol_v2"})
        self.assertIsNotNone(cost_resolver_setup(cfg, vehicle="equity").resolver)
        self.assertIsNone(cost_resolver_setup(cfg, vehicle="option").resolver)

    def test_per_symbol_rows_carry_their_expected_round_trip(self):
        spec = validate_rule_spec(fixtures.SPEC)
        quotes = [fixtures._quote(4, 100.79, 100.81), fixtures._quote(8, 100.79, 100.81)]
        row = simulate_account(fixtures._bars(fixtures.TIME_EXIT), [], spec, vehicle="equity",
                               account_id="per-symbol", risk_pct=.05,
                               policy=fixtures.PERMISSIVE, quotes=quotes,
                               cost_resolver=PerSymbolCostModel().resolver())["rows"][0]
        self.assertFalse(row["no_trade"])
        expected = PerSymbolCostModel().round_trip(
            "SPY", row["entry_reference"], shares=float(row["quantity"]))["total_bps"]
        self.assertAlmostEqual(row["expected_round_trip_bps"], expected, places=6)
        self.assertLess(row["expected_round_trip_bps"], 5.0)
        static = simulate_account(fixtures._bars(fixtures.TIME_EXIT), [], spec, vehicle="equity",
                                  account_id="static", risk_pct=.05,
                                  policy=fixtures.PERMISSIVE, quotes=quotes)["rows"][0]
        self.assertNotIn("expected_round_trip_bps", static)

    def test_research_cli_loader_applies_the_environment(self):
        spec = importlib.util.spec_from_file_location("research_cli_under_test",
                                                      ROOT / "research.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Args:
            agent_config = str(ROOT / "config.yaml")
        with patch.dict(os.environ, {RESEARCH_COST_MODEL_ENV: "per_symbol_v2"}):
            self.assertEqual(module._agent_config(Args())["costs"]["research_model"],
                             "per_symbol_v2")
        with patch.dict(os.environ, {RESEARCH_COST_MODEL_ENV: ""}):
            self.assertNotIn("research_model", module._agent_config(Args())["costs"])


if __name__ == "__main__":
    unittest.main()
