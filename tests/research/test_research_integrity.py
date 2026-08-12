"""Regression tests for scheduled research boundaries and portability."""

from argparse import Namespace
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

from research.costs import CostModel

_CLI_PATH = Path(__file__).parents[2] / "research.py"
_CLI_SPEC = importlib.util.spec_from_file_location("research_cli_test", _CLI_PATH)
research_cli = importlib.util.module_from_spec(_CLI_SPEC)
assert _CLI_SPEC.loader is not None
_CLI_SPEC.loader.exec_module(research_cli)


class ScheduledResearchTests(unittest.TestCase):
    def test_factory_command_uses_validated_agent_cost_and_execution_config(self):
        agent_config = {
            "costs": {"spread_bps": 7.0, "slippage_bps": 4.0,
                      "fee_bps": 0.8},
            "execution": {"max_spread_bps": 20.0,
                          "max_slippage_bps": 15.0},
            "research": {"strategy_llm": {"enabled": False}},
        }
        args = Namespace(
            agent_config="config.yaml", config=None, data="market.jsonl",
            db="edge.sqlite3", vehicle="equity", strategies=1,
            variants=2, workers=1, starting_cash=100_000.0,
            min_trades=1, min_sessions=1, alpha=.05, max_generations=1)
        captured = {}

        def fake_run_factory(*_args, **kwargs):
            captured.update(kwargs)
            return {"results": []}

        with patch.object(research_cli, "_agent_config", return_value=agent_config), \
             patch.object(research_cli, "run_factory", side_effect=fake_run_factory), \
             patch.object(research_cli, "_emit_proofs", return_value=False), \
             patch.object(research_cli, "_write_factory_report", return_value=None):
            research_cli.cmd_factory_run(args)
        model = captured["costs"]
        self.assertIsInstance(model, CostModel)
        self.assertEqual(model.spread_bps, 7.0)
        self.assertEqual(model.slippage_bps, 4.0)
        self.assertEqual(model.max_spread_bps, 20.0)
        self.assertEqual(model.max_slippage_bps, 15.0)

    def test_research_cycle_script_has_no_bash4_mapfile_or_empty_quote_array(self):
        script = (Path(__file__).parents[2] / "deploy" / "research-cycle.sh").read_text()
        self.assertNotRegex(script, r"(?m)^\s*mapfile\b")
        self.assertNotIn('"${quote_flags[@]}"', script)
        # Bash 3.2 still supports the rest of this script's syntax.
        import subprocess
        result = subprocess.run(["bash", "-n", str(Path(__file__).parents[2] /
                                                     "deploy" / "research-cycle.sh")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
